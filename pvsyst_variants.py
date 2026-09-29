"""Bounded, reversible edits to existing PVsyst variant component references."""
from __future__ import annotations

import hashlib
import os
import re
import threading
import uuid
from pathlib import Path

from pvsyst_components import ComponentStore, FIELD, MAX_PROJECT_BYTES

FIELDS = {"PAN": "PVModule", "OND": "GInverter", "BTR": "BatteryFile", "GEN": "GensetFile"}
STARTS = {"pvSubArray": "PVObject_=pvSubArray", "pvSystem": "PVObject_System=pvSystem"}


class VariantStore:
    def __init__(self, workspace: str | os.PathLike, components: ComponentStore,
                 lock: threading.RLock | None = None):
        self.workspace = Path(workspace).resolve(strict=True)
        self.components = components
        self.lock = lock or threading.RLock()

    def _root(self) -> Path:
        base = self.workspace / "Projects"
        if base.is_symlink() or base.resolve().parent != self.workspace:
            raise ValueError("Projects directory resolves outside workspace")
        return base.resolve()

    def _path(self, project: str, variant: str | None = None) -> Path:
        if (not isinstance(project, str) or len(project) > 180 or project.rstrip(" .") != project
                or not re.fullmatch(r'[^\\/:*?"<>|\x00-\x1f]+\.PRJ', project, re.I)):
            raise ValueError("Project must be a .PRJ filename without directories")
        if variant is not None and (not isinstance(variant, str) or len(variant) > 40
                                    or not re.fullmatch(r"VC[A-Za-z0-9]+", variant, re.I)):
            raise ValueError("Variant must be a VC identifier without directories")
        root = self._root()
        name = project if variant is None else f"{project[:-4]}.{variant.upper()}"
        path = root / name
        if path.is_symlink() or path.resolve().parent != root:
            raise ValueError("Symlink or escaping project files are not supported")
        return path

    @staticmethod
    def _read(path: Path) -> bytes:
        if not path.is_file():
            raise FileNotFoundError(path.name)
        if path.stat().st_size > MAX_PROJECT_BYTES:
            raise ValueError("Project or variant exceeds 32 MB")
        data = path.read_bytes()
        if len(data) > MAX_PROJECT_BYTES:
            raise ValueError("Project or variant exceeds 32 MB")
        return data

    def _check_project(self, project: str) -> None:
        data = self._read(self._path(project))
        try:
            lines = data.decode("utf-8-sig").splitlines()
        except UnicodeDecodeError as exc:
            raise ValueError("Project must be UTF-8 text") from exc
        if (not lines or lines[0].strip() != "PVObject_=pvProject"
                or lines[-1].strip() != "End of PVObject pvProject"
                or "\x00" in "".join(lines)):
            raise ValueError("Project is not a supported pvProject file")

    @staticmethod
    def _text(data: bytes) -> tuple[str, list[str]]:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Variant must be UTF-8 text") from exc
        if "\x00" in text or not text.startswith("PVObject_=pvVCalcul"):
            raise ValueError("Variant is not a supported pvVCalcul file")
        lines = text.splitlines(keepends=True)
        if not lines or lines[-1].strip() != "End of PVObject pvVCalcul":
            raise ValueError("Variant has no pvVCalcul closing tag")
        return text, lines

    @staticmethod
    def _sections(lines: list[str]) -> tuple[dict[int, tuple[int, int]], tuple[int, int] | None]:
        arrays = {}
        system = None
        active = None
        for index, line in enumerate(lines):
            value = line.strip()
            if value in STARTS.values():
                if active is not None:
                    raise ValueError("Nested component sections are not supported")
                active = ("pvSubArray" if value == STARTS["pvSubArray"] else "pvSystem", index)
            elif value in ("End of PVObject pvSubArray", "End of PVObject pvSystem"):
                kind = value.removeprefix("End of PVObject ")
                if active is None or active[0] != kind:
                    raise ValueError("Unmatched component section")
                bounds = (active[1], index)
                if kind == "pvSystem":
                    if system is not None:
                        raise ValueError("Multiple system sections")
                    system = bounds
                else:
                    matches = VariantStore._field_indices(lines, bounds, "SubArrayId")
                    if len(matches) != 1:
                        raise ValueError("Subarray must have one SubArrayId")
                    identifier = FIELD.fullmatch(lines[matches[0]].rstrip("\r\n"))[2]
                    if not identifier.isdigit() or int(identifier) in arrays:
                        raise ValueError("SubArrayId must be a unique positive integer")
                    if int(identifier) < 1:
                        raise ValueError("SubArrayId must be a unique positive integer")
                    arrays[int(identifier)] = bounds
                active = None
        if active is not None or not arrays:
            raise ValueError("Unclosed section or missing subarray")
        return arrays, system

    @staticmethod
    def _field_indices(lines: list[str], bounds: tuple[int, int], key: str) -> list[int]:
        return [index for index in range(bounds[0] + 1, bounds[1])
                if (match := FIELD.fullmatch(lines[index].rstrip("\r\n"))) and match[1] == key]

    @staticmethod
    def _check_hash(data: bytes, expected: str) -> None:
        if (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected)
                or hashlib.sha256(data).hexdigest() != expected.lower()):
            raise ValueError("Variant changed since inspection; provide current sha256")

    def inspect(self, project: str, variant: str) -> dict:
        with self.lock:
            self._check_project(project)
            data = self._read(self._path(project, variant))
            _, lines = self._text(data)
            arrays, system = self._sections(lines)
            def values(bounds, types):
                return {kind: [FIELD.fullmatch(lines[i].rstrip("\r\n"))[2]
                               for i in self._field_indices(lines, bounds, FIELDS[kind])]
                        for kind in types}
            return {"project": project, "variant": variant.upper(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "subarrays": [{"id": identifier, "references": values(bounds, ("PAN", "OND"))}
                                  for identifier, bounds in sorted(arrays.items())],
                    "system_references": values(system, ("BTR", "GEN")) if system else {}}

    def _replace(self, data: bytes, updates: dict[str, str], subarray_id: int) -> bytes:
        if (not isinstance(updates, dict) or not updates
                or any(kind not in FIELDS for kind in updates)):
            raise ValueError("Choose one or more of PAN, OND, BTR, GEN")
        if not isinstance(subarray_id, int) or isinstance(subarray_id, bool) or subarray_id < 1:
            raise ValueError("subarray_id must be a positive integer")
        _, lines = self._text(data)
        arrays, system = self._sections(lines)
        for kind, filename in updates.items():
            component = self.components._path(kind, filename)
            if not component.is_file() and self.components.builtin:
                component = self.components._path(kind, filename, "builtin")
            if not component.is_file():
                raise FileNotFoundError(f"Component {filename} must exist as a loose file")
            inspected = self.components._inspect(kind, self.components._read(component))
            self.components._require_valid(inspected, complete=inspected["format"] == "text")
            bounds = arrays.get(subarray_id) if kind in ("PAN", "OND") else system
            if bounds is None:
                raise ValueError(f"No section for {kind} at subarray {subarray_id}")
            indices = self._field_indices(lines, bounds, FIELDS[kind])
            if len(indices) != 1:
                raise ValueError(f"{FIELDS[kind]} must occur exactly once in its section")
            index = indices[0]
            line = lines[index]
            prefix = line[:line.index("=") + 1]
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines[index] = f"{prefix}{filename}{ending}"
        candidate = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")
        self._text(candidate)
        return candidate

    def _backup_folder(self) -> Path:
        root = self._root()
        folder = root / ".mcp-variant-backups"
        if folder.is_symlink() or folder.resolve().parent != root:
            raise ValueError("Variant backup folder resolves outside workspace")
        folder.mkdir(exist_ok=True)
        return folder

    def _backup(self, path: Path, data: bytes) -> str:
        name = f"{path.name}.{uuid.uuid4().hex}.bak"
        with (self._backup_folder() / name).open("xb") as stream:
            stream.write(data)
        return name

    def clone(self, project: str, source_variant: str, new_variant: str,
              updates: dict[str, str] | None = None, subarray_id: int = 1) -> dict:
        with self.lock:
            self._check_project(project)
            source = self._path(project, source_variant)
            target = self._path(project, new_variant)
            data = self._read(source)
            _, lines = self._text(data)
            self._sections(lines)
            candidate = self._replace(data, updates, subarray_id) if updates else data
            if any(path.name.casefold() == target.name.casefold() for path in target.parent.iterdir()):
                raise FileExistsError(target.name)
            with target.open("xb") as stream:
                stream.write(candidate)
            return {"project": project, "variant": new_variant.upper(),
                    "source_variant": source_variant.upper(), "sha256": hashlib.sha256(candidate).hexdigest()}

    def update(self, project: str, variant: str, updates: dict[str, str],
               expected_sha256: str, subarray_id: int = 1) -> dict:
        with self.lock:
            self._check_project(project)
            path = self._path(project, variant)
            data = self._read(path)
            self._check_hash(data, expected_sha256)
            candidate = self._replace(data, updates, subarray_id)
            backup = self._backup(path, data)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(candidate)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {"project": project, "variant": variant.upper(), "backup_name": backup,
                    "sha256": hashlib.sha256(candidate).hexdigest()}

    def restore(self, project: str, variant: str, backup_name: str,
                expected_sha256: str, confirm: bool = False) -> dict:
        if not confirm:
            raise ValueError("Restore requires confirm=true")
        with self.lock:
            self._check_project(project)
            path = self._path(project, variant)
            data = self._read(path)
            self._check_hash(data, expected_sha256)
            if not isinstance(backup_name, str) or not re.fullmatch(
                    re.escape(path.name) + r"\.[0-9a-f]{32}\.bak", backup_name):
                raise ValueError("Backup name must match the variant")
            backup = self._backup_folder() / backup_name
            if backup.is_symlink() or backup.resolve().parent != self._backup_folder():
                raise ValueError("Symlink backups are not supported")
            previous_data = self._read(backup)
            _, lines = self._text(previous_data)
            self._sections(lines)
            previous_backup = self._backup(path, data)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(previous_data)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {"project": project, "variant": variant.upper(), "backup_name": previous_backup,
                    "restored_from": backup_name, "sha256": hashlib.sha256(previous_data).hexdigest()}

"""Guarded project copies, lifecycle operations and bounded PVsyst variant edits."""
from __future__ import annotations

import hashlib
import difflib
import itertools
import json
import math
import os
import re
import threading
import uuid
from pathlib import Path

from pvsyst_components import ComponentStore, FIELD, MAX_PROJECT_BYTES
from pvsyst_paths import project_path, project_root
from pvsyst_generator import state as generator_state, replace_generator

FIELDS = {"PAN": "PVModule", "OND": "GInverter", "BTR": "BatteryFile", "GEN": "GensetFile"}
STARTS = {"pvSubArray": "PVObject_=pvSubArray", "pvSystem": "PVObject_System=pvSystem"}
MAX_ARCHIVE_MEMBERS = 128


class VariantStore:
    def __init__(self, workspace: str | os.PathLike, components: ComponentStore,
                 lock: threading.RLock | None = None):
        self.workspace = Path(workspace).resolve(strict=True)
        self.components = components
        self.lock = lock or threading.RLock()

    def _root(self) -> Path:
        return project_root(self.workspace)

    def _path(self, project: str, variant: str | None = None) -> Path:
        return project_path(self.workspace, project, variant)

    @staticmethod
    def _check_size(data: bytes) -> None:
        if len(data) > MAX_PROJECT_BYTES:
            raise ValueError("Project or variant exceeds 32 MB")

    @staticmethod
    def _belongs_to_project(path: Path, project: str) -> bool:
        """Exact stems plus attached PRJ/VC sidecars, excluding other dotted projects."""
        stem = project[:-4].casefold()
        if path.stem.casefold() == stem:
            return True
        if re.fullmatch(r"\.(?:PRJ|VC[A-Za-z0-9]+)", path.suffix, re.I):
            return False
        return bool(path.name.casefold().startswith(stem + ".") and re.fullmatch(
            r"(?:PRJ|VC[A-Za-z0-9]+)\..+", path.name[len(project[:-4]) + 1:], re.I))

    @staticmethod
    def _read(path: Path) -> bytes:
        if not path.is_file():
            raise FileNotFoundError(path.name)
        if path.stat().st_size > MAX_PROJECT_BYTES:
            raise ValueError("Project or variant exceeds 32 MB")
        data = path.read_bytes()
        VariantStore._check_size(data)
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
        VariantStore._check_size(data)
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

    @staticmethod
    def _orientation(lines: list[str], number: int) -> tuple[int, int]:
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError("orientation_id must be a positive integer")
        groups = []
        start = None
        for index, line in enumerate(lines):
            value = line.strip()
            if value == "PVObject_=pvOrient":
                if start is not None:
                    raise ValueError("Nested orientations")
                start = index
            elif value == "End of TOrientGroup" and start is not None:
                groups.append((start, index))
                start = None
        if start is not None:
            raise ValueError("Unclosed orientation")
        matches = [bounds for bounds in groups if any(
            (match := FIELD.fullmatch(lines[i].rstrip("\r\n")))
            and match[1] == "NoOrient" and match[2] == str(number)
            for i in range(bounds[0] + 1, bounds[1]))]
        if len(matches) != 1:
            raise ValueError("Orientation ID must occur exactly once")
        return matches[0]

    def inspect_parameters(self, project: str, variant: str) -> dict:
        with self.lock:
            self._check_project(project)
            data = self._read(self._path(project, variant))
            _, lines = self._text(data)
            arrays, system = self._sections(lines)
            def fields(bounds, names):
                return {name: values[0] for name in names
                        if len(values := [FIELD.fullmatch(lines[i].rstrip("\r\n"))[2]
                                         for i in self._field_indices(lines, bounds, name)]) == 1}
            orientations = []
            for index, line in enumerate(lines):
                if line.strip() != "PVObject_=pvOrient":
                    continue
                end = next((i for i in range(index + 1, len(lines))
                            if lines[i].strip() == "End of TOrientGroup"), None)
                if end is None:
                    raise ValueError("Unclosed orientation")
                indices = self._field_indices(lines, (index, end), "NoOrient")
                if len(indices) != 1:
                    raise ValueError("Orientation requires one ID")
                number = FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]
                if not number.isdigit():
                    raise ValueError("Orientation ID must be numeric")
                self._orientation(lines, int(number))
                orientations.append({"id": int(number), "values": fields((index, end),
                                     ("FieldType", "FieldTilt", "FieldAzim"))})
            return {"project": project, "variant": variant.upper(),
                    "sha256": hashlib.sha256(data).hexdigest(), "orientations": orientations,
                    "generator": generator_state(lines, arrays, system),
                    "subarrays": [{"id": identifier, "values": fields(bounds, (
                        "NModSerie", "NStringCh", "NStrOrient1", "NoOrientation",
                        "VBkUpEncl_syst", "VBkUpDecl_syst"))}
                                  for identifier, bounds in sorted(arrays.items())]}

    def update_parameters(self, project: str, variant: str, expected_sha256: str,
                          subarray_id: int, orientation_id: int,
                          subarray_updates: dict[str, float] | None = None,
                          orientation_updates: dict[str, float] | None = None,
                          generator_updates: dict | None = None,
                          dry_run: bool = False) -> dict:
        allowed_subarray = {"NModSerie": (1, 100), "VBkUpEncl_syst": (0, 1),
                            "VBkUpDecl_syst": (0, 1)}
        allowed_orientation = {"FieldTilt": (0, 90), "FieldAzim": (-180, 180)}
        requested = ((subarray_updates, allowed_subarray), (orientation_updates, allowed_orientation))
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        if (not any(changes for changes, _ in requested) and not generator_updates) or any(
                changes is not None and (not isinstance(changes, dict) or
                                         any(key not in allowed for key in changes))
                for changes, allowed in requested):
            raise ValueError("Provide supported subarray, orientation or generator parameter updates")
        if generator_updates is not None and (not isinstance(generator_updates, dict) or not generator_updates):
            raise ValueError("generator_updates must be a nonempty dictionary")
        if generator_updates is not None and set(subarray_updates or {}) & {"VBkUpEncl_syst", "VBkUpDecl_syst"}:
            raise ValueError("Use generator_updates.thresholds for thresholds in a generator transaction")
        with self.lock:
            self._check_project(project)
            path = self._path(project, variant)
            data = self._read(path)
            self._check_hash(data, expected_sha256)
            text, lines = self._text(data)
            arrays, system = self._sections(lines)
            if subarray_updates:
                if not isinstance(subarray_id, int) or isinstance(subarray_id, bool) or subarray_id not in arrays:
                    raise ValueError("Subarray ID does not exist")
            targets = [(subarray_updates, allowed_subarray, arrays[subarray_id])] if subarray_updates else []
            if orientation_updates:
                bounds = self._orientation(lines, orientation_id)
                if any("PVObject_ShdTable" in line for line in lines[bounds[0]:bounds[1] + 1]):
                    raise ValueError("Orientation has a dependent shading table")
                shade = [i for i, line in enumerate(lines)
                         if line.strip() == "PVObject_Ombrage=pvShading"]
                if shade:
                    if len(shade) != 1:
                        raise ValueError("Ambiguous shading sections")
                    end = next((i for i in range(shade[0] + 1, len(lines))
                                if lines[i].strip() == "End of PVObject pvShading"), None)
                    if end is None or any("ListeObjets, list of=" in line or "PVObject_Shd" in line
                                          for line in lines[shade[0]:end]):
                        raise ValueError("Orientation has an active shading scene")
                    flags = self._field_indices(lines, (shade[0], end), "Flags")
                    if len(flags) != 1 or FIELD.fullmatch(lines[flags[0]].rstrip("\r\n"))[2] != "$00":
                        raise ValueError("Orientation has an active shading scene")
                if self._field_indices(lines, bounds, "FieldType"):
                    field = self._field_indices(lines, bounds, "FieldType")
                    if len(field) != 1 or FIELD.fullmatch(lines[field[0]].rstrip("\r\n"))[2] != "FixedPlane":
                        raise ValueError("Orientation must be a fixed plane")
                else:
                    raise ValueError("Orientation must be a fixed plane")
                targets.append((orientation_updates, allowed_orientation, bounds))
            for changes, allowed, bounds in targets:
                for name, value in changes.items():
                    if (not isinstance(value, (int, float)) or isinstance(value, bool)
                            or not math.isfinite(value) or not allowed[name][0] <= value <= allowed[name][1]
                            or name == "NModSerie" and int(value) != value):
                        raise ValueError(f"Invalid {name} value")
                    indices = self._field_indices(lines, bounds, name)
                    if len(indices) != 1:
                        raise ValueError(f"{name} must occur exactly once in its section")
                    index = indices[0]
                    old = lines[index]
                    prefix = old[:old.index("=") + 1]
                    ending = "\r\n" if old.endswith("\r\n") else "\n" if old.endswith("\n") else ""
                    rendered = str(int(value)) if name == "NModSerie" else f"{value:.3f}"
                    lines[index] = f"{prefix}{rendered}{ending}"
            generator = None
            if generator_updates is not None:
                generator = replace_generator(lines, arrays, system, generator_updates, self.components)
            edited_text = "".join(lines)
            candidate = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + edited_text.encode("utf-8")
            self._text(candidate)
            diff = "".join(itertools.islice(difflib.unified_diff(
                text.splitlines(keepends=True), lines, fromfile=path.name,
                tofile=path.name + " (candidate)"), 501))
            result = {"project": project, "variant": variant.upper(),
                "before_sha256": hashlib.sha256(data).hexdigest(),
                "sha256": hashlib.sha256(candidate).hexdigest(), "changed": candidate != data,
                "dry_run": dry_run, "diff": diff[:32000],
                "diff_truncated": len(diff) > 32000 or len(diff.splitlines()) > 500}
            if generator is not None:
                new_arrays, new_system = self._sections(lines)
                result.update(generator_before=generator["before"],
                    generator_after=generator_state(lines, new_arrays, new_system),
                    generator_component=generator["component"], warnings=generator["warnings"])
            if dry_run or candidate == data:
                return result
            if self._read(path) != data:
                raise ValueError("Variant changed while preparing the edit")
            backup = self._backup(path, data)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(candidate)
                if self._read(path) != data:
                    raise ValueError("Variant changed while preparing the edit")
                if generator is not None and generator["component"] is not None:
                    component = generator["component"]
                    if (component["library"] == "builtin" and
                            self.components._path("GEN", component["filename"]).exists()):
                        raise ValueError("Generator component changed: workspace now shadows builtin")
                    target = self.components._path("GEN", component["filename"], component["library"])
                    if hashlib.sha256(self.components._read(target)).hexdigest() != component["sha256"]:
                        raise ValueError("Generator component changed while preparing the edit")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {**result, "backup_name": backup}

    def _backup_folder(self) -> Path:
        root = self._root()
        folder = root / ".mcp-variant-backups"
        if folder.is_symlink() or folder.resolve().parent != root:
            raise ValueError("Variant backup folder resolves outside workspace")
        folder.mkdir(exist_ok=True)
        return folder

    def _backup(self, path: Path, data: bytes, transaction_id: str | None = None) -> str:
        name = f"{path.name}.{transaction_id or uuid.uuid4().hex}.bak"
        with (self._backup_folder() / name).open("xb") as stream:
            stream.write(data)
        return name

    def clone_project(self, source_project: str, new_project: str) -> dict:
        """Copy a project and its variants without rewriting embedded site/meteo snapshots."""
        with self.lock:
            self._check_project(source_project)
            source_root = self._root()
            target_prj = self._path(new_project)
            sources = [self._path(source_project)]
            source_paths = list(source_root.iterdir())
            for path in source_paths:
                if path.name.casefold() == source_project.casefold():
                    continue
                if self._belongs_to_project(path, source_project):
                    extension = path.suffix[1:]
                    if not re.fullmatch(r"VC[A-Za-z0-9]+", extension, re.I):
                        raise ValueError(f"Unrecognized project sidecar: {path.name}")
                    sources.append(self._path(source_project, extension))
            if len(sources) == 1:
                raise ValueError("Source project has no variants")
            snapshots = []
            for source in sources:
                data = self._read(source)
                if source == sources[0]:
                    self._check_project(source_project)
                else:
                    _, lines = self._text(data)
                    self._sections(lines)
                target = target_prj if source == sources[0] else self._path(new_project, source.suffix[1:])
                snapshots.append((target, data))
            if any(self._belongs_to_project(path, new_project) for path in source_paths):
                raise FileExistsError("Destination project or variant already exists")
            created = []
            try:
                for target, data in snapshots:
                    with target.open("xb") as stream:
                        created.append(target)
                        stream.write(data)
            except BaseException:
                for target in reversed(created):
                    target.unlink(missing_ok=True)
                raise
            return {"project": new_project, "source_project": source_project,
                    "variants": [path.suffix[1:] for path, _ in snapshots[1:]],
                    "sha256": hashlib.sha256(snapshots[0][1]).hexdigest()}

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

    @staticmethod
    def _inverter_nodes(lines: list[str]) -> list[tuple[int, int]]:
        stack = []
        nodes = []
        for index, line in enumerate(lines):
            value = line.strip()
            start = re.fullmatch(r"(InjectionPointNode|TransformerNode|InverterNode|MPPTNode|StringNode) Start;", value)
            end = re.fullmatch(r"(InjectionPointNode|TransformerNode|InverterNode|MPPTNode|StringNode) End;", value)
            if start:
                stack.append((start[1], index))
            elif end:
                if not stack or stack[-1][0] != end[1]:
                    raise ValueError("Circuit tree contains unmatched nodes")
                kind, beginning = stack.pop()
                if kind == "InverterNode":
                    nodes.append((beginning, index))
        if stack:
            raise ValueError("Circuit tree contains unclosed nodes")
        return nodes

    def validate_structure(self, project: str, variant: str) -> dict:
        """Check known cross-object references; simulation remains the physical validator."""
        with self.lock:
            self._check_project(project)
            data = self._read(self._path(project, variant))
            _, lines = self._text(data)
            arrays, system = self._sections(lines)
            issues = []
            warnings = []
            orientation_ids = set()
            for index, line in enumerate(lines):
                if line.strip() not in ("PVObject_=pvOrient", "PVObject_=pvDomeOrient"):
                    continue
                closing = ("End of TOrientGroup" if line.strip() == "PVObject_=pvOrient"
                           else "End of TDomeOrientGroup")
                end = next((i for i in range(index + 1, len(lines)) if lines[i].strip() == closing), None)
                if end is None:
                    issues.append("Unclosed orientation")
                    continue
                identifiers = self._field_indices(lines, (index, end), "NoOrient")
                if len(identifiers) != 1:
                    issues.append("Orientation needs one NoOrient")
                    continue
                raw = FIELD.fullmatch(lines[identifiers[0]].rstrip("\r\n"))[2]
                if not raw.isdigit() or int(raw) in orientation_ids:
                    issues.append(f"Duplicate or invalid orientation ID: {raw}")
                else:
                    orientation_ids.add(int(raw))
            for identifier, bounds in arrays.items():
                indices = self._field_indices(lines, bounds, "NoOrientation")
                if len(indices) != 1:
                    issues.append(f"Subarray {identifier} needs one NoOrientation")
                    continue
                raw = FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]
                if not raw.isdigit() or int(raw) not in orientation_ids:
                    issues.append(f"Subarray {identifier} references unknown orientation {raw}")
            system_type = None
            unchecked = []
            if system is None:
                issues.append("Missing pvSystem section")
                unchecked.append("system circuit references")
            else:
                kinds = self._field_indices(lines, system, "SystemType")
                if len(kinds) != 1:
                    issues.append("System needs exactly one SystemType")
                    unchecked.append("system circuit references")
                else:
                    system_type = FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2]
                    if not system_type:
                        issues.append("SystemType must not be empty")
                    if system_type != "Grid":
                        unchecked.append("system circuit references")
                        warnings.append(f"Circuit checks are not implemented for SystemType={system_type!r}")
                if system_type == "Grid":
                    branches = {}
                    for start, end in self._inverter_nodes(lines):
                        ids = [field[2] for i in range(start + 1, end)
                               if (field := FIELD.fullmatch(lines[i].rstrip("\r\n")))
                               and field[1] == "SubArrayId"]
                        if len(ids) not in (2, 3) or len(set(ids)) != 1 or not ids[0].isdigit():
                            issues.append(f"Inverter branch at line {start + 1} has mixed or missing IDs")
                            continue
                        identifier = int(ids[0])
                        branches[identifier] = branches.get(identifier, 0) + 1
                        if identifier not in arrays:
                            issues.append(f"Inverter branch references unknown subarray {identifier}")
                            continue
                        names = [field[2] for line in lines[start:end + 1]
                                 if (field := FIELD.fullmatch(line.rstrip("\r\n")))
                                 and field[1] == "SubArrayName"]
                        comments = self._field_indices(lines, arrays[identifier], "Comment")
                        if (len(comments) != 1 or len(names) != len(ids)
                                or any(name != FIELD.fullmatch(lines[comments[0]].rstrip("\r\n"))[2]
                                       for name in names)):
                            issues.append(f"Subarray {identifier} circuit name differs from Comment")
                    for identifier in arrays:
                        if branches.get(identifier) != 1:
                            issues.append(f"Subarray {identifier} requires one isolated inverter branch")
            if any("PVObject_ShdTable" in line or "ListeObjets, list of=" in line for line in lines):
                warnings.append("Shading geometry and derived factor tables require simulation verification")
            return {"project": project, "variant": variant.upper(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "valid": False if issues else None if unchecked else True,
                    "checks_complete": not unchecked, "unchecked_checks": unchecked,
                    "system_type": system_type,
                    "scope": "orientation and supported grid circuit references; not physical validation",
                    "issues": issues, "warnings": warnings,
                    "subarray_ids": sorted(arrays), "orientation_ids": sorted(orientation_ids)}

    def clone_subarray(self, project: str, variant: str, source_subarray_id: int,
                       expected_sha256: str) -> dict:
        """Duplicate one self-contained grid inverter branch and its subarray."""
        if not isinstance(source_subarray_id, int) or isinstance(source_subarray_id, bool):
            raise ValueError("source_subarray_id must be an integer")
        with self.lock:
            self._check_project(project)
            path = self._path(project, variant)
            data = self._read(path)
            self._check_hash(data, expected_sha256)
            _, lines = self._text(data)
            arrays, system = self._sections(lines)
            if source_subarray_id not in arrays or system is None:
                raise ValueError("Source subarray and grid system are required")
            system_type = self._field_indices(lines, system, "SystemType")
            if len(system_type) != 1 or FIELD.fullmatch(lines[system_type[0]].rstrip("\r\n"))[2] != "Grid":
                raise ValueError("Only a grid system is supported")
            if any("PVObject_ShdTable" in line or "ListeObjets, list of=" in line for line in lines):
                raise ValueError("Shading scene or shading table requires separate geometry updates")
            shade = [i for i, line in enumerate(lines) if line.strip() == "PVObject_Ombrage=pvShading"]
            if len(shade) != 1:
                raise ValueError("Expected one empty shading section")
            shade_end = next((i for i in range(shade[0] + 1, len(lines))
                              if lines[i].strip() == "End of PVObject pvShading"), None)
            if shade_end is None or any(line.strip().startswith("PVObject_")
                                       for line in lines[shade[0] + 1:shade_end]):
                raise ValueError("Shading scene is not empty")
            flags = self._field_indices(lines, (shade[0], shade_end), "Flags")
            if len(flags) != 1 or FIELD.fullmatch(lines[flags[0]].rstrip("\r\n"))[2] != "$00":
                raise ValueError("Shading is active or unrecognized")
            bounds = arrays[source_subarray_id]
            orientation_fields = self._field_indices(lines, bounds, "NoOrientation")
            if len(orientation_fields) != 1:
                raise ValueError("Source subarray requires one orientation")
            orientation_id = FIELD.fullmatch(lines[orientation_fields[0]].rstrip("\r\n"))[2]
            if not orientation_id.isdigit():
                raise ValueError("Source orientation is invalid")
            orientation = self._orientation(lines, int(orientation_id))
            field_type = self._field_indices(lines, orientation, "FieldType")
            if len(field_type) != 1 or FIELD.fullmatch(lines[field_type[0]].rstrip("\r\n"))[2] != "FixedPlane":
                raise ValueError("Only a fixed-plane orientation is supported")
            name_indices = self._field_indices(lines, bounds, "Comment")
            if len(name_indices) != 1:
                raise ValueError("Source subarray requires one name")
            old_name = FIELD.fullmatch(lines[name_indices[0]].rstrip("\r\n"))[2]
            nodes = self._inverter_nodes(lines)
            matches = []
            for start, end in nodes:
                ids = [FIELD.fullmatch(lines[i].rstrip("\r\n"))[2]
                       for i in range(start + 1, end)
                       if (field := FIELD.fullmatch(lines[i].rstrip("\r\n"))) and field[1] == "SubArrayId"]
                if ids and all(value == str(source_subarray_id) for value in ids):
                    matches.append((start, end))
            if len(matches) != 1 or any(
                    FIELD.fullmatch(lines[i].rstrip("\r\n"))[2] == str(source_subarray_id)
                    for start, end in nodes if (start, end) not in matches
                    for i in range(start + 1, end)
                    if (field := FIELD.fullmatch(lines[i].rstrip("\r\n"))) and field[1] == "SubArrayId"):
                raise ValueError("Source must own one complete inverter branch")
            node_start, node_end = matches[0]
            branch = lines[node_start:node_end + 1]
            names = [FIELD.fullmatch(line.rstrip("\r\n"))[2] for line in branch
                     if (field := FIELD.fullmatch(line.rstrip("\r\n"))) and field[1] == "SubArrayName"]
            if not names or any(name != old_name for name in names):
                raise ValueError("Circuit branch names do not match the subarray")
            if len([line for line in branch if re.fullmatch(r"\s*SubArrayId=\d+\s*", line)]) not in (2, 3):
                raise ValueError("Unexpected circuit branch depth")
            occurrences = [i for i, line in enumerate(lines)
                           if (field := FIELD.fullmatch(line.rstrip("\r\n")))
                           and field[1] == "SubArrayId" and field[2] == str(source_subarray_id)]
            array_ids = self._field_indices(lines, bounds, "SubArrayId")
            if len(array_ids) != 1 or not set(occurrences).issubset(
                    set(range(node_start, node_end + 1)) | set(array_ids)):
                raise ValueError("Source subarray is referenced outside its inverter branch")
            current_ids = set(arrays)
            for item in self._root().iterdir():
                if (item.stem.casefold() == project[:-4].casefold()
                        and re.fullmatch(r"\.VC[A-Za-z0-9]+", item.suffix, re.I)):
                    other_path = self._path(project, item.suffix[1:])
                    _, other_lines = self._text(self._read(other_path))
                    other_arrays, _ = self._sections(other_lines)
                    current_ids.update(other_arrays)
            new_id = max(current_ids) + 1
            if new_id > 2147483647:
                raise ValueError("No available subarray ID")
            new_name = f"Sub-array #{new_id}"
            def remap(block, array_comment=-1):
                rewritten = []
                for index, line in enumerate(block):
                    field = FIELD.fullmatch(line.rstrip("\r\n"))
                    if field and (field[1] in ("SubArrayId", "SubArrayName")
                                  or index == array_comment and field[1] == "Comment"):
                        value = str(new_id) if field[1] == "SubArrayId" else new_name
                        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                        line = line[:line.index("=") + 1] + value + ending
                    rewritten.append(line)
                return rewritten
            new_branch = remap(branch)
            new_array = remap(lines[bounds[0]:bounds[1] + 1], name_indices[0] - bounds[0])
            for offset, block in sorted(((node_end + 1, new_branch), (bounds[1] + 1, new_array)),
                                        reverse=True):
                lines[offset:offset] = block
            candidate = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")
            _, checked = self._text(candidate)
            checked_arrays, _ = self._sections(checked)
            if new_id not in checked_arrays or len(self._inverter_nodes(checked)) != len(nodes) + 1:
                raise ValueError("Subarray structure validation failed")
            backup = self._backup(path, data)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(candidate)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {"project": project, "variant": variant.upper(), "subarray_id": new_id,
                    "backup_name": backup, "sha256": hashlib.sha256(candidate).hexdigest()}

    def remove_subarray(self, project: str, variant: str, subarray_id: int,
                        expected_sha256: str) -> dict:
        """Remove one isolated inverter branch, keeping a shared orientation in use."""
        if not isinstance(subarray_id, int) or isinstance(subarray_id, bool):
            raise ValueError("subarray_id must be an integer")
        with self.lock:
            self._check_project(project)
            path = self._path(project, variant)
            data = self._read(path)
            self._check_hash(data, expected_sha256)
            _, lines = self._text(data)
            arrays, system = self._sections(lines)
            if subarray_id not in arrays or len(arrays) < 2 or system is None:
                raise ValueError("A target and another remaining subarray are required")
            kinds = self._field_indices(lines, system, "SystemType")
            if len(kinds) != 1 or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid":
                raise ValueError("Only grid subarrays are supported")
            shade = [i for i, line in enumerate(lines) if line.strip() == "PVObject_Ombrage=pvShading"]
            if len(shade) != 1 or any("PVObject_ShdTable" in line or "ListeObjets, list of=" in line
                                      for line in lines):
                raise ValueError("Shading scene is not supported")
            shade_end = next((i for i in range(shade[0] + 1, len(lines))
                              if lines[i].strip() == "End of PVObject pvShading"), None)
            if shade_end is None or any(line.strip().startswith("PVObject_")
                                       for line in lines[shade[0] + 1:shade_end]):
                raise ValueError("Shading scene is not empty")
            flags = self._field_indices(lines, (shade[0], shade_end), "Flags")
            if len(flags) != 1 or FIELD.fullmatch(lines[flags[0]].rstrip("\r\n"))[2] != "$00":
                raise ValueError("Shading is active or unrecognized")
            bounds = arrays[subarray_id]
            no_orient = self._field_indices(lines, bounds, "NoOrientation")
            if len(no_orient) != 1:
                raise ValueError("Subarray needs one orientation")
            orientation = FIELD.fullmatch(lines[no_orient[0]].rstrip("\r\n"))[2]
            if not any(other != subarray_id and any(
                    FIELD.fullmatch(lines[i].rstrip("\r\n"))[2] == orientation
                    for i in self._field_indices(lines, other_bounds, "NoOrientation"))
                    for other, other_bounds in arrays.items()):
                raise ValueError("Orientation must remain in use by another subarray")
            name_indices = self._field_indices(lines, bounds, "Comment")
            if len(name_indices) != 1:
                raise ValueError("Subarray needs one name")
            name = FIELD.fullmatch(lines[name_indices[0]].rstrip("\r\n"))[2]
            nodes = self._inverter_nodes(lines)
            matches = []
            for start, end in nodes:
                ids = [field[2] for i in range(start + 1, end)
                       if (field := FIELD.fullmatch(lines[i].rstrip("\r\n"))) and field[1] == "SubArrayId"]
                if ids and all(value == str(subarray_id) for value in ids):
                    matches.append((start, end, ids))
            if len(matches) != 1 or len(matches[0][2]) not in (2, 3):
                raise ValueError("Subarray must own one complete inverter branch")
            start, end, ids = matches[0]
            branch_names = [field[2] for line in lines[start:end + 1]
                            if (field := FIELD.fullmatch(line.rstrip("\r\n"))) and field[1] == "SubArrayName"]
            if len(branch_names) != len(ids) or any(value != name for value in branch_names):
                raise ValueError("Branch names do not match subarray")
            occurrences = [i for i, line in enumerate(lines)
                           if (field := FIELD.fullmatch(line.rstrip("\r\n")))
                           and field[1] == "SubArrayId" and field[2] == str(subarray_id)]
            array_ids = self._field_indices(lines, bounds, "SubArrayId")
            if len(array_ids) != 1 or not set(occurrences).issubset(
                    set(range(start, end + 1)) | set(array_ids)):
                raise ValueError("Subarray ID is referenced outside the isolated branch")
            for first, last in sorted(((start, end), bounds), reverse=True):
                del lines[first:last + 1]
            candidate = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")
            _, checked = self._text(candidate)
            checked_arrays, _ = self._sections(checked)
            if subarray_id in checked_arrays or len(checked_arrays) != len(arrays) - 1:
                raise ValueError("Subarray removal failed validation")
            backup = self._backup(path, data)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(candidate)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {"project": project, "variant": variant.upper(), "removed_subarray_id": subarray_id,
                    "backup_name": backup, "sha256": hashlib.sha256(candidate).hexdigest()}

    def _archive_folder(self) -> Path:
        root = self._root()
        folder = root / ".mcp-project-archive"
        if folder.is_symlink() or folder.resolve().parent != root:
            raise ValueError("Project archive resolves outside workspace")
        folder.mkdir(exist_ok=True)
        return folder

    @staticmethod
    def _embedded_reference(lines: list[str], header: str, closing: str,
                            field_name: str = "NomF") -> str | None:
        starts = [i for i, line in enumerate(lines) if line.strip() == header]
        if not starts:
            return None
        if len(starts) != 1:
            raise ValueError(f"Multiple {header} sections")
        end = next((i for i in range(starts[0] + 1, len(lines))
                    if lines[i].strip() == closing), None)
        if end is None:
            raise ValueError(f"Unclosed {header} section")
        child = next((i for i in range(starts[0] + 1, end)
                      if lines[i].strip().startswith("PVObject_")), end)
        indices = VariantStore._field_indices(lines, (starts[0], child), field_name)
        if len(indices) != 1:
            raise ValueError(f"{header} requires one {field_name}")
        return FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]

    def _workspace_asset(self, directory: str, name: str | None) -> dict:
        if name is None:
            return {"name": None, "workspace_file": False}
        if (not name or Path(name).name != name or "\\" in name or "/" in name
                or not name.lower().endswith({"Sites": ".sit", "Meteo": ".met"}[directory])):
            return {"name": name, "workspace_file": False}
        base = self.workspace / directory
        path = base / name
        found = (base.is_dir() and not base.is_symlink() and base.resolve().parent == self.workspace
                 and not path.is_symlink()
                 and path.is_file() and path.resolve().parent == base.resolve())
        return {"name": name, "workspace_file": found}

    def inspect_project(self, project: str) -> dict:
        with self.lock:
            self._check_project(project)
            members = [self._path(project)]
            for path in self._root().iterdir():
                if path.name.casefold() == project.casefold():
                    continue
                if self._belongs_to_project(path, project):
                    if not re.fullmatch(r"VC[A-Za-z0-9]+", path.suffix[1:], re.I):
                        raise ValueError(f"Unrecognized project sidecar: {path.name}")
                    members.append(self._path(project, path.suffix[1:]))
            hashes = {}
            sources = {}
            warnings = []
            for path in members:
                data = self._read(path)
                if path.suffix.lower() == ".prj":
                    lines = data.decode("utf-8-sig").splitlines()
                    meteo_fields = [field[2] for line in lines
                                    if (field := FIELD.fullmatch(line)) and field[1] == "MeteoFileName"]
                    if len(meteo_fields) > 1:
                        raise ValueError("Project has multiple MeteoFileName fields")
                    site = self._embedded_reference(lines, "PVObject_SitePrj=pvSite",
                                                    "End of PVObject pvSite")
                    meteo = meteo_fields[0] if meteo_fields else None
                else:
                    _, lines = self._text(data)
                    self._sections(lines)
                    site = self._embedded_reference(lines, "PVObject_SiteSimul=pvSite",
                                                    "End of PVObject pvSite")
                    meteo = self._embedded_reference(lines, "PVObject_MeteoSimul=pvMeteo",
                                                     "End of PVObject pvMeteo")
                hashes[path.name] = hashlib.sha256(data).hexdigest()
                sources[path.name] = {"site": self._workspace_asset("Sites", site),
                                      "meteo": self._workspace_asset("Meteo", meteo)}
                for kind, asset in sources[path.name].items():
                    if asset["name"] and not asset["workspace_file"]:
                        warnings.append(f"{path.name}: {kind} {asset['name']} is not a workspace file")
            project_refs = sources[members[0].name]
            for path in members[1:]:
                refs = sources[path.name]
                for kind in ("site", "meteo"):
                    expected, actual = project_refs[kind]["name"], refs[kind]["name"]
                    if expected and actual and expected.casefold() != actual.casefold():
                        warnings.append(f"{path.name}: {kind} differs from {members[0].name}")
            return {"project": project, "files": hashes,
                    "variants": [path.suffix[1:] for path in members[1:]],
                    "sources": sources, "warnings": warnings}

    @staticmethod
    def _replace_site_block(lines: list[str], role: str, site_lines: list[str]) -> None:
        header = f"PVObject_{role}=pvSite"
        starts = [i for i, line in enumerate(lines) if line.strip() == header]
        if len(starts) != 1:
            raise ValueError(f"Expected one {header} section")
        start = starts[0]
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].strip() == "End of PVObject pvSite"), None)
        if end is None or any(line.strip().startswith("PVObject_")
                              for line in lines[start + 1:end]):
            raise ValueError(f"Unsupported nested {header} section")
        prefix = lines[start][:len(lines[start]) - len(lines[start].lstrip())]
        ending = "\r\n" if lines[start].endswith("\r\n") else "\n"
        lines[start:end + 1] = [prefix + header + ending] + [
            prefix + line.rstrip("\r\n") + ending for line in site_lines[1:-1]
        ] + [prefix + "End of PVObject pvSite" + ending]

    @staticmethod
    def _replace_unique_field(lines: list[str], key: str, value: str,
                              bounds: tuple[int, int]) -> None:
        indices = [i for i in range(bounds[0], bounds[1])
                   if (field := FIELD.fullmatch(lines[i].rstrip("\r\n"))) and field[1] == key]
        if len(indices) != 1:
            raise ValueError(f"Expected one {key} in target section")
        index = indices[0]
        original = lines[index]
        ending = "\r\n" if original.endswith("\r\n") else "\n" if original.endswith("\n") else ""
        lines[index] = original[:original.index("=") + 1] + value + ending

    @staticmethod
    def _section_bounds(lines: list[str], header: str, closing: str) -> tuple[int, int]:
        starts = [i for i, line in enumerate(lines) if line.strip() == header]
        if len(starts) != 1:
            raise ValueError(f"Expected one {header} section")
        end = next((i for i in range(starts[0] + 1, len(lines))
                    if lines[i].strip() == closing), None)
        if end is None:
            raise ValueError(f"Unclosed {header} section")
        return starts[0], end

    def _commit_project_files(self, project: str, originals: dict[Path, bytes],
                              candidates: dict[Path, bytes]) -> dict:
        if not originals or originals.keys() != candidates.keys():
            raise ValueError("Project transaction must contain the same complete file set")
        for data in (*originals.values(), *candidates.values()):
            self._check_size(data)
        transaction_id = uuid.uuid4().hex
        manifest_name = f"transaction.{transaction_id}.json"
        backups = {}
        staged = {}
        written = []
        try:
            for path, data in originals.items():
                backups[path.name] = self._backup(path, data, transaction_id)
            with (self._backup_folder() / manifest_name).open("x", encoding="utf-8") as stream:
                json.dump({"version": 1, "transaction_id": transaction_id, "project": project,
                           "backups": backups, "original_files": {
                               path.name: hashlib.sha256(data).hexdigest()
                               for path, data in originals.items()}}, stream)
            for path, data in candidates.items():
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                staged[path] = temporary
                with temporary.open("xb") as stream:
                    stream.write(data)
            for path, temporary in staged.items():
                os.replace(temporary, path)
                written.append(path)
        except BaseException:
            for path in reversed(written):
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                with temporary.open("xb") as stream:
                    stream.write(originals[path])
                os.replace(temporary, path)
            raise
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)
        return {"backups": backups, "transaction_id": transaction_id,
                "transaction_manifest": manifest_name,
                "files": {path.name: hashlib.sha256(data).hexdigest()
                          for path, data in candidates.items()}}

    def update_project_sources(self, project: str, site_name: str, met_name: str,
                               expected_files: dict[str, str]) -> dict:
        """Rebind PRJ and every VC to one workspace SIT/MET pair."""
        with self.lock:
            inventory = self.inspect_project(project)
            if inventory["files"] != expected_files or len(expected_files) < 2:
                raise ValueError("Project files changed since inspection or has no variants")
            site_ref = self._workspace_asset("Sites", site_name)
            met_ref = self._workspace_asset("Meteo", met_name)
            if not site_ref["workspace_file"] or not met_ref["workspace_file"]:
                raise ValueError("SIT and MET must be existing workspace files")
            site_path = self.workspace / "Sites" / site_name
            site_bytes = self._read(site_path)
            try:
                site_text = site_bytes.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError("SIT must be UTF-8 text") from exc
            if "\x00" in site_text:
                raise ValueError("SIT contains NUL bytes")
            site_lines = site_text.splitlines(keepends=True)
            if (len(site_lines) < 3 or site_lines[0].strip() != "PVObject_=pvSite"
                    or site_lines[-1].strip() != "End of PVObject pvSite"):
                raise ValueError("SIT is not a supported pvSite object")
            if any(line.strip().startswith(("PVObject_", "End of PVObject"))
                   for line in site_lines[1:-1]):
                raise ValueError("SIT must contain one non-nested pvSite object")
            identifiers = [field[2] for line in site_lines
                           if (field := FIELD.fullmatch(line.rstrip("\r\n"))) and field[1] == "Site"]
            filenames = [field[2] for line in site_lines
                         if (field := FIELD.fullmatch(line.rstrip("\r\n"))) and field[1] == "NomF"]
            if len(identifiers) != 1 or not identifiers[0] or filenames != [site_name]:
                raise ValueError("SIT site identity or filename does not match")
            site_identity = identifiers[0]
            originals = {}
            candidates = {}
            for name, digest in expected_files.items():
                path = self._path(project) if name == project else self._path(project, name[len(project[:-4]) + 1:])
                data = self._read(path)
                self._check_hash(data, digest)
                text = data.decode("utf-8-sig")
                lines = text.splitlines(keepends=True)
                if name == project:
                    self._replace_site_block(lines, "SitePrj", site_lines)
                    self._replace_unique_field(lines, "MeteoFileName", met_name, (0, len(lines)))
                else:
                    self._text(data)
                    self._replace_site_block(lines, "SiteSimul", site_lines)
                    self._replace_site_block(lines, "SiteMet", site_lines)
                    start, end = self._section_bounds(lines, "PVObject_MeteoSimul=pvMeteo",
                                                      "End of PVObject pvMeteo")
                    child = next((i for i in range(start + 1, end)
                                  if lines[i].strip().startswith("PVObject_")), end)
                    self._replace_unique_field(lines, "NomF", met_name, (start + 1, child))
                    self._replace_unique_field(lines, "SiteM", site_identity, (start + 1, child))
                candidate = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")
                self._check_size(candidate)
                if name != project:
                    self._text(candidate)
                    self._sections(self._text(candidate)[1])
                originals[path] = data
                candidates[path] = candidate
            transaction = self._commit_project_files(project, originals, candidates)
            return {"project": project, "site": site_name, "meteo": met_name,
                    "note": "Historical weather origin fields remain from the source variant",
                    **transaction}

    def restore_project_sources(self, project: str, backups: dict[str, str],
                                expected_files: dict[str, str], confirm: bool = False) -> dict:
        """Restore every PRJ/VC snapshot from a source-update transaction."""
        if not confirm:
            raise ValueError("Restore requires confirm=true")
        with self.lock:
            inventory = self.inspect_project(project)["files"]
            if inventory != expected_files or set(backups) != set(expected_files):
                raise ValueError("Project files changed since inspection or backup set is incomplete")
            transaction_ids = set()
            for name, backup_name in backups.items():
                match = (re.fullmatch(re.escape(name) + r"\.([0-9a-f]{32})\.bak", backup_name)
                         if isinstance(backup_name, str) else None)
                if match is None:
                    raise ValueError("Backup name must match each project file")
                transaction_ids.add(match[1])
            if len(transaction_ids) != 1:
                raise ValueError("Backups must belong to one complete source transaction")
            transaction_id = transaction_ids.pop()
            folder = self._backup_folder()
            manifest_path = folder / f"transaction.{transaction_id}.json"
            if (manifest_path.is_symlink() or manifest_path.resolve().parent != folder
                    or not manifest_path.is_file()):
                raise ValueError("Missing or unsafe source transaction manifest; legacy sets require manual recovery")
            manifest = json.loads(self._read(manifest_path).decode("utf-8"))
            if (not isinstance(manifest, dict) or manifest.get("version") != 1
                    or manifest.get("project") != project
                    or manifest.get("transaction_id") != transaction_id
                    or manifest.get("backups") != backups
                    or not isinstance(manifest.get("original_files"), dict)
                    or set(manifest["original_files"]) != set(backups)
                    or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                           for value in manifest["original_files"].values())):
                raise ValueError("Backups must match one complete source transaction manifest")
            originals = {}
            candidates = {}
            for name, digest in expected_files.items():
                path = self._path(project) if name == project else self._path(project, name[len(project[:-4]) + 1:])
                data = self._read(path)
                self._check_hash(data, digest)
                backup_name = backups[name]
                if (not isinstance(backup_name, str) or not re.fullmatch(
                        re.escape(name) + r"\.[0-9a-f]{32}\.bak", backup_name)):
                    raise ValueError("Backup name must match each project file")
                snapshot = self._backup_folder() / backup_name
                if snapshot.is_symlink() or snapshot.resolve().parent != self._backup_folder():
                    raise ValueError("Linked project backup is not supported")
                previous = self._read(snapshot)
                if hashlib.sha256(previous).hexdigest() != manifest["original_files"][name]:
                    raise ValueError(f"Project backup integrity check failed: {name}")
                if name == project:
                    lines = previous.decode("utf-8-sig").splitlines()
                    if (not lines or lines[0].strip() != "PVObject_=pvProject"
                            or lines[-1].strip() != "End of PVObject pvProject"):
                        raise ValueError("Archived project snapshot is malformed")
                else:
                    _, lines = self._text(previous)
                    self._sections(lines)
                originals[path] = data
                candidates[path] = previous
            return {"project": project, **self._commit_project_files(project, originals, candidates)}

    def archive_project(self, project: str, expected_files: dict[str, str],
                        confirm: bool = False) -> dict:
        if not confirm:
            raise ValueError("Archive requires confirm=true")
        with self.lock:
            actual = self.inspect_project(project)["files"]
            if actual != expected_files or len(actual) < 2:
                raise ValueError("Project files changed since inspection or has no variants")
            if len(actual) > MAX_ARCHIVE_MEMBERS:
                raise ValueError(f"Project archive exceeds {MAX_ARCHIVE_MEMBERS} members")
            folder = self._archive_folder() / uuid.uuid4().hex
            folder.mkdir()
            moved = []
            try:
                with (folder / "manifest.json").open("x", encoding="utf-8") as stream:
                    json.dump({"project": project, "files": actual}, stream)
                for name in actual:
                    path = self._root() / name
                    os.replace(path, folder / name)
                    moved.append(name)
            except BaseException:
                for name in reversed(moved):
                    os.replace(folder / name, self._root() / name)
                (folder / "manifest.json").unlink(missing_ok=True)
                folder.rmdir()
                raise
            return {"project": project, "archive_name": folder.name, "files": list(actual)}

    def archive_variant(self, project: str, variant: str, expected_sha256: str,
                        confirm: bool = False) -> dict:
        if not confirm:
            raise ValueError("Archive requires confirm=true")
        with self.lock:
            self._check_project(project)
            path = self._path(project, variant)
            data = self._read(path)
            self._check_hash(data, expected_sha256)
            _, lines = self._text(data)
            self._sections(lines)
            members = self.inspect_project(project)["files"]
            others = [name for name in members
                      if name.casefold() not in (project.casefold(), path.name.casefold())]
            if not others:
                raise ValueError("Archive the project instead of its last variant")
            archive_name = uuid.uuid4().hex
            target = self._archive_folder() / archive_name
            target.mkdir()
            try:
                with (target / "manifest.json").open("x", encoding="utf-8") as stream:
                    json.dump({"project": project, "files": {path.name: expected_sha256.lower()}}, stream)
                os.replace(path, target / path.name)
            except BaseException:
                if (target / path.name).exists():
                    os.replace(target / path.name, path)
                (target / "manifest.json").unlink(missing_ok=True)
                target.rmdir()
                raise
            return {"project": project, "variant": variant.upper(), "archive_name": archive_name}

    def restore_archive(self, project: str, archive_name: str, confirm: bool = False) -> dict:
        if not confirm:
            raise ValueError("Restore requires confirm=true")
        if not isinstance(archive_name, str) or not re.fullmatch(r"[0-9a-f]{32}", archive_name):
            raise ValueError("Invalid archive name")
        with self.lock:
            folder = self._archive_folder() / archive_name
            if folder.is_symlink() or folder.resolve().parent != self._archive_folder():
                raise ValueError("Archive resolves outside workspace")
            with (folder / "manifest.json").open("r", encoding="utf-8") as stream:
                manifest = json.load(stream)
            if manifest.get("project") != project or not isinstance(manifest.get("files"), dict):
                raise ValueError("Archive belongs to another project")
            files = manifest["files"]
            if not files or len(files) > MAX_ARCHIVE_MEMBERS or any(not isinstance(name, str) or not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest) for name, digest in files.items()):
                raise ValueError("Invalid archive manifest")
            sources = []
            for name, digest in files.items():
                if name == project:
                    path = self._path(project)
                elif name.startswith(project[:-4] + "."):
                    path = self._path(project, name[len(project[:-4]) + 1:])
                else:
                    raise ValueError("Archive filename does not match project")
                source = folder / name
                if source.is_symlink() or source.resolve().parent != folder or path.exists():
                    raise ValueError("Archive has linked data or destination already exists")
                data = self._read(source)
                self._check_hash(data, digest)
                if name == project:
                    if data.decode("utf-8-sig").splitlines()[0].strip() != "PVObject_=pvProject":
                        raise ValueError("Archived project is malformed")
                else:
                    _, lines = self._text(data)
                    self._sections(lines)
                sources.append((source, path))
            if {p.name for p in folder.iterdir()} != {"manifest.json", *files}:
                raise ValueError("Archive contains unexpected files")
            moved = []
            try:
                for source, target in sources:
                    os.replace(source, target)
                    moved.append((source, target))
            except BaseException:
                for source, target in reversed(moved):
                    os.replace(target, source)
                raise
            (folder / "manifest.json").unlink()
            folder.rmdir()
            return {"project": project, "restored": list(files)}

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

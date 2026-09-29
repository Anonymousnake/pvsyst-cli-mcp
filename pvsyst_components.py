"""Workspace-scoped management of PVsyst component files.

Only PAN, OND, BTR and GEN are supported. This is a file manager, not a
replacement for PVsyst's physical model validation. Legacy binary PAN files
are copied byte-for-byte and are never decoded or edited.
"""
from __future__ import annotations

import difflib
import hashlib
import itertools
import json
import math
import os
import re
import threading
import uuid
from pathlib import Path

from pvsyst_paths import project_name, project_path, variant_id
from pvsyst_component_editor import curve_inventory, replace_curves, replace_value
from pvsyst_battery_editor import battery_curve_inventory, replace_battery_curve
from pvsyst_commercial import commercial_inventory, replace_commercial


# Workspace spelling follows existing PVsyst workspaces; Windows paths are case-insensitive.
KINDS = {
    "PAN": ("PVmodules", "PVModules", "pvModule"),
    "OND": ("Inverters", "Inverters", "pvGInverter"),
    "BTR": ("Batteries", "Batteries", "pvBattery"),
    "GEN": ("Gensets", "Gensets", "pvGenerator"),
}
COMMON_EDIT = frozenset(("Manufacturer", "Model", "DataSource"))
EDITABLE = {
    "PAN": COMMON_EDIT | {"PNom", "ISC", "Voc", "Imp", "Vmp", "MuISC", "muVocSpec"},
    "OND": COMMON_EDIT | {"PNomConv", "PMaxOUT", "VMppMin", "VMPPMax", "VAbsMax", "EfficMax", "EfficEuro"},
    "BTR": COMMON_EDIT | {"CapNomC10", "CapaRef", "AlphaSOC"},
    "GEN": COMMON_EDIT | {"TypeGen", "PNomGen", "CFuelHor"},
}
NUMERIC = set().union(*(fields - COMMON_EDIT - {"BattTechnol", "TypeGen"}
                        for fields in EDITABLE.values()))
REQUIRED = {
    "PAN": ("Version", "Flags", "Manufacturer", "Model", "Technol", "NCelS", "NCelP", "NDiode", "PNom",
            "LargApp", "LongApp", "GRef", "TRef", "Absorb", "ISC", "Voc", "Imp", "Vmp",
            "MuISC", "muVocSpec"),
    "OND": ("Version", "Flags", "Manufacturer", "Model", "Converter", "PNomConv", "PMaxOUT",
            "VMppMin", "VMPPMax", "VAbsMax", "EfficMax", "ProfilPIO"),
    "BTR": ("Version", "Flags", "Manufacturer", "Model", "BattTechnol", "CapNomC10", "AlphaSOC"),
    "GEN": ("Version", "Flags", "Manufacturer", "Model", "TypeGen", "PNomGen", "CFuelHor"),
}
FIELD = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
REFERENCE = re.compile(r"^\s*(PVModule|GInverter|BatteryFile|GensetFile)\s*=\s*(\S.*?)\s*$")
REF_KIND = {"PVModule": "PAN", "GInverter": "OND", "BatteryFile": "BTR",
            "GensetFile": "GEN"}
MAX_BYTES = 2_000_000
MAX_PROJECT_BYTES = 32_000_000


def inspect_curves(kind: str, text: str) -> dict:
    return battery_curve_inventory(text) if kind == "BTR" else curve_inventory(kind, text)


class ComponentStore:
    def __init__(self, workspace: str | os.PathLike,
                 builtin: str | os.PathLike | None = None,
                 lock: threading.RLock | None = None):
        self.workspace = Path(workspace).resolve(strict=True)
        self.builtin = Path(builtin).resolve() if builtin else None
        self.lock = lock or threading.RLock()

    @staticmethod
    def _kind(kind: str) -> str:
        value = kind.upper()
        if value not in KINDS:
            raise ValueError("Component type must be PAN, OND, BTR or GEN")
        return value

    def _folder(self, kind: str, library: str = "workspace") -> Path:
        if library not in ("workspace", "builtin"):
            raise ValueError("Library must be workspace or builtin")
        if library == "builtin" and self.builtin is None:
            raise ValueError("Installed component library is not configured")
        base = self.workspace / "ComposPV" if library == "workspace" else self.builtin
        if base.is_symlink():
            raise ValueError("Symlink component libraries are not supported")
        root = base.resolve()
        if library == "workspace" and root.parent != self.workspace:
            raise ValueError("Component library resolves outside workspace")
        name = KINDS[kind][0 if library == "workspace" else 1]
        if (base / name).is_symlink():
            raise ValueError("Symlink component folders are not supported")
        folder = (base / name).resolve()
        if folder.parent != root:
            raise ValueError("Component folder resolves outside its library")
        return folder

    def _path(self, kind: str, name: str, library: str = "workspace") -> Path:
        if (not isinstance(name, str) or not name or name in (".", "..")
                or Path(name).name != name or any(c in name for c in "\\/:*?\"<>|")
                or any(ord(c) < 32 for c in name) or name.rstrip(" .") != name
                or len(name) > 180 or Path(name).suffix.upper() != f".{kind}"):
            raise ValueError(f"Expected a {kind} filename without directories")
        folder = self._folder(kind, library)
        if (folder / name).is_symlink():
            raise ValueError("Symlink components are not supported")
        path = (folder / name).resolve()
        if path.parent != folder:
            raise ValueError("Component path resolves outside its library")
        return path

    @staticmethod
    def _read(path: Path) -> bytes:
        if not path.is_file():
            raise FileNotFoundError(path.name)
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Component file exceeds 2 MB limit")
        data = path.read_bytes()
        if len(data) > MAX_BYTES:
            raise ValueError("Component file exceeds 2 MB limit")
        return data

    @staticmethod
    def _text(kind: str, data: bytes) -> tuple[str, str | None]:
        if kind == "PAN" and data[:9] == b"pvModule;":
            return "legacy-binary", None
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Component is neither supported UTF-8 text nor legacy PAN") from exc
        if "\x00" in text or not text.startswith(f"PVObject_={KINDS[kind][2]}"):
            raise ValueError("Component object type or format does not match extension")
        return "text", text

    @staticmethod
    def _fields(text: str) -> dict[str, list[str]]:
        fields: dict[str, list[str]] = {}
        for line in text.splitlines():
            match = FIELD.fullmatch(line)
            if match:
                fields.setdefault(match[1], []).append(match[2])
        return fields

    def _inspect(self, kind: str, data: bytes) -> dict:
        if len(data) > MAX_BYTES:
            raise ValueError("Component file exceeds 2 MB limit")
        format_name, text = self._text(kind, data)
        result = {"type": kind, "format": format_name, "size": len(data),
                  "sha256": hashlib.sha256(data).hexdigest()}
        if text is None:
            return {**result, "structural_errors": [],
                    "warnings": ["Legacy binary PAN: inspect by filename and copy bytes only"]}
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        closing = f"End of PVObject {KINDS[kind][2]}"
        errors = []
        if not lines or lines[0] != f"PVObject_={KINDS[kind][2]}":
            errors.append("Invalid top-level object header")
        if not lines or lines[-1] != closing:
            errors.append("Invalid top-level object closing tag")
        stack = []
        for line in lines:
            start = re.fullmatch(r"PVObject_[A-Za-z0-9_]*\s*=\s*(pv[A-Za-z0-9_]+)", line)
            nested = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\s*=\s*(TConverter|TCubicProfile)", line)
            end = re.fullmatch(r"End of (?:PVObject )?(pv[A-Za-z0-9_]+|TConverter|TCubicProfile)", line)
            if start or nested:
                stack.append((start or nested)[1])
            elif end:
                if not stack or stack.pop() != end[1]:
                    errors.append(f"Unmatched closing tag: {line}")
        if stack:
            errors.append("Unclosed nested object or curve")
        fields = self._fields(text)
        if fields.get("PVObject_Commercial") != ["pvCommercial"]:
            errors.append("Expected one pvCommercial subobject")
        if kind == "OND" and (fields.get("Converter") != ["TConverter"]
                              or fields.get("ProfilPIO") != ["TCubicProfile"]):
            errors.append("Expected TConverter and TCubicProfile subobjects")
        missing = [name for name in REQUIRED[kind] if name not in fields]
        problems = self._numeric_errors(kind, fields)
        return {**result, "manufacturer": fields.get("Manufacturer", [None])[0],
                "model": fields.get("Model", [None])[0], "fields": fields,
                "structural_errors": errors, "warnings": [f"Missing verified field: {name}" for name in missing],
                "known_value_errors": problems}

    @staticmethod
    def _numeric_errors(kind: str, fields: dict[str, list[str]]) -> list[str]:
        errors = []
        numeric = (NUMERIC | {"NCelS", "NCelP", "NDiode", "LargApp", "LongApp",
                              "GRef", "Absorb", "CapaRef"}) & fields.keys()
        values = {}
        for field in numeric:
            try:
                if len(fields[field]) != 1:
                    raise ValueError()
                values[field] = float(fields[field][0])
                if not math.isfinite(values[field]):
                    raise ValueError()
            except ValueError:
                errors.append(f"{field} must be one finite number")
        for field in (("PNom", "ISC", "Voc", "Imp", "Vmp", "NCelS", "NCelP",
                       "NDiode", "LargApp", "LongApp", "GRef")
                      if kind == "PAN" else
                      ("PNomConv", "PMaxOUT", "VMppMin", "VMPPMax", "VAbsMax")
                      if kind == "OND" else
                      ("CapNomC10",) if kind == "BTR" else ("PNomGen", "CFuelHor")):
            if field in values and values[field] <= 0:
                errors.append(f"{field} must be greater than zero")
        if kind == "BTR" and "AlphaSOC" in values and not 0.16 <= values["AlphaSOC"] <= 0.22:
            errors.append("AlphaSOC must be between 0.16 and 0.22")
        if kind == "PAN":
            for field in ("NCelS", "NCelP", "NDiode"):
                if field in values and not values[field].is_integer():
                    errors.append(f"{field} must be an integer")
            for hi, lo in (("Voc", "Vmp"), ("ISC", "Imp")):
                if hi in values and lo in values and values[hi] < values[lo]:
                    errors.append(f"{hi} must be >= {lo}")
        if kind == "OND":
            if all(key in values for key in ("VMppMin", "VMPPMax", "VAbsMax")):
                if not values["VMppMin"] < values["VMPPMax"] <= values["VAbsMax"]:
                    errors.append("Inverter voltage limits must be ordered")
            for field in ("EfficMax", "EfficEuro"):
                if field in values and not 0 < values[field] <= 100:
                    errors.append(f"{field} must be within (0, 100]")
        return errors

    @staticmethod
    def _require_valid(result: dict, *, complete: bool = False) -> None:
        if result["structural_errors"] or result.get("known_value_errors"):
            raise ValueError("Invalid component: " + "; ".join(
                result["structural_errors"] + result.get("known_value_errors", [])))
        if complete and result["format"] != "text":
            raise ValueError("Legacy binary PAN cannot be used as an editable template")
        if complete and result["warnings"]:
            raise ValueError("Incomplete template: " + "; ".join(result["warnings"]))

    def inspect(self, kind: str, name: str, library: str = "workspace",
                offset: int = 0, limit: int = 100) -> dict:
        kind = self._kind(kind)
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("Offset must be >= 0 and limit between 1 and 200")
        path = self._path(kind, name, library)
        data = self._read(path)
        result = self._inspect(kind, data)
        result.update({"name": name, "library": library})
        if result["format"] == "text":
            result["editable_fields"] = sorted(EDITABLE[kind])
            result["curves"] = inspect_curves(kind, self._text(kind, data)[1])
            result["commercial"] = commercial_inventory(kind, self._text(kind, data)[1])
            fields = result.pop("fields")
            result["field_count"] = len(fields)
            result["fields_truncated"] = (len(fields) > 80 or any(
                len(values) > 3 or any(len(value) > 256 for value in values)
                for values in fields.values()))
            result["fields"] = {key: [value[:256] for value in values[:3]]
                                for key, values in list(fields.items())[:80]}
            lines = self._text(kind, data)[1].splitlines()
            page = lines[offset:offset + limit]
            result["total_lines"] = len(lines)
            result["line_truncated"] = any(len(line) > 2000 for line in page)
            result["page_truncated"] = offset + len(page) < len(lines)
            result["lines_truncated"] = result["line_truncated"] or result["page_truncated"]
            result["lines"] = [line[:2000] for line in page]
        return result

    def list(self, kind: str, library: str = "workspace", query: str = "",
             offset: int = 0, limit: int = 100) -> dict:
        kind = self._kind(kind)
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("Offset must be >= 0 and limit between 1 and 200")
        folder = self._folder(kind, library)
        files = sorted((p for p in folder.iterdir() if p.is_file() and not p.is_symlink()
                        and p.suffix.upper() == f".{kind}"),
                       key=lambda path: path.name.casefold()) if folder.is_dir() else []
        matched = []
        for path in files:
            try:
                info = self._inspect(kind, self._read(self._path(kind, path.name, library)))
                if query.casefold() in " ".join((path.name, info.get("manufacturer") or "",
                                                   info.get("model") or "")).casefold():
                    matched.append({"name": path.name, "format": info["format"],
                                    "manufacturer": info.get("manufacturer"), "model": info.get("model")})
            except (ValueError, FileNotFoundError):
                matched.append({"name": path.name, "format": "unrecognized"})
        return {"total": len(matched), "offset": offset, "items": matched[offset:offset + limit]}

    def validate(self, kind: str, name: str, library: str = "workspace") -> dict:
        result = self.inspect(kind, name, library, limit=1)
        result.pop("lines", None)
        result.pop("fields", None)
        result["scope"] = ("legacy header only; copy bytes and run a project for validation"
                           if result["format"] == "legacy-binary" else
                           "static structure and known fields; run a project for model validation")
        return result

    def _backup_folder(self, kind: str) -> Path:
        components = (self.workspace / "ComposPV").resolve()
        if components.parent != self.workspace:
            raise ValueError("Component library resolves outside workspace")
        base = (components / ".mcp-backups").resolve()
        if base.parent != components:
            raise ValueError("Backup folder resolves outside workspace")
        folder = (base / kind).resolve()
        if folder.parent != base:
            raise ValueError("Backup folder resolves outside workspace")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def backup(self, kind: str, name: str) -> dict:
        kind = self._kind(kind)
        with self.lock:
            path = self._path(kind, name)
            data = self._read(path)
            folder = self._backup_folder(kind)
            backup = folder / f"{name}.v2-{uuid.uuid4().hex}.bak"
            with backup.open("xb") as stream:
                stream.write(data)
            try:
                self._write_snapshot_metadata(kind, name, backup, data)
            except BaseException:
                backup.unlink(missing_ok=True)
                raise
            return {"name": name, "backup_name": backup.name, "sha256": hashlib.sha256(data).hexdigest()}

    @staticmethod
    def _write_snapshot_metadata(kind: str, name: str, snapshot: Path, data: bytes) -> None:
        metadata = snapshot.with_name(snapshot.name + ".json")
        created = False
        try:
            with metadata.open("x", encoding="utf-8") as stream:
                created = True
                json.dump({"version": 2, "type": kind, "name": name,
                           "sha256": hashlib.sha256(data).hexdigest()}, stream)
        except BaseException:
            # Only remove a metadata file we opened, never a pre-existing one.
            if created:
                metadata.unlink(missing_ok=True)
            raise

    @staticmethod
    def _verify_snapshot(kind: str, name: str, snapshot: Path, data: bytes) -> bool:
        if not snapshot.name.startswith(name + ".v2-"):
            return False  # Legacy snapshots have no recorded integrity hash.
        metadata = snapshot.with_name(snapshot.name + ".json")
        if (metadata.is_symlink() or metadata.resolve().parent != snapshot.parent
                or not metadata.is_file() or metadata.stat().st_size > 4096):
            raise ValueError("Missing or unsafe component snapshot metadata")
        with metadata.open("r", encoding="utf-8") as stream:
            recorded = json.load(stream)
        if recorded != {"version": 2, "type": kind, "name": name,
                        "sha256": hashlib.sha256(data).hexdigest()}:
            raise ValueError("Component snapshot integrity check failed")
        return True

    def _recovery_info(self, kind: str, data: bytes) -> dict:
        try:
            info = self._inspect(kind, data)
            return {key: info[key] for key in ("format", "structural_errors", "warnings",
                                               "known_value_errors") if key in info}
        except ValueError as exc:
            return {"format": "unrecognized", "structural_errors": [str(exc)], "warnings": []}

    def _new(self, kind: str, name: str, data: bytes, *, recovery: bool = False) -> dict:
        path = self._path(kind, name)
        result = self._recovery_info(kind, data) if recovery else self._inspect(kind, data)
        if not recovery:
            self._require_valid(result, complete=result["format"] == "text")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Windows names are case-insensitive; enforce the same rule in tests on other OSes.
        if any(p.name.casefold() == name.casefold() for p in path.parent.iterdir()):
            raise FileExistsError(name)
        with path.open("xb") as stream:
            stream.write(data)
        return {"name": name, "type": kind, "format": result["format"],
                "sha256": hashlib.sha256(data).hexdigest(),
                **({"validation": result} if recovery else {})}

    def copy(self, kind: str, source_name: str, new_name: str,
             source_library: str = "workspace") -> dict:
        kind = self._kind(kind)
        with self.lock:
            source = self._path(kind, source_name, source_library)
            return self._new(kind, new_name, self._read(source))

    def create(self, kind: str, name: str, content: str) -> dict:
        """Create from complete UTF-8 PVObject_ text, without needing a template."""
        kind = self._kind(kind)
        if not isinstance(content, str):
            raise ValueError("Content must be complete UTF-8 component text")
        with self.lock:
            return self._new(kind, name, content.encode("utf-8"))

    @staticmethod
    def _replace_fields(kind: str, text: str, updates: dict[str, str]) -> str:
        if not updates or any(key not in EDITABLE[kind] for key in updates):
            raise ValueError(f"Editable {kind} fields: {', '.join(sorted(EDITABLE[kind]))}")
        lines = text.splitlines(keepends=True)
        for key, raw in updates.items():
            value = str(raw)
            if not value.strip() or any(c in value for c in "\r\n\x00"):
                raise ValueError(f"Invalid value for {key}")
            if key in NUMERIC:
                try:
                    if not math.isfinite(float(value)):
                        raise ValueError()
                except ValueError:
                    raise ValueError(f"{key} must be a finite number") from None
            matches = [index for index, line in enumerate(lines)
                       if (match := FIELD.fullmatch(line.rstrip("\r\n"))) and match[1] == key]
            if len(matches) != 1:
                raise ValueError(f"{key} must occur exactly once in the template")
            index = matches[0]
            line = lines[index]
            lines[index] = replace_value(line, value)
        return "".join(lines)

    def clone(self, kind: str, source_name: str, new_name: str,
              updates: dict[str, str], source_library: str = "workspace") -> dict:
        kind = self._kind(kind)
        with self.lock:
            if not {"Manufacturer", "Model"}.issubset(updates):
                raise ValueError("Cloning requires new Manufacturer and Model")
            source_data = self._read(self._path(kind, source_name, source_library))
            has_bom = source_data.startswith(b"\xef\xbb\xbf")
            _, source = self._text(kind, source_data)
            if source is None:
                raise ValueError("Legacy binary PAN cannot be edited")
            self._require_valid(self._inspect(kind, source.encode("utf-8")), complete=True)
            candidate = self._replace_fields(kind, source, updates).encode("utf-8")
            if has_bom:
                candidate = b"\xef\xbb\xbf" + candidate
            self._require_valid(self._inspect(kind, candidate), complete=True)
            return self._new(kind, new_name, candidate)

    def update(self, kind: str, name: str, updates: dict[str, str],
               expected_sha256: str | None = None, dry_run: bool = False,
               curve_updates: dict[str, list[list[float]]] | None = None,
               use_file_curve: bool = False,
               commercial_updates: dict[str, str] | None = None,
               remarks: list[str] | None = None) -> dict:
        kind = self._kind(kind)
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        if not isinstance(use_file_curve, bool):
            raise ValueError("use_file_curve must be a boolean")
        if expected_sha256 is not None and (not isinstance(expected_sha256, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256)):
            raise ValueError("expected_sha256 must be a SHA-256 digest")
        if updates is not None and not isinstance(updates, dict):
            raise ValueError("updates must be a dictionary")
        if commercial_updates is not None or remarks is not None:
            if expected_sha256 is None:
                raise ValueError("Commercial editing requires expected_sha256 from inspection")
            if commercial_updates is not None and not isinstance(commercial_updates, dict):
                raise ValueError("commercial_updates must be a dictionary")
            if set(updates or {}) & set(commercial_updates or {}):
                raise ValueError("Supply each field in updates or commercial_updates, not both")
            if set(updates or {}) & COMMON_EDIT:
                raise ValueError("Use commercial_updates for identity fields when editing the commercial form")
        if curve_updates is not None:
            if not isinstance(curve_updates, dict) or not curve_updates:
                raise ValueError("curve_updates must be a nonempty dictionary")
            if expected_sha256 is None:
                raise ValueError("Curve editing requires expected_sha256 from inspection")
        elif use_file_curve:
            raise ValueError("use_file_curve requires explicit curve_updates")
        if not updates and curve_updates is None and not commercial_updates and remarks is None:
            raise ValueError("Supply scalar updates, curve_updates, commercial_updates or remarks")
        with self.lock:
            path = self._path(kind, name)
            data = self._read(path)
            before_sha = hashlib.sha256(data).hexdigest()
            if expected_sha256 is not None and before_sha != expected_sha256.lower():
                raise ValueError("Component changed since inspection; read it again before editing")
            _, text = self._text(kind, data)
            if text is None:
                raise ValueError("Legacy binary PAN cannot be edited")
            edited = self._replace_fields(kind, text, updates) if updates else text
            if commercial_updates is not None or remarks is not None:
                edited = replace_commercial(kind, edited, commercial_updates or {}, remarks)
            if curve_updates is not None:
                edited = (replace_battery_curve(edited, curve_updates, use_file_curve) if kind == "BTR"
                          else replace_curves(kind, edited, curve_updates, use_file_curve))
            candidate = edited.encode("utf-8")
            if data.startswith(b"\xef\xbb\xbf"):
                candidate = b"\xef\xbb\xbf" + candidate
            validation = self._inspect(kind, candidate)
            self._require_valid(validation, complete=True)
            diff = "".join(itertools.islice(difflib.unified_diff(
                text.splitlines(keepends=True), edited.splitlines(keepends=True),
                fromfile=name, tofile=name + " (candidate)"), 501))
            result = {"name": name, "before_sha256": before_sha,
                      "sha256": hashlib.sha256(candidate).hexdigest(),
                      "changed": candidate != data, "dry_run": dry_run,
                      "conflict_checked": expected_sha256 is not None,
                      "diff": diff[:32000],
                      "diff_truncated": len(diff) > 32000 or len(diff.splitlines()) > 500,
                      "validation": {key: validation[key] for key in (
                          "structural_errors", "warnings", "known_value_errors")},
                      "validation_scope": "Static structure and limited values only; simulate a referencing project"}
            if curve_updates is not None:
                result["curve_control_before"] = inspect_curves(kind, text)["control"]
                result["curve_control_after"] = inspect_curves(kind, edited)["control"]
            if commercial_updates is not None or remarks is not None:
                result["commercial_before"] = commercial_inventory(kind, text)
                result["commercial_after"] = commercial_inventory(kind, edited)
            if dry_run or candidate == data:
                return result
            if self._read(path) != data:
                raise ValueError("Component changed while preparing the edit")
            backup = self.backup(kind, name)
            temporary = path.with_name(f".{name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(candidate)
                if backup["sha256"] != before_sha or self._read(path) != data:
                    raise ValueError("Component changed while preparing the edit")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {**result, "backup_name": backup["backup_name"]}

    def restore(self, kind: str, name: str, backup_name: str, confirm: bool = False) -> dict:
        kind = self._kind(kind)
        if not confirm:
            raise ValueError("Restore requires confirm=true")
        if not re.fullmatch(re.escape(name) + r"\.(?:v2-)?[0-9a-f]{32}\.bak", backup_name):
            raise ValueError("Backup name must match the component")
        with self.lock:
            path = self._path(kind, name)
            backup_path = self._backup_folder(kind) / backup_name
            if backup_path.is_symlink():
                raise ValueError("Symlink backups are not supported")
            source = backup_path.resolve(strict=True)
            if source.parent != self._backup_folder(kind):
                raise ValueError("Backup resolves outside workspace")
            data = self._read(source)
            verified = self._verify_snapshot(kind, name, source, data)
            validation = self._recovery_info(kind, data)
            previous = self.backup(kind, name) if path.exists() else None
            temporary = path.with_name(f".{name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(data)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {"name": name, "restored_from": backup_name,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "integrity_verified": verified, "validation": validation,
                    "previous_backup": previous["backup_name"] if previous else None}

    def _archive_folder(self, kind: str) -> Path:
        components = (self.workspace / "ComposPV").resolve()
        if components.parent != self.workspace:
            raise ValueError("Component library resolves outside workspace")
        base = (components / ".mcp-archive").resolve()
        if base.parent != components:
            raise ValueError("Archive folder resolves outside workspace")
        folder = (base / kind).resolve()
        if folder.parent != base:
            raise ValueError("Archive folder resolves outside workspace")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _project_uses(self, kind: str, name: str) -> list[str]:
        root = (self.workspace / "Projects").resolve()
        if root.parent != self.workspace:
            raise ValueError("Projects folder resolves outside workspace")
        if not root.is_dir():
            return []
        matches = []
        directories = [root]
        while directories:
            directory = directories.pop()
            for path in directory.iterdir():
                relative = path.relative_to(root).as_posix()
                if path.is_symlink() or path.resolve() != path:
                    if path.is_dir() or path.suffix.upper() == ".PRJ" or re.fullmatch(
                            r"\.VC[A-Za-z0-9]+", path.suffix, re.I):
                        raise ValueError(f"Cannot safely inspect symlink project path: {relative}")
                    continue
                if path.is_dir():
                    directories.append(path)
                    continue
                relevant = (path.suffix.upper() == ".PRJ"
                            or re.fullmatch(r"\.VC[A-Za-z0-9]+", path.suffix, re.I))
                if not relevant or not path.is_file():
                    continue
                if path.stat().st_size > MAX_PROJECT_BYTES:
                    raise ValueError(f"Cannot check references in oversized project file: {relative}")
                try:
                    with path.open("r", encoding="utf-8-sig") as stream:
                        for line in stream:
                            match = REFERENCE.fullmatch(line.rstrip("\r\n"))
                            if (match and REF_KIND[match[1]] == kind
                                    and match[2].casefold() == name.casefold()):
                                matches.append(relative)
                                break
                except (UnicodeDecodeError, OSError) as exc:
                    raise ValueError(f"Cannot safely inspect project references: {relative}") from exc
        return sorted(matches)

    def archive(self, kind: str, name: str, confirm: bool = False) -> dict:
        kind = self._kind(kind)
        if not confirm:
            raise ValueError("Archive requires confirm=true")
        with self.lock:
            path = self._path(kind, name)
            data = self._read(path)
            users = self._project_uses(kind, name)
            if users:
                raise ValueError("Component is still referenced by: " + ", ".join(users[:10]))
            archived = self._archive_folder(kind) / f"{name}.v2-{uuid.uuid4().hex}.archived"
            self._write_snapshot_metadata(kind, name, archived, data)
            try:
                os.replace(path, archived)
            except BaseException:
                archived.with_name(archived.name + ".json").unlink(missing_ok=True)
                raise
            return {"name": name, "archive_name": archived.name,
                    "sha256": hashlib.sha256(data).hexdigest()}

    def restore_archive(self, kind: str, name: str, archive_name: str,
                        confirm: bool = False) -> dict:
        kind = self._kind(kind)
        if not confirm:
            raise ValueError("Archive restore requires confirm=true")
        self._path(kind, name)
        if not re.fullmatch(re.escape(name) + r"\.(?:v2-)?[0-9a-f]{32}\.archived", archive_name):
            raise ValueError("Archive name must match the component")
        with self.lock:
            archived_path = self._archive_folder(kind) / archive_name
            if archived_path.is_symlink():
                raise ValueError("Symlink archives are not supported")
            source = archived_path.resolve(strict=True)
            if source.parent != self._archive_folder(kind):
                raise ValueError("Archive resolves outside workspace")
            data = self._read(source)
            verified = self._verify_snapshot(kind, name, source, data)
            result = self._new(kind, name, data, recovery=True)
            return {**result, "restored_from": archive_name, "integrity_verified": verified}

    def compare(self, kind: str, first: str, second: str,
                second_library: str = "workspace") -> dict:
        kind = self._kind(kind)
        before = self._read(self._path(kind, first))
        after = self._read(self._path(kind, second, second_library))
        info_a, text_a = self._text(kind, before)
        info_b, text_b = self._text(kind, after)
        result = {"same_bytes": before == after, "first_format": info_a,
                  "second_format": info_b,
                  "first_sha256": hashlib.sha256(before).hexdigest(),
                  "second_sha256": hashlib.sha256(after).hexdigest()}
        if text_a is not None and text_b is not None:
            changes = difflib.unified_diff(text_a.splitlines(), text_b.splitlines(),
                                           fromfile=first, tofile=second, lineterm="")
            first_lines = list(itertools.islice(changes, 151))
            result["diff_truncated"] = len(first_lines) > 150 or any(
                len(line) > 2000 for line in first_lines)
            result["diff"] = [line[:2000] for line in first_lines[:150]]
        return result

    def dependencies(self, project: str, variant: str) -> dict:
        if (not isinstance(project, str) or project in (".", "..")
                or project.rstrip(" .") != project):
            raise ValueError("Expected project basename and variant ID")
        project = project_name(project if project.upper().endswith(".PRJ") else project + ".PRJ")
        base = project[:-4]
        variant = variant_id(variant)
        refs = []
        variant_text = ""
        for identifier in (None, variant):
            path = project_path(self.workspace, project, identifier)
            filename = path.name
            if path.stat().st_size > MAX_PROJECT_BYTES:
                raise ValueError("Project or variant exceeds 32 MB")
            try:
                data = path.read_bytes()
                if len(data) > MAX_PROJECT_BYTES:
                    raise ValueError("Project or variant exceeds 32 MB")
                text = data.decode("utf-8-sig").replace("\r\n", "\n")
            except UnicodeDecodeError as exc:
                raise ValueError(f"Cannot safely inspect project references: {filename}") from exc
            if filename.upper().endswith(f".{variant}".upper()):
                variant_text = text
            for line_no, line in enumerate(text.splitlines(), 1):
                match = REFERENCE.fullmatch(line)
                if not match:
                    continue
                kind = REF_KIND[match[1]]
                name = match[2]
                try:
                    workspace = self._path(kind, name).is_file()
                    builtin = self._path(kind, name, "builtin").is_file() if self.builtin else False
                    status = "workspace" if workspace else "builtin-file" if builtin else "unknown-or-builtin-db"
                except ValueError:
                    status = "invalid-reference"
                refs.append({"source": filename, "line": line_no, "field": match[1],
                             "type": kind, "name": name, "status": status})
        section = re.search(r"(?ms)^[ \t]*PVObject_System=pvSystem[ \t]*$\n"
                            r"(?P<body>.*?)^[ \t]*End of PVObject pvSystem[ \t]*$",
                            variant_text)
        generator = None
        if any(ref["type"] == "GEN" for ref in refs):
            body = section["body"] if section else ""
            flags = re.search(r"(?m)^[ \t]*Flags=\$([0-9A-Fa-f]+)[ \t]*$", body)
            power = re.search(r"(?m)^[ \t]*PEffBackUp=([^\r\n]+)", body)
            try:
                effective_power = float(power[1]) if power else None
                if effective_power is not None and not math.isfinite(effective_power):
                    effective_power = None
            except ValueError:
                effective_power = None
            subarrays = []
            for index, array in enumerate(re.finditer(
                    r"(?ms)^[ \t]*PVObject_[A-Za-z0-9_]*=pvSubArray[ \t]*$\n"
                    r"(?P<body>.*?)^[ \t]*End of PVObject pvSubArray[ \t]*$",
                    variant_text)):
                array_body = array["body"]
                enclosure = re.search(r"(?m)^[ \t]*VBkUpEncl_syst=([^\r\n]+)", array_body)
                release = re.search(r"(?m)^[ \t]*VBkUpDecl_syst=([^\r\n]+)", array_body)
                subarrays.append({"index": index, "line": variant_text.count("\n", 0, array.start()) + 1,
                                  "enclosure": enclosure[1].strip() if enclosure else None,
                                  "release": release[1].strip() if release else None})
            generator = {"enabled_flag": bool(int(flags[1], 16) & 0x20) if flags else False,
                         "effective_backup_kw": effective_power,
                         "subarray_thresholds": subarrays,
                         "enclosure_threshold_present": any(item["enclosure"] is not None
                                                            for item in subarrays),
                         "release_threshold_present": any(item["release"] is not None
                                                          for item in subarrays),
                         "note": "File reference alone does not activate a generator; inspect simulation outputs"}
        return {"project": f"{base}.PRJ", "variant": variant, "references": refs,
                "generator_configuration": generator,
                "note": "Encrypted built-in databases are not enumerable; unknown is not proof of absence"}

"""Workspace-scoped management of PVsyst component files (PAN, OND, BTR, GEN, REV)."""
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

from pvsyst_auto.components.builder import (
    battery_curve_inventory,
    commercial_inventory,
    replace_battery_curve,
    replace_commercial,
)
from pvsyst_auto.components.editor import (
    NUMBER,
    OBJECT_CLOSE_ALIAS,
    PAN_ALIASES,
    PAN_CANONICAL,
    PAN_DIMENSIONS,
    curve_inventory,
    export_curve_csv,
    import_curve_csv,
    objects,
    pan_blocks,
    pan_validation_fields,
    replace_curves,
    replace_value,
)
from pvsyst_auto.project.paths import project_name, project_path, replace_checked, variant_id, write_json

KINDS = {
    "PAN": ("PVmodules", "PVModules", "pvModule"),
    "OND": ("Inverters", "Inverters", "pvGInverter"),
    "BTR": ("Batteries", "Batteries", "pvBattery"),
    "GEN": ("Gensets", "Gensets", "pvGenerator"),
    "REV": ("Sellers", "Sellers", "pvAddress"),
}
COMMON_EDIT = frozenset(("Manufacturer", "Model", "DataSource"))
EDITABLE = {
    "PAN": COMMON_EDIT | {"PNom", "ISC", "Isc", "Voc", "Imp", "Vmp", "MuISC", "muISC", "muVocSpec"},
    "OND": COMMON_EDIT | {"PNomConv", "PMaxOUT", "VMppMin", "VMPPMax", "VAbsMax", "EfficMax", "EfficEuro"},
    "BTR": COMMON_EDIT | {"CapNomC10", "CapaRef", "AlphaSOC"},
    "GEN": COMMON_EDIT | {"TypeGen", "PNomGen", "CFuelHor"},
    "REV": frozenset({"Comment", "Person", "Address", "Town", "Country"}),
}
NUMERIC = set().union(*(f - COMMON_EDIT - {"BattTechnol", "TypeGen"} for k, f in EDITABLE.items() if k != "REV")) - {"Isc", "muISC"}
REQUIRED = {
    "PAN": ("Version", "Flags", "Manufacturer", "Model", "Technol", "NCelS", "NCelP", "NDiode", "PNom",
            "GRef", "TRef", "Absorb", "ISC", "Voc", "Imp", "Vmp", "MuISC", "muVocSpec"),
    "OND": ("Version", "Flags", "Manufacturer", "Model", "Converter", "PNomConv", "PMaxOUT",
            "VMppMin", "VMPPMax", "VAbsMax", "EfficMax", "ProfilPIO"),
    "BTR": ("Version", "Flags", "Manufacturer", "Model", "BattTechnol", "CapNomC10", "AlphaSOC"),
    "GEN": ("Version", "Flags", "Manufacturer", "Model", "TypeGen", "PNomGen", "CFuelHor"),
    "REV": ("Version", "Flags", "Comment"),
}
FIELD = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
REFERENCE = re.compile(r"^\s*(PVModule|GInverter|BatteryFile|GensetFile)\s*=\s*(\S.*?)\s*$")
REF_KIND = {"PVModule": "PAN", "GInverter": "OND", "BatteryFile": "BTR", "GensetFile": "GEN"}
MAX_BYTES = 2_000_000
MAX_PROJECT_BYTES = 32_000_000


def inspect_curves(kind: str, text: str) -> dict:
    return battery_curve_inventory(text) if kind == "BTR" else curve_inventory(kind, text)


def electrical_inventory(kind: str, text: str) -> dict:
    """Read declared reference-point parameters in their owning PAN/OND/BTR objects."""
    result = {"fields": {}, "issues": [], "unverified": []}
    try:
        blocks = objects(text)
        root, = [b for b in blocks if not b.path]
        if kind == "PAN":
            root, _ = pan_blocks(text)
            specs = {k: (u, root) for k, u in (("PNom", "W"), ("GRef", "W/m2"), ("TRef", "degC"), ("Voc", "V"),
                                               ("Vmp", "V"), ("Imp", "A"), ("ISC", "A"), ("VMaxIEC", "V"), ("VMaxUL", "V"))}
            for name in ("muVocSpec", "MuISC", "muISC", "muPmpReq"):
                if name in root.fields:
                    result["unverified"].append({"field": name, "raw": [v[:256] for _, v in root.fields[name]], "unit": None,
                        "reason": "Serialized coefficient units/model use require separate verification; no conversion assumed"})
        elif kind == "BTR":
            specs = {k: (u, root) for k, u in (("CapNomC10", "Ah"), ("VNomCell", "V"), ("NCellSerie", "count"),
                                               ("NCellParall", "count"), ("ResInterne", "Ohm"), ("EfficI", "ratio"),
                                               ("IMaxCharge", "A"), ("IMaxDischarge", "A"))}
        else:
            conv, = [b for b in blocks if b.path == "Converter" and b.object_type == "TConverter"]
            result["modes"] = {n: conv.fields[n][0][1] if len(conv.fields.get(n, [])) == 1 else None
                               for n in ("ModeOper", "CompPMax", "CompVMax", "IsHybrid")}
            specs = {**{k: (u, conv) for k, u in (("PNomConv", "kW"), ("PMaxOUT", "kW"), ("VMppMin", "V"),
                                                  ("VMPPMax", "V"), ("VAbsMax", "V"), ("IDCMax", "A"))},
                     "NbInputs": ("count", root), "NbMPPT": ("count", root)}
        for name, (unit, owner) in specs.items():
            aliases = PAN_ALIASES.get(name, (name,)) if kind == "PAN" else (name,)
            values = [(alias, raw) for alias in aliases for _, raw in owner.fields.get(alias, [])]
            value, status = None, "missing" if not values else "ambiguous"
            if len(values) == 1:
                try:
                    value = float(values[0][1])
                    if not math.isfinite(value) or (name != "TRef" and value <= 0) or (unit == "count" and not value.is_integer()):
                        status = "not_declared" if name == "IDCMax" and value == 0 else "invalid"
                        value = 0 if status == "not_declared" else None
                    else:
                        status = "declared"
                        if unit == "count":
                            value = int(value)
                except ValueError:
                    status = "invalid"
            result["fields"][name] = {"value": value, "unit": unit, "status": status, "path": owner.path or "root",
                                      "source_key": values[0][0] if len(values) == 1 else None}
            if status != "declared":
                result["issues"].append(f"{name}: {status}; not a verified design constraint")
        if kind == "OND":
            if result["modes"].get("IsHybrid") == "True":
                result["unverified"].append({"field": "IsHybrid", "unit": None,
                    "reason": "OND IsHybrid=True is catalog metadata only; PVsyst 8.1.7 only simulates Common AC Bus grid storage (pvGridStorage) and does not expose DC-coupled battery ports"})
            result["unverified"].append({"field": "per_MPPT_limits", "unit": None,
                "reason": "Converter current and hardware MPPT counts do not establish per-input limits, short-circuit ratings or string allocation"})
        result["status"] = "readable" if result["fields"] else "unreadable"
    except ValueError as exc:
        result.update(status="unreadable", issues=[str(exc)])
    return result


class ComponentStore:
    def __init__(self, workspace: str | os.PathLike, builtin: str | os.PathLike | None = None,
                 lock: threading.RLock | None = None):
        self.workspace = Path(workspace).resolve(strict=True)
        self.builtin = Path(builtin).resolve() if builtin else None
        self.lock = lock or threading.RLock()
        self._meta_cache: dict[tuple[str, int, int], dict] = {}

    @staticmethod
    def _kind(kind: str) -> str:
        value = kind.upper()
        if value not in KINDS:
            raise ValueError("Component type must be PAN, OND, BTR, GEN or REV")
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
        if (not isinstance(name, str) or not name or name in (".", "..") or Path(name).name != name
                or any(c in name for c in "\\/:*?\"<>|") or any(ord(c) < 32 for c in name)
                or name.rstrip(" .") != name or len(name) > 180 or Path(name).suffix.upper() != f".{kind}"):
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
            if match := FIELD.fullmatch(line):
                fields.setdefault(match[1], []).append(match[2])
        return fields

    @staticmethod
    def _owned_identity(text: str, fields: dict[str, list[str]]) -> dict[str, list[str]]:
        blocks = objects(text)
        owners = [b for b in blocks if b.path == "PVObject_Commercial"] + [b for b in blocks if not b.path]
        checked = dict(fields)
        for key in ("Manufacturer", "Model"):
            checked.pop(key, None)
            if (owner := next((b for b in owners if key in b.fields), None)) is not None:
                checked[key] = [v for _, v in owner.fields[key]]
        return checked

    def _inspect(self, kind: str, data: bytes) -> dict:
        if len(data) > MAX_BYTES:
            raise ValueError("Component file exceeds 2 MB limit")
        format_name, text = self._text(kind, data)
        result = {"type": kind, "format": format_name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if text is None:
            return {**result, "structural_errors": [], "warnings": ["Legacy binary PAN: inspect by filename and copy bytes only"]}
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        errors = []
        if not lines or lines[0] != f"PVObject_={KINDS[kind][2]}":
            errors.append("Invalid top-level object header")
        if not lines or lines[-1] != f"End of PVObject {KINDS[kind][2]}":
            errors.append("Invalid top-level object closing tag")
        stack = []
        for line in lines:
            start = re.fullmatch(r"PVObject_[A-Za-z0-9_]*\s*=\s*(pv[A-Za-z0-9_]+)", line)
            nested = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\s*=\s*(TConverter|TCubicProfile)", line)
            end = re.fullmatch(r"End of (?:PVObject )?(pv[A-Za-z0-9_]+|TConverter|TCubicProfile|TAgeing)", line)
            if start or nested:
                stack.append((start or nested)[1])
            elif end and (not stack or stack.pop() != OBJECT_CLOSE_ALIAS.get(end[1], end[1])):
                errors.append(f"Unmatched closing tag: {line}")
        if stack:
            errors.append("Unclosed nested object or curve")
        fields = self._fields(text)
        if kind != "REV" and fields.get("PVObject_Commercial") != ["pvCommercial"]:
            errors.append("Expected one pvCommercial subobject")
        if kind == "OND" and (fields.get("Converter") != ["TConverter"] or fields.get("ProfilPIO") != ["TCubicProfile"]):
            errors.append("Expected TConverter and TCubicProfile subobjects")
        checked_fields = fields
        if not errors:
            try:
                checked_fields = pan_validation_fields(text) if kind == "PAN" else self._owned_identity(text, fields)
            except ValueError as exc:
                errors.append(str(exc))
        if kind == "BTR":
            technol = (checked_fields.get("BattTechnol") or [""])[0].casefold().removeprefix("bt")
            missing = [n for n in ("Version", "Flags", "Manufacturer", "Model", "BattTechnol") if n not in checked_fields]
            if technol.startswith("li_"):
                if "CapNomC10" not in checked_fields and "CapaRef" not in checked_fields:
                    missing.append("CapNomC10 or CapaRef")
            else:
                missing.extend(n for n in ("CapNomC10", "AlphaSOC") if n not in checked_fields)
        else:
            missing = [n for n in REQUIRED[kind] if n not in checked_fields]
        if kind == "PAN":
            missing.extend(f"{root} or PVObject_Commercial/{form}" for form, root in PAN_DIMENSIONS.items()
                           if root not in checked_fields and form not in checked_fields)
        return {**result, "manufacturer": checked_fields.get("Manufacturer", [None])[0],
                "model": checked_fields.get("Model", [None])[0], "fields": fields, "structural_errors": errors,
                "warnings": [f"Missing verified field: {n}" for n in missing],
                "known_value_errors": self._numeric_errors(kind, checked_fields)}

    @staticmethod
    def _numeric_errors(kind: str, fields: dict[str, list[str]]) -> list[str]:
        errors = []
        numeric = (NUMERIC | {"NCelS", "NCelP", "NDiode", "LargApp", "LongApp", "GRef", "Absorb", "CapaRef"}) & fields.keys()
        if kind == "PAN":
            numeric |= {"Width", "Height"} & fields.keys()
        values = {}
        for fld in numeric:
            try:
                if len(fields[fld]) != 1:
                    raise ValueError()
                val = float(fields[fld][0])
                if not math.isfinite(val):
                    raise ValueError()
                values[fld] = val
            except ValueError:
                errors.append(f"{fld} must be one finite number")
        positive = {"PAN": ("PNom", "ISC", "Voc", "Imp", "Vmp", "NCelS", "NCelP", "NDiode", "LargApp", "LongApp", "Width", "Height", "GRef"),
                    "OND": ("PNomConv", "PMaxOUT", "VMppMin", "VMPPMax", "VAbsMax"),
                    "BTR": ("CapNomC10",), "GEN": ("PNomGen", "CFuelHor")}.get(kind, ())
        for fld in positive:
            if fld in values and values[fld] <= 0:
                errors.append(f"{fld} must be greater than zero")
        if kind == "BTR" and "AlphaSOC" in values:
            technol = (fields.get("BattTechnol") or [""])[0].casefold().removeprefix("bt")
            lo_a, hi_a = (0.12, 0.90) if technol.startswith("li_") else (0.16, 0.22)
            if not lo_a <= values["AlphaSOC"] <= hi_a:
                errors.append(f"AlphaSOC must be between {lo_a:.2f} and {hi_a:.2f}")
        if kind == "PAN":
            errors.extend(f"{f} must be an integer" for f in ("NCelS", "NCelP", "NDiode") if f in values and not values[f].is_integer())
            errors.extend(f"{hi} must be greater than {lo}" for hi, lo in (("Voc", "Vmp"), ("ISC", "Imp"))
                          if hi in values and lo in values and values[hi] <= values[lo])
        if kind == "OND":
            if all(k in values for k in ("VMppMin", "VMPPMax", "VAbsMax")) and not values["VMppMin"] < values["VMPPMax"] <= values["VAbsMax"]:
                errors.append("Inverter voltage limits must be ordered")
            errors.extend(f"{f} must be within (0, 100]" for f in ("EfficMax", "EfficEuro") if f in values and not 0 < values[f] <= 100)
        return errors

    @staticmethod
    def _require_valid(result: dict, *, complete: bool = False) -> None:
        if result["structural_errors"] or result.get("known_value_errors"):
            raise ValueError("Invalid component: " + "; ".join(result["structural_errors"] + result.get("known_value_errors", [])))
        if complete and result["format"] != "text":
            raise ValueError("Legacy binary PAN cannot be used as an editable template")
        if complete and result["warnings"]:
            raise ValueError("Incomplete template: " + "; ".join(result["warnings"]))

    def inspect(self, kind: str, name: str, library: str = "workspace",
                offset: int = 0, limit: int = 100, include_curve_csv: bool = False) -> dict:
        kind = self._kind(kind)
        if not isinstance(include_curve_csv, bool):
            raise ValueError("include_curve_csv must be a boolean")
        if include_curve_csv and kind not in ("OND", "BTR"):
            raise ValueError("CSV curve export supports OND and BTR only")
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("Offset must be >= 0 and limit between 1 and 200")
        data = self._read(self._path(kind, name, library))
        result = self._inspect(kind, data)
        result.update({"name": name, "library": library})
        if result["format"] == "text":
            text = self._text(kind, data)[1]
            result["editable_fields"] = sorted(EDITABLE[kind])
            if kind == "PAN":
                result["scalar_aliases"] = PAN_ALIASES
            if kind == "REV":
                result["curves"] = {"items": [], "truncated": False}
                result["commercial"] = {"editable": False, "fields": {}, "errors": [],
                                        "remarks": [], "remarks_count": 0, "remarks_truncated": False}
            else:
                result["curves"] = inspect_curves(kind, text)
                if include_curve_csv:
                    result.update(export_curve_csv(kind, result["curves"]))
                result["commercial"] = commercial_inventory(kind, text)
                if kind in ("PAN", "OND"):
                    result["electrical"] = electrical_inventory(kind, text)
            fields = result.pop("fields")
            result["field_count"] = len(fields)
            result["fields_truncated"] = len(fields) > 80 or any(len(vs) > 3 or any(len(v) > 256 for v in vs) for vs in fields.values())
            result["fields"] = {k: [v[:256] for v in vs[:3]] for k, vs in list(fields.items())[:80]}
            lines = text.splitlines()
            page = lines[offset:offset + limit]
            result["total_lines"] = len(lines)
            result["line_truncated"] = any(len(ln) > 2000 for ln in page)
            result["page_truncated"] = offset + len(page) < len(lines)
            result["lines_truncated"] = result["line_truncated"] or result["page_truncated"]
            result["lines"] = [ln[:2000] for ln in page]
        return result

    @staticmethod
    def _parse_float(values: list[str] | None) -> float | None:
        if not values:
            return None
        try:
            num = float(values[0])
            return num if math.isfinite(num) else None
        except (TypeError, ValueError):
            return None

    def _extract_specs(self, kind: str, text: str | None) -> dict:
        if not text:
            return {}
        fields = self._fields(text)
        specs: dict = {}
        key_maps = {
            "PAN": (("PNom", "pnom_w"), ("Voc", "voc_v"), ("Vmp", "vmp_v"), ("Imp", "imp_a"), ("BifacialityFactor", "bifaciality_factor")),
            "OND": (("PNomConv", "pnom_ac_kw"), ("PMaxOUT", "pmax_ac_kw"), ("VMppMin", "vmpp_min_v"), ("VMPPMax", "vmpp_max_v"), ("VAbsMax", "vabs_max_v"), ("EfficMax", "effic_max_pct")),
        }
        for raw_key, out_key in key_maps.get(kind, ()):
            if (val := self._parse_float(fields.get(raw_key))) is not None:
                specs[out_key] = val
        if kind == "PAN":
            if (isc := self._parse_float(fields.get("ISC") or fields.get("Isc"))) is not None:
                specs["isc_a"] = isc
            flags_raw = (fields.get("Flags") or [""])[0]
            flag_bifi = bool(re.fullmatch(r"\$[0-9a-fA-F]{1,8}", flags_raw) and (int(flags_raw[1:], 16) & 0x00800000))
            specs["bifacial"] = bool(flag_bifi or (specs.get("bifaciality_factor") is not None and specs["bifaciality_factor"] > 0))
            vmax = max((v for v in (self._parse_float(fields.get("VMaxIEC")), self._parse_float(fields.get("VMaxUL"))) if v is not None), default=None)
            if vmax is not None:
                specs["vmax_v"] = vmax
        elif kind == "OND":
            if "pnom_ac_kw" in specs:
                specs["pnom_w"] = round(specs["pnom_ac_kw"] * 1000.0, 2)
            if (mppt := self._parse_float(fields.get("NbMPPT"))) is not None:
                specs["nb_mppt"] = int(mppt)
        elif kind == "BTR":
            if (cap := self._parse_float(fields.get("CapNomC10") or fields.get("CapaRef"))) is not None:
                specs["cap_nom_ah"] = cap
        elif kind == "GEN":
            if (pgen := self._parse_float(fields.get("PNomGen"))) is not None:
                specs["pnom_gen_kva"] = pgen
                specs["pnom_w"] = round(pgen * 1000.0, 2)
        return specs

    def _cached_summary(self, kind: str, path: Path, library: str) -> dict:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if key in self._meta_cache:
            return dict(self._meta_cache[key])
        data = self._read(self._path(kind, path.name, library))
        info = self._inspect(kind, data)
        entry = {"name": path.name, "format": info["format"], "manufacturer": info.get("manufacturer"),
                 "model": info.get("model"), "specs": self._extract_specs(kind, self._text(kind, data)[1])}
        self._meta_cache[key] = entry
        return dict(entry)

    def _persist_workspace_index(self, kind: str, items: list[dict]) -> None:
        compos = self.workspace / "ComposPV"
        index_path = compos / ".mcp-component-index.json"
        if not compos.is_dir() or compos.is_symlink() or index_path.is_symlink():
            return
        try:
            existing = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {}
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, ValueError):
            existing = {}
        existing[kind] = items
        try:
            write_json(index_path, existing)
        except OSError:
            pass

    def list(self, kind: str, library: str = "workspace", query: str = "", offset: int = 0, limit: int = 100, *,
             min_power_w: float | None = None, max_power_w: float | None = None, bifacial: bool | None = None,
             min_system_voltage_v: float | None = None, min_mppt: int | None = None, manufacturer: str | None = None) -> dict:
        kind = self._kind(kind)
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("Offset must be >= 0 and limit between 1 and 200")
        for fname, fval in (("min_power_w", min_power_w), ("max_power_w", max_power_w), ("min_system_voltage_v", min_system_voltage_v)):
            if fval is not None and (isinstance(fval, bool) or not isinstance(fval, (int, float)) or not math.isfinite(fval) or fval < 0):
                raise ValueError(f"{fname} must be a non-negative finite number")
        if min_power_w is not None and max_power_w is not None and min_power_w > max_power_w:
            raise ValueError("min_power_w must be <= max_power_w")
        if bifacial is not None and type(bifacial) is not bool:
            raise ValueError("bifacial filter must be a strict boolean")
        if min_mppt is not None and (type(min_mppt) is not int or min_mppt < 1):
            raise ValueError("min_mppt must be a positive integer")
        has_elec_filter = any(v is not None for v in (min_power_w, max_power_w, bifacial, min_system_voltage_v, min_mppt, manufacturer))

        folder = self._folder(kind, library)
        files = sorted((p for p in folder.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.upper() == f".{kind}"),
                       key=lambda p: p.name.casefold()) if folder.is_dir() else []
        all_indexed, matched = [], []
        for path in files:
            try:
                entry = self._cached_summary(kind, path, library)
                all_indexed.append(entry)
                if query and query.casefold() not in " ".join((path.name, entry.get("manufacturer") or "", entry.get("model") or "")).casefold():
                    continue
                if manufacturer and manufacturer.casefold() not in (entry.get("manufacturer") or "").casefold():
                    continue
                specs = entry.get("specs") or {}
                pw = specs.get("pnom_w")
                if (min_power_w is not None or max_power_w is not None) and (pw is None or (min_power_w is not None and pw < min_power_w) or (max_power_w is not None and pw > max_power_w)):
                    continue
                if bifacial is not None and specs.get("bifacial") is not bifacial:
                    continue
                if min_system_voltage_v is not None:
                    vmax = specs.get("vmax_v") if kind == "PAN" else specs.get("vabs_max_v")
                    if vmax is None or vmax < min_system_voltage_v:
                        continue
                if min_mppt is not None and ((mppt := specs.get("nb_mppt")) is None or mppt < min_mppt):
                    continue
                matched.append(entry)
            except (ValueError, FileNotFoundError):
                if not has_elec_filter and not query:
                    matched.append({"name": path.name, "format": "unrecognized"})
        if library == "workspace" and all_indexed:
            self._persist_workspace_index(kind, all_indexed)
        return {"total": len(matched), "offset": offset, "items": matched[offset:offset + limit]}

    def validate(self, kind: str, name: str, library: str = "workspace") -> dict:
        result = self.inspect(kind, name, library, limit=1)
        result.pop("lines", None)
        result.pop("fields", None)
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
            data = self._read(self._path(kind, name))
            backup = self._backup_folder(kind) / f"{name}.v2-{uuid.uuid4().hex}.bak"
            with backup.open("xb") as stream:
                stream.write(data)
            try:
                metadata = backup.with_name(backup.name + ".json")
                created = False
                try:
                    with metadata.open("x", encoding="utf-8") as stream:
                        created = True
                        json.dump({"version": 2, "type": kind, "name": name, "sha256": hashlib.sha256(data).hexdigest()}, stream)
                except BaseException:
                    if created:
                        metadata.unlink(missing_ok=True)
                    raise
            except BaseException:
                backup.unlink(missing_ok=True)
                raise
            return {"name": name, "backup_name": backup.name, "sha256": hashlib.sha256(data).hexdigest()}

    def _new(self, kind: str, name: str, data: bytes) -> dict:
        path = self._path(kind, name)
        result = self._inspect(kind, data)
        self._require_valid(result, complete=result["format"] == "text")
        path.parent.mkdir(parents=True, exist_ok=True)
        if any(p.name.casefold() == name.casefold() for p in path.parent.iterdir()):
            raise FileExistsError(name)
        with path.open("xb") as stream:
            stream.write(data)
        return {"name": name, "type": kind, "format": result["format"], "sha256": hashlib.sha256(data).hexdigest()}

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
        pan_root = pan_blocks(text)[0] if kind == "PAN" else None
        canonical_keys = [PAN_CANONICAL.get(k, k) for k in updates] if kind == "PAN" else list(updates)
        if len(canonical_keys) != len(set(canonical_keys)):
            raise ValueError("Supply each PAN physical field once, not multiple spellings")
        for key, raw in updates.items():
            value = str(raw)
            if not value.strip() or any(c in value for c in "\r\n\x00"):
                raise ValueError(f"Invalid value for {key}")
            numeric_key = PAN_CANONICAL.get(key, key) if kind == "PAN" else key
            if numeric_key in NUMERIC and (not NUMBER.fullmatch(value) or not math.isfinite(float(value))):
                raise ValueError(f"{key} must be a finite number")
            if pan_root is not None and key not in COMMON_EDIT:
                aliases = PAN_ALIASES.get(PAN_CANONICAL.get(key, key), (key,))
                matches = [index for alias in aliases for index, _ in pan_root.fields.get(alias, [])]
            else:
                matches = [index for index, line in enumerate(lines)
                           if (match := FIELD.fullmatch(line.rstrip("\r\n"))) and match[1] == key]
            if len(matches) != 1:
                raise ValueError(f"{key} must occur exactly once in the template")
            lines[matches[0]] = replace_value(lines[matches[0]], value)
        return "".join(lines)

    def update(self, kind: str, name: str, updates: dict[str, str], expected_sha256: str | None = None,
               dry_run: bool = False, curve_updates: dict[str, list[list[float]]] | None = None,
               use_file_curve: bool = False, commercial_updates: dict[str, str] | None = None,
               remarks: list[str] | None = None, curve_csv: dict[str, str] | None = None,
               refit_single_diode: bool = False) -> dict:
        kind = self._kind(kind)
        if type(refit_single_diode) is not bool:
            raise ValueError("refit_single_diode must be a strict boolean")
        if refit_single_diode and kind != "PAN":
            raise ValueError("refit_single_diode is supported for PAN components only")
        if kind == "REV":
            if curve_updates is not None or curve_csv is not None or use_file_curve:
                raise ValueError("REV seller address has no electrical power curves; curve editing is not applicable")
            if commercial_updates is not None or remarks is not None:
                raise ValueError("REV seller address has no commercial IV form; edit Comment/Person/Address/Town/Country via updates")
        if curve_csv is not None:
            if curve_updates is not None:
                raise ValueError("Supply curve_csv or curve_updates, not both")
            curve_updates = import_curve_csv(kind, curve_csv)
        if not isinstance(dry_run, bool) or not isinstance(use_file_curve, bool):
            raise ValueError("dry_run and use_file_curve must be booleans")
        if expected_sha256 is not None and (not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256)):
            raise ValueError("expected_sha256 must be a SHA-256 digest")
        if updates is not None and not isinstance(updates, dict):
            raise ValueError("updates must be a dictionary")
        if commercial_updates is not None or remarks is not None:
            if commercial_updates is not None and not isinstance(commercial_updates, dict):
                raise ValueError("commercial_updates must be a dictionary")
            if set(updates or {}) & set(commercial_updates or {}):
                raise ValueError("Supply each field in updates or commercial_updates, not both")
            if set(updates or {}) & COMMON_EDIT:
                raise ValueError("Use commercial_updates for identity fields when editing the commercial form")
        if curve_updates is not None:
            if not isinstance(curve_updates, dict) or not curve_updates:
                raise ValueError("curve_updates must be a nonempty dictionary")
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
            refit_meta: dict | None = None
            if refit_single_diode and kind == "PAN" and updates and (set(updates) & {"Voc", "ISC", "Isc", "Vmp", "Imp"}):
                from pvsyst_auto.components.single_diode import derive_single_diode_parameters

                pf = pan_validation_fields(edited)
                isc_entries = pf.get("ISC") or pf.get("Isc")
                missing_stc = [k for k, v in (("Voc", pf.get("Voc")), ("ISC", isc_entries), ("Vmp", pf.get("Vmp")), ("Imp", pf.get("Imp"))) if not v]
                if missing_stc:
                    raise ValueError(f"PAN single-diode refit requires STC fields: {', '.join(missing_stc)}")
                diode = derive_single_diode_parameters(
                    voc=float(pf["Voc"][0]), isc=float(isc_entries[0]), vmp=float(pf["Vmp"][0]), imp=float(pf["Imp"][0]),
                    n_cells_s=int(round(float(pf["NCelS"][0]))) if pf.get("NCelS") else 72,
                )
                lines_refit = edited.splitlines(keepends=True)
                if root_blocks := [b for b in objects(edited) if not b.path]:
                    root_blk = root_blocks[0]
                    diode_fmt = {"RShunt": f"{diode['RShunt']:.2f}", "Rp_0": f"{diode['Rp_0']:.1f}", "Rp_Exp": f"{diode['Rp_Exp']:.2f}",
                                 "RSerie": f"{diode['RSerie']:.4f}", "Gamma": f"{diode['Gamma']:.4f}", "muGamma": f"{diode['muGamma']:.4f}"}
                    patched_fields: dict[str, str] = {}
                    for d_key, d_val in diode_fmt.items():
                        if len(entries := root_blk.fields.get(d_key, [])) == 1:
                            lines_refit[entries[0][0]] = replace_value(lines_refit[entries[0][0]], d_val)
                            patched_fields[d_key] = d_val
                    edited = "".join(lines_refit)
                    refit_meta = {"refitted": True, "patched_fields": patched_fields, "single_diode": diode}
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
            diff = "".join(itertools.islice(difflib.unified_diff(text.splitlines(keepends=True), edited.splitlines(keepends=True),
                                                                 fromfile=name, tofile=name + " (candidate)"), 501))
            result = {
                "name": name, "before_sha256": before_sha, "sha256": hashlib.sha256(candidate).hexdigest(),
                "changed": candidate != data, "dry_run": dry_run, "conflict_checked": expected_sha256 is not None,
                "diff": diff[:32000], "diff_truncated": len(diff) > 32000 or len(diff.splitlines()) > 500,
                "validation": {k: validation[k] for k in ("structural_errors", "warnings", "known_value_errors")},
            }
            if curve_updates is not None:
                before_curves, after_curves = inspect_curves(kind, text), inspect_curves(kind, edited)
                result.update(
                    curve_control_before=before_curves["control"], curve_control_after=after_curves["control"],
                    curve_counts_before=[{k: it[k] for k in ("path", "point_count", "allocated_point_count")} for it in before_curves["items"]],
                    curve_counts_after=[{k: it[k] for k in ("path", "point_count", "allocated_point_count")} for it in after_curves["items"]],
                )
            if commercial_updates is not None or remarks is not None:
                result.update(commercial_before=commercial_inventory(kind, text), commercial_after=commercial_inventory(kind, edited))
            if refit_meta is not None:
                result["single_diode_refit"] = refit_meta
            if dry_run or candidate == data:
                return result

            def unchanged_backup():
                backup = self.backup(kind, name)
                if backup["sha256"] != before_sha:
                    raise ValueError("Component changed while preparing the edit")
                return backup

            backup = replace_checked(path, data, candidate, self._read, "Component changed while preparing the edit", prepare=unchanged_backup)
            return {**result, "backup_name": backup["backup_name"]}

    def compare(self, kind: str, first: str, second: str, second_library: str = "workspace") -> dict:
        kind = self._kind(kind)
        before = self._read(self._path(kind, first))
        after = self._read(self._path(kind, second, second_library))
        info_a, text_a = self._text(kind, before)
        info_b, text_b = self._text(kind, after)
        result = {"same_bytes": before == after, "first_format": info_a, "second_format": info_b,
                  "first_sha256": hashlib.sha256(before).hexdigest(), "second_sha256": hashlib.sha256(after).hexdigest()}
        if text_a is not None and text_b is not None:
            first_lines = list(itertools.islice(difflib.unified_diff(text_a.splitlines(), text_b.splitlines(),
                                                                     fromfile=first, tofile=second, lineterm=""), 151))
            result["diff_truncated"] = len(first_lines) > 150 or any(len(line) > 2000 for line in first_lines)
            result["diff"] = [line[:2000] for line in first_lines[:150]]
        return result

    def dependencies(self, project: str, variant: str) -> dict:
        if not isinstance(project, str) or project in (".", "..") or project.rstrip(" .") != project:
            raise ValueError("Expected project basename and variant ID")
        project = project_name(project if project.upper().endswith(".PRJ") else project + ".PRJ")
        base, variant = project[:-4], variant_id(variant)
        refs, variant_text = [], ""
        for identifier in (None, variant):
            path = project_path(self.workspace, project, identifier)
            if path.stat().st_size > MAX_PROJECT_BYTES:
                raise ValueError("Project or variant exceeds 32 MB")
            try:
                data = path.read_bytes()
                if len(data) > MAX_PROJECT_BYTES:
                    raise ValueError("Project or variant exceeds 32 MB")
                text = data.decode("utf-8-sig").replace("\r\n", "\n")
            except UnicodeDecodeError as exc:
                raise ValueError(f"Cannot safely inspect project references: {path.name}") from exc
            if path.name.upper().endswith(f".{variant}".upper()):
                variant_text = text
            for line_no, line in enumerate(text.splitlines(), 1):
                if match := REFERENCE.fullmatch(line):
                    kind, name = REF_KIND[match[1]], match[2]
                    try:
                        ws = self._path(kind, name).is_file()
                        bi = self._path(kind, name, "builtin").is_file() if self.builtin else False
                        status = "workspace" if ws else "builtin-file" if bi else "unknown-or-builtin-db"
                    except ValueError:
                        status = "invalid-reference"
                    refs.append({"source": path.name, "line": line_no, "field": match[1], "type": kind, "name": name, "status": status})
        generator = None
        if any(ref["type"] == "GEN" for ref in refs):
            section = re.search(r"(?ms)^[ \t]*PVObject_System=pvSystem[ \t]*$\n(?P<body>.*?)^[ \t]*End of PVObject pvSystem[ \t]*$", variant_text)
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
            for index, array in enumerate(re.finditer(r"(?ms)^[ \t]*PVObject_[A-Za-z0-9_]*=pvSubArray[ \t]*$\n(?P<body>.*?)^[ \t]*End of PVObject pvSubArray[ \t]*$", variant_text)):
                array_body = array["body"]
                encl = re.search(r"(?m)^[ \t]*VBkUpEncl_syst=([^\r\n]+)", array_body)
                rel = re.search(r"(?m)^[ \t]*VBkUpDecl_syst=([^\r\n]+)", array_body)
                subarrays.append({"index": index, "line": variant_text.count("\n", 0, array.start()) + 1,
                                  "enclosure": encl[1].strip() if encl else None, "release": rel[1].strip() if rel else None})
            generator = {
                "enabled_flag": bool(int(flags[1], 16) & 0x20) if flags else False,
                "effective_backup_kw": effective_power,
                "subarray_thresholds": subarrays,
                "enclosure_threshold_present": any(item["enclosure"] is not None for item in subarrays),
                "release_threshold_present": any(item["release"] is not None for item in subarrays),
            }
        return {"project": f"{base}.PRJ", "variant": variant, "references": refs, "generator_configuration": generator}

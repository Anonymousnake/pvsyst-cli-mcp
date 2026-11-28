"""Guarded project copies, lifecycle operations, validation, saved-result readers, and PVsyst variant storage."""
from __future__ import annotations

import difflib
import hashlib
import itertools
import json
import os
import re
import threading
import uuid
from pathlib import Path

from pvsyst_auto.components.editor import objects, replace_value, root_block
from pvsyst_auto.components.store import ComponentStore, FIELD, MAX_PROJECT_BYTES
from pvsyst_auto.project import circuit, paths, variant_updates
from pvsyst_auto.project.paths import battery_reference_name, project_path, project_root
from pvsyst_auto.validation import NATIVE_TEXT_OR_SEED, SEED_LABEL

FIELDS = {"PAN": "PVModule", "OND": "GInverter", "BTR": "BatteryFile", "GEN": "GensetFile"}
STARTS = {"pvSubArray": "PVObject_=pvSubArray", "pvSystem": "PVObject_System=pvSystem"}
MAX_ARCHIVE_MEMBERS = 128
EMPTY_SHADING_FLAGS = ("$00", "$00020000", "$00080000")
VAR_LIST_HEADER = re.compile(r"^\s*VarList,\s*list of=([0-9]+)\s+TVarSimul\s*$")
VAR_SIMUL_OPEN = re.compile(r"^\s*VarSimul_([0-9]+)=(.*?)\s*$")
VAR_SIMUL_CLOSE = re.compile(r"^\s*End of VarSimul=(.*?)\s*$")


def _declared_numbers(text: str) -> list[float] | None:
    values = []
    for item in text.split(","):
        if not (item := item.strip()):
            continue
        try:
            values.append(float(item))
        except ValueError:
            return None
    return values or None


def saved_results(text: str, lines: list[str]) -> dict:
    """Read recognized saved-result records and the separate output-file configuration."""
    from pvsyst_auto.native.headless import _PLOT_SAMPLES, _result_histograms, _simul_plots

    issues = []
    try:
        blocks = objects(text, include_private=True)
    except ValueError as exc:
        blocks, issues = [], [f"native result-cache objects unreadable: {exc}"]
    root = next((b for b in blocks if not b.path), None)
    root_flags = root.fields["Flags"][0][1] if root and len(root.fields.get("Flags", [])) == 1 else None
    if root_flags is None:
        issues.append("root Flags missing or duplicated")
    pv_simul_file = None
    if (simul := next((b for b in blocks if b.object_type == "pvSimulFile"), None)) is not None:
        pv_simul_file = {
            "file_name_written": simul.fields.get("FileNameWr", [[None, None]])[0][1],
            "var_idents_written": [x.strip() for x in simul.fields.get("VarIdentWr", [[None, ""]])[0][1].split(",") if x.strip()],
            "native_unit_codes": [x.strip() for x in simul.fields.get("Units", [[None, ""]])[0][1].split(",") if x.strip()],
            "type_val_written": simul.fields.get("TypeValW", [[None, None]])[0][1],
        }
    var_list_start = next((i for i, ln in enumerate(lines) if VAR_LIST_HEADER.match(ln)), None)
    declared_count, var_list_closed, entries = None, False, []
    if var_list_start is not None:
        declared_count = int(VAR_LIST_HEADER.match(lines[var_list_start])[1])
        var_list_end = next((i for i in range(var_list_start + 1, len(lines)) if lines[i].strip() == "End of the List VarList"), None)
        if var_list_end is None:
            issues.append("VarList missing 'End of the List VarList'")
        var_list_closed, end_scan, i = var_list_end is not None, (len(lines) if var_list_end is None else var_list_end), var_list_start + 1
        while i < end_scan:
            if not (m := VAR_SIMUL_OPEN.match(lines[i])):
                i += 1
                continue
            index, ident, close_line, fields, day_lines, j = int(m[1]), m[2], None, {}, [], i + 1
            while j < end_scan:
                stripped = lines[j].strip()
                if close_m := VAR_SIMUL_CLOSE.match(stripped):
                    close_line = j
                    break
                if VAR_SIMUL_OPEN.match(lines[j]):
                    break
                if f := FIELD.fullmatch(stripped):
                    (day_lines.append((f[1], f[2])) if f[1].startswith("Day_") else fields.setdefault(f[1], []).append(f[2]))
                j += 1
            if close_line is None:
                issues.append(f"VarSimul_{index}={ident} missing closing tag")
                i = j
                continue
            if close_m[1] != ident:
                issues.append(f"VarSimul_{index}={ident} closed as {close_m[1]}")
            one = lambda k: (fields.get(k, [])[0] if len(fields.get(k, [])) == 1 else None)
            raw_months, raw_factor = one("ValMonth"), one("StoredWeighted")
            values = _declared_numbers(raw_months) if raw_months is not None else None
            try:
                stored_factor = float(raw_factor) if raw_factor is not None else None
            except ValueError:
                stored_factor = None
            entries.append({
                "index": index, "ident": ident, "native_ident": one("VarIdent"), "declared_type": one("VarType"),
                "accumulator": one("TypeAccum"), "mode_ss": one("ModeSS"), "date_beg": one("DateBeg"), "unit": None,
                "unit_source": "not_declared_in_varlist", "values": values, "values_scope": "month" if values is not None else None,
                "day_rows": len(day_lines), "stored_weighted_factor": stored_factor,
                "value_declaration": ("\n".join(f"{k}={v}" for k, v in day_lines) if day_lines else (raw_months if values is None else None)),
            })
            i = close_line + 1
    if declared_count is not None and len(entries) != declared_count:
        issues.append(f"VarList declares {declared_count} TVarSimul but {len(entries)} parsed")
    missing_indices = sorted(set(range(1, declared_count + 1)) - {e["index"] for e in entries}) if declared_count is not None else []
    plots = []
    if root is not None:
        try:
            plots = list(_simul_plots(lines, root, blocks))
        except ValueError as exc:
            issues.append(str(exc))
    histograms = len(_result_histograms([root, *plots], blocks)) if root else 0
    return {
        "present": var_list_start is not None or histograms > 0 or any(_PLOT_SAMPLES & p.fields.keys() for p in plots),
        "root_flags": root_flags, "pv_simul_file": pv_simul_file,
        "var_list": {"present": var_list_start is not None, "closed": var_list_closed, "declared_count": declared_count,
                     "parsed_count": len(entries), "missing_indices": missing_indices, "entries": entries},
        "histogram_count": histograms, "issues": issues,
    }


def economic_block(lines: list[str]) -> dict:
    """Read an optional root ``EconomicEval=TEcoEvaluation`` block."""
    starts = [i for i, line in enumerate(lines) if line.strip() == "EconomicEval=TEcoEvaluation"]
    if not starts:
        return {"present": False}
    if len(starts) != 1:
        raise ValueError("Multiple EconomicEval sections")
    terminator = "End of TEcoEvaluation"
    if (end := next((i for i in range(starts[0] + 1, len(lines)) if lines[i].strip() == terminator), None)) is None:
        raise ValueError("Unclosed EconomicEval section")
    block, opens, closes, lists, scalars = lines[starts[0] + 1:end], 0, 0, {}, {}
    list_open, list_close = re.compile(r"^\s*([A-Za-z0-9_]+),\s*list of=([0-9]+)\s+([A-Za-z0-9_]+)\s*$"), re.compile(r"^\s*End of List\b")
    for line in block:
        stripped = line.strip()
        if list_close.match(stripped):
            closes += 1
        elif lst := list_open.match(stripped):
            opens += 1
            if opens == closes + 1:
                lists[lst.group(1)] = {"type": lst.group(3), "declared_count": int(lst.group(2))}
        elif opens == closes and (field := FIELD.fullmatch(stripped)):
            scalars.setdefault(field.group(1), []).append(field.group(2))
    if opens != closes:
        raise ValueError(f"EconomicEval list structure is unbalanced: {opens} 'list of=' but {closes} 'End of List'")
    return {"present": True, "owner": "TEcoEvaluation", "terminator": terminator, "native_type": "TEcoEvaluation",
            "raw": "".join(block), "scalars": scalars, "lists": lists, "native_consumption_verified": False}


class VariantStore:
    def __init__(self, workspace: str | os.PathLike, components: ComponentStore, lock: threading.RLock | None = None):
        self.workspace, self.components, self.lock = Path(workspace).resolve(strict=True), components, lock or threading.RLock()

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
        stem = project[:-4].casefold()
        if path.stem.casefold() == stem:
            return True
        if re.fullmatch(r"\.(?:PRJ|VC[A-Za-z0-9]+)", path.suffix, re.I):
            return False
        return bool(path.name.casefold().startswith(stem + ".") and re.fullmatch(r"(?:PRJ|VC[A-Za-z0-9]+)\..+", path.name[len(project[:-4]) + 1:], re.I))

    @staticmethod
    def _read(path: Path) -> bytes:
        if not path.is_file():
            raise FileNotFoundError(path.name)
        if path.stat().st_size > MAX_PROJECT_BYTES:
            raise ValueError("Project or variant exceeds 32 MB")
        VariantStore._check_size(data := path.read_bytes())
        return data

    def _check_project(self, project: str) -> None:
        self._project_text(self._read(self._path(project)))

    @staticmethod
    def _project_text(data: bytes) -> None:
        VariantStore._check_size(data)
        try:
            lines = data.decode("utf-8-sig").splitlines()
        except UnicodeDecodeError as exc:
            raise ValueError("Project must be UTF-8 text") from exc
        if not lines or lines[0].strip() != "PVObject_=pvProject" or lines[-1].strip() != "End of PVObject pvProject" or "\x00" in "".join(lines):
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
        arrays, system, active = {}, None, None
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
                    indices = VariantStore._field_indices(lines, bounds, "SubArrayId")
                    if len(indices) != 1:
                        raise ValueError("Subarray requires one SubArrayId")
                    identifier = FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]
                    if not identifier.isdigit() or int(identifier) < 1 or int(identifier) in arrays:
                        raise ValueError("SubArrayId must be a unique positive integer")
                    arrays[int(identifier)] = bounds
                active = None
        if active is not None or not arrays:
            raise ValueError("Unclosed section or missing subarray")
        return arrays, system

    @staticmethod
    def _main_array(lines: list[str]) -> tuple[int, int] | None:
        starts = [i for i, line in enumerate(lines) if line.strip() == "PVObject_PVMainArray=pvMainArray"]
        ends = [i for i, line in enumerate(lines) if line.strip() == "End of PVObject pvMainArray"]
        if not starts and not ends:
            return None
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise ValueError("Variant must have one complete main array")
        return starts[0], ends[0]

    @staticmethod
    def _field_indices(lines: list[str], bounds: tuple[int, int], key: str) -> list[int]:
        first = lines[bounds[0]].strip()
        if first in (*STARTS.values(), "PVObject_PVMainArray=pvMainArray"):
            root = next(b for b in objects("".join(lines[bounds[0]:bounds[1] + 1])) if not b.path)
            return [bounds[0] + idx for idx, _ in root.fields.get(key, [])]
        if first == "PVObject_=pvOrient":
            depth, found = 0, []
            for index in range(bounds[0] + 1, bounds[1]):
                stripped = lines[index].strip()
                if stripped.startswith("PVObject_"):
                    depth += 1
                elif stripped.startswith("End of PVObject "):
                    depth = max(0, depth - 1)
                elif depth == 0 and (m := FIELD.fullmatch(lines[index].rstrip("\r\n"))) and m[1] == key:
                    found.append(index)
            return found
        return [i for i in range(bounds[0] + 1, bounds[1]) if (m := FIELD.fullmatch(lines[i].rstrip("\r\n"))) and m[1] == key]

    @staticmethod
    def _display_name_fields(lines: list[str], complete: bool = False) -> list[int]:
        child = next((i for i in range(1, len(lines)) if lines[i].strip().startswith("PVObject_") or re.match(r"\s*PrintSettings\s*=", lines[i])), len(lines) - 1)
        indices = VariantStore._field_indices(lines, (0, child), "Comment")
        if complete:
            root = root_block(lines)
            if len(indices) != 1 or indices != [i for i, _ in root.fields.get("Comment", [])]:
                raise ValueError("Display name requires one owning header Comment")
        return indices

    @staticmethod
    def _validate_display_name(value: str, field: str) -> None:
        if not isinstance(value, str) or not value or value != value.strip() or not value.isprintable() or len(value.encode("utf-8")) > 80:
            raise ValueError(f"Invalid {field}: require 1..80 UTF-8 bytes of printable text without surrounding whitespace")

    @staticmethod
    def _display_name_prefix(data: bytes, name: str, add_utf8_bom: bool, field: str, content: str | None = None) -> bytes:
        bom = data.startswith(b"\xef\xbb\xbf")
        if not name.isascii() and not bom and not add_utf8_bom:
            raise ValueError(f"Non-ASCII {field} values require UTF-8 BOM; use add_utf8_bom=true explicitly")
        if content is not None and not content.isascii():
            return b"\xef\xbb\xbf"
        return b"\xef\xbb\xbf" if bom or add_utf8_bom else b""

    @staticmethod
    def _check_hash(data: bytes, expected: str | None) -> None:
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected) or hashlib.sha256(data).hexdigest() != expected.lower()):
            raise ValueError("Variant changed since inspection; provide current sha256")

    def inspect(self, project: str, variant: str) -> dict:
        with self.lock:
            self._check_project(project)
            data = self._read(self._path(project, variant))
            _, lines = self._text(data)
            arrays, system = self._sections(lines)
            values = lambda b, types: {k: [FIELD.fullmatch(lines[i].rstrip("\r\n"))[2] for i in self._field_indices(lines, b, FIELDS[k])] for k in types}
            return {
                "project": project, "variant": variant.upper(), "sha256": hashlib.sha256(data).hexdigest(),
                "subarrays": [{"id": i, "references": values(b, ("PAN", "OND"))} for i, b in sorted(arrays.items())],
                "system_references": values(system, ("BTR", "GEN")) if system else {},
            }

    def _replace(self, data: bytes, updates: dict[str, str], subarray_id: int) -> bytes:
        if not isinstance(updates, dict) or not updates or any(k not in FIELDS for k in updates):
            raise ValueError("Choose one or more of PAN, OND, BTR, GEN")
        if not isinstance(subarray_id, int) or isinstance(subarray_id, bool) or subarray_id < 1:
            raise ValueError("subarray_id must be a positive integer")
        text, lines = self._text(data)
        arrays, system = self._sections(lines)
        for kind, filename in updates.items():
            if kind == "BTR":
                battery_reference_name(filename)
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
            idx = indices[0]
            line = lines[idx]
            if kind == "BTR":
                key, eq, old = line.partition("=")
                line = key + eq + old.lstrip(" \t")
            lines[idx] = replace_value(line, filename)
        if "".join(lines) == text:
            return data
        from pvsyst_auto.native.headless import strip_result_cache
        lines, _ = strip_result_cache(lines)
        candidate = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")
        self._text(candidate)
        return candidate

    @staticmethod
    def _orientation(lines: list[str], number: int) -> tuple[int, int]:
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError("orientation_id must be a positive integer")
        groups, start, close_tag = [], None, None
        for index, line in enumerate(lines):
            value = line.strip()
            if value in ("PVObject_=pvOrient", "PVObject_=pvDomeOrient"):
                if start is not None:
                    raise ValueError("Nested orientations")
                start = index
                close_tag = "End of TOrientGroup" if value == "PVObject_=pvOrient" else "End of TDomeOrientGroup"
            elif value in ("End of TOrientGroup", "End of TDomeOrientGroup") and start is not None:
                if value != close_tag:
                    raise ValueError("Mismatched orientation closing tag")
                groups.append((start, index))
                start = close_tag = None
        if start is not None:
            raise ValueError("Unclosed orientation")
        matches = [b for b in groups if any((m := FIELD.fullmatch(lines[i].rstrip("\r\n"))) and m[1] == "NoOrient" and m[2] == str(number) for i in range(b[0] + 1, b[1]))]
        if len(matches) != 1:
            raise ValueError("Orientation ID must occur exactly once")
        return matches[0]

    def _orientation_list(self, lines):
        headers = [(i, re.fullmatch(r"Orientations, list of=([0-9]+) TOrientGroup", line.strip())) for i, line in enumerate(lines) if line.strip().startswith("Orientations, list of=")]
        ends = [i for i, line in enumerate(lines) if line.strip() == "End of Orientations"]
        if len(headers) != 1 or not headers[0][1] or len(ends) != 1 or headers[0][0] >= ends[0]:
            raise ValueError("Expected one counted TOrientGroup orientation list")
        header, match = headers[0]
        open_tags = {"PVObject_=pvOrient": "End of TOrientGroup", "PVObject_=pvDomeOrient": "End of TDomeOrientGroup"}
        starts = [i for i in range(header + 1, ends[0]) if lines[i].strip() in open_tags]
        closings = [i for i in range(header + 1, ends[0]) if lines[i].strip() in ("End of TOrientGroup", "End of TDomeOrientGroup")]
        if len(starts) != sum(line.strip() in open_tags for line in lines):
            raise ValueError("Only listed TOrientGroup orientation records are supported")
        if not starts or len(starts) != len(closings) or int(match[1]) != len(starts):
            raise ValueError("Orientation list count differs from its records")
        groups, dome_meta = [], {}
        for pos, (first, last) in enumerate(zip(starts, closings)):
            if first >= last or (pos and closings[pos - 1] >= first):
                raise ValueError("Overlapping orientation records")
            if lines[last].strip() != open_tags[lines[first].strip()]:
                raise ValueError("Mismatched orientation record closing tag")
            if len(ids := self._field_indices(lines, (first, last), "NoOrient")) != 1:
                raise ValueError("Orientation requires one ID")
            if not (raw := FIELD.fullmatch(lines[ids[0]].rstrip("\r\n"))[2]).isdigit() or not 1 <= int(raw) <= 2147483647:
                raise ValueError("Orientation ID must be a positive 32-bit integer")
            gid = int(raw)
            if self._orientation(lines, gid) != (first, last):
                raise ValueError("Orientation is outside its declared list")
            if lines[first].strip() == "PVObject_=pvDomeOrient":
                ft_idx = self._field_indices(lines, (first, last), "FieldType")
                pr_idx = self._field_indices(lines, (first, last), "NoPairOrient")
                mn_idx = self._field_indices(lines, (first, last), "IsMainOrient")
                if len(ft_idx) != 1 or len(pr_idx) != 1 or len(mn_idx) != 1:
                    raise ValueError("pvDomeOrient requires FieldType, NoPairOrient, and IsMainOrient")
                ft_val = FIELD.fullmatch(lines[ft_idx[0]].rstrip("\r\n"))[2]
                pr_val = FIELD.fullmatch(lines[pr_idx[0]].rstrip("\r\n"))[2]
                mn_val = FIELD.fullmatch(lines[mn_idx[0]].rstrip("\r\n"))[2]
                expected_main = "True" if ft_val == "Dome" else "False" if ft_val == "DomeBackfaces" else None
                if expected_main is None or mn_val != expected_main or not pr_val.isdigit():
                    raise ValueError("Invalid pvDomeOrient FieldType, IsMainOrient, or NoPairOrient")
                dome_meta[gid] = (int(pr_val), ft_val)
            groups.append((first, last, gid))
        for gid, (pair_id, ft_val) in dome_meta.items():
            if pair_id == gid or pair_id not in dome_meta or dome_meta[pair_id][0] != gid or {ft_val, dome_meta[pair_id][1]} != {"Dome", "DomeBackfaces"}:
                raise ValueError(f"Dome orientation {gid} lacks a valid paired partner")
        arrays, _ = self._sections(lines)
        identifiers, referenced = {gid for _, _, gid in groups}, set()
        for first, last in arrays.values():
            block = next(b for b in objects("".join(lines[first:last + 1])) if not b.path)
            raw = block.one("NoOrientation")[1]
            if not raw.isdigit() or int(raw) not in identifiers:
                raise ValueError("Subarray references an orientation outside its declared list")
            referenced.add(int(raw))
            if "NoOrientation2" in block.fields:
                raw2 = block.one("NoOrientation2")[1]
                if not raw2.isdigit() or int(raw2) not in identifiers or int(raw2) == int(raw):
                    raise ValueError("Subarray NoOrientation2 must reference another declared orientation")
                referenced.add(int(raw2))
        if unused := identifiers - referenced:
            raise ValueError(f"Orientation has no assigned subarray: {sorted(unused)}")
        return header, groups

    _VERIFIED_MAIN_FLAG_MASK = 0x21EC0000

    @classmethod
    def _is_verified_main_flags(cls, raw: str) -> bool:
        return bool(re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", raw)) and (int(raw[1:], 16) & ~cls._VERIFIED_MAIN_FLAG_MASK) in (0x00100400, 0x00000400)

    @staticmethod
    def _is_verified_main_flags2(raw: str) -> bool:
        return bool(re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", raw)) and (int(raw[1:], 16) & ~0x00100000) == 0x082A

    @staticmethod
    def _ordinary_fixed_plane(lines, bounds, allow_missing_flags=False, allow_sheds=False):
        if lines[bounds[0]].strip() != "PVObject_=pvOrient":
            raise ValueError("Operation requires ordinary PVObject_=pvOrient")
        _allowed_nested = ("PVObject_Bifacial=", "PVObject_BiFacialShedModel=", "PVObject_BiFacialTrackerModel=")
        if any((line.strip().startswith("PVObject_") and not line.strip().startswith(_allowed_nested)) or line.strip().endswith("=Start") for line in lines[bounds[0] + 1:bounds[1]]):
            raise ValueError("Operation requires a flat fixed-plane record")
        allowed_types = ("FixedPlane", "Sheds") if allow_sheds else ("FixedPlane",)
        if len(ft := VariantStore._field_indices(lines, bounds, "FieldType")) != 1 or FIELD.fullmatch(lines[ft[0]].rstrip("\r\n"))[2] not in allowed_types:
            raise ValueError("Operation requires ordinary FieldType=FixedPlane" + (" or Sheds" if allow_sheds else ""))
        if ((fl := VariantStore._field_indices(lines, bounds, "Flags")) or not allow_missing_flags) and (len(fl) != 1 or FIELD.fullmatch(lines[fl[0]].rstrip("\r\n"))[2] != "$00"):
            raise ValueError("Operation requires ordinary Flags=$00")
        for key in ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver"):
            if (idx := VariantStore._field_indices(lines, bounds, key)) and (len(idx) != 1 or FIELD.fullmatch(lines[idx[0]].rstrip("\r\n"))[2] != "False"):
                raise ValueError("Averaged orientations require separate geometry handling")

    @staticmethod
    def _require_no_geometry(lines, require_shading_section=False):
        if any("PVObject_ShdTable" in ln or "ListeObjets, list of=" in ln or "PVObject_ModuleLayout" in ln for ln in lines):
            raise ValueError("Operation has an active shading scene, dependent shading table or module layout")
        shade = [i for i, ln in enumerate(lines) if ln.strip() == "PVObject_Ombrage=pvShading"]
        if not shade and not require_shading_section:
            return
        if len(shade) != 1:
            raise ValueError("Expected one empty shading section")
        end = next((i for i in range(shade[0] + 1, len(lines)) if lines[i].strip() == "End of PVObject pvShading"), None)
        if end is None or any(ln.strip().startswith("PVObject_") for ln in lines[shade[0] + 1:end]):
            raise ValueError("Operation has an active shading scene")
        if len(flags := VariantStore._field_indices(lines, (shade[0], end), "Flags")) != 1 or FIELD.fullmatch(lines[flags[0]].rstrip("\r\n"))[2] not in EMPTY_SHADING_FLAGS:
            raise ValueError("Shading is active or unrecognized: active shading scene")

    @staticmethod
    def _uv_edit_gate(lines, system, arrays, main_array) -> None:
        kinds = VariantStore._field_indices(lines, system, "SystemType") if system is not None else []
        versions = [VariantStore._field_indices(lines, bounds, "Version") for bounds in arrays.values()]
        if (main_array is None or len(kinds) != 1 or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid"
                or not versions or any(len(idx) != 1 or FIELD.fullmatch(lines[idx[0]].rstrip("\r\n"))[2] not in NATIVE_TEXT_OR_SEED for idx in versions)):
            raise ValueError(f"UVFactor editing requires a verified {SEED_LABEL} grid main array")
        flags, flags2 = VariantStore._field_indices(lines, main_array, "Flags"), VariantStore._field_indices(lines, main_array, "Flags2")
        if (len(flags) != 1 or not VariantStore._is_verified_main_flags(FIELD.fullmatch(lines[flags[0]].rstrip("\r\n"))[2])
                or len(flags2) != 1 or not VariantStore._is_verified_main_flags2(FIELD.fullmatch(lines[flags2[0]].rstrip("\r\n"))[2])):
            raise ValueError("UVFactor editing requires verified main-array Flags/Flags2")
        if len(VariantStore._field_indices(lines, main_array, "UVFactor")) != 1:
            raise ValueError("UVFactor must occur exactly once directly on the main array")

    def inspect_parameters(self, project: str, variant: str, include_saved_results: bool = False,
                           include_economics: bool = False, include_topology: bool = False) -> dict:
        return variant_updates.inspect_parameters(self, project, variant, include_saved_results=include_saved_results, include_economics=include_economics, include_topology=include_topology)

    def update_parameters(self, project: str, variant: str, expected_sha256: str | None = None, subarray_id: int = 1, orientation_id: int = 1,
                          subarray_updates: dict[str, float] | None = None, orientation_updates: dict[str, float] | None = None,
                          generator_updates: dict | None = None, dry_run: bool = False, power_sharing_enabled: bool | None = None,
                          independent_mppt_enabled: bool | None = None, main_array_updates: dict[str, float] | None = None,
                          variant_name: str | None = None, add_utf8_bom: bool = False,
                          soiling_updates: dict | None = None, grid_limit_updates: dict | None = None,
                          auxiliary_updates: dict | None = None, seasonal_updates: dict | None = None,
                          horizon_updates: dict | None = None, inverter_temperature_updates: dict | None = None,
                          power_factor_updates: dict | None = None, p50_p90_updates: dict | None = None,
                          power_controls_updates: dict | None = None, string_mismatch_updates: dict | None = None,
                          ageing_updates: dict | None = None, iam_updates: dict | None = None,
                          ageing_enabled: bool | None = None, system_unavailability_updates: dict | None = None,
                          bifacial_updates: dict | None = None, spectral_updates: dict | None = None,
                          native_economics_updates: dict | None = None) -> dict:
        return variant_updates.update_parameters(
            self, project, variant, expected_sha256, subarray_id, orientation_id, subarray_updates=subarray_updates,
            orientation_updates=orientation_updates, generator_updates=generator_updates, dry_run=dry_run,
            power_sharing_enabled=power_sharing_enabled, independent_mppt_enabled=independent_mppt_enabled,
            main_array_updates=main_array_updates, variant_name=variant_name, add_utf8_bom=add_utf8_bom,
            soiling_updates=soiling_updates, grid_limit_updates=grid_limit_updates, auxiliary_updates=auxiliary_updates,
            seasonal_updates=seasonal_updates, horizon_updates=horizon_updates,
            inverter_temperature_updates=inverter_temperature_updates, power_factor_updates=power_factor_updates,
            p50_p90_updates=p50_p90_updates, power_controls_updates=power_controls_updates,
            string_mismatch_updates=string_mismatch_updates, ageing_updates=ageing_updates, iam_updates=iam_updates,
            ageing_enabled=ageing_enabled, system_unavailability_updates=system_unavailability_updates,
            bifacial_updates=bifacial_updates, spectral_updates=spectral_updates,
            native_economics_updates=native_economics_updates,
        )

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

    def _replace_checked(self, path: Path, data: bytes, candidate: bytes, *, prepare=None, verify=None):
        return paths.replace_checked(path, data, candidate, self._read, "Variant changed while preparing the edit", prepare=prepare, verify=verify)

    @staticmethod
    def _rollback(error: BaseException, steps, recovery: str) -> None:
        failed = []
        for name, undo, *args in steps:
            try:
                undo(*args)
            except Exception as exc:
                failed.append(f"{name} ({type(exc).__name__})")
        if failed and isinstance(error, Exception):
            raise OSError(f"{type(error).__name__}: {error}; rollback incomplete for {', '.join(failed)}; {recovery}") from error

    def _create_project_files(self, project, snapshots):
        if len(snapshots) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("Project exceeds 128 PRJ/VC members")
        if len({path for path, _ in snapshots}) != len(snapshots):
            raise ValueError("Duplicate variant identifiers after case normalization")
        if any(self._belongs_to_project(path, project) for path in self._root().iterdir()):
            raise FileExistsError("Destination project or variant already exists")
        created = []
        try:
            for target, data in snapshots:
                with target.open("xb") as stream:
                    created.append(target)
                    stream.write(data)
        except BaseException as exc:
            self._rollback(exc, [(t.name, t.unlink, True) for t in reversed(created)], "delete those partial files before retrying")
            raise
        return {"project": project, "variants": [p.suffix[1:] for p, _ in snapshots[1:]], "sha256": hashlib.sha256(snapshots[0][1]).hexdigest(), "files": {p.name: hashlib.sha256(d).hexdigest() for p, d in snapshots}}

    def create_project(self, project: str, project_content: str, variants: dict[str, str]) -> dict:
        if not isinstance(project_content, str) or not isinstance(variants, dict) or not variants or any(not isinstance(c, str) for c in variants.values()):
            raise ValueError("Supply PRJ text and a nonempty VC identifier-to-text map")
        if len(variants) >= MAX_ARCHIVE_MEMBERS:
            raise ValueError("Project exceeds 128 PRJ/VC members")
        with self.lock:
            self._project_text(data := project_content.encode("utf-8"))
            snapshots = [(self._path(project), data)]
            for identifier, content in variants.items():
                self._sections(self._text(vdata := content.encode("utf-8"))[1])
                snapshots.append((self._path(project, identifier), vdata))
            return self._create_project_files(project, snapshots)

    def clone_project(self, source_project: str, new_project: str) -> dict:
        with self.lock:
            self._check_project(source_project)
            target_prj, sources = self._path(new_project), [self._path(source_project)]
            for path in list(self._root().iterdir()):
                if path.name.casefold() != source_project.casefold() and self._belongs_to_project(path, source_project):
                    if not re.fullmatch(r"VC[A-Za-z0-9]+", ext := path.suffix[1:], re.I):
                        raise ValueError(f"Unrecognized project sidecar: {path.name}")
                    sources.append(self._path(source_project, ext))
            if len(sources) == 1:
                raise ValueError("Source project has no variants")
            snapshots = []
            for source in sources:
                data = self._read(source)
                self._project_text(data) if source == sources[0] else self._sections(self._text(data)[1])
                snapshots.append((target_prj if source == sources[0] else self._path(new_project, source.suffix[1:]), data))
            return {**self._create_project_files(new_project, snapshots), "source_project": source_project}

    def clone(self, project: str, source_variant: str, new_variant: str, updates: dict[str, str] | None = None,
              subarray_id: int = 1, source_project: str | None = None) -> dict:
        with self.lock:
            self._check_project(project)
            source_project = project if source_project is None else source_project
            self._check_project(source_project)
            target, data = self._path(project, new_variant), self._read(self._path(source_project, source_variant))
            self._sections(self._text(data)[1])
            candidate = self._replace(data, updates, subarray_id) if updates else data
            if any(p.name.casefold() == target.name.casefold() for p in target.parent.iterdir()):
                raise FileExistsError(target.name)
            paths.publish_new(target, candidate)
            return {"project": project, "variant": new_variant.upper(), "source_project": source_project, "source_variant": source_variant.upper(), "sha256": hashlib.sha256(candidate).hexdigest()}

    def update(self, project: str, variant: str, updates: dict[str, str], expected_sha256: str | None = None, subarray_id: int = 1) -> dict:
        with self.lock:
            self._check_project(project)
            self._check_hash(data := self._read(path := self._path(project, variant)), expected_sha256)
            candidate = self._replace(data, updates, subarray_id)
            backup = self._backup(path, data)
            self._replace_checked(path, data, candidate)
            return {"project": project, "variant": variant.upper(), "backup_name": backup, "sha256": hashlib.sha256(candidate).hexdigest()}

    _subarray_branches = staticmethod(circuit.subarray_branches)
    _require_plane_branch_mode = staticmethod(circuit.require_plane_branch_mode)

    def validate_structure(self, project: str, variant: str) -> dict:
        """Check cross-object references inside a variant."""
        return circuit.validate_structure(self, project, variant)

    def _clone_subarray_data(self, data, source_subarray_id, *, clone_orientation=False, share_inverter=False, reserved_ids=()):
        return circuit.clone_subarray_data(self, data, source_subarray_id, clone_orientation=clone_orientation, share_inverter=share_inverter, reserved_ids=reserved_ids)

    def clone_subarray(self, project: str, variant: str, source_subarray_id: int, expected_sha256: str | None = None, clone_orientation: bool = False, share_inverter: bool = False) -> dict:
        return circuit.clone_subarray(self, project, variant, source_subarray_id, expected_sha256, clone_orientation=clone_orientation, share_inverter=share_inverter)

    def remove_subarray(self, project: str, variant: str, subarray_id: int, expected_sha256: str | None = None, remove_orientation: bool = False, shared_member: bool = False) -> dict:
        return circuit.remove_subarray(self, project, variant, subarray_id, expected_sha256, remove_orientation=remove_orientation, shared_member=shared_member)

    @staticmethod
    def _section_bounds(lines: list[str], header: str, closing: str, *, multiple_err: str | None = None) -> tuple[int, int]:
        starts = [i for i, ln in enumerate(lines) if ln.strip() == header]
        if len(starts) != 1:
            raise ValueError(multiple_err if len(starts) > 1 and multiple_err else f"Expected one {header} section")
        if (end := next((i for i in range(starts[0] + 1, len(lines)) if lines[i].strip() == closing), None)) is None:
            raise ValueError(f"Unclosed {header} section")
        return starts[0], end

    @staticmethod
    def _embedded_reference(lines: list[str], header: str, closing: str, field_name: str = "NomF") -> str | None:
        if not any(line.strip() == header for line in lines):
            return None
        start, end = VariantStore._section_bounds(lines, header, closing, multiple_err=f"Multiple {header} sections")
        child = next((i for i in range(start + 1, end) if lines[i].strip().startswith("PVObject_")), end)
        indices = VariantStore._field_indices(lines, (start, child), field_name)
        if len(indices) != 1:
            raise ValueError(f"{header} requires one {field_name}")
        return FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]

    def _workspace_asset(self, directory: str, name: str | None) -> dict:
        if name is None:
            return {"name": None, "workspace_file": False}
        if not name or Path(name).name != name or "\\" in name or "/" in name or not name.lower().endswith({"Sites": ".sit", "Meteo": ".met"}[directory]):
            return {"name": name, "workspace_file": False}
        base, path = self.workspace / directory, self.workspace / directory / name
        found = base.is_dir() and not base.is_symlink() and base.resolve().parent == self.workspace and not path.is_symlink() and path.is_file() and path.resolve().parent == base.resolve()
        return {"name": name, "workspace_file": found}

    def inspect_project(self, project: str) -> dict:
        with self.lock:
            self._check_project(project)
            members = [self._path(project)]
            for path in self._root().iterdir():
                if path.name.casefold() != project.casefold() and self._belongs_to_project(path, project):
                    if not re.fullmatch(r"VC[A-Za-z0-9]+", path.suffix[1:], re.I):
                        raise ValueError(f"Unrecognized project sidecar: {path.name}")
                    members.append(self._path(project, path.suffix[1:]))
            hashes, sources, warnings = {}, {}, []
            for path in members:
                data = self._read(path)
                if path.suffix.lower() == ".prj":
                    lines = data.decode("utf-8-sig").splitlines()
                    names = self._display_name_fields(lines)
                    project_name, project_bom = (FIELD.fullmatch(lines[names[0]])[2] if len(names) == 1 else None), data.startswith(b"\xef\xbb\xbf")
                    meteo_fields = [f[2] for ln in lines if (f := FIELD.fullmatch(ln)) and f[1] == "MeteoFileName"]
                    if len(meteo_fields) > 1:
                        raise ValueError("Project has multiple MeteoFileName fields")
                    site, meteo = self._embedded_reference(lines, "PVObject_SitePrj=pvSite", "End of PVObject pvSite"), (meteo_fields[0] if meteo_fields else None)
                else:
                    self._sections(lines := self._text(data)[1])
                    site, meteo = self._embedded_reference(lines, "PVObject_SiteSimul=pvSite", "End of PVObject pvSite"), self._embedded_reference(lines, "PVObject_MeteoSimul=pvMeteo", "End of PVObject pvMeteo")
                hashes[path.name] = hashlib.sha256(data).hexdigest()
                sources[path.name] = {"site": self._workspace_asset("Sites", site), "meteo": self._workspace_asset("Meteo", meteo)}
                warnings.extend(f"{path.name}: {kind} {asset['name']} is not a workspace file" for kind, asset in sources[path.name].items() if asset["name"] and not asset["workspace_file"])
            project_refs = sources[members[0].name]
            for path in members[1:]:
                for kind in ("site", "meteo"):
                    expected, actual = project_refs[kind]["name"], sources[path.name][kind]["name"]
                    if expected and actual and expected.casefold() != actual.casefold():
                        warnings.append(f"{path.name}: {kind} differs from {members[0].name}")
            return {"project": project, "project_name": project_name, "utf8_bom": project_bom, "files": hashes,
                    "variants": [p.suffix[1:] for p in members[1:]], "sources": sources, "warnings": warnings}

    @staticmethod
    def _replace_site_block(lines: list[str], role: str, site_lines: list[str]) -> None:
        start, end = VariantStore._section_bounds(lines, header := f"PVObject_{role}=pvSite", "End of PVObject pvSite")
        if any(ln.strip().startswith("PVObject_") for ln in lines[start + 1:end]):
            raise ValueError(f"Unsupported nested {header} section")
        prefix, ending = lines[start][:len(lines[start]) - len(lines[start].lstrip())], ("\r\n" if lines[start].endswith("\r\n") else "\n")
        lines[start:end + 1] = [prefix + header + ending] + [prefix + ln.rstrip("\r\n") + ending for ln in site_lines[1:-1]] + [prefix + "End of PVObject pvSite" + ending]

    @staticmethod
    def _replace_unique_field(lines: list[str], key: str, value: str, bounds: tuple[int, int]) -> None:
        indices = [i for i in range(bounds[0], bounds[1]) if (f := FIELD.fullmatch(lines[i].rstrip("\r\n"))) and f[1] == key]
        if len(indices) != 1:
            raise ValueError(f"Expected one {key} in target section")
        orig = lines[indices[0]]
        lines[indices[0]] = orig[:orig.index("=") + 1] + value + ("\r\n" if orig.endswith("\r\n") else "\n" if orig.endswith("\n") else "")

    def _project_files_snapshot(self, project: str, expected_files: dict[str, str] | None,
                                mismatch_err: str = "Project files changed since inspection") -> tuple[dict, dict[Path, bytes]]:
        inventory = self.inspect_project(project)
        if expected_files is not None and inventory["files"] != expected_files:
            raise ValueError(mismatch_err)
        originals = {}
        for name, digest in (inventory["files"] if expected_files is None else expected_files).items():
            path = self._path(project) if name == project else self._path(project, name[len(project[:-4]) + 1:])
            self._check_hash(data := self._read(path), digest)
            originals[path] = data
        return inventory, originals

    def _commit_project_files(self, project: str, originals: dict[Path, bytes], candidates: dict[Path, bytes]) -> dict:
        if not originals or originals.keys() != candidates.keys():
            raise ValueError("Project transaction must contain the same complete file set")
        for data in (*originals.values(), *candidates.values()):
            self._check_size(data)
        tid, backups, staged, written = uuid.uuid4().hex, {}, {}, []
        manifest_name = f"transaction.{tid}.json"
        try:
            for path, data in originals.items():
                backups[path.name] = self._backup(path, data, tid)
            with (self._backup_folder() / manifest_name).open("x", encoding="utf-8") as stream:
                json.dump({"version": 1, "transaction_id": tid, "project": project, "backups": backups,
                           "original_files": {p.name: hashlib.sha256(d).hexdigest() for p, d in originals.items()}}, stream)
            for path, data in candidates.items():
                if data != originals[path]:
                    staged[path] = paths.stage(path, data)
            if self.inspect_project(project)["files"] != {p.name: hashlib.sha256(d).hexdigest() for p, d in originals.items()}:
                raise ValueError("Project files changed while preparing the transaction")
            for path, temporary in staged.items():
                os.replace(temporary, path)
                written.append(path)
        except BaseException as exc:
            self._rollback(exc, [(p.name, self._replace_checked, p, candidates[p], originals[p]) for p in reversed(written)], f"restore the originals from backup transaction {tid}")
            raise
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)
        return {"backups": backups, "transaction_id": tid, "transaction_manifest": manifest_name,
                "files": {p.name: hashlib.sha256(d).hexdigest() for p, d in candidates.items()}}

    def update_project_parameters(self, project: str, expected_files: dict[str, str] | None = None, project_name: str | None = None,
                                  dry_run: bool = False, add_utf8_bom: bool = False) -> dict:
        if project_name is None:
            raise ValueError("project_name is required")
        self._validate_display_name(project_name, "project_name")
        if not isinstance(dry_run, bool) or not isinstance(add_utf8_bom, bool):
            raise ValueError("dry_run and add_utf8_bom must be booleans")
        with self.lock:
            inventory, originals = self._project_files_snapshot(project, expected_files)
            path = self._path(project)
            data = originals[path]
            prefix = self._display_name_prefix(data, project_name, add_utf8_bom, "project_name")
            lines = (text := data.decode("utf-8-sig")).splitlines(keepends=True)
            index, = self._display_name_fields(lines, complete=True)
            name_before = FIELD.fullmatch(lines[index].rstrip("\r\n"))[2]
            lines[index] = replace_value(lines[index], project_name)
            self._project_text(candidate := prefix + "".join(lines).encode("utf-8"))
            candidates = {**originals, path: candidate}
            diff = "".join(itertools.islice(difflib.unified_diff(text.splitlines(keepends=True), lines, fromfile=path.name, tofile=path.name + " (candidate)"), 501))
            result = {
                "project": project, "before_files": inventory["files"],
                "files": {p.name: hashlib.sha256(v).hexdigest() for p, v in candidates.items()},
                "changed": candidate != data, "dry_run": dry_run, "diff": diff[:32000],
                "diff_truncated": len(diff) > 32000 or len(diff.splitlines()) > 500,
                "name_change": {"before": name_before, "after": project_name}, "bom_added": add_utf8_bom and not data.startswith(b"\xef\xbb\xbf"),
            }
            return result if (dry_run or candidate == data) else {**result, **self._commit_project_files(project, originals, candidates)}

    def update_project_sources(self, project: str, site_name: str, met_name: str, expected_files: dict[str, str] | None = None,
                               *, allow_synthetic_met: bool = False) -> dict:
        with self.lock:
            _, originals = self._project_files_snapshot(project, expected_files, "Project files changed since inspection or has no variants")
            if len(originals) < 2:
                raise ValueError("Project files changed since inspection or has no variants")
            site_ref = self._workspace_asset("Sites", site_name)
            if allow_synthetic_met and not met_name:
                if not site_ref["workspace_file"]:
                    raise ValueError("SIT must be an existing workspace file")
                effective_met_name, met_exists = f"{Path(site_name).stem}_Synthetic.MET", False
            else:
                if not site_ref["workspace_file"] or not self._workspace_asset("Meteo", met_name)["workspace_file"]:
                    raise ValueError("SIT and MET must be existing workspace files")
                effective_met_name, met_exists = met_name, True
            try:
                site_text = self._read(self.workspace / "Sites" / site_name).decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError("SIT must be UTF-8 text") from exc
            if "\x00" in site_text:
                raise ValueError("SIT contains NUL bytes")
            site_lines = site_text.splitlines(keepends=True)
            if len(site_lines) < 3 or site_lines[0].strip() != "PVObject_=pvSite" or site_lines[-1].strip() != "End of PVObject pvSite":
                raise ValueError("SIT is not a supported pvSite object")
            if any(ln.strip().startswith(("PVObject_", "End of PVObject")) for ln in site_lines[1:-1]):
                raise ValueError("SIT must contain one non-nested pvSite object")
            identifiers = [f[2] for ln in site_lines if (f := FIELD.fullmatch(ln.rstrip("\r\n"))) and f[1] == "Site"]
            filenames = [f[2] for ln in site_lines if (f := FIELD.fullmatch(ln.rstrip("\r\n"))) and f[1] == "NomF"]
            if len(identifiers) != 1 or not identifiers[0] or filenames != [site_name]:
                raise ValueError("SIT site identity or filename does not match")
            site_identity = identifiers[0]
            from pvsyst_auto.project.synthesize import _is_pvmeteo_file, load_meteo_header
            met_path = self.workspace / "Meteo" / effective_met_name
            meteo_block_lines = load_meteo_header(met_path, site_identity, [ln.strip() for ln in site_lines[1:-1] if ln.strip()])[1] if met_exists and _is_pvmeteo_file(met_path) else None
            candidates = {}
            for path, data in originals.items():
                lines = (text := data.decode("utf-8-sig")).splitlines(keepends=True)
                if path.name == project:
                    self._replace_site_block(lines, "SitePrj", site_lines)
                    self._replace_unique_field(lines, "MeteoFileName", effective_met_name, (0, len(lines)))
                else:
                    self._text(data)
                    self._replace_site_block(lines, "SiteSimul", site_lines)
                    start, end = self._section_bounds(lines, "PVObject_MeteoSimul=pvMeteo", "End of PVObject pvMeteo")
                    child = next((i for i in range(start + 1, end) if lines[i].strip().startswith("PVObject_")), end)
                    self._replace_unique_field(lines, "NomF", effective_met_name, (start + 1, child))
                    self._replace_unique_field(lines, "SiteM", site_identity, (start + 1, child))
                    if meteo_block_lines is not None:
                        self._section_bounds(lines, "PVObject_SiteMet=pvSite", "End of PVObject pvSite")
                        indent, eol = lines[start][:len(lines[start]) - len(lines[start].lstrip(" \t"))], ("\r\n" if "\r\n" in lines[start] else "\n")
                        lines[start:end + 1] = [eol if not raw_ln else (indent + (raw_ln[2:] if raw_ln.startswith("  ") else raw_ln) + eol) for raw_ln in meteo_block_lines]
                    else:
                        self._replace_site_block(lines, "SiteMet", site_lines)
                    if "".join(lines) != text:
                        from pvsyst_auto.native.headless import strip_result_cache
                        lines, _ = strip_result_cache(lines)
                candidate = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")
                self._project_text(candidate) if path.name == project else self._sections(self._text(candidate)[1])
                candidates[path] = candidate
            return {"project": project, "site": site_name, "meteo": met_name, **self._commit_project_files(project, originals, candidates)}

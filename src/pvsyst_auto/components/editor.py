"""Scoped component parsing, lossless replacements, PAN/curve helpers, identity, and generator editor."""
from __future__ import annotations

import csv
import io
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pvsyst_auto.versions import NATIVE_TEXT, NATIVE_TEXT_OR_SEED, SEED_LABEL, TEXT_LABEL

if TYPE_CHECKING:
    from pvsyst_auto.components.store import ComponentStore

ASSIGNMENT = re.compile(r"^([ \t]*)([A-Za-z_][A-Za-z0-9_#]*)([ \t]*=[ \t]*)(.*?)([ \t]*)$")
LINE_BREAK = re.compile(r"[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
CURVE_PATHS = frozenset(f"Converter/{n}" for n in ("ProfilPIO", "ProfilPIOV1", "ProfilPIOV2", "ProfilPIOV3"))
THREE_VOLTAGE_CURVES = frozenset(f"Converter/{n}" for n in ("ProfilPIOV1", "ProfilPIOV2", "ProfilPIOV3"))
MAX_POINTS = 256
MAIN_CURVE = "Converter/ProfilPIO"
AUTO_CURVE_MASK = 0x10
THREE_VOLTAGE_MASK = 0x1000
OBJECT_CLOSE_ALIAS = {"TAgeing": "pvAgeing", "TOrientGroup": "pvOrient",
                      "TDomeOrientGroup": "pvDomeOrient", "TSimulFich": "pvSimulFile"}
OBJECT_CLOSE = re.compile(r"\s*End of (?:PVObject )?(pv\w+|TConverter|TCubicProfile|%s)\s*"
                          % "|".join(map(re.escape, OBJECT_CLOSE_ALIAS)))
PRIVATE_OPEN = {
    ("pvProject", "PrintSettings", "TPrintSettings"), ("pvVCalcul", "BatchSimul", "TBatchSimul"),
    ("pvVCalcul", "EconomicEval", "TEcoEvaluation"), ("pvVCalcul", "P50_P90", "TP50_P90"),
    ("pvVCalcul", "Histogram", "THistogramme"), ("pvSimulPlot", "Histogram", "THistogramme"),
    ("pvCarbonBalance", "Custom_Fractions", "TEnergyMixSet"),
    ("pvCarbonBalance", "Custom_LCE_Definitions", "TEnergyMixSet"),
    ("pvSubArray", "WiringResistance", "TWiringResistance"), ("pvShdFTable", "SFTable", "TInterpole2D"),
    *(("pvMeteoTransp", "TransposTable_" + m, "TInterpole2D") for m in "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()),
    ("pvLoad", "HouseHoldUses", "THouseHoldUses"), ("pvLoad", "WaterUse", "TWaterUse"),
    ("pvLoad", "HourlyLoad", "THourlyParams"), ("pvHydrCircuit", "Fluide", "TFluide"),
    ("TEcoEvaluation", "HourlyTariff", "THourlyParams"),
    ("TEcoEvaluation", "HourlyCSVTariff", "TVarSimul"),
    ("TEcoEvaluation", "ConsumptionHourlyCSVTariff", "TVarSimul"),
    ("THourlyParams", "ParSimulH", "TVarSimul"),
    ("pvGridStorage", "ChargingPVFracParSim", "TParSimulSeries"),
    ("pvGridStorage", "ChargingPowerParSim", "TParSimulSeries"),
    ("pvGridStorage", "DischargingPowerParSim", "TParSimulSeries"),
    ("TParSimulSeries", "fVarSimul", "TVarSimul"),
}
PRIVATE_CLOSE = {
    **dict.fromkeys(("TBatchSimul", "TEnergyMixSet", "TP50_P90", "TPrintSettings", "TWiringResistance"), "EndTags"),
    "THouseHoldUses": "End of HouseHoldUses", "TWaterUse": "End of WaterUse", "TVarSimul": "End of VarSimul=",
    **{k: f"End of {k}" for k in ("TEcoEvaluation", "TFluide", "THistogramme", "THourlyParams", "TInterpole2D", "TParSimulSeries", "TPipe")},
}
PAN_ALIASES = {"ISC": ("ISC", "Isc"), "MuISC": ("MuISC", "muISC")}
PAN_CANONICAL = {alias: name for name, aliases in PAN_ALIASES.items() for alias in aliases}
PAN_DIMENSIONS = {"Width": "LargApp", "Height": "LongApp"}
HEADERS = {
    ("OND", "Converter/ProfilPIO"): ("input_watts", "output_watts"),
    ("OND", "Converter/ProfilPIOV1"): ("input_watts", "output_watts"),
    ("OND", "Converter/ProfilPIOV2"): ("input_watts", "output_watts"),
    ("OND", "Converter/ProfilPIOV3"): ("input_watts", "output_watts"),
    ("BTR", "CapaCourant"): ("discharge_hours", "relative_capacity"),
    ("BTR", "Capa_DischRate"): ("discharge_hours", "relative_capacity"),
}
MAX_CSV_BYTES = 65536
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
THRESHOLDS = ("VBkUpEncl_syst", "VBkUpDecl_syst")
GENERATOR_MASK = 0x20
SUPPORTED_VERSIONS = NATIVE_TEXT_OR_SEED


def replace_value(line: str, value: str) -> str:
    """Preserve the tag spelling, spaces, indentation and exact line ending."""
    body = line.rstrip("\r\n")
    match = ASSIGNMENT.fullmatch(body)
    if not match:
        raise ValueError("Expected a component assignment")
    if LINE_BREAK.search(value):
        raise ValueError("A component value must not contain a line break")
    return body[:match.start(4)] + value + body[match.end(4):] + line[len(body):]


def replace_key(line: str, key: str) -> str:
    """Rename a selected assignment without changing its value or whitespace."""
    body = line.rstrip("\r\n")
    match = ASSIGNMENT.fullmatch(body)
    if not match or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ValueError("Expected a component assignment and valid key")
    return body[:match.start(2)] + key + body[match.end(2):] + line[len(body):]


@dataclass
class ObjectBlock:
    path: str
    line: int
    object_type: str
    fields: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    end_line: int = 0

    def one(self, name: str) -> tuple[int, str]:
        values = self.fields.get(name, [])
        if len(values) != 1:
            loc = f"{self.object_type} at line {self.line}" + (f" ({self.path})" if self.path else "")
            occ = f"found {len(values)}" + (f", at lines {', '.join(str(i + 1) for i, _ in values)}" if values else "")
            raise ValueError(f"{name} must occur exactly once in {loc} ({occ})")
        return values[0]


@dataclass
class Profile(ObjectBlock):
    def points(self) -> list[list[float]]:
        def count(name: str) -> int:
            val = self.one(name)[1]
            if not re.fullmatch(r"[0-9]+", val):
                raise ValueError(f"{self.path}: {name} must be an integer")
            return int(val)

        capacity, effective = count("NPtsMax"), count("NPtsEff")
        if not 1 <= effective <= capacity <= MAX_POINTS:
            raise ValueError(f"{self.path}: inspection requires 1 <= NPtsEff <= NPtsMax <= {MAX_POINTS}")
        if {n for n in self.fields if n.startswith("Point_")} != {f"Point_{i}" for i in range(1, capacity + 1)}:
            raise ValueError(f"{self.path}: Point_N rows must match NPtsMax")
        pts = []
        for i in range(1, capacity + 1):
            raw = self.one(f"Point_{i}")[1].split(",")
            if len(raw) not in (2, 5):
                raise ValueError(f"{self.path}: invalid Point_{i}")
            try:
                vals = [float(v) for v in raw]
            except ValueError:
                raise ValueError(f"{self.path}: invalid Point_{i}") from None
            if not all(math.isfinite(v) for v in vals):
                raise ValueError(f"{self.path}: Point_{i} must be finite")
            if i <= effective:
                pts.append(vals[:2])
        return pts


def objects(text: str, *, include_private: bool = False) -> list[ObjectBlock]:
    """Keep each field in its owning object, including root and nested Flags."""
    stack: list[tuple[str, ObjectBlock]] = []
    result = []
    for index, line in enumerate(text.splitlines()):
        owner = stack[-1][1].object_type if stack else None
        closer = PRIVATE_CLOSE.get(owner)
        stripped = line.strip()
        if closer and (stripped == closer or (owner == "TVarSimul" and stripped.startswith(closer))):
            stack.pop()[1].end_line = index + 1
            continue
        end = OBJECT_CLOSE.fullmatch(line)
        if end:
            kind = OBJECT_CLOSE_ALIAS.get(end[1], end[1])
            if owner != kind:
                raise ValueError(f"Unmatched object closing tag at line {index + 1}")
            stack.pop()[1].end_line = index + 1
            continue
        match = ASSIGNMENT.fullmatch(line)
        if not match:
            continue
        name, value = match[2], match[4]
        pipe = (owner, name) == ("pvHydrCircuit", "Pipe") and value.isdecimal()
        private = pipe or ((owner, name, value) in PRIVATE_OPEN and (value != "TBatchSimul" or len(stack) == 1))
        if pipe:
            value = "TPipe"
        opens_object = (name.startswith("PVObject_") and re.fullmatch(r"pv\w+", value)) or value in ("TConverter", "TCubicProfile")
        if owner == "TBatchSimul" and (opens_object or private or (name, value) == ("BatchSimul", "TBatchSimul")):
            raise ValueError(f"Unclosed BatchSimul before line {index + 1}")
        if opens_object or private:
            if len(stack) >= 64:
                raise ValueError("Object nesting exceeds 64 supported levels")
            if stack and isinstance(stack[-1][1], Profile):
                stack[-1][1].problems.append("Nested objects inside curves are unsupported")
            path = "/".join([item[0] for item in stack[1:]] + [name]) if stack else ""
            block = (Profile if value == "TCubicProfile" else ObjectBlock)(path, index + 1, value)
            if not private or include_private:
                result.append(block)
            stack.append((name, block))
        elif stack:
            stack[-1][1].fields.setdefault(name, []).append((index, value))
    if stack:
        raise ValueError("Unclosed component object")
    return result


def root_block(text_or_lines: str | list[str], bounds: tuple[int, int] | None = None) -> ObjectBlock:
    """The top-level object of a text, or of lines[bounds[0]:bounds[1] + 1]."""
    lines = text_or_lines.splitlines(keepends=True) if isinstance(text_or_lines, str) else text_or_lines
    if bounds is not None:
        lines = lines[bounds[0]:bounds[1] + 1]
    root = next((block for block in objects("".join(lines)) if not block.path), None)
    if root is None:
        raise ValueError(f"{'Project file' if bounds is None else 'Section'} has no root PVObject block")
    return root


def has_object(lines, *object_types: str) -> bool:
    """Whether any line opens an object of one of `object_types`."""
    return any(
        (match := ASSIGNMENT.fullmatch(line.rstrip("\r\n")))
        and match[2].startswith("PVObject_") and match[4] in object_types
        for line in lines
    )


def profiles(text: str) -> list[Profile]:
    """Locate curves by their object path, never by flattened Point_N names."""
    return [block for block in objects(text) if isinstance(block, Profile)]


def checked_points(supplied: list, count: int | None = None) -> list[list[float]]:
    if not isinstance(supplied, list):
        raise ValueError("Supply a list of active points")
    if count is not None and len(supplied) != count:
        raise ValueError(f"Supply exactly {count} active points")
    if not 4 <= len(supplied) <= MAX_POINTS:
        raise ValueError(f"Supply 4..{MAX_POINTS} active points")
    result, last_x = [], -math.inf
    for pair in supplied:
        if not isinstance(pair, list) or len(pair) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in pair):
            raise ValueError("Each point must contain two finite numbers")
        try:
            x, y = map(float, pair)
        except (OverflowError, ValueError):
            raise ValueError("Each point must contain two finite numbers") from None
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("Each point must contain two finite numbers")
        if x <= last_x:
            raise ValueError("Active X values must be strictly increasing")
        if x < 1e-9 or x - last_x < 1e-8:
            raise ValueError("Native compilation can remove points: require X >= 1e-9 and gaps >= 1e-8")
        result.append([x, y])
        last_x = x
    return result


def point_count_errors(profile: Profile) -> list[str]:
    """Requirements for replacing a complete, ordered Mode 1 point table."""
    errors = []
    try:
        compile_fields = profile.fields.get("LastCompile", [])
        if len(compile_fields) > 1:
            raise ValueError("LastCompile must occur at most once")
        if compile_fields:
            raw = compile_fields[0][1]
            if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw) or int(raw[1:], 16) not in (0, 0x19, 0x8d, 0x8089):
                raise ValueError("Point-count editing supports LastCompile absent, $0, $19, $008D or $8089")
        capacity = int(profile.one("NPtsMax")[1])
        rows = [profile.one(f"Point_{i}")[0] for i in range(1, capacity + 1)]
        if rows != list(range(rows[0], rows[0] + capacity)):
            raise ValueError("Point-count editing requires consecutive Point_N rows in numeric order")
        if any(idx >= rows[0] for key in ("NPtsMax", "NPtsEff", "Mode", "LastCompile") for idx, _ in profile.fields.get(key, [])):
            raise ValueError("Curve count/mode/compile fields must precede the point table")
    except (ValueError, IndexError) as exc:
        errors.append(str(exc))
    return errors


def write_points(lines: list[str], profile: Profile, previous: list, points: list) -> None:
    if len(points) != len(previous):
        if errors := point_count_errors(profile):
            raise ValueError("; ".join(errors))
        cap_idx, cap = profile.one("NPtsMax")
        eff_idx, _ = profile.one("NPtsEff")
        start, _ = profile.one("Point_1")
        template = lines[start]
        table = [replace_value(replace_key(template, f"Point_{i}"), f"{x:.17g},{y:.17g}") for i, (x, y) in enumerate(points, 1)]
        lines[cap_idx] = replace_value(lines[cap_idx], str(len(points)))
        lines[eff_idx] = replace_value(lines[eff_idx], str(len(points)))
        lines[start:start + int(cap)] = table
        return
    for i, pair in enumerate(points, 1):
        if pair != previous[i - 1]:
            lines[profile.one(f"Point_{i}")[0]] = replace_value(lines[profile.one(f"Point_{i}")[0]], f"{pair[0]:.17g},{pair[1]:.17g}")


def _curve_control(blocks: list[ObjectBlock]) -> tuple[dict, tuple[int, str] | None]:
    control = {"source": "unknown", "three_voltage": None, "flags": None, "version": None, "errors": []}
    flag_field = None
    try:
        roots = [b for b in blocks if b.path == ""]
        if len(roots) != 1 or roots[0].object_type != "pvGInverter":
            raise ValueError("Expected exactly one root pvGInverter object")
        root = roots[0]
        flag_field = root.one("Flags")
        if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", flag_field[1]):
            raise ValueError("Root Flags must be a hexadecimal value of at most 32 bits")
        flags = int(flag_field[1][1:], 16)
        control.update(flags=flag_field[1], source="automatic" if flags & AUTO_CURVE_MASK else "file",
                       three_voltage=bool(flags & THREE_VOLTAGE_MASK))
        version = root.one("Version")[1]
        control["version"] = version[:256]
        if version not in NATIVE_TEXT:
            raise ValueError(f"Curve editing is verified only for OND Version={TEXT_LABEL}")
        converters = [b for b in blocks if b.path == "Converter"]
        if len(converters) != 1 or converters[0].object_type != "TConverter":
            raise ValueError("Expected exactly one root Converter=TConverter")
        if control["three_voltage"]:
            found_3v = {b.path for b in blocks if isinstance(b, Profile) and b.path in THREE_VOLTAGE_CURVES}
            if not converters[0].fields.get("VNomEff") or found_3v != THREE_VOLTAGE_CURVES:
                raise ValueError("Three-voltage OND curve editing requires VNomEff and ProfilPIOV1..ProfilPIOV3")
    except ValueError as exc:
        control["errors"].append(str(exc))
    return control, flag_field


def _edit_errors(profile: Profile, points: list[list[float]], *, three_voltage: bool = False) -> list[str]:
    errors = []
    if profile.path not in (THREE_VOLTAGE_CURVES if three_voltage else {MAIN_CURVE}):
        errors.append("Three-voltage OND edits support Converter/ProfilPIOV1..ProfilPIOV3 only"
                      if three_voltage else "Only Converter/ProfilPIO is editable")
    if len(points) < 4:
        errors.append("Curve editing requires at least four active points")
    try:
        if profile.one("Mode")[1] != "1":
            errors.append("Only profile Mode=1 is supported for editing")
    except ValueError as exc:
        errors.append(str(exc))
    return errors


def inspect_profile_points(profile: Profile, blocks: list) -> tuple[list[list[float]], list[str]]:
    """Extract profile points and structural/duplication errors for one curve block."""
    errors, points = list(profile.problems), []
    try:
        points = profile.points()
    except ValueError as exc:
        errors.append(str(exc))
    if sum(other.path == profile.path for other in blocks) != 1:
        errors.append("Ambiguous duplicate curve path")
    return points, errors


def curve_inventory(kind: str, text: str) -> dict:
    if kind != "OND":
        return {"items": [], "truncated": False}
    try:
        blocks = objects(text)
    except ValueError as exc:
        return {"items": [], "errors": [str(exc)], "truncated": False}
    found = [b for b in blocks if isinstance(b, Profile)]
    control, _ = _curve_control(blocks)
    is_3v = bool(control["three_voltage"]) and not control["errors"]
    items = []
    for profile in found[:16]:
        points, errors = inspect_profile_points(profile, blocks)
        edit_errors = errors + control["errors"] + _edit_errors(profile, points, three_voltage=is_3v)
        items.append({
            "path": profile.path, "line": profile.line, "editable": not edit_errors,
            "structure_complete": not errors, "errors": errors, "edit_errors": edit_errors,
            "requires_use_file_curve": control["source"] == "automatic", "requires_expected_sha256": True,
            "points": points, "point_count": len(points),
            "allocated_point_count": int(profile.one("NPtsMax")[1]) if not errors else None,
            "point_count_editable": not edit_errors and not point_count_errors(profile),
            "point_count_errors": point_count_errors(profile) if not errors else errors,
            "point_count_range": [4, MAX_POINTS],
            "axes": ["input power (W)", "output power (W)"] if profile.path in CURVE_PATHS else None,
        })
    return {"control": control, "items": items, "truncated": len(found) > len(items)}


def replace_curves(kind: str, text: str, updates: dict[str, list[list[float]]], use_file_curve: bool = False) -> str:
    if kind != "OND" or not isinstance(updates, dict) or not updates:
        raise ValueError("Curve edits support only OND Converter/ProfilPIO (or ProfilPIOV1..V3 in 3-voltage mode)")
    blocks = objects(text)
    control, flag_field = _curve_control(blocks)
    if control["errors"]:
        raise ValueError("; ".join(control["errors"]))
    is_3v = bool(control["three_voltage"])
    if not set(updates) <= (THREE_VOLTAGE_CURVES if is_3v else {MAIN_CURVE}):
        raise ValueError("Three-voltage OND curve edits support Converter/ProfilPIOV1..ProfilPIOV3 only"
                         if is_3v else "Curve edits support only OND Converter/ProfilPIO")
    if control["source"] == "automatic" and not use_file_curve:
        raise ValueError("Automatic curve is enabled; supply use_file_curve=true to use supplied points")

    plan: list[tuple[Profile, list, list]] = []
    for target_path, raw_pts in updates.items():
        matches = [b for b in blocks if b.path == target_path]
        if len(matches) != 1 or not isinstance(matches[0], Profile):
            raise ValueError(f"{target_path} must identify exactly one supported curve")
        profile = matches[0]
        previous = profile.points()
        if errors := profile.problems + _edit_errors(profile, previous, three_voltage=is_3v):
            raise ValueError("; ".join(errors))
        points = checked_points(raw_pts)
        if any(not 0 <= y <= x for x, y in points):
            raise ValueError("Supported power points require 0 <= output <= input")
        plan.append((profile, previous, points))

    lines = text.splitlines(keepends=True)
    if control["source"] == "automatic":
        index, raw = flag_field
        digit = format(int(raw[-2], 16) & ~1, "x" if raw[-2].islower() else "X")
        lines[index] = replace_value(lines[index], raw[:-2] + digit + raw[-1])
    for profile, previous, points in sorted(plan, key=lambda item: item[0].line, reverse=True):
        write_points(lines, profile, previous, points)
    return "".join(lines)


def import_curve_csv(kind: str, supplied: dict[str, str]) -> dict[str, list[list[float]]]:
    """Parse one full curve CSV table without inferring units, separators, or missing rows."""
    if not isinstance(supplied, dict) or len(supplied) != 1:
        raise ValueError("curve_csv must contain exactly one curve path and CSV text")
    path, text = next(iter(supplied.items()))
    header = HEADERS.get((kind, path))
    if header is None:
        raise ValueError("CSV supports OND Converter/ProfilPIO and BTR CapaCourant/Capa_DischRate only")
    if not isinstance(text, str) or len(text) > MAX_CSV_BYTES or len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise ValueError(f"Curve CSV must be text of at most {MAX_CSV_BYTES} UTF-8 bytes")
    rows = csv.reader(io.StringIO(text.removeprefix("\ufeff"), newline=""), strict=True)
    points = []
    try:
        if next(rows, None) != list(header):
            raise ValueError("Curve CSV requires the header: " + ",".join(header))
        for row in rows:
            if len(points) >= MAX_POINTS:
                raise ValueError(f"Curve CSV supports at most {MAX_POINTS} point rows")
            if len(row) != 2 or any(not NUMBER.fullmatch(v.strip(" \t")) for v in row):
                raise ValueError(f"Curve CSV row {rows.line_num} requires two decimal numbers; blanks and formulas are unsupported")
            points.append([float(v) for v in row])
    except csv.Error as exc:
        raise ValueError(f"Invalid curve CSV: {exc}") from None
    return {path: checked_points(points)}


def export_curve_csv(kind: str, inventory: dict) -> dict:
    """Export complete profiles with known axis semantics."""
    exported, errors = {}, {}
    if inventory.get("errors"):
        errors["<component>"] = inventory["errors"]
    for item in inventory["items"]:
        path = item["path"]
        header = HEADERS.get((kind, path))
        if header is None:
            errors[path] = ["CSV axis semantics are unsupported for this curve"]
        elif not item["structure_complete"]:
            errors[path] = item["errors"]
        else:
            stream = io.StringIO(newline="")
            writer = csv.writer(stream, lineterminator="\r\n")
            writer.writerow(header)
            writer.writerows([[format(v, ".17g") for v in row] for row in item["points"]])
            exported[path] = stream.getvalue()
    return {"curve_csv": exported, "curve_csv_errors": errors}


def pan_blocks(text: str) -> tuple[ObjectBlock, ObjectBlock]:
    blocks = objects(text)
    roots = [b for b in blocks if not b.path]
    commercial = [b for b in blocks if b.path == "PVObject_Commercial"]
    if len(roots) != 1 or roots[0].object_type != "pvModule":
        raise ValueError("Expected one root pvModule object")
    if len(commercial) != 1 or commercial[0].object_type != "pvCommercial":
        raise ValueError("Expected one direct PVObject_Commercial=pvCommercial")
    return roots[0], commercial[0]


def pan_validation_fields(text: str) -> dict[str, list[str]]:
    """Validate physical fields in root and identity/dimensions in commercial form."""
    root, commercial = pan_blocks(text)
    fields: dict[str, list[str]] = {}
    for key, values in root.fields.items():
        fields.setdefault(PAN_CANONICAL.get(key, key), []).extend(v for _, v in values)
    for key in ("Manufacturer", "Model", "Width", "Height", "Depth"):
        fields.pop(key, None)
        if key in commercial.fields:
            fields[key] = [v for _, v in commercial.fields[key]]
    return fields


validation_fields = pan_validation_fields


def sourced_component(store: ComponentStore, kind: str, filename: str) -> tuple[str, str, dict | None]:
    """Read one loose UTF-8 component by exact filename and bind its declared identity."""
    path = None
    for library in ("workspace", "builtin"):
        if library == "builtin" and store.builtin is None:
            continue
        try:
            candidate = store._path(kind, filename, library)
        except ValueError as exc:
            return f"{kind}.name", str(exc), None
        if candidate.is_file():
            path = candidate
            break
    if path is None:
        return f"{kind}.name", f"{filename} is not a loose {kind} file in the workspace or the installed library", None
    try:
        text = store._text(kind, store._read(path))[1]
    except (ValueError, OSError) as exc:
        return f"{kind}.name", str(exc), None
    if text is None:
        return f"{kind}.name", "Legacy binary PAN carries no readable declared identity; use a UTF-8 text component", None
    try:
        blocks = objects(text)
    except ValueError as exc:
        return f"{kind}.name", str(exc), None
    roots = [b for b in blocks if not b.path]
    commercial = [b for b in blocks if b.path == "PVObject_Commercial"]
    if len(roots) != 1 or len(commercial) > 1:
        return f"{kind}.name", "Expected exactly one root object and at most one PVObject_Commercial block", None
    owners = ([("Commercial", commercial[0].fields)] if commercial else []) + [("root", roots[0].fields)]
    identity = {}
    for name, key in (("Manufacturer", "manufacturer"), ("Model", "model")):
        declared = {}
        for label, flds in owners:
            vals = [v.strip() for _, v in flds.get(name, []) if v.strip()]
            if len(vals) > 1:
                return f"{kind}.{key}", f"{name} occurs {len(vals)} times in its {label} object; exactly one occurrence is bound", None
            if vals:
                declared[label] = vals[0]
        if len({*declared.values()}) > 1:
            return f"{kind}.{key}", f"{name} is declared differently in the root object and the Commercial block", None
        if not declared:
            return f"{kind}.{key}", f"{name} is missing in its owning object", None
        identity[key] = next(iter(declared.values()))
    electrical_inventory_fn = globals().get("electrical_inventory") or __getattr__("electrical_inventory")
    return "", "", {"manufacturer": identity["manufacturer"], "model": identity["model"], "electrical": electrical_inventory_fn(kind, text)}


def direct_fields(lines, bounds):
    """Read only fields owned by a system/subarray, skipping nested objects."""
    fields, stack, in_unavail = {}, [], False
    for i in range(bounds[0] + 1, bounds[1]):
        line = lines[i].strip()
        if in_unavail:
            if line == "End of PVObject pvUnavail":
                in_unavail = False
            continue
        match = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))
        if line == "SystemUnavail=TUnavailability":
            in_unavail = True
        elif match and (match[2].startswith("PVObject_") and re.fullmatch(r"pv\w+", match[4])
                        or match[2] != "Comment" and re.fullmatch(r"T[A-Z]\w*", match[4])):
            stack.append(match[4])
        elif line.startswith("End of VarSimul="):
            if stack and stack[-1] == "TVarSimul":
                stack.pop()
        elif line.startswith("End of PVObject ") or line.startswith("End of T"):
            kind = OBJECT_CLOSE_ALIAS.get(line.removeprefix("End of PVObject ").removeprefix("End of "), line.removeprefix("End of PVObject ").removeprefix("End of "))
            if not stack or stack.pop() != kind:
                raise ValueError("Unmatched nested object in generator configuration scope")
        elif line == "EndTags":
            if not stack:
                raise ValueError("Unmatched EndTags in generator configuration scope")
            stack.pop()
        elif match and not stack:
            fields.setdefault(match[2], []).append((i, match[4]))
    if stack or in_unavail:
        raise ValueError("Unclosed nested object in generator configuration scope")
    return fields


def one(fields, name, required=True):
    entries = fields.get(name, [])
    if len(entries) != 1:
        if not entries and not required:
            return None
        raise ValueError(f"{name} must occur exactly once in its owning object")
    return entries[0]


def finite(value, label, minimum=0, maximum=math.inf, positive=False):
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{label} must be a finite number") from None
    if not math.isfinite(number) or not minimum <= number <= maximum or (positive and number <= 0):
        raise ValueError(f"{label} is outside the supported range")
    return number


def _read_number(fields, name):
    raw = one(fields, name, False)
    if raw is None:
        return None
    try:
        value = float(raw[1])
    except ValueError:
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def generator_state(lines, arrays, system):
    result = {"supported": False, "errors": [], "enabled": None, "filename": None,
              "operating_power_kw": None, "flags": None, "subarrays": [],
              "threshold_units": "normalized voltage thresholds (0..1)"}
    try:
        if system is None:
            raise ValueError("No pvSystem object")
        first_child = next((i for i, line in enumerate(lines[1:], 1) if re.match(r"\s*PVObject_.*=pv\w+", line)), len(lines) - 1)
        if one(direct_fields(lines, (0, first_child)), "Version")[1] not in SUPPORTED_VERSIONS:
            raise ValueError(f"Generator configuration supports variant Version={SEED_LABEL}")
        fields = direct_fields(lines, system)
        if one(fields, "SystemType")[1] != "Battery":
            raise ValueError("Generator configuration requires SystemType=Battery")
        flags = one(fields, "Flags")[1]
        if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", flags):
            raise ValueError("System Flags must be hexadecimal, at most 32 bits")
        filename = one(fields, "GensetFile", False)
        result.update(flags=flags, enabled=bool(int(flags[1:], 16) & GENERATOR_MASK),
                      filename=filename[1] if filename else None, operating_power_kw=_read_number(fields, "PEffBackUp"))
        for identifier, bounds in sorted(arrays.items()):
            f = direct_fields(lines, bounds)
            result["subarrays"].append({"id": identifier, **{name: _read_number(f, name) for name in THRESHOLDS}})
        result["supported"] = True
    except ValueError as exc:
        result["errors"].append(str(exc))
    return result


state = generator_state


def replace_generator(lines, arrays, system, updates, components):
    if not isinstance(updates, dict) or not updates or set(updates) - {"enabled", "filename", "operating_power_kw", "thresholds"}:
        raise ValueError("Generator updates: enabled, filename, operating_power_kw, thresholds")
    before = generator_state(lines, arrays, system)
    if not before["supported"]:
        raise ValueError("; ".join(before["errors"]))
    if "enabled" in updates and not isinstance(updates["enabled"], bool):
        raise ValueError("enabled must be a boolean")
    enabled = updates.get("enabled", before["enabled"])
    filename = updates.get("filename", before["filename"])
    power = updates.get("operating_power_kw", before["operating_power_kw"])
    if "operating_power_kw" in updates or enabled:
        power = finite(power, "operating_power_kw", positive=True)
    warnings, dependency = [], None
    if "filename" in updates or enabled:
        path = components._path("GEN", filename)
        library = "workspace"
        if not path.is_file() and components.builtin:
            path, library = components._path("GEN", filename, "builtin"), "builtin"
        info = components._inspect("GEN", components._read(path))
        components._require_valid(info, complete=True)
        nominal = float(info["fields"]["PNomGen"][0])
        dependency = {"filename": filename, "library": library, "sha256": info["sha256"], "nominal_power_kw": nominal}
        if power is not None and power > nominal:
            warnings.append("Operating power exceeds GEN nominal power; review system sizing")
    thresholds = updates.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise ValueError("thresholds must map subarray IDs to threshold dictionaries")
    selected = {}
    for key, values in thresholds.items():
        if not isinstance(key, str) or not re.fullmatch(r"[1-9][0-9]*", key) or int(key) not in arrays:
            raise ValueError("Each thresholds key must be an existing positive subarray ID")
        if not isinstance(values, dict) or not values or set(values) - set(THRESHOLDS):
            raise ValueError("Threshold fields are VBkUpEncl_syst and VBkUpDecl_syst")
        selected[int(key)] = {name: finite(val, name, maximum=1) for name, val in values.items()}
    for array in before["subarrays"]:
        merged = array | selected.get(array["id"], {})
        if enabled:
            for name in THRESHOLDS:
                finite(merged[name], f"subarray {array['id']} {name}", maximum=1)
    insertions = []

    def assign(bounds, values):
        flds = direct_fields(lines, bounds)
        ending = "\r\n" if lines[bounds[0]].endswith("\r\n") else "\n"
        indent = re.match(r"[ \t]*", lines[bounds[0]])[0] + "  "
        added = []
        for name, value in values.items():
            old = one(flds, name, False)
            rendered = value if isinstance(value, str) else format(value, ".17g")
            if old:
                if (isinstance(value, str) and old[1] == value) or (not isinstance(value, str) and float(old[1]) == value):
                    continue
                lines[old[0]] = replace_value(lines[old[0]], rendered)
            else:
                added.append(f"{indent}{name}={rendered}{ending}")
        if added:
            insertions.append((bounds[1], added))

    system_values = {}
    if "enabled" in updates:
        raw = before["flags"]
        digits = raw[1:].rjust(2, "0") if enabled else raw[1:]
        if len(digits) >= 2:
            old_ch = digits[-2]
            digit = (int(old_ch, 16) | 2) if enabled else (int(old_ch, 16) & ~2)
            digits = digits[:-2] + format(digit, "x" if old_ch.islower() else "X") + digits[-1]
        system_values["Flags"] = "$" + digits
    if "filename" in updates:
        system_values["GensetFile"] = filename
    if "operating_power_kw" in updates:
        system_values["PEffBackUp"] = power
    assign(system, system_values)
    for identifier, values in selected.items():
        assign(arrays[identifier], values)
    for position, added in sorted(insertions, reverse=True):
        lines[position:position] = added
    return {"before": before, "warnings": warnings, "component": dependency}


def read_flat_grid_system(
    lines: list[str], system: tuple[int, int] | None, *, feature_label: str, owned_fields: Iterable[str],
    load_storage_error: str, unknown_field_error: str, hex_flag_error: str,
    exclusive_system_fields: Iterable[str] = (),
) -> tuple[dict[str, list[tuple[int, str]]], int, int]:
    """Validate and parse a flat native 8.1.6 Grid pvSystem block for whole-system editors."""
    if system is None:
        raise ValueError("Require one owning native pvSystem")
    first = next((i for i, line in enumerate(lines[1:], 1) if line.strip().startswith("PVObject_")), len(lines) - 1)
    if one(direct_fields(lines, (0, first)), "Version")[1] not in NATIVE_TEXT:
        raise ValueError(f"{feature_label} requires variant Version={TEXT_LABEL}")
    if has_object(lines, "pvLoad", "pvGridStorage"):
        raise ValueError(load_storage_error)
    if any(line.strip().startswith("PVObject_") or line.strip() == "EndTags" for line in lines[system[0] + 1:system[1]]):
        raise ValueError("Require a flat native grid system")
    fields = direct_fields(lines, system)
    if set(fields) - {"Comment", "Flags", "SystemType", *owned_fields}:
        raise ValueError(unknown_field_error)
    for key in fields:
        one(fields, key)
    if one(fields, "SystemType")[1] != "Grid":
        raise ValueError(f"{feature_label} requires SystemType=Grid")
    index, raw = one(fields, "Flags")
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw):
        raise ValueError(hex_flag_error)
    for key in exclusive_system_fields:
        total = sum(m is not None and m[2] == key for line in lines for m in [ASSIGNMENT.fullmatch(line.rstrip("\r\n"))])
        if total != len(fields.get(key, [])):
            raise ValueError(f"Require {key} only in the owning system")
    return fields, index, int(raw[1:], 16)


def __getattr__(name: str):
    if name in ("ComponentStore", "electrical_inventory"):
        from pvsyst_auto.components import store as _store

        value = getattr(_store, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


import sys as _sys

if "pvsyst_auto.components.store" not in _sys.modules and "pvsyst_auto.components.builder" not in _sys.modules:
    import pvsyst_auto.components.store

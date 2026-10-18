"""Create a NEW VC with one verified native flat load block; no load model or cache."""
from __future__ import annotations

import difflib
import re
from decimal import Decimal, InvalidOperation

from pvsyst_component_editor import objects, replace_value
from pvsyst_generator import finite
from pvsyst_grid import inventory as grid_inventory

START = "PVObject_UserNeeds=pvLoad"
END = "End of PVObject pvLoad"
ANCHOR = "SimulPlots=Start"
BOM = b"\xef\xbb\xbf"
# Adapter bound only; not a native or engineering limit.
LIMIT = 1e6
STEP = Decimal("0.001")
# Parser view only: the saved bytes keep their original orientation closing tag.
ORIENT_CLOSE = re.compile(r"(?m)^([ \t]*)End of TOrientGroup([ \t]*)(?=\r?$)")
COMMENTS = {"constant": "MCP native constant load", "monthly": "MCP native monthly load",
            "daily": "MCP native daily profile load"}
ALLOWED = {
    "Constant": ("Comment", "Flags", "LoadType", "Energy Units", "ConstantLoad"),
    "Monthly": ("Comment", "Flags", "LoadType", "UsesModul", "Energy Units", "ValMonth"),
    "DailyProfile": ("Comment", "Flags", "LoadType", "UsesModul", "Energy Units", "NHourlyValues", "ValHour_1"),
}
REQUIRED = {
    "Constant": ("Flags", "LoadType", "Energy Units", "ConstantLoad"),
    "Monthly": ("Flags", "LoadType", "UsesModul", "Energy Units", "ValMonth"),
    "DailyProfile": ("Flags", "LoadType", "UsesModul", "Energy Units", "NHourlyValues", "ValHour_1"),
}
# Whole-line expectations, so `Energy Units` (with its space) is verified rather than guessed.
EXPECTED = {
    "Constant": {"Flags": "$00", "LoadType": "Constant", "Energy Units": "kW"},
    "Monthly": {"Flags": "$01", "LoadType": "Monthly", "UsesModul": "Yearly", "Energy Units": "kW"},
    "DailyProfile": {"Flags": "$02", "LoadType": "DailyProfile", "UsesModul": "Yearly",
                     "Energy Units": "kW", "NHourlyValues": "1"},
}
# mode -> (declared values, trailing slots, trailing slot equals their sum)
SERIES = {"Monthly": ("ValMonth", 12, 3, False), "DailyProfile": ("ValHour_1", 24, 1, True)}


def _view(lines):
    return ORIENT_CLOSE.sub(r"\1End of PVObject pvOrient\2", "".join(lines))


def _root_block(lines):
    return next(block for block in objects(_view(lines)) if not block.path)


def _block_of(lines, bounds):
    blocks = objects(_view(lines[bounds[0]:bounds[1] + 1]))
    return next(block for block in blocks if not block.path)


def _decimal(text, label, maximum=LIMIT):
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a decimal number") from None
    if not value.is_finite():
        raise ValueError(f"{label} must be finite")
    if value < 0 or value > Decimal(str(maximum)):
        raise ValueError(f"{label} must be within 0..{_plain(maximum)} (adapter bound, not a native limit)")
    if value != value.quantize(STEP):
        raise ValueError(f"{label} must use at most 0.001 steps")
    return value


def _tail(text, label):
    """Trailing native slot is an exact sum of declared inputs, so it is not capped per value."""
    return _decimal(text, label, LIMIT * 24)


def _number(value, label, positive=False):
    number = finite(value, label, 0, LIMIT, positive)
    _decimal(str(value), label)
    return number


def _plain(value):
    """Shortest exact text for one 0.001-step value; never rounds through %g."""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def native_load_block(load, newline, blank_before=False):
    """Serialize the three verified native flat forms; trailing slots are format only."""
    if not isinstance(load, dict):
        raise ValueError("load must be one explicit mode object")
    mode = load.get("mode")
    if mode == "constant":
        if set(load) != {"mode", "constant_kw"}:
            raise ValueError("constant load accepts only mode and constant_kw")
        constant = _number(load["constant_kw"], "constant_kw", positive=True)
        data = ["    Flags=$00", "    LoadType=Constant", "    Energy Units=kW",
                "    ConstantLoad=" + _plain(constant)]
    elif mode == "monthly":
        if set(load) != {"mode", "monthly_energy_kwh"}:
            raise ValueError("monthly load accepts only mode and monthly_energy_kwh")
        raw = load["monthly_energy_kwh"]
        if not isinstance(raw, list) or len(raw) != 12:
            raise ValueError("monthly_energy_kwh must contain 12 monthly values")
        energies = [_number(value, f"monthly_energy_kwh[{index}]") for index, value in enumerate(raw)]
        if not any(value > 0 for value in energies):
            raise ValueError("monthly_energy_kwh requires at least one positive month")
        data = ["    Flags=$01", "    LoadType=Monthly", "    UsesModul=Yearly", "    Energy Units=kW",
                "    ValMonth=" + "".join(f"{value:.3f}," for value in energies) + "0.000,0.000,0.000,"]
    elif mode == "daily":
        if set(load) != {"mode", "daily_power_kw"}:
            raise ValueError("daily load accepts only mode and daily_power_kw")
        raw = load["daily_power_kw"]
        if not isinstance(raw, list) or len(raw) != 24:
            raise ValueError("daily_power_kw must contain 24 hourly values")
        powers = [_number(value, f"daily_power_kw[{index}]") for index, value in enumerate(raw)]
        if not any(value > 0 for value in powers):
            raise ValueError("daily_power_kw requires at least one positive hour")
        total = sum((Decimal(str(value)) for value in raw), Decimal(0))
        data = ["    Flags=$02", "    LoadType=DailyProfile", "    UsesModul=Yearly", "    Energy Units=kW",
                "    NHourlyValues=1",
                "    ValHour_1=" + "".join(f"{value:.3f}," for value in powers) + _plain(total) + ","]
    else:
        raise ValueError("load.mode must be constant, monthly or daily")
    rendered = ([""] if blank_before else []) + ["  " + START, "    Comment=" + COMMENTS[mode]] + data + ["  " + END]
    return newline.join(rendered) + newline


def _assignments(lines, bounds):
    """Narrow whole-line parse: `Energy Units` keeps its space, so no shared ASSIGNMENT regex."""
    fields = {}
    for index in range(bounds[0] + 1, bounds[1]):
        raw = lines[index].rstrip("\r\n")
        stripped = raw.strip()
        if not stripped:
            raise ValueError("Blank line inside the load block")
        if stripped.startswith("PVObject_") or stripped.startswith("End of PVObject"):
            raise ValueError("Nested or duplicate load objects are not supported")
        key, separator, value = raw.partition("=")
        key = key.strip()
        if not separator or not key or key in fields:
            raise ValueError("Unsupported load assignment")
        fields[key] = value.strip()
    return fields


def _series(text, label, count, trailing):
    """Declared head values are each capped at 1e6; the trailing slots are checked separately."""
    parts = text.split(",")
    if parts and not parts[-1].strip():
        parts.pop()
    if len(parts) != count + trailing or any(not part.strip() for part in parts):
        raise ValueError(f"{label} must contain {count + trailing} comma-separated values")
    head = [_decimal(part, f"{label}[{index}]") for index, part in enumerate(parts[:count])]
    tail = [_tail(part, f"{label}[{count + index}]") for index, part in enumerate(parts[count:])]
    return head, tail


def _verify_existing(mode, fields):
    """Reject a saved block whose own values are outside the verified 0.001 domain or shape."""
    if mode == "Constant":
        if not _decimal(fields["ConstantLoad"], "ConstantLoad") > 0:
            raise ValueError("Declared ConstantLoad must be greater than zero")
        return
    key, count, trailing, plus_sum = SERIES[mode]
    head, tail = _series(fields[key], key, count, trailing)
    if not any(value > 0 for value in head):
        raise ValueError(f"Declared {key} requires at least one positive value")
    if plus_sum:
        if sum(tail) != sum(head):
            raise ValueError(f"Declared {key} trailing slot must equal the sum of its {count} values")
    elif any(value != 0 for value in tail):
        raise ValueError(f"Declared {key} trailing slots must be zero")


def flat_load(lines):
    """Return the one directly owned flat load block, or None; unknown shapes stop instead of being replaced."""
    loads = [block for block in objects(_view(lines)) if block.object_type == "pvLoad"]
    if len(loads) > 1:
        raise ValueError("Only one owning pvLoad object is supported")
    if not loads:
        return None
    block = loads[0]
    if block.path != "PVObject_UserNeeds" or not block.end_line:
        raise ValueError("pvLoad must be the complete directly owned PVObject_UserNeeds block")
    begin, end = block.line - 1, block.end_line - 1
    if not 0 <= begin < end < len(lines):
        raise ValueError("Unclosed pvLoad object")
    fields = _assignments(lines, (begin, end))
    mode = fields.get("LoadType")
    if mode not in ALLOWED:
        raise ValueError(f"Unsupported or unknown LoadType: {mode!r}")
    if set(fields) - set(ALLOWED[mode]) or set(REQUIRED[mode]) - set(fields):
        raise ValueError("Unknown or missing fields in the load block")
    if any(fields.get(key) != value for key, value in EXPECTED[mode].items()):
        raise ValueError("Declared load fields do not match the verified native form")
    _verify_existing(mode, fields)
    return begin, end, mode, fields


def _require_scope(store, lines, arrays, system):
    """Verified native 8.1.6 PV-only single-subarray Grid variant; no storage, scene or P50."""
    root = _root_block(lines)
    if root.one("Version")[1] != "8.1.6":
        raise ValueError("Requires a native 8.1.6 variant")
    flags = root.one("Flags")[1]
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) or int(flags[1:], 16) not in (0, 0x00080000):
        raise ValueError("Root Flags must be 0 or 0x80000")
    if system is None or len(arrays) != 1:
        raise ValueError("Requires one pvSystem and one subarray")
    system_root = _block_of(lines, system)
    if set(system_root.fields) != {"Flags", "SystemType"}:
        raise ValueError("pvSystem must declare only Flags and SystemType for this batch")
    if system_root.one("SystemType")[1] != "Grid":
        raise ValueError("SystemType must be Grid")
    if system_root.one("Flags")[1] != "$06000000":
        raise ValueError("This batch requires pvSystem Flags=$06000000")
    main = store._main_array(lines)
    if main is None:
        raise ValueError("A pvMainArray section is required")
    main_root = _block_of(lines, main)
    if main_root.one("Flags")[1] != "$01900400" or main_root.one("Flags2")[1] != "$082A":
        raise ValueError("Main array requires the verified Flags=$01900400 and Flags2=$082A")
    sub_root = _block_of(lines, next(iter(arrays.values())))
    if sub_root.one("Version")[1] != "8.1.6":
        raise ValueError("Subarray requires the verified 8.1.6 declaration")
    if sub_root.one("Flags")[1] != "$019E1002":
        raise ValueError("Subarray requires the verified 019E1002 flag family")
    strings = sub_root.one("NStringCh")[1]
    if (sub_root.one("NoOrientation")[1] != "1" or not strings.isdigit() or int(strings) < 1
            or sub_root.one("NStrOrient1")[1] != strings):
        raise ValueError("Requires all strings on orientation 1 with NStrOrient1 equal to NStringCh")
    if any(key.startswith("NStrOrient") and key != "NStrOrient1" and sub_root.one(key)[1] != "0"
           for key in sub_root.fields):
        raise ValueError("Every other NStrOrient* must be zero")
    store._require_no_geometry(lines)
    if any("pvGridStorage" in line or re.match(r"\s*(?:P50_P90|PVObject_Horizon)\s*=", line) for line in lines):
        raise ValueError("Grid storage, P50/P90 and saved horizon objects are outside this batch")
    if sum(line.strip() == "PVObject_=pvOrient" for line in lines) != 1:
        raise ValueError("Requires exactly one orientation record")
    store._ordinary_fixed_plane(lines, store._orientation(lines, 1))
    if grid_inventory(lines, arrays)["counts_valid"] is not True:
        raise ValueError("Requires complete consistent native grid counts")
    return int(flags[1:], 16)


def _apply_block(lines, load, newline):
    """Insert or replace the block, keeping every other byte; one root anchor is always required."""
    found = flat_load(lines)
    anchors = [index for index, line in enumerate(lines) if line.strip() == ANCHOR]
    if len(anchors) != 1:
        raise ValueError("Expected one root SimulPlots=Start anchor")
    if found is None:
        block = native_load_block(load, newline, blank_before=True).splitlines(keepends=True)
        candidate = lines[:anchors[0]] + block + lines[anchors[0]:]
    else:
        block = native_load_block(load, newline).splitlines(keepends=True)
        candidate = lines[:found[0]] + block + lines[found[1] + 1:]
    index, _ = _root_block(candidate).one("Flags")
    candidate[index] = replace_value(candidate[index], "$00080000")
    return candidate


def create_native_load_variant(store, *, project, source_variant, new_variant, load,
                               dry_run=True, variant_name=None, add_utf8_bom=False):
    """Preview or create a NEW VC only; the source and every other field stay untouched."""
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run, "issues": [],
              "warnings": [], "artifacts": {}, "variant_created": False,
              "native_consumption_verified": False}
    with store.lock:
        try:
            if type(dry_run) is not bool or type(add_utf8_bom) is not bool:
                raise ValueError("Use strict booleans")
            store._check_project(project)
            source, target = store._path(project, source_variant), store._path(project, new_variant)
            if any(path.name.casefold() == target.name.casefold() for path in target.parent.iterdir()):
                raise FileExistsError("Choose a new variant: " + target.name)
            original = store._read(source)
            text, lines = store._text(original)
            arrays, system = store._sections(lines)
            root_flags = _require_scope(store, lines, arrays, system)
            if bool(root_flags & 0x00080000) != bool(flat_load(lines)):
                raise ValueError("Root bit19 and the existing load block disagree; refusing to guess")
            newline = "\r\n" if "\r\n" in text else "\n"
            candidate_lines = _apply_block(list(lines), load, newline)
            bom_prefix = BOM if original.startswith(BOM) or add_utf8_bom else b""
            name_change = None
            if variant_name is not None:
                store._validate_display_name(variant_name, "variant_name")
                bom_prefix = store._display_name_prefix(original, variant_name, add_utf8_bom, "variant_name")
                index, = store._display_name_fields(candidate_lines, complete=True)
                before = candidate_lines[index].partition("=")[2].strip()
                candidate_lines[index] = replace_value(candidate_lines[index], variant_name)
                name_change = {"before": before, "after": variant_name}
            candidate_text = "".join(candidate_lines)
            candidate = bom_prefix + candidate_text.encode("utf-8")
            _, checked = store._text(candidate)
            checked_arrays, _ = store._sections(checked)
            verified = flat_load(checked)
            if verified is None or len(checked_arrays) != 1:
                raise ValueError("Candidate lost its load block or subarray")
            result.update(status="ready", project=project, source_variant=source_variant,
                          variant=target.suffix[1:].upper(), load_mode=verified[2],
                          artifacts={"variant": str(target)},
                          bom_added=bool(bom_prefix) and not original.startswith(BOM))
            if name_change is not None:
                result["name_change"] = name_change
            result["warnings"].append(
                "Only one new VC is written: root bit19 and the owning pvLoad are set, optional root Comment changes, "
                "and pvSystem/PRJ/weather/topology/other loads stay inherited. Saving is not native consumption.")
            result["warnings"].append(
                "Supported subset is native 8.1.6 PV-only single-subarray Grid without storage, scene, module layout, "
                "saved horizon or P50/P90. Probability, HouseHold and HourlyFile blocks stop instead of being replaced.")
            diff = "".join(difflib.unified_diff(text.splitlines(keepends=True),
                                               candidate_text.splitlines(keepends=True)))
            result.update(diff=diff[:32000], diff_truncated=len(diff) > 32000)
            if store._read(source) != original:
                raise ValueError("Template changed while preparing the load variant")
            if dry_run:
                return result
            result["stage"] = "write"
            with target.open("xb") as stream:
                stream.write(candidate)
            result.update(status="created", stage="created", variant_created=True)
        except (ValueError, OSError, UnicodeError, KeyError, TypeError) as exc:
            result["issues"].append(str(exc))
            result["status"] = "failed" if result["stage"] == "write" else "blocked"
            if result["status"] == "failed":
                result["warnings"].append(
                    "The new variant may contain a partial artifact; inspect the disclosed path before retrying. "
                    "Original template is retained.")
    return result

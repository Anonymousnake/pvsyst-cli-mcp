"""Native VC editors for flat/household load, grid limiting, horizon, and P50/P90 blocks."""
from __future__ import annotations

import calendar
import random
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pvsyst_auto.components.editor import (
    ASSIGNMENT,
    direct_fields,
    finite,
    objects,
    one,
    read_flat_grid_system,
    replace_value,
    root_block,
)
from pvsyst_auto.native.headless import strip_result_cache
from pvsyst_auto.project.circuit import inventory as grid_inventory
from pvsyst_auto.versions import NATIVE_TEXT, NATIVE_TEXT_OR_SEED, SEED_LABEL, TEXT_LABEL

# --------------------------------------------------------------------- flat / household load
START = "PVObject_UserNeeds=pvLoad"
END = "End of PVObject pvLoad"
ANCHOR = "SimulPlots=Start"
LIMIT = 1e6
STEP = Decimal("0.001")
COMMENTS = {
    "constant": "MCP native constant load", "monthly": "MCP native monthly load",
    "daily": "MCP native daily profile load", "household": "MCP native household appliances load",
    "probability": "MCP native probability distribution load",
}
ALLOWED = {
    "Constant": ("Comment", "Flags", "LoadType", "Energy Units", "ConstantLoad"),
    "Monthly": ("Comment", "Flags", "LoadType", "UsesModul", "Energy Units", "ValMonth"),
    "DailyProfile": ("Comment", "Flags", "LoadType", "UsesModul", "Energy Units", "NHourlyValues", "ValHour_1"),
    "Probability": ("Comment", "Flags", "LoadType", "UsesModul", "Energy Units", "PowerStep", "NHourlyValues", "ValHour_1"),
}
REQUIRED = {
    "Constant": ("Flags", "LoadType", "Energy Units", "ConstantLoad"),
    "Monthly": ("Flags", "LoadType", "UsesModul", "Energy Units", "ValMonth"),
    "DailyProfile": ("Flags", "LoadType", "UsesModul", "Energy Units", "NHourlyValues", "ValHour_1"),
    "Probability": ("Flags", "LoadType", "UsesModul", "Energy Units", "PowerStep", "NHourlyValues", "ValHour_1"),
}
EXPECTED = {
    "Constant": {"Flags": "$00", "LoadType": "Constant", "Energy Units": "kW"},
    "Monthly": {"Flags": "$01", "LoadType": "Monthly", "UsesModul": "Yearly", "Energy Units": "kW"},
    "DailyProfile": {"Flags": "$02", "LoadType": "DailyProfile", "UsesModul": "Yearly", "Energy Units": "kW", "NHourlyValues": "1"},
    "Probability": {"Flags": "$02", "LoadType": "Probability", "UsesModul": "Yearly", "Energy Units": "kW", "NHourlyValues": "1"},
}
SERIES = {"Monthly": ("ValMonth", 12, 3, False), "DailyProfile": ("ValHour_1", 24, 1, True), "Probability": ("ValHour_1", 24, 1, True)}
HOUSEHOLD_SEASONS = ("Summer", "Autumn", "Winter", "Spring")

def _decimal(text, label, maximum=LIMIT):
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a decimal number") from None
    if not value.is_finite():
        raise ValueError(f"{label} must be finite")
    if value < 0 or value > Decimal(str(maximum)):
        raise ValueError(f"{label} must be within 0..{_plain(maximum)}")
    if value != value.quantize(STEP):
        raise ValueError(f"{label} must use at most 0.001 steps")
    return value

def _tail(text, label):
    """Trailing native slot holds an exact sum of declared inputs (uncapped per entry)."""
    return _decimal(text, label, LIMIT * 24)

def _number(value, label, positive=False):
    number = finite(value, label, 0, LIMIT, positive)
    _decimal(str(value), label)
    return 0.0 if number == 0 else number

def _plain(value):
    """Shortest exact text for one 0.001-step value without %g rounding."""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text

def _encode_appliance_masks(item: dict, idx: int) -> tuple[int, int]:
    """Encode hours [0..23] or half_hours [0..47] into two 24-bit PVsyst HoursDefined bitmasks."""
    hours, half_hours = item.get("hours"), item.get("half_hours")
    if (hours is None) == (half_hours is None):
        raise ValueError(f"appliance[{idx}] must specify exactly one of hours (0..23) or half_hours (0..47)")
    m0 = m1 = 0
    if hours is not None:
        if not isinstance(hours, list) or not hours:
            raise ValueError(f"appliance[{idx}].hours must be a non-empty list of integers in 0..23")
        for h in hours:
            if type(h) is not int or not 0 <= h <= 23:
                raise ValueError(f"appliance[{idx}].hours values must be integers in 0..23")
            m0 |= 1 << h
            m1 |= 1 << h
    else:
        if not isinstance(half_hours, list) or not half_hours:
            raise ValueError(f"appliance[{idx}].half_hours must be a non-empty list of integers in 0..47")
        for s in half_hours:
            if type(s) is not int or not 0 <= s <= 47:
                raise ValueError(f"appliance[{idx}].half_hours values must be integers in 0..47")
            if s % 2 == 0:
                m0 |= 1 << (s // 2)
            else:
                m1 |= 1 << (s // 2)
    return m0, m1

def _render_appliance_period(period_name: str, appliances: list[dict]) -> list[str]:
    if not isinstance(appliances, list) or not 1 <= len(appliances) <= 10:
        raise ValueError(f"household appliances for {period_name} must contain 1..10 items")
    lines, used_slots = [f"        Appliances for period={period_name}"], set()
    for idx, app in enumerate(appliances, start=1):
        if not isinstance(app, dict):
            raise ValueError(f"appliance[{idx}] must be an object")
        name = str(app.get("name") or f"Appliance {idx}").replace("\r", "").replace("\n", "").strip()
        if not name:
            raise ValueError(f"appliance[{idx}].name must be non-empty")
        count = app.get("count", 1)
        if type(count) is not int or not 1 <= count <= 10000:
            raise ValueError(f"appliance[{idx}].count must be an integer in 1..10000")
        power_w = _number(app.get("power_w"), f"appliance[{idx}].power_w", positive=True)
        slot = idx if app.get("slot_index") is None else app.get("slot_index")
        if type(slot) is not int or not 1 <= slot <= 10 or slot in used_slots:
            raise ValueError(f"appliance[{idx}].slot_index must be a unique integer in 1..10")
        used_slots.add(slot)
        m0, m1 = _encode_appliance_masks(app, idx)
        lines.extend([
            f"          HouseAppliance, NoApp={slot}", f"            NApp={count}", f"            AppName={name}",
            "            NValperHour=2", f"            HoursDefined={m0},{m1},", f"            AppPower={power_w:.1f}",
            f"          End HouseAppliance no {slot}",
        ])
    return [*lines, "          NJourW_E=7", "        End Appliances"]

def native_load_block(load, newline, blank_before=False):
    """Serialize all verified native load forms (constant, monthly, daily, household, probability)."""
    if not isinstance(load, dict):
        raise ValueError("load must be one explicit mode object")
    mode = load.get("mode")
    if mode == "constant":
        if set(load) != {"mode", "constant_kw"}:
            raise ValueError("constant load accepts only mode and constant_kw")
        constant = _number(load["constant_kw"], "constant_kw", positive=True)
        data = ["    Flags=$00", "    LoadType=Constant", "    Energy Units=kW", "    ConstantLoad=" + _plain(constant)]
    elif mode == "monthly":
        if set(load) != {"mode", "monthly_energy_kwh"}:
            raise ValueError("monthly load accepts only mode and monthly_energy_kwh")
        raw = load["monthly_energy_kwh"]
        if not isinstance(raw, list) or len(raw) != 12:
            raise ValueError("monthly_energy_kwh must contain 12 monthly values")
        energies = [_number(v, f"monthly_energy_kwh[{i}]") for i, v in enumerate(raw)]
        if not any(v > 0 for v in energies):
            raise ValueError("monthly_energy_kwh requires at least one positive month")
        data = ["    Flags=$01", "    LoadType=Monthly", "    UsesModul=Yearly", "    Energy Units=kW",
                "    ValMonth=" + "".join(f"{v:.3f}," for v in energies) + "0.000,0.000,0.000,"]
    elif mode == "daily":
        if set(load) != {"mode", "daily_power_kw"}:
            raise ValueError("daily load accepts only mode and daily_power_kw")
        raw = load["daily_power_kw"]
        if not isinstance(raw, list) or len(raw) != 24:
            raise ValueError("daily_power_kw must contain 24 hourly values")
        powers = [_number(v, f"daily_power_kw[{i}]") for i, v in enumerate(raw)]
        if not any(v > 0 for v in powers):
            raise ValueError("daily_power_kw requires at least one positive hour")
        total = sum((Decimal(str(v)) for v in raw), Decimal(0))
        data = ["    Flags=$02", "    LoadType=DailyProfile", "    UsesModul=Yearly", "    Energy Units=kW",
                "    NHourlyValues=1", "    ValHour_1=" + "".join(f"{v:.3f}," for v in powers) + _plain(total) + ","]
    elif mode == "probability":
        if not set(load) <= {"mode", "power_step_kw", "probabilities_pct", "seed", "year"}:
            raise ValueError("probability load accepts only mode, power_step_kw, probabilities_pct, seed, and year")
        from pvsyst_auto.energy.profiles import hourly_load_block
        step_kw = _number(load.get("power_step_kw"), "power_step_kw", positive=True)
        probs_raw = load.get("probabilities_pct")
        if not isinstance(probs_raw, list) or not 2 <= len(probs_raw) <= 24:
            raise ValueError("probabilities_pct must contain 2..24 bin percentages")
        probs = [_number(v, f"probabilities_pct[{i}]") for i, v in enumerate(probs_raw)]
        prob_sum = sum((Decimal(str(v)) for v in probs_raw), Decimal(0))
        if abs(prob_sum - Decimal("100")) > Decimal("0.05"):
            raise ValueError(f"probabilities_pct must sum to 100.0% (got {prob_sum}%)")
        seed, year = load.get("seed", 42), load.get("year", 1990)
        if type(seed) is not int or seed < 0:
            raise ValueError("probability.seed must be a non-negative integer")
        if type(year) is not int or not 1969 <= year <= 2068:
            raise ValueError("probability.year must be an integer in 1969..2068")
        # Materialize as an embedded HourlyFile snapshot since native Grid ignores DCGrid-only LoadType=Probability.
        chosen = random.Random(seed).choices([(i + 0.5) * step_kw for i in range(len(probs))], weights=probs,
                                             k=24 * (366 if calendar.isleap(year) else 365))
        start_dt = datetime(year, 1, 1, 0, 0)
        values = [(start_dt + timedelta(hours=h), val) for h, val in enumerate(chosen)]
        block_text, _ = hourly_load_block(Path(f"probability_load_seed{seed}.csv"), values, year, newline)
        return (newline if blank_before else "") + block_text
    elif mode == "household":
        if not set(load) <= {"mode", "modulation", "appliances", "seasons"}:
            raise ValueError("household load accepts only mode, modulation, appliances, and seasons")
        modulation = load.get("modulation", "yearly")
        if modulation not in ("yearly", "seasonal"):
            raise ValueError("household.modulation must be 'yearly' or 'seasonal'")
        if modulation == "yearly":
            if load.get("seasons") is not None:
                raise ValueError("household.seasons requires modulation='seasonal'")
            periods = _render_appliance_period("Year", load.get("appliances"))
            mod_tok = "Yearly"
        else:
            seasons_map = load.get("seasons")
            if not isinstance(seasons_map, dict) or set(seasons_map) != set(HOUSEHOLD_SEASONS):
                raise ValueError(f"seasonal household load requires seasons dict with keys {HOUSEHOLD_SEASONS}")
            periods = [ln for s in HOUSEHOLD_SEASONS for ln in _render_appliance_period(s, seasons_map[s])]
            mod_tok = "Seasonal"
        data = ["    Flags=$0200", "    LoadType=HouseHold", f"    UsesModul={mod_tok}", "    Energy Units=kWh",
                "    HouseHoldUses=THouseHoldUses", f"      UsesModul={mod_tok}", *periods, "    End of HouseHoldUses"]
    else:
        raise ValueError("load.mode must be constant, monthly, daily, household, probability, or hourly")
    rendered = ([""] if blank_before else []) + ["  " + START, "    Comment=" + COMMENTS[mode], *data, "  " + END]
    return newline.join(rendered) + newline

def _assignments(lines, bounds):
    """Narrow whole-line parse preserving spaced key names like `Energy Units`."""
    fields = {}
    for index in range(bounds[0] + 1, bounds[1]):
        raw = lines[index].rstrip("\r\n")
        stripped = raw.strip()
        if not stripped:
            raise ValueError("Blank line inside the load block")
        if stripped.startswith(("PVObject_", "End of PVObject")):
            raise ValueError("Nested or duplicate load objects are not supported")
        key, sep, val = raw.partition("=")
        key = key.strip()
        if not sep or not key or key in fields:
            raise ValueError("Unsupported load assignment")
        fields[key] = val.strip()
    return fields

def _series(text, label, count, trailing):
    """Parse head values capped at 1e6 and separate trailing summary slots."""
    parts = text.split(",")
    if parts and not parts[-1].strip():
        parts.pop()
    if len(parts) != count + trailing or any(not p.strip() for p in parts):
        raise ValueError(f"{label} must contain {count + trailing} comma-separated values")
    return ([_decimal(p, f"{label}[{i}]") for i, p in enumerate(parts[:count])],
            [_tail(p, f"{label}[{count + i}]") for i, p in enumerate(parts[count:])])

def _verify_existing(mode, fields):
    if mode == "Constant":
        if not _decimal(fields["ConstantLoad"], "ConstantLoad") > 0:
            raise ValueError("Declared ConstantLoad must be greater than zero")
        return
    if mode == "Probability" and not _decimal(fields["PowerStep"], "PowerStep") > 0:
        raise ValueError("Declared PowerStep must be greater than zero")
    key, count, trailing, plus_sum = SERIES[mode]
    head, tail = _series(fields[key], key, count, trailing)
    if not any(v > 0 for v in head):
        raise ValueError(f"Declared {key} requires at least one positive value")
    if plus_sum:
        if sum(tail) != sum(head):
            raise ValueError(f"Declared {key} trailing slot must equal the sum of its {count} values")
    elif any(v != 0 for v in tail):
        raise ValueError(f"Declared {key} trailing slots must be zero")

def flat_load(lines):
    """Return the one directly owned native load block (flat or HouseHold/HourlyFile), or None."""
    loads = [b for b in objects("".join(lines)) if b.object_type == "pvLoad"]
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
    top_lines = lines[begin + 1:end]
    if any(ln.strip() == "LoadType=HouseHold" for ln in top_lines):
        if not (any(ln.strip() == "HouseHoldUses=THouseHoldUses" for ln in top_lines)
                and any(ln.strip() == "End of HouseHoldUses" for ln in top_lines)):
            raise ValueError("HouseHold pvLoad block is missing HouseHoldUses/End of HouseHoldUses")
        return begin, end, "HouseHold", {"LoadType": "HouseHold"}
    if any(ln.strip() == "LoadType=HourlyFile" for ln in top_lines):
        if not any(ln.strip() == "HourlyLoad=THourlyParams" for ln in top_lines):
            raise ValueError("HourlyFile pvLoad block is missing HourlyLoad=THourlyParams")
        return begin, end, "HourlyFile", {"LoadType": "HourlyFile"}
    fields = _assignments(lines, (begin, end))
    mode = fields.get("LoadType")
    if mode not in ALLOWED:
        raise ValueError(f"Unsupported or unknown LoadType: {mode!r}")
    if set(fields) - set(ALLOWED[mode]) or set(REQUIRED[mode]) - set(fields):
        raise ValueError("Unknown or missing fields in the load block")
    if any(fields.get(k) != v for k, v in EXPECTED[mode].items()):
        raise ValueError("Declared load fields do not match the verified native form")
    _verify_existing(mode, fields)
    return begin, end, mode, fields

def _root_header(lines):
    root_block(lines)
    header_end = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip().startswith("PVObject_")), len(lines))
    return direct_fields(lines, (0, header_end))

def _require_scope(store, lines, arrays, system):
    """Verify PV-only Grid variant (with or without 3D scene); disallow storage or P50."""
    header = _root_header(lines)
    if one(header, "Version")[1] not in NATIVE_TEXT_OR_SEED:
        raise ValueError(f"Requires a {SEED_LABEL} variant")
    flags = one(header, "Flags")[1]
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags):
        raise ValueError("Root Flags must be hexadecimal")
    if system is None or not arrays:
        raise ValueError("Requires one pvSystem and at least one subarray")
    system_root = root_block(lines, system)
    if not {"Flags", "SystemType"} <= set(system_root.fields):
        raise ValueError("pvSystem must declare Flags and SystemType")
    if system_root.one("SystemType")[1] != "Grid":
        raise ValueError("SystemType must be Grid")
    sys_flags = system_root.one("Flags")[1]
    if (not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", sys_flags)
            or (int(sys_flags[1:], 16) & ~0x00010000) not in (0, 0x04000000, 0x05000000, 0x06000000)):
        raise ValueError("Native load editing requires plain or standard Grid pvSystem Flags")
    main = store._main_array(lines)
    if main is None:
        raise ValueError("A pvMainArray section is required")
    main_root = root_block(lines, main)
    if main_root.one("Flags")[1] not in ("$00100400", "$01900400") or main_root.one("Flags2")[1] != "$082A":
        raise ValueError("Main array requires verified Flags and Flags2=$082A")
    for bounds in arrays.values():
        sub_root = root_block(lines, bounds)
        if sub_root.one("Version")[1] not in NATIVE_TEXT_OR_SEED:
            raise ValueError(f"Subarray requires a verified {SEED_LABEL} declaration")
        if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", sub_root.one("Flags")[1]):
            raise ValueError("Subarray Flags must be hexadecimal")
    if any(b.object_type == "pvGridStorage" for b in objects("".join(lines))):
        raise ValueError("Grid storage objects are outside this batch")
    if any(re.match(r"\s*P50_P90\s*=", ln) for ln in lines):
        raise ValueError("P50/P90 objects are outside this batch")
    if grid_inventory(lines, arrays)["counts_valid"] is not True:
        raise ValueError("Requires complete consistent native grid counts")
    return int(flags[1:], 16)

def _apply_block(lines, load, newline):
    """Insert or replace the load block and clear stale result cache."""
    found = flat_load(lines)
    anchors = [i for i, ln in enumerate(lines) if ln.strip() == ANCHOR]
    if len(anchors) != 1:
        raise ValueError("Expected one root SimulPlots=Start anchor")
    if found is None:
        candidate = lines[:anchors[0]] + native_load_block(load, newline, blank_before=True).splitlines(keepends=True) + lines[anchors[0]:]
    else:
        candidate = lines[:found[0]] + native_load_block(load, newline).splitlines(keepends=True) + lines[found[1] + 1:]
    index, raw = one(_root_header(candidate), "Flags")
    candidate[index] = replace_value(candidate[index], f"${int(raw[1:], 16) | 0x00080000:08X}")
    candidate, _ = strip_result_cache(candidate)
    return candidate

def create_native_load_variant(store, *, project, source_variant, new_variant, load,
                               dry_run=True, variant_name=None, add_utf8_bom=False):
    """Preview or create a NEW VC supporting constant/monthly/daily/household/probability and hourly manifests."""
    if isinstance(load, dict) and load.get("mode") == "hourly":
        if set(load) != {"mode", "load_manifest_name"}:
            raise ValueError("hourly load accepts only mode and load_manifest_name")
        manifest_name = load["load_manifest_name"]
        if not isinstance(manifest_name, str) or not manifest_name.strip():
            raise ValueError("load_manifest_name must be a non-empty manifest filename")
        from pvsyst_auto.energy.profiles import create_load_variant
        return create_load_variant(store, project=project, source_variant=source_variant, new_variant=new_variant,
                                   load_manifest_name=manifest_name, dry_run=dry_run,
                                   variant_name=variant_name, add_utf8_bom=add_utf8_bom)

    def build(*, text, lines, **_):
        arrays, system = store._sections(lines)
        if bool(_require_scope(store, lines, arrays, system) & 0x00080000) != bool(flat_load(lines)):
            raise ValueError("Root bit19 and the existing load block disagree; refusing to guess")
        candidate_lines = _apply_block(list(lines), load, "\r\n" if "\r\n" in text else "\n")

        def verify(checked, _, res):
            checked_arrays, _ = store._sections(checked)
            verified = flat_load(checked)
            if verified is None or not checked_arrays:
                raise ValueError("Candidate lost its load block or subarray")
            res["load_mode"] = verified[2]

        return {"candidate_lines": candidate_lines, "verify": verify}

    from pvsyst_auto.project.variant_updates import run_new_variant_transaction
    return run_new_variant_transaction(store, project=project, source_variant=source_variant, new_variant=new_variant,
                                       dry_run=dry_run, add_utf8_bom=add_utf8_bom, variant_name=variant_name,
                                       build=build, source_changed_error="Template changed while preparing the load variant")

# --------------------------------------------------------------------- grid injection limit
def _grid_limit_read(lines, system):
    fields, index, flags = read_flat_grid_system(
        lines, system, feature_label="Grid limiting", owned_fields=("PLimGrid", "LimGrSeparLoss"),
        load_storage_error="Load/storage grid limiting is outside the verified PV-only scope",
        unknown_field_error="PF or unverified native system fields require separate review",
        hex_flag_error="Require one hexadecimal owning system Flags",
        exclusive_system_fields=("PLimGrid", "LimGrSeparLoss"),
    )
    if flags & ~0x05000000:
        raise ValueError("PF, export permission or other native system modes are not verified here")
    limit = one(fields, "PLimGrid", False)
    try:
        value = finite(float(limit[1]), "PLimGrid", positive=True) if limit else None
    except (ValueError, OverflowError):
        raise ValueError("PLimGrid must be a positive finite native kW value") from None
    separate = one(fields, "LimGrSeparLoss", False)
    if separate and separate[1] not in ("True", "False"):
        raise ValueError("LimGrSeparLoss must be an explicit native boolean")
    enabled = bool(flags & 0x01000000)
    if enabled and value is None:
        raise ValueError("An active native grid limit requires PLimGrid")
    return fields, index, flags, {
        "mode": "constant" if enabled else "none", "limit_kw": value if enabled else None,
        "point": "injection" if flags & 0x04000000 else "inverter",
        "separate_loss": separate[1] == "True" if separate else None,
    }

def grid_limit_state(lines, system):
    try:
        return _grid_limit_read(lines, system)[3]
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}

def replace_grid_limit(lines, system, updates):
    """Edit grid-limit fields in place only after the edited system re-reads cleanly."""
    if not isinstance(updates, dict) or updates.get("mode") not in ("none", "constant"):
        raise ValueError("Require grid-limit mode none or constant")
    enabled = updates["mode"] == "constant"
    if set(updates) != ({"mode", "limit_kw"} if enabled else {"mode"}):
        raise ValueError("Provide only limit_kw for constant mode, and no value for none")
    if enabled:
        value = finite(updates["limit_kw"], "limit_kw", positive=True)
        if Decimal(str(value)) * 10 != (Decimal(str(value)) * 10).to_integral_value():
            raise ValueError("Use 0.1 kW increments, matching native GUI save precision")
    fields, index, flags, before = _grid_limit_read(lines, system)
    work, count = list(lines), len(lines)
    new_flags = flags | 0x05000000 if enabled else flags & ~0x05000000
    if new_flags != flags:
        work[index] = replace_value(work[index], f"${new_flags:08X}" if new_flags else "$00")
    if enabled:
        newline = "\r\n" if work[system[0]].endswith("\r\n") else "\n"
        indent = work[system[0]][:len(work[system[0]]) - len(work[system[0]].lstrip())] + "  "
        added = []
        for key, raw in (("PLimGrid", f"{value:.1f}"), ("LimGrSeparLoss", "True")):
            entry = one(fields, key, False)
            if entry:
                work[entry[0]] = replace_value(work[entry[0]], raw)
            else:
                added.append(f"{indent}{key}={raw}{newline}")
        work[system[1]:system[1]] = added
    else:
        for i in sorted((r[0] for k in ("PLimGrid", "LimGrSeparLoss") for r in fields.get(k, [])), reverse=True):
            del work[i]
    after = _grid_limit_read(work, (system[0], system[1] + len(work) - count))[3]
    lines[:] = work
    return {"before": before, "after": after}

# --------------------------------------------------------------------- root horizon
_HORIZON_ROOT = "PVObject_Horizon=pvHorizon"
_HORIZON_END = "End of PVObject pvHorizon"
_HORIZON_PROFILE = "HorizonLine=TCubicProfile"
_HORIZON_PROFILE_END = "End of TCubicProfile"
_HORIZON_MAIN = "PVObject_PVMainArray=pvMainArray"
_HORIZON_SLOTS = 100
_HORIZON_DERIVED = ("FHHDiffus", "FHPDiffus", "FHPAlbedo")
_HORIZON_OUTER = ("Comment", "Flags", "FracAlbedo") + _HORIZON_DERIVED
_HORIZON_INNER = ("NPtsMax", "NPtsEff", "LastCompile", "Mode")
_HORIZON_POINT = re.compile(r"Point_([0-9]{1,3})")
_HORIZON_LAST_COMPILE = "$81"

def _horizon_number(value, label, low, high, step=False):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} must be a finite number") from None
    if not low <= number <= high or (step and (Decimal(str(number)) * 10) % 1):
        raise ValueError(f"{label} must be a finite value inside {low}..{high}")
    return 0.0 if number == 0 else number

def _horizon_locate(lines):
    starts = [i for i, ln in enumerate(lines) if ln.strip() == _HORIZON_ROOT]
    if len(starts) > 1:
        raise ValueError("Horizon editing requires at most one root pvHorizon object")
    if not starts:
        return None
    end = next((i for i in range(starts[0] + 1, len(lines)) if lines[i].strip() == _HORIZON_END), None)
    if end is None:
        raise ValueError("Unclosed root pvHorizon object")
    return starts[0], end

def _horizon_read(lines):
    located = _horizon_locate(lines)
    if located is None:
        return None
    start, end = located
    opens = [i for i in range(start + 1, end) if lines[i].strip() == _HORIZON_PROFILE]
    if len(opens) != 1:
        raise ValueError("Horizon requires exactly one HorizonLine profile")
    body = opens[0]
    close = next((i for i in range(body + 1, end) if lines[i].strip() == _HORIZON_PROFILE_END), None)
    if close is None:
        raise ValueError("Unclosed horizon profile")
    outer, inner, points = {}, {}, {}
    for i in range(start + 1, end):
        if i in (body, close):
            continue
        if not lines[i].strip():
            raise ValueError("Blank lines inside the horizon block are not supported")
        match = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))
        if match is None:
            raise ValueError("Unknown horizon line")
        key = match[2]
        if slot := _HORIZON_POINT.fullmatch(key):
            if not body < i < close:
                raise ValueError("Horizon points must sit inside the profile")
            index = int(slot[1])
            if not 1 <= index <= _HORIZON_SLOTS or index in points:
                raise ValueError("Horizon point slots must be 1..100 without repeats")
            raw = match[4].split(",")
            if len(raw) != 2:
                raise ValueError("Horizon points must be an x,y pair")
            points[index] = (_horizon_number(raw[0], f"Point_{index} x", -180, 180, True),
                             _horizon_number(raw[1], f"Point_{index} y", 0, 90, True))
        elif body < i < close:
            if key not in _HORIZON_INNER or key in inner:
                raise ValueError("Unknown or duplicate horizon profile field")
            inner[key] = match[4]
        elif i > close:
            raise ValueError("Unknown field after the horizon profile")
        elif key not in _HORIZON_OUTER or key in outer:
            raise ValueError("Unknown or duplicate horizon field")
        else:
            outer[key] = match[4]
    if (len(points) != _HORIZON_SLOTS or set(inner) != set(_HORIZON_INNER) or set(outer) - set(_HORIZON_OUTER)
            or not {"Comment", "Flags", "FracAlbedo"} <= set(outer)):
        raise ValueError("Horizon requires the outer fields, profile scalars and 100 slots")
    if inner["NPtsMax"] != str(_HORIZON_SLOTS) or inner["Mode"] != "1":
        raise ValueError("Horizon editing requires NPtsMax=100 and Mode=1")
    if inner["LastCompile"] != _HORIZON_LAST_COMPILE:
        raise ValueError(f"Horizon editing requires the native LastCompile={_HORIZON_LAST_COMPILE}")
    _horizon_number(outer.get("FracAlbedo", ""), "FracAlbedo", 1, 1)
    derived = [k for k in _HORIZON_DERIVED if k in outer]
    if outer["Flags"] == "$01":
        if derived:
            raise ValueError("Flags=$01 must not carry derived horizon factors")
    elif outer["Flags"] == "$03":
        if len(derived) != len(_HORIZON_DERIVED):
            raise ValueError("Flags=$03 requires all three derived horizon factors")
        for k in derived:
            _horizon_number(outer[k], k, 0, 1)
    else:
        raise ValueError("Horizon editing requires native Flags=$01 or $03")
    try:
        effective = int(inner["NPtsEff"])
    except ValueError:
        raise ValueError("NPtsEff must be a native integer") from None
    if not 1 <= effective <= _HORIZON_SLOTS:
        raise ValueError("NPtsEff must be inside 1..100")
    for idx in range(effective + 1, _HORIZON_SLOTS + 1):
        if points[idx] != (0.0, 0.0):
            raise ValueError("Unused horizon slots must stay at the native 0.0,0.0 placeholder")
    real = [points[idx] for idx in range(1, effective + 1)]
    for prev_pt, curr_pt in zip(real, real[1:]):
        if curr_pt[0] <= prev_pt[0]:
            raise ValueError("Horizon point azimuths must increase strictly")
    return {"start": start, "end": end, "outer": outer, "inner": inner, "points": real}

def horizon_state(lines):
    """Return semantic horizon state; absent blocks report mode 'none'."""
    try:
        state = _horizon_read(lines)
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}
    if state is None:
        return {"mode": "none", "profile_name": None, "points": []}
    return {
        "mode": "profile", "profile_name": state["outer"]["Comment"] or None,
        "points": [[x, y] for x, y in state["points"]],
        "native_fields": {"flags": state["outer"]["Flags"],
                          "derived": {k: _horizon_number(state["outer"][k], k, 0, 1)
                                      for k in _HORIZON_DERIVED if k in state["outer"]}},
    }

def _horizon_check_points(points):
    if not isinstance(points, list) or not 4 <= len(points) <= _HORIZON_SLOTS:
        raise ValueError("Horizon profiles require 4 to 100 points")
    decoded = []
    for entry in points:
        if (not isinstance(entry, list) or len(entry) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in entry)):
            raise ValueError("Each horizon point must be a strict numeric [x, y] pair")
        decoded.append((_horizon_number(entry[0], "point azimuth", -180, 180, True),
                        _horizon_number(entry[1], "point height", 0, 90, True)))
    if not any(p[1] > 0 for p in decoded):
        raise ValueError("Use horizon mode none for an all-zero profile")
    if len(set(decoded)) != len(decoded):
        raise ValueError("Horizon points must not repeat")
    for prev_pt, curr_pt in zip(decoded, decoded[1:]):
        if curr_pt[0] <= prev_pt[0]:
            raise ValueError("Horizon point azimuths must increase strictly")
    return decoded

def _horizon_check_name(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("profile_name must be a non-empty trimmed string")
    if len(value.encode("utf-8")) > 80 or not value.isprintable():
        raise ValueError("profile_name must be 1..80 printable UTF-8 bytes")
    return value

def _horizon_body(indent, name, points, ending):
    lines = [f"{indent}{_HORIZON_ROOT}{ending}", f"{indent}  Comment={name}{ending}",
             f"{indent}  Flags=$01{ending}", f"{indent}  FracAlbedo=1.000{ending}",
             f"{indent}  {_HORIZON_PROFILE}{ending}", f"{indent}    NPtsMax={_HORIZON_SLOTS}{ending}",
             f"{indent}    NPtsEff={len(points)}{ending}", f"{indent}    LastCompile={_HORIZON_LAST_COMPILE}{ending}",
             f"{indent}    Mode=1{ending}"]
    for i in range(1, _HORIZON_SLOTS + 1):
        x, y = points[i - 1] if i <= len(points) else (0.0, 0.0)
        lines.append(f"{indent}    Point_{i}={x:.1f},{y:.1f}{ending}")
    return [*lines, f"{indent}  {_HORIZON_PROFILE_END}{ending}", f"{indent}{_HORIZON_END}{ending}"]

def replace_horizon(lines, updates):
    """Write the root horizon block in place matching the verified GUI template."""
    if not isinstance(updates, dict) or updates.get("mode") not in ("none", "profile"):
        raise ValueError("Require horizon mode none or profile")
    mode = updates["mode"]
    if set(updates) != ({"mode"} if mode == "none" else {"mode", "points", "profile_name"}):
        raise ValueError("Provide only the values required by the chosen horizon mode")
    points = [] if mode == "none" else _horizon_check_points(updates["points"])
    name = None if mode == "none" else _horizon_check_name(updates["profile_name"])
    current = horizon_state(lines)
    if current["mode"] is None:
        raise ValueError(current["issue"])
    before = {k: current[k] for k in ("mode", "profile_name", "points")}
    located = _horizon_locate(lines)
    if mode == "none":
        if located is None:
            return {"before": before, "after": before}
        start, end = located
        stop = end + 1 if end + 1 < len(lines) and not lines[end + 1].strip() else end
        del lines[start:stop + 1]
        after = {"mode": "none", "profile_name": None, "points": []}
    elif before["mode"] == "profile" and before["profile_name"] == name and before["points"] == [list(p) for p in points]:
        return {"before": before, "after": before}
    elif before["mode"] == "profile" and before["points"] == [list(p) for p in points]:
        start, end = located
        index = next(i for i in range(start + 1, end)
                     if (m := ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))) and m[2] == "Comment")
        lines[index] = replace_value(lines[index], name)
        after = {**before, "profile_name": name}
    else:
        ending = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
        if located is None:
            mains = [i for i, ln in enumerate(lines) if ln.strip() == _HORIZON_MAIN]
            if len(mains) != 1:
                raise ValueError("Horizon insertion requires exactly one pvMainArray object")
            indent = re.match(r"[ \t]*", lines[mains[0]])[0]
            lines[mains[0]:mains[0]] = _horizon_body(indent, name, points, ending) + [ending]
        else:
            indent = re.match(r"[ \t]*", lines[located[0]])[0]
            lines[located[0]:located[1] + 1] = _horizon_body(indent, name, points, ending)
        after = {"mode": "profile", "profile_name": name, "points": [[x, y] for x, y in points]}
    return {"before": before, "after": after}

# --------------------------------------------------------------------- P50 / P90 editor
_P50_HEAD = "P50_P90=TP50_P90"
_P50_END = "EndTags"
_P50_PLOTS = "SimulPlots=Start"
_P50_ROOT_TYPE = "pvVCalcul"
_P50_ROOT_FLAGS = 0x01800001
_P50_READ_FLAGS = (0x01800001, 0x01000000, 0x00800001, 0x00000000)
_P50_ORDER = ("MeteoKind", "P90Custom", "PxxCustom", "PxxReport", "PxxMainResults", "PlotKind",
              "YearDevFromAver", "MeteoAnnualVariab", "PVModelVariab", "InverterVariab",
              "SoilingVariab", "DegradVariab", "CustomVariab", "CustomVarLabel")
_P50_INTS = ("P90Custom", "PxxCustom")
_P50_BOOLS = ("PxxReport", "PxxMainResults")
_P50_VARIAB = ("MeteoAnnualVariab", "PVModelVariab", "InverterVariab", "SoilingVariab", "DegradVariab", "CustomVariab")
_P50_PLOT_KIND = 1

def _p50_step(value, label, low, high):
    number = finite(value, label, minimum=low, maximum=high)
    scaled = Decimal(str(number)) * 1000
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{label} must use 0.001 increments")
    return 0.0 if number == 0 else number

def _p50_label(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("CustomVarLabel must be a trimmed single-line string")
    if not value.isascii() or not value.isprintable() or len(value.encode("ascii")) > 80:
        raise ValueError("CustomVarLabel must be 1..80 printable ASCII bytes")
    return value

def _p50_validate(values):
    if not isinstance(values, dict) or set(values) != set(_P50_ORDER):
        raise ValueError("Require exactly the fourteen native P50 keys")
    out = {"MeteoKind": values["MeteoKind"]}
    if out["MeteoKind"] not in ("SpecYear", "TMY"):
        raise ValueError("MeteoKind must be SpecYear or TMY")
    for key in _P50_INTS:
        if type(values[key]) is not int:
            raise ValueError(f"{key} must be a strict integer")
        if not 51 <= values[key] <= 99:
            raise ValueError(f"{key} must stay inside 51..99")
        out[key] = values[key]
    if not 51 <= out["P90Custom"] < out["PxxCustom"] <= 99:
        raise ValueError("Require 51 <= P90Custom < PxxCustom <= 99")
    for key in _P50_BOOLS:
        if type(values[key]) is not bool:
            raise ValueError(f"{key} must be a strict boolean")
        out[key] = values[key]
    if type(values["PlotKind"]) is not int or values["PlotKind"] != _P50_PLOT_KIND:
        raise ValueError(f"PlotKind must be the integer {_P50_PLOT_KIND}")
    out["PlotKind"] = _P50_PLOT_KIND
    out["YearDevFromAver"] = _p50_step(values["YearDevFromAver"], "YearDevFromAver", -100, 100)
    for key in _P50_VARIAB:
        out[key] = _p50_step(values[key], key, 0, 100)
    out["CustomVarLabel"] = _p50_label(values["CustomVarLabel"])
    return out

def _p50_read(lines, require_template=False):
    heads = [i for i, ln in enumerate(lines) if ln.strip() == _P50_HEAD]
    if len(heads) > 1:
        raise ValueError("Require at most one root P50_P90 block")
    if not heads:
        return None
    head = heads[0]
    tail = next((i for i in range(head + 1, len(lines)) if lines[i].strip() == _P50_END), None)
    if tail is None:
        raise ValueError("Unclosed P50_P90 block")
    plots = [i for i, ln in enumerate(lines) if ln.strip() == _P50_PLOTS]
    if len(plots) != 1 or head > plots[0] or tail > plots[0]:
        raise ValueError("Require one SimulPlots marker after the whole P50_P90 block")
    eol = "\r\n" if lines[head].endswith("\r\n") else "\n"
    try:
        blocks = objects("".join(lines[:head]) + f"End of PVObject {_P50_ROOT_TYPE}{eol}")
    except ValueError:
        raise ValueError("Root object prefix is unbalanced") from None
    roots = [b for b in blocks if not b.path]
    if len(roots) != 1 or roots[0].object_type != _P50_ROOT_TYPE:
        raise ValueError(f"Require a single {_P50_ROOT_TYPE} root object above P50_P90")
    if roots[0].one("Version")[1] not in NATIVE_TEXT:
        raise ValueError(f"P50 requires root Version={TEXT_LABEL}")
    raw_flags = roots[0].one("Flags")[1]
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw_flags):
        raise ValueError("P50 root Flags must be a hexadecimal token")
    state = int(raw_flags[1:], 16)
    if require_template and state != _P50_ROOT_FLAGS:
        raise ValueError("P50 editing requires the observed result-template root Flags $01800001")
    if state not in _P50_READ_FLAGS:
        raise ValueError("P50 root Flags is not one of the recorded native states")
    fields = {}
    for i in range(head + 1, tail):
        if lines[i].strip().startswith(("PVObject_", "End of ", _P50_HEAD)):
            raise ValueError("P50_P90 must stay a flat tag block")
        match = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))
        if match is None or match[2] not in _P50_ORDER or match[2] in fields:
            raise ValueError("Unknown or duplicate P50 field inside the block")
        fields[match[2]] = (i, match[4])
    if set(fields) != set(_P50_ORDER):
        raise ValueError("P50_P90 requires all fourteen native fields")
    decoded = {}
    for key in _P50_ORDER:
        raw = fields[key][1]
        if key in _P50_BOOLS:
            if raw not in ("True", "False"):
                raise ValueError(f"{key} must be native True or False")
            decoded[key] = raw == "True"
        elif key in _P50_INTS or key == "PlotKind":
            if not re.fullmatch(r"-?[0-9]+", raw):
                raise ValueError(f"{key} must be a native integer")
            decoded[key] = int(raw)
        elif key in ("MeteoKind", "CustomVarLabel"):
            decoded[key] = raw
        else:
            try:
                decoded[key] = float(raw)
            except (ValueError, OverflowError):
                raise ValueError(f"{key} must be a native number") from None
    return fields, _p50_validate(decoded)

def p50_state(lines):
    """Return native P50 state; absent blocks report 'none', invalid ones report an issue."""
    try:
        got = _p50_read(lines)
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}
    return {"mode": "none", "values": None} if got is None else {"mode": "configured", "values": got[1]}

def replace_p50(lines, updates):
    """Replace the fourteen existing native P50 values in place."""
    target = _p50_validate(updates)
    got = _p50_read(lines, require_template=True)
    if got is None:
        raise ValueError("Require an existing root P50_P90 block; this editor never creates one")
    fields, before = got
    if before == target:
        return {"before": before, "after": before}
    for key, (index, raw) in fields.items():
        val = target[key]
        rendered = ("True" if val is True else "False") if key in _P50_BOOLS else (
            format(val, ".3f") if key in _P50_VARIAB or key == "YearDevFromAver" else str(val))
        if rendered != raw:
            lines[index] = replace_value(lines[index], rendered)
    return {"before": before, "after": target}

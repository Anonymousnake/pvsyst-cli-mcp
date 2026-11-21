"""Create a NEW VC with one verified native flat load block; no load model or cache."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from pvsyst_auto.components.editor import objects, replace_value, root_block
from pvsyst_auto.components.generator import direct_fields, finite, one
from pvsyst_auto.native.headless import strip_result_cache
from pvsyst_auto.project.circuit import inventory as grid_inventory
from pvsyst_auto.project.variant_writer import run_new_variant_transaction
from pvsyst_auto.versions import NATIVE_TEXT_OR_SEED, SEED_LABEL

START = "PVObject_UserNeeds=pvLoad"
END = "End of PVObject pvLoad"
ANCHOR = "SimulPlots=Start"
LIMIT = 1e6
STEP = Decimal("0.001")
COMMENTS = {"constant": "MCP native constant load", "monthly": "MCP native monthly load",
            "daily": "MCP native daily profile load",
            "household": "MCP native household appliances load",
            "probability": "MCP native probability distribution load"}
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
# Whole-line expectations, so `Energy Units` (with its space) is verified rather than guessed.
EXPECTED = {
    "Constant": {"Flags": "$00", "LoadType": "Constant", "Energy Units": "kW"},
    "Monthly": {"Flags": "$01", "LoadType": "Monthly", "UsesModul": "Yearly", "Energy Units": "kW"},
    "DailyProfile": {"Flags": "$02", "LoadType": "DailyProfile", "UsesModul": "Yearly",
                     "Energy Units": "kW", "NHourlyValues": "1"},
    "Probability": {"Flags": "$02", "LoadType": "Probability", "UsesModul": "Yearly",
                    "Energy Units": "kW", "NHourlyValues": "1"},
}
# mode -> (declared values, trailing slots, trailing slot equals their sum)
SERIES = {
    "Monthly": ("ValMonth", 12, 3, False),
    "DailyProfile": ("ValHour_1", 24, 1, True),
    "Probability": ("ValHour_1", 24, 1, True),
}
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
    """Trailing native slot is an exact sum of declared inputs, so it is not capped per value."""
    return _decimal(text, label, LIMIT * 24)


def _number(value, label, positive=False):
    number = finite(value, label, 0, LIMIT, positive)
    _decimal(str(value), label)
    return 0.0 if number == 0 else number


def _plain(value):
    """Shortest exact text for one 0.001-step value; never rounds through %g."""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _encode_appliance_masks(item: dict, idx: int) -> tuple[int, int]:
    """Encode hours [0..23] or half_hours [0..47] into two 24-bit PVsyst HoursDefined bitmasks."""
    hours = item.get("hours")
    half_hours = item.get("half_hours")
    if (hours is None) == (half_hours is None):
        raise ValueError(f"appliance[{idx}] must specify exactly one of hours (0..23) or half_hours (0..47)")
    m0 = 0
    m1 = 0
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
    lines = [f"        Appliances for period={period_name}"]
    used_slots: set[int] = set()
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
        slot = app.get("slot_index", idx)
        if type(slot) is not int or not 1 <= slot <= 10 or slot in used_slots:
            raise ValueError(f"appliance[{idx}].slot_index must be a unique integer in 1..10")
        used_slots.add(slot)
        m0, m1 = _encode_appliance_masks(app, idx)
        lines.extend([
            f"          HouseAppliance, NoApp={slot}",
            f"            NApp={count}",
            f"            AppName={name}",
            "            NValperHour=2",
            f"            HoursDefined={m0},{m1},",
            f"            AppPower={power_w:.1f}",
            f"          End HouseAppliance no {slot}",
        ])
    lines.extend(["          NJourW_E=7", "        End Appliances"])
    return lines


def native_load_block(load, newline, blank_before=False):
    """Serialize all verified native load forms (constant, monthly, daily, household, probability)."""
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
    elif mode == "probability":
        if not set(load) <= {"mode", "power_step_kw", "probabilities_pct", "seed", "year"}:
            raise ValueError("probability load accepts only mode, power_step_kw, probabilities_pct, seed, and year")
        import calendar
        from datetime import datetime, timedelta
        from pathlib import Path
        import random
        from pvsyst_auto.energy.load import hourly_load_block

        step_kw = _number(load.get("power_step_kw"), "power_step_kw", positive=True)
        probs_raw = load.get("probabilities_pct")
        if not isinstance(probs_raw, list) or not 2 <= len(probs_raw) <= 24:
            raise ValueError("probabilities_pct must contain 2..24 bin percentages")
        probs = [_number(v, f"probabilities_pct[{i}]") for i, v in enumerate(probs_raw)]
        prob_sum = sum((Decimal(str(v)) for v in probs_raw), Decimal(0))
        if abs(prob_sum - Decimal("100")) > Decimal("0.05"):
            raise ValueError(f"probabilities_pct must sum to 100.0% (got {prob_sum}%)")
        seed = load.get("seed", 42)
        if type(seed) is not int or seed < 0:
            raise ValueError("probability.seed must be a non-negative integer")
        year = load.get("year", 1990)
        if type(year) is not int or not 1969 <= year <= 2068:
            raise ValueError("probability.year must be an integer in 1969..2068")
        # PVsyst Grid systems ignore LoadType=Probability natively (it is DCGrid-only),
        # so deterministically sample bin midpoints (i + 0.5) * power_step_kw into a native
        # embedded HourlyFile snapshot so PVsystCLI 8.1.7 consumes every hour faithfully.
        rng = random.Random(seed)
        bin_midpoints = [(i + 0.5) * step_kw for i in range(len(probs))]
        n_hours = 24 * (366 if calendar.isleap(year) else 365)
        start_dt = datetime(year, 1, 1, 0, 0)
        chosen = rng.choices(bin_midpoints, weights=probs, k=n_hours)
        values = [(start_dt + timedelta(hours=h), val) for h, val in enumerate(chosen)]
        block_text, _ = hourly_load_block(Path(f"probability_load_seed{seed}.csv"), values, year, newline)
        return (newline if blank_before else "") + block_text
    elif mode == "household":
        if not set(load) <= {"mode", "modulation", "appliances", "seasons"}:
            raise ValueError("household load accepts only mode, modulation, appliances, and seasons")
        modulation = load.get("modulation", "yearly")
        if modulation not in ("yearly", "seasonal"):
            raise ValueError("household.modulation must be 'yearly' or 'seasonal'")
        hh_lines = ["    Flags=$0200", "    LoadType=HouseHold"]
        if modulation == "yearly":
            if load.get("seasons") is not None:
                raise ValueError("household.seasons requires modulation='seasonal'")
            hh_lines.extend([
                "    UsesModul=Yearly",
                "    Energy Units=kWh",
                "    HouseHoldUses=THouseHoldUses",
                "      UsesModul=Yearly",
            ])
            hh_lines.extend(_render_appliance_period("Year", load.get("appliances")))
            hh_lines.append("    End of HouseHoldUses")
        else:
            seasons_map = load.get("seasons")
            if not isinstance(seasons_map, dict) or set(seasons_map) != set(HOUSEHOLD_SEASONS):
                raise ValueError(f"seasonal household load requires seasons dict with keys {HOUSEHOLD_SEASONS}")
            hh_lines.extend([
                "    UsesModul=Seasonal",
                "    Energy Units=kWh",
                "    HouseHoldUses=THouseHoldUses",
                "      UsesModul=Seasonal",
            ])
            for sname in HOUSEHOLD_SEASONS:
                hh_lines.extend(_render_appliance_period(sname, seasons_map[sname]))
            hh_lines.append("    End of HouseHoldUses")
        data = hh_lines
    else:
        raise ValueError("load.mode must be constant, monthly, daily, household, probability, or hourly")
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
    if mode == "Probability":
        if not _decimal(fields["PowerStep"], "PowerStep") > 0:
            raise ValueError("Declared PowerStep must be greater than zero")
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
    """Return the one directly owned native load block (flat or HouseHold), or None."""
    loads = [block for block in objects("".join(lines)) if block.object_type == "pvLoad"]
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
    # Recognize structured HouseHold or embedded HourlyFile (e.g. materialized Probability) block cleanly
    top_lines = lines[begin + 1:end]
    if any(ln.strip() == "LoadType=HouseHold" for ln in top_lines):
        if not any(ln.strip() == "HouseHoldUses=THouseHoldUses" for ln in top_lines) or not any(
            ln.strip() == "End of HouseHoldUses" for ln in top_lines
        ):
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
    if any(fields.get(key) != value for key, value in EXPECTED[mode].items()):
        raise ValueError("Declared load fields do not match the verified native form")
    _verify_existing(mode, fields)
    return begin, end, mode, fields


def _root_header(lines):
    root_block(lines)
    header_end = next((i for i, line in enumerate(lines[1:], 1) if line.strip().startswith("PVObject_")), len(lines))
    return direct_fields(lines, (0, header_end))


def _require_scope(store, lines, arrays, system):
    """Verified PV-only Grid variant (with or without 3D scene); no storage or P50."""
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
        sflags = sub_root.one("Flags")[1]
        if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", sflags):
            raise ValueError("Subarray Flags must be hexadecimal")
    if any(block.object_type == "pvGridStorage" for block in objects("".join(lines))):
        raise ValueError("Grid storage objects are outside this batch")
    if any(re.match(r"\s*P50_P90\s*=", line) for line in lines):
        raise ValueError("P50/P90 objects are outside this batch")
    if grid_inventory(lines, arrays)["counts_valid"] is not True:
        raise ValueError("Requires complete consistent native grid counts")
    return int(flags[1:], 16)


def _apply_block(lines, load, newline):
    """Insert or replace the block, keeping 3D scenes and clearing stale result cache."""
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
    index, raw = one(_root_header(candidate), "Flags")
    flags_val = int(raw[1:], 16) | 0x00080000
    candidate[index] = replace_value(candidate[index], f"${flags_val:08X}")
    candidate, _ = strip_result_cache(candidate)
    return candidate


def create_native_load_variant(store, *, project, source_variant, new_variant, load,
                               dry_run=True, variant_name=None, add_utf8_bom=False):
    """Preview or create a NEW VC only; handles constant/monthly/daily native blocks and hourly manifests."""
    if isinstance(load, dict) and load.get("mode") == "hourly":
        if set(load) != {"mode", "load_manifest_name"}:
            raise ValueError("hourly load accepts only mode and load_manifest_name")
        manifest_name = load["load_manifest_name"]
        if not isinstance(manifest_name, str) or not manifest_name.strip():
            raise ValueError("load_manifest_name must be a non-empty manifest filename")
        from pvsyst_auto.energy.load import create_load_variant
        return create_load_variant(
            store, project=project, source_variant=source_variant, new_variant=new_variant,
            load_manifest_name=manifest_name, dry_run=dry_run,
            variant_name=variant_name, add_utf8_bom=add_utf8_bom,
        )

    def build(*, text, lines, **_):
        arrays, system = store._sections(lines)
        root_flags = _require_scope(store, lines, arrays, system)
        if bool(root_flags & 0x00080000) != bool(flat_load(lines)):
            raise ValueError("Root bit19 and the existing load block disagree; refusing to guess")
        newline = "\r\n" if "\r\n" in text else "\n"
        candidate_lines = _apply_block(list(lines), load, newline)

        def verify(checked, _, res):
            checked_arrays, _ = store._sections(checked)
            verified = flat_load(checked)
            if verified is None or not checked_arrays:
                raise ValueError("Candidate lost its load block or subarray")
            res["load_mode"] = verified[2]

        return {"candidate_lines": candidate_lines, "verify": verify}

    return run_new_variant_transaction(
        store,
        project=project,
        source_variant=source_variant,
        new_variant=new_variant,
        dry_run=dry_run,
        add_utf8_bom=add_utf8_bom,
        variant_name=variant_name,
        build=build,
        source_changed_error="Template changed while preparing the load variant",
    )

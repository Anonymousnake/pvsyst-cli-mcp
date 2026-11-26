"""Native AC-coupled grid storage fields, PeakShaving state inspection, and TOU schedule planner."""
from __future__ import annotations

import csv
import math
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from pvsyst_auto.components.editor import ASSIGNMENT, direct_fields, finite, objects, one, replace_value
from pvsyst_auto.energy.profiles import PEAK_SHAVING_LOAD_ERROR, has_peak_shaving_storage, native_availability
from pvsyst_auto.native.headless import strip_result_cache
from pvsyst_auto.project.variant_updates import run_new_variant_transaction
from pvsyst_auto.validation import pos_finite as _pos_finite
from pvsyst_auto.validation import NATIVE_TEXT_OR_SEED, SEED_LABEL

DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
SUBHOUR_TARGET_TOKEN = 90_000
# Conservative writer ceiling, verified by native consumption; not an asserted native limit.
SUBHOUR_SCALE_LIMIT = 1_000_000
SUBHOUR_TOKEN_LIMIT = 62**3
SUBHOUR_DECIMAL_PLACES = 6
ENABLED, APPARENT, INJECTION = 0x01000000, 0x02000000, 0x04000000

NUMERIC_FIELDS = {
    "soc_min": ("SOCDischDecl", 0, 1),
    "soc_max": ("SOCChDecl", 0, 1),
    "initial_soc": ("BattSoCInit", 0, 1),
    "reserve_soc": ("SOCDischSelf", 0, 1),
    "max_charge_ac_kw": ("PMaxCharge", 0, None),
    "max_discharge_ac_kw": ("PMaxDisch", 0, None),
    "charger_max_efficiency_pct": ("BattRectMaxEffic", 0, 100),
    "charger_euro_efficiency_pct": ("BattRectEuroEffic", 0, 100),
    "battery_inverter_max_efficiency_pct": ("BattInvMaxEffic", 0, 100),
    "battery_inverter_euro_efficiency_pct": ("BattInvEuroEffic", 0, 100),
}
STRATEGIES = {"self_consumption": "SelfConsumption", "weak_grid": "WeakGrid", "peak_shaving": "PeakShaving", "power_shift": "PowerShift"}
CHARGING_STRATEGIES = {
    "pv_available": "PVAvailable", "pv_above_thresh": "PVaboveThresh",
    "pv_to_grid_min": "PVToGridMin", "pv_prod_fraction": "PVProdFraction", "spec_charg_power": "SpecChargPower",
}
DISCHARGING_STRATEGIES = {
    "immediate": "Immediate", "after_sunset": "AfterSunset", "night": "Night",
    "hour_window": "MinMaxHour", "specified_hours": "SpecifiedHours",
}
KNOWN_PAIRS = frozenset({
    ("SelfConsumption", "PVAvailable", "Immediate"), ("WeakGrid", "PVAvailable", "Immediate"),
    ("PeakShaving", "PVToGridMin", "Immediate"), ("PeakShaving", "PVToGridMin", "AfterSunset"), ("PeakShaving", "PVToGridMin", "MinMaxHour"),
    ("PeakShaving", "PVaboveThresh", "Immediate"), ("PeakShaving", "PVaboveThresh", "AfterSunset"), ("PeakShaving", "PVaboveThresh", "MinMaxHour"),
    ("PowerShift", "PVProdFraction", "SpecifiedHours"), ("PowerShift", "PVProdFraction", "Night"), ("PowerShift", "PVProdFraction", "MinMaxHour"),
    ("PowerShift", "SpecChargPower", "SpecifiedHours"), ("PowerShift", "SpecChargPower", "Night"), ("PowerShift", "SpecChargPower", "MinMaxHour"),
    ("PowerShift", "PVToGridMin", "SpecifiedHours"), ("PowerShift", "PVToGridMin", "Night"), ("PowerShift", "PVToGridMin", "MinMaxHour"),
})
STRATEGY_KEYS = ("strategy", "charging_strategy", "discharging_strategy")
CONTROL_UPDATE_KEYS = frozenset({
    "strategy", "charging_strategy", "discharging_strategy", "grid_limit_kw",
    "charging_pv_fraction", "charging_power_kw", "charging_schedule_48",
    "discharging_power_kw", "discharging_schedule_48", "discharge_start_hour", "discharge_end_hour",
})
STORAGE_CHARGING = frozenset({"PVToGridMin", "PVaboveThresh"})
STORAGE_NUMBER = (
    "BattSoCInit", "SOCChDecl", "SOCDischDecl", "SOCDischSelf", "PMaxCharge", "PMaxDisch",
    "BattRectMaxEffic", "BattRectEuroEffic", "BattInvMaxEffic", "BattInvEuroEffic",
)
WINDOW = "MinMaxHour"
_HEX_FLAG = re.compile(r"\$[0-9a-fA-F]{1,8}")


def has_user_load(lines) -> bool:
    """Return True when the variant declares a self-consumption pvLoad object."""
    return any(re.fullmatch(r"\s*PVObject_\w*\s*=\s*pvLoad\s*", line) for line in lines)


def decimal_text(value):
    """Exact positional text of a float's shortest repr: 1e-05 is written 0.00001, never in E notation."""
    return format(Decimal(str(value)), "f")


def encode_subhour_schedule(slots: list[float], *, maximum: float = math.inf) -> str:
    """Encode 48 half-hour values (00:00..23:30) as native PVsyst TParSimulSeries ValSubHour."""
    if not isinstance(slots, list) or len(slots) != 48:
        raise ValueError("Sub-hour daily schedule must contain exactly 48 half-hour values")
    checked = [finite(value, "schedule slot", 0, maximum) for value in slots]
    normalized = [round(value, SUBHOUR_DECIMAL_PLACES) for value in checked]
    for index, (value, rounded) in enumerate(zip(checked, normalized), 1):
        if value > 0 and rounded == 0:
            raise ValueError(f"Sub-hour schedule slot {index} rounds to zero at six decimal places")
    peak = max(normalized)
    if peak == 0:
        return "48+1" + "+0" * 48
    default_scale = min(SUBHOUR_SCALE_LIMIT, max(1, round(SUBHOUR_TARGET_TOKEN / peak)))
    default_tokens = [round(value * default_scale) for value in normalized]
    if max(default_tokens) >= SUBHOUR_TOKEN_LIMIT:
        raise ValueError("Sub-hour schedule value exceeds the native base62 token limit")
    exact_scale = math.lcm(*(Fraction(str(value)).denominator for value in normalized))
    exact_peak = Fraction(str(peak))
    candidates = [default_scale]
    if exact_scale <= SUBHOUR_SCALE_LIMIT and exact_peak * exact_scale < SUBHOUR_TOKEN_LIMIT:
        candidates.append(exact_scale)
    candidates.append(min(SUBHOUR_SCALE_LIMIT, (SUBHOUR_TOKEN_LIMIT - 1) // exact_peak))
    for scale in dict.fromkeys(candidates):
        if scale < 1:
            continue
        tokens = default_tokens if scale == default_scale else [round(value * scale) for value in normalized]
        if max(tokens) < SUBHOUR_TOKEN_LIMIT and all(round(t / scale, SUBHOUR_DECIMAL_PLACES) == v for t, v in zip(tokens, normalized)):
            break
    else:
        scale, tokens = default_scale, default_tokens
        for index, (value, token) in enumerate(zip(normalized, tokens), 1):
            if value > 0 and token == 0:
                raise ValueError(f"Sub-hour schedule slot {index} would encode as zero at the native scale")
    parts = [f"48+{scale}"]
    for raw in tokens:
        if raw == 0:
            parts.append("+0")
        else:
            encoded, current = "", raw
            while current:
                current, rem = divmod(current, 62)
                encoded = DIGITS[rem] + encoded
            parts.append("+" + encoded)
    return "".join(parts)


def decode_subhour_schedule(raw: str) -> list[float]:
    """Decode PVsyst 48-slot TParSimulSeries ValSubHour text back into 48 float values."""
    match = re.fullmatch(r"48\+([0-9]+)((?:[+-][0-9A-Za-z]+){48})", raw.strip())
    if not match:
        raise ValueError("Expected 48-slot ValSubHour schedule with integer scale")
    scale = int(match[1])
    if scale <= 0:
        raise ValueError("ValSubHour scale factor must be positive")
    values = []
    for token in re.findall(r"[+-][0-9A-Za-z]+", match[2]):
        acc = 0
        for ch in token[1:]:
            idx = DIGITS.find(ch)
            if idx < 0:
                raise ValueError(f"Invalid base62 digit in ValSubHour: {ch!r}")
            acc = acc * 62 + idx
        values.append(round((-1 if token[0] == "-" else 1) * acc / scale, SUBHOUR_DECIMAL_PLACES))
    return values


def warn_schedule_quantization(requested, stored, warnings):
    """Report native encoding error relative to the six-decimal schedule input."""
    for key in ("charging_schedule_48", "discharging_schedule_48"):
        if key in requested and key in stored:
            error = max(abs(round(e, SUBHOUR_DECIMAL_PLACES) - a) for e, a in zip(requested[key], stored[key], strict=True))
            if error:
                warnings.append(f"Native encoding approximated {key} (max absolute error {error:.6g})")


def _parse_par_simul_series(lines, bounds) -> dict[str, dict]:
    """Extract named TParSimulSeries blocks directly inside pvGridStorage."""
    found, i = {}, bounds[0] + 1
    while i < bounds[1]:
        m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*TParSimulSeries", lines[i].strip())
        if not m:
            i += 1
            continue
        prop_name, end, varident, val_sub_hour, step_duration = m[1], None, None, None, None
        for j in range(i + 1, bounds[1]):
            line = lines[j].strip()
            if line == "End of TParSimulSeries":
                end = j
                break
            if line.startswith("Varident="):
                varident = line.split("=", 1)[1].strip()
            elif line.startswith("StepDuration="):
                step_duration = line.split("=", 1)[1].strip()
            elif line.startswith("ValSubHour="):
                val_sub_hour = line.split("=", 1)[1].strip()
        if end is None:
            raise ValueError(f"Unclosed TParSimulSeries block: {prop_name}")
        if step_duration != "1800" or val_sub_hour is None or not varident:
            raise ValueError(f"Unsupported TParSimulSeries payload in {prop_name}")
        found[prop_name] = {"span": (i, end), "varident": varident, "schedule_48": decode_subhour_schedule(val_sub_hour)}
        i = end + 1
    return found


def render_par_simul_series(prop_name: str, varident: str, var_type: str, scalar_key: str,
                            scalar_value: float, slots: list[float], *, maximum: float = math.inf) -> list[str]:
    """Build exact native PVsyst TParSimulSeries lines inside pvGridStorage."""
    encoded = encode_subhour_schedule(slots, maximum=maximum)
    return [
        f"    {scalar_key}={decimal_text(scalar_value)}", f"    {prop_name}=TParSimulSeries",
        "      fVarSimul=TVarSimul", f"        Varident={varident}", "        VarAcc=SubHour",
        f"        VarType={var_type}", "        VarStore=Simul", "        VarModeSS=Aver",
        "        DateBeg=01/01/90 00:00", "        DateEnd=01/01/90 23:30", "        StepDuration=1800",
        f"        ValSubHour={encoded}", "        WRFactor=0.000000", f"      End of VarSimul=vi{varident}",
        "    End of TParSimulSeries",
    ]


def set_grid_export(lines, system, allow_grid_export):
    """Toggle only native TSysteme.IsNoGridInjection (bit27), when declared."""
    if allow_grid_export is None:
        return
    if type(allow_grid_export) is not bool:
        raise ValueError("allow_grid_export must be a strict boolean")
    index, flags = one(direct_fields(lines, system), "Flags")
    if not _HEX_FLAG.fullmatch(flags):
        raise ValueError("Owning system flags must be unambiguous hexadecimal values")
    value = int(flags[1:], 16)
    lines[index] = replace_value(lines[index], f"${(value & ~0x08000000 if allow_grid_export else value | 0x08000000):08X}")


def set_system_grid_limit(lines, system, grid_limit_kw: float) -> float:
    """Update an enabled kW limit's value, or enable a new kW limit at injection."""
    limit = finite(grid_limit_kw, "grid_limit_kw", 0, math.inf, positive=True)
    if (Decimal(str(limit)) * 10) != (Decimal(str(limit)) * 10).to_integral_value():
        raise ValueError("Use 0.1 kW increments, matching native GUI save precision")
    fields = direct_fields(lines, system)
    flag_idx, raw_flags = one(fields, "Flags")
    if not _HEX_FLAG.fullmatch(raw_flags):
        raise ValueError("Owning system flags must be unambiguous hexadecimal values")
    flags = int(raw_flags[1:], 16)
    edits = {"PLimGrid": f"{limit:.1f}"}
    if flags & ENABLED:
        if flags & APPARENT:
            raise ValueError("Native apparent grid limits use kVA and cannot be set with grid_limit_kw")
    else:
        # Keep the legacy injection default: the 8.1.7 PeakShaving fixture does not charge at the inverter limit.
        flags = (flags & ~APPARENT) | ENABLED | INJECTION
        lines[flag_idx] = replace_value(lines[flag_idx], f"${flags:08X}")
        edits["LimGrSeparLoss"] = "False"
    newline = "\r\n" if any("\r\n" in line for line in lines) else "\n"
    insert = []
    for key, val in edits.items():
        existing = one(fields, key, False)
        if existing:
            lines[existing[0]] = replace_value(lines[existing[0]], val)
        else:
            insert.append(f"    {key}={val}{newline}")
    if insert:
        lines[system[1]:system[1]] = insert
    return limit


def _system_grid_limit(fields) -> dict[str, float]:
    raw_flags = one(fields, "Flags")[1]
    if not _HEX_FLAG.fullmatch(raw_flags):
        return {}
    flags = int(raw_flags[1:], 16)
    if not flags & ENABLED or (plim := one(fields, "PLimGrid", False)) is None:
        return {}
    return {("grid_limit_kva" if flags & APPARENT else "grid_limit_kw"): finite(float(Decimal(plim[1])), "PLimGrid", 0, math.inf, positive=True)}


def storage_state(lines, system):
    """Read owning declared storage fields (SelfConsumption, WeakGrid, PeakShaving, PowerShift)."""
    root_end = next((i for i, line in enumerate(lines[1:], 1) if line.strip().startswith("PVObject_")), len(lines) - 1)
    if one(direct_fields(lines, (0, root_end)), "Version")[1] not in NATIVE_TEXT_OR_SEED:
        raise ValueError(f"Grid storage requires a verified {SEED_LABEL} text variant")
    if system is None:
        raise ValueError("One grid pvSystem is required")
    fields = direct_fields(lines, system)
    if one(fields, "SystemType")[1] != "Grid":
        raise ValueError("Grid storage requires SystemType=Grid, not standalone backup")
    flags = one(fields, "Flags")[1]
    if not _HEX_FLAG.fullmatch(flags) or int(flags[1:], 16) & 0x10000040 != 0x10000040:
        raise ValueError("The template must already declare the checked grid-storage/battery enabling bits; dormant fields alone do not configure storage")
    battery = one(fields, "BatteryFile")[1]
    if not battery or any(char in battery for char in ("/", "\\")) or not battery.lower().endswith(".btr"):
        raise ValueError("Existing battery reference must be one plain BTR filename")
    counts = {}
    for key in ("NBattSerie", "NBattParall"):
        raw = one(fields, key)[1]
        if not raw.isdigit() or not 1 <= int(raw) <= 10000:
            raise ValueError("Battery pack counts must be positive bounded integers")
        counts[key] = int(raw)
    records = [block for block in objects("".join(lines)) if block.object_type == "pvGridStorage"]
    if len(records) != 1 or records[0].path != "PVObject_GridStorage" or not records[0].end_line:
        raise ValueError("An existing complete unique pvGridStorage template is required")
    bounds = (records[0].line - 1, records[0].end_line - 1)
    values = direct_fields(lines, bounds)
    if one(values, "Flags")[1] != "$00":
        raise ValueError("Only the checked flat grid storage Flags=$00 form is supported")
    state = {
        "battery_file": battery, "pack_counts": counts, "system_flags": flags,
        "allow_grid_export": not bool(int(flags[1:], 16) & 0x08000000),
        "strategy": one(values, "Strategy")[1], "charging_strategy": one(values, "ChargingStrategy")[1],
        "discharging_strategy": one(values, "DischStrategy")[1], "backend": "pvsyst_native_ac_coupled",
        **_system_grid_limit(fields),
    }
    for name, (key, low, high) in NUMERIC_FIELDS.items():
        try:
            raw = Decimal(one(values, key)[1])
            value = float(raw)
            if not raw.is_finite() or value == 0 and raw != 0:
                raise ValueError("Nonfinite or underflowed storage value")
        except (ValueError, InvalidOperation, OverflowError):
            raise ValueError("Invalid declared storage field: " + key) from None
        state[name] = finite(value, key, low, high if high is not None else float("inf"), positive=name.endswith(("kw", "pct")))
    if state["discharging_strategy"] == WINDOW:
        for name in ("HourBegDisch", "HourEndDisch"):
            raw = one(values, name)[1]
            if not re.fullmatch(r"[0-9]+", raw) or not 0 <= int(raw) <= 23:
                raise ValueError(name + " must be a native hour in 0..23")
            state[name] = int(raw)
        if state["HourBegDisch"] == state["HourEndDisch"]:
            raise ValueError("Native discharge window endpoints must differ")

    series = _parse_par_simul_series(lines, bounds)
    if state["strategy"] == "PowerShift":
        chg, dis = state["charging_strategy"], state["discharging_strategy"]
        if chg == "PVProdFraction":
            state["charging_pv_fraction"] = finite(float(Decimal(one(values, "ChargingPVFrac")[1])), "ChargingPVFrac", 0, 1, positive=True)
            if "ChargingPVFracParSim" in series:
                state["charging_schedule_48"] = series["ChargingPVFracParSim"]["schedule_48"]
        elif chg in ("SpecChargPower", "PVToGridMin"):
            state["charging_power_kw"] = finite(float(Decimal(one(values, "ChargingPower")[1])), "ChargingPower", 0, math.inf, positive=(chg == "SpecChargPower"))
            if "ChargingPowerParSim" in series:
                state["charging_schedule_48"] = series["ChargingPowerParSim"]["schedule_48"]
        if dis in ("SpecifiedHours", "Night", WINDOW):
            state["discharging_power_kw"] = finite(float(Decimal(one(values, "DischargingPower")[1])), "DischargingPower", 0, math.inf, positive=True)
            if dis == "SpecifiedHours" and "DischargingPowerParSim" in series:
                state["discharging_schedule_48"] = series["DischargingPowerParSim"]["schedule_48"]
    return state, bounds, values


def _validate_48_slots(sched, maximum: float, require_positive: bool, error: str) -> None:
    if sched is None:
        return
    if len(sched) != 48 or any(not 0 <= v <= maximum for v in sched) or (require_positive and max(sched) <= 0):
        raise ValueError(error)


def validate_storage(state, *, allow_weak=False, preserved=None):
    if not state["soc_min"] < state["soc_max"]:
        raise ValueError("Require soc_min < soc_max")
    for name in ("initial_soc", "reserve_soc"):
        if not state["soc_min"] <= state[name] <= state["soc_max"]:
            raise ValueError(name + " must lie within the declared SOC bounds")
    for prefix in ("charger", "battery_inverter"):
        if state[prefix + "_euro_efficiency_pct"] > state[prefix + "_max_efficiency_pct"]:
            raise ValueError(prefix + " Euro efficiency must not exceed max efficiency")
    pair = tuple(state[name] for name in STRATEGY_KEYS)
    if pair not in KNOWN_PAIRS:
        raise ValueError(f"Unsupported storage triplet {pair}")
    if state["strategy"] == "WeakGrid" and not allow_weak:
        raise ValueError("WeakGrid requires separately validated grid availability input")
    if preserved is not None and pair == tuple(preserved[name] for name in STRATEGY_KEYS):
        return
    if state["strategy"] == "PeakShaving" and state.get("grid_limit_kw") is None and state.get("grid_limit_kva") is None:
        raise ValueError("PeakShaving requires an enabled PLimGrid")
    if state["strategy"] == "PowerShift":
        chg, dis = state["charging_strategy"], state["discharging_strategy"]
        if chg == "PVProdFraction":
            frac = state.get("charging_pv_fraction")
            if frac is None or not 0 < frac <= 1:
                raise ValueError("PowerShift PVProdFraction requires charging_pv_fraction in (0, 1]")
            _validate_48_slots(state.get("charging_schedule_48"), 1, True, "PVProdFraction charging_schedule_48 must contain 48 slots in [0, 1] with at least one > 0")
        elif chg == "SpecChargPower":
            if (state.get("charging_power_kw") or 0) <= 0:
                raise ValueError("PowerShift SpecChargPower requires positive charging_power_kw")
            _validate_48_slots(state.get("charging_schedule_48"), math.inf, True, "SpecChargPower charging_schedule_48 must contain 48 non-negative slots with at least one > 0")
        elif chg == "PVToGridMin":
            pwr = state.get("charging_power_kw")
            if pwr is None or pwr < 0:
                raise ValueError("PowerShift PVToGridMin requires non-negative charging_power_kw")
            _validate_48_slots(state.get("charging_schedule_48"), math.inf, False, "PVToGridMin charging_schedule_48 must contain 48 non-negative slots")
        if (state.get("discharging_power_kw") or 0) <= 0:
            raise ValueError(f"PowerShift {dis} requires positive discharging_power_kw")
        if dis == "SpecifiedHours":
            _validate_48_slots(state.get("discharging_schedule_48"), math.inf, True, "SpecifiedHours discharging_schedule_48 must contain 48 non-negative slots with at least one > 0")


def _apply_hour_window_bounds(spec: dict, base: dict | None, *, allow_inherit: bool, dis: str, resolved: dict) -> None:
    if dis == WINDOW:
        sh = spec.get("discharge_start_hour", base.get("HourBegDisch") if allow_inherit and base else None)
        eh = spec.get("discharge_end_hour", base.get("HourEndDisch") if allow_inherit and base else None)
        if type(sh) is not int or type(eh) is not int or not (0 <= sh <= 23 and 0 <= eh <= 23) or sh == eh:
            raise ValueError("hour_window requires distinct integer discharge_start_hour and discharge_end_hour in 0..23")
        resolved.update(HourBegDisch=sh, HourEndDisch=eh)
    elif "discharge_start_hour" in spec or "discharge_end_hour" in spec:
        raise ValueError("discharge_start_hour/discharge_end_hour apply only to discharging_strategy='hour_window'")


def _resolve_48_slot_schedule(spec: dict, base: dict | None, *, same_mode: bool, schedule_key: str,
                              scalar_key: str, maximum: float, require_positive: bool, mode_label: str) -> tuple[float, list[float]]:
    sched = spec.get(schedule_key, base.get(schedule_key) if same_mode and base else None)
    scalar = spec.get(scalar_key, base.get(scalar_key) if same_mode and base else None)
    if sched is not None:
        if not isinstance(sched, list) or len(sched) != 48:
            raise ValueError(f"{schedule_key} must contain 48 values")
        sched = [finite(v, f"{schedule_key} slot", 0, maximum) for v in sched]
        if require_positive and max(sched) <= 0:
            raise ValueError(f"{mode_label} {schedule_key} requires at least one slot > 0")
        if scalar is None:
            scalar = max(sched)
    elif scalar is not None:
        scalar = finite(scalar, scalar_key, 0, maximum, positive=require_positive)
        sched = [scalar] * 48
    else:
        raise ValueError(f"PowerShift {mode_label} requires {scalar_key} and/or {schedule_key}")
    return finite(scalar, scalar_key, 0, maximum, positive=require_positive), sched


def resolve_storage_controls(spec: dict, *, base: dict | None = None, inherited_limit: dict | None = None) -> dict:
    """Resolve and strictly validate strategy, charging/discharging modes, schedules, and limits."""
    strat_key = spec.get("strategy")
    if strat_key is not None:
        if strat_key not in STRATEGIES:
            raise ValueError("strategy must be self_consumption, weak_grid, peak_shaving, or power_shift")
        strategy = STRATEGIES[strat_key]
    elif base is not None:
        strategy = base["strategy"]
    else:
        raise ValueError("strategy is required")

    def _map(raw, mapping, label, default_val):
        if raw is None:
            return default_val
        if raw not in mapping:
            raise ValueError(f"Invalid {label}: {raw!r}")
        return mapping[raw]

    same_strategy = base is not None and strategy == base["strategy"]
    if strategy in ("SelfConsumption", "WeakGrid"):
        chg = _map(spec.get("charging_strategy"), CHARGING_STRATEGIES, "charging_strategy", "PVAvailable")
        dis = _map(spec.get("discharging_strategy"), DISCHARGING_STRATEGIES, "discharging_strategy", "Immediate")
        if (chg, dis) != ("PVAvailable", "Immediate"):
            raise ValueError(f"{strategy} requires charging_strategy='pv_available' and discharging_strategy='immediate'")
        present = (CONTROL_UPDATE_KEYS - {"strategy", "charging_strategy", "discharging_strategy"}) & set(spec)
        if present:
            raise ValueError(f"{strategy} does not accept {sorted(present)}")
        return {"strategy": strategy, "charging_strategy": chg, "discharging_strategy": dis}

    if strategy == "PeakShaving":
        chg = _map(spec.get("charging_strategy"), CHARGING_STRATEGIES, "charging_strategy", base["charging_strategy"] if same_strategy else "PVaboveThresh")
        dis = _map(spec.get("discharging_strategy"), DISCHARGING_STRATEGIES, "discharging_strategy", base["discharging_strategy"] if same_strategy else "Immediate")
        if (strategy, chg, dis) not in KNOWN_PAIRS:
            raise ValueError(f"Unsupported PeakShaving pair ({chg}, {dis})")
        present = {"charging_pv_fraction", "charging_power_kw", "charging_schedule_48", "discharging_power_kw", "discharging_schedule_48"} & set(spec)
        if present:
            raise ValueError(f"PeakShaving does not accept PowerShift fields {sorted(present)}")
        resolved = {"strategy": strategy, "charging_strategy": chg, "discharging_strategy": dis}
        if "grid_limit_kw" in spec:
            resolved["grid_limit_kw"] = finite(spec["grid_limit_kw"], "grid_limit_kw", 0, math.inf, positive=True)
        else:
            limits = base if base is not None else inherited_limit or {}
            if "grid_limit_kw" in limits:
                resolved["grid_limit_kw"] = limits["grid_limit_kw"]
            elif "grid_limit_kva" in limits:
                if not same_strategy:
                    raise ValueError("Native apparent grid limits use kVA and cannot be inherited as grid_limit_kw")
                resolved["grid_limit_kva"] = limits["grid_limit_kva"]
            else:
                raise ValueError("PeakShaving requires grid_limit_kw (or an existing PLimGrid on pvSystem)")
        _apply_hour_window_bounds(spec, base, allow_inherit=same_strategy, dis=dis, resolved=resolved)
        return resolved

    chg = _map(spec.get("charging_strategy"), CHARGING_STRATEGIES, "charging_strategy", base["charging_strategy"] if same_strategy else "PVProdFraction")
    dis = _map(spec.get("discharging_strategy"), DISCHARGING_STRATEGIES, "discharging_strategy", base["discharging_strategy"] if same_strategy else "SpecifiedHours")
    if (strategy, chg, dis) not in KNOWN_PAIRS:
        raise ValueError(f"Unsupported PowerShift pair ({chg}, {dis}); note AfterSunset is rejected by PVsyst for PowerShift")
    resolved = {"strategy": strategy, "charging_strategy": chg, "discharging_strategy": dis}
    if "grid_limit_kw" in spec:
        resolved["grid_limit_kw"] = finite(spec["grid_limit_kw"], "grid_limit_kw", 0, math.inf, positive=True)
    same_chg = same_strategy and chg == base["charging_strategy"]
    if chg == "PVProdFraction":
        if "charging_power_kw" in spec:
            raise ValueError("PVProdFraction uses charging_pv_fraction, not charging_power_kw")
        frac, sched = _resolve_48_slot_schedule(spec, base, same_mode=same_chg, schedule_key="charging_schedule_48", scalar_key="charging_pv_fraction", maximum=1, require_positive=True, mode_label="PVProdFraction")
        resolved.update(charging_pv_fraction=frac, charging_schedule_48=sched)
    else:
        if "charging_pv_fraction" in spec:
            raise ValueError(f"{chg} uses charging_power_kw, not charging_pv_fraction")
        pwr, sched = _resolve_48_slot_schedule(spec, base, same_mode=same_chg, schedule_key="charging_schedule_48", scalar_key="charging_power_kw", maximum=math.inf, require_positive=(chg == "SpecChargPower"), mode_label=chg)
        resolved.update(charging_power_kw=pwr, charging_schedule_48=sched)
    same_dis = same_strategy and dis == base["discharging_strategy"]
    if dis == "SpecifiedHours":
        if "discharge_start_hour" in spec or "discharge_end_hour" in spec:
            raise ValueError("discharge_start_hour/discharge_end_hour apply only to hour_window")
        dpwr, dsched = _resolve_48_slot_schedule(spec, base, same_mode=same_dis, schedule_key="discharging_schedule_48", scalar_key="discharging_power_kw", maximum=math.inf, require_positive=True, mode_label="SpecifiedHours")
        resolved.update(discharging_power_kw=dpwr, discharging_schedule_48=dsched)
    else:
        if "discharging_schedule_48" in spec:
            raise ValueError(f"discharging_schedule_48 applies only to discharging_strategy='specified_hours', not {dis}")
        dpwr = spec.get("discharging_power_kw", base.get("discharging_power_kw") if same_dis else None)
        if dpwr is None:
            raise ValueError(f"PowerShift {dis} requires discharging_power_kw")
        resolved["discharging_power_kw"] = finite(dpwr, "discharging_power_kw", 0, math.inf, positive=True)
        _apply_hour_window_bounds(spec, base, allow_inherit=same_dis, dis=dis, resolved=resolved)
    return resolved


def render_grid_storage_block(state: dict, unavail_lines: list[str] | None = None) -> list[str]:
    """Render a complete flat/TParSimulSeries pvGridStorage block in PVsyst 8.1.7 order."""
    lines = [
        "  PVObject_GridStorage=pvGridStorage", "    Flags=$00",
        "    Strategy=" + state["strategy"], "    ChargingStrategy=" + state["charging_strategy"],
        "    DischStrategy=" + state["discharging_strategy"],
    ]
    if state["strategy"] == "PowerShift":
        if state["charging_strategy"] == "PVProdFraction":
            lines.extend(render_par_simul_series("ChargingPVFracParSim", "FPVChMx", "Factor", "ChargingPVFrac", state["charging_pv_fraction"], state["charging_schedule_48"], maximum=1.0))
        else:
            lines.extend(render_par_simul_series("ChargingPowerParSim", "EChgMax", "Power", "ChargingPower", state["charging_power_kw"], state["charging_schedule_48"]))
        if state["discharging_strategy"] == "SpecifiedHours":
            lines.extend(render_par_simul_series("DischargingPowerParSim", "EDisMax", "Power", "DischargingPower", state["discharging_power_kw"], state["discharging_schedule_48"]))
        else:
            lines.append(f"    DischargingPower={decimal_text(state['discharging_power_kw'])}")
    if state["discharging_strategy"] == WINDOW:
        lines.extend([f"    HourBegDisch={state['HourBegDisch']}", f"    HourEndDisch={state['HourEndDisch']}"])
    lines.extend("    " + key + "=" + decimal_text(state[name]) for name, (key, _, _) in NUMERIC_FIELDS.items())
    if unavail_lines:
        lines.extend(unavail_lines)
    lines.append("  End of PVObject pvGridStorage")
    return lines


def grid_storage_state(lines, system):
    """Expose stored native scalars & schedules, without asserting availability or native consumption."""
    if not any(re.fullmatch(r"\s*PVObject_\w*\s*=\s*pvGridStorage\s*", line) for line in lines):
        return None
    try:
        state, _, _ = storage_state(lines, system)
        validate_storage(state, allow_weak=True, preserved=state)
        return dict(state, mode="stored")
    except (ValueError, KeyError, TypeError, StopIteration) as exc:
        return {"mode": "unsupported", "issues": [str(exc)]}


def create_grid_storage_variant(store, *, project, source_variant, new_variant, updates, dry_run=True,
                                availability=None, allow_grid_export=None):
    """Create a new variant updating scalar, strategy, schedule, grid limit, or export controls on AC storage."""
    allowed_keys = set(NUMERIC_FIELDS) | CONTROL_UPDATE_KEYS | {"coupling"}

    def preflight():
        if not isinstance(updates, dict):
            raise ValueError("Supply strict storage updates or explicit grid export control and boolean dry_run")
        coupling = updates.get("coupling")
        if coupling is not None and coupling != "ac_coupled":
            raise ValueError("PVsyst 8.1.7 only implements Common AC Bus grid storage (pvGridStorage); DC-coupled hybrid battery ports and DC clipping recovery are unsupported (use equivalent AC charger/inverter efficiencies with coupling='ac_coupled')")
        clean = {k: v for k, v in updates.items() if k != "coupling"}
        if (not clean and allow_grid_export is None) or (set(updates) - allowed_keys):
            raise ValueError("Supply strict storage updates or explicit grid export control and boolean dry_run" if not clean and allow_grid_export is None else "Unknown storage update fields")

    def build(*, text, lines, result, **_):
        _, system = store._sections(lines)
        before, bounds, fields = storage_state(lines, system)
        validate_storage(before, allow_weak=True, preserved=before)
        newline = "\r\n" if "\r\n" in text else "\n"
        scalar_edits, merged_state = {}, dict(before)
        clean_updates = {k: v for k, v in updates.items() if k != "coupling"}
        for name, value in clean_updates.items():
            if name in NUMERIC_FIELDS:
                key, low, high = NUMERIC_FIELDS[name]
                num = finite(value, name, low, high if high is not None else float("inf"), positive=name.endswith(("kw", "pct")))
                scalar_edits[key] = decimal_text(num)
                merged_state[name] = num

        structural = bool(CONTROL_UPDATE_KEYS & set(clean_updates))
        if clean_updates.get("strategy") == "peak_shaving" and has_user_load(lines):
            raise ValueError(PEAK_SHAVING_LOAD_ERROR)
        if not structural:
            for key, value in scalar_edits.items():
                lines[one(fields, key)[0]] = replace_value(lines[one(fields, key)[0]], value)
            set_grid_export(lines, system, allow_grid_export)
            after, _, _ = storage_state(lines, system)
            weak = after["strategy"] == "WeakGrid"
            validate_storage(after, allow_weak=availability is not None, preserved=before)
        else:
            controls = resolve_storage_controls(clean_updates, base=before)
            merged_state.update(controls)
            if "grid_limit_kw" in clean_updates:
                set_system_grid_limit(lines, system, controls["grid_limit_kw"])
                _, system = store._sections(lines)
            set_grid_export(lines, system, allow_grid_export)
            weak = merged_state["strategy"] == "WeakGrid"
            validate_storage(merged_state, allow_weak=weak and availability is not None)
            _, bounds, _ = storage_state(lines, store._sections(lines)[1])
            local_unavail = []
            if weak:
                in_unavail = False
                for line in lines[bounds[0]:bounds[1] + 1]:
                    s = line.strip()
                    if s == "PVObject_GridUnavail=pvUnavail":
                        in_unavail = True
                    if in_unavail:
                        local_unavail.append(line.rstrip("\r\n"))
                    if s == "End of PVObject pvUnavail":
                        in_unavail = False
            lines[bounds[0]:bounds[1] + 1] = [row + newline for row in render_grid_storage_block(merged_state, local_unavail)]
            after, bounds, _ = storage_state(lines, store._sections(lines)[1])
            validate_storage(after, allow_weak=weak and availability is not None)

        if after["strategy"] == "PeakShaving" and has_user_load(lines):
            raise ValueError(PEAK_SHAVING_LOAD_ERROR)
        if (availability is not None) != weak:
            raise ValueError("Supply a fresh complete availability declaration exactly when configuring WeakGrid")

        strip_result_cache(lines)
        _, bounds, _ = storage_state(lines, store._sections(lines)[1])
        candidate_text = "".join(lines)
        if weak:
            first_child = next(i for i, line in enumerate(lines[1:], 1) if line.strip().startswith("PVObject_"))
            root_flags = one(direct_fields(lines, (0, first_child)), "Flags")[1]
            if not _HEX_FLAG.fullmatch(root_flags) or not int(root_flags[1:], 16) & 0x80000:
                raise ValueError("WeakGrid requires an already enabled native self-consumption load")
            profile, metadata = native_availability(store.workspace, availability, newline)
            load_starts = [i for i, line in enumerate(lines) if line.strip() == "PVObject_UserNeeds=pvLoad"]
            load_ends = [i for i, line in enumerate(lines) if line.strip() == "End of PVObject pvLoad"]
            if len(load_starts) != 1 or len(load_ends) != 1 or load_starts[0] >= load_ends[0]:
                raise ValueError("WeakGrid requires one complete native load snapshot")
            load_lines = lines[load_starts[0]:load_ends[0]]
            year = metadata["year"] % 100
            if (sum(line.strip() == f"DateBeg=01/01/{year:02d} 00:00" for line in load_lines) != 1
                    or sum(line.strip() == f"DateEnd=31/12/{year:02d} 23:00" for line in load_lines) != 1):
                raise ValueError("Availability must match the complete owning load snapshot year")
            local = "".join(lines[bounds[0]:bounds[1] + 1])
            starts = list(re.finditer(r"(?m)^[ \t]*PVObject_GridUnavail=pvUnavail[ \t]*\r?$", local))
            ends = list(re.finditer(r"(?m)^[ \t]*End of PVObject pvUnavail[ \t]*\r?$", local))
            if len(starts) > 1 or len(ends) > 1 or bool(starts) != bool(ends):
                raise ValueError("Ambiguous owning grid availability object")
            if starts:
                if starts[0].start() >= ends[0].end():
                    raise ValueError("Unmatched grid availability object")
                local = local[:starts[0].start()] + profile + local[ends[0].end() + local.startswith("\n", ends[0].end()):]
            else:
                line_start = local.rfind("\n", 0, local.rindex("End of PVObject pvGridStorage")) + 1
                local = local[:line_start] + profile + local[line_start:]
            candidate_text = "".join(lines[:bounds[0]]) + local + "".join(lines[bounds[1] + 1:])
            result["grid_availability"] = metadata

        def verify(checked, _, res):
            verified, _, _ = storage_state(checked, store._sections(checked)[1])
            validate_storage(verified, allow_weak=availability is not None, preserved=before if not structural else None)
            if verified != after:
                raise ValueError("Candidate did not retain the declared storage state")
            if structural:
                warn_schedule_quantization(merged_state, verified, res["warnings"])
            res.update(before=before, after=after)

        return {"candidate_text": candidate_text, "verify": verify}

    return run_new_variant_transaction(
        store, project=project, source_variant=source_variant, new_variant=new_variant, dry_run=dry_run,
        bom_display_target=new_variant, preflight=preflight, build=build,
        exists_prefix="Choose a new grid storage variant: ", source_changed_error="Storage template changed before creation",
        write_failure_warning="New variant may be a partial artifact; inspect the disclosed path. Original template is preserved.",
        extra_exceptions=(csv.Error, InvalidOperation), include_bom_added=False, uppercase_variant=False,
    )


def peak_shaving_state(lines):
    """Inspect the PeakShaving discharge state directly from pvGridStorage without throwing."""
    owner = [i for i, line in enumerate(lines)
             if (match := ASSIGNMENT.fullmatch(line.rstrip("\r\n"))) and (match[2], match[4]) == ("PVObject_GridStorage", "pvGridStorage")]
    if not owner:
        return {"mode": None, "reason": "no pvGridStorage section"}
    if len(owner) != 1:
        return {"mode": None, "issue": "Requires exactly one pvGridStorage record"}
    end = next((i for i in range(owner[0] + 1, len(lines)) if lines[i].strip() == "End of PVObject pvGridStorage"), None)
    if end is None:
        return {"mode": None, "issue": "pvGridStorage is not closed"}
    try:
        owned = direct_fields(lines, (owner[0], end))
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}
    for key, entries in owned.items():
        if len(entries) != 1:
            return {"mode": None, "issue": f"Duplicate {key} in pvGridStorage"}
    fields = {key: entries[0][1] for key, entries in owned.items()}
    if "Strategy" not in fields:
        return {"mode": None, "issue": "pvGridStorage carries no Strategy"}
    if fields["Strategy"] != "PeakShaving":
        return {"mode": "other", "strategy": fields["Strategy"], "reason": "the stored strategy is not peak shaving"}
    if fields.get("ChargingStrategy") not in STORAGE_CHARGING:
        return {"mode": "other", "charging_strategy": fields.get("ChargingStrategy"), "reason": "unsupported peak shaving charging strategy"}
    expected = {"Flags", "Strategy", "ChargingStrategy", "DischStrategy"} | set(STORAGE_NUMBER)
    strategy = fields.get("DischStrategy")
    if strategy == WINDOW:
        expected |= {"HourBegDisch", "HourEndDisch"}
    if set(fields) != expected:
        return {"mode": None, "issue": "pvGridStorage fields do not match the recorded native shape"}
    discharge_map = {
        "AfterSunset": {"mode": "after_sunset"},
        "Immediate": {"mode": "immediate"},
        WINDOW: {"mode": "hour_window", "start_hour": fields.get("HourBegDisch"), "end_hour": fields.get("HourEndDisch")},
    }
    if strategy not in discharge_map:
        return {"mode": "other", "discharge_strategy": strategy, "reason": "unrecorded discharge strategy"}
    return {"mode": "configured", "discharge": discharge_map[strategy], "charging_strategy": fields["ChargingStrategy"],
            "values": {key: fields[key] for key in sorted(expected)}}


def _resolve_hourly_rates(tariff: dict, *, month: int, day_type: str) -> list[dict[str, Any]]:
    is_weekend = day_type == "weekend"
    resolved: list[dict[str, Any]] = []
    for hour in range(24):
        name, imp, exp = "default", tariff["import_tariff"], tariff["feed_in_tariff"]
        for period in tariff["tou_schedule"]:
            if ((period["months"] is None or month in period["months"])
                    and not (period["day_type"] == "weekday" and is_weekend)
                    and not (period["day_type"] == "weekend" and not is_weekend)
                    and hour in period["hours"]):
                name, imp, exp = period["name"], period["import_rate"], period["export_rate"]
                break
        resolved.append({"hour": hour, "period": name, "import_rate": round(imp, 6), "export_rate": round(exp, 6)})
    return resolved


def _segment_day(hourly: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse consecutive hours with identical import_rate into contiguous segments."""
    segments: list[dict[str, Any]] = []
    for rec in hourly:
        if segments and abs(segments[-1]["rate"] - rec["import_rate"]) < 1e-9:
            segments[-1]["hours"].append(rec["hour"])
        else:
            segments.append({"period": rec["period"], "rate": rec["import_rate"], "hours": [rec["hour"]]})
    return segments


def plan_tou_storage_schedule(
    *, tou_schedule: list[dict[str, Any]], charge_power_kw: float, discharge_power_kw: float | None = None,
    usable_energy_kwh: float | None = None, cycles_per_day: int = 2, round_trip_efficiency: float = 0.88,
    min_spread: float = 0.05, month: int = 7, day_type: str = "weekday", charging_strategy: str = "spec_charg_power",
    default_import_rate: float = 0.0, default_export_rate: float = 0.0,
) -> dict[str, Any]:
    """Derive a PVsyst PowerShift 48-slot (half-hour) schedule from a TOU tariff schedule."""
    from pvsyst_auto.engineering.economics import _validate_tariff

    ch_kw = _pos_finite(charge_power_kw, "charge_power_kw")
    dis_kw = _pos_finite(discharge_power_kw, "discharge_power_kw") if discharge_power_kw is not None else ch_kw
    usable_kwh = _pos_finite(usable_energy_kwh, "usable_energy_kwh") if usable_energy_kwh is not None else None
    if type(cycles_per_day) is not int or cycles_per_day not in (1, 2):
        raise ValueError("cycles_per_day must be 1 or 2")
    rte = _pos_finite(round_trip_efficiency, "round_trip_efficiency")
    if not 0.5 <= rte <= 1.0:
        raise ValueError("round_trip_efficiency must be in [0.5, 1.0]")
    min_sp = _pos_finite(min_spread, "min_spread", allow_zero=True)
    if type(month) is not int or not 1 <= month <= 12:
        raise ValueError("month must be an integer in 1..12")
    if day_type not in ("all", "weekday", "weekend"):
        raise ValueError("day_type must be 'all', 'weekday', or 'weekend'")
    if charging_strategy not in ("spec_charg_power", "pv_prod_fraction"):
        raise ValueError("charging_strategy must be 'spec_charg_power' or 'pv_prod_fraction'")

    tar = _validate_tariff({"mode": "tou", "import_tariff": default_import_rate, "feed_in_tariff": default_export_rate, "tou_schedule": tou_schedule})
    hourly = _resolve_hourly_rates(tar, month=month, day_type=day_type)
    rates, segments = [h["import_rate"] for h in hourly], _segment_day(hourly)
    min_rate, max_rate, warnings, raw_cycles = min(rates), max(rates), [], []
    meets_spread = lambda high, low: (Decimal(str(high)) - Decimal(str(min_sp))) * Decimal(str(rte)) >= Decimal(str(low))

    if not meets_spread(max_rate, min_rate):
        warnings.append(f"Maximum effective spread ({max_rate - min_rate / rte:.4f}) is below min_spread ({min_sp:.4f}); returning idle 48-slot schedules to avoid unprofitable cycling.")
    else:
        n_seg = len(segments)
        peak_indices = [
            i for i, seg in enumerate(segments)
            if meets_spread(seg["rate"], min_rate) and (i == 0 or seg["rate"] >= segments[i - 1]["rate"]) and (i == n_seg - 1 or seg["rate"] >= segments[i + 1]["rate"])
        ] or [i for i, seg in enumerate(segments) if abs(seg["rate"] - max_rate) < 1e-9]
        chosen_peaks = sorted(sorted(peak_indices, key=lambda i: (-segments[i]["rate"], segments[i]["hours"][0]))[:cycles_per_day])
        used_hours = {h for idx in chosen_peaks for h in segments[idx]["hours"]}
        prev_peak_idx = -1
        for peak_idx in chosen_peaks:
            peak_seg = segments[peak_idx]
            cands = [j for j in range(prev_peak_idx + 1, peak_idx) if not (used_hours & set(segments[j]["hours"]))]
            if peak_idx == chosen_peaks[0]:
                cands.extend(range(chosen_peaks[-1] + 1, n_seg))
            best_ch_idx, best_spread = None, -1e9
            for j in cands:
                spread = peak_seg["rate"] - (segments[j]["rate"] / rte)
                if meets_spread(peak_seg["rate"], segments[j]["rate"]) and spread > best_spread:
                    best_ch_idx, best_spread = j, spread
            if best_ch_idx is not None:
                ch_seg = segments[best_ch_idx]
                used_hours.update(ch_seg["hours"])
                raw_cycles.append({
                    "charge_hours": list(ch_seg["hours"]), "discharge_hours": list(peak_seg["hours"]),
                    "charge_period": ch_seg["period"], "discharge_period": peak_seg["period"],
                    "avg_charge_rate": ch_seg["rate"], "avg_discharge_rate": peak_seg["rate"],
                    "effective_spread": round(best_spread, 5),
                })
                prev_peak_idx = peak_idx
        if not raw_cycles:
            warnings.append("No chronological charge-then-discharge window pair satisfied min_spread; returning idle schedules.")

    ch_48, dis_48, eta_one, planned_cycles = [0.0] * 48, [0.0] * 48, math.sqrt(rte), []
    for c_idx, cyc in enumerate(raw_cycles, start=1):
        ch_hours, dis_hours = cyc["charge_hours"], cyc["discharge_hours"]
        ch_slots = [s for h in ch_hours for s in (2 * h, 2 * h + 1)]
        dis_slots = [s for h in dis_hours for s in (2 * h, 2 * h + 1)]
        eff_ch_kw, eff_dis_kw = ch_kw, dis_kw
        if usable_kwh is not None:
            if ch_slots:
                eff_ch_kw = min(ch_kw, (usable_kwh / eta_one) / (len(ch_slots) * 0.5))
            if dis_slots:
                eff_dis_kw = min(dis_kw, (usable_kwh * eta_one) / (len(dis_slots) * 0.5))
        eff_ch_kw, eff_dis_kw = round(eff_ch_kw, 3), round(eff_dis_kw, 3)
        if charging_strategy == "spec_charg_power":
            eff_dis_kw = min(eff_dis_kw, math.floor((eff_ch_kw * len(ch_slots) * rte / len(dis_slots)) * 1000) / 1000)
        for s in ch_slots:
            ch_48[s] = eff_ch_kw
        for s in dis_slots:
            dis_48[s] = eff_dis_kw
        planned_cycles.append({
            "cycle": c_idx, "charge_period": cyc["charge_period"], "discharge_period": cyc["discharge_period"],
            "charge_hours": ch_hours, "discharge_hours": dis_hours,
            "charge_power_kw": eff_ch_kw, "discharge_power_kw": eff_dis_kw,
            "avg_charge_rate": round(cyc["avg_charge_rate"], 4), "avg_discharge_rate": round(cyc["avg_discharge_rate"], 4),
            "effective_spread": cyc["effective_spread"],
            "planned_charge_kwh": round(eff_ch_kw * len(ch_slots) * 0.5, 2),
            "planned_discharge_kwh": round(eff_dis_kw * len(dis_slots) * 0.5, 2),
        })

    storage_updates: dict[str, Any] = {
        "coupling": "ac_coupled", "strategy": "power_shift", "charging_strategy": charging_strategy,
        "discharging_strategy": "specified_hours", "max_charge_ac_kw": round(ch_kw, 3),
        "max_discharge_ac_kw": round(dis_kw, 3), "discharging_schedule_48": dis_48,
    }
    if charging_strategy == "spec_charg_power":
        storage_updates["charging_schedule_48"] = ch_48
    else:
        storage_updates["charging_pv_fraction"] = 1.0

    return {
        "status": "ok" if planned_cycles else "idle", "month": month, "day_type": day_type,
        "requested_cycles_per_day": cycles_per_day, "planned_cycles_count": len(planned_cycles),
        "round_trip_efficiency": rte, "min_spread": min_sp, "cycles": planned_cycles,
        "charging_schedule_48": ch_48, "discharging_schedule_48": dis_48,
        "storage_updates": storage_updates, "hourly_rates": hourly, "warnings": warnings,
    }

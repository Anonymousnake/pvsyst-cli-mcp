"""Native AC-coupled grid storage fields and PowerShift/PeakShaving schedules."""
from __future__ import annotations

import csv
import math
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from pvsyst_auto.components.editor import objects, replace_value
from pvsyst_auto.components.generator import direct_fields, finite, one
from pvsyst_auto.energy.availability import native_availability
from pvsyst_auto.energy.power import APPARENT, ENABLED, INJECTION
from pvsyst_auto.native.headless import strip_result_cache
from pvsyst_auto.project.variant_writer import run_new_variant_transaction
from pvsyst_auto.versions import NATIVE_TEXT_OR_SEED, SEED_LABEL

DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
SUBHOUR_TARGET_TOKEN = 90_000
# Conservative writer ceiling, verified by native consumption; not an asserted native limit.
SUBHOUR_SCALE_LIMIT = 1_000_000
SUBHOUR_TOKEN_LIMIT = 62**3
SUBHOUR_DECIMAL_PLACES = 6

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

STRATEGIES = {
    "self_consumption": "SelfConsumption",
    "weak_grid": "WeakGrid",
    "peak_shaving": "PeakShaving",
    "power_shift": "PowerShift",
}
CHARGING_STRATEGIES = {
    "pv_available": "PVAvailable",
    "pv_above_thresh": "PVaboveThresh",
    "pv_to_grid_min": "PVToGridMin",
    "pv_prod_fraction": "PVProdFraction",
    "spec_charg_power": "SpecChargPower",
}
DISCHARGING_STRATEGIES = {
    "immediate": "Immediate",
    "after_sunset": "AfterSunset",
    "night": "Night",
    "hour_window": "MinMaxHour",
    "specified_hours": "SpecifiedHours",
}

KNOWN_PAIRS = frozenset({
    ("SelfConsumption", "PVAvailable", "Immediate"),
    ("WeakGrid", "PVAvailable", "Immediate"),
    ("PeakShaving", "PVToGridMin", "Immediate"),
    ("PeakShaving", "PVToGridMin", "AfterSunset"),
    ("PeakShaving", "PVToGridMin", "MinMaxHour"),
    ("PeakShaving", "PVaboveThresh", "Immediate"),
    ("PeakShaving", "PVaboveThresh", "AfterSunset"),
    ("PeakShaving", "PVaboveThresh", "MinMaxHour"),
    ("PowerShift", "PVProdFraction", "SpecifiedHours"),
    ("PowerShift", "PVProdFraction", "Night"),
    ("PowerShift", "PVProdFraction", "MinMaxHour"),
    ("PowerShift", "SpecChargPower", "SpecifiedHours"),
    ("PowerShift", "SpecChargPower", "Night"),
    ("PowerShift", "SpecChargPower", "MinMaxHour"),
    ("PowerShift", "PVToGridMin", "SpecifiedHours"),
    ("PowerShift", "PVToGridMin", "Night"),
    ("PowerShift", "PVToGridMin", "MinMaxHour"),
})
STRATEGY_KEYS = ("strategy", "charging_strategy", "discharging_strategy")
CONTROL_UPDATE_KEYS = frozenset({
    "strategy",
    "charging_strategy",
    "discharging_strategy",
    "grid_limit_kw",
    "charging_pv_fraction",
    "charging_power_kw",
    "charging_schedule_48",
    "discharging_power_kw",
    "discharging_schedule_48",
    "discharge_start_hour",
    "discharge_end_hour",
})


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
        if max(tokens) < SUBHOUR_TOKEN_LIMIT and all(
            round(token / scale, SUBHOUR_DECIMAL_PLACES) == value
            for token, value in zip(tokens, normalized)
        ):
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
            encoded = ""
            current = raw
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
    tokens = re.findall(r"[+-][0-9A-Za-z]+", match[2])
    values = []
    for token in tokens:
        sign = -1 if token[0] == "-" else 1
        acc = 0
        for ch in token[1:]:
            idx = DIGITS.find(ch)
            if idx < 0:
                raise ValueError(f"Invalid base62 digit in ValSubHour: {ch!r}")
            acc = acc * 62 + idx
        values.append(round(sign * acc / scale, SUBHOUR_DECIMAL_PLACES))
    return values


def warn_schedule_quantization(requested, stored, warnings):
    """Report native encoding error relative to the six-decimal schedule input."""
    for key in ("charging_schedule_48", "discharging_schedule_48"):
        if key not in requested or key not in stored:
            continue
        error = max(abs(round(expected, SUBHOUR_DECIMAL_PLACES) - actual)
                    for expected, actual in zip(requested[key], stored[key], strict=True))
        if error:
            warnings.append(f"Native encoding approximated {key} (max absolute error {error:.6g})")


def _parse_par_simul_series(lines, bounds) -> dict[str, dict]:
    """Extract named TParSimulSeries blocks directly inside pvGridStorage."""
    found = {}
    i = bounds[0] + 1
    while i < bounds[1]:
        stripped = lines[i].strip()
        m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*TParSimulSeries", stripped)
        if not m:
            i += 1
            continue
        prop_name = m[1]
        start = i
        end = None
        varident = None
        val_sub_hour = None
        step_duration = None
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
        found[prop_name] = {
            "span": (start, end),
            "varident": varident,
            "schedule_48": decode_subhour_schedule(val_sub_hour),
        }
        i = end + 1
    return found


def render_par_simul_series(prop_name: str, varident: str, var_type: str,
                            scalar_key: str, scalar_value: float,
                            slots: list[float], *, maximum: float = math.inf) -> list[str]:
    """Build exact native PVsyst TParSimulSeries lines inside pvGridStorage."""
    encoded = encode_subhour_schedule(slots, maximum=maximum)
    return [
        f"    {scalar_key}={decimal_text(scalar_value)}",
        f"    {prop_name}=TParSimulSeries",
        "      fVarSimul=TVarSimul",
        f"        Varident={varident}",
        "        VarAcc=SubHour",
        f"        VarType={var_type}",
        "        VarStore=Simul",
        "        VarModeSS=Aver",
        "        DateBeg=01/01/90 00:00",
        "        DateEnd=01/01/90 23:30",
        "        StepDuration=1800",
        f"        ValSubHour={encoded}",
        "        WRFactor=0.000000",
        f"      End of VarSimul=vi{varident}",
        "    End of TParSimulSeries",
    ]


def set_grid_export(lines, system, allow_grid_export):
    """Toggle only native TSysteme.IsNoGridInjection (bit27), when declared."""
    if allow_grid_export is None:
        return
    if type(allow_grid_export) is not bool:
        raise ValueError("allow_grid_export must be a strict boolean")
    index, flags = one(direct_fields(lines, system), "Flags")
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", flags):
        raise ValueError("Owning system flags must be unambiguous hexadecimal values")
    value = int(flags[1:], 16)
    value = value & ~0x08000000 if allow_grid_export else value | 0x08000000
    lines[index] = replace_value(lines[index], f"${value:08X}")


def set_system_grid_limit(lines, system, grid_limit_kw: float) -> float:
    """Update an enabled kW limit's value, or enable a new kW limit at injection."""
    limit = finite(grid_limit_kw, "grid_limit_kw", 0, math.inf, positive=True)
    tenths = Decimal(str(limit)) * 10
    if tenths != tenths.to_integral_value():
        raise ValueError("Use 0.1 kW increments, matching native GUI save precision")
    fields = direct_fields(lines, system)
    flag_idx, raw_flags = one(fields, "Flags")
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw_flags):
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
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw_flags):
        return {}
    flags = int(raw_flags[1:], 16)
    if not flags & ENABLED:
        return {}
    plim = one(fields, "PLimGrid", False)
    if plim is None:
        return {}
    key = "grid_limit_kva" if flags & APPARENT else "grid_limit_kw"
    return {key: finite(float(Decimal(plim[1])), "PLimGrid", 0, math.inf, positive=True)}


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
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", flags) or int(flags[1:], 16) & 0x10000040 != 0x10000040:
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
        "battery_file": battery,
        "pack_counts": counts,
        "system_flags": flags,
        "allow_grid_export": not bool(int(flags[1:], 16) & 0x08000000),
        "strategy": one(values, "Strategy")[1],
        "charging_strategy": one(values, "ChargingStrategy")[1],
        "discharging_strategy": one(values, "DischStrategy")[1],
        "backend": "pvsyst_native_ac_coupled",
    }
    state.update(_system_grid_limit(fields))
    for name, (key, low, high) in NUMERIC_FIELDS.items():
        try:
            raw = Decimal(one(values, key)[1])
            value = float(raw)
            if not raw.is_finite() or value == 0 and raw != 0:
                raise ValueError("Nonfinite or underflowed storage value")
        except (ValueError, InvalidOperation, OverflowError):
            raise ValueError("Invalid declared storage field: " + key) from None
        state[name] = finite(value, key, low, high if high is not None else float("inf"),
                             positive=name.endswith("kw") or name.endswith("pct"))
    if state["discharging_strategy"] == "MinMaxHour":
        for name in ("HourBegDisch", "HourEndDisch"):
            raw = one(values, name)[1]
            if not re.fullmatch(r"[0-9]+", raw) or not 0 <= int(raw) <= 23:
                raise ValueError(name + " must be a native hour in 0..23")
            state[name] = int(raw)
        if state["HourBegDisch"] == state["HourEndDisch"]:
            raise ValueError("Native discharge window endpoints must differ")

    series = _parse_par_simul_series(lines, bounds)
    if state["strategy"] == "PowerShift":
        if state["charging_strategy"] == "PVProdFraction":
            raw_frac = one(values, "ChargingPVFrac")[1]
            state["charging_pv_fraction"] = finite(float(Decimal(raw_frac)), "ChargingPVFrac", 0, 1, positive=True)
            if "ChargingPVFracParSim" in series:
                state["charging_schedule_48"] = series["ChargingPVFracParSim"]["schedule_48"]
        elif state["charging_strategy"] in ("SpecChargPower", "PVToGridMin"):
            raw_pwr = one(values, "ChargingPower")[1]
            positive = state["charging_strategy"] == "SpecChargPower"
            state["charging_power_kw"] = finite(float(Decimal(raw_pwr)), "ChargingPower", 0, math.inf, positive=positive)
            if "ChargingPowerParSim" in series:
                state["charging_schedule_48"] = series["ChargingPowerParSim"]["schedule_48"]
        if state["discharging_strategy"] in ("SpecifiedHours", "Night", "MinMaxHour"):
            raw_dis = one(values, "DischargingPower")[1]
            state["discharging_power_kw"] = finite(float(Decimal(raw_dis)), "DischargingPower", 0, math.inf, positive=True)
            if state["discharging_strategy"] == "SpecifiedHours" and "DischargingPowerParSim" in series:
                state["discharging_schedule_48"] = series["DischargingPowerParSim"]["schedule_48"]
    return state, bounds, values


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
    if state["strategy"] == "PeakShaving":
        if state.get("grid_limit_kw") is None and state.get("grid_limit_kva") is None:
            raise ValueError("PeakShaving requires an enabled PLimGrid")
    if state["strategy"] == "PowerShift":
        chg = state["charging_strategy"]
        if chg == "PVProdFraction":
            frac = state.get("charging_pv_fraction")
            if frac is None or not 0 < frac <= 1:
                raise ValueError("PowerShift PVProdFraction requires charging_pv_fraction in (0, 1]")
            sched = state.get("charging_schedule_48")
            if sched is not None and (len(sched) != 48 or any(not 0 <= v <= 1 for v in sched) or max(sched) <= 0):
                raise ValueError("PVProdFraction charging_schedule_48 must contain 48 slots in [0, 1] with at least one > 0")
        elif chg == "SpecChargPower":
            pwr = state.get("charging_power_kw")
            if pwr is None or pwr <= 0:
                raise ValueError("PowerShift SpecChargPower requires positive charging_power_kw")
            sched = state.get("charging_schedule_48")
            if sched is not None and (len(sched) != 48 or any(v < 0 for v in sched) or max(sched) <= 0):
                raise ValueError("SpecChargPower charging_schedule_48 must contain 48 non-negative slots with at least one > 0")
        elif chg == "PVToGridMin":
            pwr = state.get("charging_power_kw")
            if pwr is None or pwr < 0:
                raise ValueError("PowerShift PVToGridMin requires non-negative charging_power_kw")
            sched = state.get("charging_schedule_48")
            if sched is not None and (len(sched) != 48 or any(v < 0 for v in sched)):
                raise ValueError("PVToGridMin charging_schedule_48 must contain 48 non-negative slots")
        dis = state["discharging_strategy"]
        dpwr = state.get("discharging_power_kw")
        if dpwr is None or dpwr <= 0:
            raise ValueError(f"PowerShift {dis} requires positive discharging_power_kw")
        if dis == "SpecifiedHours":
            dsched = state.get("discharging_schedule_48")
            if dsched is not None and (len(dsched) != 48 or any(v < 0 for v in dsched) or max(dsched) <= 0):
                raise ValueError("SpecifiedHours discharging_schedule_48 must contain 48 non-negative slots with at least one > 0")


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
        disallowed = {
            "grid_limit_kw", "charging_pv_fraction", "charging_power_kw", "charging_schedule_48",
            "discharging_power_kw", "discharging_schedule_48", "discharge_start_hour", "discharge_end_hour",
        }
        present = disallowed & set(spec)
        if present:
            raise ValueError(f"{strategy} does not accept {sorted(present)}")
        return {"strategy": strategy, "charging_strategy": chg, "discharging_strategy": dis}

    if strategy == "PeakShaving":
        default_chg = base["charging_strategy"] if same_strategy else "PVaboveThresh"
        default_dis = base["discharging_strategy"] if same_strategy else "Immediate"
        chg = _map(spec.get("charging_strategy"), CHARGING_STRATEGIES, "charging_strategy", default_chg)
        dis = _map(spec.get("discharging_strategy"), DISCHARGING_STRATEGIES, "discharging_strategy", default_dis)
        if (strategy, chg, dis) not in KNOWN_PAIRS:
            raise ValueError(f"Unsupported PeakShaving pair ({chg}, {dis})")
        disallowed = {
            "charging_pv_fraction", "charging_power_kw", "charging_schedule_48",
            "discharging_power_kw", "discharging_schedule_48",
        }
        present = disallowed & set(spec)
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
        if dis == "MinMaxHour":
            sh = spec.get("discharge_start_hour", base.get("HourBegDisch") if same_strategy and base else None)
            eh = spec.get("discharge_end_hour", base.get("HourEndDisch") if same_strategy and base else None)
            if type(sh) is not int or type(eh) is not int or not (0 <= sh <= 23 and 0 <= eh <= 23) or sh == eh:
                raise ValueError("hour_window requires distinct integer discharge_start_hour and discharge_end_hour in 0..23")
            resolved.update(HourBegDisch=sh, HourEndDisch=eh)
        elif "discharge_start_hour" in spec or "discharge_end_hour" in spec:
            raise ValueError("discharge_start_hour/discharge_end_hour apply only to discharging_strategy='hour_window'")
        return resolved

    # PowerShift
    default_chg = base["charging_strategy"] if same_strategy else "PVProdFraction"
    default_dis = base["discharging_strategy"] if same_strategy else "SpecifiedHours"
    chg = _map(spec.get("charging_strategy"), CHARGING_STRATEGIES, "charging_strategy", default_chg)
    dis = _map(spec.get("discharging_strategy"), DISCHARGING_STRATEGIES, "discharging_strategy", default_dis)
    if (strategy, chg, dis) not in KNOWN_PAIRS:
        raise ValueError(f"Unsupported PowerShift pair ({chg}, {dis}); note AfterSunset is rejected by PVsyst for PowerShift")
    resolved = {"strategy": strategy, "charging_strategy": chg, "discharging_strategy": dis}
    if "grid_limit_kw" in spec:
        resolved["grid_limit_kw"] = finite(spec["grid_limit_kw"], "grid_limit_kw", 0, math.inf, positive=True)

    same_chg = same_strategy and chg == base["charging_strategy"]
    if chg == "PVProdFraction":
        if "charging_power_kw" in spec:
            raise ValueError("PVProdFraction uses charging_pv_fraction, not charging_power_kw")
        sched = spec.get("charging_schedule_48", base.get("charging_schedule_48") if same_chg else None)
        frac = spec.get("charging_pv_fraction", base.get("charging_pv_fraction") if same_chg else None)
        if sched is not None:
            if not isinstance(sched, list) or len(sched) != 48:
                raise ValueError("charging_schedule_48 must contain 48 values")
            sched = [finite(v, "charging_schedule_48 slot", 0, 1) for v in sched]
            if max(sched) <= 0:
                raise ValueError("PVProdFraction charging_schedule_48 requires at least one slot > 0")
            if frac is None:
                frac = max(sched)
        elif frac is not None:
            frac = finite(frac, "charging_pv_fraction", 0, 1, positive=True)
            sched = [frac] * 48
        else:
            raise ValueError("PowerShift PVProdFraction requires charging_pv_fraction and/or charging_schedule_48")
        frac = finite(frac, "charging_pv_fraction", 0, 1, positive=True)
        resolved.update(charging_pv_fraction=frac, charging_schedule_48=sched)
    else:
        if "charging_pv_fraction" in spec:
            raise ValueError(f"{chg} uses charging_power_kw, not charging_pv_fraction")
        positive = chg == "SpecChargPower"
        sched = spec.get("charging_schedule_48", base.get("charging_schedule_48") if same_chg else None)
        pwr = spec.get("charging_power_kw", base.get("charging_power_kw") if same_chg else None)
        if sched is not None:
            if not isinstance(sched, list) or len(sched) != 48:
                raise ValueError("charging_schedule_48 must contain 48 values")
            sched = [finite(v, "charging_schedule_48 slot", 0, math.inf) for v in sched]
            if positive and max(sched) <= 0:
                raise ValueError("SpecChargPower charging_schedule_48 requires at least one slot > 0")
            if pwr is None:
                pwr = max(sched)
        elif pwr is not None:
            pwr = finite(pwr, "charging_power_kw", 0, math.inf, positive=positive)
            sched = [pwr] * 48
        else:
            raise ValueError(f"PowerShift {chg} requires charging_power_kw and/or charging_schedule_48")
        pwr = finite(pwr, "charging_power_kw", 0, math.inf, positive=positive)
        resolved.update(charging_power_kw=pwr, charging_schedule_48=sched)

    same_dis = same_strategy and dis == base["discharging_strategy"]
    if dis == "SpecifiedHours":
        if "discharge_start_hour" in spec or "discharge_end_hour" in spec:
            raise ValueError("discharge_start_hour/discharge_end_hour apply only to hour_window")
        dsched = spec.get("discharging_schedule_48", base.get("discharging_schedule_48") if same_dis else None)
        dpwr = spec.get("discharging_power_kw", base.get("discharging_power_kw") if same_dis else None)
        if dsched is not None:
            if not isinstance(dsched, list) or len(dsched) != 48:
                raise ValueError("discharging_schedule_48 must contain 48 values")
            dsched = [finite(v, "discharging_schedule_48 slot", 0, math.inf) for v in dsched]
            if max(dsched) <= 0:
                raise ValueError("SpecifiedHours discharging_schedule_48 requires at least one slot > 0")
            if dpwr is None:
                dpwr = max(dsched)
        elif dpwr is not None:
            dpwr = finite(dpwr, "discharging_power_kw", 0, math.inf, positive=True)
            dsched = [dpwr] * 48
        else:
            raise ValueError("PowerShift SpecifiedHours requires discharging_power_kw and/or discharging_schedule_48")
        dpwr = finite(dpwr, "discharging_power_kw", 0, math.inf, positive=True)
        resolved.update(discharging_power_kw=dpwr, discharging_schedule_48=dsched)
    else:
        if "discharging_schedule_48" in spec:
            raise ValueError(f"discharging_schedule_48 applies only to discharging_strategy='specified_hours', not {dis}")
        dpwr = spec.get("discharging_power_kw", base.get("discharging_power_kw") if same_dis else None)
        if dpwr is None:
            raise ValueError(f"PowerShift {dis} requires discharging_power_kw")
        dpwr = finite(dpwr, "discharging_power_kw", 0, math.inf, positive=True)
        resolved["discharging_power_kw"] = dpwr
        if dis == "MinMaxHour":
            sh = spec.get("discharge_start_hour", base.get("HourBegDisch") if same_dis and base else None)
            eh = spec.get("discharge_end_hour", base.get("HourEndDisch") if same_dis and base else None)
            if type(sh) is not int or type(eh) is not int or not (0 <= sh <= 23 and 0 <= eh <= 23) or sh == eh:
                raise ValueError("hour_window requires distinct integer discharge_start_hour and discharge_end_hour in 0..23")
            resolved.update(HourBegDisch=sh, HourEndDisch=eh)
        elif "discharge_start_hour" in spec or "discharge_end_hour" in spec:
            raise ValueError("discharge_start_hour/discharge_end_hour apply only to discharging_strategy='hour_window'")
    return resolved


def render_grid_storage_block(state: dict, unavail_lines: list[str] | None = None) -> list[str]:
    """Render a complete flat/TParSimulSeries pvGridStorage block in PVsyst 8.1.7 order."""
    lines = [
        "  PVObject_GridStorage=pvGridStorage",
        "    Flags=$00",
        "    Strategy=" + state["strategy"],
        "    ChargingStrategy=" + state["charging_strategy"],
        "    DischStrategy=" + state["discharging_strategy"],
    ]
    if state["strategy"] == "PowerShift":
        if state["charging_strategy"] == "PVProdFraction":
            lines.extend(render_par_simul_series(
                "ChargingPVFracParSim", "FPVChMx", "Factor",
                "ChargingPVFrac", state["charging_pv_fraction"], state["charging_schedule_48"], maximum=1.0,
            ))
        else:
            lines.extend(render_par_simul_series(
                "ChargingPowerParSim", "EChgMax", "Power",
                "ChargingPower", state["charging_power_kw"], state["charging_schedule_48"],
            ))
        if state["discharging_strategy"] == "SpecifiedHours":
            lines.extend(render_par_simul_series(
                "DischargingPowerParSim", "EDisMax", "Power",
                "DischargingPower", state["discharging_power_kw"], state["discharging_schedule_48"],
            ))
        else:
            lines.append(f"    DischargingPower={decimal_text(state['discharging_power_kw'])}")
    if state["discharging_strategy"] == "MinMaxHour":
        lines.append(f"    HourBegDisch={state['HourBegDisch']}")
        lines.append(f"    HourEndDisch={state['HourEndDisch']}")
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
            raise ValueError(
                "PVsyst 8.1.7 only implements Common AC Bus grid storage (pvGridStorage); "
                "DC-coupled hybrid battery ports and DC clipping recovery are unsupported "
                "(use equivalent AC charger/inverter efficiencies with coupling='ac_coupled')"
            )
        clean = {k: v for k, v in updates.items() if k != "coupling"}
        if not clean and allow_grid_export is None:
            raise ValueError("Supply strict storage updates or explicit grid export control and boolean dry_run")
        if set(updates) - allowed_keys:
            raise ValueError("Unknown storage update fields")

    def build(*, text, lines, result, **_):
        _, system = store._sections(lines)
        before, bounds, fields = storage_state(lines, system)
        validate_storage(before, allow_weak=True, preserved=before)
        newline = "\r\n" if "\r\n" in text else "\n"

        scalar_edits = {}
        merged_state = dict(before)
        clean_updates = {k: v for k, v in updates.items() if k != "coupling"}
        for name, value in clean_updates.items():
            if name in NUMERIC_FIELDS:
                key, low, high = NUMERIC_FIELDS[name]
                num = finite(value, name, low, high if high is not None else float("inf"),
                             positive=name.endswith("kw") or name.endswith("pct"))
                scalar_edits[key] = decimal_text(num)
                merged_state[name] = num

        structural = bool(CONTROL_UPDATE_KEYS & set(clean_updates))
        if not structural:
            for key, value in scalar_edits.items():
                index, _ = one(fields, key)
                lines[index] = replace_value(lines[index], value)
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
            # Re-locate bounds in case set_system_grid_limit inserted lines before pvGridStorage.
            _, bounds, _ = storage_state(lines, store._sections(lines)[1])
            local_unavail = []
            if weak:
                # Preserve existing pvUnavail lines only if availability wasn't rejected; it will be replaced below.
                in_unavail = False
                for line in lines[bounds[0]:bounds[1] + 1]:
                    s = line.strip()
                    if s == "PVObject_GridUnavail=pvUnavail":
                        in_unavail = True
                    if in_unavail:
                        local_unavail.append(line.rstrip("\r\n"))
                    if s == "End of PVObject pvUnavail":
                        in_unavail = False
            rebuilt = [row + newline for row in render_grid_storage_block(merged_state, local_unavail)]
            lines[bounds[0]:bounds[1] + 1] = rebuilt
            _, system = store._sections(lines)
            after, bounds, _ = storage_state(lines, system)
            validate_storage(after, allow_weak=weak and availability is not None)

        if (availability is not None) != weak:
            raise ValueError("Supply a fresh complete availability declaration exactly when configuring WeakGrid")

        strip_result_cache(lines)
        _, bounds, _ = storage_state(lines, store._sections(lines)[1])

        candidate_text = "".join(lines)
        if weak:
            first_child = next(i for i, line in enumerate(lines[1:], 1) if line.strip().startswith("PVObject_"))
            root = direct_fields(lines, (0, first_child))
            root_flags = one(root, "Flags")[1]
            if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", root_flags) or not int(root_flags[1:], 16) & 0x80000:
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
                finish = ends[0].end() + local.startswith("\n", ends[0].end())
                local = local[:starts[0].start()] + profile + local[finish:]
            else:
                close = local.rindex("End of PVObject pvGridStorage")
                line_start = local.rfind("\n", 0, close) + 1
                local = local[:line_start] + profile + local[line_start:]
            candidate_text = "".join(lines[:bounds[0]]) + local + "".join(lines[bounds[1] + 1:])
            result["grid_availability"] = metadata

        def verify(checked, _, res):
            _, checked_system = store._sections(checked)
            verified, _, _ = storage_state(checked, checked_system)
            validate_storage(verified, allow_weak=availability is not None,
                             preserved=before if not structural else None)
            if verified != after:
                raise ValueError("Candidate did not retain the declared storage state")
            if structural:
                warn_schedule_quantization(merged_state, verified, res["warnings"])
            res.update(before=before, after=after)

        return {"candidate_text": candidate_text, "verify": verify}

    return run_new_variant_transaction(
        store,
        project=project,
        source_variant=source_variant,
        new_variant=new_variant,
        dry_run=dry_run,
        bom_display_target=new_variant,
        preflight=preflight,
        build=build,
        exists_prefix="Choose a new grid storage variant: ",
        source_changed_error="Storage template changed before creation",
        write_failure_warning="New variant may be a partial artifact; inspect the disclosed path. Original template is preserved.",
        extra_exceptions=(csv.Error, InvalidOperation),
        include_bom_added=False,
        uppercase_variant=False,
    )

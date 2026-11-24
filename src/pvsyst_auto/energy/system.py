"""Native AC system controls: grid storage system builder, AC cabling, power factor, grid limits, auxiliaries, and inverter temperature."""
from __future__ import annotations

import csv
import math
import re
from decimal import Decimal

from pvsyst_auto.components.editor import ASSIGNMENT, direct_fields, finite, has_object, objects, one, read_flat_grid_system, replace_value
from pvsyst_auto.energy.profiles import PEAK_SHAVING_LOAD_ERROR, hourly_load_block, load_snapshot, native_availability, upsert_load_block
from pvsyst_auto.energy.storage import (
    APPARENT, CONTROL_UPDATE_KEYS, ENABLED, INJECTION, NUMERIC_FIELDS, STRATEGIES, _system_grid_limit,
    decimal_text, has_user_load, render_grid_storage_block, resolve_storage_controls, set_grid_export,
    set_system_grid_limit, storage_state, validate_storage, warn_schedule_quantization,
)
from pvsyst_auto.native.grid_limit import grid_limit_state, replace_grid_limit
from pvsyst_auto.native.headless import strip_result_cache
from pvsyst_auto.project.circuit import circuit_nodes, positive_integer
from pvsyst_auto.project.paths import battery_reference_name
from pvsyst_auto.project.tracking import _decimal, _require_scope
from pvsyst_auto.project.variant_updates import run_new_variant_transaction
from pvsyst_auto.versions import NATIVE_TEXT, NATIVE_TEXT_OR_SEED, TEXT_LABEL

_HEX_FLAG = re.compile(r"\$[0-9a-fA-F]{1,8}")

# --- Shared system-block helper ---

def _apply_system_fields(lines, system, fields, new_flags: str | None, remove_keys, new_rows, read_fn, after, err_msg: str):
    """Replace Flags and owned fields inside a flat pvSystem block and verify the readback matches."""
    candidate = list(lines)
    if new_flags is not None:
        idx = one(fields, "Flags")[0]
        candidate[idx] = replace_value(candidate[idx], new_flags)
    remove = {row[0] for key in remove_keys for row in fields.get(key, [])}
    body = [candidate[i] for i in range(system[0] + 1, system[1]) if i not in remove]
    body.extend(new_rows)
    candidate[system[0] + 1:system[1]] = body
    if read_fn(candidate, (system[0], system[0] + len(body) + 1)) != after:
        raise ValueError(err_msg)
    lines[:] = candidate


def _system_eol_indent(lines, system):
    line = lines[system[0]]
    return ("\r\n" if line.endswith("\r\n") else "\n"), line[:len(line) - len(line.lstrip())] + "  "


def _step_limit(value, label, scale: int, minimum: float, maximum: float, step_err: str):
    num = finite(value, label, minimum=minimum, maximum=maximum)
    scaled = Decimal(str(num)) * scale
    if scaled != scaled.to_integral_value():
        raise ValueError(step_err)
    return num


def _native_parse(fn, raw, label: str, err: str | None = None):
    try:
        return fn(float(raw), label)
    except (ValueError, OverflowError):
        raise ValueError(err or f"Require a finite native {label}") from None


# --- Inverter temperature ---

_TEMP_FIELDS = ("TempShift", "TempFixed")
_TEMP_KNOWN = {"Comment", "Flags", "SystemType", *_TEMP_FIELDS}
_TEMP_FLAGS = {"ambient": "$00", "offset": "$4000", "fixed": "$8000"}


def _temp_limit(value, label):
    return _step_limit(value, label, 10, -100, 100, f"{label} must use 0.1 degree increments")


def _read_inverter_temp(lines, system):
    fields, index, flags = read_flat_grid_system(
        lines, system, feature_label="Inverter temperature editing", owned_fields=_TEMP_KNOWN,
        load_storage_error="Inverter temperature editing currently verifies PV-only variants",
        unknown_field_error="PF, grid limits or other system fields require separate review",
        hex_flag_error="Require native hexadecimal system Flags",
    )
    if flags & ~0xC000 or flags == 0xC000:
        raise ValueError("Require ambient, offset-only or fixed-only native system Flags")
    present = [one(fields, key, False) for key in _TEMP_FIELDS]
    if not flags:
        if any(entry is not None for entry in present):
            raise ValueError("An ambient system must not carry temperature fields")
        state = {"mode": "ambient", "offset_c": None, "fixed_c": None}
    else:
        if any(entry is None for entry in present):
            raise ValueError("A controlled system requires both temperature fields")
        vals = [_native_parse(_temp_limit, entry[1], key) for key, entry in zip(_TEMP_FIELDS, present)]
        state = {"mode": "offset" if flags == 0x4000 else "fixed", "offset_c": vals[0], "fixed_c": vals[1]}
    return fields, index, flags, state


def inverter_temperature_state(lines, system):
    try:
        return _read_inverter_temp(lines, system)[3]
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}


def replace_inverter_temperature(lines, system, updates):
    if not isinstance(updates, dict) or updates.get("mode") not in _TEMP_FLAGS:
        raise ValueError("Require inverter temperature mode ambient, offset or fixed")
    mode = updates["mode"]
    if set(updates) != ({"mode"} if mode == "ambient" else {"mode", "offset_c", "fixed_c"}):
        raise ValueError("Offset and fixed modes require explicit offset_c and fixed_c; ambient accepts no values")
    vals = [0.0, 0.0] if mode == "ambient" else [_temp_limit(updates["offset_c"], "offset_c"), _temp_limit(updates["fixed_c"], "fixed_c")]
    fields, _, _, before = _read_inverter_temp(lines, system)
    after = {"mode": mode, "offset_c": None if mode == "ambient" else vals[0], "fixed_c": None if mode == "ambient" else vals[1]}
    if after != before:
        eol, indent = _system_eol_indent(lines, system)
        rows = [] if mode == "ambient" else [f"{indent}{k}={Decimal(str(v)):.1f}{eol}" for k, v in zip(_TEMP_FIELDS, vals)]
        _apply_system_fields(lines, system, fields, _TEMP_FLAGS[mode], _TEMP_FIELDS, rows,
                             lambda c, s: _read_inverter_temp(c, s)[3], after, "Candidate inverter temperature state differs from the request")
    return {"before": before, "after": after}


# --- Power factor ---

_PF_FIELDS = ("TanPhi", "TanPhiM")
_PF_KNOWN = {"Comment", "Flags", "SystemType", *_PF_FIELDS}
_PF_BITS = {"constant": 0x00100000, "monthly": 0x00300000}
_PNOM_BITS = {"native": 0, "apparent": 0x00400000, "active": 0x00800000}


def _tan(value, label):
    return _step_limit(value, label, 1000, -1, 1, f"{label} must use 0.001 increments")


def _read_pf(lines, system):
    fields, index, flags = read_flat_grid_system(
        lines, system, feature_label="Power factor editing", owned_fields=_PF_KNOWN,
        load_storage_error="Power factor editing currently verifies PV-only variants",
        unknown_field_error="Storage, load, temperature, grid-limit or other system fields require separate review",
        hex_flag_error="Require native hexadecimal system Flags", exclusive_system_fields=_PF_FIELDS,
    )
    if flags & ~0x00F00000:
        raise ValueError("Only the PF and PNom bits are verified; other system bits require separate review")
    if flags & 0x00200000 and not flags & 0x00100000:
        raise ValueError("A monthly profile requires the constant PF bit as well")
    if flags & 0x00400000 and flags & 0x00800000:
        raise ValueError("Active and apparent PNom limits are mutually exclusive")
    present = [one(fields, key, False) for key in _PF_FIELDS]
    tan = monthly = None
    if not flags & 0x00100000:
        if any(entry is not None for entry in present):
            raise ValueError("A disabled power factor must not carry TanPhi fields")
        mode = "none"
    elif flags & 0x00200000:
        if present[0] is not None or present[1] is None:
            raise ValueError("A monthly profile requires exactly TanPhiM")
        monthly = [_native_parse(_tan, v, "TanPhiM") for v in present[1][1].removesuffix(",").split(",")]
        if len(monthly) != 12:
            raise ValueError("A monthly profile requires twelve TanPhiM values")
        mode = "monthly"
    else:
        if present[1] is not None or present[0] is None:
            raise ValueError("A constant profile requires exactly TanPhi")
        tan, mode = _native_parse(_tan, present[0][1], "TanPhi"), "constant"
    pnom = "apparent" if flags & 0x00400000 else "active" if flags & 0x00800000 else "native"
    if mode == "none" and pnom != "native":
        raise ValueError("A disabled power factor requires pnom_limit native")
    return fields, index, {"mode": mode, "tan_phi": tan, "monthly_tan_phi": monthly, "pnom_limit": pnom}


def power_factor_state(lines, system):
    """Semantic power-factor state; unsupported layouts report mode None and an issue."""
    try:
        return _read_pf(lines, system)[2]
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}


def replace_power_factor(lines, system, updates):
    """Write the verified native power-factor tags in place. Signed values are passed through."""
    if not isinstance(updates, dict) or updates.get("mode") not in ("none", "constant", "monthly"):
        raise ValueError("Require power factor mode none, constant or monthly")
    mode, pnom = updates["mode"], updates.get("pnom_limit")
    if not isinstance(pnom, str) or pnom not in _PNOM_BITS:
        raise ValueError("Require pnom_limit native, active or apparent")
    extra = {"tan_phi"} if mode == "constant" else {"monthly_tan_phi"} if mode == "monthly" else set()
    if set(updates) != {"mode", "pnom_limit"} | extra:
        raise ValueError("Provide only the values required by the chosen power factor mode")
    if mode == "none" and pnom != "native":
        raise ValueError("A disabled power factor requires pnom_limit native")
    tan = monthly = None
    if mode == "constant":
        tan = _tan(updates["tan_phi"], "tan_phi")
    elif mode == "monthly":
        supplied = updates["monthly_tan_phi"]
        if not isinstance(supplied, list) or len(supplied) != 12:
            raise ValueError("Monthly power factor requires twelve monthly_tan_phi values")
        monthly = [_tan(v, "monthly_tan_phi") for v in supplied]
    fields, _, before = _read_pf(lines, system)
    after = {"mode": mode, "tan_phi": tan, "monthly_tan_phi": monthly, "pnom_limit": pnom}
    if after != before:
        flags = "$00" if mode == "none" else "$%08X" % (_PF_BITS[mode] | _PNOM_BITS[pnom])
        eol, indent = _system_eol_indent(lines, system)
        rows = ([f"{indent}TanPhi={Decimal(str(tan)):.3f}{eol}"] if mode == "constant" else
                [f"{indent}TanPhiM={','.join(f'{Decimal(str(v)):.3f}' for v in monthly)},{eol}"] if mode == "monthly" else [])
        _apply_system_fields(lines, system, fields, flags, _PF_FIELDS, rows,
                             lambda c, s: _read_pf(c, s)[2], after, "Candidate power factor state differs from the request")
    return {"before": before, "after": after}


# --- Auxiliaries ---

_AUX_POWER_FIELDS = ("AuxPower_Const", "AuxPower_Night")
_AUX_PROFILE_FIELDS = ("AuxPower_CThresh", "AuxPower_Pout", "AuxPower_PThresh")
_AUX_OWNED = _AUX_POWER_FIELDS + _AUX_PROFILE_FIELDS
_AUX_STEPS = {"operating_kw": 100, "night_kw": 100, "operating_threshold_kw": 10,
              "proportional_w_per_kw": 10, "proportional_threshold_kw": 10}


def _coupled_p50(lines):
    return any(line.strip().startswith("P50_P90=") for line in lines)


def _profile_family(values):
    """Accept only the two verified native profile families, both with zero night power."""
    if values["night_kw"]:
        return None
    const, cthresh = values["operating_kw"], values["operating_threshold_kw"]
    pout, pthresh = values["proportional_w_per_kw"], values["proportional_threshold_kw"]
    if const > 0 and cthresh > 0 and not pout and not pthresh:
        return "constant"
    if not const and not cthresh and pout > 0 and pthresh >= 0:
        return "proportional"
    return None


def _read_aux(lines, system):
    fields, index, flags = read_flat_grid_system(
        lines, system, feature_label="Auxiliary editing", owned_fields=_AUX_OWNED,
        load_storage_error="Auxiliary editing currently verifies PV-only variants",
        unknown_field_error="PF, grid limits, coupled auxiliary or other system fields require separate review",
        hex_flag_error="Require verified auxiliary-only native system Flags", exclusive_system_fields=_AUX_OWNED,
    )
    if flags & ~0x00040000:
        raise ValueError("Require verified auxiliary-only native system Flags")
    native = {}
    for key in _AUX_OWNED:
        entry = one(fields, key, False)
        try:
            native[key] = finite(float(entry[1]), key) if entry else 0.0
        except (ValueError, OverflowError):
            raise ValueError(f"Require finite nonnegative native value for {key}") from None
    if not flags and any(key in fields for key in _AUX_OWNED):
        raise ValueError("Unverified inactive auxiliary cache; resave a native control first")
    operating, night = round(native["AuxPower_Const"] / 1000, 10), round(native["AuxPower_Night"] / 1000, 10)
    if any(key in fields for key in _AUX_PROFILE_FIELDS):
        state = {"mode": "profile", "operating_kw": operating, "night_kw": night,
                 "operating_threshold_kw": native["AuxPower_CThresh"],
                 "proportional_w_per_kw": native["AuxPower_Pout"], "proportional_threshold_kw": native["AuxPower_PThresh"]}
        if _profile_family(state) is None:
            raise ValueError("Profile auxiliary state is not one of the verified native families")
        if _coupled_p50(lines):
            raise ValueError("Coupled P50 parameter block with profile auxiliary fields requires separate review")
        return fields, index, flags, state
    return fields, index, flags, {"mode": "constant" if flags else "none", "operating_kw": operating, "night_kw": night}


def auxiliary_state(lines, system):
    try:
        return _read_aux(lines, system)[3]
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}


def replace_auxiliaries(lines, system, updates):
    if not isinstance(updates, dict) or updates.get("mode") not in ("none", "constant", "profile"):
        raise ValueError("Require auxiliary mode none, constant or profile")
    mode = updates["mode"]
    required = {"mode", *_AUX_STEPS} if mode == "profile" else ({"mode", "operating_kw", "night_kw"} if mode == "constant" else {"mode"})
    if set(updates) != required:
        raise ValueError("Profile mode requires all five explicit values; constant requires operating_kw and night_kw; none accepts no powers")
    values = {}
    for key, step in _AUX_STEPS.items():
        if key not in updates:
            values[key] = 0.0
            continue
        number = finite(updates[key], f"auxiliary {key}")
        if key in ("operating_kw", "night_kw"):
            finite(float(number) * 1000, f"native auxiliary watts for {key}")
        scaled = Decimal(str(number)) * step
        if scaled != scaled.to_integral_value():
            raise ValueError(f"Use {'0.01 kW' if step == 100 else '0.1 native unit'} increments for auxiliary {key}")
        values[key] = number
    if mode == "profile":
        if not any(values[key] for key in _AUX_STEPS):
            raise ValueError("Profile mode requires at least one non-zero threshold or proportional value; an all-zero profile must use constant mode")
        if _profile_family(values) is None:
            raise ValueError("Profile mode accepts only the verified native families: constant with threshold, or proportional")
        if _coupled_p50(lines):
            raise ValueError("Refusing profile auxiliary edit on a variant with a coupled P50 parameter block")
    fields, _, flags, before = _read_aux(lines, system)
    enabled = mode != "none"
    if mode == "profile":
        after = {"mode": "profile", **{k: values[k] for k in _AUX_STEPS}}
    else:
        after = {"mode": mode, "operating_kw": values["operating_kw"] if enabled else 0.0, "night_kw": values["night_kw"] if enabled else 0.0}
    if after != before:
        eol, indent = _system_eol_indent(lines, system)
        write = [("AuxPower_Const", Decimal(str(values["operating_kw"])) * 1000),
                 ("AuxPower_Night", Decimal(str(values["night_kw"])) * 1000)]
        if mode == "profile":
            write += [("AuxPower_CThresh", Decimal(str(values["operating_threshold_kw"]))),
                      ("AuxPower_Pout", Decimal(str(values["proportional_w_per_kw"]))),
                      ("AuxPower_PThresh", Decimal(str(values["proportional_threshold_kw"])))]
        rows = [f"{indent}{key}={val:.3f}{eol}" for key, val in write if val != 0]
        new_flags = ("$00040000" if enabled else "$00") if bool(flags) != enabled else None
        _apply_system_fields(
            lines, system, fields, new_flags, _AUX_OWNED, rows,
            lambda c, s: _read_aux(c, s)[3], after, "Candidate auxiliary state differs from the request",
        )
    return {"before": before, "after": after}


# --- Combined PF + grid limit ---

_PWR_COMMON = ("Comment", "Flags", "SystemType")
_PWR_PF_OWN = ("TanPhi", "TanPhiM")
_PWR_GRID_OWN = ("PLimGrid", "LimGrSeparLoss")
_PWR_ORDER = (*_PWR_PF_OWN, *_PWR_GRID_OWN)
_PWR_PF_BITS = 0x00F00000
_PWR_PF_MODE_BITS = {"none": 0x00000000, "constant": 0x00100000, "monthly": 0x00300000}
_PWR_LEGAL = frozenset((
    0x00000000, 0x00100000, 0x00300000, 0x00500000, 0x00900000, 0x04000000, 0x05000000,
    0x06000000, 0x06100000, 0x05100000, 0x07100000, 0x05300000, 0x07300000, 0x01100000,
    0x03100000, 0x05900000, 0x07900000, 0x05500000, 0x07500000, 0x00B00000, 0x00700000, 0x05B00000, 0x05700000,
))


def _pwr_owning(lines, system):
    if not isinstance(system, tuple) or len(system) != 2:
        raise ValueError("Require one owning native pvSystem")
    if any(line.strip().startswith("PVObject_") or line.strip() == "EndTags" for line in lines[system[0] + 1:system[1]]):
        raise ValueError("Require a flat native grid system")
    if has_object(lines, "pvLoad", "pvGridStorage"):
        raise ValueError("Load or storage coupling is outside this scope")
    if any(line.strip() == "P50_P90=TP50_P90" for line in lines):
        raise ValueError("P50 parameter coupling is outside this scope")
    fields = {}
    for i in range(system[0] + 1, system[1]):
        match = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))
        if match is None or match[2] in fields:
            raise ValueError("Unknown or duplicate line inside the owning system")
        fields[match[2]] = (i, match[4])
    if set(fields) - set(_PWR_COMMON) - set(_PWR_ORDER):
        raise ValueError("Unknown owning system fields require separate review")
    for key in ("Flags", "SystemType"):
        if key not in fields:
            raise ValueError(f"Require one owning {key}")
    if fields["SystemType"][1] != "Grid":
        raise ValueError("Require SystemType=Grid")
    totals = dict.fromkeys(_PWR_ORDER, 0)
    for line in lines:
        match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if match is not None and match[2] in totals:
            totals[match[2]] += 1
    for key, total in totals.items():
        if total != (1 if key in fields else 0):
            raise ValueError(f"Require {key} only in the owning system")
    raw = fields["Flags"][1]
    if not _HEX_FLAG.fullmatch(raw):
        raise ValueError("Require hexadecimal owning Flags")
    flags = int(raw[1:], 16)
    if flags not in _PWR_LEGAL:
        raise ValueError("Unverified native flag family for a combined control")
    if not flags & ENABLED and any(key in fields for key in _PWR_GRID_OWN):
        raise ValueError("A disabled grid limit must not carry PLimGrid or LimGrSeparLoss")
    return fields, flags


def _masked_view(lines, system, keep, own):
    view, delta = list(lines), 0
    for i in range(system[0] + 1, system[1]):
        match = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))
        key = match[2]
        if key == "Flags":
            masked = int(match[4][1:], 16) & keep
            view[i] = replace_value(lines[i], "$%08X" % masked if masked else "$00")
        elif key not in _PWR_COMMON and key not in own:
            view[i] = None
            delta -= 1
    return [line for line in view if line is not None], (system[0], system[1] + delta)


def _own_lines(view):
    own = {}
    for line in view:
        match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if match and match[2] in _PWR_ORDER:
            own[match[2]] = match[4]
    return own


def _coupling(power_factor, grid):
    mode, pnom = power_factor["mode"], power_factor["pnom_limit"]
    if grid["mode"] != "constant":
        return
    strict = mode == "constant" and pnom == "native"
    separate, point = grid["separate_loss"], grid["point"]
    if not separate and not (strict and point == "injection"):
        raise ValueError("A limit without separate loss is verified only for injection with native constant PF")
    if point == "inverter" and not (strict and separate):
        raise ValueError("An inverter-point limit requires constant PF, native PNom and separate loss")
    if mode == "monthly" and not (point == "injection" and separate):
        raise ValueError("Monthly PF is verified only with an injection-point limit and separate loss")
    if pnom != "native" and not (point == "injection" and separate):
        raise ValueError("A Legacy PNom limit is verified only with injection and separate loss")
    if mode == "none" and not (grid["quantity"] == "active" and point == "injection" and separate):
        raise ValueError("A limit without PF is verified only for active injection with separate loss")


def _pwr_parts(lines, system):
    _, flags = _pwr_owning(lines, system)
    pf_view, pf_bounds = _masked_view(lines, system, _PWR_PF_BITS, _PWR_PF_OWN)
    grid_view, grid_bounds = _masked_view(lines, system, ENABLED | INJECTION, _PWR_GRID_OWN)
    pf = power_factor_state(pf_view, pf_bounds)
    raw_grid = grid_limit_state(grid_view, grid_bounds)
    grid = raw_grid if raw_grid.get("mode") is None else {
        "mode": raw_grid["mode"], "limit": raw_grid["limit_kw"],
        "quantity": "apparent" if flags & APPARENT else "active",
        "point": raw_grid["point"], "separate_loss": raw_grid["separate_loss"],
    }
    if pf.get("mode") is None or grid.get("mode") is None:
        raise ValueError(pf.get("issue") or grid.get("issue"))
    if grid["mode"] == "constant" and type(grid["separate_loss"]) is not bool:
        raise ValueError("An enabled limit requires explicit native LimGrSeparLoss")
    _coupling(pf, grid)
    return pf, grid, pf_view, pf_bounds, grid_view, grid_bounds


def power_controls_state(lines, system):
    """Semantic state of both controls; unusable input reports mode None plus an issue."""
    if not isinstance(system, tuple) or len(system) != 2:
        return {"mode": None, "issue": "Require one owning native pvSystem"}
    try:
        pf, grid, *_ = _pwr_parts(lines, system)
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}
    if pf.get("mode") is None or grid.get("mode") is None:
        return {"mode": None, "issue": pf.get("issue") or grid.get("issue")}
    return {"mode": "configured", "power_factor": pf, "grid_limit": grid}


def replace_power_controls(lines, system, updates):
    """Validate everything first, then rewrite the single owning system once, in place."""
    if not isinstance(updates, dict) or set(updates) != {"power_factor", "grid_limit"}:
        raise ValueError("Require exactly power_factor and grid_limit")
    if not isinstance(system, tuple) or len(system) != 2:
        raise ValueError("Require one owning native pvSystem")
    grid = updates["grid_limit"]
    if not isinstance(grid, dict) or grid.get("mode") not in ("none", "constant"):
        raise ValueError("Require grid limit mode none or constant")
    keys = {"mode", "limit", "quantity", "point", "separate_loss"} if grid["mode"] == "constant" else {"mode", "quantity", "point"}
    if set(grid) != keys:
        raise ValueError("Constant needs limit/quantity/point/separate_loss; none needs quantity/point")
    if grid["quantity"] not in ("active", "apparent") or grid["point"] not in ("injection", "inverter"):
        raise ValueError("Require quantity active or apparent and point injection or inverter")
    if grid["mode"] == "constant" and type(grid["separate_loss"]) is not bool:
        raise ValueError("separate_loss must be a strict boolean")
    eol, indent = _system_eol_indent(lines, system)
    pf, grid_before, pf_view, pf_bounds, grid_view, grid_bounds = _pwr_parts(lines, system)
    if pf.get("mode") is None:
        raise ValueError(pf.get("issue", "Unsupported power factor source"))
    pf_after = replace_power_factor(pf_view, pf_bounds, updates["power_factor"])["after"]
    replace_grid_limit(grid_view, grid_bounds, {"mode": "constant", "limit_kw": grid["limit"]} if grid["mode"] == "constant" else {"mode": "none"})
    grid_after = {
        "mode": grid["mode"], "limit": grid["limit"] if grid["mode"] == "constant" else None,
        "quantity": grid["quantity"], "point": grid["point"],
        "separate_loss": grid["separate_loss"] if grid["mode"] == "constant" else None,
    }
    _coupling(pf_after, grid_after)
    flags = (_PWR_PF_MODE_BITS[pf_after["mode"]] | _PNOM_BITS[pf_after["pnom_limit"]]
             | (ENABLED if grid["mode"] == "constant" else 0)
             | (APPARENT if grid["quantity"] == "apparent" else 0)
             | (INJECTION if grid["point"] == "injection" else 0))
    if flags not in _PWR_LEGAL:
        raise ValueError("Combined control does not match a verified native flag family")
    before, after = {"power_factor": pf, "grid_limit": grid_before}, {"power_factor": pf_after, "grid_limit": grid_after}
    if before == after:
        return {"before": before, "after": after}
    rendered = {k: v for k, v in _own_lines(pf_view).items()
                if (k == "TanPhi" and pf_after["mode"] == "constant") or (k == "TanPhiM" and pf_after["mode"] == "monthly")}
    if grid["mode"] == "constant":
        rendered["PLimGrid"] = _own_lines(grid_view)["PLimGrid"]
        rendered["LimGrSeparLoss"] = "True" if grid["separate_loss"] else "False"
    body = []
    for i in range(system[0] + 1, system[1]):
        key = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))[2]
        if key not in _PWR_ORDER:
            body.append(replace_value(lines[i], "$%08X" % flags if flags else "$00") if key == "Flags" else lines[i])
    body.extend(f"{indent}{key}={rendered[key]}{eol}" for key in _PWR_ORDER if key in rendered)
    candidate = list(lines)
    candidate[system[0] + 1:system[1]] = body
    if power_controls_state(candidate, (system[0], system[0] + len(body) + 1)) != dict(mode="configured", **after):
        raise ValueError("Candidate combined control state differs from the request")
    lines[:] = candidate
    return {"before": before, "after": after}


# --- Per-inverter AC cabling ---

AC_FLAG = 0x200000
AC_FIELDS = ("ACWireNoSect", "ACWireLength", "ACWireMetal")
METALS = ("Copper", "Aluminium")
SECTIONS = tuple("ISO60228_" + v for v in ("10", "16", "25", "35", "50", "70", "95", "120"))


def _ac_length(raw):
    value = _decimal(str(raw), "AC length_m")
    try:
        stepped = value.quantize(Decimal("0.1"))
    except ArithmeticError:
        raise ValueError("AC length is outside the supported decimal range") from None
    if not 0 < value <= 100000 or value != stepped:
        raise ValueError("AC length must be positive, at most 100000 m and in exact 0.1 m steps")
    return value


def _header_cable(lines, node):
    stack, spans = [], []
    for index in range(node.line, node.end_line - 1):
        match = ASSIGNMENT.fullmatch(lines[index].rstrip("\r\n"))
        if not match:
            if lines[index].strip():
                raise ValueError("Unexpected inverter header record")
            continue
        key, value = match[2], match[4]
        if key == "Children" and value == "Start" and not stack:
            break
        if value == "Start":
            if key == "CableDefinition" and stack:
                raise ValueError("AC cable must be directly owned by its inverter")
            stack.append((key, index))
        elif value == "End":
            if not stack or stack[-1][0] != key:
                raise ValueError("Unmatched inverter metadata")
            name, start = stack.pop()
            if name == "CableDefinition":
                spans.append((start, index))
    if stack or len(spans) > 1:
        raise ValueError("Ambiguous or incomplete inverter AC cable")
    if not spans:
        return None
    start, end = spans[0]
    tokens = [line.strip() for line in lines[start:end + 1]]
    if (len(tokens) != 8 or tokens[0] != "CableDefinition=Start" or tokens[1] != "CustomResistance=False"
            or tokens[3] != "CableType=Start" or tokens[6:] != ["CableType=End", "CableDefinition=End"]):
        raise ValueError("Only the recorded native noncustom AC cable shape is supported")
    length, metal, section = re.fullmatch(r"Length=(.+) m", tokens[2]), re.fullmatch(r"Material=(\w+)", tokens[4]), re.fullmatch(r"StdCrossSect=(\w+)", tokens[5])
    if not length or not metal or not section or metal[1] not in METALS or section[1] not in SECTIONS:
        raise ValueError("Unknown AC cable units, conductor or section")
    return {"length": _ac_length(length[1]), "metal": metal[1], "section": section[1], "start": start, "end": end}


def _layout(lines):
    blocks = objects("".join(lines))
    mains = [b for b in blocks if b.object_type == "pvMainArray"]
    subs = [b for b in blocks if b.object_type == "pvSubArray"]
    circuits = [b for b in blocks if b.object_type == "pvCircuit"]
    if (len(mains) != 1 or mains[0].path != "PVObject_PVMainArray" or not mains[0].end_line
            or len(subs) != 1 or not subs[0].end_line or subs[0].path != "PVObject_PVMainArray/PVObject_"
            or len(circuits) != 1 or circuits[0].path != "PVObject_PVMainArray/PVObject_SystemCircuit" or not circuits[0].end_line):
        raise ValueError("Requires one complete owning main array, subarray and native circuit")
    main, sub = mains[0], subs[0]
    raw_flags = main.one("Flags")[1]
    if not _HEX_FLAG.fullmatch(raw_flags):
        raise ValueError("Main array flags must be native hex")
    active = bool(int(raw_flags[1:], 16) & AC_FLAG)
    nodes, transformers = [], []

    def walk(node):
        if node.kind == "InverterNode":
            nodes.append(node)
        if node.kind == "TransformerNode":
            transformers.append(node)
        for child in node.children:
            walk(child)

    for root in circuit_nodes(lines):
        walk(root)
    if not nodes:
        raise ValueError("Requires native inverter nodes")
    cables = [_header_cable(lines, node) for node in nodes]
    declared = [name in main.fields for name in AC_FIELDS]
    if active:
        if not all(declared) or any(cable is None for cable in cables):
            raise ValueError("Enabled AC requires all main and per-inverter cable declarations")
        length, metal = _ac_length(sub.one("ACWireLength")[1]), main.one("ACWireMetal")[1]
        if main.one("ACWireNoSect")[1] != "0" or main.one("ACWireLength")[1] != "0.0":
            raise ValueError("Only the recorded per-inverter AC summary form is supported")
        section = cables[0]["section"]
        if any((c["length"], c["metal"], c["section"]) != (length, metal, section) for c in cables):
            raise ValueError("Source AC inverter cables and subarray summary must agree; no mixed override")
    else:
        if any(declared) or any(c is not None for c in cables) or _decimal(sub.one("ACWireLength")[1], "ACWireLength") != 0:
            raise ValueError("Inactive AC must not carry partial or dormant cable declarations")
        length = metal = section = None
    state = {"mode": "active" if active else "none", "length_m": float(length) if active else None,
             "metal": metal, "section": section, "inverter_groups": len(nodes),
             "inverter_count": sum(positive_integer(n.one("NElements", "1"), "inverter count") for n in nodes)}
    return state, main, sub, nodes, cables, transformers, circuits[0]


def ac_cabling_state(lines):
    try:
        return _layout(lines)[0]
    except (ValueError, TypeError, KeyError, StopIteration) as exc:
        return {"mode": "unsupported", "issues": [str(exc)]}


def _ac_profile(store, lines):
    arrays, system = store._sections(lines)
    _require_scope(store, lines, arrays, system, main_flags=("$01900400", "$01B00400"), system_flags=("$00",))
    store._ordinary_fixed_plane(lines, store._orientation(lines, 1))
    layout = _layout(lines)
    if layout[5] or layout[1].one("MVNbTransfos")[1] != "0":
        raise ValueError("AC edits with MV/HV references are outside the verified profile")
    if layout[6].one("Version")[1] not in NATIVE_TEXT or layout[6].one("Flags")[1] != "$00":
        raise ValueError(f"Requires native {TEXT_LABEL} circuit Flags=$00")
    return layout


def create_ac_cabling_variant(store, *, project, source_variant, new_variant, ac,
                              dry_run=True, variant_name=None, add_utf8_bom=False):
    parsed = {}

    def preflight():
        if not isinstance(ac, dict) or set(ac) - {"length_m", "metal", "section"} or "length_m" not in ac:
            raise ValueError("Declare AC length_m and optional metal/section only")
        if type(ac["length_m"]) is bool or not isinstance(ac["length_m"], (int, float)):
            raise ValueError("AC length_m must be a strict finite number")
        parsed["length"] = _ac_length(ac["length_m"])

    def build(*, text, lines, **_):
        length = parsed["length"]
        state, main, sub, nodes, cables, _, _ = _ac_profile(store, lines)
        metal = ac.get("metal", state["metal"] if state["mode"] == "active" else "Copper")
        section = ac.get("section", state["section"] if state["mode"] == "active" else "ISO60228_10")
        if metal not in METALS or section not in SECTIONS:
            raise ValueError("Use recorded native metal and standard section identifiers")
        newline = "\r\n" if "\r\n" in text else "\n"
        edits = [
            (main.one("Flags")[0], main.one("Flags")[0] + 1, [replace_value(lines[main.one("Flags")[0]], "$01B00400")]),
            (sub.one("ACWireLength")[0], sub.one("ACWireLength")[0] + 1, [replace_value(lines[sub.one("ACWireLength")[0]], f"{length:.1f}")]),
        ]
        if state["mode"] == "active":
            idx = main.one("ACWireMetal")[0]
            edits.append((idx, idx + 1, [replace_value(lines[idx], metal)]))
        else:
            anchor_field = next((k for k in ("UFAverEffic", "MVNbTransfos", "Flags2", "Flags") if k in main.fields), None)
            if anchor_field is None:
                raise ValueError("Main array lacks an anchor field for AC cable declarations")
            idx = main.one(anchor_field)[0] + 1
            indent = re.match(r"\s*", lines[idx - 1]).group(0)
            edits.append((idx, idx, [f"{indent}{k}={v}{newline}" for k, v in (("ACWireNoSect", "0"), ("ACWireLength", "0.0"), ("ACWireMetal", metal))]))
        for node, cable in zip(nodes, cables):
            if "NElements" in node.field_lines:
                anchor, = node.field_lines["NElements"]
                indent = re.match(r"[ \t]*", lines[anchor]).group(0)
            elif node.field_lines:
                anchor = max(idx for indices in node.field_lines.values() for idx in indices)
                indent = re.match(r"[ \t]*", lines[anchor]).group(0)
            else:
                anchor = node.line - 1
                indent = re.match(r"[ \t]*", lines[anchor]).group(0) + "  "
            replacement = [indent + line + newline for line in (
                "CableDefinition=Start", "    CustomResistance=False", f"    Length={format(length.normalize(), 'f')} m",
                "    CableType=Start", f"        Material={metal}", f"        StdCrossSect={section}",
                "    CableType=End", "CableDefinition=End")]
            start, end = (cable["start"], cable["end"] + 1) if cable else (anchor + 1, anchor + 1)
            edits.append((start, end, replacement))
        candidate_lines = list(lines)
        for start, end, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
            candidate_lines[start:end] = replacement

        def verify(checked, _, res):
            verified = _ac_profile(store, checked)[0]
            if verified != dict(state, mode="active", length_m=float(length), metal=metal, section=section):
                raise ValueError("Candidate AC owners did not read back the declared values")
            res["ac"] = verified
            res["warnings"].append("Native AC cable metadata only; source, StringNode DC resistance, hardware and all unrelated records remain inherited. No resistance/loss calculation or MV/HV coupling is added.")

        return {"candidate_lines": candidate_lines, "verify": verify}

    return run_new_variant_transaction(
        store, project=project, source_variant=source_variant, new_variant=new_variant,
        dry_run=dry_run, add_utf8_bom=add_utf8_bom, variant_name=variant_name, preflight=preflight,
        build=build, source_changed_error="AC source changed before creation",
        write_failure_warning="A partial new artifact may remain; inspect its disclosed path. Source retained.",
        extra_exceptions=(ArithmeticError,),
    )


# --- Create AC grid storage system ---

def create_grid_storage_system(store, *, project, source_variant, new_variant, battery,
                               storage, load_manifest_name=None, dry_run=True,
                               availability=None, allow_grid_export=None):
    parsed = {}

    def preflight():
        req_batt = {"file_name", "series_count", "parallel_count", "monthly_temperature_c", "cycle_sow", "static_sow"}
        if not isinstance(battery, dict) or not req_batt <= set(battery) or set(battery) - (req_batt | {"library"}):
            raise ValueError("Complete explicit battery declaration is required")
        battery_reference_name(battery["file_name"])
        batt_lib = battery.get("library", "workspace")
        if batt_lib not in ("workspace", "builtin"):
            raise ValueError("battery.library must be 'workspace' or 'builtin'")
        if not isinstance(storage, dict):
            raise ValueError("Complete explicit native storage declaration is required")
        coupling = storage.get("coupling")
        if coupling is not None and coupling != "ac_coupled":
            raise ValueError("PVsyst 8.1.7 only implements Common AC Bus grid storage (pvGridStorage); DC-coupled hybrid battery ports and DC clipping recovery are unsupported (use equivalent AC charger/inverter efficiencies with coupling='ac_coupled')")
        storage_clean = {k: v for k, v in storage.items() if v is not None and k != "coupling"}
        if not (set(NUMERIC_FIELDS) | {"strategy"}) <= set(storage_clean) or set(storage_clean) - (set(NUMERIC_FIELDS) | CONTROL_UPDATE_KEYS):
            raise ValueError("Complete explicit battery and native storage declarations are required")
        for key in ("series_count", "parallel_count"):
            if type(battery[key]) is not int or not 1 <= battery[key] <= 10000:
                raise ValueError(key + " must be a bounded positive integer")
        temps = battery["monthly_temperature_c"]
        if not isinstance(temps, list) or len(temps) != 12:
            raise ValueError("Declare all twelve battery monthly temperatures")
        temps = [finite(value, "monthly battery temperature", -50, 80) for value in temps]
        cycle, static = finite(battery["cycle_sow"], "cycle_sow", 0, 1, positive=True), finite(battery["static_sow"], "static_sow", 0, 1, positive=True)
        strat_key = storage_clean.get("strategy")
        if strat_key not in STRATEGIES:
            raise ValueError("Use a verified native self_consumption, weak_grid, peak_shaving, or power_shift strategy")
        weak = strat_key == "weak_grid"
        if (availability is not None) != weak:
            raise ValueError("Supply complete availability exactly when creating WeakGrid")
        if strat_key in ("self_consumption", "weak_grid") and not load_manifest_name:
            raise ValueError(f"{strat_key} requires an explicit load_manifest_name")
        if strat_key == "peak_shaving" and load_manifest_name is not None:
            raise ValueError(PEAK_SHAVING_LOAD_ERROR)
        preview = resolve_storage_controls(storage_clean, inherited_limit={"grid_limit_kw": 1.0})
        state = {k: preview[k] for k in ("strategy", "charging_strategy", "discharging_strategy")}
        for name, (_, low, high) in NUMERIC_FIELDS.items():
            state[name] = finite(storage_clean[name], name, low, high if high is not None else math.inf, positive=name.endswith(("kw", "pct")))
        validate_storage({**state, **preview}, allow_weak=weak)
        parsed.update(temperatures=temps, cycle=cycle, static=static, weak=weak, batt_lib=batt_lib, storage_clean=storage_clean, base_state=state)

    def build(*, text, lines, result, **_):
        temps, cycle, static = parsed["temperatures"], parsed["cycle"], parsed["static"]
        weak, batt_lib, storage_clean, base_state = parsed["weak"], parsed["batt_lib"], parsed["storage_clean"], parsed["base_state"]
        _, system = store._sections(lines)
        first_child = next(i for i, line in enumerate(lines[1:], 1) if line.strip().startswith("PVObject_"))
        root_fields = direct_fields(lines, (0, first_child))
        if one(root_fields, "Version")[1] not in NATIVE_TEXT_OR_SEED or system is None:
            raise ValueError("A complete verified-version PV-only Grid template is required")
        system_fields = direct_fields(lines, system)
        if one(system_fields, "SystemType")[1] != "Grid":
            raise ValueError("SystemType must be Grid")
        if any("PVObject_GridStorage=" in line for line in lines) or any(k in system_fields for k in ("BatteryFile", "NBattSerie", "NBattParall")):
            raise ValueError("Use the existing-storage editor for a template that already declares battery/storage hardware")
        if weak and any(line.strip().startswith("PVObject_GridUnavail=") for line in lines):
            raise ValueError("Preserve the source's existing grid-unavailability object instead of adding a duplicate")
        if base_state["strategy"] == "PeakShaving" and has_user_load(lines):
            raise ValueError(PEAK_SHAVING_LOAD_ERROR)
        inherited_limit = _system_grid_limit(system_fields) if base_state["strategy"] == "PeakShaving" and "grid_limit_kw" not in storage_clean else None
        controls = resolve_storage_controls(storage_clean, inherited_limit=inherited_limit)
        state = {**base_state, **controls}
        validate_storage(state, allow_weak=weak)
        if batt_lib == "workspace":
            component_path = store.components._path("BTR", battery["file_name"])
            store.components._require_valid(store.components._inspect("BTR", store.components._read(component_path)), complete=True)
            battery_artifact = str(component_path)
        else:
            store.components._path("BTR", battery["file_name"])
            if store.components.builtin is not None and (bp := store.components._path("BTR", battery["file_name"], "builtin")).is_file():
                store.components._require_valid(store.components._inspect("BTR", store.components._read(bp)), complete=True)
            battery_artifact = f"builtin:{battery['file_name']}"
        newline = "\r\n" if "\r\n" in text else "\n"
        load_record, load_profile, load_values = load_snapshot(store.workspace, load_manifest_name) if load_manifest_name is not None else (None, None, None)
        grid_profile = ""
        if weak:
            grid_profile, grid_metadata = native_availability(store.workspace, availability, newline)
            if grid_metadata["year"] != load_record["year"] or grid_metadata["utc_offset_hours"] != load_record["utc_offset_hours"]:
                raise ValueError("Grid availability and prepared load must match year and standard UTC offset")
            result["grid_availability"] = grid_metadata
        root_index, root_flags = one(root_fields, "Flags")
        system_index, system_flags = one(system_fields, "Flags")
        for flags in (root_flags, system_flags):
            if not _HEX_FLAG.fullmatch(flags):
                raise ValueError("Owning flags must be unambiguous hexadecimal values")
        if load_record is not None:
            lines[root_index] = replace_value(lines[root_index], f"${int(root_flags[1:], 16) | 0x00080000:08X}")
        lines[system_index] = replace_value(lines[system_index], f"${int(system_flags[1:], 16) | 0x10000040:08X}")
        set_grid_export(lines, system, allow_grid_export)
        if "grid_limit_kw" in storage_clean:
            set_system_grid_limit(lines, system, controls["grid_limit_kw"])
            _, system = store._sections(lines)
            system_fields = direct_fields(lines, system)
        additions = {
            "BatteryFile": battery["file_name"], "NBattSerie": str(battery["series_count"]), "NBattParall": str(battery["parallel_count"]),
            "CycleSOWBeg": decimal_text(cycle), "StaticSOWBeg": decimal_text(static), "TypeTempBatt": "1",
            "TempBattRef": decimal_text(temps[0]), "TempBattMois": ",".join(decimal_text(v) for v in temps) + ",",
        }
        new_fields = []
        for key, value in additions.items():
            if existing := one(system_fields, key, False):
                lines[existing[0]] = replace_value(lines[existing[0]], value)
            else:
                new_fields.append(f"    {key}={value}{newline}")
        lines[system[1]:system[1]] = new_fields
        strip_result_cache(lines)
        candidate_text = "".join(lines)
        load, encoding = "", None
        if load_record is not None:
            load, encoding = hourly_load_block(load_profile, load_values, load_record["year"], newline)
            candidate_text, replaced = upsert_load_block(candidate_text, load)
            if replaced:
                load = ""
        storage_lines = render_grid_storage_block(state, grid_profile.splitlines() if grid_profile else None)
        closing = candidate_text.rfind("\n", 0, candidate_text.rindex("End of PVObject pvVCalcul")) + 1
        candidate_text = candidate_text[:closing] + load + newline.join(storage_lines) + newline + candidate_text[closing:]

        def verify(checked, _, res):
            configured, _, _ = storage_state(checked, store._sections(checked)[1])
            validate_storage(configured, allow_weak=weak)
            warn_schedule_quantization(state, configured, res["warnings"])
            artifacts = {"battery": battery_artifact, **({"load_csv": str(load_profile)} if load_profile is not None else {})}
            res.update(artifacts=artifacts, battery_library=batt_lib, configuration=configured,
                       battery_temperature_monthly_c=temps, cycle_sow=cycle, static_sow=static)
            if encoding is not None:
                res.update(load_encoding=encoding, year=load_record["year"], utc_offset_hours=load_record["utc_offset_hours"])
            if batt_lib == "builtin":
                res["warnings"].append("Builtin BTR model is resolved from PVsyst's embedded database at simulation time; verify with pvsyst_run_simulation.")

        return {"candidate_text": candidate_text, "verify": verify}

    return run_new_variant_transaction(
        store, project=project, source_variant=source_variant, new_variant=new_variant,
        dry_run=dry_run, bom_display_target=new_variant, preflight=preflight, build=build,
        initial_fields={"backend": "pvsyst_native_ac_coupled"}, exists_prefix="Choose a new storage system variant: ",
        source_changed_error="A source battery/load/template changed before creation",
        write_failure_warning="Inspect the disclosed new variant artifact before retrying; original PV template and input files remain preserved.",
        extra_exceptions=(csv.Error,), uppercase_variant=False,
    )

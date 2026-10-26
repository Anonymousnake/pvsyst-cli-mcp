"""Native 8.1.6 whole-system auxiliary powers.

Reads and writes the owning ``pvSystem`` constant operating/night powers plus the
verified auxiliary threshold and proportional families, with no load calculation.
"""
from __future__ import annotations

import re
from decimal import Decimal

from pvsyst_auto.components.editor import ASSIGNMENT, replace_value
from pvsyst_auto.components.generator import direct_fields, finite, one

POWER_FIELDS = ("AuxPower_Const", "AuxPower_Night")
# L/M-verified native fields; a written file's absence is the serializer's own default
PROFILE_FIELDS = ("AuxPower_CThresh", "AuxPower_Pout", "AuxPower_PThresh")
OWNED_FIELDS = POWER_FIELDS + PROFILE_FIELDS
POWER_FIELDS_KW = ("operating_kw", "night_kw")
# 0.01 kW steps for the two kW powers, native-unit 0.1 steps for the other three
STEPS = {"operating_kw": 100, "night_kw": 100, "operating_threshold_kw": 10,
         "proportional_w_per_kw": 10, "proportional_threshold_kw": 10}
PROFILE_REQUEST_KEYS = {"mode", *STEPS}
P50_MARKER = "P50_P90="


def _coupled_p50(lines):
    return any(line.strip().startswith(P50_MARKER) for line in lines)


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


def _read(lines, system):
    if system is None:
        raise ValueError("Require one owning native pvSystem")
    first = next((i for i, line in enumerate(lines[1:], 1) if line.strip().startswith("PVObject_")), len(lines) - 1)
    if one(direct_fields(lines, (0, first)), "Version")[1] != "8.1.6":
        raise ValueError("Auxiliary editing requires variant Version=8.1.6")
    if any("pvLoad" in line or "pvGridStorage" in line for line in lines):
        raise ValueError("Auxiliary editing currently verifies PV-only variants")
    if any(line.strip().startswith("PVObject_") or line.strip() == "EndTags" for line in lines[system[0] + 1:system[1]]):
        raise ValueError("Require a flat native grid system")
    fields = direct_fields(lines, system)
    if set(fields) - {"Comment", "Flags", "SystemType", *OWNED_FIELDS}:
        raise ValueError("PF, grid limits, coupled auxiliary or other system fields require separate review")
    for key in fields:
        one(fields, key)
    if one(fields, "SystemType")[1] != "Grid":
        raise ValueError("Auxiliary editing requires SystemType=Grid")
    index, raw = one(fields, "Flags")
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw) or int(raw[1:], 16) & ~0x00040000:
        raise ValueError("Require verified auxiliary-only native system Flags")
    flags = int(raw[1:], 16)
    native = {}
    for key in OWNED_FIELDS:
        total = sum(m is not None and m[2] == key for line in lines
                    for m in [ASSIGNMENT.fullmatch(line.rstrip("\r\n"))])
        if total != len(fields.get(key, [])):
            raise ValueError(f"Require {key} only in the owning system")
        entry = one(fields, key, False)
        try:
            native[key] = finite(float(entry[1]), key) if entry else 0.0
        except (ValueError, OverflowError):
            raise ValueError(f"Require finite nonnegative native value for {key}") from None
    if not flags and any(key in fields for key in OWNED_FIELDS):
        raise ValueError("Unverified inactive auxiliary cache; resave a native control first")
    operating = round(native["AuxPower_Const"] / 1000, 10)
    night = round(native["AuxPower_Night"] / 1000, 10)
    if any(key in fields for key in PROFILE_FIELDS):
        # A profile-only field makes the whole state a profile state, which a
        # constant-only reader would silently drop; unknown or mixed families stop here.
        state = {"mode": "profile", "operating_kw": operating, "night_kw": night,
                 "operating_threshold_kw": native["AuxPower_CThresh"],
                 "proportional_w_per_kw": native["AuxPower_Pout"],
                 "proportional_threshold_kw": native["AuxPower_PThresh"]}
        if _profile_family(state) is None:
            raise ValueError("Profile auxiliary state is not one of the verified native families")
        if _coupled_p50(lines):
            raise ValueError("Coupled P50 parameter block with profile auxiliary fields requires separate review")
        return fields, index, flags, state
    return fields, index, flags, {"mode": "constant" if flags else "none",
                                  "operating_kw": operating, "night_kw": night}


def auxiliary_state(lines, system):
    try:
        return _read(lines, system)[3]
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}


def replace_auxiliaries(lines, system, updates):
    if not isinstance(updates, dict) or updates.get("mode") not in ("none", "constant", "profile"):
        raise ValueError("Require auxiliary mode none, constant or profile")
    mode = updates["mode"]
    required = PROFILE_REQUEST_KEYS if mode == "profile" else ({"mode", "operating_kw", "night_kw"} if mode == "constant" else {"mode"})
    if set(updates) != required:
        raise ValueError("Profile mode requires all five explicit values; constant requires operating_kw and night_kw; none accepts no powers")
    values = {}
    for key, step in STEPS.items():
        if key not in updates:
            values[key] = 0.0
            continue
        number = finite(updates[key], f"auxiliary {key}")
        if key in POWER_FIELDS_KW:
            # the kW value must survive the x1000 native watt conversion
            finite(float(number) * 1000, f"native auxiliary watts for {key}")
        scaled = Decimal(str(number)) * step
        if scaled != scaled.to_integral_value():
            raise ValueError(f"Use {'0.01 kW' if step == 100 else '0.1 native unit'} increments for auxiliary {key}")
        values[key] = number
    if mode == "profile":
        if not any(values[key] for key in STEPS):
            raise ValueError("Profile mode requires at least one non-zero threshold or proportional value; an all-zero profile must use constant mode")
        if _profile_family(values) is None:
            raise ValueError("Profile mode accepts only the verified native families: constant with threshold, or proportional")
        if _coupled_p50(lines):
            raise ValueError("Refusing profile auxiliary edit on a variant with a coupled P50 parameter block")
    fields, index, flags, before = _read(lines, system)
    enabled = mode != "none"
    if mode == "profile":
        after = {"mode": "profile", "operating_kw": values["operating_kw"], "night_kw": values["night_kw"],
                 "operating_threshold_kw": values["operating_threshold_kw"],
                 "proportional_w_per_kw": values["proportional_w_per_kw"],
                 "proportional_threshold_kw": values["proportional_threshold_kw"]}
    else:
        # leaving profile mode clears the profile-only fields
        after = {"mode": mode, "operating_kw": values["operating_kw"] if enabled else 0.0,
                 "night_kw": values["night_kw"] if enabled else 0.0}
    if after != before:
        if bool(flags) != enabled:
            lines[index] = replace_value(lines[index], "$00040000" if enabled else "$00")
        newline = "\r\n" if lines[system[0]].endswith("\r\n") else "\n"
        indent = lines[system[0]][:len(lines[system[0]]) - len(lines[system[0]].lstrip())] + "  "
        remove = {row[0] for key in OWNED_FIELDS for row in fields.get(key, [])}
        body = [lines[i] for i in range(system[0] + 1, system[1]) if i not in remove]
        write = [("AuxPower_Const", Decimal(str(values["operating_kw"])) * 1000),
                 ("AuxPower_Night", Decimal(str(values["night_kw"])) * 1000)]
        if mode == "profile":
            write += [("AuxPower_CThresh", Decimal(str(values["operating_threshold_kw"]))),
                      ("AuxPower_Pout", Decimal(str(values["proportional_w_per_kw"]))),
                      ("AuxPower_PThresh", Decimal(str(values["proportional_threshold_kw"])))]
        body += [f"{indent}{key}={value:.3f}{newline}" for key, value in write if value != 0]
        lines[system[0] + 1:system[1]] = body
    scope = ("Native operating/night auxiliary powers with the constant threshold and proportional terms, "
             "restricted to the verified native families" if mode == "profile" else
             "Native whole-system operating/night auxiliary powers; thresholds and proportional term stay zero")
    return {"before": before, "after": after, "scope": scope}

"""Native 8.1.6 inverter temperature offset/fixed control; no thermal or power model."""
from __future__ import annotations

import re
from decimal import Decimal

from pvsyst_auto.components.editor import replace_value
from pvsyst_auto.components.generator import direct_fields, finite, one

FIELDS = ("TempShift", "TempFixed")
KNOWN = {"Comment", "Flags", "SystemType", *FIELDS}
FLAG_MASK = 0xC000
FLAGS = {"ambient": "$00", "offset": "$4000", "fixed": "$8000"}


def _limit(value, label):
    """Adapter support envelope, not a native GUI or manufacturer limit."""
    number = finite(value, label, minimum=-100, maximum=100)
    scaled = Decimal(str(number)) * 10
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{label} must use 0.1 degree increments")
    return number


def _native(raw, label):
    try:
        return _limit(float(raw), label)
    except (ValueError, OverflowError):
        raise ValueError(f"Require a finite native {label}") from None


def _read(lines, system):
    if system is None:
        raise ValueError("Require one owning native pvSystem")
    first = next((i for i, line in enumerate(lines[1:], 1) if line.strip().startswith("PVObject_")), len(lines) - 1)
    if one(direct_fields(lines, (0, first)), "Version")[1] != "8.1.6":
        raise ValueError("Inverter temperature editing requires variant Version=8.1.6")
    if any("pvLoad" in line or "pvGridStorage" in line for line in lines):
        raise ValueError("Inverter temperature editing currently verifies PV-only variants")
    if any(line.strip().startswith("PVObject_") or line.strip() == "EndTags" for line in lines[system[0] + 1:system[1]]):
        raise ValueError("Require a flat native grid system")
    fields = direct_fields(lines, system)
    if set(fields) - KNOWN:
        raise ValueError("PF, grid limits or other system fields require separate review")
    for key in fields:
        one(fields, key)
    if one(fields, "SystemType")[1] != "Grid":
        raise ValueError("Inverter temperature editing requires SystemType=Grid")
    index, raw = one(fields, "Flags")
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw):
        raise ValueError("Require native hexadecimal system Flags")
    flags = int(raw[1:], 16)
    if flags & ~FLAG_MASK or flags == FLAG_MASK:
        raise ValueError("Require ambient, offset-only or fixed-only native system Flags")
    present = [one(fields, key, False) for key in FIELDS]
    if not flags:
        if any(entry is not None for entry in present):
            raise ValueError("An ambient system must not carry temperature fields")
        state = {"mode": "ambient", "offset_c": None, "fixed_c": None}
    else:
        if any(entry is None for entry in present):
            raise ValueError("A controlled system requires both temperature fields")
        values = [_native(entry[1], key) for key, entry in zip(FIELDS, present)]
        state = {"mode": "offset" if flags == 0x4000 else "fixed",
                 "offset_c": values[0], "fixed_c": values[1]}
    return fields, index, flags, state


def inverter_temperature_state(lines, system):
    try:
        return _read(lines, system)[3]
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}


def replace_inverter_temperature(lines, system, updates):
    if not isinstance(updates, dict) or updates.get("mode") not in FLAGS:
        raise ValueError("Require inverter temperature mode ambient, offset or fixed")
    mode = updates["mode"]
    keys = {"mode"} if mode == "ambient" else {"mode", "offset_c", "fixed_c"}
    if set(updates) != keys:
        raise ValueError("Offset and fixed modes require explicit offset_c and fixed_c; ambient accepts no values")
    values = [0.0, 0.0] if mode == "ambient" else [_limit(updates["offset_c"], "offset_c"),
                                                   _limit(updates["fixed_c"], "fixed_c")]
    fields, index, flags, before = _read(lines, system)
    after = {"mode": mode,
             "offset_c": None if mode == "ambient" else values[0],
             "fixed_c": None if mode == "ambient" else values[1]}
    if after != before:
        candidate = list(lines)
        candidate[index] = replace_value(candidate[index], FLAGS[mode])
        newline = "\r\n" if lines[system[0]].endswith("\r\n") else "\n"
        indent = lines[system[0]][:len(lines[system[0]]) - len(lines[system[0]].lstrip())] + "  "
        remove = {row[0] for key in FIELDS for row in fields.get(key, [])}
        body = [candidate[i] for i in range(system[0] + 1, system[1]) if i not in remove]
        if mode != "ambient":
            body += [f"{indent}{key}={Decimal(str(value)):.1f}{newline}"
                     for key, value in zip(FIELDS, values)]
        candidate[system[0] + 1:system[1]] = body
        # Commit only a candidate that reads back as the requested state.
        if _read(candidate, (system[0], system[0] + len(body) + 1))[3] != after:
            raise ValueError("Candidate inverter temperature state differs from the request")
        lines[:] = candidate
    return {"before": before, "after": after,
            "scope": "Native whole-system inverter temperature control; no thermal or power model"}

"""Native 8.1.6 constant operating/night auxiliary power; no load calculation."""
from __future__ import annotations

import re
from decimal import Decimal

from pvsyst_component_editor import ASSIGNMENT, replace_value
from pvsyst_generator import direct_fields, finite, one

POWER_FIELDS = ("AuxPower_Const", "AuxPower_Night")


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
    if set(fields) - {"Comment", "Flags", "SystemType", *POWER_FIELDS}:
        raise ValueError("PF, grid limits, proportional/threshold auxiliary or other system fields require separate review")
    for key in fields:
        one(fields, key)
    if one(fields, "SystemType")[1] != "Grid":
        raise ValueError("Auxiliary editing requires SystemType=Grid")
    index, raw = one(fields, "Flags")
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw) or int(raw[1:], 16) & ~0x00040000:
        raise ValueError("Require verified auxiliary-only native system Flags")
    flags = int(raw[1:], 16)
    values = []
    for key in POWER_FIELDS:
        total = sum(m is not None and m[2] == key for line in lines
                    for m in [ASSIGNMENT.fullmatch(line.rstrip("\r\n"))])
        if total != len(fields.get(key, [])):
            raise ValueError(f"Require {key} only in the owning system")
        entry = one(fields, key, False)
        try:
            watts = finite(float(entry[1]), key) if entry else 0.0
        except (ValueError, OverflowError):
            raise ValueError(f"Require finite nonnegative native watts for {key}") from None
        values.append(round(watts / 1000, 10))
    if not flags and any(key in fields for key in POWER_FIELDS):
        raise ValueError("Unverified inactive auxiliary cache; resave a native control first")
    return fields, index, flags, {"mode": "constant" if flags else "none",
                                "operating_kw": values[0], "night_kw": values[1]}


def auxiliary_state(lines, system):
    try:
        return _read(lines, system)[3]
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}


def replace_auxiliaries(lines, system, updates):
    if not isinstance(updates, dict) or updates.get("mode") not in ("none", "constant"):
        raise ValueError("Require auxiliary mode none or constant")
    enabled = updates["mode"] == "constant"
    if set(updates) != ({"mode", "operating_kw", "night_kw"} if enabled else {"mode"}):
        raise ValueError("Constant mode requires explicit operating_kw and night_kw; none accepts no powers")
    values = [updates["operating_kw"], updates["night_kw"]] if enabled else [0, 0]
    for value in values:
        finite(value, "auxiliary kW")
        finite(float(value) * 1000, "native auxiliary watts")
        if Decimal(str(value)) * 100 != (Decimal(str(value)) * 100).to_integral_value():
            raise ValueError("Use 0.01 kW increments for this bounded auxiliary entry")
    fields, index, flags, before = _read(lines, system)
    after = {"mode": updates["mode"], "operating_kw": float(values[0]), "night_kw": float(values[1])}
    if after != before:
        if bool(flags) != enabled:
            lines[index] = replace_value(lines[index], "$00040000" if enabled else "$00")
        newline = "\r\n" if lines[system[0]].endswith("\r\n") else "\n"
        indent = lines[system[0]][:len(lines[system[0]]) - len(lines[system[0]].lstrip())] + "  "
        remove = {row[0] for key in POWER_FIELDS for row in fields.get(key, [])}
        body = [lines[i] for i in range(system[0] + 1, system[1]) if i not in remove]
        body += [f"{indent}{key}={Decimal(str(value)) * 1000:.3f}{newline}"
                 for key, value in zip(POWER_FIELDS, values) if value != 0]
        lines[system[0] + 1:system[1]] = body
    return {"before": before, "after": after,
            "scope": "Native whole-system operating/night auxiliary powers; thresholds and proportional term stay zero"}

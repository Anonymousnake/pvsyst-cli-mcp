"""Native 8.1.6 power-factor tags (TanPhi / TanPhiM) serialization only.

Writes the signed native value as given: no cos conversion, no power or energy
model. The numeric domain checked here is an adapter support envelope, not a
native GUI or manufacturer limit.
"""
from __future__ import annotations

import re
from decimal import Decimal

from pvsyst_auto.components.editor import ASSIGNMENT, replace_value
from pvsyst_auto.components.generator import direct_fields, finite, one

FIELDS = ("TanPhi", "TanPhiM")
KNOWN = {"Comment", "Flags", "SystemType", *FIELDS}
PF_BITS = {"constant": 0x00100000, "monthly": 0x00300000}
PNOM_BITS = {"native": 0, "apparent": 0x00400000, "active": 0x00800000}
ALLOWED_BITS = 0x00100000 | 0x00200000 | 0x00400000 | 0x00800000
MODES = ("none", "constant", "monthly")
MONTHS = 12


def _tan(value, label):
    number = finite(value, label, minimum=-1, maximum=1)
    scaled = Decimal(str(number)) * 1000
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{label} must use 0.001 increments")
    return number


def _native(raw, label):
    try:
        return _tan(float(raw), label)
    except (ValueError, OverflowError):
        raise ValueError(f"Require a finite native {label}") from None


def _check_pair(mode, pnom):
    """One mode rule for native reads and requested writes."""
    if mode == "none" and pnom != "native":
        raise ValueError("A disabled power factor requires pnom_limit native")


def _read(lines, system):
    if system is None:
        raise ValueError("Require one owning native pvSystem")
    first = next((i for i, line in enumerate(lines[1:], 1) if line.strip().startswith("PVObject_")), len(lines) - 1)
    if one(direct_fields(lines, (0, first)), "Version")[1] != "8.1.6":
        raise ValueError("Power factor editing requires variant Version=8.1.6")
    if any("pvLoad" in line or "pvGridStorage" in line for line in lines):
        raise ValueError("Power factor editing currently verifies PV-only variants")
    if any(line.strip().startswith("PVObject_") or line.strip() == "EndTags" for line in lines[system[0] + 1:system[1]]):
        raise ValueError("Require a flat native grid system")
    fields = direct_fields(lines, system)
    if set(fields) - KNOWN:
        raise ValueError("Storage, load, temperature, grid-limit or other system fields require separate review")
    for key in fields:
        one(fields, key)
    for key in FIELDS:
        total = sum(m is not None and m[2] == key for line in lines
                    for m in [ASSIGNMENT.fullmatch(line.rstrip("\r\n"))])
        if total != len(fields.get(key, [])):
            raise ValueError(f"Require {key} only in the owning system")
    if one(fields, "SystemType")[1] != "Grid":
        raise ValueError("Power factor editing requires SystemType=Grid")
    index, raw = one(fields, "Flags")
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw):
        raise ValueError("Require native hexadecimal system Flags")
    flags = int(raw[1:], 16)
    if flags & ~ALLOWED_BITS:
        raise ValueError("Only the PF and PNom bits are verified; other system bits require separate review")
    if flags & 0x00200000 and not flags & 0x00100000:
        raise ValueError("A monthly profile requires the constant PF bit as well")
    if flags & 0x00400000 and flags & 0x00800000:
        raise ValueError("Active and apparent PNom limits are mutually exclusive")
    present = [one(fields, key, False) for key in FIELDS]
    tan = monthly = None
    if not flags & 0x00100000:
        if any(entry is not None for entry in present):
            raise ValueError("A disabled power factor must not carry TanPhi fields")
        mode = "none"
    elif flags & 0x00200000:
        if present[0] is not None or present[1] is None:
            raise ValueError("A monthly profile requires exactly TanPhiM")
        monthly = [_native(value, "TanPhiM") for value in present[1][1].removesuffix(",").split(",")]
        if len(monthly) != MONTHS:
            raise ValueError("A monthly profile requires twelve TanPhiM values")
        mode = "monthly"
    else:
        if present[1] is not None or present[0] is None:
            raise ValueError("A constant profile requires exactly TanPhi")
        tan = _native(present[0][1], "TanPhi")
        mode = "constant"
    pnom = "apparent" if flags & 0x00400000 else "active" if flags & 0x00800000 else "native"
    _check_pair(mode, pnom)
    return fields, index, {"mode": mode, "tan_phi": tan, "monthly_tan_phi": monthly, "pnom_limit": pnom}


def power_factor_state(lines, system):
    """Semantic power-factor state; unsupported layouts report mode None and an issue."""
    try:
        return _read(lines, system)[2]
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}


def replace_power_factor(lines, system, updates):
    """Write the verified native power-factor tags in place. Signed values are passed through."""
    if not isinstance(updates, dict) or updates.get("mode") not in MODES:
        raise ValueError("Require power factor mode none, constant or monthly")
    mode = updates["mode"]
    pnom = updates.get("pnom_limit")
    if not isinstance(pnom, str) or pnom not in PNOM_BITS:
        raise ValueError("Require pnom_limit native, active or apparent")
    extra = {"tan_phi"} if mode == "constant" else {"monthly_tan_phi"} if mode == "monthly" else set()
    if set(updates) != {"mode", "pnom_limit"} | extra:
        raise ValueError("Provide only the values required by the chosen power factor mode")
    _check_pair(mode, pnom)
    tan = monthly = None
    if mode == "constant":
        tan = _tan(updates["tan_phi"], "tan_phi")
    elif mode == "monthly":
        supplied = updates["monthly_tan_phi"]
        if not isinstance(supplied, list) or len(supplied) != MONTHS:
            raise ValueError("Monthly power factor requires twelve monthly_tan_phi values")
        monthly = [_tan(value, "monthly_tan_phi") for value in supplied]
    fields, index, before = _read(lines, system)
    after = {"mode": mode, "tan_phi": tan, "monthly_tan_phi": monthly, "pnom_limit": pnom}
    if after != before:
        flags = "$00" if mode == "none" else "$%08X" % (PF_BITS[mode] | PNOM_BITS[pnom])
        candidate = list(lines)
        candidate[index] = replace_value(candidate[index], flags)
        newline = "\r\n" if lines[system[0]].endswith("\r\n") else "\n"
        indent = lines[system[0]][:len(lines[system[0]]) - len(lines[system[0]].lstrip())] + "  "
        remove = {row[0] for key in FIELDS for row in fields.get(key, [])}
        body = [candidate[i] for i in range(system[0] + 1, system[1]) if i not in remove]
        if mode == "constant":
            body.append(f"{indent}TanPhi={Decimal(str(tan)):.3f}{newline}")
        elif mode == "monthly":
            body.append(f"{indent}TanPhiM={','.join(f'{Decimal(str(v)):.3f}' for v in monthly)},{newline}")
        candidate[system[0] + 1:system[1]] = body
        checked = _read(candidate, (system[0], system[0] + len(body) + 1))[2]
        if checked != after:
            raise ValueError("Candidate power factor state differs from the request")
        lines[:] = candidate
    return {"before": before, "after": after,
            "scope": "Native whole-system power factor tags only; no cos conversion or power model"}

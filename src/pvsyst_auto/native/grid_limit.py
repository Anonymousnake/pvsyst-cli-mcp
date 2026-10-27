"""Bounded native 8.1.6 injection-point limit fields; no power calculation."""
from __future__ import annotations

import re
from decimal import Decimal

from pvsyst_auto.components.editor import ASSIGNMENT, replace_value
from pvsyst_auto.components.generator import direct_fields, finite, one


def _read(lines, system):
    if system is None:
        raise ValueError("Require one native pvSystem")
    first = next((i for i, line in enumerate(lines[1:], 1) if line.strip().startswith("PVObject_")), len(lines) - 1)
    if one(direct_fields(lines, (0, first)), "Version")[1] != "8.1.6":
        raise ValueError("Grid-limit fields require variant Version=8.1.6")
    if any("pvLoad" in line or "pvGridStorage" in line for line in lines):
        raise ValueError("Load/storage grid limiting is outside the verified PV-only scope")
    if any(line.strip().startswith("PVObject_") or line.strip() == "EndTags" for line in lines[system[0] + 1:system[1]]):
        raise ValueError("Require a flat native grid system")
    fields = direct_fields(lines, system)
    if set(fields) - {"Comment", "Flags", "SystemType", "PLimGrid", "LimGrSeparLoss"}:
        raise ValueError("PF or unverified native system fields require separate review")
    for key in fields:
        one(fields, key)
    if one(fields, "SystemType")[1] != "Grid":
        raise ValueError("Grid limiting requires SystemType=Grid")
    index, raw = one(fields, "Flags")
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw):
        raise ValueError("Require one hexadecimal owning system Flags")
    flags = int(raw[1:], 16)
    if flags & ~0x05000000:
        raise ValueError("PF, export permission or other native system modes are not verified here")
    for key in ("PLimGrid", "LimGrSeparLoss"):
        total = sum(m is not None and m[2] == key for line in lines
                    for m in [ASSIGNMENT.fullmatch(line.rstrip("\r\n"))])
        if total != len(fields.get(key, [])):
            raise ValueError(f"Require {key} only in the owning system")
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
    return fields, index, flags, {"mode": "constant" if enabled else "none",
        "limit_kw": value if enabled else None,
        "point": "injection" if flags & 0x04000000 else "inverter",
        "separate_loss": separate[1] == "True" if separate else None}


def grid_limit_state(lines, system):
    try:
        return _read(lines, system)[3]
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}


def replace_grid_limit(lines, system, updates):
    """Edit a copy; `lines` changes in place only after the edited system re-reads cleanly."""
    if not isinstance(updates, dict) or updates.get("mode") not in ("none", "constant"):
        raise ValueError("Require grid-limit mode none or constant")
    enabled = updates["mode"] == "constant"
    if set(updates) != ({"mode", "limit_kw"} if enabled else {"mode"}):
        raise ValueError("Provide only limit_kw for constant mode, and no value for none")
    if enabled:
        value = finite(updates["limit_kw"], "limit_kw", positive=True)
        if Decimal(str(value)) * 10 != (Decimal(str(value)) * 10).to_integral_value():
            raise ValueError("Use 0.1 kW increments, matching native GUI save precision")
    fields, index, flags, before = _read(lines, system)
    work = list(lines)
    new_flags = flags | 0x05000000 if enabled else flags & ~0x01000000
    if new_flags != flags:
        work[index] = replace_value(work[index], f"${new_flags:08X}" if new_flags else "$00")
    count = len(work)
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
        for i in sorted((row[0] for key in ("PLimGrid", "LimGrSeparLoss") for row in fields.get(key, [])), reverse=True):
            del work[i]
    bounds = (system[0], system[1] + len(work) - count)
    after = _read(work, bounds)[3]
    lines[:] = work
    return {"before": before, "after": after,
            "scope": "Native 8.1.6 PV-only active kW limit at injection point; separate grid loss"}

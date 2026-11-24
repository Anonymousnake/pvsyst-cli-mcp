"""Bounded native 8.1.6 injection-point limit fields; no power calculation."""
from __future__ import annotations

from decimal import Decimal

from pvsyst_auto.components.editor import replace_value
from pvsyst_auto.components.editor import finite, one, read_flat_grid_system


def _read(lines, system):
    fields, index, flags = read_flat_grid_system(
        lines,
        system,
        feature_label="Grid limiting",
        owned_fields=("PLimGrid", "LimGrSeparLoss"),
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
    new_flags = flags | 0x05000000 if enabled else flags & ~0x05000000
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
    return {"before": before, "after": after}

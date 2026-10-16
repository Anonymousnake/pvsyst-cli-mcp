"""Minimal native string-voltage-mismatch helper.

Serialises only the verified native pair -- the owning pvSubArray default bit and
PMismString -- reusing the shared editor helpers. No loss, probability or energy
calculation is performed, and no other object or field is touched. The real `lines`
are never mutated before the whole target body is computed and every check passed.
"""
from __future__ import annotations

import re
from decimal import Decimal

from pvsyst_component_editor import ASSIGNMENT, replace_value
from pvsyst_generator import direct_fields, finite, one

DEFAULT_BIT = 0x00020000          # bit17: set = native default, clear = manual value
FIELD = "PMismString"
ANCHOR = "PMismMPP"               # GUI order: PMismString follows PMismMPP ...
SIBLING = "PMismUFixe"            # ... and in the saved file it follows PMismUFixe too
SCOPE = "Owning pvSubArray only; native string-voltage-mismatch default bit and PMismString"


def _domain(number):
    """Adapter support envelope (0.01..100, 0.01 steps); zero is expressed as mode default."""
    number = finite(number, "loss_percent", minimum=0.01, maximum=100)
    scaled = Decimal(str(number)) * 100
    if scaled != scaled.to_integral_value():
        raise ValueError("loss_percent must use 0.01 increments")
    return number


def _percent_input(value):
    """Writer side: a strict number; strings are rejected, never coerced."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("loss_percent must be a strict number")
    return _domain(value)


def _percent_native(raw):
    """Reader side: the native literal is text, so decode it before the shared domain check."""
    try:
        return _domain(float(raw))
    except (ValueError, OverflowError):
        raise ValueError("Require a finite native PMismString") from None


def _read(lines, bounds):
    """Owning subarray only: direct_fields skips nested objects; one() rejects duplicates."""
    if not isinstance(bounds, tuple) or len(bounds) != 2:
        raise ValueError("Require one owning subarray bounds")
    fields = direct_fields(lines, bounds)
    if one(fields, "Version")[1] != "8.1.6":
        raise ValueError("String mismatch editing requires subarray Version=8.1.6")
    raw = one(fields, "Flags")[1]
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", raw):
        raise ValueError("Require one hexadecimal owning subarray Flags")
    total = sum(m is not None and m[2] == FIELD for line in lines
                for m in [ASSIGNMENT.fullmatch(line.rstrip("\r\n"))])
    if total != len(fields.get(FIELD, [])):
        raise ValueError(f"Require {FIELD} only in the owning subarray")
    flags = int(raw[1:], 16)
    entry = one(fields, FIELD, False)
    if flags & DEFAULT_BIT:
        if entry is not None:
            raise ValueError("A defaulted string mismatch must not carry PMismString")
        state = {"mode": "default", "loss_percent": None}
    else:
        if entry is None:
            raise ValueError("A manual string mismatch requires one finite PMismString")
        state = {"mode": "constant", "loss_percent": _percent_native(entry[1])}
    return fields, flags, state


def string_mismatch_state(lines, bounds):
    """Semantic state; unsupported layouts report mode None plus an issue and never a zero."""
    try:
        return _read(lines, bounds)[2]
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}


def _body(lines, bounds, fields, flags, mode, value):
    """The complete replacement body, built without touching `lines`."""
    target = (flags | DEFAULT_BIT) if mode == "default" else (flags & ~DEFAULT_BIT)
    entry = one(fields, FIELD, False)
    anchor = None
    if mode == "constant" and entry is None:
        anchor = one(fields, SIBLING, False) or one(fields, ANCHOR, False)
        if anchor is None:
            raise ValueError(f"Require an owning {SIBLING} or {ANCHOR} to place {FIELD} in GUI order")
    body = []
    flags_index, flags_raw = fields["Flags"][0]
    field_index = entry[0] if entry is not None else None
    for i in range(bounds[0] + 1, bounds[1]):
        if i == flags_index:
            if target != flags:
                body.append(replace_value(lines[i], "$%0*X" % (len(flags_raw) - 1, target)))
            else:
                body.append(lines[i])
            continue
        if i == field_index:
            if mode == "constant":
                body.append(replace_value(lines[i], f"{value:.2f}"))
            continue
        body.append(lines[i])
        if anchor is not None and anchor[0] == i:
            line = lines[i]
            indent = line[:len(line) - len(line.lstrip())]
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            body.append(f"{indent}{FIELD}={value:.2f}{ending}")
    return body


def replace_string_mismatch(lines, bounds, updates):
    """Validate input and source, build the whole body, then replace once."""
    if not isinstance(updates, dict) or updates.get("mode") not in ("default", "constant"):
        raise ValueError("Require string mismatch mode default or constant")
    mode = updates["mode"]
    keys = {"mode"} if mode == "default" else {"mode", "loss_percent"}
    if set(updates) != keys:
        raise ValueError("default accepts only mode; constant requires loss_percent")
    value = None if mode == "default" else _percent_input(updates["loss_percent"])
    fields, flags, before = _read(lines, bounds)
    after = {"mode": mode, "loss_percent": value}
    if before == after:
        return {"before": before, "after": after, "scope": SCOPE}
    body = _body(lines, bounds, fields, flags, mode, value)
    lines[bounds[0] + 1:bounds[1]] = body
    return {"before": before, "after": after, "scope": SCOPE}

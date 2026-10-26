"""Native 8.1.6 root horizon (pvHorizon / TCubicProfile) serialization.

Copies the GUI-saved tag block only; no interpolation, albedo or energy
calculation, and never derives the native FHHDiffus/FHPDiffus/FHPAlbedo factors.
"""
from __future__ import annotations

import re
from decimal import Decimal

from pvsyst_auto.components.editor import ASSIGNMENT, replace_value

ROOT = "PVObject_Horizon=pvHorizon"
END = "End of PVObject pvHorizon"
PROFILE = "HorizonLine=TCubicProfile"
PROFILE_END = "End of TCubicProfile"
MAIN = "PVObject_PVMainArray=pvMainArray"
SLOTS = 100
DERIVED = ("FHHDiffus", "FHPDiffus", "FHPAlbedo")
OUTER = ("Comment", "Flags", "FracAlbedo") + DERIVED
INNER = ("NPtsMax", "NPtsEff", "LastCompile", "Mode")
POINT = re.compile(r"Point_([0-9]{1,3})")
LAST_COMPILE = "$81"
SCOPE = ("Root pvHorizon tag block only; native 8.1.6 horizon profile, "
         "not an interpolation, albedo or energy model")


def _number(value, label, low, high, step=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a finite number") from None
    if not low <= number <= high or (step and (Decimal(str(number)) * 10) % 1):
        raise ValueError(f"{label} must be a finite value inside {low}..{high}")
    return number


def _locate(lines):
    """The single root horizon block, or None when the file carries no horizon."""
    starts = [i for i, line in enumerate(lines) if line.strip() == ROOT]
    if len(starts) > 1:
        raise ValueError("Horizon editing requires at most one root pvHorizon object")
    if not starts:
        return None
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == END), None)
    if end is None:
        raise ValueError("Unclosed root pvHorizon object")
    return start, end


def _read(lines):
    """Decode the GUI-saved block; None when absent. Never invents missing data."""
    located = _locate(lines)
    if located is None:
        return None
    start, end = located
    opens = [i for i in range(start + 1, end) if lines[i].strip() == PROFILE]
    if len(opens) != 1:
        raise ValueError("Horizon requires exactly one HorizonLine profile")
    body = opens[0]
    close = next((i for i in range(body + 1, end) if lines[i].strip() == PROFILE_END), None)
    if close is None:
        raise ValueError("Unclosed horizon profile")
    outer, inner, points, outer_at = {}, {}, {}, {}
    for i in range(start + 1, end):
        if i in (body, close):
            continue
        if not lines[i].strip():
            raise ValueError("Blank lines inside the horizon block are not supported")
        match = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))
        if match is None:
            raise ValueError("Unknown horizon line")
        key = match[2]
        slot = POINT.fullmatch(key)
        if slot:
            if not body < i < close:
                raise ValueError("Horizon points must sit inside the profile")
            index = int(slot[1])
            if not 1 <= index <= SLOTS or index in points:
                raise ValueError("Horizon point slots must be 1..100 without repeats")
            raw = match[4].split(",")
            if len(raw) != 2:
                raise ValueError("Horizon points must be an x,y pair")
            points[index] = (_number(raw[0], f"Point_{index} x", -180, 180, True),
                             _number(raw[1], f"Point_{index} y", 0, 90, True))
        elif body < i < close:
            if key not in INNER or key in inner:
                raise ValueError("Unknown or duplicate horizon profile field")
            inner[key] = match[4]
        elif i > close:
            raise ValueError("Unknown field after the horizon profile")
        elif key not in OUTER or key in outer:
            raise ValueError("Unknown or duplicate horizon field")
        else:
            outer[key] = match[4]
    if (len(points) != SLOTS or set(inner) != set(INNER) or set(outer) - set(OUTER)
            or not {"Comment", "Flags", "FracAlbedo"} <= set(outer)):
        raise ValueError("Horizon requires the outer fields, profile scalars and 100 slots")
    if inner["NPtsMax"] != str(SLOTS) or inner["Mode"] != "1":
        raise ValueError("Horizon editing requires NPtsMax=100 and Mode=1")
    if inner["LastCompile"] != LAST_COMPILE:
        raise ValueError(f"Horizon editing requires the native LastCompile={LAST_COMPILE}")
    _number(outer.get("FracAlbedo", ""), "FracAlbedo", 1, 1)
    derived = [key for key in DERIVED if key in outer]
    if outer["Flags"] == "$01":
        if derived:
            raise ValueError("Flags=$01 must not carry derived horizon factors")
    elif outer["Flags"] == "$03":
        if len(derived) != len(DERIVED):
            raise ValueError("Flags=$03 requires all three derived horizon factors")
        for key in derived:
            _number(outer[key], key, 0, 1)
    else:
        raise ValueError("Horizon editing requires native Flags=$01 or $03")
    try:
        effective = int(inner["NPtsEff"])
    except ValueError:
        raise ValueError("NPtsEff must be a native integer") from None
    if not 1 <= effective <= SLOTS:
        raise ValueError("NPtsEff must be inside 1..100")
    for index in range(effective + 1, SLOTS + 1):
        if points[index] != (0.0, 0.0):
            raise ValueError("Unused horizon slots must stay at the native 0.0,0.0 placeholder")
    real = [points[index] for index in range(1, effective + 1)]
    for previous, current in zip(real, real[1:]):
        if current[0] <= previous[0]:
            raise ValueError("Horizon point azimuths must increase strictly")
    return {"start": start, "end": end, "outer": outer, "inner": inner, "points": real}


def horizon_state(lines):
    """Semantic horizon state; a file without a horizon reports an empty profile."""
    try:
        state = _read(lines)
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}
    if state is None:
        return {"mode": "none", "profile_name": None, "points": []}
    return {"mode": "profile", "profile_name": state["outer"]["Comment"] or None,
            "points": [[x, y] for x, y in state["points"]],
            "native_fields": {"flags": state["outer"]["Flags"],
                              "derived": {key: _number(state["outer"][key], key, 0, 1)
                                          for key in DERIVED if key in state["outer"]}}}


def _check_points(points):
    if not isinstance(points, list) or not 4 <= len(points) <= SLOTS:
        raise ValueError("Horizon profiles require 4 to 100 points")
    decoded = []
    for entry in points:
        if not isinstance(entry, list) or len(entry) != 2:
            raise ValueError("Each horizon point must be a strict numeric [x, y] pair")
        for value in entry:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Each horizon point must be a strict numeric [x, y] pair")
        decoded.append((_number(entry[0], "point azimuth", -180, 180, True),
                        _number(entry[1], "point height", 0, 90, True)))
    if not any(pair[1] > 0 for pair in decoded):
        raise ValueError("Use horizon mode none for an all-zero profile")
    if len(set(decoded)) != len(decoded):
        raise ValueError("Horizon points must not repeat")
    for previous, current in zip(decoded, decoded[1:]):
        if current[0] <= previous[0]:
            raise ValueError("Horizon point azimuths must increase strictly")
    return decoded


def _check_name(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("profile_name must be a non-empty trimmed string")
    if len(value.encode("utf-8")) > 80 or not value.isprintable():
        raise ValueError("profile_name must be 1..80 printable UTF-8 bytes")
    return value


def _body(indent, name, points, ending):
    """The verified GUI template, written with the file's own indent and ending."""
    lines = [f"{indent}{ROOT}{ending}", f"{indent}  Comment={name}{ending}",
             f"{indent}  Flags=$01{ending}", f"{indent}  FracAlbedo=1.000{ending}",
             f"{indent}  {PROFILE}{ending}", f"{indent}    NPtsMax={SLOTS}{ending}",
             f"{indent}    NPtsEff={len(points)}{ending}",
             f"{indent}    LastCompile={LAST_COMPILE}{ending}", f"{indent}    Mode=1{ending}"]
    for index in range(1, SLOTS + 1):
        x, y = points[index - 1] if index <= len(points) else (0.0, 0.0)
        lines.append(f"{indent}    Point_{index}={x:.1f},{y:.1f}{ending}")
    lines += [f"{indent}  {PROFILE_END}{ending}", f"{indent}{END}{ending}"]
    return lines


def replace_horizon(lines, updates):
    """Write the root horizon block in place, copying the verified GUI template."""
    if not isinstance(updates, dict) or updates.get("mode") not in ("none", "profile"):
        raise ValueError("Require horizon mode none or profile")
    mode = updates["mode"]
    keys = {"mode"} if mode == "none" else {"mode", "points", "profile_name"}
    if set(updates) != keys:
        raise ValueError("Provide only the values required by the chosen horizon mode")
    points = [] if mode == "none" else _check_points(updates["points"])
    name = None if mode == "none" else _check_name(updates["profile_name"])
    current = horizon_state(lines)
    if current["mode"] is None:
        raise ValueError(current["issue"])
    before = {key: current[key] for key in ("mode", "profile_name", "points")}
    located = _locate(lines)
    if mode == "none":
        if located is None:
            return {"before": before, "after": before, "scope": SCOPE}
        start, end = located
        stop = end + 1 if end + 1 < len(lines) and not lines[end + 1].strip() else end
        del lines[start:stop + 1]
        after = {"mode": "none", "profile_name": None, "points": []}
    elif (before["mode"] == "profile" and before["profile_name"] == name
            and before["points"] == [list(pair) for pair in points]):
        return {"before": before, "after": before, "scope": SCOPE}
    elif before["mode"] == "profile" and before["points"] == [list(pair) for pair in points]:
        start, end = located
        index = next(i for i in range(start + 1, end) if
                     (match := ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))) and match[2] == "Comment")
        lines[index] = replace_value(lines[index], name)
        after = {**before, "profile_name": name}
    else:
        ending = "\r\n" if lines[0].endswith("\r\n") else "\n"
        if located is None:
            mains = [i for i, line in enumerate(lines) if line.strip() == MAIN]
            if len(mains) != 1:
                raise ValueError("Horizon insertion requires exactly one pvMainArray object")
            indent = re.match(r"[ \t]*", lines[mains[0]])[0]
            lines[mains[0]:mains[0]] = _body(indent, name, points, ending) + [ending]
        else:
            indent = re.match(r"[ \t]*", lines[located[0]])[0]
            lines[located[0]:located[1] + 1] = _body(indent, name, points, ending)
        after = {"mode": "profile", "profile_name": name,
                 "points": [[x, y] for x, y in points]}
    return {"before": before, "after": after, "scope": SCOPE}

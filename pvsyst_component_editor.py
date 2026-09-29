"""Lossless scalar replacement and read-only OND curve structure inspection.

This module handles file structure only. It neither fits a physical model nor
claims that every PVsyst version consumes every serialized field.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field


ASSIGNMENT = re.compile(r"^([ \t]*)([A-Za-z_][A-Za-z0-9_]*)([ \t]*=[ \t]*)(.*?)([ \t]*)$")
CURVE_PATHS = frozenset(f"Converter/{name}" for name in
                        ("ProfilPIO", "ProfilPIOV1", "ProfilPIOV2", "ProfilPIOV3"))
MAX_POINTS = 256


def replace_value(line: str, value: str) -> str:
    """Preserve the tag spelling, spaces, indentation and exact line ending."""
    body = line.rstrip("\r\n")
    match = ASSIGNMENT.fullmatch(body)
    if not match:
        raise ValueError("Expected a component assignment")
    return body[:match.start(4)] + value + body[match.end(4):] + line[len(body):]


@dataclass
class Profile:
    path: str
    line: int
    fields: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def one(self, name: str) -> tuple[int, str]:
        values = self.fields.get(name, [])
        if len(values) != 1:
            raise ValueError(f"{name} must occur exactly once in {self.path}")
        return values[0]

    def points(self) -> list[list[float]]:
        def count(name: str) -> int:
            value = self.one(name)[1]
            if not re.fullmatch(r"[0-9]+", value):
                raise ValueError(f"{self.path}: {name} must be an integer")
            return int(value)

        capacity, effective = count("NPtsMax"), count("NPtsEff")
        if not 1 <= effective <= capacity <= MAX_POINTS:
            raise ValueError(f"{self.path}: inspection requires 1 <= NPtsEff <= NPtsMax <= {MAX_POINTS}")
        names = {name for name in self.fields if name.startswith("Point_")}
        if names != {f"Point_{i}" for i in range(1, capacity + 1)}:
            raise ValueError(f"{self.path}: Point_N rows must match NPtsMax")
        points = []
        for i in range(1, capacity + 1):
            raw = self.one(f"Point_{i}")[1].split(",")
            if len(raw) != 2:
                raise ValueError(f"{self.path}: invalid Point_{i}")
            try:
                pair = [float(value) for value in raw]
            except ValueError:
                raise ValueError(f"{self.path}: invalid Point_{i}") from None
            if not all(math.isfinite(value) for value in pair):
                raise ValueError(f"{self.path}: Point_{i} must be finite")
            if i <= effective:
                points.append(pair)
        return points


def profiles(text: str) -> list[Profile]:
    """Locate curves by their object path, never by flattened Point_N names."""
    stack: list[tuple[str, str, Profile | None]] = []
    result = []
    for index, line in enumerate(text.splitlines()):
        end = re.fullmatch(r"\s*End of (?:PVObject )?(pv\w+|TConverter|TCubicProfile)\s*", line)
        if end:
            if not stack or stack[-1][1] != end[1]:
                raise ValueError(f"Unmatched object closing tag at line {index + 1}")
            stack.pop()
            continue
        match = ASSIGNMENT.fullmatch(line)
        if not match:
            continue
        name, value = match[2], match[4]
        if ((name.startswith("PVObject_") and re.fullmatch(r"pv\w+", value))
                or value in ("TConverter", "TCubicProfile")):
            profile = None
            if stack and stack[-1][2] is not None:
                stack[-1][2].problems.append("Nested objects inside curves are unsupported")
            if value == "TCubicProfile":
                path = "/".join([item[0] for item in stack[1:]] + [name])
                profile = Profile(path, index + 1)
                result.append(profile)
            stack.append((name, value, profile))
        elif stack and stack[-1][2] is not None:
            stack[-1][2].fields.setdefault(name, []).append((index, value))
    if stack:
        raise ValueError("Unclosed component object")
    return result


def curve_inventory(kind: str, text: str) -> dict:
    if kind != "OND":
        return {"scope": "OND power curves only", "items": [], "truncated": False}
    try:
        found = profiles(text)
    except ValueError as exc:
        return {"scope": "OND power curves only", "items": [], "errors": [str(exc)], "truncated": False}
    items = []
    for profile in found[:16]:
        errors = list(profile.problems)
        points = []
        try:
            points = profile.points()
        except ValueError as exc:
            errors.append(str(exc))
        if sum(other.path == profile.path for other in found) != 1:
            errors.append("Ambiguous duplicate curve path")
        supported = profile.path in CURVE_PATHS
        items.append({"path": profile.path, "line": profile.line,
                      "editable": False, "structure_complete": not errors, "errors": errors,
                      "points": points, "point_count": len(points),
                      "point_count_editable": False,
                      "simulation_effect": "unverified",
                      "axes": ["input power (W)", "output power (W)"] if supported else None})
    return {"scope": "OND power curves only; physical behavior requires simulation",
            "items": items, "truncated": len(found) > len(items)}


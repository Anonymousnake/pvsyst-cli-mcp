"""Scoped component parsing, lossless replacements and OND curve editing.

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
MAIN_CURVE = "Converter/ProfilPIO"
AUTO_CURVE_MASK = 0x10
THREE_VOLTAGE_MASK = 0x1000


def replace_value(line: str, value: str) -> str:
    """Preserve the tag spelling, spaces, indentation and exact line ending."""
    body = line.rstrip("\r\n")
    match = ASSIGNMENT.fullmatch(body)
    if not match:
        raise ValueError("Expected a component assignment")
    return body[:match.start(4)] + value + body[match.end(4):] + line[len(body):]


def replace_key(line: str, key: str) -> str:
    """Rename a selected assignment without changing its value or whitespace."""
    body = line.rstrip("\r\n")
    match = ASSIGNMENT.fullmatch(body)
    if not match or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ValueError("Expected a component assignment and valid key")
    return body[:match.start(2)] + key + body[match.end(2):] + line[len(body):]


@dataclass
class ObjectBlock:
    path: str
    line: int
    object_type: str
    fields: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    end_line: int = 0

    def one(self, name: str) -> tuple[int, str]:
        values = self.fields.get(name, [])
        if len(values) != 1:
            raise ValueError(f"{name} must occur exactly once in {self.path}")
        return values[0]


@dataclass
class Profile(ObjectBlock):
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
            if len(raw) not in (2, 5):
                raise ValueError(f"{self.path}: invalid Point_{i}")
            try:
                values = [float(value) for value in raw]
            except ValueError:
                raise ValueError(f"{self.path}: invalid Point_{i}") from None
            if not all(math.isfinite(value) for value in values):
                raise ValueError(f"{self.path}: Point_{i} must be finite")
            if i <= effective:
                points.append(values[:2])
        return points


def objects(text: str) -> list[ObjectBlock]:
    """Keep each field in its owning object, including root and nested Flags."""
    stack: list[tuple[str, ObjectBlock]] = []
    result = []
    for index, line in enumerate(text.splitlines()):
        end = re.fullmatch(r"\s*End of (?:PVObject )?(pv\w+|TConverter|TCubicProfile)\s*", line)
        if end:
            if not stack or stack[-1][1].object_type != end[1]:
                raise ValueError(f"Unmatched object closing tag at line {index + 1}")
            stack[-1][1].end_line = index + 1
            stack.pop()
            continue
        match = ASSIGNMENT.fullmatch(line)
        if not match:
            continue
        name, value = match[2], match[4]
        if ((name.startswith("PVObject_") and re.fullmatch(r"pv\w+", value))
                or value in ("TConverter", "TCubicProfile")):
            if len(stack) >= 64:
                raise ValueError("Object nesting exceeds 64 supported levels")
            if stack and isinstance(stack[-1][1], Profile):
                stack[-1][1].problems.append("Nested objects inside curves are unsupported")
            path = "/".join([item[0] for item in stack[1:]] + [name]) if stack else ""
            cls = Profile if value == "TCubicProfile" else ObjectBlock
            block = cls(path, index + 1, value)
            result.append(block)
            stack.append((name, block))
        elif stack:
            stack[-1][1].fields.setdefault(name, []).append((index, value))
    if stack:
        raise ValueError("Unclosed component object")
    return result


def profiles(text: str) -> list[Profile]:
    """Locate curves by their object path, never by flattened Point_N names."""
    return [block for block in objects(text) if isinstance(block, Profile)]


def checked_points(supplied: list, count: int | None = None) -> list[list[float]]:
    if not isinstance(supplied, list):
        raise ValueError("Supply a list of active points")
    if count is not None and len(supplied) != count:
        raise ValueError(f"Supply exactly {count} active points")
    if not 4 <= len(supplied) <= MAX_POINTS:
        raise ValueError(f"Supply 4..{MAX_POINTS} active points")
    result = []
    last_x = -math.inf
    for pair in supplied:
        if (not isinstance(pair, list) or len(pair) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in pair)):
            raise ValueError("Each point must contain two finite numbers")
        try:
            x, y = map(float, pair)
        except (OverflowError, ValueError):
            raise ValueError("Each point must contain two finite numbers") from None
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("Each point must contain two finite numbers")
        if x <= last_x:
            raise ValueError("Active X values must be strictly increasing")
        if x < 1e-9 or x - last_x < 1e-8:
            raise ValueError("Native compilation can remove points: require X >= 1e-9 and gaps >= 1e-8")
        result.append([x, y])
        last_x = x
    return result


def point_count_errors(profile: Profile) -> list[str]:
    """Requirements for replacing a complete, ordered Mode 1 point table."""
    errors = []
    try:
        compile_fields = profile.fields.get("LastCompile", [])
        if len(compile_fields) > 1:
            raise ValueError("LastCompile must occur at most once")
        if compile_fields:
            raw = compile_fields[0][1]
            if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw) or int(raw[1:], 16) not in (0, 0x19, 0x8d, 0x8089):
                raise ValueError("Point-count editing supports LastCompile absent, $0, $19, $008D or $8089")
        capacity = int(profile.one("NPtsMax")[1])
        rows = [profile.one(f"Point_{i}")[0] for i in range(1, capacity + 1)]
        if rows != list(range(rows[0], rows[0] + capacity)):
            raise ValueError("Point-count editing requires consecutive Point_N rows in numeric order")
        if any(index >= rows[0] for key in ("NPtsMax", "NPtsEff", "Mode", "LastCompile")
               for index, _ in profile.fields.get(key, [])):
            raise ValueError("Curve count/mode/compile fields must precede the point table")
    except (ValueError, IndexError) as exc:
        errors.append(str(exc))
    return errors


def write_points(lines: list[str], profile: Profile, previous: list, points: list) -> None:
    if len(points) != len(previous):
        errors = point_count_errors(profile)
        if errors:
            raise ValueError("; ".join(errors))
        capacity_index, capacity = profile.one("NPtsMax")
        effective_index, _ = profile.one("NPtsEff")
        start, _ = profile.one("Point_1")
        template = lines[start]
        # Canonical resized table: every allocated point is active. Preserve
        # Mode/LastCompile; native Mode 1 compilation derives a/b/c from X/Y.
        table = [replace_value(replace_key(template, f"Point_{i}"), f"{x:.17g},{y:.17g}")
                 for i, (x, y) in enumerate(points, 1)]
        lines[capacity_index] = replace_value(lines[capacity_index], str(len(points)))
        lines[effective_index] = replace_value(lines[effective_index], str(len(points)))
        lines[start:start + int(capacity)] = table
        return
    for i, pair in enumerate(points, 1):
        if pair != previous[i - 1]:
            index, _ = profile.one(f"Point_{i}")
            lines[index] = replace_value(lines[index], f"{pair[0]:.17g},{pair[1]:.17g}")


def _curve_control(objects: list[ObjectBlock]) -> tuple[dict, tuple[int, str] | None]:
    control = {"source": "unknown", "three_voltage": None, "flags": None,
               "version": None, "errors": []}
    flag_field = None
    try:
        roots = [block for block in objects if block.path == ""]
        if len(roots) != 1 or roots[0].object_type != "pvGInverter":
            raise ValueError("Expected exactly one root pvGInverter object")
        root = roots[0]
        flag_field = root.one("Flags")
        if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", flag_field[1]):
            raise ValueError("Root Flags must be a hexadecimal value of at most 32 bits")
        flags = int(flag_field[1][1:], 16)
        control.update(flags=flag_field[1], source="automatic" if flags & AUTO_CURVE_MASK else "file",
                       three_voltage=bool(flags & THREE_VOLTAGE_MASK))
        version = root.one("Version")[1]
        control["version"] = version[:256]
        if version != "8.1.6":
            raise ValueError("Curve editing is verified only for OND Version=8.1.6")
        if control["three_voltage"]:
            raise ValueError("Three-voltage OND curve editing is not supported")
        converters = [b for b in objects if b.path == "Converter"]
        if len(converters) != 1 or converters[0].object_type != "TConverter":
            raise ValueError("Expected exactly one root Converter=TConverter")
    except ValueError as exc:
        control["errors"].append(str(exc))
    return control, flag_field


def _edit_errors(profile: Profile, points: list[list[float]]) -> list[str]:
    errors = []
    if profile.path != MAIN_CURVE:
        errors.append("Only Converter/ProfilPIO is editable")
    if len(points) < 4:
        errors.append("Curve editing requires at least four active points")
    try:
        if profile.one("Mode")[1] != "1":
            errors.append("Only profile Mode=1 is supported for editing")
    except ValueError as exc:
        errors.append(str(exc))
    return errors


def curve_inventory(kind: str, text: str) -> dict:
    if kind != "OND":
        return {"scope": "OND power curves only", "items": [], "truncated": False}
    try:
        blocks = objects(text)
        found = [block for block in blocks if isinstance(block, Profile)]
    except ValueError as exc:
        return {"scope": "OND power curves only", "items": [], "errors": [str(exc)], "truncated": False}
    control, _ = _curve_control(blocks)
    items = []
    for profile in found[:16]:
        errors = list(profile.problems)
        points = []
        try:
            points = profile.points()
        except ValueError as exc:
            errors.append(str(exc))
        if sum(other.path == profile.path for other in blocks) != 1:
            errors.append("Ambiguous duplicate curve path")
        supported = profile.path in CURVE_PATHS
        edit_errors = errors + control["errors"] + _edit_errors(profile, points)
        items.append({"path": profile.path, "line": profile.line,
                      "editable": not edit_errors, "structure_complete": not errors, "errors": errors,
                      "edit_errors": edit_errors,
                      "requires_use_file_curve": control["source"] == "automatic",
                      "requires_expected_sha256": True,
                      "points": points, "point_count": len(points),
                      "allocated_point_count": int(profile.one("NPtsMax")[1]) if not errors else None,
                      "point_count_editable": not edit_errors and not point_count_errors(profile),
                      "point_count_errors": point_count_errors(profile) if not errors else errors,
                      "point_count_range": [4, MAX_POINTS],
                      "simulation_effect": "unverified",
                      "axes": ["input power (W)", "output power (W)"] if supported else None})
    return {"scope": "OND power curves only; physical behavior requires simulation",
            "control": control, "items": items, "truncated": len(found) > len(items)}


def replace_curves(kind: str, text: str, updates: dict[str, list[list[float]]],
                   use_file_curve: bool = False) -> str:
    if kind != "OND" or not isinstance(updates, dict) or set(updates) != {MAIN_CURVE}:
        raise ValueError("Curve edits support only OND Converter/ProfilPIO")
    blocks = objects(text)
    control, flag_field = _curve_control(blocks)
    if control["errors"]:
        raise ValueError("; ".join(control["errors"]))
    if control["source"] == "automatic" and not use_file_curve:
        raise ValueError("Automatic curve is enabled; supply use_file_curve=true to use supplied points")
    matches = [b for b in blocks if b.path == MAIN_CURVE]
    if len(matches) != 1 or not isinstance(matches[0], Profile):
        raise ValueError(f"{MAIN_CURVE} must identify exactly one supported curve")
    profile = matches[0]
    previous = profile.points()
    errors = profile.problems + _edit_errors(profile, previous)
    if errors:
        raise ValueError("; ".join(errors))
    points = checked_points(updates[MAIN_CURVE])
    if any(not 0 <= y <= x for x, y in points):
        raise ValueError("Supported power points require 0 <= output <= input")
    lines = text.splitlines(keepends=True)
    if control["source"] == "automatic":
        index, raw = flag_field
        # Bit 4 is the low bit of the second hex digit from the right.
        # Replace only that digit, preserving even mixed-case surrounding hex.
        digit = format(int(raw[-2], 16) & ~1, "x" if raw[-2].islower() else "X")
        lines[index] = replace_value(lines[index], raw[:-2] + digit + raw[-1])
    write_points(lines, profile, previous, points)
    return "".join(lines)

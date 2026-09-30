"""Guarded BTR capacity-versus-discharge-duration curves for PVsystCLI 8.1.6."""
from __future__ import annotations

import math

from pvsyst_component_editor import (ObjectBlock, Profile, checked_points, objects,
                                     replace_key, write_points, point_count_errors, MAX_POINTS)


CAPACITY_TAG = "CapaCourant"
CAPACITY_ALIAS = "Capa_DischRate"
CAPACITY_PATHS = frozenset((CAPACITY_TAG, CAPACITY_ALIAS))
ALIASES = {CAPACITY_ALIAS: CAPACITY_TAG, "SelfDisch_Temp": "IAutoShape",
           "Capa_Temperature": "CapaTemper", "VMaxCharge_Rate": "VMaxChargeRate"}
KNOWN_TAGS = frozenset((*ALIASES.values(), "DVGassShape", "NbCycleMax"))
RATIO_RANGE = (1.15, 1.45)
SCOPE = "BTR capacity versus discharge duration; other battery curves are inspection-only"


def _control(blocks: list[ObjectBlock]) -> dict:
    result = {"source": "unknown", "path": None, "version": None,
              "battery_technology": None, "ratio_100h_range": None, "errors": []}
    try:
        roots = [b for b in blocks if b.path == ""]
        if len(roots) != 1 or roots[0].object_type != "pvBattery":
            raise ValueError("Expected exactly one root pvBattery object")
        root = roots[0]
        version, technology = root.one("Version")[1], root.one("BattTechnol")[1]
        result.update(version=version[:256], battery_technology=technology[:256])
        capacity = [b for b in blocks if b.path in CAPACITY_PATHS]
        if len(capacity) != 1 or not isinstance(capacity[0], Profile):
            raise ValueError("Expected exactly one root CapaCourant or Capa_DischRate curve, never both")
        result.update(path=capacity[0].path,
                      source="file" if capacity[0].path == CAPACITY_TAG else "unrecognized-tag")
        if version != "8.1.6":
            raise ValueError("Battery curve editing is verified only for BTR Version=8.1.6")
        normalized = technology.casefold()
        if normalized.startswith("bt"):
            normalized = normalized[2:]
        if normalized not in ("pb_sealed_agm", "pb_sealed_gel"):
            raise ValueError("Battery curve editing supports Pb_Sealed_AGM and Pb_Sealed_Gel only")
        result["ratio_100h_range"] = list(RATIO_RANGE)
    except ValueError as exc:
        result["errors"].append(str(exc))
    return result


def _profile_errors(profile: Profile, points: list) -> list[str]:
    errors = []
    if len(points) < 4:
        errors.append("Battery capacity curves require at least four active points")
    try:
        if profile.one("Mode")[1] != "1":
            errors.append("Only battery profile Mode=1 (linear) is supported for editing")
    except ValueError as exc:
        errors.append(str(exc))
    return errors


def capacity_ratio(points: list[list[float]]) -> float:
    """Validate our supported domain and interpolate the native C100/C10 check."""
    points = checked_points(points, len(points))
    if len(points) < 4:
        raise ValueError("Battery capacity curves require at least four active points")
    if any(x <= 0 or y <= 0 for x, y in points):
        raise ValueError("Discharge duration and relative capacity must be positive")
    if any(a[1] > b[1] for a, b in zip(points, points[1:])):
        raise ValueError("Supported capacity curves require nondecreasing relative capacity")
    if not points[0][0] <= 100 <= points[-1][0]:
        raise ValueError("Capacity curve must cover 100 hours; extrapolation is unsupported")
    for (xa, ya), (xb, yb) in zip(points, points[1:]):
        if xa <= 100 <= xb:
            ratio = ya + (yb - ya) * ((100 - xa) / (xb - xa))
            if not math.isfinite(ratio) or not RATIO_RANGE[0] <= ratio <= RATIO_RANGE[1]:
                raise ValueError("Capacity ratio C100/C10 at 100 hours must be within [1.15, 1.45]")
            return ratio
    raise ValueError("Capacity curve must cover 100 hours")


def battery_curve_inventory(text: str) -> dict:
    try:
        blocks = objects(text)
    except ValueError as exc:
        return {"scope": SCOPE, "items": [], "errors": [str(exc)], "truncated": False}
    control = _control(blocks)
    found = [b for b in blocks if isinstance(b, Profile)]
    items = []
    for profile in found[:16]:
        errors = list(profile.problems)
        points = []
        try:
            points = profile.points()
        except ValueError as exc:
            errors.append(str(exc))
        if sum(b.path == profile.path for b in blocks) != 1:
            errors.append("Ambiguous duplicate curve path")
        capacity = profile.path in CAPACITY_PATHS
        edit_errors = errors + control["errors"] + _profile_errors(profile, points) if capacity else [
            "Only CapaCourant / Capa_DischRate capacity curves are editable"]
        native_tag = ALIASES.get(profile.path, profile.path if profile.path in KNOWN_TAGS else None)
        item = {"path": profile.path, "line": profile.line,
                "cli_tag": native_tag,
                "cli_recognition": "unrecognized-tag" if profile.path in ALIASES else
                                   "recognized" if native_tag else "unverified",
                "editable": not edit_errors, "edit_errors": edit_errors,
                "structure_complete": not errors, "errors": errors,
                "points": points, "point_count": len(points),
                "allocated_point_count": int(profile.one("NPtsMax")[1]) if not errors else None,
                "point_count_editable": not edit_errors and not point_count_errors(profile),
                "point_count_errors": point_count_errors(profile) if not errors else errors,
                "point_count_range": [4, MAX_POINTS],
                "requires_expected_sha256": True,
                "requires_use_file_curve": profile.path == CAPACITY_ALIAS,
                "simulation_effect": "unverified",
                "axes": ["discharge duration (h)", "capacity relative to C10"] if capacity else None}
        if capacity:
            item.update(ratio_100h=None, value_errors=[])
            try:
                if control["errors"]:
                    raise ValueError("Capacity ratio check is unavailable for this battery configuration")
                if errors:
                    raise ValueError("Cannot check capacity ratio with incomplete curve structure")
                if profile.one("Mode")[1] != "1":
                    raise ValueError("Capacity ratio inspection supports Mode=1 only")
                item["ratio_100h"] = capacity_ratio(points)
            except ValueError as exc:
                item["value_errors"].append(str(exc))
        items.append(item)
    return {"scope": SCOPE, "control": control, "items": items, "truncated": len(found) > len(items)}


def replace_battery_curve(text: str, updates: dict[str, list[list[float]]],
                          use_file_curve: bool = False) -> str:
    if not isinstance(updates, dict) or len(updates) != 1 or not set(updates) <= CAPACITY_PATHS:
        raise ValueError("Battery curve edits support one existing CapaCourant or Capa_DischRate curve")
    blocks = objects(text)
    control = _control(blocks)
    if control["errors"]:
        raise ValueError("; ".join(control["errors"]))
    path = next(iter(updates))
    if path != control["path"]:
        raise ValueError("Use the existing capacity curve path returned by inspection")
    if path == CAPACITY_ALIAS and not use_file_curve:
        raise ValueError("Capa_DischRate is ignored by CLI; supply use_file_curve=true to rename it to CapaCourant")
    profile = next(b for b in blocks if b.path == path)
    previous = profile.points()
    errors = profile.problems + _profile_errors(profile, previous)
    if errors:
        raise ValueError("; ".join(errors))
    points = checked_points(updates[path])
    capacity_ratio(points)
    lines = text.splitlines(keepends=True)
    write_points(lines, profile, previous, points)
    if path == CAPACITY_ALIAS:
        lines[profile.line - 1] = replace_key(lines[profile.line - 1], CAPACITY_TAG)
    return "".join(lines)

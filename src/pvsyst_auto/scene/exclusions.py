"""Pure-stdlib 2D exclusion-zone validation, polygon predicates, and interval subtraction."""
from __future__ import annotations

import math
from typing import Any

from pvsyst_auto.validation import finite_number as _finite

MAX_EXCLUSION_ZONES = 128
_EPS = 1e-9


def polygon_area(pts: list[tuple[float, float]]) -> float:
    """Return unsigned 2D polygon area via the shoelace formula."""
    n = len(pts)
    return 0.5 * abs(sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n)))


def point_segment_distance(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """Return Euclidean distance from point (px, py) to segment (x1, y1)-(x2, y2)."""
    dx, dy = x2 - x1, y2 - y1
    len_sq = dx * dx + dy * dy
    if len_sq <= _EPS:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / len_sq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _ray_cast_inside(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    inside, n = False, len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon check (inclusive of boundary within 1e-7 m)."""
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if point_segment_distance(x, y, x1, y1, x2, y2) <= 1e-7:
            return True
    return _ray_cast_inside(x, y, poly)


def segments_intersect(
    ax: float, ay: float, bx: float, by: float,
    cx: float, cy: float, dx: float, dy: float,
) -> bool:
    """Return True if closed segments AB and CD intersect (shift-invariant for UTM coords)."""
    if (
        max(ax, bx) < min(cx, dx) - _EPS
        or max(cx, dx) < min(ax, bx) - _EPS
        or max(ay, by) < min(cy, dy) - _EPS
        or max(cy, dy) < min(ay, by) - _EPS
    ):
        return False
    abx, aby = bx - ax, by - ay
    cx_, cy_ = cx - ax, cy - ay
    dx_, dy_ = dx - ax, dy - ay
    c1 = abx * cy_ - aby * cx_
    c2 = abx * dy_ - aby * dx_
    cdx, cdy = dx_ - cx_, dy_ - cy_
    c3 = cdx * (-cy_) - cdy * (-cx_)
    c4 = cdx * (aby - cy_) - cdy * (abx - cx_)
    if ((c1 > 0.0 and c2 < 0.0) or (c1 < 0.0 and c2 > 0.0)) and (
        (c3 > 0.0 and c4 < 0.0) or (c3 < 0.0 and c4 > 0.0)
    ):
        return True
    return (
        point_segment_distance(ax, ay, cx, cy, dx, dy) <= _EPS
        or point_segment_distance(bx, by, cx, cy, dx, dy) <= _EPS
        or point_segment_distance(cx, cy, ax, ay, bx, by) <= _EPS
        or point_segment_distance(dx, dy, ax, ay, bx, by) <= _EPS
    )


def rect_overlaps_polygon(
    u0: float, u1: float, v0: float, v1: float,
    poly: list[tuple[float, float]], *, tol: float = 1e-7,
) -> bool:
    """Return True if rectangle [u0, u1] x [v0, v1] overlaps the interior of poly by > tol."""
    if u0 > u1:
        u0, u1 = u1, u0
    if v0 > v1:
        v0, v1 = v1, v0
    if u1 - u0 > 2.0 * tol:
        su0, su1 = u0 + tol, u1 - tol
    else:
        su0 = su1 = 0.5 * (u0 + u1)
    if v1 - v0 > 2.0 * tol:
        sv0, sv1 = v0 + tol, v1 - tol
    else:
        sv0 = sv1 = 0.5 * (v0 + v1)

    pxs = [p[0] for p in poly]
    pys = [p[1] for p in poly]
    if max(pxs) < su0 - _EPS or min(pxs) > su1 + _EPS or max(pys) < sv0 - _EPS or min(pys) > sv1 + _EPS:
        return False

    for cu, cv in ((su0, sv0), (su1, sv0), (su1, sv1), (su0, sv1)):
        if _ray_cast_inside(cu, cv, poly):
            return True

    for px, py in poly:
        if su0 < px < su1 and sv0 < py < sv1:
            return True

    rect_edges = (
        (su0, sv0, su1, sv0),
        (su1, sv0, su1, sv1),
        (su1, sv1, su0, sv1),
        (su0, sv1, su0, sv0),
    )
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        for rx1, ry1, rx2, ry2 in rect_edges:
            if segments_intersect(x1, y1, x2, y2, rx1, ry1, rx2, ry2):
                return True
    return False


def rect_overlaps_any_polygon(
    u0: float, u1: float, v0: float, v1: float,
    polys: list[list[tuple[float, float]]], *, tol: float = 1e-7,
) -> bool:
    """Return True if rectangle [u0, u1] x [v0, v1] overlaps any exclusion polygon."""
    return any(rect_overlaps_polygon(u0, u1, v0, v1, poly, tol=tol) for poly in polys)


def validate_exclusion_zones(exclusion_zones: Any) -> list[list[tuple[float, float]]]:
    """Validate optional list of 2D exclusion polygons [[x, y], ...]."""
    if exclusion_zones is None:
        return []
    if not isinstance(exclusion_zones, list):
        raise ValueError("exclusion_zones must be a list of polygons")
    if len(exclusion_zones) > MAX_EXCLUSION_ZONES:
        raise ValueError(f"exclusion_zones supports at most {MAX_EXCLUSION_ZONES} polygons")
    out: list[list[tuple[float, float]]] = []
    for z_idx, raw_poly in enumerate(exclusion_zones):
        if not isinstance(raw_poly, (list, tuple)) or not 3 <= len(raw_poly) <= 64:
            raise ValueError(f"exclusion_zones[{z_idx}] must be a list of 3..64 [x, y] vertices")
        pts: list[tuple[float, float]] = []
        for v_idx, pt in enumerate(raw_poly):
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                raise ValueError(f"exclusion_zones[{z_idx}][{v_idx}] must be a [x, y] pair")
            pts.append((
                _finite(pt[0], f"exclusion_zones[{z_idx}][{v_idx}][0]"),
                _finite(pt[1], f"exclusion_zones[{z_idx}][{v_idx}][1]"),
            ))
        if len(pts) > 3 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) <= _EPS:
            pts.pop()
        if len(pts) < 3 or polygon_area(pts) <= 1e-3:
            raise ValueError(f"exclusion_zones[{z_idx}] must enclose a positive non-degenerate area")
        out.append(pts)
    return out


def merge_intervals(intervals: list[tuple[float, float]], *, eps: float = _EPS) -> list[tuple[float, float]]:
    """Sort and merge overlapping or touching [lo, hi] 1D intervals."""
    valid = [(lo, hi) for lo, hi in intervals if hi >= lo - eps]
    if not valid:
        return []
    valid.sort(key=lambda seg: (seg[0], seg[1]))
    merged: list[tuple[float, float]] = [(min(valid[0][0], valid[0][1]), max(valid[0][0], valid[0][1]))]
    for lo, hi in valid[1:]:
        lo, hi = (lo, hi) if lo <= hi else (hi, lo)
        prev_lo, prev_hi = merged[-1]
        if lo <= prev_hi + eps:
            merged[-1] = (prev_lo, max(prev_hi, hi))
        else:
            merged.append((lo, hi))
    return merged


def subtract_intervals(
    base: list[tuple[float, float]],
    cutouts: list[tuple[float, float]],
    *,
    min_length: float = 0.0,
) -> list[tuple[float, float]]:
    """Subtract merged cutouts from base intervals and retain segments with length >= min_length."""
    if not base:
        return []
    merged_cuts = merge_intervals(cutouts)
    if not merged_cuts:
        return [(lo, hi) for lo, hi in base if hi - lo >= min_length]
    surviving: list[tuple[float, float]] = []
    for seg_lo, seg_hi in base:
        if seg_hi - seg_lo < min_length:
            continue
        pieces: list[tuple[float, float]] = [(seg_lo, seg_hi)]
        for cut_lo, cut_hi in merged_cuts:
            next_pieces: list[tuple[float, float]] = []
            for p_lo, p_hi in pieces:
                if cut_hi <= p_lo + _EPS or cut_lo >= p_hi - _EPS:
                    next_pieces.append((p_lo, p_hi))
                else:
                    if cut_lo - p_lo >= min_length:
                        next_pieces.append((p_lo, cut_lo))
                    if p_hi - cut_hi >= min_length:
                        next_pieces.append((cut_hi, p_hi))
            pieces = next_pieces
            if not pieces:
                break
        surviving.extend(pieces)
    return [(lo, hi) for lo, hi in surviving if hi - lo >= min_length]


def polygon_x_shadow_in_band(poly: list[tuple[float, float]], y0: float, y1: float) -> list[tuple[float, float]]:
    """Return exact merged X-intervals of poly intersected with horizontal band y0 <= y <= y1."""
    if y0 > y1:
        y0, y1 = y1, y0
    pys = [p[1] for p in poly]
    if max(pys) < y0 - _EPS or min(pys) > y1 + _EPS:
        return []
    eps_y = max(_EPS, 1e-12 * max(1.0, abs(y0), abs(y1)))
    n = len(poly)
    if y1 - y0 <= eps_y:
        ym = 0.5 * (y0 + y1)
        xs: list[float] = []
        for i in range(n):
            x1, y1_e = poly[i]
            x2, y2_e = poly[(i + 1) % n]
            if (y1_e <= ym < y2_e) or (y2_e <= ym < y1_e):
                xs.append(x1 + (ym - y1_e) * (x2 - x1) / (y2_e - y1_e))
        xs.sort()
        return merge_intervals([(xs[i], xs[i + 1]) for i in range(0, len(xs) - 1, 2)], eps=_EPS)

    raw_ys = sorted([y0, y1, *(py for _, py in poly if y0 < py < y1)])
    levels = [y0]
    for yv in raw_ys:
        if y1 - yv <= eps_y:
            continue
        if yv - levels[-1] > eps_y:
            levels.append(yv)
    levels.append(y1)

    shadows: list[tuple[float, float]] = []
    for s_idx in range(len(levels) - 1):
        ya, yb = levels[s_idx], levels[s_idx + 1]
        ym = 0.5 * (ya + yb)
        crossings: list[tuple[float, float, float]] = []
        for i in range(n):
            x1, y1_e = poly[i]
            x2, y2_e = poly[(i + 1) % n]
            if (y1_e <= ym < y2_e) or (y2_e <= ym < y1_e):
                inv_dy = (x2 - x1) / (y2_e - y1_e)
                ya_c = max(ya, min(y1_e, y2_e))
                yb_c = min(yb, max(y1_e, y2_e))
                crossings.append((
                    x1 + (ym - y1_e) * inv_dy,
                    x1 + (ya_c - y1_e) * inv_dy,
                    x1 + (yb_c - y1_e) * inv_dy,
                ))
        crossings.sort(key=lambda item: (item[0], min(item[1], item[2])))
        for i in range(0, len(crossings) - 1, 2):
            _, l1, l2 = crossings[i]
            _, r1, r2 = crossings[i + 1]
            shadows.append((min(l1, l2, r1, r2), max(l1, l2, r1, r2)))
    return merge_intervals(shadows, eps=_EPS)


def subtract_exclusions_from_intervals(
    intervals: list[tuple[float, float]],
    exclusions: list[list[tuple[float, float]]],
    y0: float,
    y1: float,
    *,
    margin: float = 0.0,
    min_length: float = 0.0,
) -> list[tuple[float, float]]:
    """Subtract 2D exclusion polygon shadows over band [y0, y1] from 1D row intervals."""
    if not intervals:
        return []
    if not exclusions:
        return [(lo, hi) for lo, hi in intervals if hi - lo >= min_length]
    cutouts: list[tuple[float, float]] = []
    for poly in exclusions:
        for lo, hi in polygon_x_shadow_in_band(poly, y0 - margin, y1 + margin):
            cutouts.append((lo - margin, hi + margin) if margin > 0.0 else (lo, hi))
    return subtract_intervals(intervals, cutouts, min_length=min_length)

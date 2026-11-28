"""Pure-stdlib ASCII .dxf parser extracting site polygon, exclusion zones, 3D obstacles, and terrain."""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from pvsyst_auto.project.paths import workspace_file
from pvsyst_auto.scene.author import MAX_OBSTACLES, obstacle_profiles
from pvsyst_auto.scene.exclusions import MAX_EXCLUSION_ZONES, is_self_intersecting_polygon, polygon_area, round_closed_polygon, validate_exclusion_zones
from pvsyst_auto.scene.reader import validate_site_polygon
from pvsyst_auto.validation import finite_number as _finite

MAX_DXF_BYTES = 64 * 1024 * 1024
MAX_DXF_TERRAIN_POINTS = 2500
MAX_DXF_INSERT_DEPTH = 8
MAX_DXF_ENTITIES = 50000
MAX_DXF_VERTICES = 500_000

_INSUNITS_TO_M: dict[int, float] = {
    0: 1.0, 1: 0.0254, 2: 0.3048, 3: 1609.344, 4: 0.001, 5: 0.01,
    6: 1.0, 7: 1000.0, 8: 1e-6, 9: 0.0254 / 1000.0, 10: 0.9144, 14: 0.1,
}
_EXCLUSION_RE = re.compile(r"(?:excl|keepout|road|pad|pond|buffer|void|hole|no_?pv)", re.I)
_BOUNDARY_RE = re.compile(r"(?:bound|site|parcel|perimet|fence|property|pv_?area|limit)", re.I)
_OBSTACLE_RE = re.compile(r"(?:obst|bldg|build|shade|shadow|tree|struct|wall)", re.I)
_TERRAIN_RE = re.compile(r"(?:topo|terrain|dtm|dem|contour|elev|ground|grade|surf)", re.I)
_DXF_LAYER_ROLES = frozenset(("boundary", "obstacles", "terrain", "exclusions"))

_FLOAT_FIELDS: dict[str, dict[int, str]] = {
    "VERTEX": {10: "x", 20: "y", 30: "z", 42: "bulge"},
    "LWPOLYLINE": {210: "nx", 220: "ny", 230: "nz"},
    "POLYLINE": {30: "z0", 39: "thick", 210: "nx", 220: "ny", 230: "nz"},
    "POINT": {10: "x", 20: "y", 30: "z"},
    "CIRCLE": {10: "cx", 20: "cy", 30: "cz", 39: "thick", 40: "r", 210: "nx", 220: "ny", 230: "nz"},
    "ARC": {10: "cx", 20: "cy", 30: "cz", 39: "thick", 40: "r", 50: "start", 51: "end", 210: "nx", 220: "ny", 230: "nz"},
    "ELLIPSE": {10: "cx", 20: "cy", 30: "cz", 11: "mx", 21: "my", 31: "mz", 40: "ratio", 41: "start", 42: "end", 210: "nx", 220: "ny", 230: "nz"},
    "LINE": {10: "x1", 20: "y1", 30: "z1", 11: "x2", 21: "y2", 31: "z2"},
    "INSERT": {10: "x", 20: "y", 30: "z", 41: "sx", 42: "sy", 43: "sz", 50: "rot", 44: "col_sp", 45: "row_sp", 210: "nx", 220: "ny", 230: "nz"},
}


def read_bounded_cad_bytes(workspace: Path, filename: str, expected_ext: str) -> tuple[Path, bytes]:
    """Read a workspace UserData CAD file enforcing 64 MiB ceiling and extension safety."""
    path = workspace_file(workspace, "UserData", filename, expected_ext)
    if not path.is_file():
        raise ValueError(f"CAD file not found in workspace UserData: {filename}")
    if path.stat().st_size > MAX_DXF_BYTES:
        raise ValueError(f"CAD file exceeds 64 MiB limit: {filename}")
    with path.open("rb") as handle:
        raw = handle.read(MAX_DXF_BYTES + 1)
    if len(raw) > MAX_DXF_BYTES:
        raise ValueError(f"CAD file exceeds 64 MiB limit: {filename}")
    return path, raw


def downsample_xyz_points(points: list[tuple[float, float, float]], limit: int = MAX_DXF_TERRAIN_POINTS) -> list[list[float]]:
    """Deduplicate XYZ points at 1 cm XY tolerance and bound to limit."""
    seen: dict[tuple[int, int], tuple[float, float, float]] = {}
    for x, y, z in points:
        key = (round(x * 100.0), round(y * 100.0))
        if key not in seen:
            seen[key] = (x, y, z)
    deduped = list(seen.values())
    if len(deduped) > limit:
        step = len(deduped) / limit
        deduped = [deduped[int(i * step)] for i in range(limit)]
    return [[round(x, 4), round(y, 4), round(z, 4)] for x, y, z in deduped]


def _douglas_peucker_2d(pts: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    if len(pts) <= 2:
        return pts
    ax, ay = pts[0]
    bx, by = pts[-1]
    dx, dy = bx - ax, by - ay
    seg_len = math.hypot(dx, dy)
    max_d, idx = -1.0, 0
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        d = abs((px - ax) * dy - (py - ay) * dx) / seg_len if seg_len > 1e-12 else math.hypot(px - ax, py - ay)
        if d > max_d:
            max_d, idx = d, i
    if max_d > eps:
        return _douglas_peucker_2d(pts[:idx + 1], eps)[:-1] + _douglas_peucker_2d(pts[idx:], eps)
    return [pts[0], pts[-1]]


def _simplify_closed_polygon(pts: list[tuple[float, float]], max_verts: int = 64) -> tuple[list[tuple[float, float]], bool]:
    clean: list[tuple[float, float]] = []
    for x, y in pts:
        if not clean or math.hypot(x - clean[-1][0], y - clean[-1][1]) > 1e-6:
            clean.append((x, y))
    if len(clean) > 3 and math.hypot(clean[0][0] - clean[-1][0], clean[0][1] - clean[-1][1]) <= 1e-6:
        clean.pop()
    if len(clean) <= max_verts:
        return clean, False
    span = max(max(p[0] for p in clean) - min(p[0] for p in clean), max(p[1] for p in clean) - min(p[1] for p in clean), 1.0)
    lo, hi, closed, best = 0.0, span * 0.2, clean + [clean[0]], clean
    for _ in range(28):
        mid = 0.5 * (lo + hi)
        simp = _douglas_peucker_2d(closed, mid)[:-1]
        if len(simp) <= max_verts:
            best, hi = simp, mid
        else:
            lo = mid
    if len(best) > max_verts:
        step = len(best) / max_verts
        best = [best[int(i * step)] for i in range(max_verts)]
    return best, True


def _extrude_polygon_prism(poly: list[tuple[float, float]], z0: float, z1: float) -> tuple[list[list[float]], list[list[int]]] | None:
    pts_2d = round_closed_polygon(poly, 5)
    n = len(pts_2d)
    if n < 3 or round(z1, 5) <= round(z0, 5):
        return None
    area2 = sum(pts_2d[i][0] * pts_2d[(i + 1) % n][1] - pts_2d[(i + 1) % n][0] * pts_2d[i][1] for i in range(n))
    pts = list(reversed(pts_2d)) if area2 < 0.0 else list(pts_2d)
    rz0, rz1 = round(z0, 5), round(z1, 5)
    verts = [[x, y, rz0] for x, y in pts] + [[x, y, rz1] for x, y in pts]
    faces: list[list[int]] = []
    for i in range(1, n - 1):
        ax, ay = pts[0]
        bx, by = pts[i]
        cx, cy = pts[i + 1]
        if abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) > 1e-8:
            faces.extend(([0, i + 1, i], [n, n + i, n + i + 1]))
    for i in range(n):
        j = (i + 1) % n
        faces.extend(([i, j, n + j], [i, n + j, n + i]))
    return (verts, faces) if faces else None


def normalize_dxf_layers(dxf_layers: dict[str, Any] | None) -> dict[str, set[str]]:
    """Validate and case-fold optional DXF layer role mapping."""
    if dxf_layers is None:
        return {}
    if not isinstance(dxf_layers, dict):
        raise ValueError("dxf_layers must be an object mapping boundary/exclusions/obstacles/terrain to layer names")
    out: dict[str, set[str]] = {}
    for role, val in dxf_layers.items():
        if role not in _DXF_LAYER_ROLES:
            raise ValueError(
                f"Unsupported dxf_layers role {role!r}; choose 'boundary', 'exclusions', 'obstacles', or 'terrain'"
            )
        names = [val] if isinstance(val, str) else val if isinstance(val, list) else None
        if names is None or not names or any(not isinstance(s, str) or not s.strip() for s in names):
            raise ValueError(f"dxf_layers.{role} must be a non-empty layer name or list of layer names")
        out[role] = {s.strip().casefold() for s in names}
    return out


def _dxf_pairs(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    pairs: list[tuple[int, str]] = []
    i = 0
    while i + 1 < len(lines):
        try:
            code = int(lines[i].strip())
        except ValueError as exc:
            raise ValueError(f"Invalid DXF group code at line {i + 1}: {lines[i]!r}") from exc
        pairs.append((code, lines[i + 1].strip()))
        i += 2
    return pairs


def _classify_layer(layer: str, role_map: dict[str, set[str]]) -> str | None:
    lf = layer.casefold()
    for role in ("boundary", "exclusions", "obstacles", "terrain"):
        if lf in role_map.get(role, ()):
            return role
    for role, pattern in (
        ("exclusions", _EXCLUSION_RE), ("boundary", _BOUNDARY_RE),
        ("terrain", _TERRAIN_RE), ("obstacles", _OBSTACLE_RE),
    ):
        if role not in role_map and pattern.search(layer):
            return role
    return None


def _mat_identity() -> tuple[tuple[float, float, float, float], ...]:
    return ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0))


def _mat_mul(
    a: tuple[tuple[float, float, float, float], ...],
    b: tuple[tuple[float, float, float, float], ...],
) -> tuple[tuple[float, float, float, float], ...]:
    return tuple(
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) + (a[i][3] if j == 3 else 0.0) for j in range(4))
        for i in range(3)
    )


def _ocs_matrix(nx: float, ny: float, nz: float) -> tuple[tuple[float, float, float, float], ...]:
    n_len = math.sqrt(nx * nx + ny * ny + nz * nz)
    if n_len <= 1e-9:
        return _mat_identity()
    nx, ny, nz = nx / n_len, ny / n_len, nz / n_len
    ax, ay, az = (nz, 0.0, -nx) if (abs(nx) < 1.0 / 64.0 and abs(ny) < 1.0 / 64.0) else (-ny, nx, 0.0)
    a_len = math.sqrt(ax * ax + ay * ay + az * az)
    ax, ay, az = ax / a_len, ay / a_len, az / a_len
    bx, by, bz = ny * az - nz * ay, nz * ax - nx * az, nx * ay - ny * ax
    return ((ax, bx, nx, 0.0), (ay, by, ny, 0.0), (az, bz, nz, 0.0))


def _insert_matrix(
    base: tuple[float, float, float], pos: tuple[float, float, float], scale: tuple[float, float, float], rot_deg: float,
) -> tuple[tuple[float, float, float, float], ...]:
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)
    sx, sy, sz = scale
    bx, by, bz = base
    px, py, pz = pos
    r00, r01, r10, r11 = c * sx, -s * sy, s * sx, c * sy
    return ((r00, r01, 0.0, px - (r00 * bx + r01 * by)), (r10, r11, 0.0, py - (r10 * bx + r11 * by)), (0.0, 0.0, sz, pz - sz * bz))


def _apply_mat(m: tuple[tuple[float, float, float, float], ...], pt: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        m[0][0] * pt[0] + m[0][1] * pt[1] + m[0][2] * pt[2] + m[0][3],
        m[1][0] * pt[0] + m[1][1] * pt[1] + m[1][2] * pt[2] + m[1][3],
        m[2][0] * pt[0] + m[2][1] * pt[1] + m[2][2] * pt[2] + m[2][3],
    )


def _discretize_bulge_chain(verts: list[tuple[float, float, float, float]], closed: bool) -> list[tuple[float, float, float]]:
    """Tessellate (x, y, z, bulge) vertices into 3D polyline vertices."""
    n = len(verts)
    if n == 0:
        return []
    if not closed and n == 1:
        return [(verts[0][0], verts[0][1], verts[0][2])]
    out: list[tuple[float, float, float]] = []
    for i in range(n if closed else n - 1):
        x1, y1, z1, bulge = verts[i]
        x2, y2, z2, _ = verts[(i + 1) % n]
        out.append((x1, y1, z1))
        if abs(bulge) > 1e-9:
            dx, dy = x2 - x1, y2 - y1
            if math.hypot(dx, dy) > 1e-12:
                k = 0.25 * (1.0 - bulge * bulge) / bulge
                cx, cy = 0.5 * (x1 + x2) - k * dy, 0.5 * (y1 + y2) + k * dx
                r, a1, theta = math.hypot(x1 - cx, y1 - cy), math.atan2(y1 - cy, x1 - cx), 4.0 * math.atan(bulge)
                n_arc = max(2, min(64, int(math.ceil(abs(theta) / math.radians(10.0)))))
                for j in range(1, n_arc):
                    frac = j / n_arc
                    ang = a1 + theta * frac
                    out.append((cx + r * math.cos(ang), cy + r * math.sin(ang), z1 + (z2 - z1) * frac))
    if not closed:
        out.append((verts[-1][0], verts[-1][1], verts[-1][2]))
    return out


def _discretize_circle(cx: float, cy: float, cz: float, r: float, n_seg: int = 32) -> list[tuple[float, float, float]]:
    if r <= 1e-9:
        return []
    return [(cx + r * math.cos(2.0 * math.pi * i / n_seg), cy + r * math.sin(2.0 * math.pi * i / n_seg), cz) for i in range(n_seg)]


def _discretize_arc(cx: float, cy: float, cz: float, r: float, start_deg: float, end_deg: float) -> tuple[list[tuple[float, float, float]], bool]:
    if r <= 1e-9:
        return [], False
    sweep = (end_deg - start_deg) % 360.0
    full = sweep <= 1e-9
    sweep = 360.0 if full else sweep
    n_seg = max(4, min(64, int(math.ceil(sweep / 10.0))))
    pts = [
        (cx + r * math.cos(math.radians(start_deg + sweep * j / n_seg)), cy + r * math.sin(math.radians(start_deg + sweep * j / n_seg)), cz)
        for j in range(n_seg if full else n_seg + 1)
    ]
    return pts, full


def _discretize_ellipse(ent: dict[str, Any]) -> tuple[list[tuple[float, float, float]], bool]:
    cx, cy, cz = ent["cx"], ent["cy"], ent["cz"]
    mx, my, mz = ent["mx"], ent["my"], ent["mz"]
    ratio, start_rad, end_rad = ent["ratio"], ent["start"], ent["end"]
    nx, ny, nz = ent["nx"], ent["ny"], ent["nz"]
    a = math.sqrt(mx * mx + my * my + mz * mz)
    if a <= 1e-9 or ratio <= 1e-9:
        return [], False
    n_len = math.sqrt(nx * nx + ny * ny + nz * nz)
    if n_len <= 1e-9:
        nx, ny, nz = 0.0, 0.0, 1.0
    else:
        nx, ny, nz = nx / n_len, ny / n_len, nz / n_len
    ux, uy, uz = mx / a, my / a, mz / a
    vx, vy, vz = ny * uz - nz * uy, nz * ux - nx * uz, nx * uy - ny * ux
    v_len = math.sqrt(vx * vx + vy * vy + vz * vz)
    if v_len <= 1e-9:
        return [], False
    b = a * ratio
    bx, by, bz = b * vx / v_len, b * vy / v_len, b * vz / v_len
    tau = 2.0 * math.pi
    raw_diff = end_rad - start_rad
    if abs(raw_diff) >= tau - 1e-4:
        full, sweep = True, tau
    else:
        sweep = raw_diff % tau
        if sweep <= 1e-6:
            return [], False
        full = sweep >= tau - 1e-4
        if full:
            sweep = tau
    n_seg = max(8, min(64, int(math.ceil(sweep / math.radians(10.0)))))
    pts = [(cx + math.cos(t) * mx + math.sin(t) * bx, cy + math.cos(t) * my + math.sin(t) * by, cz + math.cos(t) * mz + math.sin(t) * bz)
           for t in (start_rad + sweep * j / n_seg for j in range(n_seg if full else n_seg + 1))]
    return pts, full


def _de_boor(u: float, knots: list[float], ctrl: list[tuple[float, float, float]], weights: list[float], p: int) -> tuple[float, float, float]:
    n = len(ctrl)
    u_min, u_max = knots[p], knots[n]
    if u <= u_min:
        u = u_min
    elif u >= u_max:
        if knots[n] == knots[n + p]:
            return ctrl[-1]
        u = u_max
    for idx in range(p, n):
        if knots[idx] <= u < knots[idx + 1]:
            k = idx
            break
    else:
        k = n - 1
        while k > p and knots[k] >= knots[k + 1]:
            k -= 1
    d = [[ctrl[j][0] * weights[j], ctrl[j][1] * weights[j], ctrl[j][2] * weights[j], weights[j]] for j in range(k - p, k + 1)]
    for r in range(1, p + 1):
        for j in range(p, r - 1, -1):
            i = k - p + j
            denom = knots[i + p - r + 1] - knots[i]
            alpha = (u - knots[i]) / denom if denom > 1e-12 else 0.0
            one_minus = 1.0 - alpha
            for ax in range(4):
                d[j][ax] = one_minus * d[j - 1][ax] + alpha * d[j][ax]
    w = d[p][3] if abs(d[p][3]) > 1e-12 else 1.0
    return (d[p][0] / w, d[p][1] / w, d[p][2] / w)


def _catmull_rom_fit(fit: list[tuple[float, float, float]], closed: bool) -> tuple[list[tuple[float, float, float]], bool]:
    pts = list(fit)
    if len(pts) >= 3 and math.dist(pts[0], pts[-1]) <= 1e-5:
        closed = True
        pts.pop()
    if len(pts) < 2 or len(pts) == 2:
        return pts, closed if len(pts) < 2 else False
    n = len(pts)
    n_segs = n if closed else n - 1
    steps = max(2, min(8, 64 // max(1, n_segs)))
    out: list[tuple[float, float, float]] = []

    def _lerp(a: tuple[float, float, float], b: tuple[float, float, float], ta: float, tb: float, t: float) -> tuple[float, float, float]:
        w = (t - ta) / (tb - ta)
        return (a[0] + (b[0] - a[0]) * w, a[1] + (b[1] - a[1]) * w, a[2] + (b[2] - a[2]) * w)

    for i in range(n_segs):
        if closed:
            p0, p1, p2, p3 = pts[(i - 1) % n], pts[i], pts[(i + 1) % n], pts[(i + 2) % n]
        else:
            p1, p2 = pts[i], pts[i + 1]
            p0 = pts[i - 1] if i > 0 else (2.0 * p1[0] - p2[0], 2.0 * p1[1] - p2[1], 2.0 * p1[2] - p2[2])
            p3 = pts[i + 2] if i + 2 < n else (2.0 * p2[0] - p1[0], 2.0 * p2[1] - p1[1], 2.0 * p2[2] - p1[2])
        t0 = 0.0
        t1 = t0 + max(math.dist(p0, p1) ** 0.5, 1e-6)
        t2 = t1 + max(math.dist(p1, p2) ** 0.5, 1e-6)
        t3 = t2 + max(math.dist(p2, p3) ** 0.5, 1e-6)
        for s in range(steps):
            t = t1 + (t2 - t1) * (s / steps)
            a1, a2, a3 = _lerp(p0, p1, t0, t1, t), _lerp(p1, p2, t1, t2, t), _lerp(p2, p3, t2, t3, t)
            out.append(_lerp(_lerp(a1, a2, t0, t2, t), _lerp(a2, a3, t1, t3, t), t1, t2, t))
    if not closed:
        out.append(pts[-1])
    return out, closed


def _discretize_spline(ent: dict[str, Any]) -> tuple[list[tuple[float, float, float]], bool]:
    ctrl, fit, knots, weights = ent["ctrl_pts"], ent["fit_pts"], ent["knots"], ent["weights"]
    closed = bool(ent["flag"] & 1)
    degree = max(1, min(int(ent.get("degree", 3)), 5))
    if len(ctrl) >= 2:
        p = min(degree, len(ctrl) - 1)
        valid_knots = (
            len(knots) == len(ctrl) + p + 1
            and all(knots[i] <= knots[i + 1] + 1e-12 for i in range(len(knots) - 1))
            and knots[len(ctrl)] > knots[p] + 1e-12
        )
        if valid_knots:
            w_ctrl, w_knots = list(ctrl), list(knots)
            w_weights = list(weights) if len(weights) == len(ctrl) else [1.0] * len(ctrl)
        elif closed and len(ctrl) >= 3 and math.dist(ctrl[0], ctrl[-1]) > 1e-7:
            w_ctrl = list(ctrl) + list(ctrl[:p])
            w_weights = (list(weights) + list(weights[:p])) if len(weights) == len(ctrl) else [1.0] * len(w_ctrl)
            w_knots = [float(i) for i in range(len(w_ctrl) + p + 1)]
        else:
            w_ctrl = list(ctrl)
            w_weights = list(weights) if len(weights) == len(ctrl) else [1.0] * len(ctrl)
            spans = max(1, len(w_ctrl) - p)
            w_knots = [0.0] * (p + 1) + [float(i) for i in range(1, spans)] + [float(spans)] * (p + 1)
        u0, u1 = w_knots[p], w_knots[len(w_ctrl)]
        m = max(16, min(64, len(w_ctrl) * 8))
        pts = [_de_boor(u0 + (u1 - u0) * (i / m), w_knots, w_ctrl, w_weights, p) for i in range(m if closed else m + 1)]
        is_closed = closed or (len(pts) >= 4 and math.dist(pts[0], pts[-1]) <= 1e-4)
        if is_closed and len(pts) >= 4 and math.dist(pts[0], pts[-1]) <= 1e-5:
            pts.pop()
        return pts, is_closed
    if len(fit) >= 2:
        if len(fit) == 2 or degree <= 1:
            return list(fit), closed or math.dist(fit[0], fit[-1]) <= 1e-4
        return _catmull_rom_fit(fit, closed)
    return [], False


def _stitch_open_loops(chains: list[list[tuple[float, float]]], tol: float = 1e-3) -> list[list[tuple[float, float]]]:
    """Stitch connected open 2D polylines endpoint-to-endpoint into closed polygons."""
    pool = [list(c) for c in chains if len(c) >= 2]
    if not pool:
        return []
    used, cell = [False] * len(pool), max(tol * 4.0, 1e-3)
    buckets: dict[tuple[int, int], list[tuple[int, bool]]] = {}
    for idx, ch in enumerate(pool):
        for pt, at_start in ((ch[0], True), (ch[-1], False)):
            buckets.setdefault((round(pt[0] / cell), round(pt[1] / cell)), []).append((idx, at_start))

    def _find_match(pt: tuple[float, float]) -> tuple[int, bool] | None:
        gx, gy = round(pt[0] / cell), round(pt[1] / cell)
        best: tuple[float, int, bool] | None = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for idx, at_start in buckets.get((gx + dx, gy + dy), ()):
                    if not used[idx]:
                        cand = pool[idx][0] if at_start else pool[idx][-1]
                        if (d := math.hypot(pt[0] - cand[0], pt[1] - cand[1])) <= tol and (best is None or d < best[0]):
                            best = (d, idx, at_start)
        return (best[1], best[2]) if best is not None else None

    closed_polys: list[list[tuple[float, float]]] = []
    for start_idx in range(len(pool)):
        if used[start_idx]:
            continue
        used[start_idx], cur = True, list(pool[start_idx])
        while not (len(cur) >= 3 and math.hypot(cur[0][0] - cur[-1][0], cur[0][1] - cur[-1][1]) <= tol):
            if (hit := _find_match(cur[-1])) is not None:
                used[hit[0]] = True
                cur.extend((pool[hit[0]] if hit[1] else list(reversed(pool[hit[0]])))[1:])
            elif (hit_f := _find_match(cur[0])) is not None:
                used[hit_f[0]] = True
                cur = (list(reversed(pool[hit_f[0]])) if hit_f[1] else pool[hit_f[0]])[:-1] + cur
            else:
                break
        if len(cur) >= 4 and math.hypot(cur[0][0] - cur[-1][0], cur[0][1] - cur[-1][1]) <= tol:
            cur.pop()
            if len(cur) >= 3:
                closed_polys.append(cur)
    return closed_polys


def _new_entity(et: str) -> dict[str, Any] | None:
    if et == "LWPOLYLINE":
        return {"type": "LWPOLYLINE", "layer": "0", "closed": False, "z0": 0.0, "thick": 0.0, "pts": [], "_cur": [None, None, 0.0]}
    if et == "3DFACE":
        return {"type": "3DFACE", "layer": "0", "coords": {}}
    if et == "POINT":
        return {"type": "POINT", "layer": "0", "x": 0.0, "y": 0.0, "z": 0.0}
    if et == "CIRCLE":
        return {"type": "CIRCLE", "layer": "0", "cx": 0.0, "cy": 0.0, "cz": 0.0, "r": 0.0, "thick": 0.0}
    if et == "ARC":
        return {"type": "ARC", "layer": "0", "cx": 0.0, "cy": 0.0, "cz": 0.0, "r": 0.0, "start": 0.0, "end": 360.0, "thick": 0.0}
    if et == "ELLIPSE":
        return {"type": "ELLIPSE", "layer": "0", "cx": 0.0, "cy": 0.0, "cz": 0.0,
                "mx": 0.0, "my": 0.0, "mz": 0.0, "ratio": 1.0, "start": 0.0, "end": 2.0 * math.pi,
                "nx": 0.0, "ny": 0.0, "nz": 1.0}
    if et == "LINE":
        return {"type": "LINE", "layer": "0", "x1": 0.0, "y1": 0.0, "z1": 0.0, "x2": 0.0, "y2": 0.0, "z2": 0.0}
    if et == "SPLINE":
        return {"type": "SPLINE", "layer": "0", "flag": 0, "degree": 3, "knots": [], "weights": [],
                "ctrl_pts": [], "fit_pts": [], "_c_cur": [None, None, None], "_f_cur": [None, None, None]}
    if et == "INSERT":
        return {"type": "INSERT", "layer": "0", "name": "", "x": 0.0, "y": 0.0, "z": 0.0,
                "sx": 1.0, "sy": 1.0, "sz": 1.0, "rot": 0.0, "cols": 1, "rows": 1, "col_sp": 0.0, "row_sp": 0.0,
                "nx": 0.0, "ny": 0.0, "nz": 1.0}
    return None


def _flush_spline_cur(ent: dict[str, Any], cur_key: str, dst_key: str) -> None:
    cur = ent[cur_key]
    if cur[0] is not None and cur[1] is not None:
        ent[dst_key].append((cur[0], cur[1], 0.0 if cur[2] is None else cur[2]))
    ent[cur_key] = [None, None, None]


def _push_spline_coord(ent: dict[str, Any], cur_key: str, dst_key: str, slot: int, val: float) -> None:
    cur = ent[cur_key]
    if cur[slot] is not None or (slot == 0 and cur[0] is not None and cur[1] is not None):
        _flush_spline_cur(ent, cur_key, dst_key)
        cur = ent[cur_key]
    cur[slot] = val
    if cur[0] is not None and cur[1] is not None and cur[2] is not None:
        _flush_spline_cur(ent, cur_key, dst_key)


def _apply_entity_code(entity: dict[str, Any], active_poly: dict[str, Any] | None, code: int, val: str) -> None:
    t = entity["type"]
    if t == "VERTEX" and (active_poly is None or entity is not active_poly["vertices"][-1]):
        return
    if code == 8 and t != "VERTEX":
        entity["layer"] = val or "0"
        return
    if (fmap := _FLOAT_FIELDS.get(t)) is not None and code in fmap:
        entity[fmap[code]] = float(val)
        return
    if t == "VERTEX":
        if code == 70:
            entity["flag"] = int(val)
        elif code in (71, 72, 73, 74):
            entity["idx"].append(int(val))
    elif t == "LWPOLYLINE":
        if code == 70:
            entity["closed"] = bool(int(val) & 1)
        elif code == 38:
            entity["z0"] = float(val)
        elif code == 39:
            entity["thick"] = float(val)
        elif code in (10, 20):
            cur, slot = entity["_cur"], 0 if code == 10 else 1
            if cur[slot] is not None and cur[0] is not None and cur[1] is not None:
                entity["pts"].append([cur[0], cur[1], cur[2]])
                cur[:] = [None, None, 0.0]
            cur[slot] = float(val)
            if cur[0] is not None and cur[1] is not None:
                entity["pts"].append([cur[0], cur[1], cur[2]])
                cur[:] = [None, None, 0.0]
        elif code == 42:
            if entity["_cur"][0] is not None or entity["_cur"][1] is not None:
                entity["_cur"][2] = float(val)
            elif entity["pts"]:
                entity["pts"][-1][2] = float(val)
    elif t == "POLYLINE" and code == 70:
        entity["flag"] = int(val)
    elif t == "3DFACE" and code in (10, 11, 12, 13, 20, 21, 22, 23, 30, 31, 32, 33):
        entity["coords"][code] = float(val)
    elif t == "SPLINE":
        if code in (70, 71):
            entity["flag" if code == 70 else "degree"] = int(val)
        elif code in (40, 41):
            entity["knots" if code == 40 else "weights"].append(float(val))
        elif code in (10, 20, 30):
            _push_spline_coord(entity, "_c_cur", "ctrl_pts", {10: 0, 20: 1, 30: 2}[code], float(val))
        elif code in (11, 21, 31):
            _flush_spline_cur(entity, "_c_cur", "ctrl_pts")
            _push_spline_coord(entity, "_f_cur", "fit_pts", {11: 0, 21: 1, 31: 2}[code], float(val))
    elif t == "INSERT":
        if code == 2:
            entity["name"] = val
        elif code in (70, 71):
            cnt = int(val)
            if cnt < 0:
                raise ValueError("DXF INSERT rows/cols cannot be negative")
            entity["cols" if code == 70 else "rows"] = max(1, cnt)


def _read_dxf_document(pairs: list[tuple[int, str]]) -> tuple[int, dict[str, dict[str, Any]], list[dict[str, Any]]]:
    insunits_code, section, in_insunits = 6, None, False
    entity: dict[str, Any] | None = None
    active_poly: dict[str, Any] | None = None
    current_block: dict[str, Any] | None = None
    in_block_hdr = False
    blocks: dict[str, dict[str, Any]] = {}
    entities: list[dict[str, Any]] = []

    def _dest() -> list[dict[str, Any]]:
        return current_block["entities"] if (section == "BLOCKS" and current_block is not None) else entities

    def _flush() -> None:
        nonlocal entity
        if entity is not None and active_poly is None:
            if entity["type"] == "SPLINE":
                _flush_spline_cur(entity, "_c_cur", "ctrl_pts")
                _flush_spline_cur(entity, "_f_cur", "fit_pts")
            _dest().append(entity)
        entity = None

    idx = 0
    while idx < len(pairs):
        code, val = pairs[idx]
        idx += 1
        if code == 0 and val.upper() == "SECTION":
            if idx < len(pairs) and pairs[idx][0] == 2:
                section = pairs[idx][1].upper()
                idx += 1
            continue
        if code == 0 and val.upper() == "ENDSEC":
            _flush()
            if active_poly is not None:
                _dest().append(active_poly)
                active_poly = None
            section, in_insunits, current_block, in_block_hdr = None, False, None, False
            continue
        if section == "HEADER":
            if code == 9:
                in_insunits = val.upper() == "$INSUNITS"
            elif in_insunits and code == 70:
                insunits_code, in_insunits = int(val), False
            continue
        if section not in ("BLOCKS", "ENTITIES"):
            continue
        if code == 0:
            _flush()
            et = val.upper()
            if section == "BLOCKS":
                if et == "BLOCK":
                    active_poly, current_block, in_block_hdr = None, {"name": "", "base": [0.0, 0.0, 0.0], "entities": []}, True
                    continue
                if et == "ENDBLK":
                    if active_poly is not None and current_block is not None:
                        current_block["entities"].append(active_poly)
                    active_poly = None
                    if current_block is not None and current_block["name"]:
                        blocks[current_block["name"].casefold()] = current_block
                    current_block, in_block_hdr = None, False
                    continue
                if current_block is None:
                    continue
                in_block_hdr = False
            if et == "VERTEX" and active_poly is not None:
                entity = {"type": "VERTEX", "x": 0.0, "y": 0.0, "z": active_poly.get("z0", 0.0), "bulge": 0.0, "flag": 0, "idx": []}
                active_poly["vertices"].append(entity)
            elif et == "SEQEND" and active_poly is not None:
                _dest().append(active_poly)
                active_poly = None
            elif et == "POLYLINE":
                active_poly = {"type": "POLYLINE", "layer": "0", "flag": 0, "z0": 0.0, "thick": 0.0, "vertices": []}
                entity = active_poly
            else:
                entity = _new_entity(et)
            continue
        if section == "BLOCKS" and in_block_hdr and current_block is not None:
            if code == 2 or (code == 3 and not current_block["name"]):
                current_block["name"] = val
            elif code in (10, 20, 30):
                current_block["base"][{10: 0, 20: 1, 30: 2}[code]] = float(val)
            continue
        if entity is not None:
            _apply_entity_code(entity, active_poly, code, val)
    _flush()
    return insunits_code, blocks, entities


def _expand_dxf_entities(raw_entities: list[dict[str, Any]], blocks: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Expand top-level entities and nested BLOCK/INSERT references into concrete entities."""
    expanded: list[dict[str, Any]] = []
    entity_counts: dict[str, int] = {}
    work_steps, vertex_steps = [0], [0]

    def _charge_verts(n: int) -> None:
        vertex_steps[0] += n
        if vertex_steps[0] > MAX_DXF_VERTICES:
            raise ValueError(f"DXF expanded vertex count exceeded ceiling ({MAX_DXF_VERTICES})")

    def _walk(items: list[dict[str, Any]], mat: tuple[tuple[float, float, float, float], ...], parent_layer: str, depth: int, active: set[str]) -> None:
        for ent in items:
            work_steps[0] += 1
            if work_steps[0] > MAX_DXF_ENTITIES:
                raise ValueError(f"DXF entity/INSERT expansion exceeded maximum budget ({MAX_DXF_ENTITIES})")
            t, raw_layer = ent["type"], ent.get("layer", "0")
            layer = parent_layer if (raw_layer == "0" and parent_layer != "0") else raw_layer
            if t == "INSERT":
                entity_counts["INSERT"] = entity_counts.get("INSERT", 0) + 1
                rows, cols = int(ent.get("rows", 1)), int(ent.get("cols", 1))
                if rows < 1 or cols < 1:
                    raise ValueError("DXF INSERT rows and cols must be >= 1")
                if rows * cols > MAX_DXF_ENTITIES or work_steps[0] + rows * cols > MAX_DXF_ENTITIES:
                    raise ValueError(f"DXF INSERT array ({rows}x{cols}) exceeds maximum expansion budget ({MAX_DXF_ENTITIES})")
                bkey = ent["name"].casefold()
                blk = blocks.get(bkey)
                if blk is None or bkey in active:
                    continue
                if depth >= MAX_DXF_INSERT_DEPTH:
                    raise ValueError(f"DXF INSERT nesting depth exceeded maximum ({MAX_DXF_INSERT_DEPTH})")
                rot = ent["rot"]
                c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
                m_ocs = _ocs_matrix(ent.get("nx", 0.0), ent.get("ny", 0.0), ent.get("nz", 1.0))
                active.add(bkey)
                try:
                    for ir in range(rows):
                        for ic in range(cols):
                            work_steps[0] += 1
                            if work_steps[0] > MAX_DXF_ENTITIES:
                                raise ValueError(f"DXF entity/INSERT expansion exceeded maximum budget ({MAX_DXF_ENTITIES})")
                            dx, dy = ic * ent["col_sp"] * c - ir * ent["row_sp"] * s, ic * ent["col_sp"] * s + ir * ent["row_sp"] * c
                            m_ins = _insert_matrix(tuple(blk["base"]), (ent["x"] + dx, ent["y"] + dy, ent["z"]), (ent["sx"], ent["sy"], ent["sz"]), rot)
                            _walk(blk["entities"], _mat_mul(mat, _mat_mul(m_ocs, m_ins)), layer, depth + 1, active)
                finally:
                    active.remove(bkey)
                continue

            entity_counts[t] = entity_counts.get(t, 0) + 1
            m_ent = _mat_mul(mat, _ocs_matrix(ent.get("nx", 0.0), ent.get("ny", 0.0), ent.get("nz", 1.0))) if ("nx" in ent or "ny" in ent or "nz" in ent) else mat
            z_scale = abs(m_ent[2][2])
            if t == "LWPOLYLINE":
                _charge_verts(len(ent["pts"]))
                local = _discretize_bulge_chain([(p[0], p[1], ent["z0"], p[2]) for p in ent["pts"]], ent["closed"])
                _charge_verts(max(0, len(local) - len(ent["pts"])))
                expanded.append({"type": "CURVE", "layer": layer, "closed": ent["closed"], "thick": ent["thick"] * z_scale, "pts": [_apply_mat(m_ent, p) for p in local]})
            elif t == "POLYLINE":
                vlist = ent["vertices"]
                _charge_verts(len(vlist))
                if ent["flag"] & 64:
                    expanded.append({"type": "POLYFACE", "layer": layer, "vertices": vlist, "pts3": [_apply_mat(mat, (v["x"], v["y"], v["z"])) for v in vlist if (v["flag"] & 128) and (v["flag"] & 64)]})
                else:
                    closed, is_3d_poly = bool(ent["flag"] & 1), bool(ent["flag"] & 8)
                    m_poly = mat if is_3d_poly else m_ent
                    local = _discretize_bulge_chain([(v["x"], v["y"], v["z"] if is_3d_poly else ent["z0"], v.get("bulge", 0.0)) for v in vlist], closed)
                    _charge_verts(max(0, len(local) - len(vlist)))
                    expanded.append({"type": "CURVE", "layer": layer, "closed": closed, "thick": ent["thick"] * abs(m_poly[2][2]), "pts": [_apply_mat(m_poly, p) for p in local]})
            elif t in ("CIRCLE", "ARC", "ELLIPSE", "SPLINE"):
                if t == "CIRCLE": local, full, tk, mt = _discretize_circle(ent["cx"], ent["cy"], ent["cz"], ent["r"]), True, ent["thick"] * z_scale, m_ent
                elif t == "ARC": local, full = _discretize_arc(ent["cx"], ent["cy"], ent["cz"], ent["r"], ent["start"], ent["end"]); tk, mt = ent["thick"] * z_scale, m_ent
                elif t == "ELLIPSE": local, full = _discretize_ellipse(ent); tk, mt = 0.0, mat
                else: local, full = _discretize_spline(ent); tk, mt = 0.0, mat
                _charge_verts(len(local))
                expanded.append({"type": "CURVE", "layer": layer, "closed": full, "thick": tk, "pts": [_apply_mat(mt, p) for p in local]})
            elif t == "LINE":
                _charge_verts(2)
                expanded.append({"type": "CURVE", "layer": layer, "closed": False, "thick": 0.0, "pts": [_apply_mat(mat, (ent["x1"], ent["y1"], ent["z1"])), _apply_mat(mat, (ent["x2"], ent["y2"], ent["z2"]))]})
            elif t == "3DFACE":
                _charge_verts(4)
                c = ent["coords"]
                expanded.append({"type": "3DFACE", "layer": layer, "pts": [_apply_mat(mat, (c.get(10 + k, 0.0), c.get(20 + k, 0.0), c.get(30 + k, 0.0))) for k in range(4)]})
            elif t == "POINT":
                _charge_verts(1)
                wx, wy, wz = _apply_mat(mat, (ent["x"], ent["y"], ent["z"]))
                expanded.append({"type": "POINT", "layer": layer, "x": wx, "y": wy, "z": wz})

    _walk(raw_entities, _mat_identity(), "0", 0, set())
    return expanded, entity_counts


def parse_ascii_dxf(workspace: Path, dxf_name: str, *, dxf_layers: dict[str, Any] | None = None, unit_scale: float | None = None) -> dict[str, Any]:
    """Parse a UserData/*.dxf ASCII file into site_polygon, exclusion_zones, obstacles, and terrain."""
    _, raw = read_bounded_cad_bytes(workspace, dxf_name, ".dxf")
    if raw.lstrip().startswith(b"AutoCAD Binary DXF"):
        raise ValueError(f"Binary DXF is not supported ({dxf_name}); save or export as ASCII DXF")
    role_map = normalize_dxf_layers(dxf_layers)
    scale_override = _finite(unit_scale, "dxf_unit_scale", positive=True) if unit_scale is not None else None
    insunits_code, blocks, raw_entities = _read_dxf_document(_dxf_pairs(raw.decode("utf-8-sig", errors="replace")))
    entities, entity_counts = _expand_dxf_entities(raw_entities, blocks)
    scale = scale_override if scale_override is not None else _INSUNITS_TO_M.get(insunits_code, 1.0)

    boundary_polys: list[list[tuple[float, float]]] = []
    fallback_polys: list[list[tuple[float, float]]] = []
    exclusion_polys: list[list[tuple[float, float]]] = []
    open_chains: dict[tuple[str, str | None], list[list[tuple[float, float]]]] = {}
    obstacles: list[dict[str, Any]] = []
    obs_faces_by_layer: dict[str, tuple[list[list[float]], list[list[int]]]] = {}
    seen_face_keys: dict[str, set[tuple[tuple[float, float, float], ...]]] = {}
    terrain_pts: list[tuple[float, float, float]] = []
    layers_seen: set[str] = set()
    used_obs: set[str] = set()

    def _add_face_obs(layer: str, corners: list[tuple[float, float, float]], indices: list[int]) -> None:
        verts, faces = obs_faces_by_layer.setdefault(layer, ([], []))
        seen, base = seen_face_keys.setdefault(layer, set()), len(verts)
        verts.extend([[round(p[0], 5), round(p[1], 5), round(p[2], 5)] for p in corners])
        for k in range(1, len(indices) - 1):
            tri = [base + indices[0], base + indices[k], base + indices[k + 1]]
            pa, pb, pc = (verts[vi] for vi in tri)
            u, v = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]), (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2])
            cx, cy, cz = u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]
            if cx * cx + cy * cy + cz * cz > 1e-12:
                key = tuple(sorted((tuple(pa), tuple(pb), tuple(pc))))
                if key not in seen:
                    seen.add(key)
                    faces.append(tri)

    def _route_closed(pts2: list[tuple[float, float]], role: str | None, z_base: float, h: float, idx_tag: int) -> None:
        if role == "obstacles":
            if len(obstacles) < MAX_OBSTACLES:
                simp, _ = _simplify_closed_polygon(pts2, 48)
                if len(simp) >= 3 and (prism := _extrude_polygon_prism(simp, z_base, z_base + h)) is not None:
                    verts, faces = prism
                    oname = f"DXF_Obs_{idx_tag}"
                    while oname.casefold() in used_obs:
                        oname += "_1"
                    used_obs.add(oname.casefold())
                    obstacles.append({"kind": "mesh", "name": oname, "vertices": verts, "triangles": faces})
        elif role == "exclusions":
            exclusion_polys.append(pts2)
        elif role == "boundary":
            boundary_polys.append(pts2)
        else:
            fallback_polys.append(pts2)

    for idx_e, ent in enumerate(entities):
        layer = ent.get("layer", "0")
        layers_seen.add(layer)
        role, t = _classify_layer(layer, role_map), ent["type"]
        if t == "CURVE":
            pts3 = [(x * scale, y * scale, z * scale) for x, y, z in ent["pts"]]
            if not pts3:
                continue
            if role == "terrain":
                terrain_pts.extend(pts3)
                continue
            pts2 = [(p[0], p[1]) for p in pts3]
            auto_closed = not ent["closed"] and len(pts2) >= 4 and math.hypot(pts2[0][0] - pts2[-1][0], pts2[0][1] - pts2[-1][1]) <= 1e-4
            if auto_closed:
                pts2 = pts2[:-1]
            closed = ent["closed"] or auto_closed
            if closed and len(pts2) >= 3:
                zs_p = [p[2] for p in pts3]
                zmin, zmax, thick = min(zs_p), max(zs_p), ent["thick"] * scale
                dz = zmax - zmin
                h = thick if thick > 1e-3 else (dz if dz > 0.05 else (zmin if zmin > 1e-3 else 4.0))
                z_base = zmin if (dz > 0.05 or thick > 1e-3) else 0.0
                _route_closed(pts2, role, z_base, h, idx_e + 1)
            elif len(pts2) >= 2:
                open_chains.setdefault((layer.casefold(), role), []).append(pts2)
        elif t == "POLYFACE":
            v_pts = [(x * scale, y * scale, z * scale) for x, y, z in ent["pts3"]]
            if role == "terrain":
                terrain_pts.extend(v_pts)
            elif role == "obstacles" and v_pts:
                for v in ent["vertices"]:
                    if (v["flag"] & 128) and not (v["flag"] & 64) and len(v["idx"]) >= 3:
                        idxs = [abs(i) - 1 for i in v["idx"] if 1 <= abs(i) <= len(v_pts)]
                        if len(idxs) >= 3:
                            _add_face_obs(layer, [v_pts[i] for i in idxs], list(range(len(idxs))))
        elif t == "3DFACE":
            pts_f = [(x * scale, y * scale, z * scale) for x, y, z in ent["pts"]]
            if math.hypot(pts_f[3][0] - pts_f[2][0], pts_f[3][1] - pts_f[2][1]) <= 1e-7 and abs(pts_f[3][2] - pts_f[2][2]) <= 1e-7:
                pts_f = pts_f[:3]
            if role == "terrain":
                terrain_pts.extend(pts_f)
            elif role == "obstacles":
                _add_face_obs(layer, pts_f, list(range(len(pts_f))))
        elif t == "POINT" and role in ("terrain", None):
            terrain_pts.append((ent["x"] * scale, ent["y"] * scale, ent["z"] * scale))

    stitch_tag = len(entities)
    for (_, role), chains in open_chains.items():
        for stitched in _stitch_open_loops(chains):
            stitch_tag += 1
            _route_closed(stitched, role, 0.0, 4.0, stitch_tag)

    for lyr, (verts, faces) in obs_faces_by_layer.items():
        if faces and len(obstacles) < MAX_OBSTACLES:
            oname = re.sub(r"\s+", "_", lyr)[:40] or "DXF_Mesh"
            while oname.casefold() in used_obs:
                oname += "_1"
            used_obs.add(oname.casefold())
            obstacles.append({"kind": "mesh", "name": oname, "vertices": verts, "triangles": faces})
    if obstacles:
        obstacle_profiles(obstacles)

    excl_candidates: list[tuple[float, list[list[float]]]] = []
    for raw_ex in exclusion_polys:
        simp_ex, _ = _simplify_closed_polygon(raw_ex, 64)
        rounded_ex = round_closed_polygon(simp_ex, 4)
        if len(rounded_ex) < 3:
            continue
        pts_ex = [(p[0], p[1]) for p in rounded_ex]
        if is_self_intersecting_polygon(pts_ex):
            raise ValueError(f"DXF {dxf_name!r} contains a self-intersecting exclusion polygon")
        if (area := polygon_area(pts_ex)) > 1e-3:
            excl_candidates.append((area, rounded_ex))
    if len(excl_candidates) > MAX_EXCLUSION_ZONES:
        raise ValueError(
            f"DXF {dxf_name!r} defines {len(excl_candidates)} exclusion zones, exceeding the maximum of {MAX_EXCLUSION_ZONES}; "
            "merge/simplify keepouts or narrow dxf_layers.exclusions"
        )
    excl_candidates.sort(key=lambda item: item[0], reverse=True)
    exclusion_zones = [poly for _, poly in excl_candidates]
    if exclusion_zones:
        validate_exclusion_zones(exclusion_zones)

    candidates = boundary_polys or fallback_polys
    if not candidates:
        raise ValueError(f"No closed boundary polygon found in DXF {dxf_name!r}; verify layer mapping via dxf_layers.boundary")
    simp_poly, simplified = _simplify_closed_polygon(max(candidates, key=polygon_area), 64)
    validated = validate_site_polygon(round_closed_polygon(simp_poly, 4))
    site_polygon = [[x, y] for x, y in validated]
    return {
        "format": "ascii_dxf", "file_name": dxf_name, "unit_scale": scale, "insunits_code": insunits_code,
        "layers": sorted(layers_seen), "entity_counts": entity_counts, "site_polygon": site_polygon,
        "site_area_m2": round(polygon_area(validated), 2), "boundary_simplified": simplified,
        "exclusion_zones": exclusion_zones, "obstacles": obstacles,
        "terrain_points": downsample_xyz_points(terrain_pts) if len(terrain_pts) >= 3 else [],
        "bounds_m": {"min_x": round(min(p[0] for p in site_polygon), 3), "max_x": round(max(p[0] for p in site_polygon), 3),
                     "min_y": round(min(p[1] for p in site_polygon), 3), "max_y": round(max(p[1] for p in site_polygon), 3)},
    }

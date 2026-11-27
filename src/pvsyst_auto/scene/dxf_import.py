"""Pure-stdlib ASCII .dxf parser extracting site polygon, 3D obstacles, and terrain points."""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from pvsyst_auto.project.paths import workspace_file
from pvsyst_auto.scene.author import MAX_OBSTACLES, obstacle_profiles
from pvsyst_auto.scene.reader import validate_site_polygon
from pvsyst_auto.validation import finite_number as _finite

MAX_DXF_BYTES = 64 * 1024 * 1024
MAX_DXF_TERRAIN_POINTS = 2500

_INSUNITS_TO_M: dict[int, float] = {
    0: 1.0,
    1: 0.0254,
    2: 0.3048,
    3: 1609.344,
    4: 0.001,
    5: 0.01,
    6: 1.0,
    7: 1000.0,
    8: 1e-6,
    9: 0.0254 / 1000.0,
    10: 0.9144,
    14: 0.1,
}

_BOUNDARY_RE = re.compile(r"(?:bound|site|parcel|perimet|fence|property|pv_?area|limit)", re.I)
_OBSTACLE_RE = re.compile(r"(?:obst|bldg|build|shade|shadow|tree|struct|wall|keepout|exclus)", re.I)
_TERRAIN_RE = re.compile(r"(?:topo|terrain|dtm|dem|contour|elev|ground|grade|surf)", re.I)
_DXF_LAYER_ROLES = frozenset(("boundary", "obstacles", "terrain"))


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
    lo, hi = 0.0, span * 0.2
    closed = clean + [clean[0]]
    best = clean
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


def _extrude_polygon_prism(poly: list[tuple[float, float]], z0: float, z1: float) -> tuple[list[list[float]], list[list[int]]]:
    n = len(poly)
    area2 = sum(poly[i][0] * poly[(i + 1) % n][1] - poly[(i + 1) % n][0] * poly[i][1] for i in range(n))
    pts = list(reversed(poly)) if area2 < 0.0 else list(poly)
    verts = [[round(x, 5), round(y, 5), round(z0, 5)] for x, y in pts] + [[round(x, 5), round(y, 5), round(z1, 5)] for x, y in pts]
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
    return verts, faces


def normalize_dxf_layers(dxf_layers: dict[str, Any] | None) -> dict[str, set[str]]:
    """Validate and case-fold optional DXF layer role mapping."""
    if dxf_layers is None:
        return {}
    if not isinstance(dxf_layers, dict):
        raise ValueError("dxf_layers must be an object mapping boundary/obstacles/terrain to layer names")
    out: dict[str, set[str]] = {}
    for role, val in dxf_layers.items():
        if role not in _DXF_LAYER_ROLES:
            raise ValueError(f"Unsupported dxf_layers role {role!r}; choose 'boundary', 'obstacles', or 'terrain'")
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
    for role in ("boundary", "obstacles", "terrain"):
        if lf in role_map.get(role, ()):
            return role
    if role_map:
        return None
    if _BOUNDARY_RE.search(layer):
        return "boundary"
    if _TERRAIN_RE.search(layer):
        return "terrain"
    if _OBSTACLE_RE.search(layer):
        return "obstacles"
    return None


def _read_dxf_entities(pairs: list[tuple[int, str]]) -> tuple[int, list[dict[str, Any]], dict[str, int]]:
    insunits_code = 6
    section: str | None = None
    in_insunits = False
    entity: dict[str, Any] | None = None
    active_poly: dict[str, Any] | None = None
    entities: list[dict[str, Any]] = []
    entity_counts: dict[str, int] = {}

    def _flush() -> None:
        nonlocal entity
        if entity is not None and active_poly is None:
            etype = entity["type"]
            entity_counts[etype] = entity_counts.get(etype, 0) + 1
            entities.append(entity)
        entity = None

    idx = 0
    while idx < len(pairs):
        code, val = pairs[idx]
        idx += 1
        if code == 0 and val == "SECTION":
            if idx < len(pairs) and pairs[idx][0] == 2:
                section = pairs[idx][1].upper()
                idx += 1
            continue
        if code == 0 and val == "ENDSEC":
            _flush()
            section, in_insunits = None, False
            continue
        if section == "HEADER":
            if code == 9:
                in_insunits = val.upper() == "$INSUNITS"
            elif in_insunits and code == 70:
                insunits_code, in_insunits = int(val), False
            continue
        if section != "ENTITIES":
            continue
        if code == 0:
            _flush()
            et = val.upper()
            if et == "VERTEX" and active_poly is not None:
                vrec: dict[str, Any] = {"type": "VERTEX", "x": 0.0, "y": 0.0, "z": active_poly.get("z0", 0.0), "flag": 0, "idx": []}
                active_poly["vertices"].append(vrec)
                entity = vrec
            elif et == "SEQEND" and active_poly is not None:
                entity_counts["POLYLINE"] = entity_counts.get("POLYLINE", 0) + 1
                entities.append(active_poly)
                active_poly = None
            elif et == "LWPOLYLINE":
                entity = {"type": "LWPOLYLINE", "layer": "0", "closed": False, "z0": 0.0, "thick": 0.0, "pts": [], "_cur": [None, None]}
            elif et == "POLYLINE":
                active_poly = {"type": "POLYLINE", "layer": "0", "flag": 0, "z0": 0.0, "thick": 0.0, "vertices": []}
                entity = active_poly
            elif et == "3DFACE":
                entity = {"type": "3DFACE", "layer": "0", "coords": {}}
            elif et == "POINT":
                entity = {"type": "POINT", "layer": "0", "x": 0.0, "y": 0.0, "z": 0.0}
            else:
                entity = None
            continue
        if entity is None:
            continue
        t = entity["type"]
        if t == "VERTEX" and active_poly is not None and entity is active_poly["vertices"][-1]:
            if code in (10, 20, 30):
                entity[{10: "x", 20: "y", 30: "z"}[code]] = float(val)
            elif code == 70:
                entity["flag"] = int(val)
            elif code in (71, 72, 73, 74):
                entity["idx"].append(int(val))
        elif t == "LWPOLYLINE":
            if code == 8:
                entity["layer"] = val or "0"
            elif code == 70:
                entity["closed"] = bool(int(val) & 1)
            elif code == 38:
                entity["z0"] = float(val)
            elif code == 39:
                entity["thick"] = float(val)
            elif code in (10, 20):
                entity["_cur"][0 if code == 10 else 1] = float(val)
                if entity["_cur"][0] is not None and entity["_cur"][1] is not None:
                    entity["pts"].append((entity["_cur"][0], entity["_cur"][1]))
                    entity["_cur"] = [None, None]
        elif t == "POLYLINE":
            if code == 8:
                entity["layer"] = val or "0"
            elif code == 70:
                entity["flag"] = int(val)
            elif code == 30:
                entity["z0"] = float(val)
            elif code == 39:
                entity["thick"] = float(val)
        elif t == "3DFACE":
            if code == 8:
                entity["layer"] = val or "0"
            elif code in (10, 11, 12, 13, 20, 21, 22, 23, 30, 31, 32, 33):
                entity["coords"][code] = float(val)
        elif t == "POINT":
            if code == 8:
                entity["layer"] = val or "0"
            elif code in (10, 20, 30):
                entity[{10: "x", 20: "y", 30: "z"}[code]] = float(val)
    _flush()
    return insunits_code, entities, entity_counts


def parse_ascii_dxf(
    workspace: Path,
    dxf_name: str,
    *,
    dxf_layers: dict[str, Any] | None = None,
    unit_scale: float | None = None,
) -> dict[str, Any]:
    """Parse a UserData/*.dxf ASCII file into site_polygon, obstacles, and terrain points."""
    _, raw = read_bounded_cad_bytes(workspace, dxf_name, ".dxf")
    if raw.lstrip().startswith(b"AutoCAD Binary DXF"):
        raise ValueError(f"Binary DXF is not supported ({dxf_name}); save or export as ASCII DXF")
    role_map = normalize_dxf_layers(dxf_layers)
    scale_override = _finite(unit_scale, "dxf_unit_scale", positive=True) if unit_scale is not None else None
    insunits_code, entities, entity_counts = _read_dxf_entities(_dxf_pairs(raw.decode("utf-8-sig", errors="replace")))
    scale = scale_override if scale_override is not None else _INSUNITS_TO_M.get(insunits_code, 1.0)

    boundary_polys: list[list[tuple[float, float]]] = []
    fallback_polys: list[list[tuple[float, float]]] = []
    obstacles: list[dict[str, Any]] = []
    obs_faces_by_layer: dict[str, tuple[list[list[float]], list[list[int]]]] = {}
    terrain_pts: list[tuple[float, float, float]] = []
    layers_seen: set[str] = set()
    used_obs: set[str] = set()

    def _add_face_obs(layer: str, corners: list[tuple[float, float, float]], indices: list[int]) -> None:
        verts, faces = obs_faces_by_layer.setdefault(layer, ([], []))
        base = len(verts)
        verts.extend([[round(p[0], 5), round(p[1], 5), round(p[2], 5)] for p in corners])
        for k in range(1, len(indices) - 1):
            tri = [base + indices[0], base + indices[k], base + indices[k + 1]]
            pa, pb, pc = (verts[vi] for vi in tri)
            u, v = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]), (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2])
            cx, cy, cz = u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]
            if cx * cx + cy * cy + cz * cz > 1e-12:
                faces.append(tri)

    for idx_e, ent in enumerate(entities):
        layer = ent.get("layer", "0")
        layers_seen.add(layer)
        role = _classify_layer(layer, role_map)
        t = ent["type"]
        if t == "LWPOLYLINE":
            pts2 = [(x * scale, y * scale) for x, y in ent["pts"]]
            if not pts2:
                continue
            closed = ent["closed"] or (len(pts2) >= 4 and math.hypot(pts2[0][0] - pts2[-1][0], pts2[0][1] - pts2[-1][1]) <= 1e-4)
            z0 = ent["z0"] * scale
            if role == "terrain":
                terrain_pts.extend((x, y, z0) for x, y in pts2)
            elif role == "obstacles" and closed and len(pts2) >= 3 and len(obstacles) < MAX_OBSTACLES:
                simp, _ = _simplify_closed_polygon(pts2, 48)
                h = ent["thick"] * scale if ent["thick"] > 0 else (z0 if z0 > 0 else 4.0)
                z_base = z0 if ent["thick"] > 0 else 0.0
                if len(simp) >= 3:
                    verts, faces = _extrude_polygon_prism(simp, z_base, z_base + h)
                    oname = f"DXF_Obs_{idx_e + 1}"
                    used_obs.add(oname.casefold())
                    obstacles.append({"kind": "mesh", "name": oname, "vertices": verts, "triangles": faces})
            elif closed and len(pts2) >= 3:
                (boundary_polys if role == "boundary" else fallback_polys).append(pts2)
        elif t == "POLYLINE":
            vlist = ent["vertices"]
            if ent["flag"] & 64:
                v_pts = [(v["x"] * scale, v["y"] * scale, v["z"] * scale) for v in vlist if (v["flag"] & 128) and (v["flag"] & 64)]
                if role == "terrain":
                    terrain_pts.extend(v_pts)
                elif role == "obstacles" and v_pts:
                    for v in vlist:
                        if (v["flag"] & 128) and not (v["flag"] & 64) and len(v["idx"]) >= 3:
                            idxs = [abs(i) - 1 for i in v["idx"] if 1 <= abs(i) <= len(v_pts)]
                            if len(idxs) >= 3:
                                _add_face_obs(layer, [v_pts[i] for i in idxs], list(range(len(idxs))))
            elif (pts3 := [(v["x"] * scale, v["y"] * scale, v["z"] * scale) for v in vlist]):
                if role == "terrain":
                    terrain_pts.extend(pts3)
                else:
                    pts2 = [(p[0], p[1]) for p in pts3]
                    closed = bool(ent["flag"] & 1) or (len(pts2) >= 4 and math.hypot(pts2[0][0] - pts2[-1][0], pts2[0][1] - pts2[-1][1]) <= 1e-4)
                    if role == "obstacles" and closed and len(pts2) >= 3 and len(obstacles) < MAX_OBSTACLES:
                        simp, _ = _simplify_closed_polygon(pts2, 48)
                        zs_p = [p[2] for p in pts3]
                        zmin, zmax = min(zs_p), max(zs_p)
                        h = ent["thick"] * scale if ent["thick"] > 0 else ((zmax - zmin) if zmax - zmin > 0.05 else (zmin if zmin > 0 else 4.0))
                        z_base = zmin if zmax - zmin > 0.05 or ent["thick"] > 0 else 0.0
                        if len(simp) >= 3:
                            verts, faces = _extrude_polygon_prism(simp, z_base, z_base + h)
                            oname = f"DXF_Obs_{idx_e + 1}"
                            used_obs.add(oname.casefold())
                            obstacles.append({"kind": "mesh", "name": oname, "vertices": verts, "triangles": faces})
                    elif closed and len(pts2) >= 3:
                        (boundary_polys if role == "boundary" else fallback_polys).append(pts2)
        elif t == "3DFACE":
            c = ent["coords"]
            pts_f = [(c.get(10 + k, 0.0) * scale, c.get(20 + k, 0.0) * scale, c.get(30 + k, 0.0) * scale) for k in range(4)]
            if math.hypot(pts_f[3][0] - pts_f[2][0], pts_f[3][1] - pts_f[2][1]) <= 1e-7 and abs(pts_f[3][2] - pts_f[2][2]) <= 1e-7:
                pts_f = pts_f[:3]
            if role == "terrain":
                terrain_pts.extend(pts_f)
            elif role == "obstacles":
                _add_face_obs(layer, pts_f, list(range(len(pts_f))))
        elif t == "POINT" and role in ("terrain", None):
            terrain_pts.append((ent["x"] * scale, ent["y"] * scale, ent["z"] * scale))

    for lyr, (verts, faces) in obs_faces_by_layer.items():
        if faces and len(obstacles) < MAX_OBSTACLES:
            oname = re.sub(r"\s+", "_", lyr)[:40] or "DXF_Mesh"
            while oname.casefold() in used_obs:
                oname += "_1"
            used_obs.add(oname.casefold())
            obstacles.append({"kind": "mesh", "name": oname, "vertices": verts, "triangles": faces})
    if obstacles:
        obstacle_profiles(obstacles)

    def _poly_area2(pts: list[tuple[float, float]]) -> float:
        return abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts))))

    candidates = boundary_polys or fallback_polys
    if not candidates:
        raise ValueError(f"No closed boundary polygon found in DXF {dxf_name!r}; verify layer mapping via dxf_layers.boundary")
    simp_poly, simplified = _simplify_closed_polygon(max(candidates, key=_poly_area2), 64)
    validated = validate_site_polygon([[round(x, 4), round(y, 4)] for x, y in simp_poly])
    site_polygon = [[x, y] for x, y in validated]
    return {
        "format": "ascii_dxf",
        "file_name": dxf_name,
        "unit_scale": scale,
        "insunits_code": insunits_code,
        "layers": sorted(layers_seen),
        "entity_counts": entity_counts,
        "site_polygon": site_polygon,
        "site_area_m2": round(0.5 * _poly_area2(validated), 2),
        "boundary_simplified": simplified,
        "obstacles": obstacles,
        "terrain_points": downsample_xyz_points(terrain_pts) if len(terrain_pts) >= 3 else [],
        "bounds_m": {
            "min_x": round(min(p[0] for p in site_polygon), 3),
            "max_x": round(max(p[0] for p in site_polygon), 3),
            "min_y": round(min(p[1] for p in site_polygon), 3),
            "max_y": round(max(p[1] for p in site_polygon), 3),
        },
    }

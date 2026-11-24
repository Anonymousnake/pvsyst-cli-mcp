"""Pure-Python 3D terrain DEM triangulation, optional GeoTIFF reader, table draping, and pvShdGround emitter.

Zero heavy dependencies by default:
- Supports inline XYZ point clouds, regular elevation grids, or workspace CSV files (local metric XYZ or lon/lat).
- Optional GeoTIFF (.tif/.tiff) direct read when the `[geo]` extra (`rasterio`) is installed.
- Pure-Python Bowyer-Watson Delaunay triangulation + barycentric elevation/slope interpolation.
- Emits PVsyst 8.1.7 `pvShdGround` blocks and drapes both `pvShdArrayTable` (sheds) and `pvShdArrayTracking` (trackers).
"""
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

from pvsyst_auto.project.paths import workspace_file
from pvsyst_auto.validation import finite_number as _finite
from pvsyst_auto.versions import SEED_RECORD_VERSION, SYNTH_VERSION

_EPS = 1e-9
_EARTH_R_M = 6371000.0


def project_lonlat_to_enu(
    points_lonlatz: list[tuple[float, float, float]],
    *,
    origin_lon: float | None = None,
    origin_lat: float | None = None,
) -> list[tuple[float, float, float]]:
    """Project (lon_deg, lat_deg, z_m) into local East-North-Up meters."""
    if not points_lonlatz:
        return []
    lon0 = origin_lon if origin_lon is not None else sum(p[0] for p in points_lonlatz) / len(points_lonlatz)
    lat0 = origin_lat if origin_lat is not None else sum(p[1] for p in points_lonlatz) / len(points_lonlatz)
    cos_lat0 = math.cos(math.radians(lat0))
    out: list[tuple[float, float, float]] = []
    for lon, lat, z in points_lonlatz:
        x = math.radians(lon - lon0) * _EARTH_R_M * cos_lat0
        y = math.radians(lat - lat0) * _EARTH_R_M
        out.append((x, y, z))
    return out


def _dedup_points(points: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    seen: dict[tuple[int, int], tuple[float, float, float]] = {}
    for x, y, z in points:
        key = (round(x * 1000), round(y * 1000))
        if key not in seen:
            seen[key] = (x, y, z)
    return list(seen.values())


def _circumcircle_contains(ax: float, ay: float, bx: float, by: float, cx: float, cy: float, px: float, py: float) -> bool:
    """Return True if (px, py) lies strictly inside the circumcircle of CCW triangle (a, b, c)."""
    # Orient CCW first
    cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    if abs(cross) <= 1e-12:
        return False
    if cross < 0:
        bx, by, cx, cy = cx, cy, bx, by
    adx, ady = ax - px, ay - py
    bdx, bdy = bx - px, by - py
    cdx, cdy = cx - px, cy - py
    ad2 = adx * adx + ady * ady
    bd2 = bdx * bdx + bdy * bdy
    cd2 = cdx * cdx + cdy * cdy
    det = (
        adx * (bdy * cd2 - bd2 * cdy)
        - ady * (bdx * cd2 - bd2 * cdx)
        + ad2 * (bdx * cdy - bdy * cdx)
    )
    return det > 1e-10


def triangulate_2d(points: list[tuple[float, float, float]]) -> list[tuple[int, int, int]]:
    """Pure-Python Bowyer-Watson 2D Delaunay triangulation returning CCW triangle index tuples."""
    n = len(points)
    if n < 3:
        raise ValueError("Terrain triangulation requires at least 3 non-collinear points")
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    dx = max_x - min_x
    dy = max_y - min_y
    delta = max(dx, dy, 1.0)
    mid_x = 0.5 * (min_x + max_x)
    mid_y = 0.5 * (min_y + max_y)

    # Super-triangle enclosing all points comfortably
    st0 = (mid_x - 20.0 * delta, mid_y - 20.0 * delta)
    st1 = (mid_x + 20.0 * delta, mid_y - 20.0 * delta)
    st2 = (mid_x, mid_y + 20.0 * delta)
    coords = [(p[0], p[1]) for p in points] + [st0, st1, st2]
    i0, i1, i2 = n, n + 1, n + 2
    triangles: list[tuple[int, int, int]] = [(i0, i1, i2)]

    for p_idx in range(n):
        px, py = coords[p_idx]
        bad: list[tuple[int, int, int]] = []
        keep: list[tuple[int, int, int]] = []
        for tri in triangles:
            a, b, c = tri
            if _circumcircle_contains(
                coords[a][0], coords[a][1],
                coords[b][0], coords[b][1],
                coords[c][0], coords[c][1],
                px, py,
            ):
                bad.append(tri)
            else:
                keep.append(tri)
        edge_count: dict[tuple[int, int], int] = {}
        directed_edges: list[tuple[int, int]] = []
        for a, b, c in bad:
            for u, v in ((a, b), (b, c), (c, a)):
                key = (u, v) if u < v else (v, u)
                edge_count[key] = edge_count.get(key, 0) + 1
                directed_edges.append((u, v))
        triangles = keep
        for u, v in directed_edges:
            key = (u, v) if u < v else (v, u)
            if edge_count[key] == 1:
                # Ensure CCW orientation
                ux, uy = coords[u]
                vx, vy = coords[v]
                if (vx - ux) * (py - uy) - (vy - uy) * (px - ux) >= 0:
                    triangles.append((u, v, p_idx))
                else:
                    triangles.append((v, u, p_idx))

    final: list[tuple[int, int, int]] = []
    for a, b, c in triangles:
        if a >= n or b >= n or c >= n:
            continue
        ax, ay = coords[a]
        bx, by = coords[b]
        cx, cy = coords[c]
        area2 = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        if abs(area2) <= 1e-8:
            continue
        final.append((a, b, c) if area2 > 0 else (a, c, b))
    if not final:
        raise ValueError("Terrain points are collinear and cannot form a 3D surface")
    return final


class TerrainMesh:
    """Triangulated 3D terrain surface supporting elevation/slope sampling and PVsyst pvShdGround export."""

    def __init__(self, vertices: list[tuple[float, float, float]], triangles: list[tuple[int, int, int]], *, name: str = "Terrain_DEM"):
        self.vertices = vertices
        self.triangles = triangles
        self.name = name
        xs = [v[0] for v in vertices]
        ys = [v[1] for v in vertices]
        zs = [v[2] for v in vertices]
        self.bounds = {
            "min_x": min(xs), "max_x": max(xs),
            "min_y": min(ys), "max_y": max(ys),
            "min_z": min(zs), "max_z": max(zs),
        }
        # Precompute per-triangle bounding boxes & normals for fast sampling
        self._tri_meta: list[tuple[float, float, float, float, tuple[float, float, float]]] = []
        for a, b, c in triangles:
            va, vb, vc = vertices[a], vertices[b], vertices[c]
            ux, uy, uz = vb[0] - va[0], vb[1] - va[1], vb[2] - va[2]
            vx, vy, vz = vc[0] - va[0], vc[1] - va[1], vc[2] - va[2]
            nx = uy * vz - uz * vy
            ny = uz * vx - ux * vz
            nz = ux * vy - uy * vx
            norm = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            nx, ny, nz = nx / norm, ny / norm, nz / norm
            if nz < 0:
                nx, ny, nz = -nx, -ny, -nz
            self._tri_meta.append((
                min(va[0], vb[0], vc[0]), max(va[0], vb[0], vc[0]),
                min(va[1], vb[1], vc[1]), max(va[1], vb[1], vc[1]),
                (nx, ny, nz),
            ))

    def sample(self, x: float, y: float) -> tuple[float, tuple[float, float, float]]:
        """Return (z_m, unit_normal=(nx, ny, nz)) at (x, y) via barycentric interpolation."""
        for (a, b, c), (minx, maxx, miny, maxy, normal) in zip(self.triangles, self._tri_meta):
            if x < minx - 1e-6 or x > maxx + 1e-6 or y < miny - 1e-6 or y > maxy + 1e-6:
                continue
            va, vb, vc = self.vertices[a], self.vertices[b], self.vertices[c]
            denom = (vb[1] - vc[1]) * (va[0] - vc[0]) + (vc[0] - vb[0]) * (va[1] - vc[1])
            if abs(denom) <= 1e-12:
                continue
            w1 = ((vb[1] - vc[1]) * (x - vc[0]) + (vc[0] - vb[0]) * (y - vc[1])) / denom
            w2 = ((vc[1] - va[1]) * (x - vc[0]) + (va[0] - vc[0]) * (y - vc[1])) / denom
            w3 = 1.0 - w1 - w2
            if w1 >= -1e-5 and w2 >= -1e-5 and w3 >= -1e-5:
                z = w1 * va[2] + w2 * vb[2] + w3 * vc[2]
                return z, normal

        # Fallback: nearest triangle centroid for points slightly outside convex hull
        best_dist2 = float("inf")
        best_z = 0.0
        best_normal = (0.0, 0.0, 1.0)
        for (a, b, c), (_, _, _, _, normal) in zip(self.triangles, self._tri_meta):
            va, vb, vc = self.vertices[a], self.vertices[b], self.vertices[c]
            cx = (va[0] + vb[0] + vc[0]) / 3.0
            cy = (va[1] + vb[1] + vc[1]) / 3.0
            d2 = (x - cx) ** 2 + (y - cy) ** 2
            if d2 < best_dist2:
                best_dist2 = d2
                best_z = (va[2] + vb[2] + vc[2]) / 3.0
                best_normal = normal
        return best_z, best_normal

    def slope_deg_along_direction(self, x: float, y: float, dx: float, dy: float, span_m: float = 2.0) -> float:
        """Compute terrain slope angle (deg) along unit horizontal vector (dx, dy) around (x, y)."""
        mag = math.hypot(dx, dy)
        if mag <= _EPS:
            return 0.0
        ux, uy = dx / mag, dy / mag
        half = max(0.5, 0.5 * span_m)
        z0, _ = self.sample(x - ux * half, y - uy * half)
        z1, _ = self.sample(x + ux * half, y + uy * half)
        return math.degrees(math.atan2(z1 - z0, 2.0 * half))

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "vertex_count": len(self.vertices),
            "triangle_count": len(self.triangles),
            "bounds_m": {k: round(v, 3) for k, v in self.bounds.items()},
            "elevation_span_m": round(self.bounds["max_z"] - self.bounds["min_z"], 3),
        }


def _load_csv_points(csv_path: Path, coordinate_system: str) -> list[tuple[float, float, float]]:
    text = csv_path.read_text(encoding="utf-8-sig")
    sample = "\n".join(text.splitlines()[:5])
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.reader(text.splitlines(), delimiter=delimiter)
    pts: list[tuple[float, float, float]] = []
    for row in reader:
        cleaned = [cell.strip() for cell in row if cell.strip()]
        if len(cleaned) < 3:
            continue
        try:
            x, y, z = float(cleaned[0]), float(cleaned[1]), float(cleaned[2])
        except ValueError:
            continue  # Skip header/comment lines
        if all(math.isfinite(v) for v in (x, y, z)):
            pts.append((x, y, z))
    if len(pts) < 3:
        raise ValueError(f"Terrain CSV {csv_path.name} yielded fewer than 3 valid (x, y, z) points")
    return project_lonlat_to_enu(pts) if coordinate_system == "lonlat" else pts


def _load_geotiff_points(tif_path: Path, *, max_grid_dim: int = 25) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    try:
        import rasterio  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ValueError(
            "GeoTIFF (.tif) terrain reading requires the optional [geo] extra: "
            "install with `pip install pvsyst-cli-mcp[geo]` (rasterio)."
        ) from exc

    with rasterio.open(tif_path) as src:
        band = src.read(1)
        rows, cols = band.shape
        if rows < 2 or cols < 2:
            raise ValueError(f"GeoTIFF {tif_path.name} must have at least 2x2 elevation pixels")
        step_r = max(1, math.ceil(rows / max_grid_dim))
        step_c = max(1, math.ceil(cols / max_grid_dim))
        nodata = src.nodata
        is_geo = bool(src.crs and src.crs.is_geographic)
        r_indices = list(range(0, rows, step_r))
        if r_indices[-1] != rows - 1:
            r_indices.append(rows - 1)
        c_indices = list(range(0, cols, step_c))
        if c_indices[-1] != cols - 1:
            c_indices.append(cols - 1)

        raw_pts: list[tuple[float, float, float]] = []
        for r in r_indices:
            for c in c_indices:
                z = float(band[r, c])
                if not math.isfinite(z) or (nodata is not None and abs(z - float(nodata)) <= 1e-6):
                    continue
                x, y = src.xy(r, c)
                raw_pts.append((float(x), float(y), z))

    if len(raw_pts) < 3:
        raise ValueError(f"GeoTIFF {tif_path.name} has fewer than 3 valid elevation pixels")
    if is_geo:
        pts = project_lonlat_to_enu(raw_pts)
    else:
        # Center projected metric coordinates around site centroid for clean PVsyst 3D coordinates
        mx = sum(p[0] for p in raw_pts) / len(raw_pts)
        my = sum(p[1] for p in raw_pts) / len(raw_pts)
        pts = [(p[0] - mx, p[1] - my, p[2]) for p in raw_pts]
    pts = _dedup_points(pts)
    return pts, triangulate_2d(pts)


def build_terrain_mesh(terrain_spec: dict[str, Any], *, workspace: Path | None = None) -> TerrainMesh:
    """Validate and construct a TerrainMesh from inline XYZ points, workspace CSV, or GeoTIFF."""
    if not isinstance(terrain_spec, dict):
        raise ValueError("terrain must be an object")
    name = str(terrain_spec.get("name") or "Terrain_DEM").strip() or "Terrain_DEM"
    coord_sys = str(terrain_spec.get("coordinate_system") or "local_m")
    if coord_sys not in ("local_m", "lonlat"):
        raise ValueError("terrain.coordinate_system must be 'local_m' or 'lonlat'")

    has_points = terrain_spec.get("points") is not None
    has_csv = terrain_spec.get("csv_name") is not None
    has_tif = terrain_spec.get("geotiff_name") is not None
    if sum(bool(x) for x in (has_points, has_csv, has_tif)) != 1:
        raise ValueError("Specify exactly one terrain source: points, csv_name, or geotiff_name")

    if has_tif:
        tif_name = str(terrain_spec["geotiff_name"])
        if workspace is None:
            raise ValueError("workspace is required to load terrain.geotiff_name")
        tif_path = workspace_file(workspace, "UserData", tif_name, Path(tif_name).suffix or ".tif")
        if not tif_path.is_file():
            raise FileNotFoundError(f"Terrain GeoTIFF not found in UserData: {tif_name}")
        max_dim = int(terrain_spec.get("max_grid_dim", 25))
        verts, tris = _load_geotiff_points(tif_path, max_grid_dim=max(4, min(60, max_dim)))
        return TerrainMesh(verts, tris, name=name)

    if has_csv:
        csv_name = str(terrain_spec["csv_name"])
        if workspace is None:
            raise ValueError("workspace is required to load terrain.csv_name")
        csv_path = workspace_file(workspace, "UserData", csv_name, ".csv")
        if not csv_path.is_file():
            raise FileNotFoundError(f"Terrain CSV not found in UserData: {csv_name}")
        raw_pts = _load_csv_points(csv_path, coord_sys)
    else:
        pts_in = terrain_spec["points"]
        if not isinstance(pts_in, list) or not 3 <= len(pts_in) <= 2500:
            raise ValueError("terrain.points must be a list of 3..2500 [x, y, z] coordinates")
        parsed: list[tuple[float, float, float]] = []
        for idx, item in enumerate(pts_in):
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                raise ValueError(f"terrain.points[{idx}] must be [x, y, z]")
            parsed.append((
                _finite(item[0], f"terrain.points[{idx}][0]"),
                _finite(item[1], f"terrain.points[{idx}][1]"),
                _finite(item[2], f"terrain.points[{idx}][2]"),
            ))
        raw_pts = project_lonlat_to_enu(parsed) if coord_sys == "lonlat" else parsed

    verts = _dedup_points(raw_pts)
    if len(verts) < 3:
        raise ValueError("Terrain requires at least 3 distinct (x, y) coordinates")
    tris = triangulate_2d(verts)
    return TerrainMesh(verts, tris, name=name)


def emit_pvshd_ground(
    mesh: TerrainMesh,
    *,
    object_index: int,
    no_obj_created: int | None = None,
    version: str = SEED_RECORD_VERSION,
) -> list[str]:
    """Emit a verified PVsyst 8.1.7 `PVObject_<idx>=pvShdGround` block."""
    oid = no_obj_created if no_obj_created is not None else object_index
    ox = sum(v[0] for v in mesh.vertices) / len(mesh.vertices)
    oy = sum(v[1] for v in mesh.vertices) / len(mesh.vertices)
    oz = sum(v[2] for v in mesh.vertices) / len(mesh.vertices)

    n_tris = len(mesh.triangles)
    lines = [
        f"      PVObject_{object_index}=pvShdGround",
        f"        Comment={mesh.name}",
        f"        Version={version}",
        "        Flags=$00200006",
        f"        NoObjCreated={oid}",
        f"        NPElements={4 * n_tris}",
        "        NPElemRes=400",
        f"        NElements={n_tris}",
        "        ColorTechn=$FF00",
        "        ColorSurf=$FF00",
        "        ColorBord=$FF00",
        f"        DOrigine={ox:.6f},{oy:.6f},{oz:.6f}",
        "        DInclin=0.000000",
        "        BaseSlope=0.000000",
        "        DAzimut=0.000000",
        "        Source=osGroundCSV",
        f"        Sommets, TPoly3D={len(mesh.vertices)} Point3D",
    ]
    for idx, (vx, vy, vz) in enumerate(mesh.vertices, start=1):
        lines.append(f"          Point_{idx}={vx - ox:.6f},{vy - oy:.6f},{vz - oz:.6f}")

    # Encode faces as -1,i0,i1,i2,... wrapped cleanly into Faces / Faces_contd lines
    tokens: list[str] = []
    for a, b, c in mesh.triangles:
        tokens.extend(("-1", str(a), str(b), str(c)))
    chunk_size = 48  # 12 triangles per line
    chunks = [tokens[i:i + chunk_size] for i in range(0, len(tokens), chunk_size)]
    for c_idx, chunk in enumerate(chunks):
        prefix = "        Faces=" if c_idx == 0 else "        Faces_contd="
        suffix = ",contd," if c_idx < len(chunks) - 1 else ","
        lines.append(prefix + ",".join(chunk) + suffix)

    lines.append("      End of PVObject pvShdGround")
    return lines


def drape_shed_tables_on_terrain(
    tables: list[dict[str, Any]],
    mesh: TerrainMesh,
    *,
    ground_clearance_m: float = 0.8,
    follow_slope: bool = True,
    max_base_slope_deg: float = 20.0,
) -> list[dict[str, Any]]:
    """Drape finite ground shed tables onto terrain Z elevation and optional local east-west BaseSlope."""
    draped: list[dict[str, Any]] = []
    for tbl in tables:
        t = dict(tbl)
        ox, oy, _ = t["origin_m"]
        z_ground, _ = mesh.sample(ox, oy)
        z_new = round(z_ground + ground_clearance_m, 6)
        t["origin_m"] = [ox, oy, z_new]
        if follow_slope:
            az_rad = math.radians(float(t.get("azimuth_deg", 0.0)))
            # Table row axis is east-west rotated by azimuth
            dx, dy = math.cos(az_rad), -math.sin(az_rad)
            span = float(t.get("long_shed_m", 4.0))
            slope = mesh.slope_deg_along_direction(ox, oy, dx, dy, span_m=span)
            slope = max(-max_base_slope_deg, min(max_base_slope_deg, slope))
            t["base_slope_deg"] = round(slope, 3)
        draped.append(t)
    return draped


def build_terrain_tracker_shading(
    mesh: TerrainMesh,
    *,
    tracker_count: int,
    pitch_ew_m: float,
    axis_azimuth_deg: float = 0.0,
    module_rows: int = 1,
    module_columns: int = 28,
    module_width_m: float = 1.134,
    module_length_m: float = 2.278,
    module_name: str = "PV Module",
    module_orient: str = "moPortrait",
    spacing_x_m: float = 0.02,
    spacing_y_m: float = 0.02,
    ground_clearance_m: float = 1.5,
    follow_slope: bool = True,
    max_axis_tilt_deg: float = 15.0,
    obstacles: list[dict[str, Any]] | None = None,
    version: str = SYNTH_VERSION,
) -> tuple[list[str], dict[str, Any]]:
    """Build a 3D pvShading scene with per-tracker draped pvShdArrayTracking + pvShdGround terrain."""
    from pvsyst_auto.native.headless import _faces, _shd_object, _shd_tree
    from pvsyst_auto.scene.author import obstacle_profiles

    if module_orient == "moLandscape":
        dim_along_axis = module_length_m
        dim_across_axis = module_width_m
    else:
        dim_along_axis = module_width_m
        dim_across_axis = module_length_m
    long_shed = module_rows * dim_across_axis + max(0, module_rows - 1) * spacing_y_m
    larg_shed = module_columns * dim_along_axis + max(0, module_columns - 1) * spacing_x_m

    parsed_obstacles = obstacle_profiles(obstacles) if obstacles else []
    total_objs = tracker_count + 1 + len(parsed_obstacles)

    cx0 = 0.5 * (mesh.bounds["min_x"] + mesh.bounds["max_x"])
    cy0 = 0.5 * (mesh.bounds["min_y"] + mesh.bounds["max_y"])
    az_rad = math.radians(axis_azimuth_deg)
    # Tracker axis runs N-S (+y rotated by azimuth); rows step E-W (+x rotated by azimuth)
    axis_dx, axis_dy = math.sin(az_rad), math.cos(az_rad)
    step_dx, step_dy = math.cos(az_rad), -math.sin(az_rad)

    lines = [
        "  PVObject_Ombrage=pvShading",
        "    Comment=3D Terrain-draped horizontal N-S tracker shading scene",
        "    Flags=$000A0300",
        "    Precision=1",
        "    FracOmbreMod=1.000",
        "    FracOmbreThin=0.400",
        "    GCROffset=1.0",
        "    DisplayedRef=refGeographical",
        "",
        f"    ListeObjets, list of={total_objs} TShdObject",
        "",
    ]

    tracker_summaries: list[dict[str, Any]] = []
    start_offset = -0.5 * (tracker_count - 1) * pitch_ew_m
    obj_idx = 0
    for t_idx in range(tracker_count):
        obj_idx += 1
        s_off = start_offset + t_idx * pitch_ew_m
        tx = cx0 + s_off * step_dx
        ty = cy0 + s_off * step_dy
        z_g, _ = mesh.sample(tx, ty)
        tz = z_g + ground_clearance_m
        axis_tilt = 0.0
        if follow_slope:
            axis_tilt = mesh.slope_deg_along_direction(tx, ty, axis_dx, axis_dy, span_m=larg_shed)
            axis_tilt = max(-max_axis_tilt_deg, min(max_axis_tilt_deg, axis_tilt))
        tracker_summaries.append({
            "index": t_idx + 1,
            "origin_m": [round(tx, 3), round(ty, 3), round(tz, 3)],
            "axis_tilt_deg": round(axis_tilt, 3),
        })
        lines.extend([
            f"      PVObject_{obj_idx}=pvShdArrayTracking",
            f"        Comment=Tracker #{t_idx + 1}",
            f"        Version={version}",
            "        Flags=$02208064",
            f"        NoObjCreated={obj_idx}",
            "        ColorTechn=$00FF0000",
            "        ColorSurf=$00FF9999",
            "        ColorBord=$00505050",
            f"        DOrigine={tx:.6f},{ty:.6f},{tz:.6f}",
            f"        DInclin={axis_tilt:.6f}",
            "        BaseSlope=0.000000",
            f"        DAzimut={axis_azimuth_deg:.6f}",
            "        Source=osPVsyst",
            "      NElemChamps=1",
            "      NoOrient=1",
            f"      PanneauPVName={module_name};{module_width_m:.3f};{module_length_m:.3f}",
            f"      ModSpacingX={spacing_x_m:.3f}",
            f"      ModSpacingY={spacing_y_m:.3f}",
            f"      NModInHeight={module_rows}",
            f"      NModInWidth={module_columns}",
            f"      ModuleOrient={module_orient}",
            f"      DXModule={dim_across_axis + spacing_y_m:.3f}",
            f"      DYModule={larg_shed:.3f}",
            f"        LongShed={long_shed:.6f}",
            f"        LargShed={larg_shed:.6f}",
            "        Cadre=0.000,0.000,0.000,0.000,",
            "        TypeOrigine=2",
            "        PitchNS=0.000",
            "        PitchEW=0.000",
            "        AxisOrig=2",
            "      End of PVObject pvShdArrayTracking",
        ])

    obj_idx += 1
    lines.extend(emit_pvshd_ground(mesh, object_index=obj_idx, version=SEED_RECORD_VERSION))

    for obs in parsed_obstacles:
        obj_idx += 1
        if obs["kind"] == "tree":
            lines.extend(_shd_tree(obj_idx, obj_idx, obs, 0.0, 0.0))
        else:
            world_verts = [tuple(v) for v in obs["vertices"]]
            lines.extend(_shd_object(obj_idx, obj_idx, world_verts, _faces(obs)))

    lines.extend([
        "    End of ListeObjets",
        "  End of PVObject pvShading",
    ])
    return lines, {
        "terrain": mesh.summary(),
        "tracker_count": tracker_count,
        "trackers": tracker_summaries,
    }

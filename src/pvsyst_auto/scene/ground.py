"""Finite 3D ground PV array layout (2D polygon clipping) and pvShading emission.

Pure Python standard library geometry: validates a 2D site boundary polygon, applies inward
setback clearance, sweeps east-west PV rows at the requested azimuth and pitch, clips each row
strip to maximal non-overlapping finite tables, and emits native PVsyst 8.1.7 pvShading text
with optional 3D obstacles (boxes, parapets, cylinders, trees) and verified BaseSlope support.
"""
from __future__ import annotations

import math
from typing import Any

from pvsyst_auto.native.headless import _faces, _shd_object, _shd_tree
from pvsyst_auto.scene.author import obstacle_profiles

_EPS = 1e-9


from pvsyst_auto.validation import finite_number as _finite


def validate_site_polygon(vertices: Any) -> list[tuple[float, float]]:
    """Validate a 2D site polygon [[x0, y0], ...] with 3..64 vertices and positive area."""
    if not isinstance(vertices, list) or not 3 <= len(vertices) <= 64:
        raise ValueError("site_polygon must be a list of 3..64 [x, y] vertices")
    pts: list[tuple[float, float]] = []
    for idx, pt in enumerate(vertices):
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise ValueError(f"site_polygon[{idx}] must be a [x, y] pair")
        pts.append((_finite(pt[0], f"site_polygon[{idx}][0]"), _finite(pt[1], f"site_polygon[{idx}][1]")))
    if pts[0] == pts[-1] and len(pts) > 3:
        pts.pop()
    area = polygon_area(pts)
    if area <= 1e-3:
        raise ValueError("site_polygon must enclose a positive non-degenerate area")
    return pts


def polygon_area(pts: list[tuple[float, float]]) -> float:
    acc = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        acc += x1 * y2 - x2 * y1
    return 0.5 * abs(acc)


def _point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon check (inclusive of boundary)."""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        # On segment check
        if _point_segment_distance(x, y, x1, y1, x2, y2) <= 1e-7:
            return True
        if ((y1 > y) != (y2 > y)):
            x_int = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_int:
                inside = not inside
    return inside


def _point_segment_distance(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    len_sq = dx * dx + dy * dy
    if len_sq <= _EPS:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / len_sq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _min_edge_distance(x: float, y: float, poly: list[tuple[float, float]]) -> float:
    n = len(poly)
    return min(_point_segment_distance(x, y, poly[i][0], poly[i][1], poly[(i + 1) % n][0], poly[(i + 1) % n][1])
               for i in range(n))


def _point_clear(x: float, y: float, poly: list[tuple[float, float]], setback: float) -> bool:
    if not _point_in_polygon(x, y, poly):
        return False
    if setback > 0.0 and _min_edge_distance(x, y, poly) < setback - 1e-7:
        return False
    return True


def _rect_inside_polygon(
    u0: float, u1: float, v0: float, v1: float,
    poly: list[tuple[float, float]], setback: float,
) -> bool:
    """Check whether axis-aligned rectangle [u0,u1] x [v0,v1] fits inside poly with setback."""
    # Check 4 corners + edge midpoints + center
    samples = (
        (u0, v0), (u1, v0), (u1, v1), (u0, v1),
        (0.5 * (u0 + u1), v0), (0.5 * (u0 + u1), v1),
        (u0, 0.5 * (v0 + v1)), (u1, 0.5 * (v0 + v1)),
        (0.5 * (u0 + u1), 0.5 * (v0 + v1)),
    )
    if not all(_point_clear(u, v, poly, setback) for u, v in samples):
        return False
    # Ensure no polygon vertex intrudes inside the rectangle (handles concave notches)
    for px, py in poly:
        if u0 - setback < px < u1 + setback and v0 - setback < py < v1 + setback:
            if u0 <= px <= u1 and v0 <= py <= v1:
                return False
            # Distance from concave vertex to rectangle must be >= setback
            cx = min(max(px, u0), u1)
            cy = min(max(py, v0), v1)
            if math.hypot(px - cx, py - cy) < setback - 1e-7:
                return False
    return True


def _rotate(x: float, y: float, angle_rad: float) -> tuple[float, float]:
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    return x * c - y * s, x * s + y * c


def _scanline_intervals(y: float, poly: list[tuple[float, float]]) -> list[tuple[float, float]]:
    xs: list[float] = []
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 <= y < y2) or (y2 <= y < y1):
            xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
    xs.sort()
    return [(xs[i], xs[i + 1]) for i in range(0, len(xs) - 1, 2)]


def plan_ground_polygon_layout(
    site_polygon: list[list[float]],
    *,
    tilt_deg: float,
    azimuth_deg: float = 0.0,
    pitch_m: float,
    module_width_m: float,
    module_length_m: float,
    module_rows: int = 2,
    module_orientation: str = "portrait",
    spacing_x_m: float = 0.02,
    spacing_y_m: float = 0.02,
    setback_m: float = 0.0,
    ground_clearance_m: float = 0.5,
    max_table_columns: int | None = None,
    base_slope_deg: float = 0.0,
) -> dict[str, Any]:
    """Place maximal non-overlapping finite PV tables inside a 2D site polygon."""
    poly_world = validate_site_polygon(site_polygon)
    tilt = _finite(tilt_deg, "tilt_deg")
    azim = _finite(azimuth_deg, "azimuth_deg")
    pitch = _finite(pitch_m, "pitch_m")
    setback = _finite(setback_m, "setback_m")
    clearance = _finite(ground_clearance_m, "ground_clearance_m")
    base_slope = _finite(base_slope_deg, "base_slope_deg")
    if not 0.0 <= tilt <= 90.0:
        raise ValueError("tilt_deg must lie in 0..90")
    if not -180.0 <= azim <= 180.0:
        raise ValueError("azimuth_deg must lie in -180..180")
    if pitch <= 0:
        raise ValueError("pitch_m must be positive")
    if setback < 0:
        raise ValueError("setback_m must be non-negative")
    if clearance < 0:
        raise ValueError("ground_clearance_m must be non-negative")
    if not -45.0 <= base_slope <= 45.0:
        raise ValueError("base_slope_deg must lie in -45..45")
    if module_orientation not in ("portrait", "landscape"):
        raise ValueError("module_orientation must be 'portrait' or 'landscape'")
    if max_table_columns is not None and (type(max_table_columns) is not int or max_table_columns < 1):
        raise ValueError("max_table_columns must be a positive integer")

    cell_w = module_width_m if module_orientation == "portrait" else module_length_m
    cell_h = module_length_m if module_orientation == "portrait" else module_width_m
    larg_shed = module_rows * cell_h + max(0, module_rows - 1) * spacing_y_m
    proj_depth = larg_shed * math.cos(math.radians(tilt))
    if pitch <= proj_depth:
        raise ValueError(
            f"pitch_m ({pitch:.3f} m) must exceed horizontal table depth ({proj_depth:.3f} m)"
        )
    col_step = cell_w + spacing_x_m

    # Rotate world polygon into row-aligned frame (PVsyst azimuth: 0=south, west positive -> rotate by +azim)
    rot_rad = math.radians(azim)
    poly_rot = [_rotate(x, y, rot_rad) for x, y in poly_world]

    us = [p[0] for p in poly_rot]
    vs = [p[1] for p in poly_rot]
    v_min, v_max = min(vs) + setback, max(vs) - setback

    tables: list[dict[str, Any]] = []
    row_idx = 0
    v = v_min
    while v + proj_depth <= v_max + 1e-6:
        # Keep both scanlines within the projected row strip, including vertical tables.
        scan_offset = min(1e-4, proj_depth / 2)
        front_iv = _scanline_intervals(v + scan_offset, poly_rot)
        back_iv = _scanline_intervals(v + proj_depth - scan_offset, poly_rot)
        # Overlap intervals of front and back
        overlaps: list[tuple[float, float]] = []
        for a0, a1 in front_iv:
            for b0, b1 in back_iv:
                lo, hi = max(a0, b0) + setback, min(a1, b1) - setback
                if hi - lo >= cell_w - 1e-6:
                    overlaps.append((lo, hi))

        row_tables = 0
        for seg_lo, seg_hi in overlaps:
            # Walk across [seg_lo, seg_hi] finding maximal valid column runs
            u = seg_lo
            while u + cell_w <= seg_hi + 1e-6:
                # Find how many consecutive columns fit starting at u
                max_avail = int(math.floor((seg_hi - u + spacing_x_m + 1e-6) / col_step))
                if max_table_columns is not None:
                    max_avail = min(max_avail, max_table_columns)
                fitted = 0
                for k in range(max_avail, 0, -1):
                    span = k * cell_w + (k - 1) * spacing_x_m
                    if _rect_inside_polygon(u, u + span, v, v + proj_depth, poly_rot, setback):
                        fitted = k
                        break
                if fitted > 0:
                    span = fitted * cell_w + (fitted - 1) * spacing_x_m
                    # Center of front edge in row frame -> world origin
                    u_origin = u + 0.5 * span
                    v_origin = v
                    wx, wy = _rotate(u_origin, v_origin, -rot_rad)
                    row_tables += 1
                    tables.append({
                        "name": f"GroundRow{row_idx + 1:02d}_T{row_tables:02d}",
                        "row_index": row_idx + 1,
                        "module_rows": module_rows,
                        "module_columns": fitted,
                        "module_count": module_rows * fitted,
                        "long_shed_m": round(span, 6),
                        "larg_shed_m": round(larg_shed, 6),
                        "origin_m": [round(wx, 6), round(wy, 6), round(clearance, 6)],
                        "tilt_deg": tilt,
                        "azimuth_deg": azim,
                        "base_slope_deg": base_slope,
                    })
                    u += span + max(spacing_x_m, 0.5)
                else:
                    u += col_step
        if row_tables > 0:
            row_idx += 1
        v += pitch

    if not tables:
        raise ValueError("No PV tables fit inside site_polygon with the requested pitch_m, tilt, and setback_m")

    site_area = polygon_area(poly_world)
    total_modules = sum(t["module_count"] for t in tables)
    collector_area = sum(t["long_shed_m"] * t["larg_shed_m"] for t in tables)
    return {
        "tables": tables,
        "row_count": row_idx,
        "table_count": len(tables),
        "total_modules": total_modules,
        "site_area_m2": round(site_area, 2),
        "collector_area_m2": round(collector_area, 2),
        "gcr": round(collector_area / site_area, 4) if site_area > 0 else 0.0,
        "proj_depth_m": round(proj_depth, 4),
        "base_slope_deg": base_slope,
    }


def build_ground_3d_shading(
    tables: list[dict[str, Any]],
    *,
    module_name: str,
    module_width_m: float,
    module_length_m: float,
    module_orient: str = "moPortrait",
    spacing_x_m: float = 0.02,
    spacing_y_m: float = 0.02,
    obstacles: list[dict[str, Any]] | None = None,
    terrain_mesh: Any = None,
    orientation_id: int = 1,
    version: str = "8.1.7",
) -> list[str]:
    """Emit a complete PVObject_Ombrage=pvShading block for finite ground tables + optional terrain + 3D obstacles."""
    if not tables:
        raise ValueError("build_ground_3d_shading requires at least one table")
    parsed_obstacles = obstacle_profiles(obstacles) if obstacles else []
    total_objs = len(tables) + len(parsed_obstacles) + (1 if terrain_mesh is not None else 0)

    lines = [
        "  PVObject_Ombrage=pvShading",
        "    Comment=Finite 3D ground polygon shading scene",
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

    obj_idx = 0
    for tbl in tables:
        obj_idx += 1
        ox, oy, oz = tbl["origin_m"]
        lines.extend([
            f"      PVObject_{obj_idx}=pvShdArrayTable",
            f"        Comment={tbl['name']}",
            f"        Version={version}",
            "        Flags=$02200074",
            f"        NoObjCreated={obj_idx}",
            "        NPElemRes=1",
            "        ColorTechn=$00FF0000",
            "        ColorSurf=$00FF9999",
            "        ColorBord=$00505050",
            f"        DOrigine={ox:.6f},{oy:.6f},{oz:.6f}",
            f"        DInclin={tbl['tilt_deg']:.6f}",
            f"        BaseSlope={tbl.get('base_slope_deg', 0.0):.6f}",
            f"        DAzimut={tbl['azimuth_deg']:.6f}",
            "        Source=osPVCollada",
            "      NElemChamps=1",
            f"      NoOrient={orientation_id}",
            f"      PanneauPVName={module_name};{module_width_m:.3f};{module_length_m:.3f}",
            f"      ModSpacingX={spacing_x_m:.3f}",
            f"      ModSpacingY={spacing_y_m:.3f}",
            f"      NModInHeight={tbl['module_rows']}",
            f"      NModInWidth={tbl['module_columns']}",
            f"      ModuleOrient={module_orient}",
            f"        LongShed={tbl['long_shed_m']:.6f}",
            f"        LargShed={tbl['larg_shed_m']:.6f}",
            "        Cadre=0.000,0.000,0.000,0.000,",
            "      TypeOrigine=2",
            "      End of PVObject pvShdArrayTable",
        ])

    if terrain_mesh is not None:
        from pvsyst_auto.scene.terrain import emit_pvshd_ground
        obj_idx += 1
        lines.extend(emit_pvshd_ground(terrain_mesh, object_index=obj_idx, version="8.1.0"))

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
    return lines

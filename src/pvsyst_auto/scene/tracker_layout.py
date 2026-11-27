"""Polygon-clipped 3D single-axis tracker layout planner with integer-string row alignment."""
from __future__ import annotations

import math
from typing import Any

from pvsyst_auto.scene.author import obstacle_profiles
from pvsyst_auto.scene.exclusions import (
    rect_overlaps_any_polygon,
    subtract_exclusions_from_intervals,
    validate_exclusion_zones,
)
from pvsyst_auto.scene.partition import (
    compute_table_partition_dims,
    normalize_electrical_shading,
    shading_header_kwargs,
)
from pvsyst_auto.scene.reader import (
    _rect_inside_polygon,
    _rotate,
    _scanline_intervals,
    emit_shading_obstacles,
    polygon_area,
    pv_shading_header,
    validate_site_polygon,
)
from pvsyst_auto.validation import SEED_RECORD_VERSION, SYNTH_VERSION, finite_number as _finite


def tracker_columns_per_string(n_mod_serie: int | None, module_rows: int) -> int:
    """Return minimum columns along tracker axis yielding exact whole electrical strings."""
    if type(module_rows) is not int or module_rows < 1:
        raise ValueError("module_rows must be a strict positive integer")
    if n_mod_serie is None:
        return 1
    if type(n_mod_serie) is not int or n_mod_serie < 1:
        raise ValueError("n_mod_serie must be a strict positive integer")
    return n_mod_serie // math.gcd(n_mod_serie, module_rows)


def plan_tracker_polygon_layout(
    site_polygon: list[list[float]],
    *,
    axis_azimuth_deg: float = 0.0,
    pitch_ew_m: float,
    module_width_m: float,
    module_length_m: float,
    module_rows: int = 1,
    module_orientation: str = "portrait",
    spacing_x_m: float = 0.02,
    spacing_y_m: float = 0.02,
    setback_m: float = 0.0,
    ground_clearance_m: float = 1.5,
    max_table_columns: int | None = None,
    n_mod_serie: int | None = None,
    exclusion_zones: list[list[list[float]]] | None = None,
) -> dict[str, Any]:
    """Place N-S tracker tables inside a polygon, clipping each row to integer string multiples."""
    poly_world = validate_site_polygon(site_polygon)
    excl_world = validate_exclusion_zones(exclusion_zones)
    axis_azim = _finite(axis_azimuth_deg, "axis_azimuth_deg")
    pitch_ew = _finite(pitch_ew_m, "pitch_ew_m", positive=True)
    mw = _finite(module_width_m, "module_width_m", positive=True)
    ml = _finite(module_length_m, "module_length_m", positive=True)
    sx = _finite(spacing_x_m, "spacing_x_m", minimum=0.0)
    sy = _finite(spacing_y_m, "spacing_y_m", minimum=0.0)
    setback = _finite(setback_m, "setback_m", minimum=0.0)
    clearance = _finite(ground_clearance_m, "ground_clearance_m", minimum=0.0)
    if not -180.0 <= axis_azim <= 180.0:
        raise ValueError("axis_azimuth_deg must lie in -180..180")
    if module_orientation not in ("portrait", "landscape"):
        raise ValueError("module_orientation must be 'portrait' or 'landscape'")
    if max_table_columns is not None and (type(max_table_columns) is not int or max_table_columns < 1):
        raise ValueError("max_table_columns must be a positive integer")

    col_quantum = tracker_columns_per_string(n_mod_serie, module_rows)
    if max_table_columns is not None and max_table_columns < col_quantum:
        raise ValueError(
            f"max_table_columns ({max_table_columns}) is smaller than one whole string quantum ({col_quantum} columns)"
        )

    chord_mod, length_mod = (ml, mw) if module_orientation == "portrait" else (mw, ml)
    long_shed = module_rows * chord_mod + max(0, module_rows - 1) * sy
    if pitch_ew <= long_shed:
        raise ValueError(f"pitch_m ({pitch_ew:.3f} m) must exceed tracker chord width ({long_shed:.3f} m)")

    len_step = length_mod + sx
    min_span = col_quantum * length_mod + max(0, col_quantum - 1) * sx
    rot_rad = math.radians(axis_azim)
    poly_rot = [_rotate(x, y, rot_rad) for x, y in poly_world]
    poly_uv_swapped = [(y, x) for x, y in poly_rot]
    excl_rot = [[_rotate(x, y, rot_rad) for x, y in p] for p in excl_world]
    excl_uv_swapped = [[(y, x) for x, y in p] for p in excl_rot]
    us = [p[0] for p in poly_rot]
    u, u_max = min(us) + setback, max(us) - setback

    tables: list[dict[str, Any]] = []
    row_idx = 0
    while u + long_shed <= u_max + 1e-6:
        scan_eps = min(1e-4, 0.5 * long_shed)
        left_iv = _scanline_intervals(u + scan_eps, poly_uv_swapped)
        right_iv = _scanline_intervals(u + long_shed - scan_eps, poly_uv_swapped)
        overlaps = [
            (lo, hi) for a0, a1 in left_iv for b0, b1 in right_iv
            if (hi := min(a1, b1) - setback) - (lo := max(a0, b0) + setback) >= min_span - 1e-6
        ]
        if excl_uv_swapped:
            overlaps = subtract_exclusions_from_intervals(
                overlaps, excl_uv_swapped, u, u + long_shed, min_length=min_span - 1e-6
            )
        row_tables = 0
        for seg_lo, seg_hi in overlaps:
            v = seg_lo
            while v + min_span <= seg_hi + 1e-6:
                max_avail = int(math.floor((seg_hi - v + sx + 1e-6) / len_step))
                if max_table_columns is not None:
                    max_avail = min(max_avail, max_table_columns)
                max_mult = (max_avail // col_quantum) * col_quantum
                fitted = next(
                    (
                        k for k in range(max_mult, col_quantum - 1, -col_quantum)
                        if _rect_inside_polygon(
                            u, u + long_shed, v, v + (k * length_mod + (k - 1) * sx), poly_rot, setback
                        )
                        and (
                            not excl_rot
                            or not rect_overlaps_any_polygon(
                                u, u + long_shed, v, v + (k * length_mod + (k - 1) * sx), excl_rot
                            )
                        )
                    ),
                    0,
                )
                if fitted > 0:
                    span = fitted * length_mod + (fitted - 1) * sx
                    wx, wy = _rotate(u + 0.5 * long_shed, v + 0.5 * span, -rot_rad)
                    row_tables += 1
                    mod_cnt = module_rows * fitted
                    tables.append({
                        "name": f"TrackerRow{row_idx + 1:02d}_T{row_tables:02d}",
                        "row_index": row_idx + 1,
                        "module_rows": module_rows,
                        "module_columns": fitted,
                        "module_count": mod_cnt,
                        "string_count": (mod_cnt // n_mod_serie) if n_mod_serie is not None else None,
                        "long_shed_m": round(long_shed, 6),
                        "larg_shed_m": round(span, 6),
                        "origin_m": [round(wx, 6), round(wy, 6), round(clearance, 6)],
                        "axis_azimuth_deg": axis_azim,
                        "axis_tilt_deg": 0.0,
                    })
                    v += span + max(sx, 1.0)
                else:
                    v += len_step
        if row_tables > 0:
            row_idx += 1
        u += pitch_ew

    if not tables:
        raise ValueError(
            "No integer-string tracker tables fit inside site_polygon with the requested pitch_m, setback_m, and string length"
        )
    site_area = polygon_area(poly_world)
    total_modules = sum(int(t["module_count"]) for t in tables)
    total_strings = sum(int(t["string_count"]) for t in tables) if n_mod_serie is not None else None
    collector_area = sum(float(t["long_shed_m"]) * float(t["larg_shed_m"]) for t in tables)
    return {
        "tables": tables,
        "row_count": row_idx,
        "tracker_count": len(tables),
        "table_count": len(tables),
        "total_modules": total_modules,
        "total_strings": total_strings,
        "columns_per_string": col_quantum,
        "site_area_m2": round(site_area, 2),
        "collector_area_m2": round(collector_area, 2),
        "gcr": round(collector_area / site_area, 4) if site_area > 0.0 else 0.0,
        "chord_width_m": round(long_shed, 4),
        "exclusion_zone_count": len(excl_world),
        "exclusion_area_m2": round(sum(polygon_area(p) for p in excl_world), 2),
    }


def drape_tracker_tables_on_terrain(
    tables: list[dict[str, Any]],
    terrain_mesh: Any,
    *,
    ground_clearance_m: float = 1.5,
    follow_slope: bool = True,
    max_axis_tilt_deg: float = 15.0,
) -> list[dict[str, Any]]:
    """Drape tracker tables onto terrain elevation and optional axis slope without mutating inputs."""
    clearance = _finite(ground_clearance_m, "ground_clearance_m", minimum=0.0)
    max_tilt = _finite(max_axis_tilt_deg, "max_axis_tilt_deg", minimum=0.0, maximum=45.0)
    draped: list[dict[str, Any]] = []
    for tbl in tables:
        entry = dict(tbl)
        ox, oy, _ = [float(v) for v in entry["origin_m"]]
        axis_az = float(entry.get("axis_azimuth_deg", 0.0))
        axis_tilt = float(entry.get("axis_tilt_deg", 0.0))
        z_g, _ = terrain_mesh.sample(ox, oy)
        oz = z_g + clearance
        if follow_slope:
            az_rad = math.radians(axis_az)
            axis_tilt = terrain_mesh.slope_deg_along_direction(
                ox, oy, math.sin(az_rad), math.cos(az_rad), span_m=float(entry["larg_shed_m"])
            )
            axis_tilt = max(-max_tilt, min(max_tilt, axis_tilt))
        entry["origin_m"] = [round(ox, 6), round(oy, 6), round(oz, 6)]
        entry["axis_tilt_deg"] = round(axis_tilt, 3)
        draped.append(entry)
    return draped


def build_tracker_3d_shading(
    tables: list[dict[str, Any]],
    *,
    module_name: str,
    module_width_m: float,
    module_length_m: float,
    module_orient: str = "moPortrait",
    spacing_x_m: float = 0.02,
    spacing_y_m: float = 0.02,
    ground_clearance_m: float = 1.5,
    obstacles: list[dict[str, Any]] | None = None,
    terrain_mesh: Any = None,
    terrain_opts: dict[str, Any] | None = None,
    orientation_id: int = 1,
    electrical_shading: Any = None,
    series_modules: int | None = None,
    is_half_cut: bool = False,
    version: str = SYNTH_VERSION,
) -> tuple[list[str], dict[str, Any] | None]:
    """Emit PVObject_Ombrage=pvShading for variable-length 3D trackers + optional terrain/obstacles."""
    if not tables:
        raise ValueError("build_tracker_3d_shading requires at least one tracker table")
    cfg = normalize_electrical_shading(electrical_shading)
    clearance = _finite(ground_clearance_m, "ground_clearance_m", minimum=0.0)
    max_axis_tilt = _finite((terrain_opts or {}).get("max_axis_tilt_deg", 15.0), "max_axis_tilt_deg", minimum=0.0, maximum=45.0)
    parsed_obs = obstacle_profiles(obstacles) if obstacles else []
    total_objs = len(tables) + len(parsed_obs) + (1 if terrain_mesh is not None else 0)
    lines = pv_shading_header("Finite 3D N-S tracker polygon shading scene", total_objs, **shading_header_kwargs(cfg))

    drape = terrain_mesh is not None and bool((terrain_opts or {}).get("drape_tables", True))
    follow_slope = bool((terrain_opts or {}).get("follow_slope", True))
    if drape:
        work_tables = drape_tracker_tables_on_terrain(
            tables,
            terrain_mesh,
            ground_clearance_m=clearance,
            follow_slope=follow_slope,
            max_axis_tilt_deg=max_axis_tilt,
        )
    else:
        work_tables = [dict(t) for t in tables]
    flags = "        Flags=$02208064" if terrain_mesh is not None else "        Flags=$02200074"
    tracker_summaries: list[dict[str, Any]] = []

    obj_idx = 0
    for idx_t, tbl in enumerate(work_tables, start=1):
        obj_idx += 1
        ox, oy, oz = [float(v) for v in tbl["origin_m"]]
        axis_az = float(tbl.get("axis_azimuth_deg", 0.0))
        axis_tilt = float(tbl.get("axis_tilt_deg", 0.0))
        tracker_summaries.append({
            "index": idx_t,
            "name": tbl["name"],
            "origin_m": [round(ox, 3), round(oy, 3), round(oz, 3)],
            "axis_tilt_deg": round(axis_tilt, 3),
            "module_columns": int(tbl["module_columns"]),
        })
        tbl_orient = str(tbl.get("module_orient") or module_orient)
        if cfg["mode"] == "partition":
            dx_mod, dy_mod, _, _ = compute_table_partition_dims(
                long_shed_m=float(tbl["long_shed_m"]),
                larg_shed_m=float(tbl["larg_shed_m"]),
                module_rows=int(tbl["module_rows"]),
                module_columns=int(tbl["module_columns"]),
                module_orient=tbl_orient,
                is_tracker=True,
                electrical_shading=cfg,
                series_modules=series_modules,
                is_half_cut=is_half_cut,
            )
        else:
            tbl_chord_mod = module_length_m if tbl_orient in ("moPortrait", "portrait") else module_width_m
            dx_mod = tbl_chord_mod + spacing_y_m
            dy_mod = float(tbl["larg_shed_m"])
        lines.extend([
            f"      PVObject_{obj_idx}=pvShdArrayTracking",
            f"        Comment={tbl['name']}",
            f"        Version={version}",
            flags,
            f"        NoObjCreated={obj_idx}",
            "        ColorTechn=$00FF0000",
            "        ColorSurf=$00FF9999",
            "        ColorBord=$00505050",
            f"        DOrigine={ox:.6f},{oy:.6f},{oz:.6f}",
            f"        DInclin={axis_tilt:.6f}",
            "        BaseSlope=0.000000",
            f"        DAzimut={axis_az:.6f}",
            "        Source=osPVsyst",
            "      NElemChamps=1",
            f"      NoOrient={orientation_id}",
            f"      PanneauPVName={module_name};{module_width_m:.3f};{module_length_m:.3f}",
            f"      ModSpacingX={spacing_x_m:.3f}",
            f"      ModSpacingY={spacing_y_m:.3f}",
            f"      NModInHeight={tbl['module_rows']}",
            f"      NModInWidth={tbl['module_columns']}",
            f"      ModuleOrient={tbl_orient}",
            f"      DXModule={dx_mod:.3f}",
            f"      DYModule={dy_mod:.3f}",
            f"        LongShed={tbl['long_shed_m']:.6f}",
            f"        LargShed={tbl['larg_shed_m']:.6f}",
            "        Cadre=0.000,0.000,0.000,0.000,",
            # TypeOrigine=7 anchors DOrigine at rectangle center, matching plan_tracker_polygon_layout.
            "        TypeOrigine=7",
            "        PitchNS=0.000",
            "        PitchEW=0.000",
            "        AxisOrig=2",
            "      End of PVObject pvShdArrayTracking",
        ])

    terrain_summary = None
    if terrain_mesh is not None:
        from pvsyst_auto.scene.terrain import emit_pvshd_ground
        obj_idx += 1
        lines.extend(emit_pvshd_ground(terrain_mesh, object_index=obj_idx, version=SEED_RECORD_VERSION))
        terrain_summary = {
            "terrain": terrain_mesh.summary(),
            "tracker_count": len(tables),
            "trackers": tracker_summaries,
        }
    lines.extend(emit_shading_obstacles(parsed_obs, start_index=obj_idx + 1))
    return [*lines, "    End of ListeObjets", "  End of PVObject pvShading"], terrain_summary

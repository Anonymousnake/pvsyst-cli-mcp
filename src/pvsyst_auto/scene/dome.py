"""East-west dual-tilt dome orientation geometry, 3D pvShdArrayDome builder, and electrical wiring helpers."""
from __future__ import annotations

import math
from typing import Any

from pvsyst_auto.scene.author import obstacle_profiles
from pvsyst_auto.scene.reader import emit_shading_obstacles, plan_ground_polygon_layout, pv_shading_header
from pvsyst_auto.validation import SEED_RECORD_VERSION, SYNTH_VERSION, finite_number as _finite


def opposite_azimuth_deg(azimuth_deg: float) -> float:
    """Return the PVsyst opposite azimuth in (-180, 180] normalized cleanly."""
    az = _finite(azimuth_deg, "azimuth_deg")
    if not -180.0 <= az <= 180.0:
        raise ValueError("azimuth_deg must lie in -180..180")
    opp = az - 180.0 if az > 0.0 else az + 180.0
    if abs(opp) < 1e-12:
        return 0.0
    return 180.0 if abs(opp - 180.0) < 1e-12 else opp


def dome_footprint_depth_m(larg_shed_m: float, tilt_deg: float, top_spacing_m: float) -> float:
    """Horizontal ground depth of one paired east-west dome row (two opposing faces + ridge gap)."""
    larg = _finite(larg_shed_m, "larg_shed_m", positive=True)
    tilt = _finite(tilt_deg, "tilt_deg")
    gap = _finite(top_spacing_m, "top_spacing_m")
    if not 0.0 < tilt <= 90.0:
        raise ValueError("tilt_deg must lie in (0, 90] for ground_dome")
    if gap < 0.0:
        raise ValueError("top_spacing_m must be non-negative")
    return 2.0 * larg * math.cos(math.radians(tilt)) + gap


def _pos_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def emit_dome_pair_objects(
    *,
    obj_idx_front: int,
    obj_idx_back: int,
    comment_prefix: str,
    n_elems: int,
    origin: tuple[float, float, float],
    tilt_deg: float,
    base_slope_deg: float,
    azimuth_deg: float,
    front_orient_id: int = 1,
    back_orient_id: int = 2,
    module_name: str,
    module_width_m: float,
    module_length_m: float,
    module_rows: int,
    module_columns: int,
    module_orient: str,
    spacing_x_m: float,
    spacing_y_m: float,
    long_shed_m: float,
    larg_shed_m: float,
    pitch_ns_m: float,
    top_spacing_m: float,
    version: str = SYNTH_VERSION,
) -> list[str]:
    """Emit a paired front/back pvShdArrayDome object pair sharing identical origin and front azimuth."""
    cell_w, cell_h = (module_width_m, module_length_m) if module_orient == "moPortrait" else (module_length_m, module_width_m)
    dx_mod, dy_mod = cell_w + spacing_x_m, cell_h + spacing_y_m
    ox, oy, oz = origin

    def _one(obj_idx: int, comment: str, orient_id: int, is_main: bool, pair_idx: int) -> list[str]:
        return [
            f"      PVObject_{obj_idx}=pvShdArrayDome",
            f"        Comment={comment}",
            f"        Version={version}",
            "        Flags=$02200054",
            f"        NoObjCreated={obj_idx}",
            "        ColorTechn=$00FF0000",
            "        ColorSurf=$00FF9999",
            "        ColorBord=$00505050",
            f"        DOrigine={ox:.6f},{oy:.6f},{oz:.6f}",
            f"        DInclin={tilt_deg:.6f}",
            f"        BaseSlope={base_slope_deg:.6f}",
            f"        DAzimut={azimuth_deg:.6f}",
            "        Source=osPVsyst",
            f"      NElemChamps={n_elems}",
            f"      NoOrient={orient_id}",
            f"      PanneauPVName={module_name};{module_width_m:.3f};{module_length_m:.3f}",
            f"      ModSpacingX={spacing_x_m:.3f}",
            f"      ModSpacingY={spacing_y_m:.3f}",
            f"      NModInHeight={module_rows}",
            f"      NModInWidth={module_columns}",
            f"      ModuleOrient={module_orient}",
            f"      DXModule={dx_mod:.3f}",
            f"      DYModule={dy_mod:.3f}",
            f"        LongShed={long_shed_m:.6f}",
            f"        LargShed={larg_shed_m:.6f}",
            "        Cadre=0.000,0.000,0.000,0.000,",
            "        TypeOrigine=2",
            f"        PitchNS={pitch_ns_m:.3f}",
            "        PitchEW=0.000",
            f"        IsMainField={'True' if is_main else 'False'}",
            f"        TopSpacing={top_spacing_m:.3f}",
            f"        NoPairField={pair_idx}",
            "      End of PVObject pvShdArrayDome",
        ]

    return [
        *_one(obj_idx_front, comment_prefix, front_orient_id, True, obj_idx_back),
        *_one(obj_idx_back, f"{comment_prefix} backfaces", back_orient_id, False, obj_idx_front),
    ]


def synthesize_ground_dome_shading(
    *,
    shed_count: int = 10,
    pitch_m: float = 6.0,
    tilt_deg: float = 10.0,
    azimuth_deg: float = 90.0,
    base_slope_deg: float = 0.0,
    top_spacing_m: float = 0.2,
    module_rows: int = 1,
    module_columns: int = 25,
    module_width_m: float = 1.052,
    module_length_m: float = 2.131,
    module_name: str = "PV Module",
    module_orient: str = "moPortrait",
    spacing_x_m: float = 0.020,
    spacing_y_m: float = 0.020,
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
    front_orient_id: int = 1,
    back_orient_id: int = 2,
    obstacles: list[dict[str, Any]] | None = None,
    version: str = SYNTH_VERSION,
) -> list[str]:
    """Synthesize a regular paired 3D pvShdArrayDome scene plus optional 3D obstacles."""
    n_sheds = _pos_int(shed_count, "shed_count")
    pitch = _finite(pitch_m, "pitch_m", minimum=0.1)
    tilt = _finite(tilt_deg, "tilt_deg")
    azim = _finite(azimuth_deg, "azimuth_deg")
    slope = _finite(base_slope_deg, "base_slope_deg")
    gap = _finite(top_spacing_m, "top_spacing_m")
    n_rows, n_cols = _pos_int(module_rows, "module_rows"), _pos_int(module_columns, "module_columns")
    cell_w, cell_h = (module_width_m, module_length_m) if module_orient == "moPortrait" else (module_length_m, module_width_m)
    larg_shed = n_rows * cell_h + max(0, n_rows - 1) * spacing_y_m
    long_shed = n_cols * cell_w + max(0, n_cols - 1) * spacing_x_m
    footprint = dome_footprint_depth_m(larg_shed, tilt, gap)
    if pitch <= footprint:
        raise ValueError(f"pitch_m ({pitch:.3f} m) must exceed dome footprint depth ({footprint:.3f} m)")
    parsed_obs = obstacle_profiles(obstacles) if obstacles else []
    lines = pv_shading_header("East-West dome 3D shading scene", 2 + len(parsed_obs))
    lines.extend(emit_dome_pair_objects(
        obj_idx_front=1, obj_idx_back=2, comment_prefix="Dome field #1", n_elems=n_sheds,
        origin=origin, tilt_deg=tilt, base_slope_deg=slope, azimuth_deg=azim,
        front_orient_id=front_orient_id, back_orient_id=back_orient_id,
        module_name=module_name, module_width_m=module_width_m, module_length_m=module_length_m,
        module_rows=n_rows, module_columns=n_cols, module_orient=module_orient,
        spacing_x_m=spacing_x_m, spacing_y_m=spacing_y_m,
        long_shed_m=long_shed, larg_shed_m=larg_shed, pitch_ns_m=pitch, top_spacing_m=gap, version=version,
    ))
    lines.extend(emit_shading_obstacles(parsed_obs, start_index=3))
    return [*lines, "    End of ListeObjets", "  End of PVObject pvShading"]


def build_dome_3d_shading(
    *,
    site_polygon: list[list[float]] | None,
    terrain_mesh: Any,
    terrain_opts: dict[str, Any] | None,
    shed_count: int,
    pitch_m: float,
    tilt_deg: float,
    azimuth_deg: float,
    base_slope_deg: float,
    top_spacing_m: float,
    module_rows: int,
    module_columns: int,
    module_width_m: float,
    module_length_m: float,
    module_name: str,
    module_orient: str,
    spacing_x_m: float,
    spacing_y_m: float,
    setback_m: float,
    ground_clearance_m: float,
    max_table_columns: int | None,
    obstacles: list[dict[str, Any]] | None = None,
    version: str = SYNTH_VERSION,
) -> tuple[list[str], dict[str, Any]]:
    """Build polygon/terrain-draped paired pvShdArrayDome 3D shading scene."""
    cell_w, cell_h = (module_width_m, module_length_m) if module_orient == "moPortrait" else (module_length_m, module_width_m)
    larg_shed = module_rows * cell_h + max(0, module_rows - 1) * spacing_y_m
    footprint = dome_footprint_depth_m(larg_shed, tilt_deg, top_spacing_m)
    if pitch_m <= footprint:
        raise ValueError(f"pitch_m ({pitch_m:.3f} m) must exceed dome footprint depth ({footprint:.3f} m)")
    ground_layout = None
    if site_polygon is not None:
        ground_layout = plan_ground_polygon_layout(
            site_polygon, tilt_deg=tilt_deg, azimuth_deg=azimuth_deg, pitch_m=pitch_m,
            module_width_m=module_width_m, module_length_m=module_length_m, module_rows=module_rows,
            module_orientation="landscape" if module_orient == "moLandscape" else "portrait",
            spacing_x_m=spacing_x_m, spacing_y_m=spacing_y_m, setback_m=setback_m,
            ground_clearance_m=ground_clearance_m, max_table_columns=max_table_columns,
            base_slope_deg=base_slope_deg, proj_depth_override_m=footprint,
        )
        tables = ground_layout["tables"]
    else:
        assert terrain_mesh is not None
        long_s = module_columns * cell_w + max(0, module_columns - 1) * spacing_x_m
        cx0 = 0.5 * (terrain_mesh.bounds["min_x"] + terrain_mesh.bounds["max_x"])
        cy0 = 0.5 * (terrain_mesh.bounds["min_y"] + terrain_mesh.bounds["max_y"])
        az_rad, start_off = math.radians(azimuth_deg), -0.5 * (shed_count - 1) * pitch_m
        step_x, step_y = math.sin(az_rad), math.cos(az_rad)
        tables = [{
            "name": f"TerrainDome_{r + 1:02d}", "row_index": r + 1, "module_rows": module_rows, "module_columns": module_columns,
            "module_count": module_rows * module_columns, "long_shed_m": round(long_s, 6), "larg_shed_m": round(larg_shed, 6),
            "origin_m": [round(cx0 + (start_off + r * pitch_m) * step_x, 6),
                         round(cy0 + (start_off + r * pitch_m) * step_y, 6), round(ground_clearance_m, 6)],
            "tilt_deg": tilt_deg, "azimuth_deg": azimuth_deg, "base_slope_deg": base_slope_deg,
        } for r in range(shed_count)]
    if terrain_mesh is not None and bool((terrain_opts or {}).get("drape_tables", True)):
        from pvsyst_auto.scene.terrain import drape_shed_tables_on_terrain
        tables = drape_shed_tables_on_terrain(
            tables, terrain_mesh, ground_clearance_m=ground_clearance_m,
            follow_slope=bool((terrain_opts or {}).get("follow_slope", True)),
            max_base_slope_deg=float((terrain_opts or {}).get("max_base_slope_deg", 20.0)),
        )
    parsed_obs = obstacle_profiles(obstacles) if obstacles else []
    total_objs = 2 * len(tables) + len(parsed_obs) + (1 if terrain_mesh is not None else 0)
    lines = pv_shading_header("East-West dome 3D polygon/terrain shading scene", total_objs)
    obj_idx = 0
    for idx_t, tbl in enumerate(tables, start=1):
        ox, oy, oz = tbl["origin_m"]
        lines.extend(emit_dome_pair_objects(
            obj_idx_front=obj_idx + 1, obj_idx_back=obj_idx + 2, comment_prefix=f"{tbl['name']}_{idx_t}",
            n_elems=1, origin=(ox, oy, oz), tilt_deg=tbl["tilt_deg"], base_slope_deg=tbl.get("base_slope_deg", base_slope_deg),
            azimuth_deg=tbl["azimuth_deg"], front_orient_id=1, back_orient_id=2,
            module_name=module_name, module_width_m=module_width_m, module_length_m=module_length_m,
            module_rows=tbl["module_rows"], module_columns=tbl["module_columns"], module_orient=module_orient,
            spacing_x_m=spacing_x_m, spacing_y_m=spacing_y_m,
            long_shed_m=tbl["long_shed_m"], larg_shed_m=tbl["larg_shed_m"], pitch_ns_m=0.0, top_spacing_m=top_spacing_m,
            version=version,
        ))
        obj_idx += 2
    if terrain_mesh is not None:
        from pvsyst_auto.scene.terrain import emit_pvshd_ground
        obj_idx += 1
        lines.extend(emit_pvshd_ground(terrain_mesh, object_index=obj_idx, version=SEED_RECORD_VERSION))
    lines.extend(emit_shading_obstacles(parsed_obs, start_index=obj_idx + 1))
    summary = dict(ground_layout) if ground_layout is not None else {
        "row_count": len(tables), "table_count": len(tables),
        "collector_area_m2": round(sum(t["long_shed_m"] * t["larg_shed_m"] for t in tables), 2),
        "proj_depth_m": round(footprint, 4), "base_slope_deg": base_slope_deg,
    }
    summary.update({
        "tables": tables,
        "dome_pair_count": len(tables),
        "total_modules": 2 * sum(int(t["module_count"]) for t in tables),
        "dome_footprint_depth_m": round(footprint, 4),
        "top_spacing_m": round(top_spacing_m, 3),
    })
    return [*lines, "    End of ListeObjets", "  End of PVObject pvShading"], summary


def resolve_dome_subarrays(
    *,
    dome_wiring: str,
    pan_name: str,
    ond_name: str,
    n_mod_serie: int,
    n_string_ch: int,
    n_inverter: int,
    n_inv_mppt: int,
    pnom_ac_in_mppt: float,
    lid_loss: float,
    pmism_mpp: float | None,
    module_quality_loss: float | None,
    dc_wiring_loss_pct: float,
    dc_wiring_resistance_mohm: float,
) -> tuple[list[dict[str, Any]], str]:
    """Build dual_mppt or mixed-orientation subarray entries for an East-West dome."""
    if dome_wiring not in ("auto", "dual_mppt", "mixed"):
        raise ValueError("dome_wiring must be 'auto', 'dual_mppt', or 'mixed'")
    if n_string_ch < 2 or n_string_ch % 2 != 0:
        raise ValueError("East-west dome requires an even total string count to balance front and back dome faces")
    mppt_per_inv = max(1, n_inv_mppt // n_inverter) if n_inverter > 0 else 1
    pnom_ac = pnom_ac_in_mppt * mppt_per_inv
    half_str = n_string_ch // 2
    common = {
        "pan_name": pan_name, "ond_name": ond_name, "n_mod_serie": n_mod_serie, "lid_loss": lid_loss,
        **({"pmism_mpp": pmism_mpp} if pmism_mpp is not None else {}),
        **({"module_quality_loss": module_quality_loss} if module_quality_loss is not None else {}),
        "dc_wiring_loss_pct": dc_wiring_loss_pct, "dc_wiring_resistance_mohm": dc_wiring_resistance_mohm,
    }
    dual_shared_ok = mppt_per_inv >= 2 and mppt_per_inv % 2 == 0 and half_str % (n_inverter * (mppt_per_inv // 2)) == 0
    dual_ded_ok = mppt_per_inv == 1 and n_inverter >= 2 and n_inverter % 2 == 0 and half_str % (n_inverter // 2) == 0

    if dome_wiring in ("auto", "dual_mppt") and dual_shared_ok:
        half_mppt = n_inverter * (mppt_per_inv // 2)
        return [
            {**common, "subarray_id": 1, "orientation_id": 1, "comment": "Dome Front Face", "n_string_ch": half_str,
             "n_inverter": n_inverter, "n_inv_mppt": half_mppt, "independent_mppt": True,
             "power_sharing_inverter_id": 0, "pnom_ac_in_mppt": pnom_ac_in_mppt},
            {**common, "subarray_id": 2, "orientation_id": 2, "comment": "Dome Back Face", "n_string_ch": half_str,
             "n_inverter": n_inverter, "n_inv_mppt": half_mppt, "independent_mppt": True,
             "power_sharing_inverter_id": 0, "pnom_ac_in_mppt": pnom_ac_in_mppt},
        ], "dual_mppt"
    if dome_wiring in ("auto", "dual_mppt") and dual_ded_ok:
        half_inv = n_inverter // 2
        return [
            {**common, "subarray_id": 1, "orientation_id": 1, "comment": "Dome Front Face", "n_string_ch": half_str,
             "n_inverter": half_inv, "n_inv_mppt": half_inv, "independent_mppt": True,
             "power_sharing_inverter_id": -1, "pnom_ac_in_mppt": pnom_ac},
            {**common, "subarray_id": 2, "orientation_id": 2, "comment": "Dome Back Face", "n_string_ch": half_str,
             "n_inverter": half_inv, "n_inv_mppt": half_inv, "independent_mppt": True,
             "power_sharing_inverter_id": -1, "pnom_ac_in_mppt": pnom_ac},
        ], "dual_mppt"
    if dome_wiring == "dual_mppt":
        raise ValueError(
            "dome_wiring='dual_mppt' requires an inverter with an even number >= 2 of MPPT inputs per inverter "
            "(dividing half-strings cleanly), or an even count >= 2 of single-MPPT inverters"
        )
    if n_string_ch % n_inverter != 0:
        raise ValueError(
            f"dome_wiring='mixed' requires n_string_ch ({n_string_ch}) divisible by n_inverter ({n_inverter})"
        )
    return [{
        **common, "subarray_id": 1, "orientation_id": 1, "orientation_id_2": 2, "n_str_orient_1": half_str,
        "comment": "Dome Mixed Array", "n_string_ch": n_string_ch, "n_inverter": n_inverter,
        "n_inv_mppt": 1, "independent_mppt": False, "power_sharing_inverter_id": -1, "pnom_ac_in_mppt": pnom_ac,
    }], "mixed"

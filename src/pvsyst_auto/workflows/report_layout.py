"""VCx 3D shading auto-extraction and interactive Plotly 2D plan-view / 3D terrain & array layout builder."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from pvsyst_auto.components.editor import objects
from pvsyst_auto.project.paths import project_path
from pvsyst_auto.scene.author import obstacle_profiles
from pvsyst_auto.scene.terrain import build_terrain_mesh, triangulate_2d

_LAYOUT_I18N = {
    "zh": {
        "x": "东向 X (m)", "y": "北向 Y (m)", "z": "高程 Z (m)",
        "boundary": "场址边界", "exclusion": "避让/禁建区",
        "fixed": "固定光伏阵列", "orient_fmt": "朝向 #{id} ({tilt:g}° / {az:g}°)",
        "tracker": "平单轴跟踪支架", "dome_front": "东西双坡前坡", "dome_back": "东西双坡后坡",
        "obstacle": "三维障碍物", "tree": "树木障碍物", "terrain": "三维地形表面",
        "hover_mod": "组件数", "hover_tilt": "倾角", "hover_az": "方位角", "hover_orient": "朝向组",
    },
    "en": {
        "x": "East X (m)", "y": "North Y (m)", "z": "Elevation Z (m)",
        "boundary": "Site Boundary", "exclusion": "Exclusion Zones",
        "fixed": "Fixed PV Arrays", "orient_fmt": "Orient #{id} ({tilt:g}° / {az:g}°)",
        "tracker": "1P Trackers", "dome_front": "Dome Front Face", "dome_back": "Dome Back Face",
        "obstacle": "3D Obstacles", "tree": "Tree Obstacles", "terrain": "3D Terrain Surface",
        "hover_mod": "Modules", "hover_tilt": "Tilt", "hover_az": "Azimuth", "hover_orient": "Orientation",
    },
}


def _parse_vec(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) < 3:
        return None
    try:
        vals = [float(parts[0]), float(parts[1]), float(parts[2])]
    except ValueError:
        return None
    return vals if all(math.isfinite(v) for v in vals) else None


def _parse_int_val(raw: str | None, default: int = 1) -> int:
    try:
        return int(str(raw).strip()) if raw is not None else default
    except ValueError:
        return default


def _parse_float_val(raw: str | None, default: float = 0.0) -> float:
    try:
        val = float(str(raw).strip()) if raw is not None else default
        return val if math.isfinite(val) else default
    except ValueError:
        return default


def _parse_points_and_faces(block: Any, origin: list[float]) -> tuple[list[list[float]], list[list[int]]]:
    ox, oy, oz = origin[0], origin[1], origin[2]
    point_items: list[tuple[int, list[float]]] = []
    for key, entries in block.fields.items():
        if key.startswith("Point_") and (idx_str := key[6:]).isdigit() and entries:
            if (vec := _parse_vec(entries[0][1])) is not None:
                point_items.append((int(idx_str), [round(ox + vec[0], 5), round(oy + vec[1], 5), round(oz + vec[2], 5)]))
    point_items.sort(key=lambda item: item[0])
    vertices = [pt for _, pt in point_items]

    raw_chunks = [v for _, v in block.fields.get("Faces", [])] + [v for _, v in block.fields.get("Faces_contd", [])]
    tokens = [
        tok.strip()
        for chunk in raw_chunks
        for tok in chunk.split(",")
        if tok.strip() and tok.strip().lower() != "contd"
    ]
    polygons: list[list[int]] = []
    cur: list[int] = []
    for tok in tokens:
        if tok == "-1":
            if cur:
                polygons.append(cur)
                cur = []
            continue
        try:
            vi = int(tok)
        except ValueError:
            continue
        if 0 <= vi < len(vertices):
            cur.append(vi)
    if cur:
        polygons.append(cur)

    triangles: list[list[int]] = []
    for poly in polygons:
        if len(poly) >= 3:
            for k in range(1, len(poly) - 1):
                t = [poly[0], poly[k], poly[k + 1]]
                if len(set(t)) == 3:
                    triangles.append(t)
    return vertices, triangles


def extract_vc_layout_preview(vc_text: str) -> dict[str, Any] | None:
    """Extract tables, 3D obstacles, and terrain mesh from a saved PVsyst VCx PVObject_Ombrage=pvShading block."""
    clean = vc_text.lstrip("\ufeff")
    try:
        blocks = objects(clean, include_private=False)
    except ValueError:
        return None

    orient_meta: dict[int, dict[str, Any]] = {}
    for blk in blocks:
        if blk.object_type in ("pvOrient", "pvDomeOrient") and "NoOrient" in blk.fields:
            oid = _parse_int_val(blk.fields["NoOrient"][0][1], 1)
            orient_meta[oid] = {
                "orientation_id": oid,
                "field_type": blk.fields.get("FieldType", [[0, "FixedPlane"]])[0][1],
                "tilt_deg": _parse_float_val(blk.fields.get("FieldTilt", [[0, "0"]])[0][1]),
                "azimuth_deg": _parse_float_val(blk.fields.get("FieldAzim", [[0, "0"]])[0][1]),
                "comment": blk.fields.get("Comment", [[0, ""]])[0][1],
            }

    tables: list[dict[str, Any]] = []
    obstacles: list[dict[str, Any]] = []
    terrain_mesh: dict[str, Any] | None = None

    for blk in blocks:
        if not blk.path.startswith("PVObject_Ombrage/"):
            continue
        otype = blk.object_type
        one = lambda key, default=None: blk.fields[key][0][1] if blk.fields.get(key) else default
        origin = _parse_vec(one("DOrigine"))
        if origin is None:
            continue

        if otype in ("pvShdArrayTable", "pvShdArrayShed"):
            tilt = _parse_float_val(one("DInclin"), 0.0)
            azim = _parse_float_val(one("DAzimut"), 0.0)
            slope = _parse_float_val(one("BaseSlope"), 0.0)
            long_s = _parse_float_val(one("LongShed"), 0.0)
            larg_s = _parse_float_val(one("LargShed"), 0.0)
            if long_s <= 0 or larg_s <= 0:
                continue
            oid = _parse_int_val(one("NoOrient"), 1)
            n_elems = max(1, _parse_int_val(one("NElemChamps"), 1))
            pitch_ns = _parse_float_val(one("PitchNS"), 0.0)
            type_orig = _parse_int_val(one("TypeOrigine"), 2)
            n_rows = max(1, _parse_int_val(one("NModInHeight"), 1))
            n_cols = max(1, _parse_int_val(one("NModInWidth"), 1))
            name = one("Comment") or f"Table_{len(tables) + 1}"
            az_rad = math.radians(azim)
            step_x, step_y = math.sin(az_rad), math.cos(az_rad)
            for idx_e in range(n_elems):
                ox = origin[0] + idx_e * pitch_ns * step_x
                oy = origin[1] + idx_e * pitch_ns * step_y
                tables.append({
                    "kind": "fixed",
                    "name": name if n_elems == 1 else f"{name}_R{idx_e + 1}",
                    "origin_m": [round(ox, 6), round(oy, 6), origin[2]],
                    "long_shed_m": long_s, "larg_shed_m": larg_s,
                    "tilt_deg": tilt, "azimuth_deg": azim, "base_slope_deg": slope,
                    "orientation_id": oid, "type_origine": type_orig,
                    "module_rows": n_rows, "module_columns": n_cols, "module_count": n_rows * n_cols,
                })
        elif otype == "pvShdArrayTracking":
            axis_tilt = _parse_float_val(one("DInclin"), 0.0)
            axis_azim = _parse_float_val(one("DAzimut"), 0.0)
            long_s = _parse_float_val(one("LongShed"), 0.0)
            larg_s = _parse_float_val(one("LargShed"), 0.0)
            if long_s <= 0 or larg_s <= 0:
                continue
            oid = _parse_int_val(one("NoOrient"), 1)
            n_elems = max(1, _parse_int_val(one("NElemChamps"), 1))
            pitch_ew = _parse_float_val(one("PitchEW"), 0.0)
            type_orig = _parse_int_val(one("TypeOrigine"), 7)
            axis_orig = _parse_int_val(one("AxisOrig"), 2)
            n_rows = max(1, _parse_int_val(one("NModInHeight"), 1))
            n_cols = max(1, _parse_int_val(one("NModInWidth"), 1))
            name = one("Comment") or f"Tracker_{len(tables) + 1}"
            az_rad = math.radians(axis_azim)
            step_x, step_y = math.cos(az_rad), -math.sin(az_rad)
            for idx_e in range(n_elems):
                ox = origin[0] + idx_e * pitch_ew * step_x
                oy = origin[1] + idx_e * pitch_ew * step_y
                tables.append({
                    "kind": "tracker",
                    "name": name if n_elems == 1 else f"{name}_T{idx_e + 1}",
                    "origin_m": [round(ox, 6), round(oy, 6), origin[2]],
                    "long_shed_m": long_s, "larg_shed_m": larg_s,
                    "axis_tilt_deg": axis_tilt, "axis_azimuth_deg": axis_azim,
                    "orientation_id": oid, "type_origine": 7 if (type_orig == 7 or axis_orig == 2) else type_orig,
                    "module_rows": n_rows, "module_columns": n_cols, "module_count": n_rows * n_cols,
                })
        elif otype == "pvShdArrayDome":
            tilt = _parse_float_val(one("DInclin"), 10.0)
            azim = _parse_float_val(one("DAzimut"), 90.0)
            slope = _parse_float_val(one("BaseSlope"), 0.0)
            long_s = _parse_float_val(one("LongShed"), 0.0)
            larg_s = _parse_float_val(one("LargShed"), 0.0)
            if long_s <= 0 or larg_s <= 0:
                continue
            oid = _parse_int_val(one("NoOrient"), 1)
            n_elems = max(1, _parse_int_val(one("NElemChamps"), 1))
            pitch_ns = _parse_float_val(one("PitchNS"), 0.0)
            top_sp = _parse_float_val(one("TopSpacing"), 0.2)
            is_main = (one("IsMainField") or "True").strip() == "True"
            n_rows = max(1, _parse_int_val(one("NModInHeight"), 1))
            n_cols = max(1, _parse_int_val(one("NModInWidth"), 1))
            name = one("Comment") or f"Dome_{len(tables) + 1}"
            az_rad = math.radians(azim)
            step_x, step_y = math.sin(az_rad), math.cos(az_rad)
            for idx_e in range(n_elems):
                ox = origin[0] + idx_e * pitch_ns * step_x
                oy = origin[1] + idx_e * pitch_ns * step_y
                tables.append({
                    "kind": "dome", "dome_face": "front" if is_main else "back",
                    "name": name if n_elems == 1 else f"{name}_D{idx_e + 1}",
                    "origin_m": [round(ox, 6), round(oy, 6), origin[2]],
                    "long_shed_m": long_s, "larg_shed_m": larg_s,
                    "tilt_deg": tilt, "azimuth_deg": azim, "base_slope_deg": slope,
                    "top_spacing_m": top_sp, "orientation_id": oid,
                    "module_rows": n_rows, "module_columns": n_cols, "module_count": n_rows * n_cols,
                })
        elif otype == "pvShdGenObject":
            verts, tris = _parse_points_and_faces(blk, origin)
            if verts and tris:
                obstacles.append({
                    "kind": "mesh",
                    "name": one("Comment") or f"Obstacle_{len(obstacles) + 1}",
                    "vertices": verts,
                    "triangles": tris,
                })
        elif otype == "pvShdElemObject" and one("ObjetElemType") == "oeTree":
            raw_p = [p.strip() for p in (one("Params") or "").split(",") if p.strip()]
            top_h = _parse_float_val(raw_p[0], 6.0) if len(raw_p) >= 1 else 6.0
            crown_d = _parse_float_val(raw_p[4], 4.0) if len(raw_p) >= 5 else 4.0
            obstacles.append({
                "kind": "tree",
                "name": one("Comment") or f"Tree_{len(obstacles) + 1}",
                "origin_m": origin,
                "top_height_m": top_h,
                "crown_diameter_m": crown_d,
            })
        elif otype == "pvShdGround":
            verts, tris = _parse_points_and_faces(blk, origin)
            if verts:
                terrain_mesh = {
                    "name": one("Comment") or "Terrain",
                    "vertices": verts,
                    "triangles": tris,
                }

    if not tables and not obstacles and terrain_mesh is None:
        return None
    out: dict[str, Any] = {
        "source": "vc_shading",
        "tables": tables,
        "obstacles": obstacles,
        "orientations": orient_meta,
    }
    if terrain_mesh is not None:
        out["terrain_mesh"] = terrain_mesh
    return out


def _rotate_local_to_world(ox: float, oy: float, du: float, dv: float, azim_deg: float) -> tuple[float, float]:
    rad = math.radians(azim_deg)
    c, s = math.cos(rad), math.sin(rad)
    return ox + du * c + dv * s, oy - du * s + dv * c


def _convex_hull_2d(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    unique = sorted(set((round(x, 6), round(y, 6)) for x, y in pts))
    if len(unique) <= 2:
        return unique
    cross = lambda o, a, b: (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower: list[tuple[float, float]] = []
    for p in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _table_corners_3d(tbl: dict[str, Any]) -> list[tuple[float, float, float]]:
    if poly := tbl.get("footprint") or tbl.get("polygon"):
        z0 = float((tbl.get("origin_m") or [0, 0, 0])[2]) if len(tbl.get("origin_m") or ()) >= 3 else 0.0
        return [(float(p[0]), float(p[1]), float(p[2]) if len(p) >= 3 else z0) for p in poly]

    orig = [float(v) for v in (tbl.get("origin_m") or [0.0, 0.0, 0.0])]
    ox, oy, oz = orig[0], orig[1], orig[2] if len(orig) >= 3 else 0.0
    long_s = float(tbl.get("long_shed_m") or 4.0)
    larg_s = float(tbl.get("larg_shed_m") or 2.0)
    kind = tbl.get("kind") or ("tracker" if "axis_azimuth_deg" in tbl else "fixed")

    if kind == "tracker":
        az = float(tbl.get("axis_azimuth_deg", tbl.get("azimuth_deg", 0.0)))
        axis_tilt = float(tbl.get("axis_tilt_deg", 0.0))
        tan_t = math.tan(math.radians(max(-45.0, min(45.0, axis_tilt))))
        type_orig = int(tbl.get("type_origine", 7))
        v0, v1 = (-0.5 * larg_s, 0.5 * larg_s) if type_orig == 7 else (0.0, larg_s)
        local = [(-0.5 * long_s, v0), (0.5 * long_s, v0), (0.5 * long_s, v1), (-0.5 * long_s, v1)]
        return [(*_rotate_local_to_world(ox, oy, du, dv, az), oz + dv * tan_t) for du, dv in local]

    tilt = float(tbl.get("tilt_deg", 20.0))
    az = float(tbl.get("azimuth_deg", 0.0))
    slope = float(tbl.get("base_slope_deg", 0.0))
    tan_slope = math.tan(math.radians(max(-45.0, min(45.0, slope))))
    proj_v = max(0.05, larg_s * math.cos(math.radians(tilt)))
    rise_z = larg_s * math.sin(math.radians(tilt))

    if kind == "dome":
        top_sp = float(tbl.get("top_spacing_m", 0.2))
        is_back = tbl.get("dome_face") == "back"
        v_lo = (proj_v + top_sp) if is_back else 0.0
        v_hi = (2.0 * proj_v + top_sp) if is_back else proj_v
        z_lo, z_hi = (oz + rise_z, oz) if is_back else (oz, oz + rise_z)
        local = [(-0.5 * long_s, v_lo, z_lo), (0.5 * long_s, v_lo, z_lo), (0.5 * long_s, v_hi, z_hi), (-0.5 * long_s, v_hi, z_hi)]
        return [(*_rotate_local_to_world(ox, oy, du, dv, az), z + du * tan_slope) for du, dv, z in local]

    type_orig = int(tbl.get("type_origine", 2))
    u0, u1 = (0.0, long_s) if type_orig == 1 else (-0.5 * long_s, 0.5 * long_s)
    v0, v1 = (-0.5 * proj_v, 0.5 * proj_v) if type_orig == 7 else (0.0, proj_v)
    local = [(u0, v0, oz), (u1, v0, oz), (u1, v1, oz + rise_z), (u0, v1, oz + rise_z)]
    return [(*_rotate_local_to_world(ox, oy, du, dv, az), z + du * tan_slope) for du, dv, z in local]


def _normalize_zones(raw_zones: Any) -> list[dict[str, Any]]:
    zones: list[dict[str, Any]] = []
    if not isinstance(raw_zones, list):
        return zones
    for idx, item in enumerate(raw_zones, start=1):
        if isinstance(item, dict) and isinstance(item.get("polygon"), list) and len(item["polygon"]) >= 3:
            pts = [(float(p[0]), float(p[1])) for p in item["polygon"] if isinstance(p, (list, tuple)) and len(p) >= 2]
            if len(pts) >= 3:
                zones.append({"name": str(item.get("name") or f"Exclusion_{idx}"), "polygon": pts})
        elif isinstance(item, list) and len(item) >= 3 and all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in item):
            zones.append({"name": f"Exclusion_{idx}", "polygon": [(float(p[0]), float(p[1])) for p in item]})
    return zones


def _normalize_obstacles_for_plot(raw_obs: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_obs, list) or not raw_obs:
        return []
    already_mesh_or_tree = [o for o in raw_obs if isinstance(o, dict) and ("vertices" in o or o.get("kind") == "tree")]
    to_author = [o for o in raw_obs if isinstance(o, dict) and o not in already_mesh_or_tree]
    authored = obstacle_profiles(to_author) if to_author else []
    return [*already_mesh_or_tree, *authored]


def resolve_report_layout(
    *,
    primary_scenario: dict[str, Any],
    all_scenarios: list[dict[str, Any]],
    top_layout_preview: dict[str, Any] | None,
    top_include_3d: bool,
    store: Any,
    workspace: Path | None,
) -> tuple[dict[str, Any] | None, bool, str]:
    """Merge explicit layout_preview and optional VCx pvShading auto-extraction for the primary scenario."""
    include_3d = bool(top_include_3d or primary_scenario.get("include_3d_layout"))
    explicit_lp = primary_scenario.get("layout_preview")
    if explicit_lp is None and top_layout_preview is not None:
        explicit_lp = top_layout_preview

    vc_lp = None
    proj, var = primary_scenario.get("project"), primary_scenario.get("variant")
    if proj and var:
        try:
            if store is not None:
                vc_bytes = store._read(store._path(proj, var))
            elif workspace is not None:
                vc_bytes = project_path(workspace, proj, var).read_bytes()
            else:
                vc_bytes = None
            if vc_bytes is not None:
                vc_lp = extract_vc_layout_preview(vc_bytes.decode("utf-8-sig"))
        except (ValueError, OSError, UnicodeError):
            vc_lp = None

    if explicit_lp is None and vc_lp is None:
        return None, include_3d, ""
    if explicit_lp is not None and not isinstance(explicit_lp, dict):
        raise ValueError("layout_preview must be an object")

    merged: dict[str, Any] = dict(vc_lp or {})
    if explicit_lp:
        if ("terrain" in explicit_lp or "terrain_points" in explicit_lp) and "terrain_mesh" not in explicit_lp:
            merged.pop("terrain_mesh", None)
        for key, val in explicit_lp.items():
            if val is not None:
                merged[key] = val
    merged["exclusion_zones"] = _normalize_zones(merged.get("exclusion_zones"))
    merged["obstacles"] = _normalize_obstacles_for_plot(merged.get("obstacles"))

    if merged.get("terrain_mesh") is None and isinstance(merged.get("terrain"), dict):
        try:
            mesh = build_terrain_mesh(merged["terrain"], workspace=workspace)
            merged["terrain_mesh"] = {
                "name": mesh.name,
                "vertices": [[v[0], v[1], v[2]] for v in mesh.vertices],
                "triangles": [[t[0], t[1], t[2]] for t in mesh.triangles],
            }
        except (ValueError, OSError):
            if pts := merged["terrain"].get("points"):
                merged.setdefault("terrain_points", pts)

    suffix = f" ({primary_scenario['label']})" if len(all_scenarios) > 1 else ""
    return merged, include_3d, suffix


def _add_poly_collection_2d(
    fig: Any,
    go: Any,
    polys: list[list[tuple[float, float]]],
    *,
    name: str,
    line_color: str,
    fill_color: str,
    hover_points: list[tuple[float, float, str]] | None = None,
    dash: str | None = None,
    line_width: float = 1.2,
) -> None:
    xs: list[float | None] = []
    ys: list[float | None] = []
    for poly in polys:
        if len(poly) < 3:
            continue
        for x, y in poly:
            xs.append(x)
            ys.append(y)
        xs.extend([poly[0][0], None])
        ys.extend([poly[0][1], None])
    if not xs:
        return
    fig.add_trace(go.Scatter(
        name=name, x=xs, y=ys, mode="lines", fill="toself", fillcolor=fill_color,
        line=dict(color=line_color, width=line_width, **({"dash": dash} if dash else {})),
        hoverinfo="skip" if hover_points else "name",
    ))
    if hover_points:
        fig.add_trace(go.Scatter(
            name=name, x=[p[0] for p in hover_points], y=[p[1] for p in hover_points],
            text=[p[2] for p in hover_points], mode="markers",
            marker=dict(size=4, color=line_color, opacity=0.01),
            hovertemplate="%{text}<extra></extra>", showlegend=False,
        ))


def _append_quads_mesh_3d(
    fig: Any, go: Any, quads: list[list[tuple[float, float, float]]], *, name: str, color: str, opacity: float = 0.92,
) -> None:
    vx, vy, vz, ti, tj, tk = [], [], [], [], [], []
    for corners in quads:
        if len(corners) < 3:
            continue
        base = len(vx)
        for x, y, z in corners:
            vx.append(x); vy.append(y); vz.append(z)
        for m in range(1, len(corners) - 1):
            ti.append(base); tj.append(base + m); tk.append(base + m + 1)
    if ti:
        fig.add_trace(go.Mesh3d(
            name=name, x=vx, y=vy, z=vz, i=ti, j=tj, k=tk,
            color=color, opacity=opacity, flatshading=True, showlegend=True,
        ))


def build_layout_figures(
    layout: dict[str, Any] | None, *, language: str, include_3d: bool, palette: list[str],
) -> tuple[Any, Any]:
    """Build interactive Plotly 2D plan-view and optional 3D terrain/array figures."""
    if not layout:
        return None, None
    import plotly.graph_objects as go

    lbl = _LAYOUT_I18N[language]
    site_poly = layout.get("site_polygon") or []
    exclusions = _normalize_zones(layout.get("exclusion_zones"))
    tables = layout.get("tables") or []
    obstacles = _normalize_obstacles_for_plot(layout.get("obstacles"))
    terrain_mesh = layout.get("terrain_mesh")
    terrain_pts = layout.get("terrain_points") or []

    has_2d_content = bool(len(site_poly) >= 3 or exclusions or tables or obstacles)
    has_terrain = bool((terrain_mesh and terrain_mesh.get("vertices")) or len(terrain_pts) >= 3)
    all_table_corners = [_table_corners_3d(tbl) for tbl in tables]
    z_vals = [c[2] for corners in all_table_corners for c in corners]
    has_z_relief = len(z_vals) >= 2 and (max(z_vals) - min(z_vals) > 1e-3)
    has_3d_site = any(isinstance(p, (list, tuple)) and len(p) >= 3 for p in site_poly)
    want_3d = bool((has_2d_content or has_terrain) and (include_3d or has_terrain or bool(obstacles) or has_z_relief or has_3d_site))

    orient_ids = sorted({int(t.get("orientation_id", 1)) for t in tables if (t.get("kind") or "fixed") == "fixed" and "axis_azimuth_deg" not in t})
    multi_orient = len(orient_ids) >= 2

    def _table_group(tbl: dict[str, Any]) -> tuple[str, str]:
        kind = tbl.get("kind") or ("tracker" if "axis_azimuth_deg" in tbl else "fixed")
        if kind == "tracker":
            return "tracker", lbl["tracker"]
        if kind == "dome":
            is_back = tbl.get("dome_face") == "back"
            return ("dome_back", lbl["dome_back"]) if is_back else ("dome_front", lbl["dome_front"])
        oid = int(tbl.get("orientation_id", 1))
        if multi_orient:
            tilt = float(tbl.get("tilt_deg", 0.0))
            az = float(tbl.get("azimuth_deg", 0.0))
            return f"orient_{oid}", lbl["orient_fmt"].format(id=oid, tilt=round(tilt, 1), az=round(az, 1))
        return "fixed", lbl["fixed"]

    group_colors: dict[str, str] = {"tracker": "#0284c7", "dome_front": "#2563eb", "dome_back": "#0d9488", "fixed": "#2563eb"}
    group_fills: dict[str, str] = {"tracker": "rgba(2, 132, 199, 0.35)", "dome_front": "rgba(37, 99, 235, 0.38)", "dome_back": "rgba(13, 148, 136, 0.38)", "fixed": "rgba(37, 99, 235, 0.35)"}
    grouped_2d: dict[str, tuple[str, list[list[tuple[float, float]]], list[tuple[float, float, str]]]] = {}
    grouped_3d: dict[str, tuple[str, list[list[tuple[float, float, float]]]]] = {}

    for tbl, corners in zip(tables, all_table_corners):
        gkey, gname = _table_group(tbl)
        if gkey.startswith("orient_") and gkey not in group_colors:
            idx_c = (int(gkey.split("_")[1]) - 1) % len(palette)
            c_hex = palette[idx_c]
            r, g, b = int(c_hex[1:3], 16), int(c_hex[3:5], 16), int(c_hex[5:7], 16)
            group_colors[gkey] = c_hex
            group_fills[gkey] = f"rgba({r}, {g}, {b}, 0.38)"
        cx = sum(c[0] for c in corners) / len(corners)
        cy = sum(c[1] for c in corners) / len(corners)
        tilt_v = tbl.get("axis_tilt_deg") if tbl.get("kind") == "tracker" or "axis_azimuth_deg" in tbl else tbl.get("tilt_deg", 0.0)
        az_v = tbl.get("axis_azimuth_deg") if tbl.get("kind") == "tracker" or "axis_azimuth_deg" in tbl else tbl.get("azimuth_deg", 0.0)
        htxt = f"<b>{tbl.get('name', 'PV')}</b><br>{lbl['hover_tilt']}: {tilt_v}° | {lbl['hover_az']}: {az_v}°<br>{lbl['hover_mod']}: {tbl.get('module_count', '-')}"
        if multi_orient:
            htxt += f"<br>{lbl['hover_orient']}: #{tbl.get('orientation_id', 1)}"
        grouped_2d.setdefault(gkey, (gname, [], []))[1].append([(c[0], c[1]) for c in corners])
        grouped_2d[gkey][2].append((cx, cy, htxt))
        grouped_3d.setdefault(gkey, (gname, []))[1].append(corners)

    fig_2d = None
    if has_2d_content:
        fig_2d = go.Figure()
        if len(site_poly) >= 3:
            _add_poly_collection_2d(
                fig_2d, go, [[(float(p[0]), float(p[1])) for p in site_poly]],
                name=lbl["boundary"], line_color="#1d4ed8", fill_color="rgba(37, 99, 235, 0.05)",
                dash="dash", line_width=2.2,
            )
        if exclusions:
            _add_poly_collection_2d(
                fig_2d, go, [z["polygon"] for z in exclusions],
                name=lbl["exclusion"], line_color="#dc2626", fill_color="rgba(220, 38, 38, 0.14)",
                dash="dot", line_width=1.6,
            )
        for gkey, (gname, polys, hovers) in grouped_2d.items():
            _add_poly_collection_2d(
                fig_2d, go, polys, name=gname, line_color=group_colors[gkey],
                fill_color=group_fills[gkey], hover_points=hovers, line_width=1.0,
            )
        mesh_obs_polys, mesh_obs_hovers = [], []
        tree_polys, tree_hovers = [], []
        for obs in obstacles:
            if obs.get("kind") == "tree":
                ox, oy = float(obs["origin_m"][0]), float(obs["origin_m"][1])
                rad_c = 0.5 * float(obs.get("crown_diameter_m", 4.0))
                tree_polys.append([(ox + rad_c * math.cos(2 * math.pi * k / 16), oy + rad_c * math.sin(2 * math.pi * k / 16)) for k in range(16)])
                tree_hovers.append((ox, oy, f"<b>{obs.get('name', 'Tree')}</b><br>H={obs.get('top_height_m', '-')} m"))
            elif verts := obs.get("vertices"):
                hull = _convex_hull_2d([(float(v[0]), float(v[1])) for v in verts])
                if len(hull) >= 3:
                    mesh_obs_polys.append(hull)
                    cx = sum(p[0] for p in hull) / len(hull)
                    cy = sum(p[1] for p in hull) / len(hull)
                    z_max = max(float(v[2]) for v in verts)
                    mesh_obs_hovers.append((cx, cy, f"<b>{obs.get('name', 'Obstacle')}</b><br>Z_max={z_max:.2f} m"))
        if mesh_obs_polys:
            _add_poly_collection_2d(
                fig_2d, go, mesh_obs_polys, name=lbl["obstacle"], line_color="#475569",
                fill_color="rgba(100, 116, 139, 0.38)", hover_points=mesh_obs_hovers, line_width=1.4,
            )
        if tree_polys:
            _add_poly_collection_2d(
                fig_2d, go, tree_polys, name=lbl["tree"], line_color="#15803d",
                fill_color="rgba(22, 163, 74, 0.35)", hover_points=tree_hovers, line_width=1.3,
            )
        fig_2d.update_layout(
            template="plotly_white", height=500, margin=dict(l=55, r=30, t=35, b=50),
            xaxis_title=lbl["x"], yaxis_title=lbl["y"],
            yaxis=dict(scaleanchor="x", scaleratio=1),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1.0),
        )

    fig_3d = None
    if want_3d:
        fig_3d = go.Figure()
        z_floor = 0.0
        if terrain_mesh and terrain_mesh.get("vertices"):
            tv = terrain_mesh["vertices"]
            z_floor = min(float(v[2]) for v in tv)
            t_tris = terrain_mesh.get("triangles") or []
            if not t_tris and len(tv) >= 3:
                try:
                    t_tris = triangulate_2d([(float(v[0]), float(v[1]), float(v[2])) for v in tv])
                except ValueError:
                    t_tris = []
            if t_tris:
                fig_3d.add_trace(go.Mesh3d(
                    name=lbl["terrain"], x=[v[0] for v in tv], y=[v[1] for v in tv], z=[v[2] for v in tv],
                    i=[t[0] for t in t_tris], j=[t[1] for t in t_tris], k=[t[2] for t in t_tris],
                    intensity=[v[2] for v in tv], colorscale="Earth", opacity=0.72,
                    colorbar=dict(title=lbl["z"], len=0.65),
                ))
            else:
                fig_3d.add_trace(go.Scatter3d(
                    name=lbl["terrain"], x=[v[0] for v in tv], y=[v[1] for v in tv], z=[v[2] for v in tv],
                    mode="markers", marker=dict(size=3, color=[v[2] for v in tv], colorscale="Earth"),
                ))
        elif len(terrain_pts) >= 3:
            pts3 = [(float(p[0]), float(p[1]), float(p[2])) for p in terrain_pts]
            z_floor = min(p[2] for p in pts3)
            try:
                t_tris = triangulate_2d(pts3)
                fig_3d.add_trace(go.Mesh3d(
                    name=lbl["terrain"], x=[p[0] for p in pts3], y=[p[1] for p in pts3], z=[p[2] for p in pts3],
                    i=[t[0] for t in t_tris], j=[t[1] for t in t_tris], k=[t[2] for t in t_tris],
                    intensity=[p[2] for p in pts3], colorscale="Earth", opacity=0.72,
                    colorbar=dict(title=lbl["z"], len=0.65),
                ))
            except ValueError:
                fig_3d.add_trace(go.Scatter3d(
                    name=lbl["terrain"], x=[p[0] for p in pts3], y=[p[1] for p in pts3], z=[p[2] for p in pts3],
                    mode="markers", marker=dict(size=3.5, color=[p[2] for p in pts3], colorscale="Earth"),
                ))
        elif z_vals:
            z_floor = min(z_vals)

        if len(site_poly) >= 3:
            sp_closed = [*site_poly, site_poly[0]]
            fig_3d.add_trace(go.Scatter3d(
                name=lbl["boundary"],
                x=[float(p[0]) for p in sp_closed], y=[float(p[1]) for p in sp_closed],
                z=[float(p[2]) if len(p) >= 3 else z_floor for p in sp_closed],
                mode="lines", line=dict(color="#1d4ed8", width=4),
            ))
        if exclusions:
            ex_x, ex_y, ex_z = [], [], []
            for zitem in exclusions:
                pts_z = zitem["polygon"]
                for x, y in [*pts_z, pts_z[0]]:
                    ex_x.append(x); ex_y.append(y); ex_z.append(z_floor)
                ex_x.append(None); ex_y.append(None); ex_z.append(None)
            fig_3d.add_trace(go.Scatter3d(
                name=lbl["exclusion"], x=ex_x, y=ex_y, z=ex_z,
                mode="lines", line=dict(color="#dc2626", width=3, dash="dot"),
            ))
        for gkey, (gname, quads) in grouped_3d.items():
            _append_quads_mesh_3d(fig_3d, go, quads, name=gname, color=group_colors[gkey])

        obs_vx, obs_vy, obs_vz, obs_i, obs_j, obs_k = [], [], [], [], [], []
        tree_cx, tree_cy, tree_cz = [], [], []
        for obs in obstacles:
            if obs.get("kind") == "tree":
                ox, oy, oz = [float(v) for v in obs["origin_m"][:3]]
                tree_cx.append(ox); tree_cy.append(oy); tree_cz.append(oz + 0.7 * float(obs.get("top_height_m", 6.0)))
            elif (verts := obs.get("vertices")) and (tris := obs.get("triangles")):
                base = len(obs_vx)
                for v in verts:
                    obs_vx.append(float(v[0])); obs_vy.append(float(v[1])); obs_vz.append(float(v[2]))
                for t in tris:
                    obs_i.append(base + int(t[0])); obs_j.append(base + int(t[1])); obs_k.append(base + int(t[2]))
        if obs_i:
            fig_3d.add_trace(go.Mesh3d(
                name=lbl["obstacle"], x=obs_vx, y=obs_vy, z=obs_vz, i=obs_i, j=obs_j, k=obs_k,
                color="#64748b", opacity=0.82, flatshading=True, showlegend=True,
            ))
        if tree_cx:
            fig_3d.add_trace(go.Scatter3d(
                name=lbl["tree"], x=tree_cx, y=tree_cy, z=tree_cz,
                mode="markers", marker=dict(size=8, color="#16a34a", opacity=0.85),
            ))
        fig_3d.update_layout(
            template="plotly_white", height=540, margin=dict(l=15, r=15, t=35, b=15),
            scene=dict(xaxis_title=lbl["x"], yaxis_title=lbl["y"], zaxis_title=lbl["z"], aspectmode="data"),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1.0),
        )
    return fig_2d, fig_3d

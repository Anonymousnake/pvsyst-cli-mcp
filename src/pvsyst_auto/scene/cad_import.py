"""Pure-stdlib .pvc (COLLADA 1.4.1 / PVcase) parser, 3D shading builder, and CAD workflow router."""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from pvsyst_auto.project.paths import safe_filename
from pvsyst_auto.scene.author import MAX_OBSTACLES
from pvsyst_auto.scene.dxf_import import (
    downsample_xyz_points,
    normalize_dxf_layers,
    parse_ascii_dxf,
    read_bounded_cad_bytes,
)
from pvsyst_auto.scene.exclusions import MAX_EXCLUSION_ZONES
from pvsyst_auto.scene.pvc_clustering import (
    MAX_PVC_ORIENTATION_TOLERANCE_DEG,
    MIN_PVC_ORIENTATION_TOLERANCE_DEG,
    circular_azimuth_diff_deg,
    cluster_fixed_pvc_orientations,
    estimate_pvc_pitch as _estimate_pvc_pitch,
    filter_pvc_subset,
    normalize_azimuth_deg,
)
from pvsyst_auto.scene.reader import build_ground_3d_shading
from pvsyst_auto.scene.terrain import build_terrain_mesh
from pvsyst_auto.scene.tracker_layout import build_tracker_3d_shading
from pvsyst_auto.validation import SYNTH_VERSION, finite_number as _finite
from pvsyst_auto.validation import is_finite_number as _is_number

COLLADA_141_NS = "http://www.collada.org/2005/11/COLLADASchema"
MAX_CAD_TABLES = 2000
MAX_PVC_NODE_DEPTH = 64
MAX_PVC_INSTANCES = 50_000
MAX_PVC_TERRAIN_POINTS = 500_000
_EPS = 1e-9
_TERRAIN_RE = re.compile(r"(?:topo|terrain|dtm|dem|contour|elev|ground|grade|surf)", re.I)


def _sub(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(v: tuple[float, float, float]) -> tuple[float, float, float] | None:
    length = math.sqrt(_dot(v, v))
    if not math.isfinite(length) or length <= _EPS:
        return None
    return (v[0] / length, v[1] / length, v[2] / length)


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _mat_identity() -> list[list[float]]:
    return [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def _mat_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _mat_rotate(ax: float, ay: float, az: float, angle_deg: float) -> list[list[float]]:
    axis = _norm((ax, ay, az))
    if axis is None:
        return _mat_identity()
    ux, uy, uz = axis
    rad = math.radians(angle_deg)
    c, s = math.cos(rad), math.sin(rad)
    t = 1.0 - c
    return [
        [t * ux * ux + c, t * ux * uy - s * uz, t * ux * uz + s * uy, 0.0],
        [t * ux * uy + s * uz, t * uy * uy + c, t * uy * uz - s * ux, 0.0],
        [t * ux * uz - s * uy, t * uy * uz + s * ux, t * uz * uz + c, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _mat_apply(m: list[list[float]], pt: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        m[0][0] * pt[0] + m[0][1] * pt[1] + m[0][2] * pt[2] + m[0][3],
        m[1][0] * pt[0] + m[1][1] * pt[1] + m[1][2] * pt[2] + m[1][3],
        m[2][0] * pt[0] + m[2][1] * pt[1] + m[2][2] * pt[2] + m[2][3],
    )


def _infer_grid_counts(span_u: float, span_v: float, mw: float, ml: float, sx: float, sy: float, *, tracker: bool) -> tuple[str, int, int]:
    """Choose portrait/landscape yielding best integer fit along shed/tracker axes."""
    best: tuple[float, int, str, int, int] | None = None
    for orient_key in ("moLandscape", "moPortrait"):
        if tracker:
            chord_m, along_m = (mw, ml) if orient_key == "moLandscape" else (ml, mw)
            rows_span, cols_span = span_u, span_v
        else:
            cell_w, cell_h = (ml, mw) if orient_key == "moLandscape" else (mw, ml)
            chord_m, along_m = cell_h, cell_w
            cols_span, rows_span = span_u, span_v
        raw_r = (rows_span + sy) / (chord_m + sy) if chord_m + sy > 0 else 1.0
        raw_c = (cols_span + sx) / (along_m + sx) if along_m + sx > 0 else 1.0
        n_r = max(1, round(raw_r)) if abs(raw_r - round(raw_r)) <= 0.12 else max(1, int(math.floor(raw_r + 1e-6)))
        n_c = max(1, round(raw_c)) if abs(raw_c - round(raw_c)) <= 0.12 else max(1, int(math.floor(raw_c + 1e-6)))
        score = abs(raw_r - n_r) + (0.25 * abs(raw_c - n_c) if tracker else abs(raw_c - n_c))
        cand = (score, 0 if orient_key == "moLandscape" else 1, orient_key, n_r, n_c)
        if best is None or cand < best:
            best = cand
    assert best is not None
    return best[2], best[3], best[4]


def _parse_pvc_geometries(root: ET.Element, unit_meter: float) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    ns = {"c": COLLADA_141_NS}
    materials = {
        mat.get("id", ""): mat.get("name", mat.get("id", ""))
        for mat in root.findall(".//c:library_materials/c:material", ns)
        if mat.get("id")
    }
    geoms: dict[str, dict[str, Any]] = {}
    for geom in root.findall("c:library_geometries/c:geometry", ns):
        gid = geom.get("id", "")
        mesh = geom.find("c:mesh", ns)
        if not gid or mesh is None:
            continue
        sources: dict[str, list[float]] = {}
        for src in mesh.findall("c:source", ns):
            sid = src.get("id", "")
            fa = src.find("c:float_array", ns)
            if sid and fa is not None and fa.text:
                sources[sid] = [_finite(float(v) * unit_meter, f"{gid}.float_array") for v in fa.text.split()]

        pos_id: str | None = None
        verts_el = mesh.find("c:vertices", ns)
        if verts_el is not None:
            for inp in verts_el.findall("c:input", ns):
                if inp.get("semantic") == "POSITION":
                    pos_id = (inp.get("source") or "").lstrip("#")
                    break
        raw_coords = sources.get(pos_id or "") or next(iter(sources.values()), [])
        if len(raw_coords) < 9 or len(raw_coords) % 3 != 0:
            raise ValueError(f"Geometry {gid!r} lacks complete 3D vertex triples")
        points = [(raw_coords[i], raw_coords[i + 1], raw_coords[i + 2]) for i in range(0, len(raw_coords), 3)]

        triangles: list[list[int]] = []
        tri_mats: list[str] = []
        for prim_tag in ("triangles", "polylist"):
            for prim in mesh.findall(f"c:{prim_tag}", ns):
                if mat_sym := prim.get("material", ""):
                    tri_mats.append(mat_sym)
                inputs = prim.findall("c:input", ns)
                offsets = [int(i.get("offset", "0")) for i in inputs]
                if any(o < 0 for o in offsets):
                    raise ValueError(f"Geometry {gid!r} contains negative input offset")
                stride = max(offsets, default=0) + 1
                v_off = next((int(i.get("offset", "0")) for i in inputs if i.get("semantic") in ("VERTEX", "POSITION")), 0)
                p_el = prim.find("c:p", ns)
                if p_el is None or not p_el.text:
                    continue
                idx_vals = [int(v) for v in p_el.text.split()]
                if prim_tag == "triangles":
                    step = 3 * stride
                    for j in range(0, len(idx_vals) - step + 1, step):
                        t = [idx_vals[j + k * stride + v_off] for k in range(3)]
                        if len(set(t)) == 3 and all(0 <= vi < len(points) for vi in t):
                            triangles.append(t)
                elif (vcount_el := prim.find("c:vcount", ns)) is not None and vcount_el.text:
                    pos = 0
                    for vc_str in vcount_el.text.split():
                        vc = int(vc_str)
                        if vc < 3 or pos + vc * stride > len(idx_vals):
                            raise ValueError(f"Geometry {gid!r} polylist index count is truncated")
                        poly = [idx_vals[pos + k * stride + v_off] for k in range(vc)]
                        pos += vc * stride
                        for k in range(1, vc - 1):
                            t = [poly[0], poly[k], poly[k + 1]]
                            if len(set(t)) == 3 and all(0 <= vi < len(points) for vi in t):
                                triangles.append(t)

        fp = mesh.find(".//c:frame_parameters", ns)
        tp = mesh.find(".//c:tracker_parameters", ns)
        kind = "fixed" if fp is not None else ("tracker" if tp is not None else "other")
        params: dict[str, Any] = {}
        if (param_el := fp if fp is not None else tp) is not None:
            for ch in param_el:
                tag = ch.tag.split("}")[-1]
                if tag == "axis_vertices":
                    params["axis_vertices"] = [
                        (vals[0], vals[1], vals[2])
                        for fa in ch.findall("c:float_array", ns)
                        if fa.text and len(vals := [float(v) * unit_meter for v in fa.text.split()]) == 3
                    ]
                elif ch.text is not None:
                    params[tag] = ch.text.strip()
        geoms[gid] = {"id": gid, "name": geom.get("name", gid), "kind": kind, "points": points,
                      "triangles": triangles, "material_symbols": tri_mats, "params": params}
    return materials, geoms


def _pvc_module_params(params: dict[str, Any]) -> tuple[float, float, float, float]:
    return (
        _finite(float(params.get("module_width") or 1000.0) / 1000.0, "module_width", positive=True),
        _finite(float(params.get("module_height") or 2000.0) / 1000.0, "module_height", positive=True),
        _finite(float(params.get("module_x_spacing") or 20.0) / 1000.0, "module_x_spacing", minimum=0.0),
        _finite(float(params.get("module_y_spacing") or 20.0) / 1000.0, "module_y_spacing", minimum=0.0),
    )


def _extract_top_quad(wpts: list[tuple[float, float, float]], tris: list[list[int]]) -> tuple[list[tuple[float, float, float]], tuple[float, float, float]]:
    best_n: tuple[float, float, float] | None = None
    best_area = 0.0
    for a, b, c in tris:
        cr = _cross(_sub(wpts[b], wpts[a]), _sub(wpts[c], wpts[a]))
        area = math.sqrt(_dot(cr, cr))
        if area > best_area and abs(cr[2]) >= 0.05 * area:
            best_area = area
            best_n = (cr[0] / area, cr[1] / area, cr[2] / area)
    if best_n is None:
        raise ValueError("PV frame geometry has no valid inclined or horizontal surface")
    if best_n[2] < 0.0:
        best_n = (-best_n[0], -best_n[1], -best_n[2])
    dots = [_dot(p, best_n) for p in wpts]
    max_d = max(dots)
    top_pts: list[tuple[float, float, float]] = []
    for p, d in zip(wpts, dots):
        if d >= max_d - 0.01 and not any(math.dist(p, q) <= 1e-4 for q in top_pts):
            top_pts.append(p)
    if len(top_pts) < 4:
        dedup: list[tuple[float, float, float]] = []
        for p in wpts:
            if not any(math.dist(p, q) <= 1e-4 for q in dedup):
                dedup.append(p)
        top_pts = dedup[:4]
    if len(top_pts) < 4:
        raise ValueError("PV frame geometry requires at least 4 distinct vertices")
    center = tuple(sum(p[ax] for p in top_pts) / len(top_pts) for ax in range(3))
    ref = (1.0, 0.0, 0.0) if abs(best_n[0]) < 0.9 else (0.0, 1.0, 0.0)
    u_ax = _norm(_cross(best_n, ref))
    assert u_ax is not None
    v_ax = _cross(best_n, u_ax)
    ordered = sorted(top_pts, key=lambda p: math.atan2(_dot(_sub(p, center), v_ax), _dot(_sub(p, center), u_ax)))
    if len(ordered) > 4:
        ordered = [ordered[0], ordered[len(ordered) // 4], ordered[len(ordered) // 2], ordered[(3 * len(ordered)) // 4]]
    return ordered, best_n


def _fixed_table_from_pvc(name: str, wpts: list[tuple[float, float, float]], tris: list[list[int]], params: dict[str, Any]) -> dict[str, Any]:
    quad, n = _extract_top_quad(wpts, tris)
    slope_dir = (n[0], n[1], n[2]) if math.hypot(n[0], n[1]) > 1e-6 else (0.0, 1.0, 0.0)

    def _pair_metrics(pair: tuple[tuple[tuple[float, float, float], tuple[float, float, float]], tuple[tuple[float, float, float], tuple[float, float, float]]]):
        e1, e2 = pair
        m1 = tuple(0.5 * (e1[0][ax] + e1[1][ax]) for ax in range(3))
        m2 = tuple(0.5 * (e2[0][ax] + e2[1][ax]) for ax in range(3))
        long_len = 0.5 * (math.dist(e1[0], e1[1]) + math.dist(e2[0], e2[1]))
        larg_len = math.dist(m1, m2)
        sin_chord = abs(m2[2] - m1[2]) / larg_len if larg_len > 1e-6 else 0.0
        return e1, e2, m1, m2, long_len, larg_len, sin_chord

    pa = _pair_metrics(((quad[0], quad[1]), (quad[3], quad[2])))
    pb = _pair_metrics(((quad[1], quad[2]), (quad[0], quad[3])))
    if pa[4] <= 1e-6 or pa[5] <= 1e-6 or pb[4] <= 1e-6 or pb[5] <= 1e-6:
        raise ValueError(f"PV frame {name!r} has degenerate zero-area dimensions")
    if abs(pa[6] - pb[6]) <= 1e-4:
        chosen = pa if pa[4] >= pb[4] else pb
    else:
        hi, lo = (pa, pb) if pa[6] >= pb[6] else (pb, pa)
        # Guard flat or near-flat long sheds on sloped terrain (wide span, shallow chord slope).
        chosen = lo if (hi[5] > 2.0 * hi[4] and hi[5] > 10.0 and hi[6] < 0.25) else hi
    e1, e2, m1, m2, _, _, _ = chosen
    if m1[2] <= m2[2] - 1e-4 or (abs(m1[2] - m2[2]) <= 1e-4 and (m1[0] * slope_dir[0] + m1[1] * slope_dir[1]) >= (m2[0] * slope_dir[0] + m2[1] * slope_dir[1])):
        bot, origin_m, top_mid = e1, m1, m2
    else:
        bot, origin_m, top_mid = e2, m2, m1
    long_shed, larg_shed = math.dist(bot[0], bot[1]), math.dist(origin_m, top_mid)
    if long_shed <= 1e-6 or larg_shed <= 1e-6:
        raise ValueError(f"PV frame {name!r} has degenerate zero-area dimensions")
    e_up = _norm(_sub(top_mid, origin_m))
    assert e_up is not None
    edge_vec = _sub(bot[1], bot[0])
    if _dot(edge_vec, _cross(n, e_up)) < 0.0:
        edge_vec = _sub(bot[0], bot[1])
    e_long = _norm(edge_vec)
    assert e_long is not None
    horiz = math.hypot(e_long[0], e_long[1])
    base_slope = _clamp(math.degrees(math.atan2(e_long[2], horiz)) if horiz > 1e-6 else 0.0, -45.0, 45.0)
    az_deg = round(normalize_azimuth_deg(math.degrees(math.atan2(-e_long[1], e_long[0]))), 3) if horiz > 1e-6 else 0.0
    if abs(az_deg) < 1e-9:
        az_deg = 0.0
    tilt_deg = round(math.degrees(math.atan2(max(0.0, e_up[2]), max(0.0, n[2]))), 3)
    mw, ml, sx, sy = _pvc_module_params(params)
    orient, n_rows, n_cols = _infer_grid_counts(long_shed, larg_shed, mw, ml, sx, sy, tracker=False)
    az_rad = math.radians(az_deg)
    return {
        "kind": "fixed", "name": name, "origin_m": [round(origin_m[0], 6), round(origin_m[1], 6), round(origin_m[2], 6)],
        "long_shed_m": round(long_shed, 6), "larg_shed_m": round(larg_shed, 6), "tilt_deg": tilt_deg,
        "azimuth_deg": az_deg, "base_slope_deg": round(base_slope, 3), "module_orient": orient,
        "module_rows": n_rows, "module_columns": n_cols, "module_count": n_rows * n_cols,
        "module_width_m": mw, "module_length_m": ml, "spacing_x_m": sx, "spacing_y_m": sy,
        "normal_xy": (round(math.sin(az_rad), 6), round(math.cos(az_rad), 6)),
    }


def _tracker_table_from_pvc(name: str, wpts: list[tuple[float, float, float]], tris: list[list[int]], params: dict[str, Any], m: list[list[float]]) -> dict[str, Any]:
    quad, _ = _extract_top_quad(wpts, tris)
    av_raw = params.get("axis_vertices") or []
    if len(av_raw) >= 2:
        a1, a2 = _mat_apply(m, av_raw[0]), _mat_apply(m, av_raw[1])
    elif math.dist(quad[0], quad[1]) >= math.dist(quad[1], quad[2]):
        a1 = tuple(0.5 * (quad[0][ax] + quad[3][ax]) for ax in range(3))
        a2 = tuple(0.5 * (quad[1][ax] + quad[2][ax]) for ax in range(3))
    else:
        a1 = tuple(0.5 * (quad[0][ax] + quad[1][ax]) for ax in range(3))
        a2 = tuple(0.5 * (quad[3][ax] + quad[2][ax]) for ax in range(3))
    d = _sub(a2, a1)
    if d[1] < -1e-6 or (abs(d[1]) <= 1e-6 and d[0] < 0.0):
        a1, a2, d = a2, a1, _sub(a1, a2)
    axis_len = math.sqrt(_dot(d, d))
    if axis_len <= 1e-6:
        raise ValueError(f"Tracker {name!r} has degenerate axis length")
    d_unit = (d[0] / axis_len, d[1] / axis_len, d[2] / axis_len)
    axis_az = round(math.degrees(math.atan2(d_unit[0], d_unit[1])), 3)
    horiz = math.hypot(d_unit[0], d_unit[1])
    axis_tilt = _clamp(math.degrees(math.atan2(d_unit[2], horiz)) if horiz > 1e-6 else 0.0, -45.0, 45.0)
    origin_m = tuple(0.5 * (a1[ax] + a2[ax]) for ax in range(3))
    e01, e12 = _sub(quad[1], quad[0]), _sub(quad[2], quad[1])
    if abs(_dot(e01, d_unit)) >= abs(_dot(e12, d_unit)):
        larg_shed, long_shed = max(axis_len, math.sqrt(_dot(e01, e01))), math.sqrt(_dot(e12, e12))
    else:
        larg_shed, long_shed = max(axis_len, math.sqrt(_dot(e12, e12))), math.sqrt(_dot(e01, e01))
    mw, ml, sx, sy = _pvc_module_params(params)
    orient, n_rows, n_cols = _infer_grid_counts(long_shed, larg_shed, mw, ml, sx, sy, tracker=True)
    return {
        "kind": "tracker", "name": name, "origin_m": [round(origin_m[0], 6), round(origin_m[1], 6), round(origin_m[2], 6)],
        "long_shed_m": round(long_shed, 6), "larg_shed_m": round(larg_shed, 6),
        "axis_azimuth_deg": axis_az, "axis_tilt_deg": round(axis_tilt, 3),
        "min_phi_deg": round(_clamp(float(params.get("min_phi", -60.0)), -90.0, 0.0), 2),
        "max_phi_deg": round(_clamp(float(params.get("max_phi", 60.0)), 0.0, 90.0), 2),
        "module_orient": orient, "module_rows": n_rows, "module_columns": n_cols, "module_count": n_rows * n_cols,
        "module_width_m": mw, "module_length_m": ml, "spacing_x_m": sx, "spacing_y_m": sy,
    }


def parse_pvc_141(workspace: Path, pvc_name: str, *, pvc_subset: str | None = None) -> dict[str, Any]:
    """Parse a UserData/*.pvc COLLADA 1.4.1 file (PVcase) into tables, obstacles, and terrain."""
    _, raw = read_bounded_cad_bytes(workspace, pvc_name, ".pvc")
    text = raw.decode("utf-8-sig")
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
        raise ValueError("DOCTYPE and ENTITY declarations are rejected for .pvc security")
    root = ET.fromstring(text)
    if root.tag != f"{{{COLLADA_141_NS}}}COLLADA" or root.get("version") != "1.4.1":
        raise ValueError("Expected COLLADA 1.4.1 (.pvc) XML root; use read_house_pv_scene for COLLADA 1.5.0 (.pvc2)")

    ns = {"c": COLLADA_141_NS}
    unit_el = root.find("c:asset/c:unit", ns)
    unit_meter = _finite(float(unit_el.get("meter", "1.0")) if unit_el is not None else 1.0, "asset/unit@meter", positive=True)
    unit_name = unit_el.get("name", "meter") if unit_el is not None else "meter"
    materials, geoms = _parse_pvc_geometries(root, unit_meter)
    scene_ref = root.find("c:scene/c:instance_visual_scene", ns)
    target_sid = (scene_ref.get("url") or "").lstrip("#") if scene_ref is not None else ""
    vis_scenes = root.findall("c:library_visual_scenes/c:visual_scene", ns)
    vis_scene = next((vs for vs in vis_scenes if target_sid and vs.get("id") == target_sid), vis_scenes[0] if vis_scenes else None)
    if vis_scene is None:
        raise ValueError("Missing library_visual_scenes/visual_scene in .pvc file")

    fixed_tables, tracker_tables, obstacles, terrain_pts, used_names = [], [], [], [], set()
    instance_count = [0]

    def _walk(node: ET.Element, parent_m: list[list[float]], depth: int = 0) -> None:
        if depth > MAX_PVC_NODE_DEPTH:
            raise ValueError(f".pvc node nesting depth exceeds ceiling ({MAX_PVC_NODE_DEPTH})")
        m = parent_m
        for ch in node:
            tag = ch.tag.split("}")[-1]
            if tag == "matrix" and ch.text and len(vals := [float(v) for v in ch.text.split()]) == 16:
                mat = [vals[r * 4:(r + 1) * 4] for r in range(4)]
                for ax in range(3):
                    mat[ax][3] *= unit_meter
                m = _mat_mul(m, mat)
            elif tag == "translate" and ch.text and len(vals := [float(v) * unit_meter for v in ch.text.split()]) == 3:
                tm = _mat_identity()
                tm[0][3], tm[1][3], tm[2][3] = vals
                m = _mat_mul(m, tm)
            elif tag == "rotate" and ch.text and len(vals := [float(v) for v in ch.text.split()]) == 4:
                m = _mat_mul(m, _mat_rotate(vals[0], vals[1], vals[2], vals[3]))
            elif tag == "scale" and ch.text and len(vals := [float(v) for v in ch.text.split()]) == 3:
                sm = _mat_identity()
                sm[0][0], sm[1][1], sm[2][2] = vals
                m = _mat_mul(m, sm)

        node_label = node.get("name") or node.get("id") or "CadObj"
        for ch in node:
            tag = ch.tag.split("}")[-1]
            if tag == "instance_geometry":
                geom = geoms.get((gid := (ch.get("url") or "").lstrip("#")))
                if geom is None:
                    continue
                instance_count[0] += 1
                if instance_count[0] > MAX_PVC_INSTANCES:
                    raise ValueError(f".pvc instance count exceeds ceiling ({MAX_PVC_INSTANCES})")
                if len(fixed_tables) + len(tracker_tables) >= MAX_CAD_TABLES:
                    raise ValueError(f".pvc table count exceeds ceiling ({MAX_CAD_TABLES})")
                bound_names = [materials.get((im.get("target") or "").lstrip("#"), im.get("symbol", "")) for im in ch.findall(".//c:instance_material", ns)]
                label_all = " ".join([gid, geom["name"], node_label, *bound_names, *geom["material_symbols"]])
                is_terrain = geom["kind"] not in ("fixed", "tracker") and bool(_TERRAIN_RE.search(label_all))
                if is_terrain and len(terrain_pts) + len(geom["points"]) > MAX_PVC_TERRAIN_POINTS:
                    raise ValueError(f".pvc terrain point count exceeds ceiling ({MAX_PVC_TERRAIN_POINTS})")
                wpts = [_mat_apply(m, p) for p in geom["points"]]
                base_name = f"{node_label}_{gid}" if len(node_label) < 60 else gid
                if geom["kind"] == "fixed":
                    fixed_tables.append(_fixed_table_from_pvc(base_name, wpts, geom["triangles"], geom["params"]))
                elif geom["kind"] == "tracker":
                    tracker_tables.append(_tracker_table_from_pvc(base_name, wpts, geom["triangles"], geom["params"], m))
                else:
                    if is_terrain:
                        terrain_pts.extend(wpts)
                    elif geom["triangles"] and len(obstacles) < MAX_OBSTACLES:
                        uniq, suffix = base_name, 1
                        while uniq.casefold() in used_names:
                            suffix += 1
                            uniq = f"{base_name}_{suffix}"
                        used_names.add(uniq.casefold())
                        cverts = [[round(v[0], 5), round(v[1], 5), round(v[2], 5)] for v in wpts]
                        seen_tris: set[tuple[tuple[float, float, float], ...]] = set()
                        vtris = []
                        for a, b, c in geom["triangles"]:
                            pa, pb, pc = tuple(cverts[a]), tuple(cverts[b]), tuple(cverts[c])
                            cr = _cross(_sub(pb, pa), _sub(pc, pa))
                            if math.sqrt(_dot(cr, cr)) <= 1e-8:
                                continue
                            key = tuple(sorted((pa, pb, pc)))
                            if key not in seen_tris:
                                seen_tris.add(key)
                                vtris.append([a, b, c])
                        if vtris:
                            obstacles.append({"kind": "mesh", "name": uniq, "vertices": cverts, "triangles": vtris})
            elif tag == "node":
                _walk(ch, m, depth + 1)

    for top_node in vis_scene.findall("c:node", ns):
        _walk(top_node, _mat_identity(), 0)

    scene_kind = "mixed" if (fixed_tables and tracker_tables) else ("fixed" if fixed_tables else ("tracker" if tracker_tables else None))
    if scene_kind is None:
        raise ValueError(f"No PV <frame_parameters> or <tracker_parameters> geometries found in {pvc_name}")
    tables = fixed_tables if scene_kind == "fixed" else (tracker_tables if scene_kind == "tracker" else [*fixed_tables, *tracker_tables])
    xs, ys, zs = [t["origin_m"][0] for t in tables], [t["origin_m"][1] for t in tables], [t["origin_m"][2] for t in tables]
    rep = tables[0]
    summary: dict[str, Any] = {
        "format": "pvc_1.4.1", "file_name": pvc_name, "scene_kind": scene_kind, "unit_name": unit_name, "unit_meter": unit_meter,
        "table_count": len(tables), "fixed_count": len(fixed_tables), "tracker_count": len(tracker_tables),
        "inferred_module_count": sum(t["module_count"] for t in tables),
        "collector_area_m2": round(sum(t["long_shed_m"] * t["larg_shed_m"] for t in tables), 2),
        "obstacle_count": len(obstacles), "terrain_point_count": len(terrain_pts),
        "bounds_m": {"min_x": round(min(xs), 3), "max_x": round(max(xs), 3), "min_y": round(min(ys), 3), "max_y": round(max(ys), 3), "min_z": round(min(zs), 3), "max_z": round(max(zs), 3)},
        "module_spec": {k: rep[k] for k in ("module_width_m", "module_length_m", "module_orient", "spacing_x_m", "spacing_y_m", "module_rows", "module_columns")},
        "tables": tables, "obstacles": obstacles, "terrain_points": downsample_xyz_points(terrain_pts) if len(terrain_pts) >= 3 else [],
    }
    if fixed_tables:
        area_f = sum(t["long_shed_m"] * t["larg_shed_m"] for t in fixed_tables)
        dom_t = sum(t["tilt_deg"] * t["long_shed_m"] * t["larg_shed_m"] for t in fixed_tables) / area_f
        sx_n, sy_n = sum(t["normal_xy"][0] * t["long_shed_m"] * t["larg_shed_m"] for t in fixed_tables), sum(t["normal_xy"][1] * t["long_shed_m"] * t["larg_shed_m"] for t in fixed_tables)
        dom_a = math.degrees(math.atan2(sx_n, sy_n)) if math.hypot(sx_n, sy_n) > 1e-6 else 0.0
        summary.update(dominant_tilt_deg=round(dom_t, 2), dominant_azimuth_deg=round(dom_a, 2),
                       pitch_estimate_m=_estimate_pvc_pitch(fixed_tables, tracker=False, az_deg=dom_a))
    if tracker_tables:
        sx_a = sum(math.sin(math.radians(t["axis_azimuth_deg"])) * t["long_shed_m"] * t["larg_shed_m"] for t in tracker_tables)
        sy_a = sum(math.cos(math.radians(t["axis_azimuth_deg"])) * t["long_shed_m"] * t["larg_shed_m"] for t in tracker_tables)
        dom_ax = math.degrees(math.atan2(sx_a, sy_a)) if math.hypot(sx_a, sy_a) > 1e-6 else 0.0
        summary.update(dominant_axis_azimuth_deg=round(dom_ax, 2), min_phi_deg=min(t["min_phi_deg"] for t in tracker_tables),
                       max_phi_deg=max(t["max_phi_deg"] for t in tracker_tables), pitch_estimate_m=_estimate_pvc_pitch(tracker_tables, tracker=True, az_deg=dom_ax))
    return filter_pvc_subset(summary, pvc_subset) if pvc_subset is not None else summary


def build_pvc_3d_shading(parsed: dict[str, Any], *, module_name: str, obstacles: list[dict[str, Any]] | None = None,
                         terrain_mesh: Any = None, electrical_shading: Any = None, series_modules: int | None = None,
                         is_half_cut: bool = False, version: str = SYNTH_VERSION) -> list[str]:
    """Build native PVsyst pvShading lines directly from a parsed .pvc 1.4.1 scene."""
    if parsed["scene_kind"] == "mixed":
        raise ValueError("Mixed fixed + tracker .pvc scenes are not supported in a single utility variant")
    rep, tables = parsed["module_spec"], parsed["tables"]
    if parsed["scene_kind"] == "fixed":
        return build_ground_3d_shading(
            tables, module_name=module_name, module_width_m=rep["module_width_m"], module_length_m=rep["module_length_m"],
            module_orient=rep["module_orient"], spacing_x_m=rep["spacing_x_m"], spacing_y_m=rep["spacing_y_m"],
            obstacles=obstacles, terrain_mesh=terrain_mesh, electrical_shading=electrical_shading,
            series_modules=series_modules, is_half_cut=is_half_cut, version=version,
        )
    lines, _ = build_tracker_3d_shading(
        tables, module_name=module_name, module_width_m=rep["module_width_m"], module_length_m=rep["module_length_m"],
        module_orient=rep["module_orient"], spacing_x_m=rep["spacing_x_m"], spacing_y_m=rep["spacing_y_m"],
        ground_clearance_m=0.0, obstacles=obstacles,
        terrain_mesh=terrain_mesh, terrain_opts={"drape_tables": False},
        electrical_shading=electrical_shading, series_modules=series_modules, is_half_cut=is_half_cut, version=version,
    )
    return lines


def validate_cad_ground_layout(layout: dict, scope: str, mode: str, bad: Any) -> None:
    """Validate pvc_name / dxf_name / dxf_layers / dxf_unit_scale on ground_layout."""
    pvc_name, dxf_name = layout.get("pvc_name"), layout.get("dxf_name")
    if pvc_name is not None and dxf_name is not None:
        bad("ground_layout.dxf_name", "pvc_name and dxf_name are mutually exclusive")
    for key, val, ext in (("pvc_name", pvc_name, ".pvc"), ("dxf_name", dxf_name, ".dxf")):
        if val is None:
            continue
        if not isinstance(val, str):
            bad(f"ground_layout.{key}", f"{key} must be a UserData {ext} filename")
        else:
            try:
                safe_filename(val, ext)
            except ValueError as exc:
                bad(f"ground_layout.{key}", str(exc))
        if key == "pvc_name" and scope == "ground_dome":
            bad("ground_layout.pvc_name", "pvc_name is supported for scope='ground_sheds' or 'ground_tracker'")
        if layout.get("model_mode") == "unlimited":
            bad("ground_layout.model_mode", f"{key} requires 3D layout (omit model_mode or set model_mode='3d')")
        for conflict in ("site_polygon", "sub_orientations"):
            if layout.get(conflict) is not None:
                suffix = "supplies the boundary polygon and " if key == "dxf_name" else ""
                bad(f"ground_layout.{conflict}", f"{key} {suffix}cannot be combined with {conflict}")
    if pvc_name is not None and layout.get("exclusion_zones"):
        bad("ground_layout.exclusion_zones", "pvc_name supplies pre-placed PV tables and cannot be combined with exclusion_zones")
    if layout.get("dxf_layers") is not None:
        if dxf_name is None:
            bad("ground_layout.dxf_layers", "dxf_layers requires ground_layout.dxf_name")
        else:
            try:
                normalize_dxf_layers(layout["dxf_layers"])
            except ValueError as exc:
                bad("ground_layout.dxf_layers", exc)
    if layout.get("dxf_unit_scale") is not None:
        val = layout["dxf_unit_scale"]
        if dxf_name is None:
            bad("ground_layout.dxf_unit_scale", "dxf_unit_scale requires ground_layout.dxf_name")
        elif not _is_number(val) or float(val) <= 0:
            bad("ground_layout.dxf_unit_scale", "dxf_unit_scale must be a positive finite number")
    pvc_subset = layout.get("pvc_subset")
    if pvc_subset is not None:
        if pvc_name is None:
            bad("ground_layout.pvc_subset", "pvc_subset requires ground_layout.pvc_name")
        elif pvc_subset not in ("fixed", "tracker"):
            bad("ground_layout.pvc_subset", "pvc_subset must be 'fixed' or 'tracker'")
        elif pvc_subset == "fixed" and scope != "ground_sheds":
            bad("ground_layout.pvc_subset", "pvc_subset='fixed' requires scope='ground_sheds'")
        elif pvc_subset == "tracker" and scope != "ground_tracker":
            bad("ground_layout.pvc_subset", "pvc_subset='tracker' requires scope='ground_tracker'")
    tol = layout.get("pvc_orientation_tolerance_deg")
    if tol is not None:
        if pvc_name is None:
            bad("ground_layout.pvc_orientation_tolerance_deg", "pvc_orientation_tolerance_deg requires ground_layout.pvc_name")
        elif scope != "ground_sheds":
            bad("ground_layout.pvc_orientation_tolerance_deg", "pvc_orientation_tolerance_deg is only supported for scope='ground_sheds' fixed tables")
        elif pvc_subset == "tracker":
            bad("ground_layout.pvc_orientation_tolerance_deg", "pvc_orientation_tolerance_deg cannot be combined with pvc_subset='tracker'")
        elif not _is_number(tol) or not (MIN_PVC_ORIENTATION_TOLERANCE_DEG <= float(tol) <= MAX_PVC_ORIENTATION_TOLERANCE_DEG):
            bad(
                "ground_layout.pvc_orientation_tolerance_deg",
                f"pvc_orientation_tolerance_deg must lie in {MIN_PVC_ORIENTATION_TOLERANCE_DEG}..{MAX_PVC_ORIENTATION_TOLERANCE_DEG} degrees",
            )


def _merge_obstacles(cad_obs: list[dict[str, Any]], user_obs: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not cad_obs and not user_obs:
        return None
    merged = [*cad_obs]
    seen = {str(o["name"]).casefold() for o in merged}
    for obs in user_obs or ():
        item, name = dict(obs), str(obs.get("name", "UserObstacle"))
        while name.casefold() in seen:
            name += "_usr"
        item["name"] = name
        seen.add(name.casefold())
        merged.append(item)
    return merged[:MAX_OBSTACLES]


def _merge_exclusion_zones(
    cad_ex: list[list[list[float]]],
    user_ex: list[list[list[float]]] | None,
) -> list[list[list[float]]] | None:
    if not cad_ex and not user_ex:
        return None
    merged = [*cad_ex, *(user_ex or ())]
    if len(merged) > MAX_EXCLUSION_ZONES:
        raise ValueError(
            f"Combined DXF ({len(cad_ex)}) and inline ({len(user_ex or ())}) exclusion zones "
            f"({len(merged)}) exceed the maximum of {MAX_EXCLUSION_ZONES}"
        )
    return merged


def resolve_cad_synthesis_inputs(
    store: Any, *, field_type: str, pvc_name: str | None, dxf_name: str | None, dxf_layers: dict[str, Any] | None,
    dxf_unit_scale: float | None, tilt_deg: float, azimuth_deg: float, axis_azimuth_deg: float, pitch_m: float,
    tilt_explicit: bool, azimuth_explicit: bool, obstacles: list[dict[str, Any]] | None,
    exclusion_zones: list[list[list[float]]] | None = None, terrain: dict[str, Any] | None = None,
    module_name: str, electrical_shading: Any = None, series_modules: int | None = None, is_half_cut: bool = False,
    version: str, pvc_subset: str | None = None, pvc_orientation_tolerance_deg: float | None = None,
) -> dict[str, Any]:
    """Resolve .pvc 1.4.1 or ASCII .dxf inputs for synthesize_utility_project."""
    ws = getattr(getattr(store, "components", None), "workspace", None)
    if ws is None:
        raise ValueError("CAD import requires a workspace-backed store")
    if dxf_name is not None:
        parsed = parse_ascii_dxf(ws, dxf_name, dxf_layers=dxf_layers, unit_scale=dxf_unit_scale)
        eff_terrain = terrain if terrain is not None else (
            {"name": f"DXF_{Path(dxf_name).stem}"[:40], "points": parsed["terrain_points"], "drape_tables": True, "follow_slope": True}
            if len(parsed["terrain_points"]) >= 3 else None
        )
        return {
            "mode": "dxf", "site_polygon": parsed["site_polygon"],
            "exclusion_zones": _merge_exclusion_zones(parsed["exclusion_zones"], exclusion_zones),
            "obstacles": _merge_obstacles(parsed["obstacles"], obstacles), "terrain": eff_terrain,
            "cad_summary": {k: parsed[k] for k in ("format", "file_name", "unit_scale", "insunits_code", "layers", "site_area_m2", "boundary_simplified", "bounds_m")}
            | {"exclusion_zone_count": len(parsed["exclusion_zones"]), "obstacle_count": len(parsed["obstacles"]), "terrain_point_count": len(parsed["terrain_points"])},
        }

    assert pvc_name is not None
    if exclusion_zones:
        raise ValueError("pvc_name supplies pre-placed PV tables and cannot be combined with exclusion_zones")
    parsed = parse_pvc_141(ws, pvc_name, pvc_subset=pvc_subset)
    if parsed["scene_kind"] == "mixed":
        raise ValueError(
            f"{pvc_name} mixes fixed tables and trackers; pass ground_layout.pvc_subset='fixed' or 'tracker', "
            "or split into separate fixed and tracker .pvc files"
        )
    if parsed["scene_kind"] == "fixed" and field_type != "RegularSheds":
        raise ValueError(f"{pvc_name} contains fixed PV tables and requires scope='ground_sheds'")
    if parsed["scene_kind"] == "tracker" and field_type != "TrackHorizNSAxis":
        raise ValueError(f"{pvc_name} contains single-axis trackers and requires scope='ground_tracker'")

    pvc_clusters: list[dict[str, Any]] | None = None
    if pvc_orientation_tolerance_deg is not None:
        if parsed["scene_kind"] != "fixed":
            raise ValueError("pvc_orientation_tolerance_deg is only supported for fixed .pvc tables")
        tagged_tables, pvc_clusters = cluster_fixed_pvc_orientations(parsed["tables"], pvc_orientation_tolerance_deg)
        parsed = {**parsed, "tables": tagged_tables}

    eff_tilt, eff_azim, eff_axis = tilt_deg, azimuth_deg, axis_azimuth_deg
    if parsed["scene_kind"] == "fixed":
        if pvc_clusters is not None and len(pvc_clusters) > 1:
            eff_tilt = float(pvc_clusters[0]["tilt_deg"])
            eff_azim = float(pvc_clusters[0]["azimuth_deg"])
        else:
            dom_t = float(pvc_clusters[0]["tilt_deg"]) if pvc_clusters else parsed["dominant_tilt_deg"]
            dom_a = float(pvc_clusters[0]["azimuth_deg"]) if pvc_clusters else parsed["dominant_azimuth_deg"]
            if tilt_explicit and abs(tilt_deg - dom_t) > 5.0:
                raise ValueError(f"spec.tilt ({tilt_deg}°) differs from .pvc frame tilt ({dom_t}°) by > 5°")
            if azimuth_explicit and circular_azimuth_diff_deg(azimuth_deg, dom_a) > 5.0:
                raise ValueError(f"spec.azimuth ({azimuth_deg}°) differs from .pvc frame azimuth ({dom_a}°) by > 5°")
            eff_tilt = dom_t if not tilt_explicit else tilt_deg
            eff_azim = dom_a if not azimuth_explicit else azimuth_deg
    else:
        dom_ax = parsed["dominant_axis_azimuth_deg"]
        if azimuth_explicit and circular_azimuth_diff_deg(axis_azimuth_deg, dom_ax) > 5.0:
            raise ValueError(f"axis azimuth ({axis_azimuth_deg}°) differs from .pvc tracker axis ({dom_ax}°) by > 5°")
        if not azimuth_explicit:
            eff_axis = dom_ax

    eff_obs = _merge_obstacles(parsed["obstacles"], obstacles)
    eff_terrain_mesh = terrain_summary = None
    if terrain is not None:
        eff_terrain_mesh = build_terrain_mesh(terrain, workspace=ws)
        terrain_summary = eff_terrain_mesh.summary()
    elif len(parsed["terrain_points"]) >= 3:
        eff_terrain_mesh = build_terrain_mesh({"name": f"PVC_{Path(pvc_name).stem}"[:40], "points": parsed["terrain_points"], "drape_tables": False}, workspace=ws)
        terrain_summary = eff_terrain_mesh.summary()

    shading_lines = build_pvc_3d_shading(
        parsed, module_name=module_name, obstacles=eff_obs, terrain_mesh=eff_terrain_mesh,
        electrical_shading=electrical_shading, series_modules=series_modules, is_half_cut=is_half_cut, version=version,
    )
    rep = parsed["module_spec"]
    chord_mod = rep["module_length_m"] if rep["module_orient"] == "moPortrait" else rep["module_width_m"]
    coll_w = round(rep["module_rows"] * chord_mod + max(0, rep["module_rows"] - 1) * rep["spacing_y_m"], 3)
    raw_pitch = parsed.get("pitch_estimate_m")
    if raw_pitch is None and pvc_clusters:
        raw_pitch = pvc_clusters[0].get("pitch_estimate_m")
    eff_pitch = raw_pitch if (raw_pitch is not None and raw_pitch > coll_w) else max(pitch_m, coll_w + 0.5)
    cad_summary = {
        k: parsed[k] for k in ("format", "file_name", "scene_kind", "unit_name", "unit_meter", "table_count",
                               "inferred_module_count", "collector_area_m2", "bounds_m", "pitch_estimate_m", "module_spec")
    } | {"obstacle_count": len(parsed["obstacles"]), "terrain_point_count": len(parsed["terrain_points"]),
         "dominant_tilt_deg": eff_tilt if parsed["scene_kind"] == "fixed" else 0.0,
         "dominant_azimuth_deg": eff_azim if parsed["scene_kind"] == "fixed" else eff_axis}
    if pvc_subset is not None:
        cad_summary.update(
            pvc_subset=pvc_subset,
            original_scene_kind=parsed.get("original_scene_kind"),
            filtered_out_table_count=parsed.get("filtered_out_table_count", 0),
        )
    if pvc_clusters is not None:
        cad_summary.update(
            orientation_tolerance_deg=float(pvc_orientation_tolerance_deg),
            orientation_count=len(pvc_clusters),
            orientations=pvc_clusters,
        )
    return {
        "mode": "pvc", "shading_lines": shading_lines, "tilt_deg": eff_tilt, "azimuth_deg": eff_azim, "axis_azimuth_deg": eff_axis,
        "pitch_m": max(eff_pitch, 0.2), "collector_width_m": coll_w, "shed_count": parsed["table_count"],
        "module_rows": rep["module_rows"], "module_columns": rep["module_columns"], "module_orient": rep["module_orient"],
        "obstacles": eff_obs, "min_phi_deg": parsed.get("min_phi_deg", -60.0), "max_phi_deg": parsed.get("max_phi_deg", 60.0),
        "terrain_summary": terrain_summary, "cad_summary": cad_summary, "first_table": parsed["tables"][0],
        **({"pvc_orientations": pvc_clusters} if pvc_clusters is not None else {}),
    }


def read_cad_scene_preview(components: Any, scene_name: str) -> dict[str, Any]:
    """Read UserData *.pvc (1.4.1) or ASCII *.dxf into structured preview metadata for read_house_pv_scene."""
    result: dict[str, Any] = {"status": "unsupported", "issues": [], "diagnostics": [], "required_inputs": [],
                              "source_verified": False, "imported": False, "native_consumption_verified": False}
    try:
        lower = scene_name.lower()
        if lower.endswith(".pvc"):
            parsed = parse_pvc_141(components.workspace, scene_name)
            scope = "ground_sheds" if parsed["scene_kind"] == "fixed" else ("ground_tracker" if parsed["scene_kind"] == "tracker" else None)
            result.update({
                "status": "ready", "format": "pvc_1.4.1", "scene_name": scene_name,
                "geometry": {k: parsed[k] for k in ("scene_kind", "table_count", "fixed_count", "tracker_count",
                                                    "inferred_module_count", "collector_area_m2",
                                                    "obstacle_count", "terrain_point_count", "bounds_m", "unit_name", "unit_meter",
                                                    "module_spec", "pitch_estimate_m")}
                | ({k: parsed[k] for k in ("dominant_tilt_deg", "dominant_azimuth_deg") if k in parsed})
                | ({k: parsed[k] for k in ("dominant_axis_azimuth_deg", "min_phi_deg", "max_phi_deg") if k in parsed})
                | ({"available_subsets": ["fixed", "tracker"]} if parsed["scene_kind"] == "mixed" else {}),
                "recommended_design_args": {**({"scope": scope} if scope else {}), "ground_layout": {"pvc_name": scene_name, "model_mode": "3d"}},
            })
        elif lower.endswith(".dxf"):
            parsed = parse_ascii_dxf(components.workspace, scene_name)
            result.update({
                "status": "ready", "format": "ascii_dxf", "scene_name": scene_name,
                "exclusion_zones": parsed["exclusion_zones"],
                "geometry": {
                    "site_area_m2": parsed["site_area_m2"], "boundary_vertices": len(parsed["site_polygon"]),
                    "boundary_simplified": parsed["boundary_simplified"],
                    "exclusion_zone_count": len(parsed["exclusion_zones"]),
                    "exclusion_zones": parsed["exclusion_zones"],
                    "obstacle_count": len(parsed["obstacles"]),
                    "terrain_point_count": len(parsed["terrain_points"]), "layers": parsed["layers"],
                    "entity_counts": parsed["entity_counts"], "unit_scale": parsed["unit_scale"],
                    "insunits_code": parsed["insunits_code"], "bounds_m": parsed["bounds_m"], "site_polygon": parsed["site_polygon"],
                },
                "recommended_design_args": {"scope": "ground_sheds", "ground_layout": {"dxf_name": scene_name, "model_mode": "3d"}},
            })
        else:
            raise ValueError("Unsupported CAD scene extension")
    except (ValueError, OSError, ET.ParseError, UnicodeError) as exc:
        result["issues"].append(str(exc))
        result["diagnostics"] = [{"code": "cad_read", "path": "scene_name", "message": str(exc)}]
    return result


def check_pvc_electrical_area(
    cad_meta: dict[str, Any] | None,
    utility_summary: dict[str, Any],
    electrical_modules: int,
    warnings: list[str],
) -> None:
    """Validate that electrical PV area fits inside imported .pvc collector area and warn if counts differ."""
    if not isinstance(cad_meta, dict) or cad_meta.get("format") != "pvc_1.4.1":
        return
    elec_mods = int(electrical_modules)
    mod_area = float(utility_summary["module_width_m"]) * float(utility_summary["module_length_m"])
    elec_area = round(elec_mods * mod_area, 2)
    cad_area = float(cad_meta["collector_area_m2"])
    if elec_area > cad_area * 1.001:
        raise ValueError(
            f"Electrical module area ({elec_area:.2f} m2 for {elec_mods} modules) exceeds imported .pvc 3D collector area ({cad_area:.2f} m2)"
        )
    cad_meta["electrical_area_m2"] = elec_area
    cad_meta["area_utilization"] = util = round(elec_area / cad_area, 4) if cad_area > 0 else 0.0
    if util < 0.85:
        warnings.append(
            f"Imported .pvc collector area ({cad_area:.1f} m2) is significantly larger than subarray module area ({elec_area:.1f} m2, {util * 100:.1f}%); PVsystCLI may block simulation."
        )
    elif int(cad_meta["inferred_module_count"]) != elec_mods:
        warnings.append(
            f"Imported .pvc scene geometry suggests {cad_meta['inferred_module_count']} modules ({cad_area:.1f} m2) "
            f"while electrical sizing places {elec_mods} modules ({elec_area:.1f} m2)."
        )
    cad_orients = cad_meta.get("orientations")
    sub_orients = utility_summary.get("sub_orientations")
    if isinstance(cad_orients, list) and isinstance(sub_orients, list) and len(cad_orients) == len(sub_orients) >= 2:
        sub_by_id = {int(s["orientation_id"]): s for s in sub_orients}
        for cl in cad_orients:
            oid = int(cl["orientation_id"])
            if (sub := sub_by_id.get(oid)) is None:
                continue
            sub_area = round(int(sub["module_count"]) * mod_area, 2)
            cl_area = float(cl["collector_area_m2"])
            cl["electrical_area_m2"] = sub_area
            cl["area_utilization"] = round(sub_area / cl_area, 4) if cl_area > 0 else 0.0
            if sub_area > cl_area * 1.001:
                raise ValueError(
                    f".pvc orientation #{oid} ({cl['label']}) electrical module area ({sub_area:.2f} m2) "
                    f"exceeds its clustered 3D collector area ({cl_area:.2f} m2)"
                )


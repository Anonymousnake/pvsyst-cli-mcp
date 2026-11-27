"""Mixed .pvc subset filtering, fixed-table multi-orientation clustering, and electrical apportionment."""
from __future__ import annotations

import heapq
import math
import statistics
from typing import Any

from pvsyst_auto.project.synthesize import _clean_text, _fmt_angle
from pvsyst_auto.validation import finite_number as _finite

MAX_PVC_ORIENTATIONS = 8
MIN_PVC_ORIENTATION_TOLERANCE_DEG = 0.5
MAX_PVC_ORIENTATION_TOLERANCE_DEG = 30.0


def normalize_azimuth_deg(angle: float) -> float:
    """Normalize PVsyst azimuth into (-180, 180] degrees (0 = south, west positive)."""
    val = ((float(angle) + 180.0) % 360.0) - 180.0
    if abs(val + 180.0) < 1e-9 or abs(val - 180.0) < 1e-9:
        return 180.0
    return 0.0 if abs(val) < 1e-9 else val


def circular_azimuth_diff_deg(a1: float, a2: float) -> float:
    """Return shortest unsigned circular angular separation in [0, 180] degrees."""
    return abs((float(a1) - float(a2) + 180.0) % 360.0 - 180.0)


def orientation_distance_deg(tilt1: float, az1: float, tilt2: float, az2: float) -> float:
    """Combined 2D Euclidean tilt + circular azimuth angular distance in degrees."""
    return math.hypot(float(tilt1) - float(tilt2), circular_azimuth_diff_deg(az1, az2))


def weighted_circular_mean_deg(angles_deg: list[float], weights: list[float]) -> float:
    """Compute weighted circular mean angle in (-180, 180] degrees."""
    sx = sum(w * math.sin(math.radians(a)) for a, w in zip(angles_deg, weights))
    sy = sum(w * math.cos(math.radians(a)) for a, w in zip(angles_deg, weights))
    if math.hypot(sx, sy) <= 1e-9:
        return normalize_azimuth_deg(angles_deg[0]) if angles_deg else 0.0
    return normalize_azimuth_deg(math.degrees(math.atan2(sx, sy)))


def estimate_pvc_pitch(tables: list[dict[str, Any]], *, tracker: bool, az_deg: float) -> float | None:
    """Estimate inter-row pitch (m) across PV tables along the perpendicular row-step direction."""
    if len(tables) < 2:
        return None
    rad = math.radians(az_deg)
    step_vec = (math.cos(rad), -math.sin(rad)) if tracker else (math.sin(rad), math.cos(rad))
    projs = sorted(float(t["origin_m"][0]) * step_vec[0] + float(t["origin_m"][1]) * step_vec[1] for t in tables)
    row_centers = [projs[0]]
    for p in projs[1:]:
        if p - row_centers[-1] > 0.5:
            row_centers.append(p)
    diffs = [
        round(row_centers[i + 1] - row_centers[i], 3)
        for i in range(len(row_centers) - 1)
        if row_centers[i + 1] - row_centers[i] > 0.5
    ]
    return round(float(statistics.median(diffs)), 3) if diffs else None


def _table_kind(tbl: dict[str, Any]) -> str:
    if (kind := tbl.get("kind")) in ("fixed", "tracker"):
        return kind
    if "axis_azimuth_deg" in tbl:
        return "tracker"
    return "fixed"


def filter_pvc_subset(parsed: dict[str, Any], pvc_subset: str | None) -> dict[str, Any]:
    """Filter a parsed .pvc summary to either 'fixed' or 'tracker' tables, keeping obstacles/terrain."""
    if pvc_subset is None:
        return parsed
    if pvc_subset not in ("fixed", "tracker"):
        raise ValueError("pvc_subset must be 'fixed' or 'tracker'")
    all_tables = list(parsed.get("tables") or ())
    selected = [dict(t) for t in all_tables if _table_kind(t) == pvc_subset]
    if not selected:
        fname = parsed.get("file_name", "scene")
        raise ValueError(f"{fname} contains no {pvc_subset} PV tables for pvc_subset={pvc_subset!r}")

    xs = [float(t["origin_m"][0]) for t in selected]
    ys = [float(t["origin_m"][1]) for t in selected]
    zs = [float(t["origin_m"][2]) for t in selected]
    rep = selected[0]
    obstacles = list(parsed.get("obstacles") or ())
    terrain_pts = list(parsed.get("terrain_points") or ())
    filtered: dict[str, Any] = {
        "format": parsed.get("format", "pvc_1.4.1"),
        "file_name": parsed.get("file_name", ""),
        "scene_kind": pvc_subset,
        "pvc_subset": pvc_subset,
        "original_scene_kind": parsed.get("scene_kind"),
        "original_fixed_count": parsed.get("fixed_count", sum(1 for t in all_tables if _table_kind(t) == "fixed")),
        "original_tracker_count": parsed.get("tracker_count", sum(1 for t in all_tables if _table_kind(t) == "tracker")),
        "filtered_out_table_count": len(all_tables) - len(selected),
        "unit_name": parsed.get("unit_name", "meter"),
        "unit_meter": parsed.get("unit_meter", 1.0),
        "table_count": len(selected),
        "fixed_count": len(selected) if pvc_subset == "fixed" else 0,
        "tracker_count": len(selected) if pvc_subset == "tracker" else 0,
        "inferred_module_count": sum(int(t["module_count"]) for t in selected),
        "collector_area_m2": round(sum(float(t["long_shed_m"]) * float(t["larg_shed_m"]) for t in selected), 2),
        "obstacle_count": len(obstacles),
        "terrain_point_count": parsed.get("terrain_point_count", len(terrain_pts)),
        "bounds_m": {
            "min_x": round(min(xs), 3), "max_x": round(max(xs), 3),
            "min_y": round(min(ys), 3), "max_y": round(max(ys), 3),
            "min_z": round(min(zs), 3), "max_z": round(max(zs), 3),
        },
        "module_spec": {
            k: rep[k]
            for k in ("module_width_m", "module_length_m", "module_orient", "spacing_x_m", "spacing_y_m", "module_rows", "module_columns")
        },
        "tables": selected,
        "obstacles": obstacles,
        "terrain_points": terrain_pts,
    }
    if pvc_subset == "fixed":
        weights = [float(t["long_shed_m"]) * float(t["larg_shed_m"]) for t in selected]
        total_w = sum(weights) or 1.0
        dom_t = sum(float(t["tilt_deg"]) * w for t, w in zip(selected, weights)) / total_w
        dom_a = weighted_circular_mean_deg([float(t["azimuth_deg"]) for t in selected], weights)
        filtered.update(
            dominant_tilt_deg=round(dom_t, 2),
            dominant_azimuth_deg=round(dom_a, 2),
            pitch_estimate_m=estimate_pvc_pitch(selected, tracker=False, az_deg=dom_a),
        )
    else:
        weights = [float(t["long_shed_m"]) * float(t["larg_shed_m"]) for t in selected]
        dom_ax = weighted_circular_mean_deg([float(t["axis_azimuth_deg"]) for t in selected], weights)
        filtered.update(
            dominant_axis_azimuth_deg=round(dom_ax, 2),
            min_phi_deg=min(float(t["min_phi_deg"]) for t in selected),
            max_phi_deg=max(float(t["max_phi_deg"]) for t in selected),
            pitch_estimate_m=estimate_pvc_pitch(selected, tracker=True, az_deg=dom_ax),
        )
    return filtered


def _centroid_from_sums(
    w_sum: float, tilt_wsum: float, sin_wsum: float, cos_wsum: float, slope_wsum: float, fallback_az: float
) -> tuple[float, float, float]:
    inv_w = 1.0 / (w_sum or 1.0)
    tilt = tilt_wsum * inv_w
    az = (
        normalize_azimuth_deg(math.degrees(math.atan2(sin_wsum, cos_wsum)))
        if math.hypot(sin_wsum, cos_wsum) > 1e-9
        else normalize_azimuth_deg(fallback_az)
    )
    slope = max(-45.0, min(45.0, slope_wsum * inv_w))
    return tilt, az, slope


def cluster_fixed_pvc_orientations(
    tables: list[dict[str, Any]],
    tolerance_deg: float,
    *,
    max_orientations: int = MAX_PVC_ORIENTATIONS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cluster fixed .pvc tables by circular azimuth + tilt distance into 1..max_orientations Orientations."""
    if not tables:
        raise ValueError("Fixed .pvc clustering requires at least one table")
    if isinstance(tolerance_deg, bool):
        raise ValueError("pvc_orientation_tolerance_deg must be a finite number")
    tol = _finite(
        tolerance_deg,
        "pvc_orientation_tolerance_deg",
        minimum=MIN_PVC_ORIENTATION_TOLERANCE_DEG,
        maximum=MAX_PVC_ORIENTATION_TOLERANCE_DEG,
    )

    tilts = [float(tbl["tilt_deg"]) for tbl in tables]
    azims = [float(tbl["azimuth_deg"]) for tbl in tables]
    weights = [float(max(1, int(tbl.get("module_count", 1)))) for tbl in tables]
    slopes = [float(tbl.get("base_slope_deg", 0.0)) for tbl in tables]
    mods_arr = [int(tbl.get("module_count", 0)) for tbl in tables]
    areas_arr = [float(tbl["long_shed_m"]) * float(tbl["larg_shed_m"]) for tbl in tables]
    sin_az = [w * math.sin(math.radians(a)) for w, a in zip(weights, azims)]
    cos_az = [w * math.cos(math.radians(a)) for w, a in zip(weights, azims)]

    bins: dict[tuple[float, float], list[int]] = {}
    for idx, (t, a) in enumerate(zip(tilts, azims)):
        key = (round(t, 3), round(normalize_azimuth_deg(a), 3))
        bins.setdefault(key, []).append(idx)

    active: dict[int, dict[str, Any]] = {}
    next_id = 0
    for indices in bins.values():
        w_sum = sum(weights[i] for i in indices)
        tilt_wsum = sum(tilts[i] * weights[i] for i in indices)
        sin_wsum = sum(sin_az[i] for i in indices)
        cos_wsum = sum(cos_az[i] for i in indices)
        slope_wsum = sum(slopes[i] * weights[i] for i in indices)
        fallback_az = azims[indices[0]]
        ct, ca, cs = _centroid_from_sums(w_sum, tilt_wsum, sin_wsum, cos_wsum, slope_wsum, fallback_az)
        max_spread = max(orientation_distance_deg(tilts[k], azims[k], ct, ca) for k in indices)
        active[next_id] = {
            "indices": list(indices), "w_sum": w_sum, "tilt_wsum": tilt_wsum,
            "sin_wsum": sin_wsum, "cos_wsum": cos_wsum, "slope_wsum": slope_wsum,
            "mods": sum(mods_arr[i] for i in indices), "area": sum(areas_arr[i] for i in indices),
            "fallback_az": fallback_az, "tilt": ct, "az": ca, "slope": cs, "max_spread": max_spread,
        }
        next_id += 1

    heap: list[tuple[float, int, int]] = []
    init_ids = list(active)
    for pos_a in range(len(init_ids)):
        id_a = init_ids[pos_a]
        ca = active[id_a]
        for pos_b in range(pos_a + 1, len(init_ids)):
            id_b = init_ids[pos_b]
            cb = active[id_b]
            if abs(ca["tilt"] - cb["tilt"]) > tol + 1e-9:
                continue
            dist = orientation_distance_deg(ca["tilt"], ca["az"], cb["tilt"], cb["az"])
            if dist <= tol + 1e-9:
                heapq.heappush(heap, (dist, id_a, id_b))

    while len(active) > 1 and heap:
        _, id_a, id_b = heapq.heappop(heap)
        if (ca := active.get(id_a)) is None or (cb := active.get(id_b)) is None:
            continue
        w_sum = ca["w_sum"] + cb["w_sum"]
        tilt_wsum = ca["tilt_wsum"] + cb["tilt_wsum"]
        sin_wsum = ca["sin_wsum"] + cb["sin_wsum"]
        cos_wsum = ca["cos_wsum"] + cb["cos_wsum"]
        slope_wsum = ca["slope_wsum"] + cb["slope_wsum"]
        fallback_az = ca["fallback_az"]
        mt, ma, ms = _centroid_from_sums(w_sum, tilt_wsum, sin_wsum, cos_wsum, slope_wsum, fallback_az)
        shift_a = orientation_distance_deg(ca["tilt"], ca["az"], mt, ma)
        shift_b = orientation_distance_deg(cb["tilt"], cb["az"], mt, ma)
        ub = max(ca["max_spread"] + shift_a, cb["max_spread"] + shift_b)
        merged_idx = ca["indices"] + cb["indices"]
        if ub <= tol + 1e-9:
            new_spread = ub
        else:
            exact_spread = max(orientation_distance_deg(tilts[k], azims[k], mt, ma) for k in merged_idx)
            if exact_spread > tol + 1e-9:
                continue
            new_spread = exact_spread

        del active[id_a], active[id_b]
        new_id = next_id
        next_id += 1
        merged = {
            "indices": merged_idx, "w_sum": w_sum, "tilt_wsum": tilt_wsum,
            "sin_wsum": sin_wsum, "cos_wsum": cos_wsum, "slope_wsum": slope_wsum,
            "mods": ca["mods"] + cb["mods"], "area": ca["area"] + cb["area"],
            "fallback_az": fallback_az, "tilt": mt, "az": ma, "slope": ms, "max_spread": new_spread,
        }
        for oid, other in active.items():
            if abs(other["tilt"] - mt) <= tol + 1e-9:
                d_new = orientation_distance_deg(other["tilt"], other["az"], mt, ma)
                if d_new <= tol + 1e-9:
                    heapq.heappush(heap, (d_new, min(oid, new_id), max(oid, new_id)))
        active[new_id] = merged

    clusters = list(active.values())

    if len(clusters) > max_orientations:
        raise ValueError(
            f"Fixed .pvc tables cluster into {len(clusters)} orientations at tolerance {tol:g} deg, "
            f"exceeding the maximum of {max_orientations}; increase ground_layout.pvc_orientation_tolerance_deg"
        )

    clusters.sort(key=lambda c: (-c["mods"], -c["area"], round(c["tilt"], 3), round(c["az"], 3)))
    total_mods = sum(c["mods"] for c in clusters)
    tagged = [dict(tbl) for tbl in tables]
    summaries: list[dict[str, Any]] = []

    for oid, cl in enumerate(clusters, start=1):
        cl_tables = []
        max_spread = 0.0
        for idx in cl["indices"]:
            tagged[idx]["orientation_id"] = oid
            cl_tables.append(tagged[idx])
            d = orientation_distance_deg(tagged[idx]["tilt_deg"], tagged[idx]["azimuth_deg"], cl["tilt"], cl["az"])
            if d > max_spread:
                max_spread = d
        tilt_r = round(cl["tilt"], 2)
        az_r = round(normalize_azimuth_deg(cl["az"]), 2)
        slope_r = round(cl["slope"], 2)
        summaries.append({
            "orientation_id": oid,
            "label": f"PVC_Orient_{oid}",
            "tilt_deg": tilt_r,
            "azimuth_deg": az_r,
            "base_slope_deg": slope_r,
            "table_count": len(cl_tables),
            "module_count": cl["mods"],
            "collector_area_m2": round(cl["area"], 2),
            "max_angular_spread_deg": round(max_spread, 3),
            "share_pct": round((cl["mods"] / total_mods) * 100.0, 2) if total_mods > 0 else 0.0,
            "pitch_estimate_m": estimate_pvc_pitch(cl_tables, tracker=False, az_deg=az_r),
        })
    return tagged, summaries


def _hamilton_allocate(weights: list[float], total: int, max_caps: list[int] | None = None) -> list[int]:
    """Allocate `total` integer units (>= 1 each) proportionally to positive `weights`."""
    count = len(weights)
    if total < count:
        raise ValueError(f"Cannot allocate {total} units across {count} buckets with at least 1 per bucket")
    caps = [total] * count if max_caps is None else list(max_caps)
    if len(caps) != count or any(c < 1 for c in caps) or sum(caps) < total:
        raise ValueError(f"Cannot allocate {total} units across {count} buckets within per-bucket limits")
    total_w = sum(weights) or float(count)
    ideals = [(w / total_w) * total for w in weights]
    alloc = [max(1, min(caps[i], int(math.floor(q)))) for i, q in enumerate(ideals)]
    while sum(alloc) < total:
        cands = [i for i in range(count) if alloc[i] < caps[i]]
        if not cands:
            raise ValueError(f"Cannot allocate {total} units across {count} buckets within per-bucket limits")
        best_i = max(cands, key=lambda i: (ideals[i] - alloc[i], -i))
        alloc[best_i] += 1
    while sum(alloc) > total:
        cands = [i for i in range(count) if alloc[i] > 1]
        best_i = max(cands, key=lambda i: (alloc[i] - ideals[i], i))
        alloc[best_i] -= 1
    return alloc


def _capacitated_hamilton_allocate(
    weights: list[float], total: int, max_caps: list[int] | None = None
) -> list[int] | None:
    """Return a proportional integer allocation respecting per-bucket upper limits, or None if infeasible."""
    try:
        return _hamilton_allocate(weights, total, max_caps=max_caps)
    except ValueError:
        return None


def _within_cluster_area_limit(cl: dict[str, Any], modules: int, module_area_m2: float | None) -> bool:
    if module_area_m2 is None or float(cl.get("collector_area_m2") or 0.0) <= 0.0:
        return True
    return modules * module_area_m2 <= float(cl["collector_area_m2"]) * 1.001 + 1e-9


def apportion_pvc_orientations(
    clusters: list[dict[str, Any]],
    *,
    n_mod_serie: int,
    n_string_ch: int,
    n_inverter: int,
    n_inv_mppt: int,
    independent_mppt: bool,
    module_area_m2: float | None = None,
) -> list[dict[str, Any]]:
    """Apportion inverters, MPPT inputs, and strings proportionally across clustered .pvc orientations."""
    k = len(clusters)
    if k == 0:
        raise ValueError("clusters must be non-empty")
    if module_area_m2 is not None and (isinstance(module_area_m2, bool) or not math.isfinite(module_area_m2) or module_area_m2 <= 0.0):
        raise ValueError("module_area_m2 must be a positive finite number")
    area_check = module_area_m2 is not None and all(float(c.get("collector_area_m2") or 0.0) > 0.0 for c in clusters)
    if k == 1:
        if area_check and not _within_cluster_area_limit(clusters[0], n_mod_serie * n_string_ch, module_area_m2):
            elec_a = n_mod_serie * n_string_ch * float(module_area_m2)
            cl_a = float(clusters[0]["collector_area_m2"])
            raise ValueError(
                f"Electrical module area ({elec_a:.2f} m2) exceeds .pvc orientation #1 collector area ({cl_a:.2f} m2)"
            )
        return [{
            "orientation_id": int(clusters[0]["orientation_id"]),
            "n_mod_serie": n_mod_serie,
            "n_inverter": n_inverter,
            "n_inv_mppt": n_inv_mppt,
            "n_string_ch": n_string_ch,
            "power_sharing_inverter_id": -1,
        }]

    weights = [float(max(1, int(c["module_count"]))) for c in clusters]
    total_w = sum(weights)
    target_shares = [w / total_w for w in weights]
    max_strings = (
        [int(math.floor((float(c["collector_area_m2"]) * 1.001 + 1e-9) / (n_mod_serie * float(module_area_m2)))) for c in clusters]
        if area_check else None
    )
    had_topology = False

    dedicated_plan: list[dict[str, Any]] | None = None
    dedicated_err = float("inf")
    if n_inverter >= k and n_string_ch % n_inverter == 0:
        mppt_ok = (not independent_mppt) or (n_inv_mppt % n_inverter == 0 and n_string_ch % n_inv_mppt == 0)
        if mppt_ok:
            had_topology = True
            mppt_per_inv = (n_inv_mppt // n_inverter) if independent_mppt else 1
            str_per_inv = n_string_ch // n_inverter
            inv_caps = [ms // str_per_inv for ms in max_strings] if max_strings is not None else None
            inv_alloc = _capacitated_hamilton_allocate(weights, n_inverter, inv_caps)
            if inv_alloc is not None and (
                not area_check or all(_within_cluster_area_limit(c, n_mod_serie * a * str_per_inv, module_area_m2) for c, a in zip(clusters, inv_alloc))
            ):
                dedicated_err = sum(abs(a / n_inverter - s) for a, s in zip(inv_alloc, target_shares))
                dedicated_plan = [
                    {
                        "orientation_id": int(c["orientation_id"]),
                        "n_mod_serie": n_mod_serie,
                        "n_inverter": a,
                        "n_inv_mppt": (a * mppt_per_inv) if independent_mppt else 1,
                        "n_string_ch": a * str_per_inv,
                        "power_sharing_inverter_id": -1,
                    }
                    for c, a in zip(clusters, inv_alloc)
                ]

    shared_plan: list[dict[str, Any]] | None = None
    shared_err = float("inf")
    if independent_mppt and n_inv_mppt % n_inverter == 0 and n_string_ch % n_inv_mppt == 0:
        mppt_per_inv = n_inv_mppt // n_inverter
        str_per_mppt = n_string_ch // n_inv_mppt
        if mppt_per_inv >= k:
            had_topology = True
            str_per_unit = n_inverter * str_per_mppt
            mppt_caps = [ms // str_per_unit for ms in max_strings] if max_strings is not None else None
            mppt_alloc = _capacitated_hamilton_allocate(weights, mppt_per_inv, mppt_caps)
            if mppt_alloc is not None and (
                not area_check or all(_within_cluster_area_limit(c, n_mod_serie * a * str_per_unit, module_area_m2) for c, a in zip(clusters, mppt_alloc))
            ):
                shared_err = sum(abs(a / mppt_per_inv - s) for a, s in zip(mppt_alloc, target_shares))
                shared_plan = [
                    {
                        "orientation_id": int(c["orientation_id"]),
                        "n_mod_serie": n_mod_serie,
                        "n_inverter": n_inverter,
                        "n_inv_mppt": n_inverter * a,
                        "n_string_ch": n_inverter * a * str_per_mppt,
                        "power_sharing_inverter_id": 0,
                    }
                    for c, a in zip(clusters, mppt_alloc)
                ]

    if dedicated_plan is None and shared_plan is None:
        if had_topology and area_check:
            raise ValueError(
                f"Cannot apportion {k} .pvc orientations across {n_inverter} inverter(s) and {n_inv_mppt} MPPT input(s) "
                f"within per-orientation 3D collector area limits; adjust target_dc_kw, inverter MPPT granularity, "
                f"or pvc_orientation_tolerance_deg"
            )
        raise ValueError(
            f"Cannot apportion {k} .pvc orientations across {n_inverter} inverter(s) and {n_inv_mppt} MPPT input(s); "
            f"require at least {k} inverters or {k} MPPT inputs per inverter with divisible strings"
        )
    if shared_plan is not None and (dedicated_plan is None or shared_err + 0.05 < dedicated_err):
        return shared_plan
    assert dedicated_plan is not None
    return dedicated_plan


def build_pvc_multi_orient_synthesis_blocks(
    clusters: list[dict[str, Any]],
    *,
    field_type: str,
    pan_name: str,
    ond_name: str,
    n_mod_serie: int,
    n_string_ch: int,
    n_inverter: int,
    n_inv_mppt: int,
    independent_mppt: bool,
    pnom_ac_in_mppt: float,
    lid_loss: float,
    pmism_mpp: float | None,
    module_quality_loss: float | None,
    dc_wiring_loss_pct: float,
    dc_wiring_resistance_mohm: float,
    bifacial: dict[str, Any] | None = None,
    module_area_m2: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build synchronized Orientations, SubArrays, and summary metadata for multi-orientation fixed .pvc scenes."""
    allocs = apportion_pvc_orientations(
        clusters,
        n_mod_serie=n_mod_serie,
        n_string_ch=n_string_ch,
        n_inverter=n_inverter,
        n_inv_mppt=n_inv_mppt,
        independent_mppt=independent_mppt,
        module_area_m2=module_area_m2,
    )
    orientations: list[dict[str, Any]] = []
    subarrays: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    total_elec_mods = sum(a["n_mod_serie"] * a["n_string_ch"] for a in allocs)

    for cl, alloc in zip(clusters, allocs):
        oid = int(cl["orientation_id"])
        tilt = float(cl["tilt_deg"])
        azim = float(cl["azimuth_deg"])
        slope = float(cl.get("base_slope_deg", 0.0))
        label = _clean_text(cl.get("label") or f"PVC_Orient_{oid}", f"pvc_orientations[{oid - 1}].label")
        orientations.append({
            "id": oid,
            "field_type": field_type,
            "tilt_deg": tilt,
            "azimuth_deg": azim,
            "base_slope_deg": slope,
            "comment": f"{label}, Tilt {_fmt_angle(tilt)} deg, Azim. {_fmt_angle(azim)} deg",
            "bifacial": bifacial,
        })
        subarrays.append({
            "subarray_id": oid,
            "orientation_id": oid,
            "comment": f"{label} ({_fmt_angle(tilt)}/{_fmt_angle(azim)})",
            "pan_name": pan_name,
            "ond_name": ond_name,
            "n_mod_serie": alloc["n_mod_serie"],
            "n_string_ch": alloc["n_string_ch"],
            "n_inverter": alloc["n_inverter"],
            "n_inv_mppt": alloc["n_inv_mppt"],
            "independent_mppt": independent_mppt,
            "power_sharing_inverter_id": alloc["power_sharing_inverter_id"],
            "pnom_ac_in_mppt": pnom_ac_in_mppt,
            "lid_loss": lid_loss,
            **({"pmism_mpp": pmism_mpp} if pmism_mpp is not None else {}),
            **({"module_quality_loss": module_quality_loss} if module_quality_loss is not None else {}),
            "dc_wiring_loss_pct": dc_wiring_loss_pct,
            "dc_wiring_resistance_mohm": dc_wiring_resistance_mohm,
        })
        elec_mods = alloc["n_mod_serie"] * alloc["n_string_ch"]
        summaries.append({
            "orientation_id": oid,
            "subarray_id": oid,
            "label": label,
            "tilt_deg": tilt,
            "azimuth_deg": azim,
            "base_slope_deg": slope,
            "table_count": int(cl["table_count"]),
            "scene_module_count": int(cl["module_count"]),
            "collector_area_m2": float(cl["collector_area_m2"]),
            "max_angular_spread_deg": float(cl.get("max_angular_spread_deg", 0.0)),
            "inverter_count": alloc["n_inverter"],
            "mppt_count": alloc["n_inv_mppt"],
            "string_count": alloc["n_string_ch"],
            "module_count": elec_mods,
            "power_sharing_inverter_id": alloc["power_sharing_inverter_id"],
            "share_pct": round((elec_mods / total_elec_mods) * 100.0, 2) if total_elec_mods > 0 else 0.0,
        })
    return orientations, subarrays, summaries

"""Mixed .pvc subset filtering, fixed-table multi-orientation clustering, and electrical apportionment."""
from __future__ import annotations

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


def _cluster_centroid(tables: list[dict[str, Any]], indices: list[int]) -> tuple[float, float, float, int, float]:
    weights = [float(max(1, int(tables[i].get("module_count", 1)))) for i in indices]
    total_w = sum(weights)
    tilt = sum(float(tables[i]["tilt_deg"]) * w for i, w in zip(indices, weights)) / total_w
    az = weighted_circular_mean_deg([float(tables[i]["azimuth_deg"]) for i in indices], weights)
    slope = sum(float(tables[i].get("base_slope_deg", 0.0)) * w for i, w in zip(indices, weights)) / total_w
    slope = max(-45.0, min(45.0, slope))
    mods = sum(int(tables[i].get("module_count", 0)) for i in indices)
    area = sum(float(tables[i]["long_shed_m"]) * float(tables[i]["larg_shed_m"]) for i in indices)
    return tilt, az, slope, mods, area


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

    bins: dict[tuple[float, float], list[int]] = {}
    for idx, tbl in enumerate(tables):
        key = (round(float(tbl["tilt_deg"]), 3), round(normalize_azimuth_deg(float(tbl["azimuth_deg"])), 3))
        bins.setdefault(key, []).append(idx)

    clusters: list[dict[str, Any]] = []
    for indices in bins.values():
        tilt, az, slope, mods, area = _cluster_centroid(tables, indices)
        clusters.append({"indices": list(indices), "tilt": tilt, "az": az, "slope": slope, "mods": mods, "area": area})

    while len(clusters) > 1:
        best_merge: tuple[float, int, int, tuple[float, float, float, int, float]] | None = None
        for i in range(len(clusters)):
            ci = clusters[i]
            for j in range(i + 1, len(clusters)):
                cj = clusters[j]
                dist = orientation_distance_deg(ci["tilt"], ci["az"], cj["tilt"], cj["az"])
                if dist > tol + 1e-9:
                    continue
                merged_idx = ci["indices"] + cj["indices"]
                mt, ma, ms, mm, marea = _cluster_centroid(tables, merged_idx)
                if any(orientation_distance_deg(tables[k]["tilt_deg"], tables[k]["azimuth_deg"], mt, ma) > tol + 1e-9 for k in merged_idx):
                    continue
                cand = (dist, i, j, (mt, ma, ms, mm, marea))
                if best_merge is None or cand[:3] < best_merge[:3]:
                    best_merge = cand
        if best_merge is None:
            break
        _, i, j, (mt, ma, ms, mm, marea) = best_merge
        clusters[i] = {
            "indices": clusters[i]["indices"] + clusters[j]["indices"],
            "tilt": mt, "az": ma, "slope": ms, "mods": mm, "area": marea,
        }
        clusters.pop(j)

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


def _hamilton_allocate(weights: list[float], total: int) -> list[int]:
    """Allocate `total` integer units (>= 1 each) proportionally to positive `weights`."""
    count = len(weights)
    if total < count:
        raise ValueError(f"Cannot allocate {total} units across {count} buckets with at least 1 per bucket")
    total_w = sum(weights) or float(count)
    ideals = [(w / total_w) * total for w in weights]
    alloc = [max(1, int(math.floor(q))) for q in ideals]
    while sum(alloc) < total:
        best_i = max(range(count), key=lambda i: (ideals[i] - alloc[i], -i))
        alloc[best_i] += 1
    while sum(alloc) > total:
        cands = [i for i in range(count) if alloc[i] > 1]
        best_i = max(cands, key=lambda i: (alloc[i] - ideals[i], i))
        alloc[best_i] -= 1
    return alloc


def apportion_pvc_orientations(
    clusters: list[dict[str, Any]],
    *,
    n_mod_serie: int,
    n_string_ch: int,
    n_inverter: int,
    n_inv_mppt: int,
    independent_mppt: bool,
) -> list[dict[str, Any]]:
    """Apportion inverters, MPPT inputs, and strings proportionally across clustered .pvc orientations."""
    k = len(clusters)
    if k == 0:
        raise ValueError("clusters must be non-empty")
    if k == 1:
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

    dedicated_plan: list[dict[str, Any]] | None = None
    dedicated_err = float("inf")
    if n_inverter >= k and n_string_ch % n_inverter == 0:
        mppt_ok = (not independent_mppt) or (n_inv_mppt % n_inverter == 0 and n_string_ch % n_inv_mppt == 0)
        if mppt_ok:
            mppt_per_inv = (n_inv_mppt // n_inverter) if independent_mppt else 1
            str_per_inv = n_string_ch // n_inverter
            inv_alloc = _hamilton_allocate(weights, n_inverter)
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
            mppt_alloc = _hamilton_allocate(weights, mppt_per_inv)
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build synchronized Orientations, SubArrays, and summary metadata for multi-orientation fixed .pvc scenes."""
    allocs = apportion_pvc_orientations(
        clusters,
        n_mod_serie=n_mod_serie,
        n_string_ch=n_string_ch,
        n_inverter=n_inverter,
        n_inv_mppt=n_inv_mppt,
        independent_mppt=independent_mppt,
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

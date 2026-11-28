"""Validation, component sizing seed, and orientation apportionment for utility designs."""
from __future__ import annotations

import math
from pathlib import Path

from pvsyst_auto.components.editor import pan_validation_fields
from pvsyst_auto.engineering.sizing import plan_capacity
from pvsyst_auto.project.bifacial import normalize_bifacial_spec
from pvsyst_auto.project.synthesize import synthesize_project_and_variant
from pvsyst_auto.project.template import _mppt_inputs
from pvsyst_auto.scene.partition import is_partition_shading_requested, normalize_electrical_shading
from pvsyst_auto.validation import is_finite_number as _is_number, is_strict_int as _is_int

UTILITY_SCOPES = ("ground_sheds", "ground_tracker", "ground_dome")
GROUND_LAYOUT_KEYS = frozenset((
    "model_mode", "pitch_m", "collector_width_m", "shed_count",
    "module_rows", "module_columns", "module_orientation",
    "spacing_x_m", "spacing_y_m",
    "inactive_band_width_1_m", "inactive_band_width_2_m",
    "electrical_effect", "shading_factor", "cell_width_m",
    "axis_azimuth_deg", "min_phi_deg", "max_phi_deg",
    "backtracking", "irrad_optimiz", "central_gap_m", "pitch_slope_deg",
    "wind_stow", "wind_stow_speed_ms", "wind_stow_position_deg",
    "site_polygon", "exclusion_zones", "setback_m", "ground_clearance_m", "max_table_columns",
    "obstacles", "base_slope_deg", "terrain", "sub_orientations",
    "top_spacing_m", "dome_wiring",
    "pvc_name", "pvc_subset", "pvc_orientation_tolerance_deg",
    "dxf_name", "dxf_layers", "dxf_unit_scale",
    "electrical_shading",
))
SUB_ORIENTATION_KEYS = frozenset((
    "label", "tilt", "azimuth", "target_dc_kw", "weight",
    "pitch_m", "collector_width_m", "shed_count",
    "inactive_band_width_1_m", "inactive_band_width_2_m",
    "electrical_effect", "shading_factor", "cell_width_m", "base_slope_deg",
))
TRACKER_ONLY_KEYS = frozenset((
    "axis_azimuth_deg", "min_phi_deg", "max_phi_deg",
    "backtracking", "irrad_optimiz", "central_gap_m", "pitch_slope_deg",
    "wind_stow", "wind_stow_speed_ms", "wind_stow_position_deg",
))
DOME_ONLY_KEYS = frozenset(("top_spacing_m", "dome_wiring"))
BIFACIAL_EXTRA_VARS = (
    "viGlobGnd", "viDiffGnd", "viGlobBak",
    "viBeamBak", "viDifSBak", "viBmSFBak", "viMismBak", "viPRBifi",
)


def resolve_utility_field_type(scope: str, ground_layout: dict | None) -> tuple[str, bool]:
    """Map (scope, ground_layout.model_mode) to native (field_type, include_3d_scene)."""
    layout = ground_layout or {}
    if scope == "ground_dome":
        return ("Dome", True)
    has_3d = (
        layout.get("model_mode", "unlimited") == "3d"
        or layout.get("site_polygon") is not None
        or bool(layout.get("exclusion_zones"))
        or bool(layout.get("obstacles"))
        or layout.get("terrain") is not None
        or layout.get("pvc_name") is not None
        or layout.get("dxf_name") is not None
        or is_partition_shading_requested(layout.get("electrical_shading"))
    )
    if scope == "ground_sheds":
        return ("RegularSheds", True) if has_3d else ("Sheds", False)
    if scope == "ground_tracker":
        return ("TrackHorizNSAxis", True) if has_3d else ("UnlimTracking", False)
    raise ValueError(f"Not a utility scope: {scope!r}")


def validate_utility_spec(spec: dict, scope: str, issues: list[dict]) -> None:
    """Validate ground_sheds / ground_tracker specific spec constraints."""
    def bad(field: str, message) -> None:
        issues.append({"field": field, "message": str(message)})

    if spec.get("template_project") is not None or spec.get("variant") is not None:
        bad("template_project",
            f"scope={scope!r} synthesizes from scratch and does not accept template_project or variant")

    raw_layout = spec.get("ground_layout")
    if raw_layout is not None and not isinstance(raw_layout, dict):
        bad("ground_layout", "Provide ground_layout as an object")
        layout = {}
    else:
        layout = dict(raw_layout or {})

    for key in sorted(set(layout) - GROUND_LAYOUT_KEYS):
        bad(f"ground_layout.{key}", "Unknown ground_layout parameter")

    implied_3d = resolve_utility_field_type(scope, layout)[1] if scope in UTILITY_SCOPES else False
    mode = layout.get("model_mode") or ("3d" if (scope == "ground_dome" or implied_3d) else "unlimited")
    if mode not in ("unlimited", "3d"):
        bad("ground_layout.model_mode", "Choose 'unlimited' or '3d'")

    if scope == "ground_dome" and layout.get("model_mode") == "unlimited":
        bad("ground_layout.model_mode", "scope='ground_dome' requires 3D paired dome shading (model_mode='3d')")
    elif layout.get("model_mode") == "unlimited" and (layout.get("site_polygon") is not None or bool(layout.get("exclusion_zones")) or layout.get("obstacles") or layout.get("terrain") is not None):
        bad("ground_layout.model_mode", "site_polygon, exclusion_zones, obstacles, and terrain require 3D layout (omit model_mode or set model_mode='3d')")
    elif mode == "unlimited" and scope == "ground_tracker":
        for key in ("central_gap_m", "pitch_slope_deg", "wind_stow", "wind_stow_speed_ms", "wind_stow_position_deg"):
            if key in layout:
                bad(f"ground_layout.{key}", f"{key} requires model_mode='3d'")
    elif mode == "3d":
        for key in ("electrical_effect", "shading_factor", "cell_width_m"):
            if key in layout:
                bad(f"ground_layout.{key}", f"{key} requires model_mode='unlimited'")
        if scope in ("ground_sheds", "ground_dome"):
            for key in ("inactive_band_width_1_m", "inactive_band_width_2_m"):
                if key in layout:
                    bad(f"ground_layout.{key}", f"{key} requires model_mode='unlimited' for {scope}")

    from pvsyst_auto.scene.cad_import import validate_cad_ground_layout
    validate_cad_ground_layout(layout, scope, mode, bad)
    if (raw_es := layout.get("electrical_shading")) is not None:
        try:
            normalize_electrical_shading(raw_es)
        except ValueError as exc:
            bad("ground_layout.electrical_shading", exc)
        if mode == "unlimited" or layout.get("model_mode") == "unlimited":
            bad(
                "ground_layout.electrical_shading",
                "electrical_shading requires 3D layout (omit model_mode or set model_mode='3d'); "
                "use electrical_effect/shading_factor/cell_width_m for model_mode='unlimited'",
            )

    if scope != "ground_dome":
        for key in sorted(set(layout) & DOME_ONLY_KEYS):
            bad(f"ground_layout.{key}", f"{key} is only supported for scope='ground_dome'")
    else:
        ts = layout.get("top_spacing_m")
        if ts is not None and (not _is_number(ts) or ts < 0):
            bad("ground_layout.top_spacing_m", "top_spacing_m must be a non-negative finite number")
        dw = layout.get("dome_wiring")
        if dw is not None and dw not in ("auto", "dual_mppt", "mixed"):
            bad("ground_layout.dome_wiring", "Choose 'auto', 'dual_mppt', or 'mixed'")

    sub_orientations = layout.get("sub_orientations")
    has_sub_orients = isinstance(sub_orientations, list) and len(sub_orientations) > 0
    if sub_orientations is not None:
        if scope != "ground_sheds":
            bad("ground_layout.sub_orientations", "sub_orientations is only supported for scope='ground_sheds'")
        elif mode != "unlimited" or layout.get("site_polygon") is not None or bool(layout.get("exclusion_zones")) or layout.get("obstacles") or layout.get("terrain") is not None:
            bad("ground_layout.sub_orientations",
                "sub_orientations requires scope='ground_sheds' with model_mode='unlimited' (no 3D scene/polygon/terrain)")
        elif not isinstance(sub_orientations, list) or not 1 <= len(sub_orientations) <= 16:
            bad("ground_layout.sub_orientations", "Provide 1..16 sub_orientations entries")
        else:
            share_modes: set[str] = set()
            for idx_o, item in enumerate(sub_orientations):
                prefix = f"ground_layout.sub_orientations[{idx_o}]"
                if not isinstance(item, dict):
                    bad(prefix, "Each sub-orientation must be an object")
                    continue
                for key in sorted(set(item) - SUB_ORIENTATION_KEYS):
                    bad(f"{prefix}.{key}", "Unknown sub_orientation parameter")
                t_val = item.get("tilt")
                if t_val is None or not _is_number(t_val) or not 0 <= t_val <= 90:
                    bad(f"{prefix}.tilt", "Use a finite tilt in 0..90 degrees")
                a_val = item.get("azimuth")
                if a_val is None or not _is_number(a_val) or not -180 <= a_val <= 180:
                    bad(f"{prefix}.azimuth", "Use finite PVsyst azimuth in -180..180 degrees, zero south and west positive")
                has_tdc = item.get("target_dc_kw") is not None
                has_w = item.get("weight") is not None
                if has_tdc and has_w:
                    bad(prefix, "Specify either target_dc_kw or weight, not both")
                elif not has_tdc and not has_w:
                    bad(prefix, "Specify either positive target_dc_kw or positive weight")
                elif has_tdc:
                    share_modes.add("target_dc_kw")
                    if not _is_number(item["target_dc_kw"]) or item["target_dc_kw"] <= 0:
                        bad(f"{prefix}.target_dc_kw", "target_dc_kw must be a positive finite number")
                else:
                    share_modes.add("weight")
                    if not _is_number(item["weight"]) or item["weight"] <= 0:
                        bad(f"{prefix}.weight", "weight must be a positive finite number")
                sub_p = item.get("pitch_m")
                if sub_p is not None and (not _is_number(sub_p) or sub_p <= 0):
                    bad(f"{prefix}.pitch_m", "pitch_m must be a positive finite number")
                sub_cw = item.get("collector_width_m")
                if sub_cw is not None and (not _is_number(sub_cw) or sub_cw <= 0):
                    bad(f"{prefix}.collector_width_m", "collector_width_m must be a positive finite number")
                if _is_number(sub_p) and _is_number(sub_cw) and float(sub_p) <= float(sub_cw):
                    bad(f"{prefix}.pitch_m", "pitch_m must exceed collector_width_m (GCR < 1.0)")
                if item.get("shed_count") is not None and (not _is_int(item["shed_count"]) or not 1 <= item["shed_count"] <= 1000):
                    bad(f"{prefix}.shed_count", "shed_count must be an integer in 1..1000")
                for key in ("inactive_band_width_1_m", "inactive_band_width_2_m"):
                    if item.get(key) is not None and (not _is_number(item[key]) or item[key] < 0):
                        bad(f"{prefix}.{key}", f"{key} must be a nonnegative finite number")
                if item.get("electrical_effect") is not None and type(item["electrical_effect"]) is not bool:
                    bad(f"{prefix}.electrical_effect", "electrical_effect must be a strict boolean")
                if item.get("shading_factor") is not None and (not _is_number(item["shading_factor"]) or not 0 <= item["shading_factor"] <= 1):
                    bad(f"{prefix}.shading_factor", "shading_factor must lie in 0..1")
                if item.get("cell_width_m") is not None and (not _is_number(item["cell_width_m"]) or item["cell_width_m"] < 0.01):
                    bad(f"{prefix}.cell_width_m", "cell_width_m must be >= 0.01 m")
                if item.get("base_slope_deg") is not None and (not _is_number(item["base_slope_deg"]) or not -45 <= item["base_slope_deg"] <= 45):
                    bad(f"{prefix}.base_slope_deg", "base_slope_deg must lie in -45..45 degrees")
                elif item.get("base_slope_deg"):
                    bad(f"{prefix}.base_slope_deg", "Nonzero base_slope_deg requires a 3D ground-sheds layout")
            if len(share_modes) > 1:
                bad("ground_layout.sub_orientations",
                    "Use either target_dc_kw on all sub_orientations or weight on all sub_orientations, not a mix")

    if scope in ("ground_sheds", "ground_dome"):
        for name, low, high in (("tilt", 0, 90), ("azimuth", -180, 180)):
            value = spec.get(name)
            note = ("Use a finite tilt in (0, 90] degrees" if scope == "ground_dome" and name == "tilt"
                    else "Use a finite tilt in 0..90 degrees" if name == "tilt"
                    else "Use finite PVsyst azimuth in -180..180 degrees, zero south and west positive")
            if value is None:
                if not has_sub_orients and not layout.get("pvc_name"):
                    bad(name, note)
            elif not _is_number(value) or not low <= value <= high or (scope == "ground_dome" and name == "tilt" and value <= 0):
                bad(name, note)
        for key in sorted(set(layout) & TRACKER_ONLY_KEYS):
            bad(f"ground_layout.{key}", f"{key} is only supported for scope='ground_tracker'")
    else:
        if spec.get("tilt") is not None:
            bad("tilt", "scope='ground_tracker' uses horizontal single-axis tracking "
                        "(min_phi_deg/max_phi_deg in ground_layout), not fixed tilt")
        azim = spec.get("azimuth")
        axis_azim = layout.get("axis_azimuth_deg")
        if azim is not None and (not _is_number(azim) or not -180 <= azim <= 180):
            bad("azimuth", "Use finite PVsyst azimuth in -180..180 degrees, zero south and west positive")
        if axis_azim is not None and (not _is_number(axis_azim) or not -180 <= axis_azim <= 180):
            bad("ground_layout.axis_azimuth_deg", "Use finite axis_azimuth_deg in -180..180 degrees")
        if (_is_number(azim) and _is_number(axis_azim)
                and not math.isclose(float(azim), float(axis_azim), abs_tol=1e-9)):
            bad("ground_layout.axis_azimuth_deg",
                f"ground_layout.axis_azimuth_deg ({axis_azim}) contradicts spec.azimuth ({azim})")

    pitch = layout.get("pitch_m")
    if pitch is not None and (not _is_number(pitch) or pitch <= 0):
        bad("ground_layout.pitch_m", "pitch_m must be a positive finite number")
    cw = layout.get("collector_width_m")
    if cw is not None and (not _is_number(cw) or cw <= 0):
        bad("ground_layout.collector_width_m", "collector_width_m must be a positive finite number")
    if _is_number(pitch) and _is_number(cw) and float(pitch) <= float(cw):
        bad("ground_layout.pitch_m", "pitch_m must exceed collector_width_m (GCR < 1.0)")

    for key, low, high in (("shed_count", 1, 1000), ("module_rows", 1, 20), ("module_columns", 1, 500)):
        val = layout.get(key)
        if val is not None and (not _is_int(val) or not low <= val <= high):
            bad(f"ground_layout.{key}", f"{key} must be an integer in {low}..{high}")

    mod_orient = layout.get("module_orientation", "portrait")
    if mod_orient not in ("portrait", "landscape"):
        bad("ground_layout.module_orientation", "Choose 'portrait' or 'landscape'")

    for key in ("spacing_x_m", "spacing_y_m", "inactive_band_width_1_m", "inactive_band_width_2_m",
                "central_gap_m", "wind_stow_speed_ms"):
        val = layout.get(key)
        if val is not None and (not _is_number(val) or val < 0):
            bad(f"ground_layout.{key}", f"{key} must be a nonnegative finite number")

    if layout.get("cell_width_m") is not None:
        val = layout["cell_width_m"]
        if not _is_number(val) or val < 0.01:
            bad("ground_layout.cell_width_m", "cell_width_m must be >= 0.01 m")
    if layout.get("shading_factor") is not None:
        val = layout["shading_factor"]
        if not _is_number(val) or not 0 <= val <= 1:
            bad("ground_layout.shading_factor", "shading_factor must lie in 0..1")
    for key in ("electrical_effect", "backtracking", "irrad_optimiz", "wind_stow"):
        val = layout.get(key)
        if val is not None and type(val) is not bool:
            bad(f"ground_layout.{key}", f"{key} must be a strict boolean")

    min_phi = layout.get("min_phi_deg", -60.0)
    max_phi = layout.get("max_phi_deg", 60.0)
    if not _is_number(min_phi) or not -90 <= min_phi <= 0:
        bad("ground_layout.min_phi_deg", "min_phi_deg must lie in -90..0 degrees")
    if not _is_number(max_phi) or not 0 <= max_phi <= 90:
        bad("ground_layout.max_phi_deg", "max_phi_deg must lie in 0..90 degrees")
    if _is_number(min_phi) and _is_number(max_phi) and not float(min_phi) < float(max_phi):
        bad("ground_layout.min_phi_deg", "min_phi_deg must be less than max_phi_deg")

    if layout.get("pitch_slope_deg") is not None:
        val = layout["pitch_slope_deg"]
        if not _is_number(val) or not -45 <= val <= 45:
            bad("ground_layout.pitch_slope_deg", "pitch_slope_deg must lie in -45..45 degrees")
    if layout.get("wind_stow_position_deg") is not None:
        val = layout["wind_stow_position_deg"]
        if not _is_number(val) or not -90 <= val <= 90:
            bad("ground_layout.wind_stow_position_deg", "wind_stow_position_deg must lie in -90..90 degrees")

    if layout.get("site_polygon") is not None:
        from pvsyst_auto.scene.reader import validate_site_polygon
        try:
            validate_site_polygon(layout["site_polygon"])
        except ValueError as exc:
            bad("ground_layout.site_polygon", exc)
    if layout.get("exclusion_zones") is not None:
        from pvsyst_auto.scene.exclusions import validate_exclusion_zones
        try:
            validated_ex = validate_exclusion_zones(layout["exclusion_zones"])
            if validated_ex and layout.get("site_polygon") is None and layout.get("dxf_name") is None and layout.get("pvc_name") is None:
                bad("ground_layout.exclusion_zones", "exclusion_zones requires ground_layout.site_polygon or ground_layout.dxf_name")
        except ValueError as exc:
            bad("ground_layout.exclusion_zones", exc)
    if layout.get("obstacles") is not None:
        from pvsyst_auto.scene.author import obstacle_profiles
        try:
            obstacle_profiles(layout["obstacles"])
        except ValueError as exc:
            bad("ground_layout.obstacles", exc)
    for key in ("setback_m", "ground_clearance_m"):
        val = layout.get(key)
        if val is not None and (not _is_number(val) or val < 0):
            bad(f"ground_layout.{key}", f"{key} must be a non-negative finite number")
    if layout.get("max_table_columns") is not None:
        val = layout["max_table_columns"]
        if not _is_int(val) or not 1 <= val <= 500:
            bad("ground_layout.max_table_columns", "max_table_columns must be an integer in 1..500")
    if layout.get("base_slope_deg") is not None:
        val = layout["base_slope_deg"]
        if scope not in ("ground_sheds", "ground_dome"):
            bad("ground_layout.base_slope_deg", "base_slope_deg is supported for scope='ground_sheds' and 'ground_dome'; use pitch_slope_deg for trackers")
        elif not _is_number(val) or not -45 <= val <= 45:
            bad("ground_layout.base_slope_deg", "base_slope_deg must lie in -45..45 degrees")
        elif val and not resolve_utility_field_type(scope, layout)[1]:
            bad("ground_layout.base_slope_deg", "Nonzero base_slope_deg requires a 3D ground layout")

    bifacial = spec.get("bifacial")
    if bifacial is not None:
        if scope == "ground_dome":
            bad("bifacial", "bifacial is not currently supported for scope='ground_dome'")
        elif mode in ("unlimited", "3d"):
            field_type, _ = resolve_utility_field_type(scope, layout)
            try:
                normalize_bifacial_spec(bifacial, field_type=field_type)
            except ValueError as exc:
                bad("bifacial", exc)


def _pan_physical_fields(store, pan_info: dict) -> dict:
    """Read pan_validation_fields from the discovered loose PAN file."""
    pan_path = Path(pan_info["path"]) if pan_info.get("path") else None
    if pan_path is None or not pan_path.is_file():
        pan_name = Path(pan_info["name"]).name
        library = pan_info.get("library") or "workspace"
        pan_path = store.components._path("PAN", pan_name, library)
    raw = store.components._read(pan_path)
    return pan_validation_fields(raw.decode("utf-8-sig"))


def _pan_is_bifacial(store, pan_info: dict) -> bool | None:
    """Return True/False for readable text .PAN based on TPVModule.isBiFacial (Flags bit 23, $00800000)."""
    from pvsyst_auto.project.bifacial import pan_is_bifacial

    return pan_is_bifacial(
        store.components,
        pan_info.get("name", ""),
        path=pan_info.get("path"),
        library=pan_info.get("library"),
    )


def _ond_pnom_kw(ond_electrical: dict) -> float:
    field = (ond_electrical.get("fields") or {}).get("PNomConv")
    if isinstance(field, dict) and field.get("status") == "declared":
        val = float(field["value"])
        if math.isfinite(val) and val > 0:
            return val
    raise ValueError("OND must declare a positive PNomConv")


def plan_utility_capacity(store, pan_info: dict, ond_info: dict, flat_capacity: dict, *, require_even_strings: bool = False) -> dict:
    """Run strict plan_capacity against an in-memory synthesized single-subarray seed."""
    pan_name = Path(pan_info["name"]).name
    ond_name = Path(ond_info["name"]).name
    ond_electrical = ond_info["electrical"]
    pan_fields = _pan_physical_fields(store, pan_info)
    mppt_inputs = _mppt_inputs(ond_electrical)
    pnom_ac = _ond_pnom_kw(ond_electrical)
    synth = synthesize_project_and_variant(
        store=None,
        project_comment="Utility Capacity Seed",
        variant_comment="Capacity Seed",
        pan_name=pan_name,
        ond_name=ond_name,
        n_mod_serie=15,
        n_string_ch=mppt_inputs,
        n_inverter=1,
        n_inv_mppt=mppt_inputs,
        independent_mppt=True,
        pnom_ac_in_mppt=pnom_ac,
        tilt_deg=20.0,
        azimuth_deg=0.0,
        with_bom=True,
    )
    _, lines = store._text(synth["vc0_text"].encode("utf-8"))
    return plan_capacity(store, lines, 1, pan_fields, ond_electrical, flat_capacity, require_even_strings=require_even_strings)


def _apportion_sub_orientations(sub_orientations: list[dict], plan: dict) -> list[dict]:
    """Apportion total inverter/MPPT/string counts across multi-orientation ground sheds."""
    count = len(sub_orientations)
    total_inv = int(plan["inverter_count"])
    mppt_per_inv = int(plan["mppt_per_inverter"])
    total_strings = int(plan["string_count"])
    n_mod_serie = int(plan["series_modules"])
    if total_inv < count:
        raise ValueError(
            f"Total inverter_count ({total_inv}) must be at least the number of sub_orientations ({count}) "
            "so every orientation subarray owns at least 1 dedicated inverter"
        )
    if total_strings % (total_inv * mppt_per_inv) != 0:
        raise ValueError(
            f"Total string_count ({total_strings}) must be divisible by inverter_count * mppt_per_inverter "
            f"({total_inv * mppt_per_inv}) to split cleanly across sub_orientations"
        )
    str_per_inv = total_strings // total_inv
    weights = [
        float(item["target_dc_kw"]) if item.get("target_dc_kw") is not None else float(item["weight"])
        for item in sub_orientations
    ]
    total_w = sum(weights)
    ideals = [(w / total_w) * total_inv for w in weights]
    alloc = [max(1, int(math.floor(q))) for q in ideals]
    while sum(alloc) < total_inv:
        best_i = max(range(count), key=lambda i: ideals[i] - alloc[i])
        alloc[best_i] += 1
    while sum(alloc) > total_inv:
        candidates = [i for i in range(count) if alloc[i] > 1]
        best_i = max(candidates, key=lambda i: alloc[i] - ideals[i])
        alloc[best_i] -= 1

    resolved: list[dict] = []
    for item, inv_k in zip(sub_orientations, alloc):
        entry = {
            "tilt_deg": float(item["tilt"]),
            "azimuth_deg": float(item["azimuth"]),
            "n_mod_serie": n_mod_serie,
            "n_inverter": inv_k,
            "n_inv_mppt": inv_k * mppt_per_inv,
            "n_string_ch": inv_k * str_per_inv,
        }
        if item.get("label") is not None:
            entry["label"] = item["label"]
        for opt_key in (
            "pitch_m", "collector_width_m", "shed_count",
            "inactive_band_width_1_m", "inactive_band_width_2_m",
            "electrical_effect", "shading_factor", "cell_width_m", "base_slope_deg",
        ):
            if item.get(opt_key) is not None:
                entry[opt_key] = item[opt_key]
        resolved.append(entry)
    return resolved

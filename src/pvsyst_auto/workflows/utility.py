"""Zero-template utility ground sheds and single-axis tracker design workflow (mono or bifacial)."""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

from pvsyst_auto.components.editor import pan_validation_fields
from pvsyst_auto.engineering.sizing import plan_capacity
from pvsyst_auto.native.cli import (
    PVsystError,
    VAR_GROUPS,
    build_sfi,
    iter_result_csv,
    result_units,
    summarize_results,
)
from pvsyst_auto.project.bifacial import normalize_bifacial_spec
from pvsyst_auto.project.paths import project_path, workspace_file, write_json
from pvsyst_auto.project.synthesize import synthesize_project_and_variant
from pvsyst_auto.project.template import synthesize_utility_project
from pvsyst_auto.project.template import _mppt_inputs
from pvsyst_auto.results import IRRAD_UNIT_KIND, POWER_UNIT_KIND, fingerprint
from pvsyst_auto.validation import is_finite_number as _is_number, is_strict_int as _is_int
from pvsyst_auto.weather.preparation import inspect_met_calendar
from pvsyst_auto.workflows.simulation import (
    collect_simulation_weather_assets,
    native_cli_weather_inputs,
    record_workflow_failure,
    verify_and_read_simulation_results,
)

UTILITY_SCOPES = ("ground_sheds", "ground_tracker")
GROUND_LAYOUT_KEYS = frozenset((
    "model_mode", "pitch_m", "collector_width_m", "shed_count",
    "module_rows", "module_columns", "module_orientation",
    "spacing_x_m", "spacing_y_m",
    "inactive_band_width_1_m", "inactive_band_width_2_m",
    "electrical_effect", "shading_factor", "cell_width_m",
    "axis_azimuth_deg", "min_phi_deg", "max_phi_deg",
    "backtracking", "irrad_optimiz", "central_gap_m", "pitch_slope_deg",
    "wind_stow", "wind_stow_speed_ms", "wind_stow_position_deg",
    "site_polygon", "setback_m", "ground_clearance_m", "max_table_columns",
    "obstacles", "base_slope_deg", "terrain", "sub_orientations",
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
BIFACIAL_EXTRA_VARS = (
    "viGlobGnd", "viDiffGnd", "viGlobBak",
    "viBeamBak", "viDifSBak", "viBmSFBak", "viMismBak", "viPRBifi",
)


def resolve_utility_field_type(scope: str, ground_layout: dict | None) -> tuple[str, bool]:
    """Map (scope, ground_layout.model_mode) to native (field_type, include_3d_scene)."""
    layout = ground_layout or {}
    mode = layout.get("model_mode", "unlimited")
    if scope == "ground_sheds":
        has_3d = (
            (mode == "3d")
            or (layout.get("site_polygon") is not None)
            or bool(layout.get("obstacles"))
            or (layout.get("terrain") is not None)
        )
        return ("RegularSheds", True) if has_3d else ("Sheds", False)
    if scope == "ground_tracker":
        has_3d = (mode == "3d") or (layout.get("terrain") is not None)
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

    mode = layout.get("model_mode", "unlimited")
    if mode not in ("unlimited", "3d"):
        bad("ground_layout.model_mode", "Choose 'unlimited' or '3d'")

    if mode == "unlimited" and scope == "ground_tracker":
        for key in ("central_gap_m", "pitch_slope_deg", "wind_stow", "wind_stow_speed_ms", "wind_stow_position_deg"):
            if key in layout:
                bad(f"ground_layout.{key}", f"{key} requires model_mode='3d'")
    elif mode == "3d":
        for key in ("electrical_effect", "shading_factor", "cell_width_m"):
            if key in layout:
                bad(f"ground_layout.{key}", f"{key} requires model_mode='unlimited'")
        if scope == "ground_sheds":
            for key in ("inactive_band_width_1_m", "inactive_band_width_2_m"):
                if key in layout:
                    bad(f"ground_layout.{key}", f"{key} requires model_mode='unlimited' for ground_sheds")

    sub_orientations = layout.get("sub_orientations")
    has_sub_orients = isinstance(sub_orientations, list) and len(sub_orientations) > 0
    if sub_orientations is not None:
        if scope != "ground_sheds":
            bad("ground_layout.sub_orientations", "sub_orientations is only supported for scope='ground_sheds'")
        elif mode != "unlimited" or layout.get("site_polygon") is not None or layout.get("obstacles") or layout.get("terrain") is not None:
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

    if scope == "ground_sheds":
        for name, low, high in (("tilt", 0, 90), ("azimuth", -180, 180)):
            value = spec.get(name)
            note = ("Use a finite tilt in 0..90 degrees" if name == "tilt"
                    else "Use finite PVsyst azimuth in -180..180 degrees, zero south and west positive")
            if value is None:
                if not has_sub_orients:
                    bad(name, note)
            elif not _is_number(value) or not low <= value <= high:
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
        if scope != "ground_sheds":
            bad("ground_layout.site_polygon", "site_polygon finite 3D layout is currently supported for scope='ground_sheds'")
        else:
            from pvsyst_auto.scene.reader import validate_site_polygon
            try:
                validate_site_polygon(layout["site_polygon"])
            except ValueError as exc:
                bad("ground_layout.site_polygon", exc)
    if layout.get("obstacles") is not None:
        if scope != "ground_sheds":
            bad("ground_layout.obstacles", "3D obstacles are currently supported for scope='ground_sheds'")
        else:
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
        if scope != "ground_sheds":
            bad("ground_layout.base_slope_deg", "base_slope_deg is supported for scope='ground_sheds'; use pitch_slope_deg for trackers")
        elif not _is_number(val) or not -45 <= val <= 45:
            bad("ground_layout.base_slope_deg", "base_slope_deg must lie in -45..45 degrees")
        elif val and not resolve_utility_field_type(scope, layout)[1]:
            bad("ground_layout.base_slope_deg", "Nonzero base_slope_deg requires a 3D ground-sheds layout")

    bifacial = spec.get("bifacial")
    if bifacial is not None:
        if mode in ("unlimited", "3d"):
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


def plan_utility_capacity(store, pan_info: dict, ond_info: dict, flat_capacity: dict) -> dict:
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
    return plan_capacity(store, lines, 1, pan_fields, ond_electrical, flat_capacity)


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


def _build_utility_kwargs(spec: dict, *, pan_name: str, ond_name: str, site_name: str, met_name: str,
                          plan: dict, pnom_ac: float, allow_missing_met: bool = False) -> dict:
    scope = spec["scope"]
    layout = dict(spec.get("ground_layout") or {})
    field_type, include_3d = resolve_utility_field_type(scope, layout)
    losses = dict(spec.get("losses") or {})

    default_pitch = 10.0 if scope == "ground_sheds" else 12.0
    pitch_m = float(layout.get("pitch_m", default_pitch))
    shed_count = int(layout.get("shed_count", 10))
    module_rows = int(layout.get("module_rows", 2))
    module_columns = int(layout.get("module_columns", 25))
    if include_3d and layout.get("site_polygon") is None:
        scene_modules = shed_count * module_rows * module_columns
        electrical_modules = int(plan["module_count"])
        if scene_modules != electrical_modules:
            raise ValueError(
                f"3D scene module count ({scene_modules} = {shed_count}x{module_rows}x{module_columns}) "
                f"disagrees with electrical module count ({electrical_modules}); "
                "adjust ground_layout shed_count/module_rows/module_columns to match the electrical design"
            )
    orient_str = "moLandscape" if layout.get("module_orientation") == "landscape" else "moPortrait"
    axis_azim = (
        float(layout["axis_azimuth_deg"])
        if layout.get("axis_azimuth_deg") is not None
        else float(spec.get("azimuth", 0.0))
    )

    bif_raw = spec.get("bifacial")
    bif_cfg = None
    if bif_raw is not None:
        normalized_bif = normalize_bifacial_spec(bif_raw, field_type=field_type)
        if normalized_bif.get("enabled"):
            if include_3d and "n_sheds_user" not in bif_raw:
                normalized_bif["n_sheds_user"] = shed_count
            bif_cfg = normalized_bif

    n_inv = int(plan["inverter_count"])
    mppt_per_inv = int(plan["mppt_per_inverter"])
    n_mppt_total = n_inv * mppt_per_inv
    pnom_per_mppt = pnom_ac / mppt_per_inv

    raw_sub_orients = layout.get("sub_orientations")
    resolved_sub_orients = (
        _apportion_sub_orientations(raw_sub_orients, plan)
        if raw_sub_orients else None
    )
    default_tilt = (
        float(spec["tilt"]) if spec.get("tilt") is not None
        else (resolved_sub_orients[0]["tilt_deg"] if resolved_sub_orients else 20.0)
    )
    default_azim = (
        float(spec["azimuth"]) if spec.get("azimuth") is not None
        else (resolved_sub_orients[0]["azimuth_deg"] if resolved_sub_orients else 0.0)
    )

    kwargs = {
        "project_comment": Path(spec["new_project"]).stem,
        "variant_comment": f"{scope} ({'3D' if include_3d else 'unlimited'})",
        "site_name": site_name,
        "met_name": met_name,
        "allow_missing_met": allow_missing_met,
        "field_type": field_type,
        "pan_name": pan_name,
        "ond_name": ond_name,
        "n_mod_serie": int(plan["series_modules"]),
        "n_string_ch": int(plan["string_count"]),
        "n_inverter": n_inv,
        "n_inv_mppt": n_mppt_total,
        "independent_mppt": True,
        "pnom_ac_in_mppt": pnom_per_mppt,
        "tilt_deg": default_tilt if scope == "ground_sheds" else 0.0,
        "azimuth_deg": default_azim if scope == "ground_sheds" else 0.0,
        "sub_orientations": resolved_sub_orients,
        "axis_azimuth_deg": axis_azim,
        "min_phi_deg": float(layout.get("min_phi_deg", -60.0)),
        "max_phi_deg": float(layout.get("max_phi_deg", 60.0)),
        "backtracking": bool(layout.get("backtracking", True)),
        "irrad_optimiz": bool(layout.get("irrad_optimiz", False)),
        "central_gap_m": float(layout.get("central_gap_m", 0.0)),
        "pitch_slope_deg": float(layout.get("pitch_slope_deg", 0.0)),
        "wind_stow": bool(layout.get("wind_stow", False)),
        "wind_stow_speed_ms": float(layout.get("wind_stow_speed_ms", 12.0)),
        "wind_stow_position_deg": float(layout.get("wind_stow_position_deg", 0.0)),
        "pitch_m": pitch_m,
        "collector_width_m": (float(layout["collector_width_m"])
                              if layout.get("collector_width_m") is not None else None),
        "shed_count": shed_count,
        "inactive_band_width_1_m": (float(layout["inactive_band_width_1_m"])
                                    if layout.get("inactive_band_width_1_m") is not None else None),
        "inactive_band_width_2_m": (float(layout["inactive_band_width_2_m"])
                                    if layout.get("inactive_band_width_2_m") is not None else None),
        "electrical_effect": layout.get("electrical_effect"),
        "shading_factor": (float(layout["shading_factor"])
                           if layout.get("shading_factor") is not None else None),
        "cell_width_m": (float(layout["cell_width_m"])
                         if layout.get("cell_width_m") is not None else None),
        "module_rows": module_rows,
        "module_columns": module_columns,
        "module_orient": orient_str,
        "spacing_x_m": float(layout.get("spacing_x_m", 0.02)),
        "spacing_y_m": float(layout.get("spacing_y_m", 0.02)),
        "bifacial": bif_cfg,
        "include_3d_scene": include_3d,
        "site_polygon": layout.get("site_polygon"),
        "setback_m": float(layout.get("setback_m", 0.0)),
        "ground_clearance_m": float(layout.get("ground_clearance_m", 0.5)),
        "max_table_columns": layout.get("max_table_columns"),
        "obstacles": layout.get("obstacles"),
        "base_slope_deg": float(layout.get("base_slope_deg", 0.0)),
        "terrain": layout.get("terrain"),
        "u_factor": float(losses.get("UFactor", 29.0)),
        "lid_loss": float(losses.get("LIDLoss", 0.0)),
        "pmism_mpp": float(losses["PMismMPP"]) if "PMismMPP" in losses else None,
        "module_quality_loss": float(losses["ModuleQualityLoss"]) if "ModuleQualityLoss" in losses else None,
        "with_bom": True,
    }
    return kwargs


def _extract_bifacial_metrics(csv_path: Path, units: dict) -> dict:
    """Compute annual bifacial irradiance and loss summaries from hourly or sub-hourly SFI CSV."""
    if not units.get("GlobBak"):
        raise ValueError("Bifacial simulation output lacks GlobBak column/unit")
    bak_info = IRRAD_UNIT_KIND.get(units["GlobBak"])
    if bak_info is None:
        raise ValueError(f"Unsupported GlobBak unit: {units['GlobBak']!r}")
    present = tuple(c for c in ("GlobBak", "GlobGnd", "DiffGnd", "BeamBak", "DifSBak", "MismBak", "PRBifi")
                    if c in units)
    res = summarize_results(csv_path, present)
    if res["rows"] >= 2 and res["step_minutes"] is None:
        raise ValueError("Bifacial simulation output does not have a regular timestamp cadence")
    step_hours = (res["step_minutes"] or 60.0) / 60.0
    summ = res["summary"]
    bak_scale, bak_is_power = bak_info
    out = {
        "rear_irradiation_kwh_m2": summ["GlobBak"]["sum"] * bak_scale * (step_hours if bak_is_power else 1.0),
    }
    if "GlobGnd" in summ:
        gnd_scale, gnd_is_power = IRRAD_UNIT_KIND.get(units["GlobGnd"], (0.001, True))
        out["ground_irradiation_kwh_m2"] = summ["GlobGnd"]["sum"] * gnd_scale * (step_hours if gnd_is_power else 1.0)
    if "MismBak" in summ:
        p_scale, p_is_power = POWER_UNIT_KIND.get(units["MismBak"], (1.0, True))
        out["rear_mismatch_loss_kwh"] = summ["MismBak"]["sum"] * p_scale * (step_hours if p_is_power else 1.0)
    if "PRBifi" in summ and summ["PRBifi"]["mean"] is not None:
        out["mean_pr_bifi"] = summ["PRBifi"]["mean"]
    return out


def run_utility_design(
    cli, store, *,
    spec: dict,
    pan_info: dict,
    ond_info: dict,
    capacity_plan: dict,
    sit_name: str,
    met_name: str,
    weather_manifest_name: str = "",
    dry_run: bool = True,
    prepare_only: bool = False,
    preparation_context: dict | None = None,
    allow_window_creation: bool | None = None,
    synthetic_weather: bool = False,
    report_language: str = "en",
) -> dict:
    """Synthesize a zero-template utility ground project and optionally run a full-year CLI simulation."""
    new_project = spec["new_project"]
    year = spec["year"]
    pan_name = Path(pan_info["name"]).name
    ond_name = Path(ond_info["name"]).name
    scope = spec["scope"]
    variant = "VC0"

    result = {
        "status": "blocked",
        "stage": "preflight",
        "scope": scope,
        "dry_run": dry_run,
        "issues": [],
        "warnings": [],
        "transactions": [],
        "recovery": [],
        "metrics": None,
        "bifacial_metrics": None,
        "utility_summary": None,
        "capacity_plan": capacity_plan,
        "artifacts": {},
        "native_consumption_verified": False,
    }
    if prepare_only:
        result.update(operation="prepare_project", prepared=False, simulated=False,
                      preparation_context=preparation_context or {})

    def issue(field: str, message) -> None:
        result["issues"].append({"field": field, "message": str(message)})

    assets: dict = {}
    for kind, info in (("PAN", pan_info), ("OND", ond_info)):
        if info.get("path") and Path(info["path"]).is_file():
            assets[kind] = {"path": str(info["path"]), "sha256": fingerprint(info["path"]),
                            "library": info.get("library", "workspace")}

    weather_report, weather_warnings = collect_simulation_weather_assets(
        cli.workspace,
        site_name=sit_name,
        met_name=met_name,
        year=year,
        synthetic_weather=synthetic_weather,
        weather_manifest_name=weather_manifest_name,
        assets=assets,
        result=result,
        issue=issue,
        inspect_calendar=inspect_met_calendar,
    )

    paths: dict[str, Path] = {}
    try:
        project_path(cli.workspace, new_project)
        if any(store._belongs_to_project(p, new_project) for p in store._root().iterdir()):
            raise FileExistsError("Working project already exists; choose a new project filename")
        prefix = f"{Path(new_project).stem}_{'prepare' if prepare_only else 'annual'}_{year}"
        paths["manifest"] = workspace_file(cli.workspace, "Results", prefix + ".json", ".json")
        if not prepare_only:
            paths["csv"] = workspace_file(cli.workspace, "Results", prefix + ".csv", ".csv")
            paths["pdf"] = workspace_file(cli.workspace, "Results", prefix + ".pdf", ".pdf")
            paths["sfi"] = workspace_file(cli.workspace, "Models", prefix + ".SFI", ".SFI")
        if any(path.exists() for path in paths.values()):
            raise FileExistsError("An output/SFI/manifest already exists; choose a new working project")
        result["artifacts"] = {key: str(path) for key, path in paths.items()}
    except (ValueError, OSError) as exc:
        issue("outputs", exc)

    synth_preview = None
    nominal_dc_kw = float(capacity_plan["installed_dc_kw"])
    result["nominal_dc_power_kw"] = nominal_dc_kw
    if not result["issues"]:
        try:
            pnom_ac = _ond_pnom_kw(ond_info["electrical"])
            effective_met = f"{Path(sit_name).stem}_Synthetic.MET" if synthetic_weather else met_name
            kwargs = _build_utility_kwargs(
                spec, pan_name=pan_name, ond_name=ond_name,
                site_name=sit_name, met_name=effective_met,
                plan=capacity_plan, pnom_ac=pnom_ac,
                allow_missing_met=synthetic_weather,
            )
            synth_preview = synthesize_utility_project(store, **kwargs)
            result["utility_summary"] = synth_preview["utility_summary"]
            ground_3d = synth_preview["utility_summary"].get("ground_3d_layout")
            if isinstance(ground_3d, dict) and "total_modules" in ground_3d:
                scene_modules = int(ground_3d["total_modules"])
                electrical_modules = int(capacity_plan["module_count"])
                if scene_modules != electrical_modules:
                    raise ValueError(
                        f"3D site_polygon scene module count ({scene_modules}) "
                        f"disagrees with electrical module count ({electrical_modules}); "
                        "adjust site_polygon/setback_m/pitch_m/module_rows or target capacity to match the electrical design"
                    )
            if synth_preview["utility_summary"]["bifacial_enabled"]:
                is_bifi_pan = _pan_is_bifacial(store, pan_info)
                if is_bifi_pan is False:
                    issue(
                        "bifacial",
                        f"Selected PAN {pan_name} is monofacial (pvModule Flags lacks bifacial bit 23 / $00800000); "
                        "PVsystCLI ignores pvBiFacial unless the .PAN module declares bifaciality",
                    )
            trf_spec = spec.get("transformers")
            if trf_spec:
                from pvsyst_auto.energy.transformers import apply_transformers_to_variant_text
                vc0_with_trf, trf_summary = apply_transformers_to_variant_text(
                    synth_preview["vc0_text"],
                    mv=trf_spec.get("mv"),
                    hv=trf_spec.get("hv"),
                    default_ref_power_kw=nominal_dc_kw,
                    components=getattr(store, "components", None),
                )
                synth_preview["vc0_text"] = vc0_with_trf
                result["utility_summary"]["transformers"] = trf_summary
                result["warnings"].extend(trf_summary.get("warnings") or ())
            # Validate synthesized .VC0 object structure in memory during preflight (including dry_run)
            from pvsyst_auto.components.editor import objects as parse_objects
            parse_objects(synth_preview["vc0_text"].lstrip("\ufeff"))
        except (ValueError, OSError, KeyError) as exc:
            issue("utility_synthesis", exc)

    result["inputs"] = assets
    if result["issues"]:
        return result

    result["status"] = "ready"
    result["stage"] = "preflight"
    if dry_run:
        return result

    manifest = paths["manifest"]

    def save(first: bool = False) -> None:
        write_json(manifest, result, first=first)

    def stage(name: str) -> None:
        result.update(status="running", stage=name)
        save()

    save(first=True)
    try:
        stage("synthesize")
        vc0_text = synth_preview["vc0_text"]
        trf_spec = spec.get("transformers")
        created = store.create_project(new_project, synth_preview["prj_text"], {variant: vc0_text})
        result["transactions"].append({"tool": "create_project", "result": created})
        result["working_files"] = store.inspect_project(new_project)["files"]
        validated = store.validate_structure(new_project, variant)
        if validated["valid"] is not True:
            raise ValueError("Synthesized utility variant failed structural validation: "
                             + "; ".join(validated["issues"] + validated["unchecked_checks"]))
        if prepare_only:
            review = bool(weather_warnings or result["preparation_context"].get("review_required", False))
            result.update(
                status="needs_review" if review else "prepared",
                stage="prepared",
                prepared=True,
                working_project=new_project,
                working_variant=variant,
            )
            result["artifacts"].update(
                project=str(store._path(new_project)),
                variant=str(store._path(new_project, variant)),
            )
        else:
            stage("simulation")
            bifacial_enabled = bool(synth_preview["utility_summary"]["bifacial_enabled"])
            variables = list(VAR_GROUPS["all_common"]) + ["viGlobShd"]
            if bifacial_enabled:
                variables.extend(BIFACIAL_EXTRA_VARS)
            if trf_spec:
                variables.extend(["viEOutInv", "viEMVOhmL", "viEMVTrfL", "viEHVOhmL", "viEHVTrfL"])
            if weather_report is not None:
                variables.extend(["viGlobHor", "viDiffHor", "viT_Amb"])
            build_sfi(paths["sfi"], variables)
            native_inputs = native_cli_weather_inputs(
                assets, synthetic_weather=synthetic_weather, weather_report=weather_report,
            )
            run = cli.run_simulation(
                new_project, variant,
                sfi=paths["sfi"],
                out_csv=paths["csv"],
                report_pdf=paths["pdf"],
                time_step="hour",
                start_date=f"{year}.01.01",
                end_date=f"{year}.12.31",
                report_language=report_language,
                log_level=3,
                report_pages=spec.get("report_pages"),
                allow_window_creation=allow_window_creation,
                **native_inputs,
            )
            result["run"] = run
            result["warnings"].extend(run.get("warnings", []))
            stage("results")

            def _validate_utility():
                units = result_units(paths["csv"], strict=True)
                if not units.get("GlobShd"):
                    raise ValueError("Native utility output lacks GlobShd column/unit")
                rows = 0
                headers = []
                ratio_indices: set[int] = set()
                globinc_idx: int | None = None
                for headers, row in iter_result_csv(paths["csv"], strict=True):
                    if rows == 0:
                        ratio_indices = {
                            idx for idx, name in enumerate(headers[1:], start=1)
                            if units.get(name) in ("ratio", "%")
                        }
                        if "GlobInc" in headers:
                            globinc_idx = headers.index("GlobInc")
                    for idx, value in enumerate(row[1:], start=1):
                        if idx in ratio_indices:
                            if (globinc_idx is not None and row[globinc_idx] > 0
                                    and (not isinstance(value, (int, float)) or not math.isfinite(value))):
                                raise ValueError(f"Native utility output has missing/nonfinite ratio {headers[idx]} during irradiation")
                        elif not isinstance(value, (int, float)) or not math.isfinite(value):
                            raise ValueError("Native utility output contains missing/nonfinite numeric data")
                    rows += 1
                if bifacial_enabled:
                    result["bifacial_metrics"] = _extract_bifacial_metrics(paths["csv"], units)
                result["utility_output"] = {"hourly_rows": rows, "numeric_columns": headers[1:], "units": units}

            diag_needs_review = verify_and_read_simulation_results(
                result,
                store=store,
                project=new_project,
                variant=variant,
                site_name=sit_name,
                met_name=met_name,
                year=year,
                nominal_dc_kw=nominal_dc_kw,
                csv_path=paths["csv"],
                pdf_path=paths["pdf"],
                assets=assets,
                synthetic_weather=synthetic_weather,
                weather_report=weather_report,
                validate_output=_validate_utility,
            )
            flags = result["metrics"]["diagnostic_flags"]
            review = bool(
                run.get("warnings") or flags or weather_warnings
                or result.get("weather_comparison", {}).get("needs_review", False)
                or diag_needs_review
            )
            result.update(
                status="needs_review" if review else "completed",
                stage="completed",
                native_consumption_verified=True,
                working_project=new_project,
                working_variant=variant,
            )
    except BaseException as exc:
        if record_workflow_failure(result, cli, exc, save):
            raise
    if prepare_only and result["status"] == "failed":
        result["prepared"] = False
    save()
    return result

"""One unified single-project design flow: discovery, design envelope, weather and annual simulation.

Scope
-----
One new single-subarray, single-ordinary-fixed-plane, unshaded grid project per call. No
project comparison, no multi-subarray layout, no binary component analysis. Everything the
selected manufacturer files do not declare must be in the spec.

This module owns the ordering only:
strict spec validation, output-name preflight, read-only native capability/licence check,
discovery without capacity, the pure design envelope, a second discovery that confirms the
accepted capacity against the template hardware, weather acquisition/preparation, then one
annual simulation. Each phase accepts only its own explicit success status; anything else
stops the flow with that phase named. A dry run writes nothing.

Reused building blocks (no new job framework, no hashing or fingerprinting here):
``discover_design_inputs``, ``prepare_design_envelope``, ``download_weather``,
``prepare_weather`` and ``simulate_template``.
"""
from __future__ import annotations
import re
import subprocess
from pathlib import Path
from pvsyst_auto.validation import (append_issue as _issue, is_finite_number as _is_number, is_printable_text as _is_text,
    is_strict_int as _is_int)
from pvsyst_auto.native.cli import PVsystError, validate_report_pages
from pvsyst_auto.engineering.review import discover_design_inputs
from pvsyst_auto.engineering.sizing import NUMBERS as CAPACITY_NUMBERS, REQUEST_KEYS, prepare_design_envelope, suggest_sizing
from pvsyst_auto.project.paths import project_name, variant_id, workspace_file, workspace_folder
from pvsyst_auto.workflows.simulation import simulate_template, prepare_template
from pvsyst_auto.project.template import SEED_VARIANT, prepare_standard_template
from pvsyst_auto.weather.acquisition import download_weather
from pvsyst_auto.weather.preparation import prepare_weather
from pvsyst_auto.project.standalone import STANDALONE_SCOPE, run_standalone_design
from pvsyst_auto.workflows.utility import UTILITY_SCOPES, plan_utility_capacity, preflight_utility_synthesis, run_utility_design, validate_utility_spec
SCOPE = "fixed_unshaded_grid"
SUPPORTED_SCOPES = (SCOPE, *UTILITY_SCOPES, STANDALONE_SCOPE)
TIME_BASIS = "local_standard_interval_start"
EXPECTED_WORKSPACE_BASENAME = "PVsyst8.1_Data"
LAYOUT_ROOT_FOLDERS = ("ComposPV", "DataMeas", "Meteo", "Models", "Other", "Projects", "Results",
    "Shadings", "Sites", "Templates", "UserBatch", "UserData", "UserHourly",
    "UserHourlyParams", "UserImages", "UserOptimization", "UserRecycleBin")
LAYOUT_COMPOSPV_FOLDERS = ("Batteries", "Clients", "Gensets", "Inverters", "Pumps", "PVmodules", "Regulators", "RegulPmp", "Sellers")
TEMPERATURE_METHOD = "endpoint_mean"   # the only method verified inside pvsyst_weather_acquisition
CREATE_SITE_MAX_OFFSET_HOURS = 12
SIMULATION_OPTIONS = ("input-sfi-file", "start-date", "end-date", "time-step")
SITE_OPTIONS = ("site-name", "altitude", "timezone", "country-code")
LOCATION_FIELDS = ("latitude", "longitude", "site_label", "altitude", "timezone_name")
ISO_COUNTRY = re.compile(r"[A-Z]{2}")
SPEC_KEYS = frozenset(("scope", "sizing_mode", "auto_sizing", "new_project", "year", "target_dc_kw", "tilt", "azimuth",
    "module", "inverter", "template_project", "variant", "capacity", "electrical_design", "losses",
    "weather", "report_pages", "report_language", "ground_layout", "bifacial", "transformers", "standalone",
    "p50_p90", "horizon"))
SIZING_MODES = ("strict", "auto_from_components")
AUTO_SIZING_KEYS = frozenset(("voltage_standard", "min_ambient_c", "min_operating_c", "max_operating_c",
    "min_dc_ac_ratio", "max_dc_ac_ratio", "capacity_tolerance_pct", "beta_voc_pct_per_c", "beta_vmp_pct_per_c", "alpha_isc_pct_per_c",
    "allow_asymmetric_mppt"))
EQUIPMENT_KEYS = frozenset(("file_name", "manufacturer", "model"))
WEATHER_KEYS = frozenset(("mode", "sit_name", "met_name", "csv_name", "country_code", "time_basis", "location_id",
    "latitude", "longitude", "site_label", "altitude", "timezone_name", "temperature_method", "weather_manifest_name",
    "strict_dhi", "fetch_horizon"))
REMOTE_WEATHER_MODES = ("open_meteo", "pvgis_tmy", "nasa_power")
WEATHER_MODES = ("existing", "synthetic_from_site", "csv", *REMOTE_WEATHER_MODES)
_PROVIDER_FOR_MODE = {"open_meteo": "open_meteo_era5", "pvgis_tmy": "pvgis_tmy", "nasa_power": "nasa_power"}
_RAW_SUFFIX_FOR_MODE = {"open_meteo": "_openmeteo_raw.json", "pvgis_tmy": "_pvgis_tmy_raw.json", "nasa_power": "_nasapower_raw.json"}
LOSS_KEYS = frozenset(("LIDLoss", "PMismMPP", "ModuleQualityLoss", "UFactor", "ageing"))
CAPACITY_KEYS = frozenset((*CAPACITY_NUMBERS, "min_dc_ac_ratio", "capacity_tolerance_pct", "voltage_standard", "design_basis", "allow_asymmetric_mppt"))
ENVELOPE_NESTED = ("temperatures", "envelope", "coefficients", "module_limits", "mppt_limits", "effective_irradiance")
ACQUIRE_SUCCESS = ("prepared",)
PREPARE_SUCCESS = ("prepared", "needs_review")
SIMULATE_SUCCESS = ("completed", "needs_review")
RETAINED_NOTE = ("Produced provider JSON, CSV, preparation artifacts, the working project and the "
    "recovery steps are retained; no automatic cleanup, overwrite or rollback was "
    "performed and a documented recovery is never reported as success")
_PHASE_ERRORS = (ValueError, OSError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired)
def _prune_empty(value):
    """Drop unset optionals - None and blank strings - the shape a typed server sends by default."""
    if isinstance(value, dict):
        return {key: item for key, raw in value.items()
            if (item := _prune_empty(raw)) is not None and not (isinstance(item, str) and not item.strip())}
    if isinstance(value, list): return [_prune_empty(item) for item in value]
    return value
def _inherit(result: dict, source: dict, field: str) -> None:
    """Carry one phase's issues/warnings/review_required/error up, normalizing plain strings."""
    for item in source.get("issues") or []:
        result["issues"].append(item if isinstance(item, dict) else {"field": field, "message": str(item)})
    error = source.get("error")
    if isinstance(error, str) and error.strip():
        reported = {item.get("message") for item in result["issues"] if isinstance(item, dict)}
        if error not in reported: result["issues"].append({"field": field, "message": error})
    for item in source.get("warnings") or []:
        result["warnings"].append(f"{item.get('field')}: {item.get('message')}" if isinstance(item, dict) else str(item))
    for item in source.get("review_required") or []: result["warnings"].append(f"{field}.review_required: {item}")
def _stop(result: dict, stage: str, status: str = "blocked") -> dict:
    result.update(status=status, stage=stage)
    return result
def _phase(result: dict, stage: str, call):
    """Run one phase call; a raised error becomes a failed stage with the artifacts kept."""
    try:
        return call(), None
    except _PHASE_ERRORS as exc:
        _issue(result, stage, getattr(exc, "diagnostics", None) or str(exc))
        return None, _stop(result, stage, "failed")
def _bad(spec_issues: list[dict], field: str, message) -> None:
    spec_issues.append({"field": field, "message": str(message)})
def _check_equipment(selector, kind, spec_issues):
    field = kind.lower()
    if not isinstance(selector, dict):
        _bad(spec_issues, field, f"Provide the {field} component selector object")
        return
    for key in sorted(set(selector) - EQUIPMENT_KEYS): _bad(spec_issues, f"{field}.{key}", "Unknown component selector input")
    file_name, manufacturer, model = selector.get("file_name"), selector.get("manufacturer"), selector.get("model")
    if file_name is not None:
        if not _is_text(file_name): _bad(spec_issues, f"{field}.file_name", "Use one loose filename without directories")
        elif Path(file_name).name != file_name or file_name != file_name.strip():
            _bad(spec_issues, f"{field}.file_name", "Pass a plain filename; the workspace resolves the library path")
    for key, value in (("manufacturer", manufacturer), ("model", model)):
        if value is not None and not _is_text(value): _bad(spec_issues, f"{field}.{key}", "Use printable text")
    if not _is_text(file_name) and not (_is_text(manufacturer) and _is_text(model)):
        _bad(spec_issues, f"{field}.name", f"Provide one exact manufacturer+model pair or an explicit {field} filename")
def _check_capacity(capacity, target, spec_issues):
    if not isinstance(capacity, dict):
        _bad(spec_issues, "capacity", "Provide the direct capacity object")
        return
    for key in sorted(set(capacity) - CAPACITY_KEYS): _bad(spec_issues, f"capacity.{key}", "Unknown capacity input")
    for name in CAPACITY_NUMBERS:
        value = capacity.get(name)
        if value is None: _bad(spec_issues, f"capacity.{name}", "Required design input is missing; no value is inferred")
        elif not _is_number(value): _bad(spec_issues, f"capacity.{name}", "Use a finite strict numeric value")
        elif not name.startswith("design_") and value <= 0: _bad(spec_issues, f"capacity.{name}", "Design magnitudes must be positive")
    if _is_number(capacity.get("target_dc_kw")) and float(capacity["target_dc_kw"]) != target:
        _bad(spec_issues, "capacity.target_dc_kw", f"capacity.target_dc_kw {capacity['target_dc_kw']} must equal spec.target_dc_kw {target}")
    tolerance = capacity.get("capacity_tolerance_pct")
    if tolerance is not None and (not _is_number(tolerance) or not 0 <= tolerance <= 20):
        _bad(spec_issues, "capacity.capacity_tolerance_pct", "Use 0..20 percent")
    if capacity.get("voltage_standard") not in ("IEC", "UL"):
        _bad(spec_issues, "capacity.voltage_standard", "Explicitly choose IEC or UL")
    if "allow_asymmetric_mppt" in capacity and capacity["allow_asymmetric_mppt"] is not None and type(capacity["allow_asymmetric_mppt"]) is not bool:
        _bad(spec_issues, "capacity.allow_asymmetric_mppt", "Use a strict boolean for allow_asymmetric_mppt")
    basis = capacity.get("design_basis")
    if not _is_text(basis) or len(basis) > 2000:
        _bad(spec_issues, "capacity.design_basis", "Provide a printable design basis up to 2000 characters")
def _check_design(design, target, spec_issues):
    if not isinstance(design, dict):
        _bad(spec_issues, "electrical_design", "Provide the declared manufacturer design object")
        return
    for key in sorted(set(design) - REQUEST_KEYS): _bad(spec_issues, f"electrical_design.{key}", "Unknown design-envelope input")
    declared = design.get("target_dc_kw")
    if declared is not None:
        if not _is_number(declared): _bad(spec_issues, "electrical_design.target_dc_kw", "Use a finite strict numeric target")
        elif float(declared) != target:
            _bad(spec_issues, "electrical_design.target_dc_kw",
                 f"The declared target {declared} contradicts spec.target_dc_kw {target}; the unified entry injects one target and never reconciles two")
    for name in ENVELOPE_NESTED:
        if (value := design.get(name)) is not None and not isinstance(value, dict):
            _bad(spec_issues, f"electrical_design.{name}", "Provide this design-envelope input as an object")
    if (label := design.get("manufacturer_model")) is not None and not _is_text(label):
        _bad(spec_issues, "electrical_design.manufacturer_model", "Use printable text")
def _check_weather(weather, spec_issues):
    if not isinstance(weather, dict):
        _bad(spec_issues, "weather", "Provide the weather request object")
        return
    for key in sorted(set(weather) - WEATHER_KEYS): _bad(spec_issues, f"weather.{key}", "Unknown weather input")
    mode = weather.get("mode")
    if mode not in WEATHER_MODES:
        _bad(spec_issues, "weather.mode", "Choose existing, synthetic_from_site, csv, open_meteo, pvgis_tmy or nasa_power")
        return
    if "strict_dhi" in weather and type(weather["strict_dhi"]) is not bool:
        _bad(spec_issues, "weather.strict_dhi", "Use a strict boolean for strict_dhi")
    if "fetch_horizon" in weather and type(weather["fetch_horizon"]) is not bool:
        _bad(spec_issues, "weather.fetch_horizon", "Use a strict boolean for fetch_horizon")
    country_code = weather.get("country_code")
    if country_code is not None and (not _is_text(country_code) or not ISO_COUNTRY.fullmatch(country_code)):
        _bad(spec_issues, "weather.country_code", "Use an explicit uppercase ISO alpha-2 country code")
    if weather.get("time_basis") is not None and weather.get("time_basis") != TIME_BASIS:
        _bad(spec_issues, "weather.time_basis", "Declare " + TIME_BASIS)
    for key in ("sit_name", "met_name", "csv_name"):
        if (value := weather.get(key)) is not None and not _is_text(value):
            _bad(spec_issues, f"weather.{key}", "Use one filename without directories")
    if mode == "existing":
        for key in ("sit_name", "met_name"):
            if not _is_text(weather.get(key)):
                _bad(spec_issues, f"weather.{key}", "Existing weather requires an explicit workspace SIT and MET filename")
        return
    if mode == "synthetic_from_site":
        if not _is_text(weather.get("sit_name")):
            _bad(spec_issues, "weather.sit_name", "synthetic_from_site requires an explicit workspace SIT filename")
        if weather.get("met_name") is not None:
            _bad(spec_issues, "weather.met_name", "synthetic_from_site generates hourly meteo from sit_name; omit met_name or use mode='existing'")
        return
    if mode == "csv":
        if not _is_text(weather.get("csv_name")):
            _bad(spec_issues, "weather.csv_name", "Provide the staged workspace weather CSV filename")
        if not _is_text(weather.get("country_code")):
            _bad(spec_issues, "weather.country_code", "A staged CSV requires the explicit uppercase ISO alpha-2 code")
        if weather.get("time_basis") != TIME_BASIS: _bad(spec_issues, "weather.time_basis", "A staged CSV requires " + TIME_BASIS)
    if mode in REMOTE_WEATHER_MODES:
        location_id = weather.get("location_id")
        if location_id is not None and any(weather.get(name) is not None for name in ("latitude", "longitude")):
            _bad(spec_issues, "weather.location_id", "Pass either location_id or explicit coordinates, not both")
        if location_id is None:
            missing = [name for name in LOCATION_FIELDS if weather.get(name) is None]
            if missing:
                _bad(spec_issues, "weather.location_id", "Provide location_id from search_locations, or the complete explicit place; missing: " + ", ".join(missing))
        elif not _is_int(location_id) or location_id <= 0:
            _bad(spec_issues, "weather.location_id", "Use a positive integer location_id from search_locations")
        for name in ("latitude", "longitude", "altitude"):
            if (value := weather.get(name)) is not None and not _is_number(value):
                _bad(spec_issues, f"weather.{name}", "Use a finite strict number")
        if _is_number(weather.get("latitude")) and not -90 <= weather["latitude"] <= 90:
            _bad(spec_issues, "weather.latitude", "Latitude must lie in -90..90")
        if _is_number(weather.get("longitude")) and not -180 <= weather["longitude"] <= 180:
            _bad(spec_issues, "weather.longitude", "Longitude must lie in -180..180")
        if (method := weather.get("temperature_method")) is not None and method != TEMPERATURE_METHOD:
            _bad(spec_issues, "weather.temperature_method", "Only " + TEMPERATURE_METHOD + " is verified")
def _validate_spec(spec) -> list[dict]:
    issues: list[dict] = []
    if not isinstance(spec, dict):
        _bad(issues, "spec", "Provide the design-flow spec object")
        return issues
    for key in sorted(set(spec) - SPEC_KEYS): _bad(issues, str(key), "Unknown design-flow spec input")
    try:
        validate_report_pages(spec.get("report_pages"))
    except ValueError as exc: _bad(issues, "report_pages", exc)
    if spec.get("report_language", "en") not in ("en", "zh"): _bad(issues, "report_language", "Choose 'en' or 'zh' for report_language")
    scope = spec.get("scope", SCOPE)
    if scope not in SUPPORTED_SCOPES:
        _bad(issues, "scope", "Only fixed_unshaded_grid, ground_sheds, ground_tracker, ground_dome, and stand_alone are supported")
    new_project = spec.get("new_project")
    if not _is_text(new_project): _bad(issues, "new_project", "Provide the new project filename")
    else:
        try:
            project_name(new_project)
        except ValueError as exc: _bad(issues, "new_project", exc)
    if not _is_int(year := spec.get("year")) or not 1969 <= year <= 2068:
        _bad(issues, "year", "Use an integer year in 1969..2068; native dates carry two-digit years")
    target = spec.get("target_dc_kw")
    if not _is_number(target) or target <= 0:
        _bad(issues, "target_dc_kw", "Provide a finite positive DC target in kW; no target is inferred")
    else: target = float(target)
    if scope in UTILITY_SCOPES: validate_utility_spec(spec, scope, issues)
    else:
        if spec.get("ground_layout") is not None:
            _bad(issues, "ground_layout", "ground_layout requires scope='ground_sheds', 'ground_tracker', or 'ground_dome'")
        if spec.get("bifacial") is not None: _bad(issues, "bifacial", "bifacial requires scope='ground_sheds' or 'ground_tracker'")
        for name, low, high in (("tilt", 0, 90), ("azimuth", -180, 180)):
            value = spec.get(name)
            note = "Use a finite tilt in 0..90 degrees" if name == "tilt" else "Use finite PVsyst azimuth in -180..180 degrees, zero south and west positive"
            if value is None or not _is_number(value) or not low <= value <= high: _bad(issues, name, note)
        for field_name, validator, msg in (("template_project", project_name, "Use one workspace PRJ filename"),
            ("variant", variant_id, "Use one VC identifier or omit it")):
            if (raw_v := spec.get(field_name)) is not None:
                if not _is_text(raw_v): _bad(issues, field_name, msg)
                else:
                    try:
                        validator(raw_v)
                    except ValueError as exc: _bad(issues, field_name, exc)
    _check_equipment(spec.get("module"), "module", issues)
    _check_equipment(spec.get("inverter"), "inverter", issues)
    sizing_mode = spec.get("sizing_mode", "strict")
    if sizing_mode not in SIZING_MODES: _bad(issues, "sizing_mode", "Choose 'strict' or 'auto_from_components'")
    auto_sizing = spec.get("auto_sizing")
    if auto_sizing is not None:
        if sizing_mode != "auto_from_components":
            _bad(issues, "auto_sizing", "auto_sizing options require sizing_mode='auto_from_components'")
        elif not isinstance(auto_sizing, dict): _bad(issues, "auto_sizing", "Provide auto_sizing as an object")
        else:
            for key in sorted(set(auto_sizing) - AUTO_SIZING_KEYS): _bad(issues, f"auto_sizing.{key}", "Unknown auto_sizing option")
            if "allow_asymmetric_mppt" in auto_sizing and auto_sizing["allow_asymmetric_mppt"] is not None and type(auto_sizing["allow_asymmetric_mppt"]) is not bool:
                _bad(issues, "auto_sizing.allow_asymmetric_mppt", "Use a strict boolean for allow_asymmetric_mppt")
    capacity, design = spec.get("capacity"), spec.get("electrical_design")
    gl = spec.get("ground_layout")
    asym_supported = scope in ("ground_sheds", "ground_tracker") and not (
        isinstance(gl, dict) and (gl.get("sub_orientations") or gl.get("pvc_orientation_tolerance_deg") is not None)
    )
    if isinstance(auto_sizing, dict) and auto_sizing.get("allow_asymmetric_mppt") is True and not asym_supported:
        _bad(issues, "auto_sizing.allow_asymmetric_mppt", "allow_asymmetric_mppt is only supported for single-orientation ground_sheds and ground_tracker")
    if isinstance(capacity, dict) and capacity.get("allow_asymmetric_mppt") is True and not asym_supported:
        _bad(issues, "capacity.allow_asymmetric_mppt", "allow_asymmetric_mppt is only supported for single-orientation ground_sheds and ground_tracker")
    if sizing_mode == "auto_from_components":
        if capacity is not None or design is not None:
            _bad(issues, "sizing_mode", "sizing_mode='auto_from_components' cannot be combined with explicit capacity or electrical_design")
    else:
        if capacity is None and design is None:
            _bad(issues, "capacity", "Provide exactly one of capacity (direct design envelope) or electrical_design (declared manufacturer inputs), or set sizing_mode='auto_from_components'; a template count is never used instead")
        elif capacity is not None and design is not None:
            _bad(issues, "electrical_design", "Provide exactly one of capacity or electrical_design; they are mutually exclusive")
        if capacity is not None and _is_number(target): _check_capacity(capacity, target, issues)
        if design is not None and _is_number(target): _check_design(design, target, issues)
    losses = spec.get("losses")
    if losses is not None:
        if not isinstance(losses, dict): _bad(issues, "losses", "Provide an explicit supported loss object")
        else:
            for key in sorted(set(losses) - LOSS_KEYS): _bad(issues, f"losses.{key}", "Unsupported loss parameter")
            for key, value in losses.items():
                if key == "ageing":
                    from pvsyst_auto.energy.ageing import normalize_ageing_spec
                    try: normalize_ageing_spec(value)
                    except ValueError as exc: _bad(issues, "losses.ageing", exc)
                elif key in LOSS_KEYS and not _is_number(value):
                    _bad(issues, f"losses.{key}", "Require a supported finite numeric loss parameter")
    if (raw_p50 := spec.get("p50_p90")) is not None:
        from pvsyst_auto.energy.p50_p90 import normalize_p50_spec
        try: normalize_p50_spec(raw_p50)
        except ValueError as exc: _bad(issues, "p50_p90", exc)
    hz_norm = None
    if (raw_hz := spec.get("horizon")) is not None:
        from pvsyst_auto.weather.horizon import normalize_horizon_spec
        try: hz_norm = normalize_horizon_spec(raw_hz)
        except ValueError as exc: _bad(issues, "horizon", exc)
    _check_weather(spec.get("weather"), issues)
    w_obj = spec.get("weather")
    fetch_hz = isinstance(w_obj, dict) and w_obj.get("fetch_horizon") is True
    if fetch_hz and hz_norm is not None and hz_norm["mode"] != "pvgis":
        _bad(issues, "weather.fetch_horizon", "weather.fetch_horizon=true contradicts explicit non-PVGIS horizon")
    if (fetch_hz or (hz_norm is not None and hz_norm["mode"] == "pvgis")) and isinstance(w_obj, dict):
        has_hz_coords = hz_norm is not None and hz_norm.get("latitude") is not None
        has_w_coords = ((w_obj.get("latitude") is not None and w_obj.get("longitude") is not None)
                        or w_obj.get("location_id") is not None or _is_text(w_obj.get("sit_name")) or _is_text(w_obj.get("csv_name")))
        if not has_hz_coords and not has_w_coords:
            _bad(issues, "horizon", "PVGIS horizon fetch requires horizon.latitude/longitude, weather coordinates/location_id, or weather.sit_name/csv_name")
    return issues
def _entries(root):
    """Workspace project entries; an absent folder is an empty workspace, nothing else is."""
    try:
        return list(root.iterdir())
    except FileNotFoundError: return []
def _output_targets(cli, store, spec, weather, sit_name, met_name, csv_name, *, prepare_only=False):
    """Every name the flow could create, checked before the first write of any phase."""
    issues, targets = [], {}
    new_project, year = spec["new_project"], spec["year"]
    stem = Path(new_project).stem
    try:
        if any(store._belongs_to_project(path, new_project) for path in _entries(store._root())):
            issues.append({"field": "new_project", "message": "Working project files already exist; choose a new project filename"})
    except (ValueError, OSError, AttributeError) as exc: issues.append({"field": "new_project", "message": str(exc)})
    try:
        if prepare_only: targets["project_manifest"] = workspace_file(cli.workspace, "Results", f"{stem}_prepare_{year}.json", ".json")
        else:
            prefix = f"{stem}_annual_{year}"
            targets.update({"annual_csv": workspace_file(cli.workspace, "Results", prefix + ".csv", ".csv"),
                "annual_pdf": workspace_file(cli.workspace, "Results", prefix + ".pdf", ".pdf"),
                "annual_manifest": workspace_file(cli.workspace, "Results", prefix + ".json", ".json"),
                "annual_sfi": workspace_file(cli.workspace, "Models", prefix + ".SFI", ".SFI")})
        if weather["mode"] in ("csv", *REMOTE_WEATHER_MODES):
            met_stem = Path(met_name).stem
            targets.update({"sit": workspace_file(cli.workspace, "Sites", sit_name, ".SIT"),
                "met": workspace_file(cli.workspace, "Meteo", met_name, ".MET"),
                "weather_import_csv": workspace_file(cli.workspace, "Meteo", f"{met_stem}_import.csv", ".csv"),
                "weather_monthly_csv": workspace_file(cli.workspace, "Meteo", f"{met_stem}_monthly.csv", ".csv"),
                "weather_mef": workspace_file(cli.workspace, "Meteo", f"{met_stem}.MEF", ".MEF"),
                "weather_manifest": workspace_file(cli.workspace, "Results", f"{met_stem}_weather.json", ".json")})
        if weather["mode"] in REMOTE_WEATHER_MODES:
            csv_stem = Path(csv_name).stem
            targets.update({"source_csv": workspace_file(cli.workspace, "Meteo", csv_name, ".csv"),
                "provider_raw": workspace_file(cli.workspace, "Meteo", f"{csv_stem}{_RAW_SUFFIX_FOR_MODE[weather['mode']]}", ".json"),
                "acquisition_manifest": workspace_file(cli.workspace, "Results", f"{csv_stem}_acquisition.json", ".json")})
    except (ValueError, OSError) as exc:
        issues.append({"field": "outputs", "message": str(exc)})
        return issues, {}
    seen = {}
    for label, path in targets.items():
        key = str(path).casefold()
        if key in seen:
            issues.append({"field": label, "message": f"Two planned outputs resolve to the same file: {path.name} (also planned as {seen[key]}); choose distinct names"})
        else: seen[key] = label
        if path.exists(): issues.append({"field": label, "message": "Destination already exists: " + path.name})
    return issues, targets
def _seed_conflicts(cli, store, seed_project, targets, stem):
    """New-only, filename and name checks for the derived seed template, before anything is written."""
    issues = []
    try:
        project_name(seed_project)
    except ValueError:
        issues.append({"field": "seed", "message": f"new_project stem {stem!r} derives a seed filename of {len(seed_project)} characters, beyond the 180-character project filename limit; supply a shorter new_project name instead of truncating the derived name"})
    try:
        if any(store._belongs_to_project(path, seed_project) for path in _entries(store._root())):
            issues.append({"field": "seed", "message": "The derived seed project already exists; a seed template is created new-only"})
    except (ValueError, OSError, AttributeError) as exc: issues.append({"field": "seed", "message": str(exc)})
    name = Path(seed_project).name.casefold()
    for label, path in targets.items():
        if path.name.casefold() == name:
            issues.append({"field": "seed", "message": f"The derived seed name {Path(seed_project).name} collides with the planned {label} file {path.name}; choose another new_project name"})
    return issues
def _layout_entries():
    for folder in LAYOUT_ROOT_FOLDERS: yield folder, (folder,)
    for folder in LAYOUT_COMPOSPV_FOLDERS: yield f"ComposPV/{folder}", ("ComposPV", folder)
def _resolve_layout(cli):
    resolved, errors = {}, {}
    for label, parts in _layout_entries():
        try:
            resolved[label] = workspace_folder(cli.workspace, "/".join(parts))
        except (ValueError, OSError) as exc: errors[label] = str(exc)
    return resolved, errors
def _readonly_layout(cli):
    """Preflight the canonical root name and the standard folders; nothing is created or moved."""
    issues = []
    layout = {"expected_basename": EXPECTED_WORKSPACE_BASENAME, "workspace": str(cli.workspace),
        "basename": Path(cli.workspace).name, "root_folders": list(LAYOUT_ROOT_FOLDERS),
        "compos_pv_folders": list(LAYOUT_COMPOSPV_FOLDERS), "present": [], "missing": [],
        "file_conflicts": [], "escaped": {}, "created_directories": []}
    if layout["basename"] != EXPECTED_WORKSPACE_BASENAME:
        _bad(issues, "workspace", f"The workspace root is named {layout['basename']!r}; the verified PVsyst 8.1 layout is {EXPECTED_WORKSPACE_BASENAME!r}. A root is never renamed, moved or replaced")
    resolved, errors = _resolve_layout(cli)
    layout["escaped"] = errors
    for label, reason in sorted(errors.items()):
        _bad(issues, "workspace." + label, f"The workspace folder does not stay inside the workspace: {reason}")
    for label, path in resolved.items():
        if path.is_dir(): layout["present"].append(label)
        elif path.exists(): layout["file_conflicts"].append(label)
        else: layout["missing"].append(label)
    for label in layout["file_conflicts"]:
        _bad(issues, "workspace." + label, f"{label} exists and is not a directory; an existing file is never overwritten or replaced")
    return issues, layout
def _init_layout(cli, store):
    """Create the missing standard folders; a file, an escape or a permission error fails."""
    try:
        store._root()
    except (ValueError, OSError, AttributeError) as exc: return [], str(exc)
    resolved, errors = _resolve_layout(cli)
    if errors: return [], "; ".join(f"{label}: {reason}" for label, reason in sorted(errors.items()))
    created = []
    for label, path in resolved.items():
        if path.is_dir(): continue
        if path.exists():
            return created, f"{label} already exists and is not a directory; an existing file is never overwritten or replaced"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc: return created, f"{label}: {exc}"
        created.append(label)
    return created, None
def _acquired_place(acquire):
    for key in ("location", "site"):
        if isinstance(value := acquire.get(key), dict): return value
    return {}
def _identity_drift(previous, current):
    """Detect components that changed between two discovery passes of one call."""
    for kind in ("PAN", "OND"):
        first, second = previous[kind], current[kind]
        if (Path(first["name"]).name, first.get("library"), first.get("manufacturer"), first.get("model")) != (
                Path(second["name"]).name, second.get("library"), second.get("manufacturer"), second.get("model")):
            return f"The selected {kind} changed between the first discovery and the capacity confirmation; the accepted envelope is never applied to a different component"
        if first.get("electrical") != second.get("electrical"):
            return f"The declared {kind} electrical values changed between the first discovery and the capacity confirmation; the accepted envelope is never applied to changed data"
    return None
def _native_preflight(cli, mode, *, prepare_only=False, allow_window_creation=None):
    """Read-only capability/licence check run before any weather download."""
    state = {}
    try:
        if not prepare_only:
            for option in SIMULATION_OPTIONS: cli._require("run-simulation", option, allow_window_creation=allow_window_creation)
            if mode == "synthetic_from_site":
                cli._require("run-simulation", "synthetic-weatherdata-generation", allow_window_creation=allow_window_creation)
        if mode not in ("existing", "synthetic_from_site"):
            for option in SITE_OPTIONS: cli._require("create-site", option, allow_window_creation=allow_window_creation)
            cli.command_help("convert-meteo", allow_window_creation=allow_window_creation)
        state["version"] = cli.version(allow_window_creation=allow_window_creation)
        licence = cli.license_info(allow_window_creation=allow_window_creation)
        state["license"] = licence
        if not prepare_only and licence.get("status") == "TRIAL" and licence.get("remaining_executions") == 0:
            raise ValueError("PVsystCLI reports no remaining trial executions")
    except (ValueError, OSError, PVsystError, subprocess.TimeoutExpired) as exc: return None, cli._redact(str(exc))
    return state, None
def _identity(selected):
    pan, ond = selected["PAN"], selected["OND"]
    for kind, item in (("PAN", pan), ("OND", ond)):
        if not _is_text(item.get("manufacturer")) or not _is_text(item.get("model")):
            return None, f"The selected {kind} declares no manufacturer/model; the unified entry binds one explicit identity label or nothing"
    return f"{pan['manufacturer']} {pan['model']} / {ond['manufacturer']} {ond['model']}", None
def _compact_discovery(discovery: dict | None) -> dict | None:
    """Fold verbose per-template scan rejection lists into concise counts unless verbose=True."""
    if not isinstance(discovery, dict): return discovery
    out = dict(discovery)
    out.pop("selection_rule", None)
    candidates = out.get("candidates")
    if isinstance(candidates, dict) and isinstance(candidates.get("templates"), list):
        templates = candidates["templates"]
        eligible = [item for item in templates if isinstance(item, dict) and item.get("valid")]
        rejected_count = len(templates) - len(eligible)
        sel_tpl = (out.get("selected") or {}).get("template")
        summary: dict = {"scanned_count": len(templates), "eligible_count": len(eligible), "rejected_count": rejected_count,
            "selected_template": {"project": sel_tpl.get("project"), "variant": sel_tpl.get("variant")} if isinstance(sel_tpl, dict) else None,
            "truncated": rejected_count > 0}
        if rejected_count > 0: summary["hint"] = "Pass verbose=true to expand per-template scan rejection details."
        out["candidates"] = {**candidates, "templates": {"summary": summary, "eligible": eligible}}
    return out
def _design_flow(cli, store, spec, dry_run, *, prepare_only=False, source_context=None, allow_window_creation=None, verbose=False):
    result = {"status": "blocked", "stage": "spec", "dry_run": dry_run,
        "execution_preflight_complete": False, "issues": [], "warnings": [], "selected": None,
        "preview": {"discovery": None, "envelope": None, "weather": None, "prepare": None,
                    "simulation": None, "template_seed": None, "seed_confirmed": None, "workspace": None},
        "metrics": None, "artifacts": {"acquire": {}, "prepare": {}, "simulate": {}, "seed": {}}, "recovery": []}
    if prepare_only: result.update(operation="prepare_project", prepared=False, simulated=False, native_consumption_verified=False)
    if type(dry_run) is not bool:
        _issue(result, "dry_run", "Use a strict boolean")
        return result
    if type(verbose) is not bool:
        _issue(result, "verbose", "Use a strict boolean")
        return result
    if isinstance(spec, dict): spec = _prune_empty(spec)
    if isinstance(spec, dict) and spec.get("scope") == STANDALONE_SCOPE:
        if prepare_only:
            _issue(result, "scope", "Stand-Alone designs require pvsyst_simulate_design")
            return result
        try:
            return run_standalone_design(cli, store, store.components, spec, dry_run=dry_run, allow_window_creation=allow_window_creation)
        except (ValueError, FileNotFoundError, FileExistsError, PVsystError) as exc:
            _issue(result, "standalone", str(exc))
            return result
    if issues := _validate_spec(spec):
        result["issues"] = issues
        return result
    scope = spec.get("scope", SCOPE)
    is_utility, year = scope in UTILITY_SCOPES, spec["year"]
    tilt = float(spec["tilt"]) if spec.get("tilt") is not None else None
    azimuth = float(spec["azimuth"]) if spec.get("azimuth") is not None else None
    target, losses, new_project = float(spec["target_dc_kw"]), spec.get("losses"), spec["new_project"]
    module, inverter, weather = spec["module"], spec["inverter"], spec["weather"]
    design, direct, mode = spec.get("electrical_design"), spec.get("capacity"), weather["mode"]
    synthetic_weather, stem = (mode == "synthetic_from_site"), Path(new_project).stem
    sit_name = weather.get("sit_name") or f"{stem}.SIT"
    met_name = "" if synthetic_weather else (weather.get("met_name") or f"{stem}.MET")
    csv_suffix = "openmeteo" if mode == "open_meteo" else mode
    csv_name = weather.get("csv_name") or (f"{stem}_{csv_suffix}.csv" if mode in REMOTE_WEATHER_MODES else "")
    country_code = weather.get("country_code") or ""
    layout_issues, layout = _readonly_layout(cli)
    result["preview"]["workspace"] = layout
    if layout_issues:
        result["issues"].extend(layout_issues)
        return _stop(result, "workspace")
    issues, targets = _output_targets(cli, store, spec, weather, sit_name, met_name, csv_name, prepare_only=prepare_only)
    result["artifacts"]["planned"] = {key: str(path) for key, path in targets.items()}
    if issues:
        result["issues"] = issues
        return _stop(result, "outputs")
    native, error = _native_preflight(cli, mode, prepare_only=prepare_only, allow_window_creation=allow_window_creation)
    if error is not None:
        _issue(result, "native_preflight", error)
        return _stop(result, "native_preflight")
    result["preview"]["native"] = native
    result["warnings"].append("Native capability and licence were checked read-only before any weather request; the measured design itself stays a declared-envelope plan")
    def discover(flat_capacity, project="", member=""):
        return discover_design_inputs(store, pan_name=module.get("file_name") or "", ond_name=inverter.get("file_name") or "",
            module_manufacturer=module.get("manufacturer") or "", module_model=module.get("model") or "",
            inverter_manufacturer=inverter.get("manufacturer") or "", inverter_model=inverter.get("model") or "",
            template_project=spec.get("template_project") or project, variant=spec.get("variant") or member,
            capacity=flat_capacity, tilt=tilt if tilt is not None else 20.0)
    basic, stopped = _phase(result, "discovery", lambda: discover(None))
    if stopped is not None: return stopped
    result["preview"]["discovery"] = basic if verbose else _compact_discovery(basic)
    resolved = basic.get("resolved_components") or {}
    seed_project = f"{stem}_template.PRJ"
    if is_utility:
        fallback = False
        if basic.get("selected") and {"PAN", "OND"} <= set(basic["selected"]):
            components = {"PAN": basic["selected"]["PAN"], "OND": basic["selected"]["OND"]}
            result["selected"] = components
        elif {"PAN", "OND"} <= set(resolved):
            components = {"PAN": resolved["PAN"], "OND": resolved["OND"]}
            result["selected"] = components
        else:
            _inherit(result, basic, "discovery")
            return _stop(result, "discovery")
    else:
        fallback = basic.get("status") != "ready" and {"PAN", "OND"} <= set(resolved) and not (spec.get("template_project") or spec.get("variant"))
        if fallback:
            for item in basic.get("issues") or []:
                message = item.get("message") if isinstance(item, dict) else item
                result["warnings"].append("Rejected template candidate (no seed decision is taken from it): " + str(message))
            result["warnings"].extend(str(item) for item in basic.get("warnings") or [])
            result["warnings"].append(f"No eligible workspace template was found; the flow falls back to the installed standard template derived as {seed_project}/{SEED_VARIANT} with the accepted capacity")
        else:
            _inherit(result, basic, "discovery")
            if basic.get("status") != "ready" or not basic.get("selected"): return _stop(result, "discovery")
        components = basic.get("selected") or resolved
        result["selected"] = basic.get("selected")
    label, label_issue = _identity(components)
    if label_issue is not None:
        _issue(result, "module.inverter", label_issue)
        return _stop(result, "discovery")
    sizing_mode, auto_sizing_review = spec.get("sizing_mode", "strict"), False
    if sizing_mode == "auto_from_components":
        auto_opts = dict(spec.get("auto_sizing") or {})
        gl_flow = spec.get("ground_layout")
        asym_supported = scope in ("ground_sheds", "ground_tracker") and not (
            isinstance(gl_flow, dict) and (gl_flow.get("sub_orientations") or gl_flow.get("pvc_orientation_tolerance_deg") is not None)
        )
        if auto_opts.get("allow_asymmetric_mppt") is None:
            auto_opts["allow_asymmetric_mppt"] = asym_supported
        suggested, stopped = _phase(result, "design_envelope", lambda: suggest_sizing(
            store, pan_name=Path(components["PAN"]["name"]).name, ond_name=Path(components["OND"]["name"]).name,
            target_dc_kw=target, require_even_strings=(scope == "ground_dome"), **auto_opts))
        if stopped is not None: return stopped
        result["preview"]["envelope"] = suggested
        _inherit(result, suggested, "auto_sizing")
        if suggested.get("status") != "ready" or suggested.get("capacity") is None: return _stop(result, "design_envelope")
        flat_capacity = suggested["capacity"]
        auto_sizing_review = True
        result["sizing_assumptions"] = list(suggested.get("assumptions") or [])
        result["warnings"].append("Electrical capacity envelope was auto-estimated from PAN/OND files (sizing_mode='auto_from_components'); review sizing_assumptions before engineering signoff")
    elif design is not None:
        request = dict(design)
        declared = request.get("manufacturer_model")
        if declared is not None and declared != label:
            _issue(result, "electrical_design.manufacturer_model", f"The declared label {declared!r} does not equal the selected identity label {label!r}")
            return _stop(result, "design_envelope")
        request["manufacturer_model"] = declared or label
        request["target_dc_kw"] = target
        envelope, stopped = _phase(result, "design_envelope", lambda: prepare_design_envelope(components["PAN"]["electrical"], components["OND"]["electrical"], request))
        if stopped is not None: return stopped
        result["preview"]["envelope"] = envelope
        _inherit(result, envelope, "electrical_design")
        if envelope["status"] != "ready" or envelope.get("capacity") is None: return _stop(result, "design_envelope")
        flat_capacity = envelope["capacity"]
    else:
        flat_capacity = direct
        result["preview"]["envelope"] = {"status": "supplied", "capacity": dict(direct)}
    utility_plan = None
    if is_utility:
        utility_plan, stopped = _phase(result, "template_capacity", lambda: plan_utility_capacity(
            store, components["PAN"], components["OND"], flat_capacity, require_even_strings=(scope == "ground_dome")))
        if stopped is not None: return stopped
        result["preview"]["utility_capacity"] = utility_plan
        _inherit(result, utility_plan, "template_capacity")
        if utility_plan.get("status") != "ready": return _stop(result, "template_capacity")
        if mode in ("csv", *REMOTE_WEATHER_MODES):
            upreflight, up_issues, up_warnings = preflight_utility_synthesis(
                store, spec=spec, pan_info=components["PAN"], ond_info=components["OND"],
                capacity_plan=utility_plan, sit_name=sit_name, met_name=met_name,
                allow_missing_site=True, allow_missing_met=True, horizon=None,
            )
            if up_issues:
                result["issues"].extend(up_issues)
                return _stop(result, "utility_preflight")
            if upreflight is not None:
                result["preview"]["utility_preflight"] = upreflight["utility_summary"]
                for w in up_warnings:
                    if w not in result["warnings"]:
                        result["warnings"].append(w)
    else:
        confirm, stopped = _phase(result, "template_capacity", lambda: discover(flat_capacity))
        if stopped is not None: return stopped
        result["preview"]["discovery_confirmed"] = confirm if verbose else _compact_discovery(confirm)
        if confirm.get("status") == "ready" and confirm.get("selected"):
            if (drift := _identity_drift(components, confirm["selected"])) is not None:
                _issue(result, "discovery", drift)
                return _stop(result, "discovery")
            components = confirm["selected"]
            result["selected"] = confirm["selected"]
            if fallback:
                fallback = False
                result["warnings"].append("The capacity-confirmed discovery selected an eligible workspace template; the standard-template seed fallback is not used")
            if confirm.get("warnings") != basic.get("warnings"):
                result["warnings"].extend(item for item in (confirm.get("warnings") or []) if item not in (basic.get("warnings") or []))
        elif fallback:
            if conflicts := _seed_conflicts(cli, store, seed_project, targets, stem):
                result["issues"] = conflicts
                return _stop(result, "outputs")
            seed_preview, stopped = _phase(result, "template_seed", lambda: prepare_standard_template(
                cli, store, ond_electrical=components["OND"]["electrical"],
                pan_name=Path(components["PAN"]["name"]).name, ond_name=Path(components["OND"]["name"]).name,
                new_project=seed_project, dry_run=True, capacity=flat_capacity,
                allow_window_creation=allow_window_creation, allow_synthesize=True))
            if stopped is not None: return stopped
            result["preview"]["template_seed"] = seed_preview
            _inherit(result, seed_preview, "template_seed")
            if seed_preview.get("status") != "ready":
                return _stop(result, "template_seed", "failed" if seed_preview.get("status") == "failed" else "blocked")
            result["warnings"].append("The seed preview derives PRJ/VC0 in memory from the installed standard template with the selected components and the accepted capacity; it retains every unlisted default and loss mode and writes nothing yet")
        else:
            _inherit(result, confirm, "template_capacity")
            return _stop(result, "template_capacity")
    template = components.get("template") or {}
    template_project = spec.get("template_project") or template.get("project") or seed_project
    variant = spec.get("variant") or template.get("variant") or SEED_VARIANT
    pan_name, ond_name = Path(components["PAN"]["name"]).name, Path(components["OND"]["name"]).name
    hz_spec = spec.get("horizon")
    if (not is_utility and not fallback and isinstance(hz_spec, dict)
            and hz_spec.get("mode", "points") != "none"
            and isinstance(pname := hz_spec.get("profile_name"), str) and not pname.isascii()):
        tpl_bytes = store._read(store._path(template_project, variant))
        if not tpl_bytes.startswith(b"\xef\xbb\xbf"):
            _issue(result, "horizon.profile_name", "Non-ASCII horizon.profile_name requires a template variant with an explicit UTF-8 BOM")
            return _stop(result, "horizon")
    manifest_name, inherited_review = weather.get("weather_manifest_name") or "", auto_sizing_review
    acquired_loc = None
    def _resolve_hz(acq_loc=None):
        if spec.get("horizon") is None and weather.get("fetch_horizon") is not True:
            return None, None
        from pvsyst_auto.weather.horizon import resolve_design_horizon
        pair, stopped_hz = _phase(result, "horizon", lambda: resolve_design_horizon(
            workspace=cli.workspace, horizon_spec=spec.get("horizon"), weather_spec=weather,
            sit_name=sit_name, acquired_location=acq_loc, dry_run=dry_run))
        if stopped_hz is not None: return None, stopped_hz
        hz_val, hz_info = pair
        if hz_info is not None:
            result["preview"]["horizon"] = hz_info
            if not dry_run:
                result["horizon"] = hz_info
                result["warnings"].extend(hz_info.get("warnings") or ())
        return hz_val, None
    if mode in ("csv", *REMOTE_WEATHER_MODES):
        if mode in REMOTE_WEATHER_MODES:
            def request_weather(run):
                kwargs = dict(csv_name=csv_name, year=year, location_id=weather.get("location_id"),
                    latitude=weather.get("latitude"), longitude=weather.get("longitude"),
                    site_name=weather.get("site_label") or "", altitude=weather.get("altitude"),
                    country_code=country_code, timezone_name=weather.get("timezone_name") or "",
                    temperature_method=weather.get("temperature_method") or TEMPERATURE_METHOD,
                    strict_dhi=bool(weather.get("strict_dhi", False)), dry_run=run)
                if mode != "open_meteo": kwargs["provider"] = _PROVIDER_FOR_MODE[mode]
                return download_weather(cli.workspace, **kwargs)
            plan, stopped = _phase(result, "acquisition", lambda: request_weather(True))
            if stopped is not None: return stopped
            result["preview"]["weather_plan"] = plan
            result["artifacts"]["acquire"] = dict(plan.get("artifacts") or {})
            if plan.get("status") != "ready":
                result["preview"]["weather"] = plan
                _inherit(result, plan, "acquisition")
                return _stop(result, "acquisition", "failed" if plan.get("status") == "failed" else "blocked")
            offset = plan.get("standard_utc_offset_hours")
            if _is_number(offset) and offset > CREATE_SITE_MAX_OFFSET_HOURS:
                _issue(result, "weather.location", f"The resolved place uses a standard UTC offset of {offset:+g} h while create-site accepts at most +{CREATE_SITE_MAX_OFFSET_HOURS} h; nothing was downloaded or written")
                return _stop(result, "acquisition")
            if dry_run:
                _, stopped_hz = _resolve_hz(_acquired_place(plan))
                if stopped_hz is not None: return stopped_hz
                result["preview"]["weather"] = plan
                _inherit(result, plan, "acquisition")
                result["warnings"].append("The acquisition plan is complete but no CSV/SIT/MET exists yet, so the native weather preflight and the annual simulation are planned, not performed")
                return _stop(result, "planned", "ready_for_weather")
            acquire, stopped = _phase(result, "acquisition", lambda: request_weather(False))
            if stopped is not None: return stopped
            result["preview"]["weather"] = acquire
            result["artifacts"]["acquire"] = dict(acquire.get("artifacts") or {})
            _inherit(result, acquire, "acquisition")
            if acquire.get("review_required"): inherited_review = True
            if acquire.get("status") not in ACQUIRE_SUCCESS:
                return _stop(result, "acquisition", "failed" if acquire.get("status") == "failed" else "blocked")
            acquired_loc = _acquired_place(acquire)
            csv_name = (acquire.get("created_files", {}).get("csv") or {}).get("name") or csv_name
            if not country_code and _is_text(place_country := acquired_loc.get("country_code")):
                country_code = place_country
                result["warnings"].append(f"weather.country_code was not supplied; the prepared SIT/MET use the ISO code {country_code} of the acquired place")
        prepare, stopped = _phase(result, "weather_prepare", lambda: prepare_weather(
            cli, csv_name=csv_name, year=year, sit_name=sit_name, met_name=met_name,
            country_code=country_code, time_basis=TIME_BASIS, dry_run=dry_run, allow_window_creation=allow_window_creation))
        if stopped is not None: return stopped
        result["preview"]["prepare"] = prepare
        result["artifacts"]["prepare"] = {key: str(path) for key, path in (prepare.get("artifacts") or {}).items()}
        _inherit(result, prepare, "weather_prepare")
        if prepare.get("status") not in (("ready",) if dry_run else PREPARE_SUCCESS):
            return _stop(result, "weather_prepare", "failed" if prepare.get("status") == "failed" else "blocked")
        if prepare.get("status") == "needs_review": inherited_review = True
        if dry_run:
            _, stopped_hz = _resolve_hz(acquired_loc)
            if stopped_hz is not None: return stopped_hz
            return _stop(result, "planned", "ready_for_weather")
        manifest_name = Path((prepare.get("artifacts") or {}).get("manifest", "")).name or manifest_name
    elif synthetic_weather:
        result["warnings"].append(f"weather.mode='synthetic_from_site' runs PVsystCLI with --synthetic-weather from the monthly site file {sit_name} without a pre-built hourly .MET file.")
    elif not manifest_name:
        result["warnings"].append("No prepared weather manifest was supplied for the existing weather inputs; the native source-weather comparison is skipped and the SIT/MET are only name-checked")
    if not dry_run:
        created, error = _init_layout(cli, store)
        result["preview"]["workspace"]["created_directories"] = created
        if error is not None:
            _issue(result, "workspace_init", error)
            return _stop(result, "workspace_init", "failed")
    resolved_horizon, stopped_hz = _resolve_hz(acquired_loc)
    if stopped_hz is not None: return stopped_hz
    if fallback:
        if dry_run: return _stop(result, "planned", "ready_for_weather")
        seed, stopped = _phase(result, "template_seed", lambda: prepare_standard_template(
            cli, store, ond_electrical=components["OND"]["electrical"], pan_name=pan_name, ond_name=ond_name,
            new_project=seed_project, dry_run=False, capacity=flat_capacity, allow_window_creation=allow_window_creation, allow_synthesize=True,
        ))
        if stopped is not None: return stopped
        result["preview"]["template_seed_write"] = seed
        result["artifacts"]["seed"] = {"project": seed_project, "variant": SEED_VARIANT, "files": seed.get("files")}
        _inherit(result, seed, "template_seed")
        if seed.get("status") != "created":
            result["warnings"].append(RETAINED_NOTE)
            return _stop(result, "template_seed", "failed" if seed.get("status") == "failed" else "blocked")
        confirmed, stopped = _phase(result, "template_seed", lambda: discover(flat_capacity, seed_project, SEED_VARIANT))
        if stopped is not None:
            result["warnings"].append(RETAINED_NOTE)
            return stopped
        result["preview"]["seed_confirmed"] = confirmed if verbose else _compact_discovery(confirmed)
        if confirmed.get("status") != "ready" or not confirmed.get("selected"):
            _inherit(result, confirmed, "template_seed")
            result["warnings"].append(RETAINED_NOTE)
            return _stop(result, "template_seed")
        if (drift := _identity_drift(components, confirmed["selected"])) is not None:
            _issue(result, "discovery", drift)
            result["warnings"].append(RETAINED_NOTE)
            return _stop(result, "discovery")
        components = confirmed["selected"]
        result["selected"] = confirmed["selected"]
        result["warnings"].append(f"The written seed {seed_project}/{SEED_VARIANT} reads back the requested components and counts; the annual simulation runs on the working copy named by new_project")
    phase_name = "project_preparation" if prepare_only else "simulation"
    context = {"preparation_context": {"request": spec, "selected": result["selected"], "envelope": result["preview"]["envelope"], "review_required": inherited_review, "warnings": list(result["warnings"])}} if prepare_only else {}
    if prepare_only and source_context is not None:
        context["preparation_context"]["source_binding"] = source_context
        context["preparation_context"]["review_required"] |= bool(source_context.get("needs_review"))
    report_language = spec.get("report_language", "en")
    if is_utility:
        simulation, stopped = _phase(result, phase_name, lambda: run_utility_design(
            cli, store, spec=spec, pan_info=components["PAN"], ond_info=components["OND"],
            capacity_plan=utility_plan, sit_name=sit_name, met_name=met_name,
            weather_manifest_name=manifest_name, dry_run=dry_run, prepare_only=prepare_only,
            preparation_context=context.get("preparation_context"), allow_window_creation=allow_window_creation,
            synthetic_weather=synthetic_weather, report_language=report_language, horizon=resolved_horizon))
    else:
        runner = prepare_template if prepare_only else simulate_template
        runner_kwargs = {"allow_window_creation": allow_window_creation} if prepare_only else {}
        simulation, stopped = _phase(result, phase_name, lambda: runner(
            cli, store, template_project=template_project, variant=variant, new_project=new_project,
            pan_name=pan_name, ond_name=ond_name, site_name=sit_name, met_name=met_name, tilt=tilt,
            year=year, dry_run=dry_run, capacity=flat_capacity, weather_manifest_name=manifest_name,
            azimuth=azimuth, losses=losses or None, report_pages=spec.get("report_pages"),
            synthetic_weather=synthetic_weather, report_language=report_language,
            p50_p90=spec.get("p50_p90"), horizon=resolved_horizon, **context, **runner_kwargs))
    if stopped is not None:
        result["warnings"].append(RETAINED_NOTE)
        return stopped
    result["preview"][phase_name] = simulation
    result["artifacts"]["project_preparation" if prepare_only else "simulate"] = dict(simulation.get("artifacts") or {})
    result["metrics"] = simulation.get("metrics")
    for k in ("diagnostics", "bifacial_metrics", "electrical_shading_metrics", "utility_summary", "p50_p90"):
        if simulation.get(k) is not None: result[k] = simulation[k]
    result["recovery"] = list(simulation.get("recovery") or [])
    _inherit(result, simulation, phase_name)
    accepted = ("prepared", "needs_review") if prepare_only else SIMULATE_SUCCESS
    if simulation.get("status") not in (("ready",) if dry_run else accepted):
        if simulation.get("status") == "failed": result["warnings"].append(RETAINED_NOTE)
        return _stop(result, simulation.get("stage") or phase_name, "failed" if simulation.get("status") == "failed" else "blocked")
    result["execution_preflight_complete"] = True
    if dry_run: return _stop(result, simulation.get("stage") or "preflight", "ready")
    if prepare_only:
        result["prepared"] = simulation.get("prepared", False)
        result["prepared_project"] = {"project": new_project, "variant": variant, "year": year, "site": sit_name, "met": met_name, "manifest": simulation["artifacts"].get("manifest")}
    elif is_utility: result["native_consumption_verified"] = bool(simulation.get("native_consumption_verified", False))
    result["status"] = "needs_review" if inherited_review or simulation["status"] == "needs_review" else ("prepared" if prepare_only else "completed")
    result["stage"] = simulation.get("stage") or ("prepared" if prepare_only else "completed")
    return result
def simulate_design(cli, store, *, spec: dict, dry_run=True, verbose=False):
    """Run the unified single-project design flow for one workspace."""
    with store.lock:
        return _design_flow(cli, store, spec, dry_run, verbose=verbose)
def prepare_design_project(cli, store, *, spec: dict, dry_run=True, source_context=None, allow_window_creation=None, verbose=False):
    """Prepare without an annual run; optional already-bound source provenance stays metadata."""
    with store.lock:
        return _design_flow(cli, store, spec, dry_run, prepare_only=True, source_context=source_context,
                            allow_window_creation=allow_window_creation, verbose=verbose)

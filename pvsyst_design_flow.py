"""One unified single-project design flow: discovery, design envelope, weather and annual simulation.

Scope
-----
One new single-subarray, single-ordinary-fixed-plane, unshaded grid project per call. No
project comparison, no multi-subarray layout, no binary component analysis. Everything the
selected manufacturer files do not declare must be in the spec: nothing is inferred, no
template count is silently reused, and no manufacturer certification is implied.

This module owns the ordering only:
strict spec gate, output-name preflight, read-only native capability/licence check,
discovery without capacity, the pure design envelope, a second discovery that confirms the
accepted capacity against the template hardware, weather acquisition/preparation, then one
annual simulation. Each phase accepts only its own explicit success status; anything else
stops the flow with that phase named. A dry run writes nothing.

Reused building blocks (no new job framework, no hashing or fingerprinting here):
``discover_design_inputs``, ``prepare_design_envelope``, ``download_weather``,
``prepare_weather`` and ``simulate_template``.
"""
from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path

from pvsyst_cli import PVsystError, validate_report_pages
from pvsyst_design_envelope import REQUEST_KEYS, prepare_design_envelope
from pvsyst_design_inputs import discover_design_inputs
from pvsyst_paths import project_name, variant_id, workspace_file, workspace_folder
from pvsyst_simulation import simulate_template, prepare_template
from pvsyst_sizing import NUMBERS as CAPACITY_NUMBERS
from pvsyst_standard_template import SEED_VARIANT, prepare_standard_template
from pvsyst_weather_acquisition import download_weather
from pvsyst_weather_flow import prepare_weather

SCOPE = "fixed_unshaded_grid"
TIME_BASIS = "local_standard_interval_start"
# The verified PVsyst 8.1 workspace layout: 17 root folders plus 9 inside ComposPV. This 26-folder
# superset is what a real bootstrap proved; it is not trimmed to a guessed minimum.
EXPECTED_WORKSPACE_BASENAME = "PVsyst8.1_Data"
LAYOUT_ROOT_FOLDERS = ("ComposPV", "DataMeas", "Meteo", "Models", "Other", "Projects", "Results",
                       "Shadings", "Sites", "Templates", "UserBatch", "UserData", "UserHourly",
                       "UserHourlyParams", "UserImages", "UserOptimization", "UserRecycleBin")
LAYOUT_COMPOSPV_FOLDERS = ("Batteries", "Clients", "Gensets", "Inverters", "Pumps", "PVmodules",
                           "Regulators", "RegulPmp", "Sellers")
TEMPERATURE_METHOD = "endpoint_mean"   # the only method verified inside pvsyst_weather_acquisition
# Acquisition accepts -12..14, but create-site does not: an offset above this is stopped before any write.
CREATE_SITE_MAX_OFFSET_HOURS = 12
SIMULATION_OPTIONS = ("input-sfi-file", "start-date", "end-date", "time-step")
SITE_OPTIONS = ("site-name", "altitude", "timezone", "country-code")
LOCATION_FIELDS = ("latitude", "longitude", "site_label", "altitude", "timezone_name")
ISO_COUNTRY = re.compile(r"[A-Z]{2}")

SPEC_KEYS = frozenset((
    "scope", "new_project", "year", "target_dc_kw", "tilt", "azimuth", "module", "inverter",
    "template_project", "variant", "capacity", "electrical_design", "losses", "weather", "report_pages",
))
EQUIPMENT_KEYS = frozenset(("file_name", "manufacturer", "model"))
WEATHER_KEYS = frozenset((
    "mode", "sit_name", "met_name", "csv_name", "country_code", "time_basis", "location_id",
    "latitude", "longitude", "site_label", "altitude", "timezone_name", "temperature_method",
    "weather_manifest_name",
))
WEATHER_MODES = ("existing", "csv", "open_meteo")
LOSS_KEYS = frozenset(("LIDLoss", "PMismMPP", "ModuleQualityLoss", "UFactor"))
CAPACITY_KEYS = frozenset((*CAPACITY_NUMBERS, "capacity_tolerance_pct", "voltage_standard", "design_basis"))
ENVELOPE_NESTED = ("temperatures", "envelope", "coefficients", "module_limits", "mppt_limits",
                   "effective_irradiance")
STATUSES = ("blocked", "ready_for_weather", "ready", "completed", "needs_review", "failed")
ACQUIRE_SUCCESS = ("prepared",)
PREPARE_SUCCESS = ("prepared", "needs_review")
SIMULATE_SUCCESS = ("completed", "needs_review")
RETAINED_NOTE = ("Produced provider JSON, CSV, preparation artifacts, the working project and the "
                 "recovery steps are retained; no automatic cleanup, overwrite or rollback was "
                 "performed and a documented recovery is never reported as success")


def _is_number(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _is_int(value) -> bool:
    return type(value) is int


def _is_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.isprintable()


def _prune_empty(value):
    """Drop unset optionals - None and blank strings - the shape a typed server sends by default.

    A blank text field means "not supplied", so it becomes absent instead of invalid; required
    fields then report a missing input and numeric/boolean fields still reject a blank string
    because it never becomes a number. False and 0 are real values and are kept.
    """
    if isinstance(value, dict):
        pruned = {}
        for key, item in value.items():
            item = _prune_empty(item)
            if item is None or (isinstance(item, str) and not item.strip()):
                continue
            pruned[key] = item
        return pruned
    if isinstance(value, list):
        return [_prune_empty(item) for item in value]
    return value


def _issue(result: dict, field: str, message) -> None:
    result["issues"].append({"field": field, "message": str(message)})


def _inherit(result: dict, source: dict, field: str) -> None:
    """Carry one phase's issues/warnings/review_required/error up, normalizing plain strings.

    A phase that fails without an issue list still reports the reason here; the full phase result,
    including its own error and diagnostics, stays in ``preview``.
    """
    for item in source.get("issues") or []:
        result["issues"].append(item if isinstance(item, dict) else {"field": field, "message": str(item)})
    error = source.get("error")
    if isinstance(error, str) and error.strip():
        reported = {item.get("message") for item in result["issues"] if isinstance(item, dict)}
        if error not in reported:
            result["issues"].append({"field": field, "message": error})
    for item in source.get("warnings") or []:
        if isinstance(item, dict):
            result["warnings"].append(f"{item.get('field')}: {item.get('message')}")
        else:
            result["warnings"].append(str(item))
    for item in source.get("review_required") or []:
        result["warnings"].append(f"{field}.review_required: {item}")


def _stop(result: dict, stage: str, status: str = "blocked") -> dict:
    result.update(status=status, stage=stage)
    return result


_PHASE_ERRORS = (ValueError, OSError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired)


def _phase(result: dict, stage: str, call):
    """Run one phase call; a raised error becomes a failed stage with the artifacts kept."""
    try:
        return call(), None
    except _PHASE_ERRORS as exc:
        _issue(result, stage, getattr(exc, "diagnostics", None) or str(exc))
        return None, _stop(result, stage, "failed")


def _check_equipment(selector, kind, spec_issues):
    def bad(field, message):
        spec_issues.append({"field": field, "message": message})

    field = kind.lower()
    if not isinstance(selector, dict):
        bad(field, f"Provide the {field} component selector object")
        return
    for key in sorted(set(selector) - EQUIPMENT_KEYS):
        bad(f"{field}.{key}", "Unknown component selector input")
    file_name = selector.get("file_name")
    manufacturer = selector.get("manufacturer")
    model = selector.get("model")
    if file_name is not None:
        if not _is_text(file_name):
            bad(f"{field}.file_name", "Use one loose filename without directories")
        elif Path(file_name).name != file_name or file_name != file_name.strip():
            bad(f"{field}.file_name", "Pass a plain filename; the workspace resolves the library path")
    for key, value in (("manufacturer", manufacturer), ("model", model)):
        if value is not None and not _is_text(value):
            bad(f"{field}.{key}", "Use printable text")
    if not _is_text(file_name) and not (_is_text(manufacturer) and _is_text(model)):
        bad(f"{field}.name", f"Provide one exact manufacturer+model pair or an explicit {field} filename")


def _check_capacity(capacity, target, spec_issues):
    def bad(field, message):
        spec_issues.append({"field": field, "message": message})

    if not isinstance(capacity, dict):
        bad("capacity", "Provide the direct capacity object")
        return
    for key in sorted(set(capacity) - CAPACITY_KEYS):
        bad(f"capacity.{key}", "Unknown capacity input")
    for name in CAPACITY_NUMBERS:
        value = capacity.get(name)
        if value is None:
            bad(f"capacity.{name}", "Required design input is missing; no value is inferred")
        elif not _is_number(value):
            bad(f"capacity.{name}", "Use a finite strict numeric value")
        elif not name.startswith("design_") and value <= 0:
            bad(f"capacity.{name}", "Design magnitudes must be positive")
    if _is_number(capacity.get("target_dc_kw")) and float(capacity["target_dc_kw"]) != target:
        bad("capacity.target_dc_kw",
            f"capacity.target_dc_kw {capacity['target_dc_kw']} must equal spec.target_dc_kw {target}")
    tolerance = capacity.get("capacity_tolerance_pct")
    if tolerance is not None and (not _is_number(tolerance) or not 0 <= tolerance <= 20):
        bad("capacity.capacity_tolerance_pct", "Use 0..20 percent")
    if capacity.get("voltage_standard") not in ("IEC", "UL"):
        bad("capacity.voltage_standard", "Explicitly choose IEC or UL")
    basis = capacity.get("design_basis")
    if not _is_text(basis) or len(basis) > 2000:
        bad("capacity.design_basis", "Provide a printable design basis up to 2000 characters")


def _check_design(design, target, spec_issues):
    def bad(field, message):
        spec_issues.append({"field": field, "message": message})

    if not isinstance(design, dict):
        bad("electrical_design", "Provide the declared manufacturer design object")
        return
    for key in sorted(set(design) - REQUEST_KEYS):
        bad(f"electrical_design.{key}", "Unknown design-envelope input")
    declared = design.get("target_dc_kw")
    if declared is not None:
        if not _is_number(declared):
            bad("electrical_design.target_dc_kw", "Use a finite strict numeric target")
        elif float(declared) != target:
            bad("electrical_design.target_dc_kw",
                f"The declared target {declared} contradicts spec.target_dc_kw {target}; "
                "the unified entry injects one target and never reconciles two")
    for name in ENVELOPE_NESTED:
        value = design.get(name)
        if value is not None and not isinstance(value, dict):
            bad(f"electrical_design.{name}", "Provide this design-envelope input as an object")
    label = design.get("manufacturer_model")
    if label is not None and not _is_text(label):
        bad("electrical_design.manufacturer_model", "Use printable text")


def _check_weather(weather, spec_issues):
    def bad(field, message):
        spec_issues.append({"field": field, "message": message})

    if not isinstance(weather, dict):
        bad("weather", "Provide the weather request object")
        return
    for key in sorted(set(weather) - WEATHER_KEYS):
        bad(f"weather.{key}", "Unknown weather input")
    mode = weather.get("mode")
    if mode not in WEATHER_MODES:
        bad("weather.mode", "Choose existing, csv or open_meteo")
        return
    country_code = weather.get("country_code")
    if country_code is not None and (not _is_text(country_code) or not ISO_COUNTRY.fullmatch(country_code)):
        bad("weather.country_code", "Use an explicit uppercase ISO alpha-2 country code")
    if weather.get("time_basis") is not None and weather.get("time_basis") != TIME_BASIS:
        bad("weather.time_basis", "Declare " + TIME_BASIS)
    for key in ("sit_name", "met_name", "csv_name"):
        value = weather.get(key)
        if value is not None and not _is_text(value):
            bad(f"weather.{key}", "Use one filename without directories")
    if mode == "existing":
        for key in ("sit_name", "met_name"):
            if not _is_text(weather.get(key)):
                bad(f"weather.{key}", "Existing weather requires an explicit workspace SIT and MET filename")
        return
    if mode == "csv":
        if not _is_text(weather.get("csv_name")):
            bad("weather.csv_name", "Provide the staged workspace weather CSV filename")
        if not _is_text(weather.get("country_code")):
            bad("weather.country_code", "A staged CSV requires the explicit uppercase ISO alpha-2 code")
        if weather.get("time_basis") != TIME_BASIS:
            bad("weather.time_basis", "A staged CSV requires " + TIME_BASIS)
    if mode == "open_meteo":
        location_id = weather.get("location_id")
        if location_id is not None and any(weather.get(name) is not None
                                           for name in ("latitude", "longitude")):
            bad("weather.location_id", "Pass either location_id or explicit coordinates, not both")
        if location_id is None:
            # a manual place never inherits anything from the provider, country code included
            missing = [name for name in LOCATION_FIELDS if weather.get(name) is None]
            if missing:
                bad("weather.location_id",
                    "Provide location_id from search_locations, or the complete explicit place; missing: "
                    + ", ".join(missing))
        elif not _is_int(location_id) or location_id <= 0:
            bad("weather.location_id", "Use a positive integer location_id from search_locations")
        for name in ("latitude", "longitude", "altitude"):
            value = weather.get(name)
            if value is not None and not _is_number(value):
                bad(f"weather.{name}", "Use a finite strict number")
        if _is_number(weather.get("latitude")) and not -90 <= weather["latitude"] <= 90:
            bad("weather.latitude", "Latitude must lie in -90..90")
        if _is_number(weather.get("longitude")) and not -180 <= weather["longitude"] <= 180:
            bad("weather.longitude", "Longitude must lie in -180..180")
        method = weather.get("temperature_method")
        if method is not None and method != TEMPERATURE_METHOD:
            bad("weather.temperature_method", "Only " + TEMPERATURE_METHOD + " is verified")


def _validate_spec(spec) -> list[dict]:
    issues: list[dict] = []

    def bad(field, message):
        issues.append({"field": field, "message": str(message)})

    if not isinstance(spec, dict):
        bad("spec", "Provide the design-flow spec object")
        return issues
    for key in sorted(set(spec) - SPEC_KEYS):
        bad(str(key), "Unknown design-flow spec input")
    try:
        validate_report_pages(spec.get("report_pages"))
    except ValueError as exc:
        bad("report_pages", exc)
    if spec.get("scope", SCOPE) != SCOPE:
        bad("scope", "Only fixed_unshaded_grid is supported; a roof or free-standing scope would need "
                     "thermal and loss inputs this flow never infers")

    new_project = spec.get("new_project")
    if not _is_text(new_project):
        bad("new_project", "Provide the new project filename")
    else:
        try:
            project_name(new_project)
        except ValueError as exc:
            bad("new_project", exc)

    year = spec.get("year")
    if not _is_int(year) or not 1969 <= year <= 2068:
        bad("year", "Use an integer year in 1969..2068; native dates carry two-digit years")

    target = spec.get("target_dc_kw")
    if not _is_number(target) or target <= 0:
        bad("target_dc_kw", "Provide a finite positive DC target in kW; no target is inferred")
    else:
        target = float(target)

    for name, low, high in (("tilt", 0, 90), ("azimuth", -180, 180)):
        value = spec.get(name)
        note = ("Use a finite tilt in 0..90 degrees" if name == "tilt"
                else "Use finite PVsyst azimuth in -180..180 degrees, zero south and west positive")
        if value is None or not _is_number(value) or not low <= value <= high:
            bad(name, note)

    _check_equipment(spec.get("module"), "module", issues)
    _check_equipment(spec.get("inverter"), "inverter", issues)

    template_project = spec.get("template_project")
    if template_project is not None:
        if not _is_text(template_project):
            bad("template_project", "Use one workspace PRJ filename")
        else:
            try:
                project_name(template_project)
            except ValueError as exc:
                bad("template_project", exc)
    variant = spec.get("variant")
    if variant is not None:
        if not _is_text(variant):
            bad("variant", "Use one VC identifier or omit it")
        else:
            try:
                variant_id(variant)
            except ValueError as exc:
                bad("variant", exc)

    capacity = spec.get("capacity")
    design = spec.get("electrical_design")
    if capacity is None and design is None:
        bad("capacity", "Provide exactly one of capacity (direct design envelope) or electrical_design "
                        "(declared manufacturer inputs); a template count is never used instead")
    elif capacity is not None and design is not None:
        bad("electrical_design", "Provide exactly one of capacity or electrical_design; they are mutually exclusive")
    if capacity is not None and _is_number(target):
        _check_capacity(capacity, target, issues)
    if design is not None and _is_number(target):
        _check_design(design, target, issues)

    losses = spec.get("losses")
    if losses is not None:
        if not isinstance(losses, dict):
            bad("losses", "Provide an explicit supported loss object")
        else:
            for key in sorted(set(losses) - LOSS_KEYS):
                bad(f"losses.{key}", "Unsupported loss parameter")
            for key, value in losses.items():
                if key in LOSS_KEYS and not _is_number(value):
                    bad(f"losses.{key}", "Require a supported finite numeric loss parameter")

    _check_weather(spec.get("weather"), issues)
    return issues


def _entries(root):
    """Workspace project entries; an absent folder is an empty workspace, nothing else is."""
    try:
        return list(root.iterdir())
    except FileNotFoundError:
        return []


def _output_targets(cli, store, spec, weather, sit_name, met_name, csv_name, *, prepare_only=False):
    """Every name the flow could create, checked before the first write of any phase."""
    issues, targets = [], {}
    new_project = spec["new_project"]
    stem = Path(new_project).stem
    year = spec["year"]
    try:
        root = store._root()
        entries = _entries(root)
        if any(store._belongs_to_project(path, new_project) for path in entries):
            issues.append({"field": "new_project",
                           "message": "Working project files already exist; choose a new project filename"})
    except (ValueError, OSError, AttributeError) as exc:
        issues.append({"field": "new_project", "message": str(exc)})
    try:
        if prepare_only:
            prefix = f"{stem}_prepare_{year}"
            targets["project_manifest"] = workspace_file(cli.workspace, "Results", prefix + ".json", ".json")
        else:
            prefix = f"{stem}_annual_{year}"
            targets["annual_csv"] = workspace_file(cli.workspace, "Results", prefix + ".csv", ".csv")
            targets["annual_pdf"] = workspace_file(cli.workspace, "Results", prefix + ".pdf", ".pdf")
            targets["annual_manifest"] = workspace_file(cli.workspace, "Results", prefix + ".json", ".json")
            targets["annual_sfi"] = workspace_file(cli.workspace, "Models", prefix + ".SFI", ".SFI")
        # These derivations mirror pvsyst_weather_flow.prepare_weather and
        # pvsyst_weather_acquisition.download_weather; keep the three in step.
        if weather["mode"] in ("csv", "open_meteo"):
            met_stem = Path(met_name).stem
            targets["sit"] = workspace_file(cli.workspace, "Sites", sit_name, ".SIT")
            targets["met"] = workspace_file(cli.workspace, "Meteo", met_name, ".MET")
            targets["weather_import_csv"] = workspace_file(cli.workspace, "Meteo", f"{met_stem}_import.csv", ".csv")
            targets["weather_monthly_csv"] = workspace_file(cli.workspace, "Meteo", f"{met_stem}_monthly.csv", ".csv")
            targets["weather_mef"] = workspace_file(cli.workspace, "Meteo", f"{met_stem}.MEF", ".MEF")
            targets["weather_manifest"] = workspace_file(cli.workspace, "Results", f"{met_stem}_weather.json", ".json")
        if weather["mode"] == "open_meteo":
            csv_stem = Path(csv_name).stem
            targets["source_csv"] = workspace_file(cli.workspace, "Meteo", csv_name, ".csv")
            targets["provider_raw"] = workspace_file(cli.workspace, "Meteo", f"{csv_stem}_openmeteo_raw.json", ".json")
            targets["acquisition_manifest"] = workspace_file(cli.workspace, "Results", f"{csv_stem}_acquisition.json", ".json")
    except (ValueError, OSError) as exc:
        issues.append({"field": "outputs", "message": str(exc)})
        return issues, {}
    seen = {}
    for label, path in targets.items():
        key = str(path).casefold()
        if key in seen:
            issues.append({"field": label, "message":
                           f"Two planned outputs resolve to the same file: {path.name} "
                           f"(also planned as {seen[key]}); choose distinct names"})
        else:
            seen[key] = label
        if path.exists():
            issues.append({"field": label, "message": "Destination already exists: " + path.name})
    return issues, targets


def _seed_conflicts(cli, store, seed_project, targets, stem):
    """New-only, filename and name checks for the derived seed template, before anything is written."""
    issues = []
    try:
        project_name(seed_project)
    except ValueError:
        issues.append({"field": "seed", "message":
                       f"new_project stem {stem!r} derives a seed filename of "
                       f"{len(seed_project)} characters, beyond the 180-character project filename "
                       "limit; supply a shorter new_project name instead of truncating the derived name"})
    try:
        root = store._root()
        if any(store._belongs_to_project(path, seed_project) for path in _entries(root)):
            issues.append({"field": "seed", "message":
                           "The derived seed project already exists; a seed template is created new-only"})
    except (ValueError, OSError, AttributeError) as exc:
        issues.append({"field": "seed", "message": str(exc)})
    name = Path(seed_project).name.casefold()
    for label, path in targets.items():
        if path.name.casefold() == name:
            issues.append({"field": "seed", "message":
                           f"The derived seed name {Path(seed_project).name} collides with the planned "
                           f"{label} file {path.name}; choose another new_project name"})
    return issues


def _layout_entries():
    """Every standard folder as (label, path parts), root folders first."""
    for folder in LAYOUT_ROOT_FOLDERS:
        yield folder, (folder,)
    for folder in LAYOUT_COMPOSPV_FOLDERS:
        yield f"ComposPV/{folder}", ("ComposPV", folder)


def _resolve_layout(cli):
    """Map each standard folder to its boundary-checked path, or to its escape error.

    The whole relative path is resolved through ``workspace_folder`` in one call, so a nested
    junction or symlink (ComposPV/Batteries) cannot escape the workspace unnoticed while a missing
    folder stays an ordinary path.
    """
    resolved, errors = {}, {}
    for label, parts in _layout_entries():
        try:
            resolved[label] = workspace_folder(cli.workspace, "/".join(parts))
        except (ValueError, OSError) as exc:
            errors[label] = str(exc)
    return resolved, errors


def _readonly_layout(cli):
    """Preflight the canonical root name and the standard folders; nothing is created or moved."""
    issues = []
    layout = {"expected_basename": EXPECTED_WORKSPACE_BASENAME, "workspace": str(cli.workspace),
              "basename": Path(cli.workspace).name, "root_folders": list(LAYOUT_ROOT_FOLDERS),
              "compos_pv_folders": list(LAYOUT_COMPOSPV_FOLDERS), "present": [], "missing": [],
              "file_conflicts": [], "escaped": {}, "created_directories": []}
    if layout["basename"] != EXPECTED_WORKSPACE_BASENAME:
        issues.append({"field": "workspace", "message":
                       f"The workspace root is named {layout['basename']!r}; the verified PVsyst 8.1 layout "
                       f"is {EXPECTED_WORKSPACE_BASENAME!r}. A root is never renamed, moved or replaced"})
    resolved, errors = _resolve_layout(cli)
    layout["escaped"] = errors
    for label, reason in sorted(errors.items()):
        issues.append({"field": "workspace." + label,
                       "message": f"The workspace folder does not stay inside the workspace: {reason}"})
    for label, path in resolved.items():
        if path.is_dir():
            layout["present"].append(label)
        elif path.exists():
            layout["file_conflicts"].append(label)
        else:
            layout["missing"].append(label)
    for label in layout["file_conflicts"]:
        # Known before any weather request: a file where a standard folder belongs is a hard block,
        # and the execution step re-checks it in case the file appears after this preflight.
        issues.append({"field": "workspace." + label,
                       "message": f"{label} exists and is not a directory; an existing file is never "
                                  "overwritten or replaced"})
    return issues, layout


def _init_layout(cli, store):
    """Create the missing standard folders; a file, an escape or a permission error fails.

    Returns (created, error). Nothing is overwritten, no settings, component or project file is
    copied and no reverse engineering of the root is attempted.
    """
    try:
        store._root()
    except (ValueError, OSError, AttributeError) as exc:
        return [], str(exc)
    resolved, errors = _resolve_layout(cli)
    if errors:
        return [], "; ".join(f"{label}: {reason}" for label, reason in sorted(errors.items()))
    created = []
    for label, path in resolved.items():
        if path.is_dir():
            continue
        if path.exists():
            return created, (f"{label} already exists and is not a directory; an existing file is never "
                             "overwritten or replaced")
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return created, f"{label}: {exc}"
        created.append(label)
    return created, None


def _acquired_place(acquire):
    """The place record of one acquisition result, whichever public name it uses."""
    for key in ("location", "site"):
        value = acquire.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _identity_drift(previous, current):
    """Detect components that changed between two discovery passes of one call."""
    for kind in ("PAN", "OND"):
        first, second = previous[kind], current[kind]
        identity = ((Path(first["name"]).name, first.get("library"), first.get("manufacturer"),
                     first.get("model")))
        if identity != (Path(second["name"]).name, second.get("library"), second.get("manufacturer"),
                        second.get("model")):
            return (f"The selected {kind} changed between the first discovery and the capacity "
                    "confirmation; the accepted envelope is never applied to a different component")
        if first.get("electrical") != second.get("electrical"):
            return (f"The declared {kind} electrical values changed between the first discovery and "
                    "the capacity confirmation; the accepted envelope is never applied to changed data")
    return None


def _native_preflight(cli, mode, *, prepare_only=False):
    """Read-only capability/licence check run before any weather download.

    ``simulate_template`` and ``prepare_weather`` repeat these checks; running them here means a
    broken CLI or exhausted licence stops the flow before a provider request is issued.
    """
    state = {}
    try:
        if not prepare_only:
            for option in SIMULATION_OPTIONS:
                cli._require("run-simulation", option)
        if mode != "existing":
            for option in SITE_OPTIONS:
                cli._require("create-site", option)
            cli.command_help("convert-meteo")
        state["version"] = cli.version()
        licence = cli.license_info()
        state["license"] = licence
        if not prepare_only and licence.get("status") == "TRIAL" and licence.get("remaining_executions") == 0:
            raise ValueError("PVsystCLI reports no remaining trial executions")
    except (ValueError, OSError, PVsystError, subprocess.TimeoutExpired) as exc:
        return None, cli._redact(str(exc))
    return state, None


def _identity(selected):
    pan, ond = selected["PAN"], selected["OND"]
    for kind, item in (("PAN", pan), ("OND", ond)):
        if not _is_text(item.get("manufacturer")) or not _is_text(item.get("model")):
            return None, (f"The selected {kind} declares no manufacturer/model; the unified entry "
                          "binds one explicit identity label or nothing")
    return f"{pan['manufacturer']} {pan['model']} / {ond['manufacturer']} {ond['model']}", None


def _design_flow(cli, store, spec, dry_run, *, prepare_only=False):
    result = {"status": "blocked", "stage": "spec", "dry_run": dry_run,
              "execution_preflight_complete": False, "issues": [], "warnings": [],
              "selected": None,
              "preview": {"discovery": None, "envelope": None, "weather": None, "prepare": None,
                          "simulation": None, "template_seed": None, "seed_confirmed": None,
                          "workspace": None},
              "metrics": None,
              "artifacts": {"acquire": {}, "prepare": {}, "simulate": {}, "seed": {}},
              "recovery": []}
    if prepare_only:
        result.update(operation="prepare_project", prepared=False, simulated=False,
                      native_consumption_verified=False)
    if type(dry_run) is not bool:
        _issue(result, "dry_run", "Use a strict boolean")
        return result
    if isinstance(spec, dict):
        spec = _prune_empty(spec)
    if issues := _validate_spec(spec):
        result["issues"] = issues
        return result

    year = spec["year"]
    tilt = float(spec["tilt"])
    azimuth = float(spec["azimuth"])
    target = float(spec["target_dc_kw"])
    losses = spec.get("losses")
    new_project = spec["new_project"]
    module, inverter, weather = spec["module"], spec["inverter"], spec["weather"]
    design, direct = spec.get("electrical_design"), spec.get("capacity")
    mode = weather["mode"]
    stem = Path(new_project).stem
    sit_name = weather.get("sit_name") or f"{stem}.SIT"
    met_name = weather.get("met_name") or f"{stem}.MET"
    csv_name = weather.get("csv_name") or (f"{stem}_openmeteo.csv" if mode == "open_meteo" else "")
    country_code = weather.get("country_code") or ""

    layout_issues, layout = _readonly_layout(cli)
    result["preview"]["workspace"] = layout
    if layout_issues:
        result["issues"].extend(layout_issues)
        return _stop(result, "workspace")

    issues, targets = _output_targets(cli, store, spec, weather, sit_name, met_name, csv_name,
                                      prepare_only=prepare_only)
    result["artifacts"]["planned"] = {key: str(path) for key, path in targets.items()}
    if issues:
        result["issues"] = issues
        return _stop(result, "outputs")

    native, error = _native_preflight(cli, mode, prepare_only=prepare_only)
    if error is not None:
        _issue(result, "native_preflight", error)
        return _stop(result, "native_preflight")
    result["preview"]["native"] = native
    result["warnings"].append(
        "Native capability and licence were checked read-only before any weather request; the "
        "measured design itself stays a declared-envelope plan")

    def discover(flat_capacity, project="", member=""):
        return discover_design_inputs(
            store,
            pan_name=module.get("file_name") or "",
            ond_name=inverter.get("file_name") or "",
            module_manufacturer=module.get("manufacturer") or "",
            module_model=module.get("model") or "",
            inverter_manufacturer=inverter.get("manufacturer") or "",
            inverter_model=inverter.get("model") or "",
            template_project=spec.get("template_project") or project,
            variant=spec.get("variant") or member,
            capacity=flat_capacity, tilt=tilt)

    basic, stopped = _phase(result, "discovery", lambda: discover(None))
    if stopped is not None:
        return stopped
    result["preview"]["discovery"] = basic
    resolved = basic.get("resolved_components") or {}
    seed_project = f"{stem}_template.PRJ"
    # A workspace with no eligible template can still be seeded from the installed standard template,
    # but only when both components resolved exactly and the caller pinned no template of its own.
    fallback = (basic.get("status") != "ready" and {"PAN", "OND"} <= set(resolved)
                and not (spec.get("template_project") or spec.get("variant")))
    if fallback:
        # Rejected candidates stay visible; their issues never become the reason for a fake success.
        for item in basic.get("issues") or []:
            message = item.get("message") if isinstance(item, dict) else item
            result["warnings"].append("Rejected template candidate (no seed decision is taken from it): "
                                      + str(message))
        result["warnings"].extend(str(item) for item in basic.get("warnings") or [])
        result["warnings"].append(
            "No eligible workspace template was found; the flow falls back to the installed standard "
            f"template derived as {seed_project}/{SEED_VARIANT} with the accepted capacity")
    else:
        _inherit(result, basic, "discovery")
        if basic.get("status") != "ready" or not basic.get("selected"):
            return _stop(result, "discovery")
    components = basic.get("selected") or resolved
    result["selected"] = basic.get("selected")

    label, label_issue = _identity(components)
    if label_issue is not None:
        _issue(result, "module.inverter", label_issue)
        return _stop(result, "discovery")

    if design is not None:
        request = dict(design)
        declared = request.get("manufacturer_model")
        if declared is not None and declared != label:
            _issue(result, "electrical_design.manufacturer_model",
                   f"The declared label {declared!r} does not equal the selected identity label {label!r}")
            return _stop(result, "design_envelope")
        request["manufacturer_model"] = declared or label
        request["target_dc_kw"] = target
        envelope, stopped = _phase(result, "design_envelope", lambda: prepare_design_envelope(
            components["PAN"]["electrical"], components["OND"]["electrical"], request))
        if stopped is not None:
            return stopped
        result["preview"]["envelope"] = envelope
        _inherit(result, envelope, "electrical_design")
        if envelope["status"] != "ready" or envelope.get("capacity") is None:
            return _stop(result, "design_envelope")
        flat_capacity = envelope["capacity"]
        result["warnings"].append(
            "The design envelope is a public linear relation over declared manufacturer data bound to "
            f"the selected identity {label}; it is a source binding, not a manufacturer certification")
    else:
        flat_capacity = direct
        result["preview"]["envelope"] = {"status": "supplied",
                                         "capacity": dict(direct), "scope": "direct capacity input"}
        result["warnings"].append(
            "The capacity object was supplied directly; the flow confirms it against the template "
            "hardware but does not re-derive or replace any declared design input")

    confirm, stopped = _phase(result, "template_capacity", lambda: discover(flat_capacity))
    if stopped is not None:
        return stopped
    result["preview"]["discovery_confirmed"] = confirm
    if confirm.get("status") == "ready" and confirm.get("selected"):
        drift = _identity_drift(components, confirm["selected"])
        if drift is not None:
            _issue(result, "discovery", drift)
            return _stop(result, "discovery")
        components = confirm["selected"]
        result["selected"] = confirm["selected"]
        if confirm.get("warnings") != basic.get("warnings"):
            result["warnings"].extend(item for item in (confirm.get("warnings") or [])
                                      if item not in (basic.get("warnings") or []))
    elif fallback:
        conflicts = _seed_conflicts(cli, store, seed_project, targets, stem)
        if conflicts:
            result["issues"] = conflicts
            return _stop(result, "outputs")
        seed_preview, stopped = _phase(result, "template_seed", lambda: prepare_standard_template(
            cli, store, ond_electrical=components["OND"]["electrical"],
            pan_name=Path(components["PAN"]["name"]).name,
            ond_name=Path(components["OND"]["name"]).name,
            new_project=seed_project, dry_run=True, capacity=flat_capacity))
        if stopped is not None:
            return stopped
        result["preview"]["template_seed"] = seed_preview
        _inherit(result, seed_preview, "template_seed")
        if seed_preview.get("status") != "ready":
            return _stop(result, "template_seed",
                         "failed" if seed_preview.get("status") == "failed" else "blocked")
        result["warnings"].append(
            "The seed preview derives PRJ/VC0 in memory from the installed standard template with the "
            "selected components and the accepted capacity; it retains every unlisted default and loss "
            "mode and writes nothing yet")
    else:
        _inherit(result, confirm, "template_capacity")
        return _stop(result, "template_capacity")

    template = components.get("template") or {}
    template_project = spec.get("template_project") or template.get("project") or seed_project
    variant = spec.get("variant") or template.get("variant") or SEED_VARIANT
    pan_name = Path(components["PAN"]["name"]).name
    ond_name = Path(components["OND"]["name"]).name

    manifest_name = weather.get("weather_manifest_name") or ""
    inherited_review = False

    if mode in ("csv", "open_meteo"):
        if mode == "open_meteo":
            def request_weather(run):
                return download_weather(cli.workspace, csv_name=csv_name, year=year,
                                        location_id=weather.get("location_id"),
                                        latitude=weather.get("latitude"), longitude=weather.get("longitude"),
                                        site_name=weather.get("site_label") or "", altitude=weather.get("altitude"),
                                        country_code=country_code,
                                        timezone_name=weather.get("timezone_name") or "",
                                        temperature_method=weather.get("temperature_method") or TEMPERATURE_METHOD,
                                        dry_run=run)

            # The plan is resolved first, even for an execution run: an unusable place or an offset
            # create-site would reject stops the flow before a provider byte is requested or stored.
            plan, stopped = _phase(result, "acquisition", lambda: request_weather(True))
            if stopped is not None:
                return stopped
            result["preview"]["weather_plan"] = plan
            result["artifacts"]["acquire"] = dict(plan.get("artifacts") or {})
            if plan.get("status") != "ready":
                result["preview"]["weather"] = plan
                _inherit(result, plan, "acquisition")
                return _stop(result, "acquisition",
                             "failed" if plan.get("status") == "failed" else "blocked")
            offset = plan.get("standard_utc_offset_hours")
            if _is_number(offset) and offset > CREATE_SITE_MAX_OFFSET_HOURS:
                _issue(result, "weather.location",
                       f"The resolved place uses a standard UTC offset of {offset:+g} h while create-site "
                       f"accepts at most +{CREATE_SITE_MAX_OFFSET_HOURS} h; nothing was downloaded or written")
                return _stop(result, "acquisition")
            if dry_run:
                # No CSV/SIT/MET exists yet: inspecting or preparing one would be a fake walk over
                # files this dry run is not allowed to create.
                result["preview"]["weather"] = plan
                _inherit(result, plan, "acquisition")
                result["warnings"].append(
                    "The acquisition plan is complete but no CSV/SIT/MET exists yet, so the native "
                    "weather preflight and the annual simulation are planned, not performed")
                return _stop(result, "planned", "ready_for_weather")
            acquire, stopped = _phase(result, "acquisition", lambda: request_weather(False))
            if stopped is not None:
                return stopped
            result["preview"]["weather"] = acquire
            result["artifacts"]["acquire"] = dict(acquire.get("artifacts") or {})
            _inherit(result, acquire, "acquisition")
            # A prepared acquisition is a provider-derived source, never a native certification.
            if acquire.get("review_required"):
                inherited_review = True
            if acquire.get("status") not in ACQUIRE_SUCCESS:
                return _stop(result, "acquisition",
                             "failed" if acquire.get("status") == "failed" else "blocked")
            csv_name = (acquire.get("created_files", {}).get("csv") or {}).get("name") or csv_name
            if not country_code:
                # A location_id already carries its ISO country; take it from the acquired place.
                place_country = _acquired_place(acquire).get("country_code")
                if _is_text(place_country):
                    country_code = place_country
                    result["warnings"].append(
                        "weather.country_code was not supplied; the prepared SIT/MET use the ISO code "
                        f"{country_code} of the acquired place")
        prepare, stopped = _phase(result, "weather_prepare", lambda: prepare_weather(
            cli, csv_name=csv_name, year=year, sit_name=sit_name, met_name=met_name,
            country_code=country_code, time_basis=TIME_BASIS, dry_run=dry_run))
        if stopped is not None:
            return stopped
        result["preview"]["prepare"] = prepare
        result["artifacts"]["prepare"] = {key: str(path) for key, path in
                                          (prepare.get("artifacts") or {}).items()}
        _inherit(result, prepare, "weather_prepare")
        if prepare.get("status") not in (("ready",) if dry_run else PREPARE_SUCCESS):
            return _stop(result, "weather_prepare",
                         "failed" if prepare.get("status") == "failed" else "blocked")
        if prepare.get("status") == "needs_review":
            inherited_review = True
        if dry_run:
            result["warnings"].append(
                "No SIT/MET exists yet, so the annual simulation is planned but not preflighted; "
                "the flow does not claim a completed preflight here")
            return _stop(result, "planned", "ready_for_weather")
        manifest_name = Path((prepare.get("artifacts") or {}).get("manifest", "")).name or manifest_name
    elif not manifest_name:
        result["warnings"].append(
            "No prepared weather manifest was supplied for the existing weather inputs; the native "
            "source-weather comparison is skipped and the SIT/MET are only name-checked")

    if not dry_run:
        # Only a real run builds the verified folder set, and only after the weather phase succeeded:
        # a dry run, a native-preflight failure or a failed provider request creates nothing.
        created, error = _init_layout(cli, store)
        result["preview"]["workspace"]["created_directories"] = created
        if error is not None:
            _issue(result, "workspace_init", error)
            return _stop(result, "workspace_init", "failed")

    if fallback:
        # The seed is written only after the capacity/seed preflight and the weather phase succeeded,
        # so a failed provider request never leaves a half-built seed behind.
        if dry_run:
            result["warnings"].append(
                "The seed template exists only as an in-memory preview, so the annual simulation is "
                "neither run nor preflighted and this dry run does not claim ready")
            return _stop(result, "planned", "ready_for_weather")
        # Projects is part of the verified folder set that the workspace_init step already created.
        seed, stopped = _phase(result, "template_seed", lambda: prepare_standard_template(
            cli, store, ond_electrical=components["OND"]["electrical"], pan_name=pan_name,
            ond_name=ond_name, new_project=seed_project, dry_run=False, capacity=flat_capacity))
        if stopped is not None:
            return stopped
        result["preview"]["template_seed_write"] = seed
        result["artifacts"]["seed"] = {"project": seed_project, "variant": SEED_VARIANT,
                                       "files": seed.get("files")}
        _inherit(result, seed, "template_seed")
        if seed.get("status") != "created":
            result["warnings"].append(RETAINED_NOTE)
            return _stop(result, "template_seed",
                         "failed" if seed.get("status") == "failed" else "blocked")
        confirmed, stopped = _phase(result, "template_seed",
                                    lambda: discover(flat_capacity, seed_project, SEED_VARIANT))
        if stopped is not None:
            result["warnings"].append(RETAINED_NOTE)
            return stopped
        result["preview"]["seed_confirmed"] = confirmed
        if confirmed.get("status") != "ready" or not confirmed.get("selected"):
            _inherit(result, confirmed, "template_seed")
            result["warnings"].append(RETAINED_NOTE)
            return _stop(result, "template_seed")
        drift = _identity_drift(components, confirmed["selected"])
        if drift is not None:
            _issue(result, "discovery", drift)
            result["warnings"].append(RETAINED_NOTE)
            return _stop(result, "discovery")
        components = confirmed["selected"]
        result["selected"] = confirmed["selected"]
        result["warnings"].append(
            f"The written seed {seed_project}/{SEED_VARIANT} reads back the requested components and "
            "counts; the annual simulation runs on the working copy named by new_project")

    phase_name = "project_preparation" if prepare_only else "simulation"
    runner = prepare_template if prepare_only else simulate_template
    context = {"preparation_context": {"request": spec, "selected": result["selected"],
               "envelope": result["preview"]["envelope"], "review_required": inherited_review,
               "warnings": list(result["warnings"])}} if prepare_only else {}
    simulation, stopped = _phase(result, phase_name, lambda: runner(
        cli, store, template_project=template_project, variant=variant, new_project=new_project,
        pan_name=pan_name, ond_name=ond_name, site_name=sit_name, met_name=met_name, tilt=tilt,
        year=year, dry_run=dry_run, capacity=flat_capacity, weather_manifest_name=manifest_name,
        azimuth=azimuth, losses=losses or None,
        report_pages=spec.get("report_pages"), **context))
    if stopped is not None:
        result["warnings"].append(RETAINED_NOTE)
        return stopped
    result["preview"][phase_name] = simulation
    result["artifacts"]["project_preparation" if prepare_only else "simulate"] = dict(simulation.get("artifacts") or {})
    result["metrics"] = simulation.get("metrics")
    result["recovery"] = list(simulation.get("recovery") or [])
    _inherit(result, simulation, phase_name)
    accepted = ("prepared", "needs_review") if prepare_only else SIMULATE_SUCCESS
    if simulation.get("status") not in (("ready",) if dry_run else accepted):
        if simulation.get("status") == "failed":
            result["warnings"].append(RETAINED_NOTE)
        return _stop(result, simulation.get("stage") or phase_name,
                     "failed" if simulation.get("status") == "failed" else "blocked")

    result["execution_preflight_complete"] = True
    if dry_run:
        return _stop(result, simulation.get("stage") or "preflight", "ready")
    if prepare_only:
        result["prepared"] = simulation.get("prepared", False)
        result["prepared_project"] = {"project": new_project, "variant": variant, "year": year,
                                      "site": sit_name, "met": met_name,
                                      "manifest": simulation["artifacts"].get("manifest")}
    result["status"] = "needs_review" if inherited_review or simulation["status"] == "needs_review" else ("prepared" if prepare_only else "completed")
    result["stage"] = simulation.get("stage") or ("prepared" if prepare_only else "completed")
    return result


def simulate_design(cli, store, *, spec: dict, dry_run=True):
    """Run the unified single-project design flow for one workspace.

    ``spec`` is validated strictly before anything is created: unknown keys, non-numeric or
    boolean numbers, a missing capacity/electrical_design choice and missing weather location
    evidence all stop the flow. Returns ``status`` (blocked / ready_for_weather / ready /
    completed / needs_review / failed), ``stage``, ``dry_run``, ``execution_preflight_complete``,
    ``issues``, ``warnings``, ``selected``, ``preview``, ``metrics``, ``artifacts`` and
    ``recovery``. A dry run writes nothing; a real run never overwrites and never cleans up.
    """
    # ponytail: one workspace lock for the whole flow; the weather helpers reuse the CLI lock, so
    # the ordering store.lock -> cli._lock is fixed and no second job/concurrency layer is needed.
    with store.lock:
        return _design_flow(cli, store, spec, dry_run)


def prepare_design_project(cli, store, *, spec: dict, dry_run=True):
    """Prepare a guarded source project without an annual run or generated SFI/CSV/PDF."""
    with store.lock:
        return _design_flow(cli, store, spec, dry_run, prepare_only=True)

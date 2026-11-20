"""Parametric batch sweep workflow: generate UserBatch CSV/RVT, execute once, rank scenarios."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import product
import math
from pathlib import Path
import re
from typing import Any

from pvsyst_auto.native.cli import (
    PVsystCLI,
    PVsystError,
    batch_summary_name,
    parse_batch_results,
)
from pvsyst_auto.project.paths import project_name, safe_filename, variant_id, workspace_file

MAX_BATCH_SCENARIOS = 100
DEFAULT_BATCH_RESULT_VARS = ("GlobInc", "GlobEff", "EArray", "E_Grid", "PR", "IL_Pmax")


@dataclass(frozen=True)
class BatchParamSpec:
    """Verified PVsyst 8.1.7 UserBatch CSV column descriptor."""

    key: str
    row1: str
    row2: str
    unit: str
    kind: str  # "float", "int", "bool", "str"
    minimum: float | None = None
    maximum: float | None = None


# Exact 3-row headers verified against PVsystCLI 8.1.7 parameter descriptor table
# and live CLI execution on fixed-plane and 3D ground variants.
BATCH_PARAMETERS: dict[str, BatchParamSpec] = {
    # Fixed plane orientation
    "plane_tilt": BatchParamSpec("plane_tilt", "Plane", "tilt", "[deg]", "float", 0.0, 90.0),
    "plane_azimuth": BatchParamSpec("plane_azimuth", "Plane", "azim", "[deg]", "float", -180.0, 180.0),
    # Unlimited sheds
    "shed_count": BatchParamSpec("shed_count", "Number", "of sheds", "", "int", 1, 10000),
    "shed_tilt": BatchParamSpec("shed_tilt", "Sheds", "tilt", "[deg]", "float", 0.0, 90.0),
    "shed_azimuth": BatchParamSpec("shed_azimuth", "Sheds", "azim", "[deg]", "float", -180.0, 180.0),
    "shed_width": BatchParamSpec("shed_width", "Sheds", "width", "[m]", "float", 0.1, 100.0),
    "shed_pitch": BatchParamSpec("shed_pitch", "Sheds", "pitch", "[m]", "float", 0.2, 200.0),
    "shed_electrical_shading": BatchParamSpec("shed_electrical_shading", "Electr.", "shd loss", "Y/N", "bool"),
    # Generic / bifacial geometry
    "tilt": BatchParamSpec("tilt", "Tilt", "", "[deg]", "float", 0.0, 90.0),
    "azimuth": BatchParamSpec("azimuth", "Azimuth", "", "[deg]", "float", -180.0, 180.0),
    "pitch": BatchParamSpec("pitch", "Pitch", "", "[m]", "float", 0.2, 200.0),
    "gcr": BatchParamSpec("gcr", "GCR", "", "[%]", "float", 5.0, 95.0),
    "height_over_ground": BatchParamSpec("height_over_ground", "Height", "over ground", "[m]", "float", 0.0, 50.0),
    "albedo": BatchParamSpec("albedo", "Albedo", "", "[%]", "float", 0.0, 100.0),
    # 3D sheds
    "sheds_3d_count": BatchParamSpec("sheds_3d_count", "Sheds 3D", "Nb of sheds", "", "int", 1, 10000),
    "sheds_3d_pitch": BatchParamSpec("sheds_3d_pitch", "Sheds 3D", "pitch", "[m]", "float", 0.2, 200.0),
    "sheds_3d_width": BatchParamSpec("sheds_3d_width", "Sheds 3D", "width", "[m]", "float", 0.1, 100.0),
    "sheds_3d_length": BatchParamSpec("sheds_3d_length", "Sheds 3D", "length", "[m]", "float", 0.1, 1000.0),
    # 3D trackers (exact PVsyst spelling "Backtraking" without 'c')
    "tracker_count": BatchParamSpec("tracker_count", "Number", "of trackers", "", "int", 1, 10000),
    "tracker_pitch_ew": BatchParamSpec("tracker_pitch_ew", "Trackers", "Pitch EW", "[m]", "float", 0.2, 200.0),
    "tracker_pitch_ns": BatchParamSpec("tracker_pitch_ns", "Trackers", "Pitch NS", "[m]", "float", 0.2, 200.0),
    "tracker_phi_min": BatchParamSpec("tracker_phi_min", "Trackers", "Phi Min", "[deg]", "float", -90.0, 0.0),
    "tracker_phi_max": BatchParamSpec("tracker_phi_max", "Trackers", "Phi Max", "[deg]", "float", 0.0, 90.0),
    "backtracking": BatchParamSpec("backtracking", "Backtraking", "", "Y/N", "bool"),
    # System, grid limit, storage
    "modules_in_series": BatchParamSpec("modules_in_series", "Nb module", "in series", "", "int", 1, 200),
    "strings_in_parallel": BatchParamSpec("strings_in_parallel", "Nb strings", "in parall", "", "int", 1, 100000),
    "inverter_count": BatchParamSpec("inverter_count", "Nb inverter", "or MPPT", "", "int", 1, 10000),
    "grid_limitation_kw": BatchParamSpec("grid_limitation_kw", "Grid Limitation", "", "[kW]", "float", 0.0, 1e7),
    "battery_parallel": BatchParamSpec("battery_parallel", "Nb. batt", "in parallel", "", "int", 1, 100000),
    # Losses & simulation year
    "thermal_uc": BatchParamSpec("thermal_uc", "Thermal", "Uc fact", "[W/m2K]", "float", 1.0, 100.0),
    "thermal_uw": BatchParamSpec("thermal_uw", "Thermal", "Uw fact", "[W/m2K]", "float", 0.0, 50.0),
    "dc_wiring_loss_pct": BatchParamSpec("dc_wiring_loss_pct", "DCWiring", "Loss", "[% STC]", "float", 0.0, 50.0),
    "ac_wiring_loss_pct": BatchParamSpec("ac_wiring_loss_pct", "ACWiring", "Loss", "[% STC]", "float", 0.0, 50.0),
    "soiling_loss_pct": BatchParamSpec("soiling_loss_pct", "Soiling", "Loss", "[%]", "float", 0.0, 50.0),
    "module_quality_loss_pct": BatchParamSpec("module_quality_loss_pct", "Mod. quality", "loss", "[%]", "float", -10.0, 20.0),
    "mismatch_loss_pct": BatchParamSpec("mismatch_loss_pct", "Mismatch", "Loss", "[%]", "float", 0.0, 50.0),
    "lid_loss_pct": BatchParamSpec("lid_loss_pct", "LID", "Loss", "[%]", "float", 0.0, 20.0),
    "unavailability_loss_pct": BatchParamSpec("unavailability_loss_pct", "Unav. time", "Loss", "[%]", "float", 0.0, 100.0),
    "simulation_year": BatchParamSpec("simulation_year", "Simulation", "for Year", "", "int", 1, 50),
}

_SWEEP_STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_RVT_VAR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")


def _clean_comment(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("Scenario comment must be a string")
    cleaned = text.strip()
    if any(ch in cleaned for ch in (";", "\r", "\n", '"')):
        raise ValueError("Scenario comment must not contain semicolons, quotes, or newlines")
    if len(cleaned) > 120:
        raise ValueError("Scenario comment must be at most 120 characters")
    return cleaned


def _format_param_value(spec: BatchParamSpec, value: Any) -> tuple[Any, str]:
    """Validate one parameter value and format it for PVsyst UserBatch CSV."""
    if spec.kind == "bool":
        if type(value) is not bool:
            raise ValueError(f"{spec.key} requires a strict boolean (true/false)")
        return value, "Y" if value else "N"
    if spec.kind == "int":
        if type(value) is not int:
            raise ValueError(f"{spec.key} requires an integer")
        if spec.minimum is not None and value < spec.minimum:
            raise ValueError(f"{spec.key} must be >= {int(spec.minimum)}")
        if spec.maximum is not None and value > spec.maximum:
            raise ValueError(f"{spec.key} must be <= {int(spec.maximum)}")
        return value, str(value)
    if spec.kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{spec.key} requires a finite number")
        fval = float(value)
        if spec.minimum is not None and fval < spec.minimum:
            raise ValueError(f"{spec.key} must be >= {spec.minimum}")
        if spec.maximum is not None and fval > spec.maximum:
            raise ValueError(f"{spec.key} must be <= {spec.maximum}")
        return fval, format(fval, ".12g")
    if not isinstance(value, str) or not value.strip() or any(ch in value for ch in (";", "\r", "\n")):
        raise ValueError(f"{spec.key} requires a nonempty string without semicolons or newlines")
    return value.strip(), value.strip()


def _normalize_result_variables(raw: list[str] | tuple[str, ...] | None) -> list[str]:
    if raw is None:
        return list(DEFAULT_BATCH_RESULT_VARS)
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("result_variables must be a nonempty list of PVsyst result variable names")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("result_variables entries must be nonempty strings")
        name = item.strip()
        if name.startswith("vi") and len(name) > 2 and name[2].isupper():
            name = name[2:]
        if not _RVT_VAR_RE.fullmatch(name):
            raise ValueError(f"Invalid PVsyst batch result variable: {item!r}")
        if name not in seen:
            seen.add(name)
            out.append(name)
    if len(out) > 64:
        raise ValueError("At most 64 result_variables are supported per batch sweep")
    return out


def _expand_scenarios(
    *,
    grid: dict[str, list[Any]] | None,
    scenarios: list[dict[str, Any]] | None,
    export_hourly: bool,
    sweep_stem: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Expand Cartesian grid or explicit scenario rows into validated SIM_1..SIM_N entries."""
    if (grid is None) == (scenarios is None):
        raise ValueError("Specify exactly one of 'grid' (Cartesian sweep) or 'scenarios' (explicit list)")
    param_keys: list[str] = []
    rows: list[dict[str, Any]] = []

    if grid is not None:
        if not isinstance(grid, dict) or not grid:
            raise ValueError("grid must be a nonempty mapping of parameter_key -> list of values")
        for key, values in grid.items():
            if key not in BATCH_PARAMETERS:
                raise ValueError(
                    f"Unsupported batch parameter {key!r}. Supported keys: {', '.join(sorted(BATCH_PARAMETERS))}"
                )
            if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)) or not values:
                raise ValueError(f"grid[{key!r}] must be a nonempty list of values")
            param_keys.append(key)
        total = 1
        for key in param_keys:
            total *= len(grid[key])
            if total > MAX_BATCH_SCENARIOS:
                raise ValueError(
                    f"Cartesian grid produces more than {MAX_BATCH_SCENARIOS} scenarios; narrow the grid"
                )
        for index, combo in enumerate(product(*(grid[k] for k in param_keys)), start=1):
            params_norm: dict[str, Any] = {}
            params_text: dict[str, str] = {}
            parts: list[str] = []
            for key, raw_val in zip(param_keys, combo):
                norm, formatted = _format_param_value(BATCH_PARAMETERS[key], raw_val)
                params_norm[key] = norm
                params_text[key] = formatted
                parts.append(f"{key}={formatted}")
            comment = ", ".join(parts)
            if len(comment) > 120:
                comment = comment[:117] + "..."
            rows.append({
                "id": f"SIM_{index}",
                "hourly_file": f"{sweep_stem}_SIM_{index}.CSV" if export_hourly else "",
                "parameters": params_norm,
                "formatted": params_text,
                "comment": _clean_comment(comment),
            })
    else:
        if not isinstance(scenarios, list) or not scenarios:
            raise ValueError("scenarios must be a nonempty list of scenario dicts")
        if len(scenarios) > MAX_BATCH_SCENARIOS:
            raise ValueError(f"At most {MAX_BATCH_SCENARIOS} scenarios are supported per batch sweep")
        key_set: list[str] = []
        for idx, entry in enumerate(scenarios, start=1):
            if not isinstance(entry, dict) or not entry:
                raise ValueError(f"Scenario #{idx} must be a nonempty object")
            for k in entry:
                if k in ("comment", "hourly_file"):
                    continue
                if k not in BATCH_PARAMETERS:
                    raise ValueError(
                        f"Unsupported batch parameter {k!r} in scenario #{idx}. "
                        f"Supported keys: {', '.join(sorted(BATCH_PARAMETERS))}"
                    )
                if k not in key_set:
                    key_set.append(k)
        if not key_set:
            raise ValueError("Each scenario must declare at least one supported batch parameter")
        param_keys = key_set
        seen_hourly: set[str] = set()
        for idx, entry in enumerate(scenarios, start=1):
            missing = [k for k in param_keys if k not in entry]
            if missing:
                raise ValueError(f"Scenario #{idx} is missing parameter(s): {', '.join(missing)}")
            params_norm = {}
            params_text = {}
            for key in param_keys:
                norm, formatted = _format_param_value(BATCH_PARAMETERS[key], entry[key])
                params_norm[key] = norm
                params_text[key] = formatted
            hourly_name = entry.get("hourly_file", "")
            if not hourly_name and export_hourly:
                hourly_name = f"{sweep_stem}_SIM_{idx}.CSV"
            if hourly_name:
                safe_filename(hourly_name, ".csv")
                if hourly_name.casefold() in seen_hourly:
                    raise ValueError(f"Duplicate scenario hourly_file: {hourly_name}")
                seen_hourly.add(hourly_name.casefold())
            default_comment = ", ".join(f"{k}={params_text[k]}" for k in param_keys)
            if len(default_comment) > 120:
                default_comment = default_comment[:117] + "..."
            comment = _clean_comment(entry.get("comment") or default_comment)
            rows.append({
                "id": f"SIM_{idx}",
                "hourly_file": hourly_name,
                "parameters": params_norm,
                "formatted": params_text,
                "comment": comment,
            })

    return param_keys, rows


def render_batch_params_csv(
    *,
    project: str,
    variant: str,
    param_keys: list[str],
    scenarios: list[dict[str, Any]],
    created_at: datetime | None = None,
) -> bytes:
    """Render exact PVsyst 8.1.7 UserBatch BatchParams CSV bytes (UTF-8 BOM + CRLF).

    Always returns bytes so callers write with ``write_bytes`` and avoid Windows
    text-mode ``\\r\\r\\n`` double-CR corruption that causes PVsyst's CSV header
    validator to silently abort batch execution.
    """
    cols: list[tuple[str, str, str]] = [
        ("Ident", "", ""),
        ("Create hourly", "file", "File name"),
    ]
    for key in param_keys:
        spec = BATCH_PARAMETERS[key]
        cols.append((spec.row1, spec.row2, spec.unit))
    cols.append(("Simul", "Comment", ""))

    width = len(cols)
    stamp = (created_at or datetime.now()).strftime("%d/%m/%y %H:%M")

    def pad(cells: list[str]) -> str:
        return ";".join(cells + [""] * max(0, width - len(cells)))

    lines = [
        pad(["PVsyst simulations Batch mode"]),
        pad(["Simulation parameters definition"]),
        pad([f"File created on {stamp}"]),
        pad([]),
        pad(["Project", "", "", project]),
        pad(["Variants based on", "", variant, "Parametric Batch Sweep"]),
        pad([]),
        pad(["Please define the parameters to be varied for each run"]),
        pad(["Do not modify the column titles!"]),
        pad(['"Only the lines beginning by ""SIM_"" will be executed"']),
        pad([]),
        ";".join(c[0] for c in cols),
        ";".join(c[1] for c in cols),
        ";".join(c[2] for c in cols),
        pad([]),
    ]
    for row in scenarios:
        cells = [row["id"], row["hourly_file"]]
        cells.extend(row["formatted"][k] for k in param_keys)
        cells.append(row["comment"])
        lines.append(";".join(cells))
    lines.extend([
        pad([]),
        pad(["Don't forget to close this file in EXCEL before returning to PVsyst for executing the simulation!"]),
    ])
    return ("\r\n".join(lines) + "\r\n").encode("utf-8-sig")


def render_batch_rvt(result_variables: list[str]) -> bytes:
    """Render PVsyst 8.1.7 batch ResultVars .RVT bytes (no trailing semicolon)."""
    return ("  ResultVars=" + ";".join(result_variables) + "\r\n").encode("utf-8")


def _scenario_failed(entry: dict[str, Any], sort_by: str) -> bool:
    err = (entry.get("error") or "").strip()
    if err.lower().startswith("error"):
        return True
    val = (entry.get("values") or {}).get(sort_by)
    return val is None or not math.isfinite(val)


def rank_batch_scenarios(
    parsed: dict[str, Any],
    input_scenarios: list[dict[str, Any]],
    *,
    sort_by: str,
    sort_descending: bool = True,
) -> dict[str, Any]:
    """Combine parsed UserBatch/*Results.CSV rows with input parameters and rank them."""
    by_id = {row["id"]: row for row in input_scenarios}
    completed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for row in parsed.get("scenarios", []):
        sid = row["id"]
        inp = by_id.get(sid, {})
        record = {
            "id": sid,
            "parameters": dict(inp.get("parameters", {})),
            "comment": inp.get("comment", ""),
            "hourly_file": row.get("hourly_file", ""),
            "error": row.get("error", ""),
            "values": dict(row.get("values", {})),
        }
        if _scenario_failed(record, sort_by):
            failed.append(record)
        else:
            completed.append(record)

    completed.sort(
        key=lambda item: float(item["values"][sort_by]),
        reverse=sort_descending,
    )

    baseline = next((item for item in completed if item["id"] == "SIM_1"), completed[0] if completed else None)
    baseline_val = float(baseline["values"][sort_by]) if baseline is not None else None

    for rank, item in enumerate(completed, start=1):
        item["rank"] = rank
        val = float(item["values"][sort_by])
        if baseline_val is not None:
            delta = val - baseline_val
            rel = (delta / baseline_val * 100.0) if baseline_val != 0 else None
            item["delta_vs_baseline"] = {
                "baseline_id": baseline["id"],
                "metric": sort_by,
                "delta": round(delta, 4),
                "relative_pct": round(rel, 3) if rel is not None else None,
            }

    return {
        "best_scenario": completed[0] if completed else None,
        "ranked_scenarios": completed,
        "failed_scenarios": failed,
    }


def run_batch_sweep(
    cli: PVsystCLI,
    *,
    project: str,
    variant: str,
    sweep_name: str,
    grid: dict[str, list[Any]] | None = None,
    scenarios: list[dict[str, Any]] | None = None,
    result_variables: list[str] | tuple[str, ...] | None = None,
    sort_by: str = "E_Grid",
    sort_descending: bool = True,
    export_hourly: bool = False,
    dry_run: bool = True,
    log_level: int | None = 3,
    allow_window_creation: bool | None = None,
) -> dict[str, Any]:
    """Preview or execute a parametric PVsyst batch sweep in ONE PVsystCLI call."""
    if type(dry_run) is not bool or type(sort_descending) is not bool or type(export_hourly) is not bool:
        raise ValueError("dry_run, sort_descending, and export_hourly must be strict booleans")
    if not isinstance(sweep_name, str) or not _SWEEP_STEM_RE.fullmatch(sweep_name):
        raise ValueError("sweep_name must be 1..64 ASCII letters/digits/_/- starting with an alphanumeric character")

    result: dict[str, Any] = {
        "status": "blocked",
        "stage": "preflight",
        "dry_run": dry_run,
        "issues": [],
        "warnings": [],
        "artifacts": {},
        "native_consumption_verified": False,
    }

    with cli._lock:
        try:
            proj = project_name(project)
            var = variant_id(variant)
            if proj not in cli.list_projects() or var not in cli.list_variants(proj):
                raise ValueError(f"Project {proj} and variant {var} must exist in the workspace")

            res_vars = _normalize_result_variables(result_variables)
            sort_metric = sort_by.strip() if isinstance(sort_by, str) else ""
            if sort_metric.startswith("vi") and len(sort_metric) > 2 and sort_metric[2].isupper():
                sort_metric = sort_metric[2:]
            if sort_metric not in res_vars:
                raise ValueError(f"sort_by {sort_by!r} must be included in result_variables ({', '.join(res_vars)})")

            param_keys, expanded = _expand_scenarios(
                grid=grid,
                scenarios=scenarios,
                export_hourly=export_hourly,
                sweep_stem=sweep_name,
            )

            params_filename = f"{sweep_name}_BatchParams.CSV"
            rvt_filename = f"{sweep_name}_BatchVariables.RVT"
            summary_filename = batch_summary_name(params_filename)

            params_path = workspace_file(cli.workspace, "UserBatch", params_filename, ".csv")
            rvt_path = workspace_file(cli.workspace, "UserBatch", rvt_filename, ".rvt")
            summary_path = workspace_file(cli.workspace, "UserBatch", summary_filename, ".csv")

            for path in (params_path, rvt_path, summary_path):
                if path.exists():
                    raise FileExistsError(f"Batch artifact already exists: {path.name}; choose a fresh sweep_name")
            for row in expanded:
                if row["hourly_file"]:
                    hourly_path = workspace_file(cli.workspace, "UserHourly", row["hourly_file"], ".csv")
                    if hourly_path.exists():
                        raise FileExistsError(f"Batch hourly output already exists: {hourly_path.name}")

            csv_bytes = render_batch_params_csv(
                project=proj,
                variant=var,
                param_keys=param_keys,
                scenarios=expanded,
            )
            rvt_bytes = render_batch_rvt(res_vars)

            result.update(
                status="ready",
                project=proj,
                variant=var,
                sweep_name=sweep_name,
                parameter_keys=param_keys,
                result_variables=res_vars,
                sort_by=sort_metric,
                sort_descending=sort_descending,
                scenario_count=len(expanded),
                planned_scenarios=[
                    {
                        "id": row["id"],
                        "parameters": row["parameters"],
                        "comment": row["comment"],
                        "hourly_file": row["hourly_file"],
                    }
                    for row in expanded
                ],
                artifacts={
                    "batch_params": str(params_path),
                    "batch_rvt": str(rvt_path),
                    "expected_batch_summary": str(summary_path),
                },
            )
            if dry_run:
                return result

            # Check license before writing workspace files.
            license_state = cli.license_info(allow_window_creation=allow_window_creation)
            if license_state.get("status") == "TRIAL" and license_state.get("remaining_executions") == 0:
                raise ValueError("PVsystCLI reports no remaining trial executions")

            result["stage"] = "write_inputs"
            params_path.parent.mkdir(parents=True, exist_ok=True)
            with params_path.open("xb") as fh:
                fh.write(csv_bytes)
            with rvt_path.open("xb") as fh:
                fh.write(rvt_bytes)

            result["stage"] = "simulation"
            sim = cli.run_simulation(
                proj,
                var,
                batch_params=params_path,
                batch_rvt=rvt_path,
                log_level=log_level,
                allow_window_creation=allow_window_creation,
            )

            result["stage"] = "parse_results"
            actual_summary = Path(sim["batch_summary"])
            parsed = parse_batch_results(actual_summary, columns=tuple(res_vars))
            ranked = rank_batch_scenarios(
                parsed,
                expanded,
                sort_by=sort_metric,
                sort_descending=sort_descending,
            )

            result["artifacts"]["batch_summary"] = str(actual_summary)
            if sim.get("batch_hourly"):
                result["artifacts"]["batch_hourly"] = list(sim["batch_hourly"])

            warnings = list(sim.get("warnings") or [])
            if ranked["failed_scenarios"]:
                warnings.append(
                    f"{len(ranked['failed_scenarios'])} of {len(expanded)} batch scenarios reported errors or missing {sort_metric}."
                )

            status = "completed" if ranked["ranked_scenarios"] and not ranked["failed_scenarios"] else (
                "needs_review" if ranked["ranked_scenarios"] else "failed"
            )
            result.update(
                status=status,
                stage="completed",
                seconds=sim.get("seconds"),
                batch_runs=sim.get("batch_runs"),
                completed_count=len(ranked["ranked_scenarios"]),
                failed_count=len(ranked["failed_scenarios"]),
                best_scenario=ranked["best_scenario"],
                ranked_scenarios=ranked["ranked_scenarios"],
                failed_scenarios=ranked["failed_scenarios"],
                warnings=warnings,
                native_consumption_verified=bool(ranked["ranked_scenarios"]),
            )
            return result
        except (ValueError, OSError, PVsystError) as exc:
            result["issues"].append(str(exc))
            result["status"] = "failed" if result["stage"] != "preflight" else "blocked"
            return result

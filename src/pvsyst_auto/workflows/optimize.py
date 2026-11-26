"""Bounded Diagnose -> Patch -> Re-simulate optimization loop for PVsyst variants.

Never mutates the baseline variant in place. Default dry_run=True produces deterministic,
physics-grounded remediation patches and unified diff previews. Explicit dry_run=False with
confirm=True clones a new sequential variant per iteration (max_iterations in 1..5), applies
safe parameter updates, runs PVsystCLI, re-diagnoses, and stops immediately once high/critical
anomalies clear or no further deterministic patch applies.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from pvsyst_auto.components.single_diode import (
    synthesize_btr_from_datasheet,
    synthesize_ond_from_datasheet,
    synthesize_pan_from_datasheet,
)
from pvsyst_auto.energy.storage import plan_tou_storage_schedule
from pvsyst_auto.engineering.diagnostics import (
    SEVERITY_ORDER,
    compact_diagnostics_summary,
    diagnose_simulation_csv,
)
from pvsyst_auto.engineering.economics import _resolve_result_csv, evaluate_economics
from pvsyst_auto.engineering.sizing import suggest_sizing
from pvsyst_auto.native.cli import VAR_GROUPS, build_sfi, summarize_results
from pvsyst_auto.native.headless import _next_variant_id
from pvsyst_auto.project.paths import project_name, safe_filename, variant_id, workspace_file
from pvsyst_auto.workflows.bess_optimize import optimize_bess_sizing
from pvsyst_auto.workflows.design import simulate_design
from pvsyst_auto.workflows.report import generate_report

MAX_ITERATIONS_CAP = 5


def _parse_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_float(value: Any) -> float | None:
    try:
        num = float(str(value).strip())
        return num if math.isfinite(num) else None
    except (TypeError, ValueError):
        return None


def _sizing_max_series(sizing_hint: dict | None) -> int | None:
    if not isinstance(sizing_hint, dict) or sizing_hint.get("status") != "ready":
        return None
    rec = sizing_hint.get("recommended") or {}
    feasible = rec.get("feasible_series_range")
    if isinstance(feasible, (list, tuple)) and len(feasible) == 2 and type(feasible[1]) is int:
        return feasible[1]
    legacy = (sizing_hint.get("string_bounds") or {}).get("max_modules_in_series")
    if type(legacy) is int:
        return legacy
    val = rec.get("series_modules")
    return val if type(val) is int else None


def _summarize_egrid_kwh(csv_path: Path) -> float | None:
    try:
        entry = summarize_results(csv_path, ("E_Grid",))["summary"].get("E_Grid") or {}
        val = entry.get("energy_kwh")
        if val is None:
            val = entry.get("observed_energy_kwh")
        if val is None:
            val = entry.get("sum")
        return round(float(val), 2) if val is not None else None
    except (ValueError, KeyError, TypeError):
        return None


def _record_nmod_serie_action(
    sub0: dict,
    sub_updates: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    code: str,
    cur: int,
    new_val: int,
    reason: str,
) -> bool:
    sub_updates["NModSerie"] = new_val
    actions.append({
        "finding_code": code,
        "target": f"subarray[{sub0['id']}].NModSerie",
        "before": cur,
        "after": new_val,
        "reason": reason,
    })
    return True


def propose_remediations(
    store,
    project: str,
    variant: str,
    diag: dict,
    *,
    allow_resize: bool = True,
    min_ambient_c: float = -10.0,
    max_operating_c: float = 65.0,
) -> dict[str, Any]:
    """Map diagnostic findings + .VC0 parameters into deterministic safe parameter patches."""
    vc_info = store.inspect_parameters(project, variant)
    subarrays = vc_info.get("subarrays") or []
    orientations = vc_info.get("orientations") or []
    sub0 = subarrays[0] if subarrays else None
    orient0 = orientations[0] if orientations else None

    sub_updates: dict[str, Any] = {}
    orient_updates: dict[str, Any] = {}
    actions: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []

    findings = sorted(
        diag.get("findings") or [],
        key=lambda f: -SEVERITY_ORDER.get(f.get("severity", "info"), 0),
    )

    # Attempt optional suggest_sizing lookup if PAN/OND exist for accurate voltage bounds
    sizing_hint = None
    if sub0 is not None:
        inv_state = sub0.get("inverter_state") or {}
        pan_name = inv_state.get("PVModule")
        ond_name = inv_state.get("GInverter")
        vals = sub0.get("values") or {}
        n_ser = _parse_int(vals.get("NModSerie"))
        n_str = _parse_int(vals.get("NStringCh"))
        if pan_name and ond_name and n_ser and n_str:
            try:
                # Estimate target DC kW from current module count * PNom
                pan_entry = store.components.list("PAN", query=Path(pan_name).stem, limit=5)["items"]
                pnom = next((i["specs"].get("pnom_w") for i in pan_entry
                             if i["name"].casefold() == Path(pan_name).name.casefold()
                             and (i.get("specs") or {}).get("pnom_w")), None)
                if pnom:
                    est_dc_kw = (n_ser * n_str * pnom) / 1000.0
                    sizing_hint = suggest_sizing(
                        store, pan_name=pan_name, ond_name=ond_name,
                        target_dc_kw=est_dc_kw,
                        min_ambient_c=min_ambient_c, max_operating_c=max_operating_c,
                    )
            except (ValueError, OSError, KeyError):
                sizing_hint = None

    series_changed = False
    for finding in findings:
        code = finding.get("code")
        if code == "INVERTER_OVERVOLTAGE" and sub0 is not None and not series_changed:
            cur = _parse_int((sub0.get("values") or {}).get("NModSerie"))
            if cur is not None and cur > 1:
                s_max = _sizing_max_series(sizing_hint)
                suggested_max = s_max if (s_max is not None and 1 <= s_max < cur) else None
                new_val = suggested_max if suggested_max is not None else max(1, cur - max(1, round(cur * 0.1)))
                if new_val < cur:
                    series_changed = _record_nmod_serie_action(
                        sub0, sub_updates, actions, code=code, cur=cur, new_val=new_val,
                        reason=(
                            f"Reduce string length from {cur} to {new_val} to clear IL_Vmax overvoltage"
                            + (" (from suggest_sizing cold Voc bound)" if suggested_max else " (-10% step)")
                        ),
                    )

        elif code == "INVERTER_UNDERVOLTAGE" and sub0 is not None and not series_changed:
            cur = _parse_int((sub0.get("values") or {}).get("NModSerie"))
            if cur is not None:
                step = max(1, round(cur * 0.1))
                new_val = cur + step
                s_max = _sizing_max_series(sizing_hint)
                if s_max is not None:
                    new_val = min(new_val, s_max)
                if new_val > cur:
                    series_changed = _record_nmod_serie_action(
                        sub0, sub_updates, actions, code=code, cur=cur, new_val=new_val,
                        reason=f"Increase string length from {cur} to {new_val} to keep hot Vmp above VMppMin",
                    )

        elif code == "INVERTER_CLIPPING_HIGH" and sub0 is not None and allow_resize:
            vals = sub0.get("values") or {}
            cur_str = _parse_int(vals.get("NStringCh"))
            cur_mppt = _parse_int(vals.get("NInvMPPT")) or 1
            if cur_str is not None and cur_str > cur_mppt:
                # Reduce strings by one MPPT-divisible step (at least 1 string, preserving divisibility)
                step = cur_mppt if cur_str >= 2 * cur_mppt else 1
                new_str = max(1, cur_str - step)
                if new_str < cur_str and "NStringCh" not in sub_updates:
                    sub_updates["NStringCh"] = new_str
                    actions.append({
                        "finding_code": code,
                        "target": f"subarray[{sub0['id']}].NStringCh",
                        "before": cur_str,
                        "after": new_str,
                        "reason": f"Reduce parallel strings from {cur_str} to {new_str} to lower DC/AC ratio and IL_Pmax clipping",
                    })

        elif code == "SHADING_LOSS_HIGH" and orient0 is not None:
            ovals = orient0.get("values") or {}
            ft = ovals.get("FieldType")
            if ft in ("Sheds", "UnlimSheds", "UnlimTracking") and "Pitch" in ovals:
                cur_pitch = _parse_float(ovals.get("Pitch"))
                if cur_pitch is not None and cur_pitch > 0 and "Pitch" not in orient_updates:
                    new_pitch = round(cur_pitch * 1.15, 2)
                    orient_updates["Pitch"] = new_pitch
                    actions.append({
                        "finding_code": code,
                        "target": f"orientation[{orient0['id']}].Pitch",
                        "before": cur_pitch,
                        "after": new_pitch,
                        "reason": f"Increase row pitch +15% ({cur_pitch} m -> {new_pitch} m) to reduce inter-row shading",
                    })
            elif ft == "FixedPlane" and "FieldTilt" in ovals:
                cur_tilt = _parse_float(ovals.get("FieldTilt"))
                if cur_tilt is not None and cur_tilt > 10.0 and "FieldTilt" not in orient_updates:
                    new_tilt = round(max(10.0, cur_tilt - 5.0), 1)
                    orient_updates["FieldTilt"] = new_tilt
                    actions.append({
                        "finding_code": code,
                        "target": f"orientation[{orient0['id']}].FieldTilt",
                        "before": cur_tilt,
                        "after": new_tilt,
                        "reason": f"Lower plane tilt ({cur_tilt} deg -> {new_tilt} deg) to reduce near-shading loss",
                    })

        elif code == "LOAD_NOT_ENABLED":
            advisories.append({
                "finding_code": code,
                "action": "manual_tool_call",
                "message": "Rebind hourly load via pvsyst_create_native_load_variant so root pvVCalcul.Flags bit 19 ($00080000) is enabled.",
            })
        elif code in ("SOILING_LOSS_HIGH", "GRID_CURTAILMENT_HIGH", "UNMET_LOAD_HIGH",
                      "SOC_PINNED_LOW", "SOC_PINNED_HIGH", "NIGHTTIME_EXPORT",
                      "IRRADIANCE_OUTPUT_PHASE_MISMATCH"):
            advisories.append({
                "finding_code": code,
                "action": "manual_review",
                "message": "; ".join(finding.get("recommended_actions") or [finding.get("message", "")]),
            })

    diff_preview = ""
    if sub_updates or orient_updates:
        preview = store.update_parameters(
            project,
            variant,
            orientation_id=orient0["id"] if (orient_updates and orient0) else None,
            subarray_id=sub0["id"] if (sub_updates and sub0) else None,
            orientation_updates=orient_updates or None,
            subarray_updates=sub_updates or None,
            dry_run=True,
        )
        diff_preview = preview.get("diff", "")

    return {
        "has_executable_patch": bool(sub_updates or orient_updates),
        "orientation_id": orient0["id"] if (orient_updates and orient0) else None,
        "subarray_id": sub0["id"] if (sub_updates and sub0) else None,
        "orientation_updates": orient_updates,
        "subarray_updates": sub_updates,
        "actions": actions,
        "advisories": advisories,
        "diff_preview": diff_preview,
    }


def optimize_variant(
    cli,
    store,
    *,
    project: str,
    variant: str,
    csv_name: str,
    max_iterations: int = 2,
    allow_resize: bool = True,
    min_ambient_c: float = -10.0,
    max_operating_c: float = 65.0,
    thresholds: dict[str, float] | None = None,
    sfi_name: str | None = None,
    time_step: str = "hour",
    allow_window_creation: bool | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Propose or execute bounded Diagnose -> Clone -> Patch -> Re-simulate iterations."""
    if type(dry_run) is not bool or type(confirm) is not bool or type(allow_resize) is not bool:
        raise ValueError("dry_run, confirm, and allow_resize must be strict booleans")
    if type(max_iterations) is not int or not 1 <= max_iterations <= MAX_ITERATIONS_CAP:
        raise ValueError(f"max_iterations must be an integer in 1..{MAX_ITERATIONS_CAP}")
    if time_step not in ("hour", "subhour"):
        raise ValueError("time_step must be 'hour' or 'subhour'")

    proj = project_name(project)
    base_var = variant_id(variant)
    safe_csv = safe_filename(csv_name, ".csv")
    csv_path = workspace_file(cli.workspace, "Results", safe_csv, ".csv")
    if not csv_path.is_file():
        raise FileNotFoundError(f"Baseline result CSV not found in Results/: {safe_csv}")

    baseline_diag = diagnose_simulation_csv(
        csv_path, store=store, project=proj, variant=base_var, thresholds=thresholds
    )
    baseline_egrid = _summarize_egrid_kwh(csv_path)
    baseline_summary = {
        "variant": base_var,
        "csv": safe_csv,
        "e_grid_sum": baseline_egrid,
        "weighted_pr": baseline_diag.get("weighted_pr"),
        "diagnostics": compact_diagnostics_summary(baseline_diag),
    }

    if baseline_diag.get("status") == "blocked":
        return {
            "status": "blocked",
            "issues": list(baseline_diag.get("issues") or ["Baseline diagnostics blocked"]),
            "dry_run": dry_run,
            "project": proj,
            "baseline_variant": base_var,
            "baseline_csv": safe_csv,
            "baseline": baseline_summary,
            "proposed_remediation": None,
            "iterations": [],
            "recommended_variant": base_var,
            "converged": False,
        }

    first_proposal = propose_remediations(
        store, proj, base_var, baseline_diag,
        allow_resize=allow_resize, min_ambient_c=min_ambient_c, max_operating_c=max_operating_c,
    )

    result: dict[str, Any] = {
        "status": "preview",
        "dry_run": dry_run,
        "project": proj,
        "baseline_variant": base_var,
        "baseline_csv": safe_csv,
        "baseline": baseline_summary,
        "proposed_remediation": first_proposal,
        "iterations": [],
        "recommended_variant": base_var,
        "converged": not baseline_diag["needs_review"] and not first_proposal["has_executable_patch"],
    }

    if not baseline_diag["needs_review"] and not first_proposal["has_executable_patch"]:
        result["status"] = "no_action_needed"
        return result

    if dry_run:
        result["status"] = "preview" if first_proposal["has_executable_patch"] else "advisory_only"
        return result

    if not confirm:
        result["status"] = "blocked"
        result["issues"] = ["Execution requires confirm=true alongside dry_run=false; baseline was not modified."]
        return result

    if not first_proposal["has_executable_patch"]:
        result["status"] = "advisory_only"
        return result

    # Prepare SFI once for all re-simulation iterations
    if sfi_name:
        sfi_path = workspace_file(cli.workspace, "Models", safe_filename(sfi_name, ".sfi"), ".sfi")
        if not sfi_path.is_file():
            raise FileNotFoundError(f"Requested SFI not found in Models/: {sfi_name}")
    else:
        step_suffix = "_subhour" if time_step == "subhour" else ""
        sfi_stem = f"{Path(proj).stem}_{base_var}_opt{step_suffix}"
        sfi_path = workspace_file(cli.workspace, "Models", f"{sfi_stem}.SFI", ".sfi")
        if not sfi_path.is_file():
            build_sfi(sfi_path, list(VAR_GROUPS["all_common"]), time_step=time_step)

    current_var = base_var
    current_diag = baseline_diag
    current_proposal = first_proposal
    best_var = base_var
    best_score = (
        0 if not baseline_diag["needs_review"] else 1,
        baseline_diag["finding_count"],
        -(baseline_diag.get("weighted_pr") or 0.0),
    )

    for iteration in range(1, max_iterations + 1):
        if not current_proposal["has_executable_patch"]:
            break
        next_var = _next_variant_id(store, proj)
        store.clone(proj, current_var, next_var)
        store.update_parameters(
            proj,
            next_var,
            orientation_id=current_proposal["orientation_id"],
            subarray_id=current_proposal["subarray_id"],
            orientation_updates=current_proposal["orientation_updates"] or None,
            subarray_updates=current_proposal["subarray_updates"] or None,
            dry_run=False,
        )
        out_name = f"{Path(proj).stem}_{next_var}_opt{iteration:02d}.csv"
        out_csv = workspace_file(cli.workspace, "Results", out_name, ".csv")
        cli.run_simulation(
            proj,
            next_var,
            sfi=sfi_path,
            out_csv=out_csv,
            time_step=time_step,
            allow_window_creation=allow_window_creation,
        )
        next_diag = diagnose_simulation_csv(
            out_csv, store=store, project=proj, variant=next_var, thresholds=thresholds
        )
        next_egrid = _summarize_egrid_kwh(out_csv)

        iter_record = {
            "iteration": iteration,
            "source_variant": current_var,
            "new_variant": next_var,
            "csv": out_name,
            "applied_actions": current_proposal["actions"],
            "e_grid_sum": next_egrid,
            "weighted_pr": next_diag.get("weighted_pr"),
            "diagnostics": compact_diagnostics_summary(next_diag),
        }
        result["iterations"].append(iter_record)

        if next_diag.get("status") == "blocked":
            result["status"] = "blocked"
            result["issues"] = list(next_diag.get("issues") or ["Iteration diagnostics blocked"])
            result["converged"] = False
            break

        score = (
            0 if not next_diag["needs_review"] else 1,
            next_diag["finding_count"],
            -(next_diag.get("weighted_pr") or 0.0),
        )
        if score < best_score:
            best_score = score
            best_var = next_var

        current_var = next_var
        current_diag = next_diag
        if not current_diag["needs_review"] and current_diag["finding_count"] == 0:
            result["converged"] = True
            result["status"] = "converged"
            break
        current_proposal = propose_remediations(
            store, proj, current_var, current_diag,
            allow_resize=allow_resize, min_ambient_c=min_ambient_c, max_operating_c=max_operating_c,
        )
        if not current_diag["needs_review"] and not current_proposal["has_executable_patch"]:
            result["converged"] = True
            result["status"] = "converged"
            break
    else:
        result["status"] = "improved" if best_var != base_var else "stopped_max_iterations"

    if result["status"] == "preview":
        result["status"] = "improved" if best_var != base_var else "stopped_max_iterations"
    result["recommended_variant"] = best_var
    return result


def _synthesize_inline_component(comp_store, item: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    kind = str(item.get("component_type", "")).upper()
    if kind not in ("PAN", "OND", "BTR"):
        raise ValueError("synthesize_components[].component_type must be 'PAN', 'OND', or 'BTR'")
    new_name = str(item.get("new_name") or "").strip()
    manufacturer = str(item.get("manufacturer") or "").strip()
    model = str(item.get("model") or "").strip()
    if not new_name or not manufacturer or not model:
        raise ValueError("synthesize_components[] requires new_name, manufacturer, and model")
    params = dict(item.get("params") or {})
    synth_fn = {"PAN": synthesize_pan_from_datasheet, "OND": synthesize_ond_from_datasheet, "BTR": synthesize_btr_from_datasheet}[kind]
    text, meta = synth_fn(manufacturer=manufacturer, model=model, **params)
    target = comp_store._path(kind, new_name)
    exists = target.is_file()
    if not dry_run and not exists:
        comp_store.create(kind, new_name, text)
    return {
        "component_type": kind, "file_name": new_name, "path": str(target),
        "already_exists": exists, "created": (not dry_run and not exists), "metadata": meta,
    }


def run_feasibility_study(
    cli, store, *, spec: dict[str, Any], dry_run: bool = True, confirm: bool = False,
) -> dict[str, Any]:
    """Preview or execute a full end-to-end PV / PV+BESS feasibility study."""
    if type(dry_run) is not bool or type(confirm) is not bool:
        raise ValueError("dry_run and confirm must be strict booleans")
    if not dry_run and not confirm:
        raise ValueError("Executing pvsyst_run_feasibility_study (dry_run=False) requires confirm=True")

    study_name = str(spec.get("study_name") or "").strip()
    if not study_name or len(study_name) > 100:
        raise ValueError("spec.study_name must be 1..100 characters")
    existing_csv, design_spec = spec.get("existing_csv"), spec.get("design")
    if not existing_csv and not design_spec:
        raise ValueError("Provide either spec.design (to simulate) or spec.existing_csv (to reuse an existing baseline CSV)")
    if existing_csv and design_spec:
        raise ValueError("Provide spec.design or spec.existing_csv, not both")

    ws: Path = cli.workspace
    comp_store = store.components
    stages: list[dict[str, Any]] = []
    est_cli_calls = 0

    synth_items = spec.get("synthesize_components") or []
    synth_results = [_synthesize_inline_component(comp_store, item, dry_run=dry_run) for item in synth_items]
    if synth_items:
        stages.append({"stage": "components", "status": "ready" if dry_run else "completed", "count": len(synth_results)})

    sim_result: dict[str, Any] | None = None
    baseline_csv_path: Path | None = None
    baseline_csv_name: str | None = None
    proj_name: str | None = spec.get("existing_project")
    var_name: str | None = spec.get("existing_variant")
    installed_dc_kw: float | None = float(spec["installed_dc_kw"]) if spec.get("installed_dc_kw") is not None else None

    if existing_csv:
        baseline_csv_path = _resolve_result_csv(ws, existing_csv)
        baseline_csv_name = baseline_csv_path.name
        if installed_dc_kw is None:
            raise ValueError("spec.installed_dc_kw is required when reusing spec.existing_csv")
        stages.append({"stage": "simulation", "status": "reused", "csv_name": baseline_csv_name})
    else:
        assert design_spec is not None
        est_cli_calls += 1
        if (design_spec.get("weather") or {}).get("mode") in ("csv", "open_meteo", "pvgis_tmy", "nasa_power"):
            est_cli_calls += 2
        if installed_dc_kw is None and design_spec.get("target_dc_kw") is not None:
            installed_dc_kw = float(design_spec["target_dc_kw"])
        sim_result = simulate_design(cli, store, spec=design_spec, dry_run=dry_run)
        sim_status = sim_result.get("status", "unknown")
        if sim_status in ("blocked", "failed", "ready_for_weather"):
            stages.append({"stage": "simulation", "status": sim_status, "issues": sim_result.get("issues")})
            return {
                "status": sim_status, "dry_run": dry_run, "study_name": study_name, "stages": stages,
                "synthesized_components": synth_results, "simulation": sim_result,
                "issues": sim_result.get("issues") or [],
            }
        raw_artifacts = sim_result.get("artifacts") or {}
        sim_artifacts = raw_artifacts.get("simulate") if isinstance(raw_artifacts.get("simulate"), dict) else raw_artifacts
        sim_preview = (sim_result.get("preview") or {}).get("simulation") or {}
        selected_tpl = (sim_result.get("selected") or {}).get("template") or {}
        proj_name = sim_artifacts.get("project_name") or sim_artifacts.get("project") or design_spec.get("new_project")
        var_name = sim_artifacts.get("variant") or selected_tpl.get("variant") or design_spec.get("variant") or "VC0"
        if not dry_run:
            csv_ref = sim_artifacts.get("csv") or (sim_preview.get("artifacts") or {}).get("csv")
            if csv_ref:
                baseline_csv_path = _resolve_result_csv(ws, Path(csv_ref).name)
                baseline_csv_name = baseline_csv_path.name
            actual_dc = (
                (sim_result.get("metrics") or {}).get("nominal_dc_power_kw")
                or sim_preview.get("nominal_dc_power_kw")
                or (sim_preview.get("capacity_plan") or {}).get("installed_dc_kw")
                or (sim_result.get("sizing") or {}).get("actual_dc_kw")
            )
            if actual_dc is not None:
                installed_dc_kw = float(actual_dc)
        stages.append({"stage": "simulation", "status": sim_status, "project": proj_name, "variant": var_name, "csv_name": baseline_csv_name})

    diag_result: dict[str, Any] | None = None
    diag_blocked = False
    if baseline_csv_path is not None and baseline_csv_path.is_file():
        diag_result = diagnose_simulation_csv(
            baseline_csv_path, store=store if (proj_name and var_name) else None,
            project=proj_name, variant=var_name, thresholds=spec.get("thresholds"),
        )
        diag_blocked = diag_result.get("status") == "blocked"
        diag_stage_status = "blocked" if diag_blocked else ("needs_review" if diag_result.get("needs_review") else "ok")
        stages.append({"stage": "diagnostics", "status": diag_stage_status, "finding_count": diag_result.get("finding_count", 0)})
    else:
        stages.append({"stage": "diagnostics", "status": "planned"})

    tariff_cfg, financial_cfg, sensitivity_cfg, bess_cfg = (
        spec.get("tariff"), spec.get("financial"), spec.get("sensitivity"), spec.get("bess_sizing"),
    )
    tou_result: dict[str, Any] | None = None
    if tariff_cfg and tariff_cfg.get("mode") == "tou" and tariff_cfg.get("tou_schedule"):
        if spec.get("auto_tou_schedule") or bess_cfg:
            raw_pcs_vals = (bess_cfg or {}).get("pcs_kw_values")
            pcs_vals = [float(v) for v in raw_pcs_vals] if raw_pcs_vals else [max(1.0, (installed_dc_kw or 100.0) * 0.5)]
            bp = (bess_cfg or {}).get("bess_params") or {}
            tou_result = plan_tou_storage_schedule(
                tou_schedule=tariff_cfg["tou_schedule"],
                charge_power_kw=max(pcs_vals), discharge_power_kw=max(pcs_vals),
                cycles_per_day=int(bp["cycles_per_day"]) if bp.get("cycles_per_day") is not None else 2,
                round_trip_efficiency=float(bp["round_trip_efficiency"]) if bp.get("round_trip_efficiency") is not None else 0.88,
                min_spread=float(bp["min_spread"]) if bp.get("min_spread") is not None else 0.05,
                default_import_rate=float(tariff_cfg["import_tariff"]) if tariff_cfg.get("import_tariff") is not None else 0.0,
                default_export_rate=float(tariff_cfg["feed_in_tariff"]) if tariff_cfg.get("feed_in_tariff") is not None else 0.0,
            )
            stages.append({"stage": "tou_schedule", "status": tou_result["status"], "planned_cycles_count": tou_result["planned_cycles_count"]})

    bess_result: dict[str, Any] | None = None
    if bess_cfg:
        if not tariff_cfg or not financial_cfg:
            raise ValueError("spec.tariff and spec.financial are required when spec.bess_sizing is configured")
        verify_k = int(bess_cfg["verify_top_k"]) if bess_cfg.get("verify_top_k") is not None else 0
        pcs_capex_kw = float(bess_cfg["pcs_capex_per_kw"]) if bess_cfg.get("pcs_capex_per_kw") is not None else 0.0
        est_cli_calls += verify_k
        if baseline_csv_path is not None and baseline_csv_path.is_file():
            bess_result = optimize_bess_sizing(
                workspace=ws, cli=cli, store=store, baseline_csv=baseline_csv_path,
                installed_dc_kw=installed_dc_kw or 100.0,
                bess_kwh_values=bess_cfg["bess_kwh_values"], pcs_kw_values=bess_cfg["pcs_kw_values"],
                tariff=tariff_cfg, financial=financial_cfg,
                strategies=bess_cfg.get("strategies"), load_profile=bess_cfg.get("load_profile"),
                bess_params=bess_cfg.get("bess_params"), pcs_capex_per_kw=pcs_capex_kw,
                rank_by=bess_cfg.get("rank_by") or "npv", verify_top_k=verify_k,
                verify_config=bess_cfg.get("verify_config"), dry_run=dry_run, confirm=confirm,
            )
            stages.append({"stage": "bess_optimization", "status": bess_result["status"], "evaluated_count": bess_result["evaluated_count"], "best": (bess_result.get("best") or {}).get("label")})
        else:
            stages.append({"stage": "bess_optimization", "status": "planned", "grid_bess_kwh": bess_cfg["bess_kwh_values"], "grid_pcs_kw": bess_cfg["pcs_kw_values"], "verify_top_k": verify_k})

    econ_result: dict[str, Any] | None = None
    report_scenarios: list[dict[str, Any]] = []
    if baseline_csv_name is not None:
        report_scenarios.append({"label": f"{study_name}_Baseline", "csv_name": baseline_csv_name, "project": proj_name, "variant": var_name, "installed_dc_kw": installed_dc_kw})
        for vc in ((bess_result or {}).get("stage2_verification") or {}).get("verified_comparisons") or ():
            vc_sc: dict[str, Any] = {
                "label": vc["label"], "csv_name": vc["csv_name"],
                "project": (bess_cfg.get("verify_config") or {}).get("project") if bess_cfg else proj_name,
                "variant": vc["variant"], "installed_dc_kw": installed_dc_kw, "battery_kwh": vc["actual_battery_kwh"],
            }
            if vc.get("capex_total") is not None:
                vc_sc["capex_total"] = vc["capex_total"]
            report_scenarios.append(vc_sc)

    if financial_cfg is not None or tariff_cfg is not None:
        if report_scenarios:
            econ_result = evaluate_economics(
                workspace=ws,
                scenarios=[{k: v for k, v in sc.items() if k in ("label", "csv_name", "installed_dc_kw", "battery_kwh", "capex_total") and v is not None} for sc in report_scenarios],
                financial=financial_cfg, tariff=tariff_cfg, sensitivity=sensitivity_cfg, include_cashflow_schedule=True,
            )
            stages.append({"stage": "economics", "status": "ok", "best_scenario": econ_result["best_scenario"], "has_sensitivity": sensitivity_cfg is not None and sensitivity_cfg.get("enabled", True)})
        else:
            stages.append({"stage": "economics", "status": "planned"})

    report_cfg = spec.get("report")
    report_result: dict[str, Any] | None = None
    if report_cfg:
        out_name = report_cfg.get("output_name") or f"{study_name}_feasibility.html"
        safe_out = safe_filename(out_name, ".html")
        out_path = workspace_file(ws, "Results", safe_out, ".html")
        if out_path.exists() and not report_cfg.get("overwrite", False):
            raise FileExistsError(f"Report already exists: {out_path.name}; choose a new output_name or set overwrite=true")
        if report_scenarios:
            report_result = generate_report(
                workspace=ws, store=store, title=report_cfg.get("title") or f"{study_name} Feasibility Report",
                output_name=safe_out, scenarios=report_scenarios, financial=financial_cfg, tariff=tariff_cfg,
                sensitivity=sensitivity_cfg, bess_optimization=bess_result,
                language=report_cfg.get("language", "zh"), plotlyjs=report_cfg.get("plotlyjs", "cdn"),
                thresholds=spec.get("thresholds"), overwrite=bool(report_cfg.get("overwrite", False)), dry_run=dry_run,
            )
            stages.append({"stage": "report", "status": report_result["status"], "report_path": report_result["report_path"]})
        else:
            stages.append({"stage": "report", "status": "planned", "report_path": str(out_path), "language": report_cfg.get("language", "zh")})

    exec_summary: dict[str, Any] = {"installed_dc_kw": installed_dc_kw, "estimated_cli_calls": est_cli_calls}
    sim_needs_review = bool(sim_result and sim_result.get("status") == "needs_review")
    diag_needs_review = bool(diag_result and diag_result.get("needs_review"))
    combined_needs_review = sim_needs_review or diag_needs_review
    if diag_result:
        exec_summary.update(weighted_pr=diag_result.get("weighted_pr"), needs_review=combined_needs_review, finding_count=diag_result.get("finding_count"))
    elif sim_needs_review:
        exec_summary["needs_review"] = True
    if econ_result and econ_result.get("scenarios"):
        sc0 = econ_result["scenarios"][0]
        f0 = sc0["financials"]
        exec_summary.update({
            "currency": econ_result["currency"], "year1_energy_kwh": sc0["year1_energy_kwh"],
            "lcoe": f0["lcoe"], "npv": f0["npv"], "irr_pct": f0["irr_pct"],
            "simple_payback_years": f0["simple_payback_years"], "year1_demand_savings": f0.get("year1_demand_savings", 0.0),
        })
    if bess_result and bess_result.get("best"):
        exec_summary["recommended_bess"] = {k: bess_result["best"][k] for k in ("label", "strategy", "bess_kwh", "pcs_kw", "npv", "irr_pct")}
    if report_result and not dry_run:
        exec_summary["report_path"] = report_result.get("report_path")

    final_status = "blocked" if diag_blocked else ("ready" if dry_run else ("needs_review" if combined_needs_review else "completed"))
    return {
        "status": final_status, "dry_run": dry_run, "study_name": study_name, "stages": stages,
        "executive_summary": exec_summary, "synthesized_components": synth_results,
        "simulation": sim_result, "diagnostics": diag_result, "tou_schedule": tou_result,
        "bess_optimization": bess_result, "economics": econ_result, "report": report_result,
    }

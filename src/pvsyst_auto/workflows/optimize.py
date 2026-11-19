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

from pvsyst_auto.engineering.diagnostics import (
    SEVERITY_ORDER,
    compact_diagnostics_summary,
    diagnose_simulation_csv,
)
from pvsyst_auto.engineering.suggest import suggest_sizing
from pvsyst_auto.native.cli import VAR_GROUPS, build_sfi, summarize_results
from pvsyst_auto.project.paths import project_name, safe_filename, variant_id, workspace_file

MAX_ITERATIONS_CAP = 5
_ACTIONABLE_CODES = frozenset((
    "INVERTER_OVERVOLTAGE",
    "INVERTER_UNDERVOLTAGE",
    "INVERTER_CLIPPING_HIGH",
    "SHADING_LOSS_HIGH",
))


def _next_free_variant(existing: list[str]) -> str:
    used = {v.upper() for v in existing}
    for n in range(1, 100):
        candidate = f"VC{n}" if n < 10 else f"V{n:02d}"
        if candidate not in used:
            return candidate
    raise ValueError("No free variant slot (VC1..V99) available in project")


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
                suggested_max = None
                if sizing_hint and sizing_hint.get("status") == "ready":
                    rec = sizing_hint.get("recommended") or {}
                    s_max = (sizing_hint.get("string_bounds") or {}).get("max_modules_in_series") or rec.get("series_modules")
                    if isinstance(s_max, int) and 1 <= s_max < cur:
                        suggested_max = s_max
                new_val = suggested_max if suggested_max is not None else max(1, cur - max(1, round(cur * 0.1)))
                if new_val < cur:
                    sub_updates["NModSerie"] = new_val
                    series_changed = True
                    actions.append({
                        "finding_code": code,
                        "target": f"subarray[{sub0['id']}].NModSerie",
                        "before": cur,
                        "after": new_val,
                        "reason": (
                            f"Reduce string length from {cur} to {new_val} to clear IL_Vmax overvoltage"
                            + (" (from suggest_sizing cold Voc bound)" if suggested_max else " (-10% step)")
                        ),
                    })

        elif code == "INVERTER_UNDERVOLTAGE" and sub0 is not None and not series_changed:
            cur = _parse_int((sub0.get("values") or {}).get("NModSerie"))
            if cur is not None:
                step = max(1, round(cur * 0.1))
                new_val = cur + step
                if sizing_hint and sizing_hint.get("status") == "ready":
                    s_max = (sizing_hint.get("string_bounds") or {}).get("max_modules_in_series")
                    if isinstance(s_max, int) and s_max > cur:
                        new_val = min(new_val, s_max)
                if new_val > cur:
                    sub_updates["NModSerie"] = new_val
                    series_changed = True
                    actions.append({
                        "finding_code": code,
                        "target": f"subarray[{sub0['id']}].NModSerie",
                        "before": cur,
                        "after": new_val,
                        "reason": f"Increase string length from {cur} to {new_val} to keep hot Vmp above VMppMin",
                    })

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
    baseline_egrid = None
    try:
        summ = summarize_results(csv_path, ("E_Grid",))["summary"]
        if "E_Grid" in summ and summ["E_Grid"]["sum"] is not None:
            baseline_egrid = round(float(summ["E_Grid"]["sum"]), 2)
    except (ValueError, KeyError):
        baseline_egrid = None

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
        "baseline": {
            "variant": base_var,
            "csv": safe_csv,
            "e_grid_sum": baseline_egrid,
            "weighted_pr": baseline_diag.get("weighted_pr"),
            "diagnostics": compact_diagnostics_summary(baseline_diag),
        },
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
        sfi_stem = f"{Path(proj).stem}_{base_var}_opt"
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
        existing_vars = store.inspect_project(proj)["variants"]
        next_var = _next_free_variant(existing_vars)
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
        next_egrid = None
        try:
            summ = summarize_results(out_csv, ("E_Grid",))["summary"]
            if "E_Grid" in summ and summ["E_Grid"]["sum"] is not None:
                next_egrid = round(float(summ["E_Grid"]["sum"]), 2)
        except (ValueError, KeyError):
            next_egrid = None

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

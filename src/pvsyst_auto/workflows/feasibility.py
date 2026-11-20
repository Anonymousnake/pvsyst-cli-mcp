"""One-shot end-to-end PV & PV+BESS Feasibility Study orchestrator (`pvsyst_run_feasibility_study`).

Chains optional zero-template component synthesis, PV simulation (or breakpoint reuse of an existing
baseline CSV), physical diagnostics, TOU PowerShift 48-slot planning, two-stage hybrid BESS sizing
optimization, two-part demand/TOU economics with sensitivity tornado analysis, and interactive
bilingual Plotly HTML report generation behind a single dry-run-first entry point.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pvsyst_auto.components.single_diode import (
    synthesize_btr_from_datasheet,
    synthesize_ond_from_datasheet,
    synthesize_pan_from_datasheet,
)
from pvsyst_auto.energy.tou_planner import plan_tou_storage_schedule
from pvsyst_auto.engineering.diagnostics import diagnose_simulation_csv
from pvsyst_auto.engineering.economics import _resolve_result_csv, evaluate_economics
from pvsyst_auto.project.paths import safe_filename, workspace_file
from pvsyst_auto.workflows.bess_optimize import optimize_bess_sizing
from pvsyst_auto.workflows.design import simulate_design
from pvsyst_auto.workflows.report import generate_report


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

    if kind == "PAN":
        text, meta = synthesize_pan_from_datasheet(manufacturer=manufacturer, model=model, **params)
    elif kind == "OND":
        text, meta = synthesize_ond_from_datasheet(manufacturer=manufacturer, model=model, **params)
    else:
        text, meta = synthesize_btr_from_datasheet(manufacturer=manufacturer, model=model, **params)

    target = comp_store._path(kind, new_name)
    exists = target.is_file()
    if not dry_run and not exists:
        comp_store.create(kind, new_name, text)
    return {
        "component_type": kind,
        "file_name": new_name,
        "path": str(target),
        "already_exists": exists,
        "created": (not dry_run and not exists),
        "metadata": meta,
    }


def run_feasibility_study(
    cli,
    store,
    *,
    spec: dict[str, Any],
    dry_run: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Preview or execute a full end-to-end PV / PV+BESS feasibility study."""
    if type(dry_run) is not bool or type(confirm) is not bool:
        raise ValueError("dry_run and confirm must be strict booleans")
    if not dry_run and not confirm:
        raise ValueError("Executing pvsyst_run_feasibility_study (dry_run=False) requires confirm=True")

    study_name = str(spec.get("study_name") or "").strip()
    if not study_name or len(study_name) > 100:
        raise ValueError("spec.study_name must be 1..100 characters")

    existing_csv = spec.get("existing_csv")
    design_spec = spec.get("design")
    if not existing_csv and not design_spec:
        raise ValueError("Provide either spec.design (to simulate) or spec.existing_csv (to reuse an existing baseline CSV)")
    if existing_csv and design_spec:
        raise ValueError("Provide spec.design or spec.existing_csv, not both")

    ws: Path = cli.workspace
    comp_store = store.components
    stages: list[dict[str, Any]] = []
    est_cli_calls = 0

    # --- Stage 1: Optional inline zero-template component synthesis ---
    synth_items = spec.get("synthesize_components") or []
    synth_results: list[dict[str, Any]] = []
    if synth_items:
        for item in synth_items:
            synth_results.append(_synthesize_inline_component(comp_store, item, dry_run=dry_run))
        stages.append({
            "stage": "components",
            "status": "ready" if dry_run else "completed",
            "count": len(synth_results),
        })

    # --- Stage 2: Simulation or Existing CSV Breakpoint Reuse ---
    sim_result: dict[str, Any] | None = None
    baseline_csv_path: Path | None = None
    baseline_csv_name: str | None = None
    proj_name: str | None = spec.get("existing_project")
    var_name: str | None = spec.get("existing_variant")
    installed_dc_kw: float | None = (
        float(spec["installed_dc_kw"]) if spec.get("installed_dc_kw") is not None else None
    )

    if existing_csv:
        baseline_csv_path = _resolve_result_csv(ws, existing_csv)
        baseline_csv_name = baseline_csv_path.name
        if installed_dc_kw is None:
            raise ValueError("spec.installed_dc_kw is required when reusing spec.existing_csv")
        stages.append({
            "stage": "simulation",
            "status": "reused",
            "csv_name": baseline_csv_name,
        })
    else:
        assert design_spec is not None
        est_cli_calls += 1
        weather_mode = (design_spec.get("weather") or {}).get("mode")
        if weather_mode in ("csv", "open_meteo", "pvgis_tmy", "nasa_power"):
            est_cli_calls += 2  # create-site + convert-meteo
        if installed_dc_kw is None and design_spec.get("target_dc_kw") is not None:
            installed_dc_kw = float(design_spec["target_dc_kw"])
        sim_result = simulate_design(cli, store, spec=design_spec, dry_run=dry_run)
        sim_status = sim_result.get("status", "unknown")
        if sim_status in ("blocked", "failed"):
            stages.append({"stage": "simulation", "status": sim_status, "issues": sim_result.get("issues")})
            return {
                "status": sim_status,
                "dry_run": dry_run,
                "study_name": study_name,
                "stages": stages,
                "synthesized_components": synth_results,
                "simulation": sim_result,
                "issues": sim_result.get("issues") or [],
            }
        proj_name = (sim_result.get("artifacts") or {}).get("project_name") or design_spec.get("new_project")
        var_name = (sim_result.get("artifacts") or {}).get("variant") or "VC0"
        if not dry_run:
            csv_ref = (sim_result.get("artifacts") or {}).get("csv")
            if csv_ref:
                baseline_csv_path = _resolve_result_csv(ws, csv_ref)
                baseline_csv_name = baseline_csv_path.name
            sizing = sim_result.get("sizing") or {}
            if sizing.get("actual_dc_kw"):
                installed_dc_kw = float(sizing["actual_dc_kw"])
        stages.append({
            "stage": "simulation",
            "status": sim_status,
            "project": proj_name,
            "variant": var_name,
            "csv_name": baseline_csv_name,
        })

    # --- Stage 3: Diagnostics (when baseline CSV is available) ---
    diag_result: dict[str, Any] | None = None
    if baseline_csv_path is not None and baseline_csv_path.is_file():
        diag_result = diagnose_simulation_csv(
            baseline_csv_path,
            store=store if (proj_name and var_name) else None,
            project=proj_name,
            variant=var_name,
            thresholds=spec.get("thresholds"),
        )
        stages.append({
            "stage": "diagnostics",
            "status": "needs_review" if diag_result.get("needs_review") else "ok",
            "finding_count": diag_result.get("finding_count", 0),
        })
    else:
        stages.append({"stage": "diagnostics", "status": "planned"})

    # --- Stage 4: Optional TOU PowerShift 48-slot schedule ---
    tariff_cfg = spec.get("tariff")
    financial_cfg = spec.get("financial")
    sensitivity_cfg = spec.get("sensitivity")
    bess_cfg = spec.get("bess_sizing")
    tou_result: dict[str, Any] | None = None

    if tariff_cfg and tariff_cfg.get("mode") == "tou" and tariff_cfg.get("tou_schedule"):
        if spec.get("auto_tou_schedule") or bess_cfg:
            pcs_vals = (bess_cfg or {}).get("pcs_kw_values") or [max(1.0, (installed_dc_kw or 100.0) * 0.5)]
            bp = (bess_cfg or {}).get("bess_params") or {}
            tou_result = plan_tou_storage_schedule(
                tou_schedule=tariff_cfg["tou_schedule"],
                charge_power_kw=max(float(v) for v in pcs_vals),
                discharge_power_kw=max(float(v) for v in pcs_vals),
                cycles_per_day=int(bp.get("cycles_per_day", 2)),
                round_trip_efficiency=float(bp.get("round_trip_efficiency", 0.88)),
                min_spread=float(bp.get("min_spread", 0.05)),
                default_import_rate=float(tariff_cfg.get("import_tariff", 0.0)),
                default_export_rate=float(tariff_cfg.get("feed_in_tariff", 0.0)),
            )
            stages.append({
                "stage": "tou_schedule",
                "status": tou_result["status"],
                "planned_cycles_count": tou_result["planned_cycles_count"],
            })

    # --- Stage 5: Optional Two-Stage Hybrid BESS Sizing Optimization ---
    bess_result: dict[str, Any] | None = None
    if bess_cfg:
        if not tariff_cfg or not financial_cfg:
            raise ValueError("spec.tariff and spec.financial are required when spec.bess_sizing is configured")
        verify_k = int(bess_cfg.get("verify_top_k", 0))
        est_cli_calls += verify_k
        if baseline_csv_path is not None and baseline_csv_path.is_file():
            bess_result = optimize_bess_sizing(
                workspace=ws,
                cli=cli,
                store=store,
                baseline_csv=baseline_csv_path,
                installed_dc_kw=installed_dc_kw or 100.0,
                bess_kwh_values=bess_cfg["bess_kwh_values"],
                pcs_kw_values=bess_cfg["pcs_kw_values"],
                tariff=tariff_cfg,
                financial=financial_cfg,
                strategies=bess_cfg.get("strategies"),
                load_profile=bess_cfg.get("load_profile"),
                bess_params=bess_cfg.get("bess_params"),
                pcs_capex_per_kw=float(bess_cfg.get("pcs_capex_per_kw", 0.0)),
                rank_by=bess_cfg.get("rank_by", "npv"),
                verify_top_k=verify_k,
                verify_config=bess_cfg.get("verify_config"),
                dry_run=dry_run,
                confirm=confirm,
            )
            stages.append({
                "stage": "bess_optimization",
                "status": bess_result["status"],
                "evaluated_count": bess_result["evaluated_count"],
                "best": bess_result["best"]["label"],
            })
        else:
            stages.append({
                "stage": "bess_optimization",
                "status": "planned",
                "grid_bess_kwh": bess_cfg["bess_kwh_values"],
                "grid_pcs_kw": bess_cfg["pcs_kw_values"],
                "verify_top_k": verify_k,
            })

    # --- Stage 6: Financial & Sensitivity Evaluation ---
    econ_result: dict[str, Any] | None = None
    report_scenarios: list[dict[str, Any]] = []
    if baseline_csv_name is not None:
        report_scenarios.append({
            "label": f"{study_name}_Baseline",
            "csv_name": baseline_csv_name,
            "project": proj_name,
            "variant": var_name,
            "installed_dc_kw": installed_dc_kw,
        })
        # Also include Stage 2 verified BESS scenarios if any were simulated
        for vc in ((bess_result or {}).get("stage2_verification") or {}).get("verified_comparisons") or ():
            report_scenarios.append({
                "label": vc["label"],
                "csv_name": vc["csv_name"],
                "project": (bess_cfg.get("verify_config") or {}).get("project") if bess_cfg else proj_name,
                "variant": vc["variant"],
                "installed_dc_kw": installed_dc_kw,
                "battery_kwh": vc["actual_battery_kwh"],
            })

    if (financial_cfg is not None or tariff_cfg is not None):
        if report_scenarios:
            econ_result = evaluate_economics(
                workspace=ws,
                scenarios=[
                    {k: v for k, v in sc.items() if k in ("label", "csv_name", "installed_dc_kw", "battery_kwh") and v is not None}
                    for sc in report_scenarios
                ],
                financial=financial_cfg,
                tariff=tariff_cfg,
                sensitivity=sensitivity_cfg,
                include_cashflow_schedule=True,
            )
            stages.append({
                "stage": "economics",
                "status": "ok",
                "best_scenario": econ_result["best_scenario"],
                "has_sensitivity": sensitivity_cfg is not None and sensitivity_cfg.get("enabled", True),
            })
        else:
            stages.append({"stage": "economics", "status": "planned"})

    # --- Stage 7: Interactive Bilingual Plotly HTML Report ---
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
                workspace=ws,
                store=store,
                title=report_cfg.get("title") or f"{study_name} Feasibility Report",
                output_name=safe_out,
                scenarios=report_scenarios,
                financial=financial_cfg,
                tariff=tariff_cfg,
                sensitivity=sensitivity_cfg,
                bess_optimization=bess_result,
                language=report_cfg.get("language", "zh"),
                plotlyjs=report_cfg.get("plotlyjs", "cdn"),
                thresholds=spec.get("thresholds"),
                overwrite=bool(report_cfg.get("overwrite", False)),
                dry_run=dry_run,
            )
            stages.append({
                "stage": "report",
                "status": report_result["status"],
                "report_path": report_result["report_path"],
            })
        else:
            stages.append({
                "stage": "report",
                "status": "planned",
                "report_path": str(out_path),
                "language": report_cfg.get("language", "zh"),
            })

    # Executive summary rollup
    exec_summary: dict[str, Any] = {
        "installed_dc_kw": installed_dc_kw,
        "estimated_cli_calls": est_cli_calls,
    }
    if diag_result:
        exec_summary["weighted_pr"] = diag_result.get("weighted_pr")
        exec_summary["needs_review"] = diag_result.get("needs_review")
        exec_summary["finding_count"] = diag_result.get("finding_count")
    if econ_result and econ_result.get("scenarios"):
        sc0 = econ_result["scenarios"][0]
        f0 = sc0["financials"]
        exec_summary.update({
            "currency": econ_result["currency"],
            "year1_energy_kwh": sc0["year1_energy_kwh"],
            "lcoe": f0["lcoe"],
            "npv": f0["npv"],
            "irr_pct": f0["irr_pct"],
            "simple_payback_years": f0["simple_payback_years"],
            "year1_demand_savings": f0.get("year1_demand_savings", 0.0),
        })
    if bess_result and bess_result.get("best"):
        exec_summary["recommended_bess"] = {
            "label": bess_result["best"]["label"],
            "strategy": bess_result["best"]["strategy"],
            "bess_kwh": bess_result["best"]["bess_kwh"],
            "pcs_kw": bess_result["best"]["pcs_kw"],
            "npv": bess_result["best"]["npv"],
            "irr_pct": bess_result["best"]["irr_pct"],
        }
    if report_result and not dry_run:
        exec_summary["report_path"] = report_result.get("report_path")

    return {
        "status": "ready" if dry_run else ("needs_review" if (diag_result and diag_result.get("needs_review")) else "completed"),
        "dry_run": dry_run,
        "study_name": study_name,
        "stages": stages,
        "executive_summary": exec_summary,
        "synthesized_components": synth_results,
        "simulation": sim_result,
        "diagnostics": diag_result,
        "tou_schedule": tou_result,
        "bess_optimization": bess_result,
        "economics": econ_result,
        "report": report_result,
    }

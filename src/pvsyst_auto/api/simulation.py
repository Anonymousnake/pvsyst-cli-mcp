"""Simulation MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from pydantic import Field, StrictBool, StrictInt, StrictStr
from pvsyst_auto.native.cli import VAR_GROUPS, build_sfi
from pvsyst_auto.engineering.review import EngineeringBriefRequest, review_engineering_brief
from pvsyst_auto.engineering.sizing import suggest_sizing
from pvsyst_auto.workflows.batch import poll_batch_sweep_job, run_batch_sweep
from pvsyst_auto.workflows.bess_optimize import optimize_bess_sizing
from pvsyst_auto.workflows.design import simulate_design
from pvsyst_auto.workflows.optimize import optimize_variant
from pvsyst_auto.workflows.sourced_design import prepare_sourced_design
from pvsyst_auto.api.schemas import (
    BatchSweepRequest, BessSizingParams, BessVerifyConfig,
    ConstantNativeLoad, DailyNativeLoad,
    EconomicsFinancialConfig, EconomicsScenarioInput, EconomicsSensitivityConfig, EconomicsTariffConfig,
    EngineeringBlock, EngineeringRevisionRequest, EngineeringScenario, FeasibilityStudyRequest,
    HouseTaskRequest, ReportScenarioInput, SingleDesignRequest, StrictNum,
    SourceDeclarationRequest, SourceTextRequest, SourcedDesignGoals, SourcedHouseTaskRequest,
)
from pvsyst_auto.api.context import categorized_tool, cli, components, optional_path, simulation_csv_path, variant_store, workspace_path
from pvsyst_auto.components.editor import sourced_component


@categorized_tool()
def pvsyst_build_sfi(filename: str, variables: str = "all_common", time_step: str = "hour") -> dict:
    """Create a new export definition (.SFI) in workspace/Models from a preset group
    (energy, inverter_losses, all_common, shading, soiling, grid_limit, grid_storage, bifacial)
    or comma-separated vi* variables, with time_step='hour' (default), 'subhour', 'day', or 'month'."""
    with cli()._lock:
        target = workspace_path("Models", filename, ".sfi")
        names = (VAR_GROUPS[variables] if variables in VAR_GROUPS
                 else [part.strip() for part in variables.split(",")])
        build_sfi(target, list(names), time_step=time_step)
    return {"sfi": str(target), "variables": list(names), "time_step": time_step.strip().lower()}


@categorized_tool()
def pvsyst_run_batch_sweep(
    spec: BatchSweepRequest | None = None,
    dry_run: StrictBool = True,
    background: StrictBool = False,
    job_id: StrictStr | None = None,
) -> dict:
    """Generate UserBatch <sweep_name>_BatchParams.CSV and <sweep_name>_BatchVariables.RVT from a
    Cartesian parameter grid or explicit scenario rows (<=100 scenarios), run one PVsystCLI batch
    simulation, and return ranked scenario results with baseline deltas.

    Supports both ``inverter_count`` (auto-scaled by MPPT/inverter on independent-MPPT subarrays)
    and ``mppt_count`` (raw MPPT count), auto-converts fractional ``albedo`` (0..1 -> %), and
    supports ``background=True`` + ``job_id`` polling for long sweeps (>8 scenarios)."""
    if job_id is not None:
        if spec is not None:
            raise ValueError("Provide either job_id (to poll an existing background sweep) or spec, not both")
        return poll_batch_sweep_job(cli(), job_id)
    if spec is None:
        raise ValueError("Provide spec to start/preview a batch sweep, or job_id to poll a running background sweep")
    return run_batch_sweep(
        cli(),
        project=spec.project,
        variant=spec.variant,
        sweep_name=spec.sweep_name,
        grid=spec.grid,
        scenarios=spec.scenarios,
        result_variables=spec.result_variables,
        sort_by=spec.sort_by,
        sort_descending=spec.sort_descending,
        export_hourly=spec.export_hourly,
        dry_run=dry_run,
        background=background,
        log_level=spec.log_level,
        allow_window_creation=spec.allow_window_creation,
    )

@categorized_tool()
def pvsyst_optimize_variant(
        project: StrictStr, variant: StrictStr, csv_name: StrictStr,
        max_iterations: Annotated[StrictInt, Field(ge=1, le=5)] = 2,
        allow_resize: StrictBool = True,
        min_ambient_c: StrictNum = -10.0,
        max_operating_c: StrictNum = 65.0,
        thresholds: dict[StrictStr, StrictNum] | None = None,
        sfi_name: StrictStr | None = None,
        time_step: Literal["hour", "subhour"] = "hour",
        allow_window_creation: StrictBool | None = None,
        dry_run: StrictBool = True,
        confirm: StrictBool = False) -> dict:
    """Bounded Diagnose -> Clone -> Patch -> Re-simulate loop. Default dry_run=true returns
    deterministic remediation actions and unified diff previews without modifying files;
    dry_run=false with confirm=true clones new sequential variants (never mutating baseline)
    for up to max_iterations (1..5), stopping early once high/critical anomalies clear."""
    return optimize_variant(
        cli(), variant_store(),
        project=project, variant=variant, csv_name=csv_name,
        max_iterations=max_iterations, allow_resize=allow_resize,
        min_ambient_c=min_ambient_c, max_operating_c=max_operating_c,
        thresholds=thresholds, sfi_name=sfi_name, time_step=time_step,
        allow_window_creation=allow_window_creation,
        dry_run=dry_run, confirm=confirm,
    )


@categorized_tool()
def pvsyst_optimize_bess_sizing(
        baseline_csv: StrictStr,
        installed_dc_kw: StrictNum,
        bess_kwh_values: Annotated[list[StrictNum], Field(min_length=1, max_length=20)],
        pcs_kw_values: Annotated[list[StrictNum], Field(min_length=1, max_length=15)],
        tariff: EconomicsTariffConfig,
        financial: EconomicsFinancialConfig,
        strategies: list[Literal["self_consumption", "tou_arbitrage", "peak_shaving"]] | None = None,
        load_profile: ConstantNativeLoad | DailyNativeLoad | None = None,
        bess_params: BessSizingParams | None = None,
        pcs_capex_per_kw: StrictNum = 0.0,
        rank_by: Literal["npv", "irr", "lcoe", "simple_payback_years", "discounted_payback_years"] = "npv",
        verify_top_k: Annotated[StrictInt, Field(ge=0, le=5)] = 0,
        verify_config: BessVerifyConfig | None = None,
        dry_run: StrictBool = True,
        confirm: StrictBool = False) -> dict:
    """Two-stage hybrid PV+BESS capacity sizing optimizer: Stage 1 runs a fast pure-Python grid sweep
    over (bess_kwh x pcs_kw x strategy) against TOU and two-part demand-charge tariffs, returning
    ranked candidates, a CAPEX-vs-NPV Pareto frontier, and a 2D heatmap; optional Stage 2
    (verify_top_k 1..5 with dry_run=false and confirm=true) runs PVsystCLI to reconcile exact KPIs."""
    current = cli()
    return optimize_bess_sizing(
        workspace=current.workspace,
        cli=current,
        store=variant_store(),
        baseline_csv=baseline_csv,
        installed_dc_kw=installed_dc_kw,
        bess_kwh_values=bess_kwh_values,
        pcs_kw_values=pcs_kw_values,
        tariff=tariff.model_dump(exclude_none=True),
        financial=financial.model_dump(exclude_none=True),
        strategies=strategies,
        load_profile=load_profile.model_dump(exclude_none=True) if load_profile is not None else None,
        bess_params=bess_params.model_dump(exclude_none=True) if bess_params is not None else None,
        pcs_capex_per_kw=pcs_capex_per_kw,
        rank_by=rank_by,
        verify_top_k=verify_top_k,
        verify_config=verify_config.model_dump(exclude_none=True) if verify_config is not None else None,
        dry_run=dry_run,
        confirm=confirm,
    )


@categorized_tool()
def pvsyst_run_house_task(spec: HouseTaskRequest, dry_run: StrictBool = True,
                            confirm: StrictBool = False, resume: StrictBool = False,
                            retry_simulation: StrictBool = False) -> dict:
    """Build a 3D house scene (.pvc2), write it headlessly via the native builder,
    bind roof orientations, save a new variant, and run a full-year CLI simulation with checkpoint resume."""
    from pvsyst_auto.workflows.house import run_house_task
    store = variant_store()
    return run_house_task(store, store.components, cli(), request=spec.model_dump(exclude_none=True),
        dry_run=dry_run, confirm=confirm, resume=resume, retry_simulation=retry_simulation)

@categorized_tool()
def pvsyst_run_simulation(project: str, variant: str,
                          sfi_name: str = "", csv_name: str = "", pdf_name: str = "",
                          met_name: str = "", rvt_name: str = "",
                          params_name: str = "", batch_params_name: str = "",
                          batch_rvt_name: str = "", site_name: str = "",
                          synthetic_weather: StrictBool = False,
                          time_step: str = "", start_date: str = "", end_date: str = "",
                          report_language: str = "", report_pages: str = "",
                          recompute_shading: StrictBool | None = None,
                          log_level: StrictInt | None = None,
                          allow_window_creation: StrictBool | None = None) -> dict:
    """Run a single or batch simulation via PVsystCLI.exe on an existing project/variant,
    writing CSV/PDF outputs to Results or batch outputs to UserBatch/UserHourly."""
    current = cli()
    with current._lock:
        options = dict(
            sfi=optional_path("Models", sfi_name, ".sfi"),
            out_csv=simulation_csv_path(csv_name),
            report_pdf=optional_path("Results", pdf_name, ".pdf"),
            met=optional_path("Meteo", met_name, ".met"),
            rvt=optional_path("Models", rvt_name, ".rvt"),
            params=optional_path("UserData", params_name, ".dat"),
            batch_params=optional_path("UserBatch", batch_params_name, ".csv"),
            batch_rvt=optional_path("UserBatch", batch_rvt_name, ".rvt"),
            site=optional_path("Sites", site_name, ".sit"),
            synthetic_weather=synthetic_weather, time_step=time_step or None,
            start_date=start_date or None, end_date=end_date or None,
            report_language=report_language or None,
            report_pages=[p.strip() for p in report_pages.split(",")] if report_pages else None,
            recompute_shading=recompute_shading, log_level=log_level,
            allow_window_creation=allow_window_creation,
        )
        # Pure filename/path/date/output checks run before any license query or help launch.
        current.preflight_run_simulation(project, variant, **options)
        license_state = current.license_info(allow_window_creation=allow_window_creation)
        if license_state.get("status") == "TRIAL" and license_state.get("remaining_executions") == 0:
            raise ValueError("PVsystCLI reports no remaining trial executions")
        sim_result = current.run_simulation(project, variant, **options)
        if sim_result.get("csv") and not options.get("batch_params"):
            from pvsyst_auto.engineering.diagnostics import compact_diagnostics_summary, diagnose_simulation_csv

            diag = None
            try:
                diag = diagnose_simulation_csv(
                    sim_result["csv"], store=variant_store(), project=project, variant=variant,
                )
            except (ValueError, OSError):
                try:
                    diag = diagnose_simulation_csv(sim_result["csv"])
                except (ValueError, OSError):
                    diag = None
            if diag is not None:
                sim_result["physical_diagnostics"] = compact_diagnostics_summary(diag)
                if diag.get("needs_review") or diag.get("status") == "blocked":
                    sim_result["status"] = "needs_review"
                else:
                    sim_result.setdefault("status", "completed")
        return sim_result

@categorized_tool()
def pvsyst_simulate_design(spec: SingleDesignRequest, dry_run: StrictBool = True,
                           verbose: StrictBool = False) -> dict:
    """Run a unified grid design workflow (fixed_unshaded_grid, ground_sheds, or ground_tracker,
    with optional ground_layout and pvBiFacial): component lookup, zero-template synthesis,
    capacity sizing, weather preparation/acquisition, and full-year simulation.
    Set verbose=true to expand per-template discovery rejection details."""
    return simulate_design(cli(), variant_store(), spec=spec.model_dump(exclude_none=True),
                           dry_run=dry_run, verbose=verbose)

@categorized_tool()
def pvsyst_prepare_sourced_design(pan_name: StrictStr, ond_name: StrictStr,
                                  sources: dict[str, SourceTextRequest],
                                  declarations: list[SourceDeclarationRequest],
                                  design: SourcedDesignGoals | None = None) -> dict:
    """Bind cited datasheet values and units against loose PAN/OND files to build
    an electrical_design envelope request."""
    selected = {}
    store = components()
    with store.lock:  # one lock for both reads: a concurrent workspace update cannot split them
        for kind, filename in (("PAN", pan_name), ("OND", ond_name)):
            field, message, payload = sourced_component(store, kind, filename)
            if payload is None:
                return {"status": "blocked", "stage": "components",
                        "issues": [{"field": field, "message": message}], "warnings": [],
                        "electrical_design": None, "capacity": None, "envelope": None,
                        "missing_fields": [], "needs_review": False, "provenance": []}
            selected[kind] = payload
    label = (f"{selected['PAN']['manufacturer']} {selected['PAN']['model']} / "
             f"{selected['OND']['manufacturer']} {selected['OND']['model']}")
    return prepare_sourced_design(
        selected["PAN"]["electrical"], selected["OND"]["electrical"], manufacturer_model=label,
        sources={key: entry.model_dump(exclude_none=True) for key, entry in sources.items()},
        declarations=[entry.model_dump(exclude_none=True) for entry in declarations],
        design=design.model_dump(exclude_none=True) if design is not None else None)

@categorized_tool()
def pvsyst_run_sourced_house_task(spec: SourcedHouseTaskRequest, dry_run: StrictBool = True,
        confirm: StrictBool = False, resume: StrictBool = False, retry_simulation: StrictBool = False,
        accept_source_review: StrictBool = False) -> dict:
    """Bind cited datasheet inputs, prepare a source project (or reuse an existing source via
    prepare_mode='preserve_source_hardware'), and run the 3D house simulation pipeline."""
    from pvsyst_auto.workflows.house import is_preserve_request, run_sourced_house_task
    current = cli()
    store = variant_store()
    with store.lock:
        request = spec.model_dump(exclude_none=True, exclude={"sources", "declarations", "goals"})
        # Branch before the loose-component/global sourced binding: preserve reuses the existing
        # source hardware and never needs a single loose PAN/OND file or a fabricated envelope.
        if is_preserve_request(request):
            return run_sourced_house_task(store, store.components, current, request=request,
                dry_run=dry_run, confirm=confirm, resume=resume, retry_simulation=retry_simulation,
                accept_source_review=accept_source_review)
        goals = spec.goals.model_dump(exclude_none=True) if spec.goals is not None else {}
        if (spec.design.target_dc_kw is not None and "target_dc_kw" not in goals
                and not any(entry.field == "target_dc_kw" for entry in spec.declarations)):
            goals["target_dc_kw"] = spec.design.target_dc_kw
        binding = pvsyst_prepare_sourced_design(
            pan_name=spec.design.module.file_name or "", ond_name=spec.design.inverter.file_name or "",
            sources=spec.sources, declarations=spec.declarations, design=SourcedDesignGoals(**goals))
        return run_sourced_house_task(store, store.components, current, request=request, binding=binding,
            dry_run=dry_run, confirm=confirm, resume=resume, retry_simulation=retry_simulation,
            accept_source_review=accept_source_review)


@categorized_tool()
def pvsyst_suggest_sizing(
        pan_name: StrictStr, ond_name: StrictStr, target_dc_kw: StrictNum,
        voltage_standard: Literal["IEC", "UL"] = "IEC",
        min_ambient_c: StrictNum = -10.0,
        min_operating_c: StrictNum = 0.0,
        max_operating_c: StrictNum = 65.0,
        min_dc_ac_ratio: StrictNum = 0.85,
        max_dc_ac_ratio: StrictNum = 1.25,
        capacity_tolerance_pct: StrictNum = 8.0,
        beta_voc_pct_per_c: StrictNum | None = None,
        beta_vmp_pct_per_c: StrictNum | None = None,
        alpha_isc_pct_per_c: StrictNum | None = None) -> dict:
    """Read-only sizing assistant: derive recommended series/parallel/inverter counts and a drop-in
    capacity envelope from readable PAN/OND files with explicit assumptions (requires engineering review)."""
    return suggest_sizing(
        variant_store(),
        pan_name=pan_name, ond_name=ond_name, target_dc_kw=target_dc_kw,
        voltage_standard=voltage_standard,
        min_ambient_c=min_ambient_c, min_operating_c=min_operating_c, max_operating_c=max_operating_c,
        min_dc_ac_ratio=min_dc_ac_ratio, max_dc_ac_ratio=max_dc_ac_ratio,
        capacity_tolerance_pct=capacity_tolerance_pct,
        beta_voc_pct_per_c=beta_voc_pct_per_c,
        beta_vmp_pct_per_c=beta_vmp_pct_per_c,
        alpha_isc_pct_per_c=alpha_isc_pct_per_c,
    )


@categorized_tool()
def pvsyst_prepare_block_project(source_project: StrictStr, source_variant: StrictStr,
                                 new_project: StrictStr,
                                 blocks: Annotated[list[EngineeringBlock], Field(min_length=1, max_length=100)],
                                 dry_run: StrictBool = True) -> dict:
    """Assemble a new project by repeating fixed subarray blocks from a template project
    by integer counts (1..100 total blocks covering every source subarray)."""
    from pvsyst_auto.workflows.simulation import prepare_block_project
    return prepare_block_project(variant_store(), source_project=source_project, source_variant=source_variant,
        new_project=new_project, blocks=[block.model_dump() for block in blocks], dry_run=dry_run)

@categorized_tool()
def pvsyst_review_engineering_brief(brief: EngineeringBriefRequest) -> dict:
    """Check structured feasibility claims (module/inverter counts, strings, block schedules,
    and required PAN/OND/weather filenames) for internal consistency and missing inputs."""
    return review_engineering_brief(brief)

@categorized_tool()
def pvsyst_project_handoff(project: StrictStr, variant: StrictStr,
                           declarations: dict[StrictStr, Any] | None = None) -> dict:
    """Read saved equipment, counts, orientation/circuit relationships, weather, load,
    and storage settings alongside caller declarations and missing inputs."""
    from pvsyst_auto.engineering.comparison import build_project_handoff
    return build_project_handoff(variant_store(), project=project, variant=variant, declarations=declarations)

@categorized_tool()
def pvsyst_run_engineering_task(spec: EngineeringRevisionRequest, dry_run: StrictBool = True) -> dict:
    """Apply an isolated revision (fixed angles/losses, UnlimTracking controls, or AC-coupled
    storage with hourly load) in a new project and run a full-year simulation."""
    from pvsyst_auto.workflows.revision import run_engineering_task
    return run_engineering_task(cli(), variant_store(), **spec.model_dump(exclude_none=True), dry_run=dry_run)

@categorized_tool()
def pvsyst_compare_engineering_scenarios(objective: StrictStr, scenarios: list[EngineeringScenario],
                                        output_name: StrictStr, dry_run: StrictBool = True) -> dict:
    """Compare two or three completed simulation runs against a baseline scenario and write
    JSON, CSV, and Markdown comparison reports under workspace/Results."""
    from pvsyst_auto.engineering.comparison import compare_engineering_scenarios
    return compare_engineering_scenarios(variant_store(), objective=objective,
        scenarios=[scenario.model_dump(exclude_none=True) for scenario in scenarios],
        output_name=output_name, dry_run=dry_run)

@categorized_tool()
def pvsyst_evaluate_economics(
        scenarios: list[EconomicsScenarioInput] | None = None,
        batch_summary_csv: StrictStr | None = None,
        financial: EconomicsFinancialConfig | None = None,
        tariff: EconomicsTariffConfig | None = None,
        sensitivity: EconomicsSensitivityConfig | None = None,
        rank_by: Literal[
            "npv", "irr", "equity_irr", "lcoe", "simple_payback_years",
            "discounted_payback_years", "year1_net_benefit",
        ] = "npv",
        include_cashflow_schedule: StrictBool = True) -> dict:
    """Pure-Python PV and PV+BESS financial evaluator: streams single or weighted multi-CSV
    hourly/sub-hourly SFI CSVs (or UserBatch summary CSVs) under fixed, TOU, or two-part
    demand-charge tariffs to compute LCOE, NPV, pre/post-tax project IRR, levered Equity IRR
    and DSCR (via optional composable VAT, CIT, loan financing, and staged OPEX tiers),
    payback, sensitivity sweeps, and scenario rankings."""
    from pvsyst_auto.engineering.economics import evaluate_economics
    return evaluate_economics(
        workspace=cli().workspace,
        scenarios=[s.model_dump(exclude_none=True) for s in scenarios] if scenarios is not None else None,
        batch_summary_csv=batch_summary_csv,
        financial=financial.model_dump(exclude_none=True) if financial is not None else None,
        tariff=tariff.model_dump(exclude_none=True) if tariff is not None else None,
        sensitivity=sensitivity.model_dump(exclude_none=True) if sensitivity is not None else None,
        rank_by=rank_by,
        include_cashflow_schedule=include_cashflow_schedule,
    )

@categorized_tool()
def pvsyst_generate_report(
        title: StrictStr,
        output_name: StrictStr,
        scenarios: Annotated[list[ReportScenarioInput], Field(min_length=1, max_length=20)],
        financial: EconomicsFinancialConfig | None = None,
        tariff: EconomicsTariffConfig | None = None,
        sensitivity: EconomicsSensitivityConfig | None = None,
        bess_optimization: dict[StrictStr, Any] | None = None,
        batch_summary_csv: StrictStr | None = None,
        language: Literal["zh", "en"] = "zh",
        plotlyjs: Literal["cdn", "embed"] = "cdn",
        thresholds: dict[StrictStr, StrictNum] | None = None,
        overwrite: StrictBool = False,
        dry_run: StrictBool = False) -> dict:
    """Generate a single-scenario or multi-scenario interactive Plotly HTML engineering report
    under workspace/Results (monthly yield/PR combo, loss waterfall, typical 24h PV/load/BESS/TOU
    profile, monthly peak demand shaving, 25-year cumulative cashflow, sensitivity tornado chart,
    optional BESS sizing heatmap/Pareto frontier, optional UserBatch parametric sweep curve chart
    via batch_summary_csv, diagnostics table, and print-ready CSS)."""
    from pvsyst_auto.workflows.report import generate_report
    return generate_report(
        workspace=cli().workspace,
        store=variant_store(),
        title=title,
        output_name=output_name,
        scenarios=[s.model_dump(exclude_none=True) for s in scenarios],
        financial=financial.model_dump(exclude_none=True) if financial is not None else None,
        tariff=tariff.model_dump(exclude_none=True) if tariff is not None else None,
        sensitivity=sensitivity.model_dump(exclude_none=True) if sensitivity is not None else None,
        bess_optimization=bess_optimization,
        batch_summary_csv=batch_summary_csv,
        language=language,
        plotlyjs=plotlyjs,
        thresholds=thresholds,
        overwrite=overwrite,
        dry_run=dry_run,
    )

@categorized_tool()
def pvsyst_export_engineering_delivery(manifest_name: StrictStr, output_name: StrictStr,
                                      dry_run: StrictBool = True, comparison_name: StrictStr = "") -> dict:
    """Bundle a completed engineering run (and optional scenario comparison) with its PRJ/VC,
    result CSV/PDF, and Markdown handoff summary into a ZIP archive under workspace/Results."""
    from pvsyst_auto.workflows.revision import export_engineering_delivery
    return export_engineering_delivery(variant_store(), manifest_name=manifest_name,
        output_name=output_name, dry_run=dry_run, comparison_name=comparison_name)

@categorized_tool()
def pvsyst_run_feasibility_study(
        spec: FeasibilityStudyRequest,
        dry_run: StrictBool = True,
        confirm: StrictBool = False) -> dict:
    """One-shot end-to-end PV & PV+BESS feasibility study orchestrator: chains optional
    zero-template PAN/OND/BTR synthesis, PV simulation (or reuse of existing_csv), physics
    diagnostics, TOU PowerShift 48-slot planning, two-stage hybrid BESS sizing optimization,
    two-part demand/TOU economics with sensitivity tornado analysis, and bilingual Plotly HTML
    report generation. Defaults to dry_run=true; requires dry_run=false and confirm=true to write."""
    from pvsyst_auto.workflows.optimize import run_feasibility_study
    return run_feasibility_study(
        cli(),
        variant_store(),
        spec=spec.model_dump(exclude_none=True),
        dry_run=dry_run,
        confirm=confirm,
    )

"""Simulation MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import Field, StrictBool, StrictInt, StrictStr
from pvsyst_auto.native.cli import VAR_GROUPS, build_sfi
from pvsyst_auto.workflows.batch import run_batch_sweep
from pvsyst_auto.workflows.design import simulate_design
from pvsyst_auto.workflows.optimize import optimize_variant
from pvsyst_auto.workflows.sourced_design import prepare_sourced_design
from pvsyst_auto.api.schemas import (
    BatchSweepRequest, HouseTaskRequest, SingleDesignRequest, StrictNum,
    SourceDeclarationRequest, SourceTextRequest, SourcedDesignGoals, SourcedHouseTaskRequest,
)
from pvsyst_auto.api.context import categorized_tool, cli, components, optional_path, simulation_csv_path, variant_store, workspace_path
from pvsyst_auto.components.identity import sourced_component


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
def pvsyst_run_batch_sweep(spec: BatchSweepRequest, dry_run: StrictBool = True) -> dict:
    """Generate UserBatch <sweep_name>_BatchParams.CSV and <sweep_name>_BatchVariables.RVT from a
    Cartesian parameter grid or explicit scenario rows (<=100 scenarios), run one PVsystCLI batch
    simulation, and return ranked scenario results with baseline deltas."""
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
def pvsyst_run_house_task(spec: HouseTaskRequest, dry_run: StrictBool = True,
                            confirm: StrictBool = False, resume: StrictBool = False,
                            retry_simulation: StrictBool = False) -> dict:
    """Build a 3D house scene (.pvc2), import it via GUI or headless native builder,
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
        return current.run_simulation(project, variant, **options)

@categorized_tool()
def pvsyst_simulate_design(spec: SingleDesignRequest, dry_run: StrictBool = True) -> dict:
    """Run a unified grid design workflow (fixed_unshaded_grid, ground_sheds, or ground_tracker,
    with optional ground_layout and pvBiFacial): component lookup, zero-template synthesis,
    capacity sizing, weather preparation/acquisition, and full-year simulation."""
    return simulate_design(cli(), variant_store(), spec=spec.model_dump(exclude_none=True), dry_run=dry_run)

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
    from pvsyst_auto.workflows.sourced_house import is_preserve_request, run_sourced_house_task
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

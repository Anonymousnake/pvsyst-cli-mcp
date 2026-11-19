"""Engineering MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from pydantic import Field, StrictBool, StrictStr
from pvsyst_auto.engineering.brief import EngineeringBriefRequest, review_engineering_brief
from pvsyst_auto.engineering.suggest import suggest_sizing
from pvsyst_auto.api.schemas import (
    EconomicsFinancialConfig,
    EconomicsScenarioInput,
    EconomicsTariffConfig,
    EngineeringBlock,
    EngineeringRevisionRequest,
    EngineeringScenario,
    StrictNum,
)
from pvsyst_auto.api.context import categorized_tool, cli, variant_store


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
    from pvsyst_auto.workflows.blocks import prepare_block_project
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
    from pvsyst_auto.engineering.handoff import build_project_handoff
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
        rank_by: Literal[
            "npv", "irr", "lcoe", "simple_payback_years",
            "discounted_payback_years", "year1_net_benefit",
        ] = "npv",
        include_cashflow_schedule: StrictBool = True) -> dict:
    """Pure-Python PV and PV+BESS financial evaluator: streams hourly or sub-hourly SFI CSVs
    (or UserBatch summary CSVs) under fixed or time-of-use (TOU) tariffs to compute LCOE,
    NPV, IRR, simple/discounted payback, 25-year degraded cashflows, and scenario rankings."""
    from pvsyst_auto.engineering.economics import evaluate_economics
    return evaluate_economics(
        workspace=cli().workspace,
        scenarios=[s.model_dump(exclude_none=True) for s in scenarios] if scenarios is not None else None,
        batch_summary_csv=batch_summary_csv,
        financial=financial.model_dump(exclude_none=True) if financial is not None else None,
        tariff=tariff.model_dump(exclude_none=True) if tariff is not None else None,
        rank_by=rank_by,
        include_cashflow_schedule=include_cashflow_schedule,
    )


@categorized_tool()
def pvsyst_export_engineering_delivery(manifest_name: StrictStr, output_name: StrictStr,
                                      dry_run: StrictBool = True, comparison_name: StrictStr = "") -> dict:
    """Bundle a completed engineering run (and optional scenario comparison) with its PRJ/VC,
    result CSV/PDF, and Markdown handoff summary into a ZIP archive under workspace/Results."""
    from pvsyst_auto.workflows.delivery import export_engineering_delivery
    return export_engineering_delivery(variant_store(), manifest_name=manifest_name,
        output_name=output_name, dry_run=dry_run, comparison_name=comparison_name)

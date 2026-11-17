"""Engineering MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from typing import Annotated, Any
from pydantic import Field, StrictBool, StrictStr
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.engineering.brief import EngineeringBriefRequest, review_engineering_brief
from pvsyst_auto.api.schemas import EngineeringBlock, EngineeringRevisionRequest, EngineeringScenario
from pvsyst_auto.api.context import categorized_tool, cli, components


@categorized_tool()
def pvsyst_prepare_block_project(source_project: StrictStr, source_variant: StrictStr,
                                 new_project: StrictStr,
                                 blocks: Annotated[list[EngineeringBlock], Field(min_length=1, max_length=100)],
                                 dry_run: StrictBool = True) -> dict:
    """Assemble a new project by repeating fixed subarray blocks from a template project
    by integer counts (1..100 total blocks covering every source subarray)."""
    from pvsyst_auto.workflows.blocks import prepare_block_project
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return prepare_block_project(store, source_project=source_project, source_variant=source_variant,
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
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return build_project_handoff(store, project=project, variant=variant, declarations=declarations)

@categorized_tool()
def pvsyst_run_engineering_task(spec: EngineeringRevisionRequest, dry_run: StrictBool = True) -> dict:
    """Apply an isolated revision (fixed angles/losses, UnlimTracking controls, or AC-coupled
    storage with hourly load) in a new project and run a full-year simulation."""
    from pvsyst_auto.workflows.revision import run_engineering_task
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return run_engineering_task(cli(), store, **spec.model_dump(exclude_none=True), dry_run=dry_run)

@categorized_tool()
def pvsyst_compare_engineering_scenarios(objective: StrictStr, scenarios: list[EngineeringScenario],
                                        output_name: StrictStr, dry_run: StrictBool = True) -> dict:
    """Compare two or three completed simulation runs against a baseline scenario and write
    JSON, CSV, and Markdown comparison reports under workspace/Results."""
    from pvsyst_auto.engineering.comparison import compare_engineering_scenarios
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return compare_engineering_scenarios(store, objective=objective,
        scenarios=[scenario.model_dump(exclude_none=True) for scenario in scenarios],
        output_name=output_name, dry_run=dry_run)

@categorized_tool()
def pvsyst_export_engineering_delivery(manifest_name: StrictStr, output_name: StrictStr,
                                      dry_run: StrictBool = True, comparison_name: StrictStr = "") -> dict:
    """Bundle a completed engineering run (and optional scenario comparison) with its PRJ/VC,
    result CSV/PDF, and Markdown handoff summary into a ZIP archive under workspace/Results."""
    from pvsyst_auto.workflows.delivery import export_engineering_delivery
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return export_engineering_delivery(store, manifest_name=manifest_name,
        output_name=output_name, dry_run=dry_run, comparison_name=comparison_name)

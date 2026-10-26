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
    """Prepare one NEW project from explicit counts of fully defined native block types.
    Each block names a unique source_subarray_id and count INCLUDING the original.
    Cover every source subarray exactly once, at most 100 blocks in total. Copy whole
    exclusive inverter/MPPT/string branches with their source equipment, counts and
    ordinary fixed-plane orientation. Do not infer internal wiring, resize hardware,
    choose models or change angles. Only the selected source VC and PRJ are published.
    Preview builds in memory without writes; execution never overwrites or backs up
    an existing project. No native run, GUI, INI, weather acquisition or certification.
    Unsupported shading, sharing and common load/storage/transformer/limit/auxiliary
    systems stop; inherited weather/losses/electrical suitability require review.
    Prepared mapping and serialized counts do not prove native consumption.
    """
    from pvsyst_auto.workflows.blocks import prepare_block_project
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return prepare_block_project(store, source_project=source_project, source_variant=source_variant,
        new_project=new_project, blocks=[block.model_dump() for block in blocks], dry_run=dry_run)

@categorized_tool()
def pvsyst_review_engineering_brief(brief: EngineeringBriefRequest) -> dict:
    """Review caller-structured cited engineering facts before a native project exists.
    Aggregate same-scope conflicts, whole-string and block quantities, declared DC/AC
    arithmetic, nameplate Pmax versus Vmp*Imp and missing exact PAN/OND/hourly weather.
    Use explicit count/W/kW/kWh/V/A/deg/m units and PDF physical page citations; no
    automatic unit conversion, chapter selection, equipment or topology inference.
    Readonly with no files, PDF, native executable, license, network, GUI or INI access.
    Source quotes remain caller declarations. Returns blocked or needs_review only;
    even consistent facts do not certify equipment, physics or engineering readiness.
    """
    return review_engineering_brief(brief)

@categorized_tool()
def pvsyst_project_handoff(project: StrictStr, variant: StrictStr,
                           declarations: dict[StrictStr, Any] | None = None) -> dict:
    """Read saved equipment, counts, orientation/circuit relationships and input dependencies.
    Includes explicit caller declarations, inherited values, missing inputs and engineering
    review items. Fixed/tracking and existing AC storage use the native saved records.
    Readonly: no GUI, license query, simulation, repairs or writes. ready/needs_review
    describe the handoff only; native_consumption_verified remains false.
    """
    from pvsyst_auto.engineering.handoff import build_project_handoff
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return build_project_handoff(store, project=project, variant=variant, declarations=declarations)

@categorized_tool()
def pvsyst_run_engineering_task(spec: EngineeringRevisionRequest, dry_run: StrictBool = True) -> dict:
    """Preview or execute an isolated revision of a fixed/UnlimTracking or existing AC-storage project.
    State the engineering objective, nameplate kWp/source and explicit supported changes.
    Existing equipment/counts/weather stay inherited; no model fitting or sizing inference.
    Existing AC storage accepts storage scalar updates and a prepared load_manifest_name;
    WeakGrid additionally requires the complete availability declaration. The stored load
    must match its prepared snapshot and site standard UTC offset. Native annual results
    verify actual load/SOC/AC limits; storage never uses the PV-only E_Grid PR formula.
    Preview writes nothing. Execution creates a NEW project/VC, reloads via native CLI,
    runs one complete hourly year, produces native CSV/PDF and records the actual run
    inputs for comparison and delivery. Existing names stop; failure retains evidence
    without automatic retry/rollback. Inspect stage and native_consumption_verified as
    well as status: needs_review can mean a completed run still requiring engineering review.
    """
    from pvsyst_auto.workflows.revision import run_engineering_task
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return run_engineering_task(cli(), store, **spec.model_dump(exclude_none=True), dry_run=dry_run)

@categorized_tool()
def pvsyst_compare_engineering_scenarios(objective: StrictStr, scenarios: list[EngineeringScenario],
                                        output_name: StrictStr, dry_run: StrictBool = True) -> dict:
    """Organize two or three native result scenarios around an explicit engineering question.
    First scenario is baseline with no declared changes. Each candidate declares its
    intended changed saved-input groups/paths. Completed engineering run manifests bind
    historical inputs to CSVs; unbound historical results retain needs_review. Compare
    only actual native columns with aligned timestamps/units; missing columns stay missing.
    Returns Chinese explanations and key-hour differences without claiming optimality
    or a single cause for simultaneous changes. Preview is readonly; execution writes
    new JSON/CSV/Markdown comparison files under Results without rerunning PVsyst.
    """
    from pvsyst_auto.engineering.comparison import compare_engineering_scenarios
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return compare_engineering_scenarios(store, objective=objective,
        scenarios=[scenario.model_dump(exclude_none=True) for scenario in scenarios],
        output_name=output_name, dry_run=dry_run)

@categorized_tool()
def pvsyst_export_engineering_delivery(manifest_name: StrictStr, output_name: StrictStr,
                                      dry_run: StrictBool = True, comparison_name: StrictStr = "") -> dict:
    """Package a completed engineering run as a NEW local Results ZIP.
    Reads one workspace run manifest and checks its input/output binding. Includes the
    selected native PRJ/VC, original CSV/PDF, Chinese summary and input handoff. Optional
    house runs additionally include their genuine task manifest, authored PVC2 and SFI.
    comparison_name includes its bound scenarios and Chinese comparison report. Commercial
    component/weather files remain external dependencies. Export does not certify the
    design or upgrade a review status. Preview writes nothing; existing ZIPs stop.
    """
    from pvsyst_auto.workflows.delivery import export_engineering_delivery
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return export_engineering_delivery(store, manifest_name=manifest_name,
        output_name=output_name, dry_run=dry_run, comparison_name=comparison_name)

"""Shared MCP registration, strict arguments, guidance layer and workspace-scoped clients."""
from __future__ import annotations

from collections.abc import Collection, Mapping
from functools import wraps
from importlib import import_module
import inspect
import json
import os
from pathlib import Path
import re
from threading import Lock
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.native.cli import PVsystCLI, PVsystError, REPORT_PAGES, VAR_GROUPS
from pvsyst_auto.project.paths import workspace_file
from pvsyst_auto.project.variants import VariantStore

_cli: PVsystCLI | None = None
_cli_key: tuple[str | None, str | None] | None = None
_cli_owner: PVsystCLI | None = None
_cli_init_lock = Lock()

TOOL_GROUPS = {
    "Setup & License": ("pvsyst_capabilities", "pvsyst_license_info"),
    "Admin & Diagnostics": (
        "pvsyst_license_activate", "pvsyst_license_deactivate",
        "pvsyst_license_sync", "pvsyst_export_logs",
    ),
    "Projects & Variants": (
        "pvsyst_list_projects", "pvsyst_inspect_project", "pvsyst_prepare_design_project",
        "pvsyst_clone_variant", "pvsyst_get_variant_parameters", "pvsyst_update_variant_parameters",
        "pvsyst_create_tracking_variant", "pvsyst_create_shed_geometry_variant",
        "pvsyst_create_ac_cabling_variant", "pvsyst_create_dc_cabling_variant",
        "pvsyst_configure_transformers", "pvsyst_convert_orientation",
    ),
    "Components": (
        "pvsyst_list_components", "pvsyst_get_component",
        "pvsyst_create_component", "pvsyst_update_component", "pvsyst_build_sourced_component",
    ),
    "Sites & Weather": (
        "pvsyst_resolve_location", "pvsyst_acquire_weather",
        "pvsyst_prepare_weather", "pvsyst_review_weather",
    ),
    "Loads & Storage": (
        "pvsyst_prepare_load_profile", "pvsyst_create_native_load_variant",
        "pvsyst_create_grid_storage_variant", "pvsyst_create_grid_storage_system",
        "pvsyst_plan_tou_storage_schedule",
    ),
    "Simulation": (
        "pvsyst_build_sfi", "pvsyst_run_simulation", "pvsyst_run_batch_sweep",
        "pvsyst_simulate_design", "pvsyst_prepare_sourced_design", "pvsyst_run_house_task",
        "pvsyst_optimize_variant", "pvsyst_optimize_bess_sizing",
    ),
    "Results": ("pvsyst_read_results", "pvsyst_read_rows", "pvsyst_read_batch_results", "pvsyst_diagnose_results"),
    "Engineering": (
        "pvsyst_suggest_sizing", "pvsyst_project_handoff",
        "pvsyst_run_engineering_task", "pvsyst_compare_engineering_scenarios",
        "pvsyst_evaluate_economics", "pvsyst_generate_report",
        "pvsyst_export_engineering_delivery", "pvsyst_run_feasibility_study",
    ),
    "Expert & Low-Level": (
        "pvsyst_list_variants", "pvsyst_create_project", "pvsyst_update_project_parameters",
        "pvsyst_update_project_sources", "pvsyst_get_variant_components",
        "pvsyst_update_variant_components", "pvsyst_validate_variant_structure",
        "pvsyst_clone_subarray", "pvsyst_remove_subarray", "pvsyst_review_project_inputs",
        "pvsyst_create_house_pv_scene", "pvsyst_read_house_pv_scene", "pvsyst_import_house_pv_scene",
        "pvsyst_validate_component", "pvsyst_compare_components", "pvsyst_project_components",
        "pvsyst_inspect_weather_csv", "pvsyst_build_monthly_weather",
        "pvsyst_create_site", "pvsyst_convert_meteo",
        "pvsyst_run_sourced_house_task",
        "pvsyst_prepare_block_project", "pvsyst_review_engineering_brief",
    ),
}

TOOL_CATEGORY = {name: category for category, names in TOOL_GROUPS.items() for name in names}
if len(TOOL_CATEGORY) != sum(map(len, TOOL_GROUPS.values())):
    raise RuntimeError("Duplicate tool category assignment")

STRICT_ARGUMENT_TOOLS = tuple(TOOL_CATEGORY)

GROUP_MODULES = {
    "setup": "Setup & License",
    "projects": "Projects & Variants",
    "components": "Components",
    "weather": "Sites & Weather",
    "storage": "Loads & Storage",
    "simulation": "Simulation",
    "results": "Results",
    "engineering": "Engineering",
    "admin": "Admin & Diagnostics",
    "expert": "Expert & Low-Level",
}
DEFAULT_TOOL_GROUPS = (
    "setup", "projects", "components", "weather",
    "storage", "simulation", "results", "engineering",
)
GROUP_IMPORT_MODULES = {
    "setup": ("projects",), "admin": ("projects",), "projects": ("projects",), "components": ("projects",),
    "weather": ("weather",), "storage": ("weather",), "results": ("weather",),
    "simulation": ("simulation",), "engineering": ("simulation",),
    "expert": ("projects", "weather", "simulation"),
}

_EXPERT_DIRECT_WRITERS = ("pvsyst_create_site", "pvsyst_build_monthly_weather", "pvsyst_convert_meteo")
_OPT_IN_TOOLS = frozenset({
    "pvsyst_license_activate", "pvsyst_license_deactivate", "pvsyst_license_sync", "pvsyst_export_logs",
    *_EXPERT_DIRECT_WRITERS,
})


def resolve_tool_groups(requested: str | None) -> tuple[str, ...]:
    if requested is None:
        return DEFAULT_TOOL_GROUPS
    groups = [group.strip() for group in requested.split(",")]
    if groups == ["all"]:
        return tuple(GROUP_MODULES)
    if any(group not in GROUP_MODULES for group in groups) or len(set(groups)) != len(groups):
        raise ValueError("PVSYST_TOOL_GROUPS requires 'all' or distinct nonempty keys from: " + ", ".join(GROUP_MODULES))
    return tuple(group for group in GROUP_MODULES if group in groups)


def tool_names_for_groups(groups: tuple[str, ...]) -> frozenset[str]:
    return frozenset(name for group in groups for name in TOOL_GROUPS[GROUP_MODULES[group]])


def import_modules_for_groups(groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(mod for group in groups for mod in GROUP_IMPORT_MODULES[group]))


def build_server_instructions(active_tools: Collection[str] | None = None) -> str:
    """Build MCP server instructions referencing only the registered tool surface."""
    active_set = frozenset(active_tools) if active_tools is not None else None

    def _has(*names: str) -> bool:
        if active_set is None:
            return all(name not in _OPT_IN_TOOLS for name in names)
        return all(name in active_set for name in names)

    def _any(*names: str) -> list[str]:
        return [name for name in names if _has(name)]

    sections: list[str] = []
    if _has("pvsyst_capabilities", "pvsyst_license_info"):
        sections.append("Check environment first: call `pvsyst_capabilities` and `pvsyst_license_info` before running simulations.")
    elif _has("pvsyst_capabilities"):
        sections.append("Check environment first: call `pvsyst_capabilities` to inspect CLI commands and options.")
    elif _has("pvsyst_license_info"):
        sections.append("Check environment first: call `pvsyst_license_info` to check license status and quota.")

    comp_parts: list[str] = []
    if _has("pvsyst_list_components", "pvsyst_get_component"):
        comp_parts.append(
            "Search & select components with `pvsyst_list_components` (supports electrical filters: "
            "`min_power_w`, `max_power_w`, `bifacial`, `min_system_voltage_v`, `min_mppt`, `manufacturer`) / `pvsyst_get_component`"
        )
    elif _has("pvsyst_list_components"):
        comp_parts.append("Search & filter components with `pvsyst_list_components`")
    elif _has("pvsyst_get_component"):
        comp_parts.append("Inspect components with `pvsyst_get_component`")
    if _has("pvsyst_build_sourced_component"):
        comp_parts.append(
            "synthesize custom commercial `.PAN`/`.OND` (single- or 3-voltage) / Li-ion `.BTR` from cited datasheets "
            "via `pvsyst_build_sourced_component` (omit `template_name` for zero-template synthesis)"
        )
    if _has("pvsyst_update_component"):
        comp_parts.append(
            "pass `refit_single_diode=true` on `pvsyst_update_component` to refit `.PAN` single-diode parameters when editing STC values"
        )
    if comp_parts:
        sections.append("Components: " + "; ".join(comp_parts) + ".")

    if _has("pvsyst_resolve_location", "pvsyst_acquire_weather", "pvsyst_simulate_design"):
        sections.append(
            "Weather: call `pvsyst_resolve_location` -> `pvsyst_acquire_weather(provider=\"open_meteo_era5\" | "
            "\"pvgis_tmy\" | \"nasa_power\", prepare=true)` to fetch and convert `.SIT`/`.MET` in one step, or pass "
            "`weather.mode=\"open_meteo\" | \"pvgis_tmy\" | \"nasa_power\"` directly in `pvsyst_simulate_design`."
        )
    elif weather_tools := _any("pvsyst_resolve_location", "pvsyst_acquire_weather", "pvsyst_prepare_weather", "pvsyst_review_weather"):
        sections.append("Weather: use " + ", ".join(f"`{t}`" for t in weather_tools) + ".")

    design_bullets: list[str] = []
    if _has("pvsyst_simulate_design"):
        design_bullets.extend([
            "   - Use `pvsyst_simulate_design` with `scope=\"fixed_unshaded_grid\" | \"ground_sheds\" | \"ground_tracker\" | \"stand_alone\"`.",
            "   - For utility ground PV, pass optional `ground_layout` (`model_mode=\"unlimited\" | \"3d\"`, `site_polygon`, `obstacles`, `base_slope_deg`, or 3D DEM `terrain` from XYZ JSON/CSV/GeoTIFF), `bifacial`, and MV/HV `transformers`.",
            "   - For off-grid Stand-Alone (`scope=\"stand_alone\"`), pass `standalone` (`.BTR` battery bank, MPPT/direct `.RLT` controller, optional `.GEN` backup generator or inline `genset_spec`, load profile, and diesel-fuel economics).",
        ])
    variant_parts: list[str] = []
    if _has("pvsyst_configure_transformers"):
        variant_parts.append("configure multi-stage MV/HV transformers & AC collection cables via `pvsyst_configure_transformers`")
    if _has("pvsyst_convert_orientation"):
        variant_parts.append("convert orientation type in-place (`fixed`/`seasonal`/`sheds`/`tracker_ns`/`tracker_2axis`) via `pvsyst_convert_orientation`")
    if _has("pvsyst_update_variant_parameters"):
        variant_parts.append("update spectral correction, random unavailability, and native `TEcoEvaluation` PDF financials via `pvsyst_update_variant_parameters`")
    if variant_parts:
        design_bullets.append("   - For existing variants, " + ", or ".join(variant_parts) + ".")
    if _has("pvsyst_suggest_sizing"):
        design_bullets.append("   - Pass `electrical_design`/`capacity` or call `pvsyst_suggest_sizing` / `sizing_mode=\"auto_from_components\"`.")
    if design_bullets:
        sections.append("Fixed-plane, utility ground PV, or Stand-Alone off-grid PV+BESS+Genset:\n" + "\n".join(design_bullets))

    if _has("pvsyst_run_house_task"):
        sections.append("3D rooftop house + obstacles + electrical shading (`module_layout`):\n   - Call `pvsyst_run_house_task` (omit `source_project`/`source_variant` for zero-template synthesis).")

    ci_bullets: list[str] = []
    if _has("pvsyst_plan_tou_storage_schedule"):
        ci_bullets.append("   - Convert TOU tariffs into PVsyst `PowerShift` 48-slot schedules via `pvsyst_plan_tou_storage_schedule`.")
    if _has("pvsyst_optimize_bess_sizing"):
        ci_bullets.append("   - Run two-stage hybrid BESS sizing (`bess_kwh x pcs_kw x strategy` + optional Stage 2 PVsystCLI verification) via `pvsyst_optimize_bess_sizing`.")
    if _has("pvsyst_run_batch_sweep") and _has("pvsyst_diagnose_results") and _has("pvsyst_optimize_variant"):
        ci_bullets.append("   - Sweep parameters in one CLI call with `pvsyst_run_batch_sweep` (`dry_run=true` first), audit with `pvsyst_diagnose_results`, and auto-remediate via `pvsyst_optimize_variant`.")
    else:
        if _has("pvsyst_run_batch_sweep"):
            ci_bullets.append("   - Sweep parameters in one CLI call with `pvsyst_run_batch_sweep` (`dry_run=true` first).")
        if _has("pvsyst_diagnose_results"):
            ci_bullets.append("   - Audit simulation CSVs with `pvsyst_diagnose_results`.")
        if _has("pvsyst_optimize_variant"):
            ci_bullets.append("   - Auto-remediate anomalies on cloned variants via `pvsyst_optimize_variant`.")
    if _has("pvsyst_evaluate_economics"):
        ci_bullets.append("   - Evaluate fixed, TOU, and two-part demand-charge tariffs, battery replacement, sensitivity tornadoes, LCOE, NPV, IRR, and payback via `pvsyst_evaluate_economics`.")
    if _has("pvsyst_run_feasibility_study"):
        ci_bullets.append("   - Run a complete one-shot feasibility study (synthesis -> simulation/reuse -> diagnostics -> TOU/BESS sizing -> economics -> Plotly report) via `pvsyst_run_feasibility_study` (`dry_run=true` first, then `dry_run=false, confirm=true`).")
    report_delivery: list[str] = []
    if _has("pvsyst_generate_report"):
        report_delivery.append("Generate interactive Plotly HTML engineering reports (zh/en, with demand shaving, tornado, and BESS heatmap charts) via `pvsyst_generate_report`")
    if _has("pvsyst_export_engineering_delivery"):
        report_delivery.append("bundle deliverables via `pvsyst_export_engineering_delivery`")
    if report_delivery:
        ci_bullets.append("   - " + ", and ".join(report_delivery).capitalize() + ".")
    if ci_bullets:
        sections.append("C&I PV+BESS optimization, economics, feasibility studies & interactive reporting:\n" + "\n".join(ci_bullets))

    if results_tools := _any("pvsyst_read_results", "pvsyst_read_rows", "pvsyst_read_batch_results"):
        if not ci_bullets or not _has("pvsyst_simulate_design"):
            extra = " (or run `pvsyst_diagnose_results` above)" if _has("pvsyst_diagnose_results") else ""
            sections.append("Simulation result inspection: read outputs with " + ", ".join(f"`{t}`" for t in results_tools) + extra + ".")

    if admin_tools := _any("pvsyst_license_activate", "pvsyst_license_deactivate", "pvsyst_license_sync", "pvsyst_export_logs"):
        sections.append("Admin & Diagnostics: manage licensing or export logs via " + ", ".join(f"`{t}`" for t in admin_tools) + " (license mutations require `confirm=true`).")

    lines = ["PVsyst CLI MCP drives a local Windows PVsyst 8.x installation and workspace.", "", "Quick workflow selection:"]
    for idx, section in enumerate(sections, start=1):
        lines.append(f"{idx}. {section}")

    lines.append(f"{len(sections) + 1}. Safety & verification conventions:")
    dry_note = " (including `pvsyst_generate_report`)" if _has("pvsyst_generate_report") else ""
    lines.append(f"   - For mutating tools whose schema declares a `dry_run` parameter{dry_note}, always preview with `dry_run=true` before executing `dry_run=false`.")
    if direct_writers := _any("pvsyst_build_sfi", "pvsyst_clone_variant", "pvsyst_create_component", "pvsyst_run_simulation", *_EXPERT_DIRECT_WRITERS):
        lines.append("   - Direct writers without a `dry_run` parameter (" + ", ".join(f"`{t}`" for t in direct_writers) + ") write immediately; never overwrite existing projects/outputs and always pick fresh output filenames.")
    lines.append("   - Consult on-demand resources `pvsyst://guide/workflows`, `pvsyst://guide/variables`, `pvsyst://workspace/projects`, `pvsyst://workspace/components`, and `pvsyst://workspace/results` for live workspace state, SFI variables, and multi-step recipes.")
    return "\n".join(lines) + "\n"


SERVER_INSTRUCTIONS = build_server_instructions()

WORKFLOW_GUIDE_MD = """# PVsyst CLI MCP — Agent Workflow Playbook

## 1. Zero-Template Fixed or Utility Ground PV (`pvsyst_simulate_design`)
1. `pvsyst_capabilities` + `pvsyst_license_info`
2. `pvsyst_list_components(component_type="PAN")` and `pvsyst_list_components(component_type="OND")`
3. Optional location lookup: `pvsyst_resolve_location(query="<city>")` -> take `location_id`
4. Optional sizing check: `pvsyst_suggest_sizing(pan_name=..., ond_name=..., target_dc_kw=...)`
5. Preflight: `pvsyst_simulate_design(spec={scope, new_project, year, target_dc_kw, module, inverter, weather, sizing_mode="auto_from_components", ground_layout, bifacial}, dry_run=True)`
6. Execute: `pvsyst_simulate_design(..., dry_run=False)` -> inspect `status`, `metrics`, `bifacial_metrics`, `diagnostics`
7. Optional package: `pvsyst_export_engineering_delivery(manifest_name=..., output_name=..., dry_run=False)`

## 2. Zero-Template 3D Rooftop / House PV (`pvsyst_run_house_task`)
1. Weather prep: `pvsyst_resolve_location` -> `pvsyst_acquire_weather(..., dry_run=False)` -> `pvsyst_prepare_weather(..., dry_run=False)`
2. Author `house` (`kind="mesh"`), `layout` (`tables`), optional `obstacles` (`building`, `tree`, `wall`, `mesh`), optional `string_layout`
3. Preflight: `pvsyst_run_house_task(spec={site_name, met_name, pan_name, ond_name, new_project, variant_name, year, house, layout, shading_mode="module_layout"}, dry_run=True)`
4. Execute: `pvsyst_run_house_task(..., dry_run=False, confirm=True)`

## 3. Existing Project + AC Storage / Load / Cabling / MV-HV Transformers / Orientation
1. `pvsyst_project_handoff(project=..., variant=...)` to inspect equipment, counts, weather, load, and storage
2. Load (skip for `peak_shaving`): `pvsyst_create_native_load_variant` (`constant` / `monthly` / `daily` / `household` / `probability`) or `pvsyst_prepare_load_profile` (hourly CSV in `UserData`; required for `self_consumption` and `weak_grid`)
3. Storage: `pvsyst_create_grid_storage_system` (`self_consumption` / `weak_grid` require `load_manifest_name`; `peak_shaving` requires a source variant without `pvLoad` and must omit `load_manifest_name`; `power_shift` accepts either; `weak_grid` also supports deterministic `mode="generated"` outage schedules)
4. MV/HV Transformers & Orientation: `pvsyst_configure_transformers` (MV-only, parallel MV, or cascaded MV+HV + cables + night disconnect) and `pvsyst_convert_orientation` (`fixed`/`seasonal`/`sheds`/`tracker_ns`/`tracker_2axis`)
5. Simulate & read: `pvsyst_build_sfi(variables="grid_storage" | "transformers" | "stand_alone")` -> `pvsyst_run_simulation` -> `pvsyst_read_results`

## 4. Stand-Alone Off-Grid PV + BESS + Backup Genset (`pvsyst_simulate_design`)
1. `pvsyst_simulate_design(spec={scope="stand_alone", new_project, tilt, azimuth, module, weather, standalone={battery_file, n_mod_serie, n_string_ch, n_batt_serie, n_batt_parall, controller_type="mppt"|"direct", genset_spec|genset_file, backup_operating_power_kw, load|daily_load_kwh, economics}}, dry_run=True)`
2. Execute with `dry_run=False` -> inspect `energy_balance` (`e_user_kwh`, `e_solar_kwh`, `e_backup_kwh`, `e_missing_kwh`, `backup_fuel_liters`, `soc_mean`, `lol_pct`), `diagnostics`, and optional diesel-fuel `economics`.

## 5. Task Status Meanings
- `ready` / `ready_for_weather`: dry-run passed; nothing written.
- `prepared`: project/weather files written; simulation not run yet.
- `completed` (`native_consumption_verified=true`): PVsystCLI ran and passed output/unit/calendar checks.
- `needs_review`: completed with caveats (e.g., `auto_from_components` sizing assumptions, weather shifts, or physical diagnostic anomalies).
- `blocked` / `failed`: preflight or CLI failed; inspect `issues` and use a new output name on retry.
"""


def _variables_guide_md() -> str:
    lines = [
        "# PVsyst SFI Variable Groups & Report Pages", "",
        "Pass any group name below to `pvsyst_build_sfi(filename=..., variables=<group>)`,",
        "or pass a comma-separated list of explicit `vi*` variable identifiers.", "",
        "## Built-in Variable Groups (`VAR_GROUPS`)", "",
        *(f"- **`{group}`** ({len(variables)} vars): `{', '.join(variables)}`" for group, variables in VAR_GROUPS.items()),
        "", "## Supported PDF Report Pages (`report_pages`)", "", f"`{', '.join(sorted(REPORT_PAGES))}`",
    ]
    return "\n".join(lines) + "\n"


_PROMPT_REQUIRED_TOOLS = {
    "design_fixed_or_utility_pv": frozenset({
        "pvsyst_capabilities", "pvsyst_license_info", "pvsyst_resolve_location",
        "pvsyst_list_components", "pvsyst_suggest_sizing", "pvsyst_simulate_design",
    }),
    "design_rooftop_3d_pv": frozenset({
        "pvsyst_capabilities", "pvsyst_license_info", "pvsyst_resolve_location",
        "pvsyst_acquire_weather", "pvsyst_prepare_weather", "pvsyst_list_components", "pvsyst_run_house_task",
    }),
    "design_pv_with_storage": frozenset({
        "pvsyst_project_handoff", "pvsyst_list_components", "pvsyst_prepare_load_profile",
        "pvsyst_create_grid_storage_system", "pvsyst_build_sfi", "pvsyst_run_simulation",
        "pvsyst_read_results", "pvsyst_compare_engineering_scenarios",
    }),
    "diagnose_and_optimize_simulation": frozenset({
        "pvsyst_get_variant_parameters", "pvsyst_project_handoff", "pvsyst_read_results",
        "pvsyst_diagnose_results", "pvsyst_optimize_variant", "pvsyst_clone_variant",
        "pvsyst_update_variant_parameters", "pvsyst_run_batch_sweep",
    }),
    "ci_bess_feasibility_study": frozenset({
        "pvsyst_capabilities", "pvsyst_license_info", "pvsyst_resolve_location",
        "pvsyst_list_components", "pvsyst_build_sourced_component",
        "pvsyst_plan_tou_storage_schedule", "pvsyst_run_feasibility_study",
    }),
    "optimize_bess_sizing": frozenset({"pvsyst_optimize_bess_sizing", "pvsyst_generate_report"}),
}


def register_guidance(server, active_tools: Collection[str] | None = None) -> None:
    """Attach enabled MCP prompts and read-only reference resources to ``server``."""
    active_set = frozenset(active_tools) if active_tools is not None else None

    def _prompt_enabled(name: str) -> bool:
        return active_set is None or _PROMPT_REQUIRED_TOOLS[name] <= active_set

    if _prompt_enabled("design_fixed_or_utility_pv"):
        @server.prompt(name="design_fixed_or_utility_pv",
                       description="Step-by-step workflow to design and simulate a fixed-plane, ground sheds, or single-axis tracker PV plant (mono or bifacial).")
        def design_fixed_or_utility_pv(location: str, target_dc_kw: str = "1000", scope: str = "ground_tracker", bifacial: str = "true") -> str:
            return (
                f"Design and simulate a {target_dc_kw} kWp PV plant at {location} (scope={scope}, bifacial={bifacial}) using the PVsyst CLI MCP tools:\n"
                "1. Call `pvsyst_capabilities` and `pvsyst_license_info` to confirm CLI readiness and quota.\n"
                "2. Call `pvsyst_resolve_location` for the target location and `pvsyst_list_components` for available PAN/OND files.\n"
                "   Note: if bifacial=true, verify the loose .PAN declares bifaciality (Flags bit 23 / $00800000).\n"
                "3. Call `pvsyst_suggest_sizing` (or pass `sizing_mode='auto_from_components'` directly in `pvsyst_simulate_design`) to derive a safe string/MPPT/inverter layout.\n"
                "4. Run `pvsyst_simulate_design(..., dry_run=True)` first to inspect planned files, sizing checks, and weather plan; once `ready`/`ready_for_weather`, execute with `dry_run=False`.\n"
                "5. Summarize annual E_Grid, PR, specific yield, bifacial rear gain (if enabled), and any diagnostic warnings."
            )

    if _prompt_enabled("design_rooftop_3d_pv"):
        @server.prompt(name="design_rooftop_3d_pv",
                       description="Step-by-step workflow to model a 3D house/rooftop PV system with obstacles and module-level shading.")
        def design_rooftop_3d_pv(location: str, roof_description: str, shading_mode: str = "module_layout") -> str:
            return (
                f"Build and simulate a 3D rooftop PV system at {location} ({roof_description}) with shading_mode={shading_mode}:\n"
                "1. Check `pvsyst_capabilities` and `pvsyst_license_info`.\n"
                "2. Prepare workspace weather files: `pvsyst_resolve_location` -> `pvsyst_acquire_weather(dry_run=False)` -> `pvsyst_prepare_weather(dry_run=False)`.\n"
                "3. List available PAN/OND files with `pvsyst_list_components`.\n"
                "4. Construct planar upward-facing roof meshes (`house.kind='mesh'`), PV table placements (`layout`), surrounding `obstacles`, and optional `string_layout`.\n"
                "5. Call `pvsyst_run_house_task` without `source_project`/`source_variant` (zero-template synthesis) with `dry_run=True` first, then `dry_run=False, confirm=True`.\n"
                "6. Report annual E_Grid, PR, shading losses (`GlobShd`/`ShdElec`), and any diagnostics."
            )

    if _prompt_enabled("design_pv_with_storage"):
        @server.prompt(name="design_pv_with_storage",
                       description="Step-by-step workflow to add load profiles and AC-coupled battery storage to a PV project and compare strategies.")
        def design_pv_with_storage(project: str, variant: str = "VC0", storage_strategy: str = "peak_shaving") -> str:
            strategy = storage_strategy.strip().lower()
            if strategy == "peak_shaving":
                return (
                    f"Add AC-coupled battery storage ({storage_strategy}) to {project}/{variant}:\n"
                    "1. Inspect the baseline via `pvsyst_project_handoff(project, variant)` to confirm the source variant "
                    "has no self-consumption `pvLoad` block (PVsyst rejects PeakShaving paired with a self-consumption load), "
                    "and list batteries via `pvsyst_list_components(component_type='BTR', library='builtin')`.\n"
                    "2. Do not attach a load profile and omit `load_manifest_name` when creating `peak_shaving` storage.\n"
                    "3. Create the storage variant via `pvsyst_create_grid_storage_system` (`storage={'strategy': 'peak_shaving', ...}`, omitting `load_manifest_name`; preview with `dry_run=True` then execute with `dry_run=False`).\n"
                    "4. Build a storage SFI with `pvsyst_build_sfi(variables='grid_storage')`, run `pvsyst_run_simulation`, and compare against baseline using `pvsyst_read_results(compare_to=...)` or `pvsyst_compare_engineering_scenarios`."
                )
            if strategy in ("self_consumption", "weak_grid"):
                extra = " and supply a matching `availability` profile" if strategy == "weak_grid" else ""
                return (
                    f"Add load and AC-coupled battery storage ({storage_strategy}) to {project}/{variant}:\n"
                    "1. Inspect the baseline via `pvsyst_project_handoff(project, variant)` and available batteries via `pvsyst_list_components(component_type='BTR', library='builtin')`.\n"
                    f"2. Prepare an hourly load profile via `pvsyst_prepare_load_profile` (hourly CSV in `UserData`) and record its `load_manifest_name` (required for `{strategy}`{extra}).\n"
                    "3. Create the storage variant via `pvsyst_create_grid_storage_system` passing `load_manifest_name` (`dry_run=True` then `dry_run=False`).\n"
                    "4. Build a storage SFI with `pvsyst_build_sfi(variables='grid_storage')`, run `pvsyst_run_simulation`, and compare against baseline using `pvsyst_read_results(compare_to=...)` or `pvsyst_compare_engineering_scenarios`."
                )
            return (
                f"Add AC-coupled battery storage ({storage_strategy}) to {project}/{variant}:\n"
                "1. Inspect the baseline via `pvsyst_project_handoff(project, variant)` and available batteries via `pvsyst_list_components(component_type='BTR', library='builtin')`.\n"
                "2. Load binding depends on strategy: for `self_consumption` or `weak_grid`, prepare an hourly load via `pvsyst_prepare_load_profile` and pass `load_manifest_name`; for `peak_shaving`, use a source variant without `pvLoad` and omit `load_manifest_name`; for `power_shift`, a load is optional.\n"
                "3. Create the storage variant via `pvsyst_create_grid_storage_system` (`dry_run=True` then `dry_run=False`).\n"
                "4. Build a storage SFI with `pvsyst_build_sfi(variables='grid_storage')`, run `pvsyst_run_simulation`, and compare against baseline using `pvsyst_read_results(compare_to=...)` or `pvsyst_compare_engineering_scenarios`."
            )

    if _prompt_enabled("diagnose_and_optimize_simulation"):
        @server.prompt(name="diagnose_and_optimize_simulation",
                       description="Audit a completed simulation CSV + variant parameters for anomalies (clipping, overvoltage, shading, storage) and propose targeted fixes.")
        def diagnose_and_optimize_simulation(project: str, variant: str, csv_name: str) -> str:
            return (
                f"Audit simulation results `{csv_name}` for `{project}/{variant}`:\n"
                "1. Read variant state with `pvsyst_get_variant_parameters` or `pvsyst_project_handoff`.\n"
                "2. Summarize key columns with `pvsyst_read_results` and run `pvsyst_diagnose_results` to inspect PR, inverter limits (`IL_Vmax`, `IL_Vmin`, `IL_Pmax`, `IL_Imax`), shading (`GlobShd`, `ShdElec`), and grid/storage losses.\n"
                "3. Map each anomaly back to concrete `.VC0` parameters (e.g., `NModSerie`, DC/AC ratio, pitch/tilt, storage schedule).\n"
                "4. Propose and verify an isolated revised variant (`pvsyst_optimize_variant`, `pvsyst_clone_variant` / `pvsyst_update_variant_parameters`, or `pvsyst_run_batch_sweep`) and compare before/after performance."
            )

    @server.resource("pvsyst://guide/workflows", name="pvsyst_workflows_guide", title="PVsyst CLI MCP Workflow Playbook",
                     description="End-to-end recipes for fixed, utility ground/bifacial, 3D rooftop, and storage simulations.", mime_type="text/markdown")
    def workflows_guide() -> str:
        return WORKFLOW_GUIDE_MD

    @server.resource("pvsyst://guide/variables", name="pvsyst_variables_guide", title="PVsyst SFI Variable Groups & Report Pages",
                     description="Available VAR_GROUPS presets for pvsyst_build_sfi and supported PDF report_pages tokens.", mime_type="text/markdown")
    def variables_guide() -> str:
        return _variables_guide_md()

    def _active_workspace() -> Path | None:
        raw = os.environ.get("PVSYST_WORKSPACE", "").strip()
        return Path(raw).resolve() if (raw and Path(raw).is_dir()) else None

    @server.resource("pvsyst://workspace/projects", name="pvsyst_workspace_projects", title="Workspace Projects & Variants",
                     description="Live read-only JSON summary of PVsyst projects and variants in the configured PVSYST_WORKSPACE.", mime_type="application/json")
    def workspace_projects_resource() -> str:
        ws = _active_workspace()
        if ws is None:
            return json.dumps({"configured": False, "projects": []}, ensure_ascii=False, indent=2)
        proj_dir = ws / "Projects"
        items = []
        if proj_dir.is_dir():
            prjs: list[Path] = []
            variants_by_stem: dict[str, list[str]] = {}
            for entry in proj_dir.iterdir():
                if not entry.is_file():
                    continue
                suf = entry.suffix.lstrip(".")
                if suf.lower() == "prj":
                    prjs.append(entry)
                elif re.fullmatch(r"V[A-Z0-9]{2}", suf.upper()):
                    variants_by_stem.setdefault(entry.stem.casefold(), []).append(suf.upper())
            for prj in sorted(prjs)[:200]:
                items.append({"project": prj.name, "variants": sorted(variants_by_stem.get(prj.stem.casefold(), ()))})
        return json.dumps({"configured": True, "workspace": str(ws), "projects": items}, ensure_ascii=False, indent=2)

    @server.resource("pvsyst://workspace/components", name="pvsyst_workspace_components", title="Workspace Components Catalog",
                     description="Live read-only JSON catalog of loose PAN, OND, BTR, and GEN components with cached electrical metadata.", mime_type="application/json")
    def workspace_components_resource() -> str:
        ws = _active_workspace()
        if ws is None:
            return json.dumps({"configured": False, "components": {}}, ensure_ascii=False, indent=2)
        comp = ComponentStore(ws)
        catalog = {}
        for kind in ("PAN", "OND", "BTR", "GEN"):
            try:
                catalog[kind] = comp.list(kind, "workspace", limit=100).get("items", [])
            except Exception:
                catalog[kind] = []
        return json.dumps({"configured": True, "workspace": str(ws), "components": catalog}, ensure_ascii=False, indent=2)

    @server.resource("pvsyst://workspace/results", name="pvsyst_workspace_results", title="Workspace Simulation Results & Reports",
                     description="Live read-only JSON listing of recent simulation CSV, PDF, HTML reports, and manifests in Results/ and UserHourly/.", mime_type="application/json")
    def workspace_results_resource() -> str:
        ws = _active_workspace()
        if ws is None:
            return json.dumps({"configured": False, "files": []}, ensure_ascii=False, indent=2)
        entries = []
        for folder in ("Results", "UserHourly"):
            d = ws / folder
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if p.is_file() and p.suffix.lower() in {".csv", ".pdf", ".html", ".json"}:
                    entries.append({"folder": folder, "name": p.name, "bytes": p.stat().st_size, "mtime": round(p.stat().st_mtime, 3)})
        entries.sort(key=lambda item: -item["mtime"])
        return json.dumps({"configured": True, "workspace": str(ws), "files": entries[:100]}, ensure_ascii=False, indent=2)

    if _prompt_enabled("ci_bess_feasibility_study"):
        @server.prompt(name="ci_bess_feasibility_study",
                       description="End-to-end C&I PV+BESS feasibility workflow with two-part demand tariff, TOU PowerShift scheduling, sizing optimization, sensitivity analysis, and bilingual Plotly HTML report.")
        def ci_bess_feasibility_study(location: str, target_dc_kw: str = "500", language: str = "zh") -> str:
            return (
                f"Run a complete C&I PV+BESS feasibility study for a {target_dc_kw} kWp plant at {location} (language={language}):\n"
                "1. Inspect workspace readiness with `pvsyst_capabilities`, `pvsyst_license_info`, and resources `pvsyst://workspace/components` and `pvsyst://workspace/projects`.\n"
                "2. Resolve location (`pvsyst_resolve_location`) and select or synthesize commercial PAN/OND/BTR components via `pvsyst_list_components` or zero-template `pvsyst_build_sourced_component`.\n"
                "3. Plan two-charge/two-discharge TOU windows with `pvsyst_plan_tou_storage_schedule`.\n"
                "4. Run `pvsyst_run_feasibility_study(spec=..., dry_run=True)` to preview the full execution graph (weather, PV/BESS simulation, diagnostics, two-part demand + TOU economics, sensitivity tornado, optional BESS sizing sweep, and interactive Plotly HTML report), then execute with `dry_run=False, confirm=True`.\n"
                "5. Present annual yield, PR, demand peak shaving savings, NPV, IRR, LCOE, payback, sensitivity drivers, and the generated HTML report path."
            )

    if _prompt_enabled("optimize_bess_sizing"):
        @server.prompt(name="optimize_bess_sizing",
                       description="Two-stage hybrid PV+BESS sizing optimization workflow (fast pure-Python grid sweep + optional Stage 2 PVsystCLI verification).")
        def optimize_bess_sizing_prompt(baseline_csv: str, installed_dc_kw: str = "500", rank_by: str = "npv") -> str:
            return (
                f"Optimize battery capacity (kWh) and PCS power (kW) for `{baseline_csv}` ({installed_dc_kw} kWp, rank_by={rank_by}):\n"
                "1. Confirm the baseline CSV and available load/TOU/demand-charge tariff parameters.\n"
                "2. Run `pvsyst_optimize_bess_sizing` Stage 1 (`verify_top_k=0`, `dry_run=True`) across candidate `bess_kwh_values` and `pcs_kw_values` (including `0` for PV-only baseline) to inspect ranked KPIs, Pareto frontier, and 2D heatmap.\n"
                "3. Optionally run Stage 2 (`verify_top_k=1..3`, `dry_run=False`, `confirm=True`) with a loose or zero-template Li-ion `.BTR` to reconcile fast-model estimates against exact `PVsystCLI` simulations.\n"
                "4. Embed the resulting `bess_optimization` dict into `pvsyst_generate_report` to render the interactive heatmap and Pareto chart."
            )


ACTIVE_TOOL_GROUPS = resolve_tool_groups(os.environ.get("PVSYST_TOOL_GROUPS"))
ACTIVE_TOOL_NAMES = tool_names_for_groups(ACTIVE_TOOL_GROUPS)
mcp = MCPServer("pvsyst-cli", instructions=build_server_instructions(ACTIVE_TOOL_NAMES))
register_guidance(mcp, active_tools=ACTIVE_TOOL_NAMES)

_strict_arguments: dict[str, frozenset[str]] = {}
_tool_registry: dict[str, tuple[str, Any, frozenset[str]]] = {}


def make_strict_middleware(strict_map: Mapping[str, frozenset[str]]):
    async def reject_unknown_tool_arguments(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        """Refuse unknown top-level arguments for every registered tool."""
        if ctx.method == "tools/call" and isinstance(ctx.params, Mapping):
            name, arguments = ctx.params.get("name"), ctx.params.get("arguments")
            accepted = strict_map.get(name) if isinstance(name, str) else None
            if accepted is not None and isinstance(arguments, Mapping):
                if unknown := sorted(set(arguments) - accepted, key=str):
                    text = f"Unknown argument(s) for {name}: {', '.join(map(str, unknown))}. Accepted arguments: {', '.join(sorted(accepted))}"
                    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)
        return await call_next(ctx)

    return reject_unknown_tool_arguments


reject_unknown_tool_arguments = make_strict_middleware(_strict_arguments)
mcp.middleware.append(reject_unknown_tool_arguments)

_VERBATIM_ERRORS = (ValueError, FileNotFoundError, FileExistsError, PVsystError)
_REPORTED_ERRORS = (ValueError, OSError, PVsystError)


def _tool_error(exc: Exception) -> ToolError:
    if isinstance(exc, _VERBATIM_ERRORS):
        return ToolError(str(exc))
    return ToolError(f"{type(exc).__name__}: {exc}")


def categorized_tool():
    """Label MCP descriptions without changing established wire-level tool names."""
    def register(func):
        category = TOOL_CATEGORY[func.__name__]
        params = frozenset(inspect.signature(func).parameters)

        @wraps(func)
        def guarded(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except _REPORTED_ERRORS as exc:
                raise _tool_error(exc) from None

        description = f"[{category}] {inspect.getdoc(func)}"
        _tool_registry[func.__name__] = (description, guarded, params)
        if func.__name__ in ACTIVE_TOOL_NAMES:
            _strict_arguments[func.__name__] = params
            mcp.tool(description=description)(guarded)
        return func

    return register


def create_mcp_server(tool_groups: str | None = None) -> MCPServer:
    """Create an isolated MCPServer populated for the requested PVSYST_TOOL_GROUPS spec."""
    groups = resolve_tool_groups(tool_groups)
    names = tool_names_for_groups(groups)
    for mod in import_modules_for_groups(groups):
        import_module(f".{mod}", __package__)
    if missing := sorted(names - set(_tool_registry)):
        raise RuntimeError(f"Unregistered strict entry points: {missing}")
    server = MCPServer("pvsyst-cli", instructions=build_server_instructions(names))
    register_guidance(server, active_tools=names)
    strict_map: dict[str, frozenset[str]] = {}
    server.middleware.append(make_strict_middleware(strict_map))
    for name in sorted(names):
        description, guarded, params = _tool_registry[name]
        strict_map[name] = params
        server.tool(description=description)(guarded)
    return server


def cli() -> PVsystCLI:
    global _cli, _cli_key, _cli_owner
    with _cli_init_lock:
        key = (os.environ.get("PVSYST_CLI"), os.environ.get("PVSYST_WORKSPACE"))
        if _cli is None or (_cli is _cli_owner and _cli_key != key):
            _cli = _cli_owner = None
            _cli_key = None
            _cli = _cli_owner = PVsystCLI(*key)
            _cli_key = key
        return _cli


def components() -> ComponentStore:
    current = cli()
    if configured := os.environ.get("PVSYST_BUILTIN_COMPONENTS"):
        builtin = Path(configured).resolve(strict=True)
        if not builtin.is_dir() or builtin.name != "ComposPV":
            raise ValueError("PVSYST_BUILTIN_COMPONENTS must be a ComposPV directory")
    else:
        data_ro = current.cli.parent / "DataRO"
        candidates = sorted(path / "ComposPV" for path in data_ro.iterdir() if path.is_dir() and (path / "ComposPV").is_dir()) if data_ro.is_dir() else []
        if len(candidates) > 1:
            raise ValueError("Multiple DataRO component libraries; set PVSYST_BUILTIN_COMPONENTS")
        builtin = candidates[0] if candidates else None
    return ComponentStore(current.workspace, builtin, current._lock)


def component_call(method: str, *args, **kwargs) -> dict:
    try:
        return getattr(components(), method)(*args, **kwargs)
    except _REPORTED_ERRORS as exc:
        raise _tool_error(exc) from None


def variant_store() -> VariantStore:
    store = components()
    return VariantStore(store.workspace, store, store.lock)


def variant_call(method: str, *args, **kwargs) -> dict:
    try:
        return getattr(variant_store(), method)(*args, **kwargs)
    except _REPORTED_ERRORS as exc:
        raise _tool_error(exc) from None


def workspace_path(folder: str, filename: str, extension: str) -> Path:
    """Allow a filename only within a named directory of the configured workspace."""
    return workspace_file(cli().workspace, folder, filename, extension)


def optional_path(folder: str, filename: str, extension: str) -> Path | None:
    return workspace_path(folder, filename, extension) if filename else None


def simulation_csv_path(filename: str) -> Path | None:
    """Accept a bare single-run output name while preserving workspace checks."""
    if not filename:
        return None
    if filename not in (".", "..") and not filename.endswith(".") and not Path(filename).suffix:
        filename += ".csv"
    return workspace_path("Results", filename, ".csv")


def result_path(folder: str, csv_name: str) -> Path:
    if folder not in ("Results", "UserHourly"):
        raise ValueError("Results folder must be Results or UserHourly")
    return workspace_path(folder, csv_name, ".csv")

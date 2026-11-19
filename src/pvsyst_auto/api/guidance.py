"""MCP protocol guidance layer: server instructions, workflow prompts, and on-demand reference resources."""
from __future__ import annotations

from pvsyst_auto.native.cli import REPORT_PAGES, VAR_GROUPS

SERVER_INSTRUCTIONS = """PVsyst CLI MCP drives a local Windows PVsyst 8.x installation and workspace.

Quick workflow selection:
1. Check environment first: call `pvsyst_capabilities` and `pvsyst_license_info` before running simulations.
2. Search & select components with `pvsyst_list_components` (supports electrical filters: `min_power_w`, `max_power_w`, `bifacial`, `min_system_voltage_v`, `min_mppt`, `manufacturer`) / `pvsyst_get_component`, or synthesize custom commercial `.PAN`/`.OND` from cited datasheets via `pvsyst_build_sourced_component` (omit `template_name` for zero-template single-diode / Mode-1 synthesis).
3. Weather: call `pvsyst_resolve_location` -> `pvsyst_acquire_weather(provider="open_meteo_era5" | "pvgis_tmy" | "nasa_power", prepare=true)` to fetch and convert `.SIT`/`.MET` in one step, or pass `weather.mode="open_meteo" | "pvgis_tmy" | "nasa_power"` directly in `pvsyst_simulate_design`.
4. Fixed-plane or ground utility PV (mono or bifacial sheds/trackers, finite 3D polygon + obstacles + BaseSlope):
   - Use `pvsyst_simulate_design` with `scope="fixed_unshaded_grid" | "ground_sheds" | "ground_tracker"`, optional `ground_layout` (`model_mode="unlimited" | "3d"`, `site_polygon`, `obstacles`, `base_slope_deg`) and `bifacial`.
   - Pass `electrical_design`/`capacity` or call `pvsyst_suggest_sizing` / `sizing_mode="auto_from_components"`.
5. 3D rooftop house + obstacles + electrical shading (`module_layout`):
   - Call `pvsyst_run_house_task` (omit `source_project`/`source_variant` for zero-template synthesis).
6. Optimization, parametric sweeps, economics & interactive reporting:
   - Sweep parameters in one CLI call with `pvsyst_run_batch_sweep` (`dry_run=true` first).
   - Audit results with `pvsyst_diagnose_results`, auto-remediate on cloned variants via `pvsyst_optimize_variant` (`dry_run=true` first, `confirm=true` bounded loop).
   - Evaluate fixed/TOU tariffs, PV+BESS arbitrage, LCOE, NPV, IRR, and payback via `pvsyst_evaluate_economics`.
   - Generate interactive Plotly HTML engineering reports (single- or multi-scenario, zh/en) via `pvsyst_generate_report`, and bundle deliverables via `pvsyst_export_engineering_delivery`.
7. Safety & verification conventions:
   - Always preview mutating workflows with `dry_run=true` before executing `dry_run=false`.
   - Never overwrite existing projects/outputs; pick fresh filenames on retry.
   - Consult on-demand resources `pvsyst://guide/workflows` and `pvsyst://guide/variables` when choosing SFI variables or multi-step flows.
"""

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

## 3. Existing Project + AC Storage / Load / Cabling
1. `pvsyst_project_handoff(project=..., variant=...)` to inspect equipment, counts, weather, load, and storage
2. Load: `pvsyst_create_native_load_variant` (constant / monthly / daily) or `pvsyst_prepare_load_profile` (hourly CSV in `UserData`)
3. Storage: `pvsyst_create_grid_storage_system` (`self_consumption`, `weak_grid`, `peak_shaving`, `power_shift`)
4. Simulate & read: `pvsyst_build_sfi(variables="grid_storage")` -> `pvsyst_run_simulation` -> `pvsyst_read_results`

## 4. Task Status Meanings
- `ready` / `ready_for_weather`: dry-run passed; nothing written.
- `prepared`: project/weather files written; simulation not run yet.
- `completed` (`native_consumption_verified=true`): PVsystCLI ran and passed output/unit/calendar checks.
- `needs_review`: completed with caveats (e.g., `auto_from_components` sizing assumptions, weather shifts, or physical diagnostic anomalies).
- `blocked` / `failed`: preflight or CLI failed; inspect `issues` and use a new output name on retry.
"""


def _variables_guide_md() -> str:
    lines = [
        "# PVsyst SFI Variable Groups & Report Pages",
        "",
        "Pass any group name below to `pvsyst_build_sfi(filename=..., variables=<group>)`,",
        "or pass a comma-separated list of explicit `vi*` variable identifiers.",
        "",
        "## Built-in Variable Groups (`VAR_GROUPS`)",
        "",
    ]
    for group, variables in VAR_GROUPS.items():
        lines.append(f"- **`{group}`** ({len(variables)} vars): `{', '.join(variables)}`")
    lines.extend([
        "",
        "## Supported PDF Report Pages (`report_pages`)",
        "",
        f"`{', '.join(sorted(REPORT_PAGES))}`",
    ])
    return "\n".join(lines) + "\n"


def register_guidance(server) -> None:
    """Attach standard MCP prompts and read-only reference resources to ``server``."""

    @server.prompt(
        name="design_fixed_or_utility_pv",
        description="Step-by-step workflow to design and simulate a fixed-plane, ground sheds, or single-axis tracker PV plant (mono or bifacial).",
    )
    def design_fixed_or_utility_pv(
        location: str,
        target_dc_kw: str = "1000",
        scope: str = "ground_tracker",
        bifacial: str = "true",
    ) -> str:
        return (
            f"Design and simulate a {target_dc_kw} kWp PV plant at {location} "
            f"(scope={scope}, bifacial={bifacial}) using the PVsyst CLI MCP tools:\n"
            "1. Call `pvsyst_capabilities` and `pvsyst_license_info` to confirm CLI readiness and quota.\n"
            "2. Call `pvsyst_resolve_location` for the target location and `pvsyst_list_components` for available PAN/OND files.\n"
            "   Note: if bifacial=true, verify the loose .PAN declares bifaciality (Flags bit 23 / $00800000).\n"
            "3. Call `pvsyst_suggest_sizing` (or pass `sizing_mode='auto_from_components'` directly in `pvsyst_simulate_design`) "
            "to derive a safe string/MPPT/inverter layout.\n"
            "4. Run `pvsyst_simulate_design(..., dry_run=True)` first to inspect planned files, sizing checks, and weather plan; "
            "once `ready`/`ready_for_weather`, execute with `dry_run=False`.\n"
            "5. Summarize annual E_Grid, PR, specific yield, bifacial rear gain (if enabled), and any diagnostic warnings."
        )

    @server.prompt(
        name="design_rooftop_3d_pv",
        description="Step-by-step workflow to model a 3D house/rooftop PV system with obstacles and module-level shading.",
    )
    def design_rooftop_3d_pv(
        location: str,
        roof_description: str,
        shading_mode: str = "module_layout",
    ) -> str:
        return (
            f"Build and simulate a 3D rooftop PV system at {location} ({roof_description}) "
            f"with shading_mode={shading_mode}:\n"
            "1. Check `pvsyst_capabilities` and `pvsyst_license_info`.\n"
            "2. Prepare workspace weather files: `pvsyst_resolve_location` -> `pvsyst_acquire_weather(dry_run=False)` "
            "-> `pvsyst_prepare_weather(dry_run=False)`.\n"
            "3. List available PAN/OND files with `pvsyst_list_components`.\n"
            "4. Construct planar upward-facing roof meshes (`house.kind='mesh'`), PV table placements (`layout`), "
            "surrounding `obstacles`, and optional `string_layout`.\n"
            "5. Call `pvsyst_run_house_task` without `source_project`/`source_variant` (zero-template synthesis) "
            "with `dry_run=True` first, then `dry_run=False, confirm=True`.\n"
            "6. Report annual E_Grid, PR, shading losses (`GlobShd`/`ShdElec`), and any diagnostics."
        )

    @server.prompt(
        name="design_pv_with_storage",
        description="Step-by-step workflow to add load profiles and AC-coupled battery storage to a PV project and compare strategies.",
    )
    def design_pv_with_storage(
        project: str,
        variant: str = "VC0",
        storage_strategy: str = "peak_shaving",
    ) -> str:
        return (
            f"Add load and AC-coupled battery storage ({storage_strategy}) to {project}/{variant}:\n"
            "1. Inspect the baseline via `pvsyst_project_handoff(project, variant)` and available batteries via "
            "`pvsyst_list_components(component_type='BTR', library='builtin')`.\n"
            "2. Attach a load profile via `pvsyst_create_native_load_variant` (constant/monthly/daily) or "
            "`pvsyst_prepare_load_profile` (hourly CSV in `UserData`).\n"
            "3. Create the storage variant via `pvsyst_create_grid_storage_system` (`dry_run=True` then `dry_run=False`).\n"
            "4. Build a storage SFI with `pvsyst_build_sfi(variables='grid_storage')`, run `pvsyst_run_simulation`, "
            "and compare against baseline using `pvsyst_read_results(compare_to=...)` or `pvsyst_compare_engineering_scenarios`."
        )

    @server.prompt(
        name="diagnose_and_optimize_simulation",
        description="Audit a completed simulation CSV + variant parameters for anomalies (clipping, overvoltage, shading, storage) and propose targeted fixes.",
    )
    def diagnose_and_optimize_simulation(
        project: str,
        variant: str,
        csv_name: str,
    ) -> str:
        return (
            f"Audit simulation results `{csv_name}` for `{project}/{variant}`:\n"
            "1. Read variant state with `pvsyst_get_variant_parameters` or `pvsyst_project_handoff`.\n"
            "2. Summarize key columns with `pvsyst_read_results` (and run `pvsyst_diagnose_results` when available) "
            "to inspect PR, inverter limits (`IL_Vmax`, `IL_Vmin`, `IL_Pmax`, `IL_Imax`), shading (`GlobShd`, `ShdElec`), "
            "and grid/storage losses.\n"
            "3. Map each anomaly back to concrete `.VC0` parameters (e.g., `NModSerie`, DC/AC ratio, pitch/tilt, storage schedule).\n"
            "4. Propose and verify an isolated revised variant (`pvsyst_clone_variant` / `pvsyst_update_variant_parameters` "
            "or `pvsyst_run_batch_sweep`) and compare before/after performance."
        )

    @server.resource(
        "pvsyst://guide/workflows",
        name="pvsyst_workflows_guide",
        title="PVsyst CLI MCP Workflow Playbook",
        description="End-to-end recipes for fixed, utility ground/bifacial, 3D rooftop, and storage simulations.",
        mime_type="text/markdown",
    )
    def workflows_guide() -> str:
        return WORKFLOW_GUIDE_MD

    @server.resource(
        "pvsyst://guide/variables",
        name="pvsyst_variables_guide",
        title="PVsyst SFI Variable Groups & Report Pages",
        description="Available VAR_GROUPS presets for pvsyst_build_sfi and supported PDF report_pages tokens.",
        mime_type="text/markdown",
    )
    def variables_guide() -> str:
        return _variables_guide_md()

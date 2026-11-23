"""MCP protocol guidance layer: server instructions, workflow prompts, and on-demand reference/workspace resources."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from pvsyst_auto.native.cli import REPORT_PAGES, VAR_GROUPS

SERVER_INSTRUCTIONS = """PVsyst CLI MCP drives a local Windows PVsyst 8.x installation and workspace.

Quick workflow selection:
1. Check environment first: call `pvsyst_capabilities` and `pvsyst_license_info` before running simulations.
2. Search & select components with `pvsyst_list_components` (supports electrical filters: `min_power_w`, `max_power_w`, `bifacial`, `min_system_voltage_v`, `min_mppt`, `manufacturer`) / `pvsyst_get_component`, or synthesize custom commercial `.PAN`/`.OND` (single- or 3-voltage) / Li-ion `.BTR` from cited datasheets via `pvsyst_build_sourced_component` (omit `template_name` for zero-template synthesis). Pass `refit_single_diode=true` on `pvsyst_update_component` to refit `.PAN` single-diode parameters when editing STC values.
3. Weather: call `pvsyst_resolve_location` -> `pvsyst_acquire_weather(provider="open_meteo_era5" | "pvgis_tmy" | "nasa_power", prepare=true)` to fetch and convert `.SIT`/`.MET` in one step, or pass `weather.mode="open_meteo" | "pvgis_tmy" | "nasa_power"` directly in `pvsyst_simulate_design`.
4. Fixed-plane, utility ground PV, or Stand-Alone off-grid PV+BESS+Genset:
   - Use `pvsyst_simulate_design` with `scope="fixed_unshaded_grid" | "ground_sheds" | "ground_tracker" | "stand_alone"`.
   - For utility ground PV, pass optional `ground_layout` (`model_mode="unlimited" | "3d"`, `site_polygon`, `obstacles`, `base_slope_deg`, or 3D DEM `terrain` from XYZ JSON/CSV/GeoTIFF), `bifacial`, and MV/HV `transformers`.
   - For off-grid Stand-Alone (`scope="stand_alone"`), pass `standalone` (`.BTR` battery bank, MPPT/direct `.RLT` controller, optional `.GEN` backup generator or inline `genset_spec`, load profile, and diesel-fuel economics).
   - For existing variants, configure multi-stage MV/HV transformers & AC collection cables via `pvsyst_configure_transformers`, convert orientation type in-place (`fixed`/`seasonal`/`sheds`/`tracker_ns`/`tracker_2axis`) via `pvsyst_convert_orientation`, or update spectral correction, random unavailability, and native `TEcoEvaluation` PDF financials via `pvsyst_update_variant_parameters`.
   - Pass `electrical_design`/`capacity` or call `pvsyst_suggest_sizing` / `sizing_mode="auto_from_components"`.
5. 3D rooftop house + obstacles + electrical shading (`module_layout`):
   - Call `pvsyst_run_house_task` (omit `source_project`/`source_variant` for zero-template synthesis).
6. C&I PV+BESS optimization, economics, feasibility studies & interactive reporting:
   - Convert TOU tariffs into PVsyst `PowerShift` 48-slot schedules via `pvsyst_plan_tou_storage_schedule`.
   - Run two-stage hybrid BESS sizing (`bess_kwh x pcs_kw x strategy` + optional Stage 2 PVsystCLI verification) via `pvsyst_optimize_bess_sizing`.
   - Sweep parameters in one CLI call with `pvsyst_run_batch_sweep` (`dry_run=true` first), audit with `pvsyst_diagnose_results`, and auto-remediate via `pvsyst_optimize_variant`.
   - Evaluate fixed, TOU, and two-part demand-charge tariffs, battery replacement, sensitivity tornadoes, LCOE, NPV, IRR, and payback via `pvsyst_evaluate_economics`.
   - Run a complete one-shot feasibility study (synthesis -> simulation/reuse -> diagnostics -> TOU/BESS sizing -> economics -> Plotly report) via `pvsyst_run_feasibility_study` (`dry_run=true` first, then `dry_run=false, confirm=true`).
   - Generate interactive Plotly HTML engineering reports (zh/en, with demand shaving, tornado, and BESS heatmap charts) via `pvsyst_generate_report`, and bundle deliverables via `pvsyst_export_engineering_delivery`.
7. Safety & verification conventions:
   - For mutating tools whose schema declares a `dry_run` parameter (including `pvsyst_generate_report`), always preview with `dry_run=true` before executing `dry_run=false`.
   - Direct writers without a `dry_run` parameter (such as `pvsyst_build_sfi`, `pvsyst_clone_variant`, `pvsyst_create_component`, and `pvsyst_run_simulation`, plus `pvsyst_create_site`/`pvsyst_build_monthly_weather`/`pvsyst_convert_meteo` when `expert` tools are enabled) write immediately; never overwrite existing projects/outputs and always pick fresh output filenames.
   - Consult on-demand resources `pvsyst://guide/workflows`, `pvsyst://guide/variables`, `pvsyst://workspace/projects`, `pvsyst://workspace/components`, and `pvsyst://workspace/results` for live workspace state, SFI variables, and multi-step recipes.
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
        strategy = storage_strategy.strip().lower()
        if strategy == "peak_shaving":
            return (
                f"Add AC-coupled battery storage ({storage_strategy}) to {project}/{variant}:\n"
                "1. Inspect the baseline via `pvsyst_project_handoff(project, variant)` to confirm the source variant "
                "has no self-consumption `pvLoad` block (PVsyst rejects PeakShaving paired with a self-consumption load), "
                "and list batteries via `pvsyst_list_components(component_type='BTR', library='builtin')`.\n"
                "2. Do not attach a load profile and omit `load_manifest_name` when creating `peak_shaving` storage.\n"
                "3. Create the storage variant via `pvsyst_create_grid_storage_system` (`storage={'strategy': 'peak_shaving', ...}`, "
                "omitting `load_manifest_name`; preview with `dry_run=True` then execute with `dry_run=False`).\n"
                "4. Build a storage SFI with `pvsyst_build_sfi(variables='grid_storage')`, run `pvsyst_run_simulation`, "
                "and compare against baseline using `pvsyst_read_results(compare_to=...)` or `pvsyst_compare_engineering_scenarios`."
            )
        if strategy in ("self_consumption", "weak_grid"):
            extra = " and supply a matching `availability` profile" if strategy == "weak_grid" else ""
            return (
                f"Add load and AC-coupled battery storage ({storage_strategy}) to {project}/{variant}:\n"
                "1. Inspect the baseline via `pvsyst_project_handoff(project, variant)` and available batteries via "
                "`pvsyst_list_components(component_type='BTR', library='builtin')`.\n"
                "2. Prepare an hourly load profile via `pvsyst_prepare_load_profile` (hourly CSV in `UserData`) and "
                f"record its `load_manifest_name` (required for `{strategy}`{extra}).\n"
                "3. Create the storage variant via `pvsyst_create_grid_storage_system` passing `load_manifest_name` "
                "(`dry_run=True` then `dry_run=False`).\n"
                "4. Build a storage SFI with `pvsyst_build_sfi(variables='grid_storage')`, run `pvsyst_run_simulation`, "
                "and compare against baseline using `pvsyst_read_results(compare_to=...)` or `pvsyst_compare_engineering_scenarios`."
            )
        return (
            f"Add AC-coupled battery storage ({storage_strategy}) to {project}/{variant}:\n"
            "1. Inspect the baseline via `pvsyst_project_handoff(project, variant)` and available batteries via "
            "`pvsyst_list_components(component_type='BTR', library='builtin')`.\n"
            "2. Load binding depends on strategy: for `self_consumption` or `weak_grid`, prepare an hourly load via "
            "`pvsyst_prepare_load_profile` and pass `load_manifest_name`; for `peak_shaving`, use a source variant "
            "without `pvLoad` and omit `load_manifest_name`; for `power_shift`, a load is optional.\n"
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
            "2. Summarize key columns with `pvsyst_read_results` and run `pvsyst_diagnose_results` "
            "to inspect PR, inverter limits (`IL_Vmax`, `IL_Vmin`, `IL_Pmax`, `IL_Imax`), shading (`GlobShd`, `ShdElec`), "
            "and grid/storage losses.\n"
            "3. Map each anomaly back to concrete `.VC0` parameters (e.g., `NModSerie`, DC/AC ratio, pitch/tilt, storage schedule).\n"
            "4. Propose and verify an isolated revised variant (`pvsyst_optimize_variant`, `pvsyst_clone_variant` / "
            "`pvsyst_update_variant_parameters`, or `pvsyst_run_batch_sweep`) and compare before/after performance."
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

    def _active_workspace() -> Path | None:
        raw = os.environ.get("PVSYST_WORKSPACE", "").strip()
        if not raw:
            return None
        p = Path(raw)
        return p.resolve() if p.is_dir() else None

    @server.resource(
        "pvsyst://workspace/projects",
        name="pvsyst_workspace_projects",
        title="Workspace Projects & Variants",
        description="Live read-only JSON summary of PVsyst projects and variants in the configured PVSYST_WORKSPACE.",
        mime_type="application/json",
    )
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
            prjs.sort()
            for prj in prjs[:200]:
                variants = sorted(variants_by_stem.get(prj.stem.casefold(), ()))
                items.append({"project": prj.name, "variants": variants})
        return json.dumps({"configured": True, "workspace": str(ws), "projects": items}, ensure_ascii=False, indent=2)

    @server.resource(
        "pvsyst://workspace/components",
        name="pvsyst_workspace_components",
        title="Workspace Components Catalog",
        description="Live read-only JSON catalog of loose PAN, OND, BTR, and GEN components with cached electrical metadata.",
        mime_type="application/json",
    )
    def workspace_components_resource() -> str:
        ws = _active_workspace()
        if ws is None:
            return json.dumps({"configured": False, "components": {}}, ensure_ascii=False, indent=2)
        from pvsyst_auto.components.store import ComponentStore

        comp = ComponentStore(ws)
        catalog = {}
        for kind in ("PAN", "OND", "BTR", "GEN"):
            try:
                catalog[kind] = comp.list(kind, "workspace", limit=100).get("items", [])
            except Exception:
                catalog[kind] = []
        return json.dumps({"configured": True, "workspace": str(ws), "components": catalog}, ensure_ascii=False, indent=2)

    @server.resource(
        "pvsyst://workspace/results",
        name="pvsyst_workspace_results",
        title="Workspace Simulation Results & Reports",
        description="Live read-only JSON listing of recent simulation CSV, PDF, HTML reports, and manifests in Results/ and UserHourly/.",
        mime_type="application/json",
    )
    def workspace_results_resource() -> str:
        ws = _active_workspace()
        if ws is None:
            return json.dumps({"configured": False, "files": []}, ensure_ascii=False, indent=2)
        entries = []
        allowed_ext = {".csv", ".pdf", ".html", ".json"}
        for folder in ("Results", "UserHourly"):
            d = ws / folder
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if p.is_file() and p.suffix.lower() in allowed_ext:
                    st = p.stat()
                    entries.append({
                        "folder": folder,
                        "name": p.name,
                        "bytes": st.st_size,
                        "mtime": round(st.st_mtime, 3),
                    })
        entries.sort(key=lambda item: -item["mtime"])
        return json.dumps({"configured": True, "workspace": str(ws), "files": entries[:100]}, ensure_ascii=False, indent=2)

    @server.prompt(
        name="ci_bess_feasibility_study",
        description="End-to-end C&I PV+BESS feasibility workflow with two-part demand tariff, TOU PowerShift scheduling, sizing optimization, sensitivity analysis, and bilingual Plotly HTML report.",
    )
    def ci_bess_feasibility_study(
        location: str,
        target_dc_kw: str = "500",
        language: str = "zh",
    ) -> str:
        return (
            f"Run a complete C&I PV+BESS feasibility study for a {target_dc_kw} kWp plant at {location} (language={language}):\n"
            "1. Inspect workspace readiness with `pvsyst_capabilities`, `pvsyst_license_info`, and resources "
            "`pvsyst://workspace/components` and `pvsyst://workspace/projects`.\n"
            "2. Resolve location (`pvsyst_resolve_location`) and select or synthesize commercial PAN/OND/BTR components "
            "via `pvsyst_list_components` or zero-template `pvsyst_build_sourced_component`.\n"
            "3. Plan two-charge/two-discharge TOU windows with `pvsyst_plan_tou_storage_schedule`.\n"
            "4. Run `pvsyst_run_feasibility_study(spec=..., dry_run=True)` to preview the full execution graph "
            "(weather, PV/BESS simulation, diagnostics, two-part demand + TOU economics, sensitivity tornado, optional BESS sizing sweep, "
            "and interactive Plotly HTML report), then execute with `dry_run=False, confirm=True`.\n"
            "5. Present annual yield, PR, demand peak shaving savings, NPV, IRR, LCOE, payback, sensitivity drivers, and the generated HTML report path."
        )

    @server.prompt(
        name="optimize_bess_sizing",
        description="Two-stage hybrid PV+BESS sizing optimization workflow (fast pure-Python grid sweep + optional Stage 2 PVsystCLI verification).",
    )
    def optimize_bess_sizing_prompt(
        baseline_csv: str,
        installed_dc_kw: str = "500",
        rank_by: str = "npv",
    ) -> str:
        return (
            f"Optimize battery capacity (kWh) and PCS power (kW) for `{baseline_csv}` ({installed_dc_kw} kWp, rank_by={rank_by}):\n"
            "1. Confirm the baseline CSV and available load/TOU/demand-charge tariff parameters.\n"
            "2. Run `pvsyst_optimize_bess_sizing` Stage 1 (`verify_top_k=0`, `dry_run=True`) across candidate `bess_kwh_values` "
            "and `pcs_kw_values` (including `0` for PV-only baseline) to inspect ranked KPIs, Pareto frontier, and 2D heatmap.\n"
            "3. Optionally run Stage 2 (`verify_top_k=1..3`, `dry_run=False`, `confirm=True`) with a loose or zero-template Li-ion `.BTR` "
            "to reconcile fast-model estimates against exact `PVsystCLI` simulations.\n"
            "4. Embed the resulting `bess_optimization` dict into `pvsyst_generate_report` to render the interactive heatmap and Pareto chart."
        )

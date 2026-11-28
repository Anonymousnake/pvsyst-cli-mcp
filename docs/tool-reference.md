# Tool and capability reference

For setup instructions and package layout, see [README.md](../README.md) or [README.zh-CN.md](../README.zh-CN.md).

By default the MCP server exposes **48 core workflow tools across 8 default groups**. `PVSYST_TOOL_GROUPS` is an explicit group list: setting `admin` or `expert` alone replaces the defaults and exposes only that group. Set `PVSYST_TOOL_GROUPS=all` to expose all **75 tools** across 10 groups, or use a complete list such as `setup,projects,components,weather,storage,simulation,results,engineering,expert` to keep the defaults and add `expert`. Every tool description starts with its `[Category]` prefix so clients that flatten the catalog can still tell which workflow a tool belongs to. Restart the server after pulling code or schema updates.

## PVsystCLI version differences

`PVsystCLI.exe` runs simulations on existing projects and variants, converts weather files, and manages licenses. Because editing project parameters, component databases, DC/AC cabling, MV/HV transformers, 3D shading scenes/terrain DEMs, or Stand-Alone systems is not part of the official CLI, this package parses and writes PVsyst's text files directly and **100% headlessly** (PVsyst 8.1.6 rev49278 / 8.1.7 rev49382). Legacy Win32 GUI scene import has been completely removed.

Before running a CLI command, the adapter inspects `PVsystCLI.exe help` on the host machine so unsupported flags fail immediately without burning a simulation run:

| CLI capability | 8.0.6 | 8.1.6 / 8.1.7 |
|---|---|---|
| Simulate existing project/variant, export CSV/PDF, date range, language, report pages | Yes | Yes |
| Convert weather CSV to `.MET`, license commands, export diagnostic logs | Yes | Yes |
| Batch parameter CSV / `.RVT`, shading table recomputation (`-rst`) | No | Yes |
| Create `.SIT` site file, override project site, generate synthetic weather | No | Yes |
| Sub-hourly / hourly step selection | No | Yes |

Two CLI flags are intentionally omitted from the MCP surface: `-cof` (options file indirection, since all options are passed explicitly) and `-odc` (overwrite default CSV, since every run requires an explicit new output filename).

## Tools by workflow group

### Setup & License (`setup`, default: 2)

| Tool | What it does |
|---|---|
| `pvsyst_capabilities` | Inspect installed `PVsystCLI.exe` version and supported CLI commands/flags (optionally for one `command`) |
| `pvsyst_license_info` | Query CLI license status and remaining simulation quota (redacts key and Host ID) |

### Admin & Diagnostics (`admin`, opt-in: 4)

| Tool | What it does |
|---|---|
| `pvsyst_license_activate`, `pvsyst_license_deactivate`, `pvsyst_license_sync` | Activate, deactivate, or sync the CLI license (`confirm=true` required) |
| `pvsyst_export_logs` | Export PVsyst diagnostic logs to a new ZIP archive under `Results` |

### Engineering (`engineering`, default: 8)

| Tool | What it does |
|---|---|
| `pvsyst_suggest_sizing` | Read-only sizing assistant deriving recommended string/MPPT/inverter counts and a drop-in capacity envelope from readable `.PAN`/`.OND` files with explicit assumptions |
| `pvsyst_project_handoff` | Read saved equipment, counts, orientation/circuit relationships, weather, load, and storage settings alongside caller declarations and missing inputs |
| `pvsyst_run_engineering_task` | Apply an isolated revision (fixed angles/losses, `UnlimTracking` parameters, or centralized AC storage with hourly load) in a new project and run a full-year simulation |
| `pvsyst_compare_engineering_scenarios` | Compare 2 or 3 completed simulation runs, separating shared conditions from intended/unintended input diffs, and export JSON/CSV/Markdown comparison reports |
| `pvsyst_evaluate_economics` | Pure-Python PV and PV+BESS financial evaluator computing fixed, TOU, and two-part demand-charge tariff settlement, battery replacement cashflows, composable project finance (`debt`, `tax`, `depreciation`, `inflation`, `salvage`, `working_capital`, DSCR/LLCR, levered equity IRR), sensitivity tornadoes, 25-year degraded cashflows, LCOE, NPV, IRR, payback, and multi-scenario rankings over SFI or batch CSVs |
| `pvsyst_generate_report` | Generate a single- or multi-scenario interactive Plotly HTML engineering report under `Results` (monthly yield/PR, loss waterfall, typical 24h TOU/storage profile, monthly demand peak shaving, 25-year cashflow, sensitivity tornado, optional BESS sizing heatmap/Pareto, diagnostics, zh/en) |
| `pvsyst_export_engineering_delivery` | Bundle a completed engineering run (and optional scenario comparison) with its `.PRJ`/`.VCx`, result CSV/PDF, and Markdown handoff summary into a ZIP archive under `Results` |
| `pvsyst_run_feasibility_study` | One-shot end-to-end PV/PV+BESS feasibility study orchestrator chaining optional zero-template component synthesis, simulation (or `existing_csv` reuse), diagnostics, TOU 48-slot planning, two-stage BESS sizing, economics/sensitivity, and interactive Plotly HTML report generation (`dry_run=true` default; requires `confirm=true`) |

Detailed guides: [engineering delivery, scenario comparison, AC storage, and brief review](workflows-guide.md#5-ac-耦合光储工程pvsyst_run_engineering_task--engineering-storage).

### Projects & Variants (`projects`, default: 12)

| Tool | What it does |
|---|---|
| `pvsyst_list_projects`, `pvsyst_inspect_project` | List workspace projects and inspect `.PRJ`/`.VCx` metadata and SHA-256 hashes |
| `pvsyst_prepare_design_project` | Run component discovery, weather preparation, and project configuration without launching an annual simulation |
| `pvsyst_clone_variant` | Clone a variant into a new `.VCx` (within or across projects) with optional component updates |
| `pvsyst_get_variant_parameters`, `pvsyst_update_variant_parameters` | Read stored variant state and apply guarded parameter updates (angles, series count, MPPT/string counts, losses, horizon, seasonal tilt, temperature, PF, grid limits, P50/P90, system unavailability periods/random/removal, bifacial `pvBiFacial`, spectral correction, or native `TEcoEvaluation` for PDF financial pages) |
| `pvsyst_configure_transformers` | Configure multi-stage MV/HV transformers (MV-only, parallel MV, or cascaded MV+HV), MV/HV AC collection cables (ISO 60228 sections), and night disconnect on a new `.VCx` variant; enforces PVsyst 8.1.7 native `PNomInvAC` basis and preflight bounds (`80%–200%` rating, strictly positive datasheet losses, `<= 1%` iron / `<= 5%` copper, `hv.output_voltage_kv >= mv.output_voltage_kv`) |
| `pvsyst_convert_orientation` | Convert an existing Grid variant's orientation type in-place (`fixed` <-> `seasonal` <-> `sheds` <-> `tracker_ns` <-> `tracker_2axis` <-> `dome`) into a new `.VCx` variant, automatically stripping incompatible 3D shading/result caches |
| `pvsyst_create_tracking_variant` | Create a new variant with updated axis/backtracking/limit parameters from an existing tracking template |
| `pvsyst_create_dc_cabling_variant` | Headlessly set DC wiring resistance (`mOhm`) or STC loss (`%`, using 1-diode `.PAN` STC MPP solver) across subarray and `StringNode` entries and save as a new `.VCx` variant (built-in unreadable modules allow `resistance` mode) |
| `pvsyst_create_ac_cabling_variant` | Configure uniform per-inverter AC cable length (`0.1 m` precision) and section in a new variant |
| `pvsyst_create_shed_geometry_variant` | Create a new variant with updated pitch, tilt, count, width, or electrical shading flag on an existing `Sheds` or `SunShields` template |

Detailed matrix: [feature entry map](native-entry-map.md).

### Components (`components`, default: 5)

| Tool | What it does |
|---|---|
| `pvsyst_list_components`, `pvsyst_get_component` | List (with electrical filters: power range, bifaciality, system voltage, MPPT count, manufacturer, backed by an mtime metadata index cache) and inspect `.PAN`, `.OND`, `.BTR`, `.GEN`, and `.REV` files |
| `pvsyst_create_component` | Create a workspace component from UTF-8 `PVObject_` text |
| `pvsyst_build_sourced_component` | Build a new `.PAN`, `.OND` (single- or 3-voltage), or Li-ion `.BTR` (`LFP`/`NMC`) from cited datasheet values: either derived from a template (`template_name` set) or synthesized zero-template (`template_name` omitted) |
| `pvsyst_update_component` | Edit allowlisted commercial/electrical fields, remarks, or Mode 1 single-/3-voltage OND and BTR curve tables with SHA-256 checks, automatic backup, and optional `refit_single_diode=true` for `.PAN` STC updates |

Detailed guide: [sourced components](workflows-guide.md#2-基于规格书引用创建自定义-pan--ond--btr-组件).

### Sites & Weather (`weather`, default: 4)

| Tool | What it does |
|---|---|
| `pvsyst_resolve_location` | Query Open-Meteo geocoding for a place name and return all candidates with `location_id`, coordinates, ISO country code, and IANA timezone |
| `pvsyst_acquire_weather` | Download full-year hourly or TMY weather from `open_meteo_era5`, `pvgis_tmy` (PVGIS v5.3 TMY), or `nasa_power`, aligned to local winter standard time (`LegalTime`), with optional one-step `prepare=true` chaining into `.SIT`/`.MET` |
| `pvsyst_prepare_weather` | Convert a validated hourly or sub-hourly (`5/10/15/30/60 min`) CSV into new `.SIT`/`.MET` files and record a provenance manifest |
| `pvsyst_review_weather` | Compare an acquired/prepared weather CSV against simulation output and `.MET`/`.SIT` headers |

Detailed guide: [weather preparation](workflows-guide.md#3-站点与标准逐时亚小时气象制备pvsyst_prepare_weather).

### Loads & Storage (`storage`, default: 5)

| Tool | What it does |
|---|---|
| `pvsyst_prepare_load_profile` | Validate a complete 8760/8784-hour load CSV in `UserData` (units, year, fixed standard time) and emit a normalized native CSV + manifest |
| `pvsyst_create_native_load_variant` | Create a PV-only self-consumption variant using a native constant (`kW`), monthly (`12 × kWh`), daily (`24 × kW`), household appliance schedule (`mode="household"`, yearly or seasonal), probability-distribution (`mode="probability"`), or prepared hourly manifest (`mode="hourly"`) load profile |
| `pvsyst_create_grid_storage_variant` | Update SOC thresholds, AC charge/discharge power limits, efficiency, `PowerShift` 48-slot schedules, `PeakShaving` limits/discharge timing, or `allow_grid_export` on an existing storage variant |
| `pvsyst_create_grid_storage_system` | Add workspace or built-in battery hardware and centralized AC-coupled `SelfConsumption`, `WeakGrid` (with explicit or deterministic `mode="generated"` outage schedules), `PeakShaving`, or `PowerShift` storage (plus optional hourly load and grid-availability bindings) to a PV-only grid variant |
| `pvsyst_plan_tou_storage_schedule` | Deterministically convert a TOU tariff schedule into PVsyst `PowerShift` 48-slot half-hourly charge/discharge schedules (1 or 2 cycles/day) and a drop-in `storage_updates` payload |

Detailed guide: [AC storage engineering](workflows-guide.md#5-ac-耦合光储工程pvsyst_run_engineering_task--engineering-storage) and [feature entry map](native-entry-map.md).

### Simulation (`simulation`, default: 8)

| Tool | What it does |
|---|---|
| `pvsyst_build_sfi` | Generate a `.SFI` export variable selection file under `Models` (presets include `all_common`, `shading`, `soiling`, `grid_limit`, `grid_storage`, `bifacial`, `transformers`, `stand_alone`, etc.; supports `time_step="hour" | "subhour" | "day" | "month"`) |
| `pvsyst_run_simulation` | Run a single or batch simulation via `PVsystCLI.exe` with optional date range, `.SFI`, `.RVT`, `time_step="hour" | "subhour"`, report page selection (`-rp`), shading recomputation (`-rst`), and automatic transient-abort retry |
| `pvsyst_run_batch_sweep` | Generate `UserBatch/<name>_BatchParams.CSV` + `_BatchVariables.RVT` from a Cartesian grid or explicit scenario rows (<=100 scenarios; supports tilt, pitch, albedo, soiling, grid limit, and MPPT sweeps), run one `PVsystCLI` batch call, and return extracted curves and ranked scenario results with baseline deltas |
| `pvsyst_run_house_task` | End-to-end 3D house + obstacles workflow: generate `.pvc2`, build headless native `.VCx` (`linear` or `module_layout`), and run a full-year CLI simulation with checkpoint resume |
| `pvsyst_simulate_design` | Unified single-design workflow (`fixed_unshaded_grid`, `ground_sheds`, `ground_tracker`, or `stand_alone` off-grid PV+BESS+Genset) combining component lookup, zero-template synthesis, sizing (`capacity`/`electrical_design`/`auto_from_components`), optional `sub_orientations`, `ground_layout` (3D `site_polygon`/`obstacles`/`base_slope_deg`/3D DEM `terrain`), `bifacial`, MV/HV `transformers`, localized PDF `report_language`, compact preview, weather acquisition/preparation (`open_meteo`, `pvgis_tmy`, `nasa_power`, `csv`, `existing`), annual simulation, automatic diagnostics, and off-grid diesel economics |
| `pvsyst_prepare_sourced_design` | Extract an `electrical_design` envelope from cited datasheet snippets and check whether all required voltage/current/temperature inputs are present |
| `pvsyst_optimize_variant` | Bounded Diagnose -> Clone -> Patch -> Re-simulate loop (`dry_run=true` default previews deterministic fixes and diffs; `confirm=true` iterates up to `max_iterations` 1..5 on cloned variants without mutating baseline) |
| `pvsyst_optimize_bess_sizing` | Two-stage hybrid PV+BESS capacity optimizer: Stage 1 fast pure-Python grid sweep (`bess_kwh × pcs_kw × strategy`) with Pareto frontier & 2D heatmap; optional Stage 2 (`verify_top_k` 1..5, `dry_run=false`, `confirm=true`) runs `PVsystCLI` to reconcile exact KPIs |

Detailed guides: [single-design inputs](workflows-guide.md#1-单方案设计输入与全年流程) and [native house task](workflows-guide.md#4-住宅-3d-场景端到端任务pvsyst_run_house_task).

### Results (`results`, default: 4)

| Tool | What it does |
|---|---|
| `pvsyst_read_results`, `pvsyst_read_rows` | Parse simulation CSV metadata, units, and summary statistics, or page through hourly rows |
| `pvsyst_read_batch_results` | Parse `SIM_*` summary rows from a batch simulation output CSV |
| `pvsyst_diagnose_results` | Run deterministic physics loss-chain and time-series anomaly diagnostics on an SFI result CSV with optional `.VC0` root-cause attribution |

### Expert & Low-Level (`expert`, opt-in: 23)

Enable with `PVSYST_TOOL_GROUPS=expert` or `PVSYST_TOOL_GROUPS=all`:
- **Projects & scene primitives (13)**: `pvsyst_list_variants`, `pvsyst_create_project`, `pvsyst_update_project_parameters`, `pvsyst_update_project_sources`, `pvsyst_get_variant_components`, `pvsyst_update_variant_components`, `pvsyst_validate_variant_structure`, `pvsyst_clone_subarray`, `pvsyst_remove_subarray`, `pvsyst_review_project_inputs`, `pvsyst_create_house_pv_scene`, `pvsyst_read_house_pv_scene`, `pvsyst_import_house_pv_scene`
- **Components (3)**: `pvsyst_validate_component`, `pvsyst_compare_components`, `pvsyst_project_components`
- **Weather primitives (4)**: `pvsyst_inspect_weather_csv`, `pvsyst_build_monthly_weather`, `pvsyst_create_site`, `pvsyst_convert_meteo`
- **Simulation & engineering (3)**: `pvsyst_run_sourced_house_task`, `pvsyst_prepare_block_project`, `pvsyst_review_engineering_brief`

**Batch summary naming.** `pvsyst_read_batch_results` accepts both the indexed and the legacy (unindexed) summary names derived from a batch-parameters file: `<name>BatchParams[_<n>].CSV` yields `<name>BatchResults[_<n>].CSV`, where the optional index `n` is a non-negative integer (including `0`). Malformed names, subdirectory paths and path escapes are rejected, and only `UserBatch` is read. The same shared predicate drives the production `pvsyst_run_simulation` batch completion scan: its pre-run snapshot and post-run "exactly one new summary" check both enumerate `UserBatch` with `is_batch_summary_name`, so an indexed `<prefix>Results_<n>.CSV` produced by the run is detected instead of being missed by a legacy `*Results.CSV` glob. Existing summaries still block the run (no overwrite) and pre-existing summaries are excluded from the "new" set. In the original control the summary is written next to its batch-parameters file (the `.CSV` named by `ParamsFileName`), while ordinary product runs place it under `UserBatch`; the two locations differ.

## Parameter & variant guides

`pvsyst_update_variant_parameters` applies one domain update per transaction (optionally paired with `variant_name`) so flag mutations stay isolated (see [feature entry map](native-entry-map.md) for underlying field mappings):
- Detailed losses: string mismatch, constant/profile auxiliary power, IAM & ageing, PV-system outage periods & removal, spectral correction
- Orientation & horizon: seasonal tilt, far horizon profile
- Energy management: inverter temperature, power factor & PNom, grid injection limit, combined PF + power controls, P50/P90 parameters

Dedicated variant-creation tools handle structural or multi-object changes (AC/headless DC cabling, `pvsyst_configure_transformers`, tracking controls, sheds & sun-shields, `pvsyst_convert_orientation`, and [engineering delivery](workflows-guide.md#6-多方案对比简报预审与工程交付打包)).

## Workspace directories and conventions

For safety, file arguments only accept bare filenames (no path traversal or arbitrary filesystem paths). Every file type maps to its standard PVsyst workspace folder:

| File types | Workspace subfolder |
|---|---|
| `.PRJ`, `.VC0`..`.VCn` | `Projects` |
| `.SFI`, input `.RVT` | `Models` |
| `.PAN`, `.OND`, `.BTR`, `.GEN`, `.REV` | `ComposPV` subdirectories |
| Weather `.CSV`, `.MEF`, `.MET`, monthly weather `.CSV` | `Meteo` |
| `.SIT` | `Sites` |
| `Advanced.dat`, load CSVs, authored `.pvc2` scenes | `UserData` |
| Batch parameter `.CSV`, batch `.RVT`, batch summaries | `UserBatch` |
| Per-scenario batch hourly `.CSV` | `UserHourly` |
| Simulation `.CSV`/`.PDF`, interactive `.HTML` reports, delivery `.ZIP`, run manifests | `Results` |

General conventions across all tools:
- **Optimistic locking & backups**: Mutating tools verify `expected_sha256` (or current file tokens), write a backup snapshot before touching existing files, and refuse to overwrite existing target filenames when creating new variants/projects.
- **Date format**: PVsyst CLI dates use dots (`MM.DD`, `YY.MM.DD`, or `YYYY.MM.DD`, e.g. `2024.01.01`), never hyphens.
- **Strict arguments**: Every tool validates its top-level argument names and types before running; unknown keys return an immediate MCP `ToolError` listing the accepted parameter names.

## Upgrading from v0.1.0

The `v0.1.0` release exposed 82 MCP tools. Today the server registers **48 tools by default** across 8 core groups and **75 tools** with `PVSYST_TOOL_GROUPS=all` (including v0.3.0–v0.6.0 tools `pvsyst_suggest_sizing`, `pvsyst_diagnose_results`, `pvsyst_run_batch_sweep`, `pvsyst_evaluate_economics`, `pvsyst_optimize_variant`, `pvsyst_generate_report`, `pvsyst_plan_tou_storage_schedule`, `pvsyst_optimize_bess_sizing`, `pvsyst_run_feasibility_study`, `pvsyst_configure_transformers`, and `pvsyst_convert_orientation`; `pvsyst_license_info`, `pvsyst_project_handoff`, and `pvsyst_export_engineering_delivery` were promoted back into the default groups in `v0.3.0`). `PVSYST_TOOL_GROUPS` is an explicit allow-list: an explicit value replaces the default groups. To retain all defaults and add `expert`, use `setup,projects,components,weather,storage,simulation,results,engineering,expert`.

The following table lists the 27 tools that remain available in opt-in groups (`admin` and `expert`):

| Tool | Group |
|---|---|
| `pvsyst_export_logs` | `admin` |
| `pvsyst_license_activate` | `admin` |
| `pvsyst_license_deactivate` | `admin` |
| `pvsyst_license_sync` | `admin` |
| `pvsyst_build_monthly_weather` | `expert` |
| `pvsyst_clone_subarray` | `expert` |
| `pvsyst_compare_components` | `expert` |
| `pvsyst_convert_meteo` | `expert` |
| `pvsyst_create_house_pv_scene` | `expert` |
| `pvsyst_create_project` | `expert` |
| `pvsyst_create_site` | `expert` |
| `pvsyst_get_variant_components` | `expert` |
| `pvsyst_import_house_pv_scene` | `expert` |
| `pvsyst_inspect_weather_csv` | `expert` |
| `pvsyst_list_variants` | `expert` |
| `pvsyst_prepare_block_project` | `expert` |
| `pvsyst_project_components` | `expert` |
| `pvsyst_read_house_pv_scene` | `expert` |
| `pvsyst_remove_subarray` | `expert` |
| `pvsyst_review_engineering_brief` | `expert` |
| `pvsyst_review_project_inputs` | `expert` |
| `pvsyst_run_sourced_house_task` | `expert` |
| `pvsyst_update_project_parameters` | `expert` |
| `pvsyst_update_project_sources` | `expert` |
| `pvsyst_update_variant_components` | `expert` |
| `pvsyst_validate_component` | `expert` |
| `pvsyst_validate_variant_structure` | `expert` |

The following 18 legacy entry points were removed. The paths below follow the consolidated APIs documented above; where no current MCP tool provides the old operation, the entry is marked explicitly.

| Legacy tool | Migration path |
|---|---|
| `pvsyst_archive_component` | No direct replacement (removed) |
| `pvsyst_archive_project` | No direct replacement (removed) |
| `pvsyst_archive_variant` | No direct replacement (removed) |
| `pvsyst_backup_component` | `pvsyst_update_component` creates an automatic backup during an update; there is no standalone backup call |
| `pvsyst_clone_component` | `pvsyst_build_sourced_component` for PAN/OND template derivation; generic component cloning was removed |
| `pvsyst_clone_project` | `pvsyst_clone_variant` can clone an individual VC; whole-project cloning has no direct replacement (removed) |
| `pvsyst_copy_component` | `pvsyst_build_sourced_component` for PAN/OND template derivation; generic component copying was removed |
| `pvsyst_create_dome_allocation_variant` | Use `pvsyst_convert_orientation(target_type="dome")` on existing variants or `pvsyst_simulate_design(scope="ground_dome")` for zero-template E-W dome synthesis |
| `pvsyst_create_load_variant` | `pvsyst_create_native_load_variant` with `mode="hourly"` and a prepared load manifest |
| `pvsyst_create_mv_night_disconnect_variant` | `pvsyst_configure_transformers` with `mv={"night_disconnect": ...}` (toggles `NightDisconnect` and bit 29 in-place on an existing MV variant; rebuilds MV settings when other `mv`/`hv` fields are given) |
| `pvsyst_create_peak_shaving_variant` | `pvsyst_create_grid_storage_variant` with the `PeakShaving` limits or discharge-timing updates |
| `pvsyst_open_native_project` | No direct replacement (removed) |
| `pvsyst_restore_archived_component` | No direct replacement (removed) |
| `pvsyst_restore_component` | No direct replacement (removed) |
| `pvsyst_restore_project_archive` | No direct replacement (removed) |
| `pvsyst_restore_project_sources` | No direct replacement (removed) |
| `pvsyst_restore_variant` | No direct replacement (removed) |
| `pvsyst_simulate_template` | `pvsyst_simulate_design` is the documented unified design workflow with template matching and annual simulation |

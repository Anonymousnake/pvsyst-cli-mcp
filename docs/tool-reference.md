# Tool and capability reference

For setup instructions and package layout, see [README.md](../README.md) or [README.zh-CN.md](../README.zh-CN.md).

By default the MCP server exposes **43 core workflow tools across 8 default groups**. `PVSYST_TOOL_GROUPS` is an explicit group list: setting `admin` or `expert` alone replaces the defaults and exposes only that group. Set `PVSYST_TOOL_GROUPS=all` to expose all **72 tools** across 10 groups, or use a complete list such as `setup,projects,components,weather,storage,simulation,results,engineering,expert` to keep the defaults and add `expert`. Every tool description starts with its `[Category]` prefix so clients that flatten the catalog can still tell which workflow a tool belongs to. Restart the server after pulling code or schema updates.

## PVsystCLI version differences

`PVsystCLI.exe` runs simulations on existing projects and variants, converts weather files, and manages licenses. Because editing project parameters, component databases, DC cabling, or 3D shading scenes is not part of the official CLI, this package parses and writes PVsyst's text files directly and headlessly (PVsyst 8.1.6 rev49278 / 8.1.7 rev49382). Legacy GUI automation (`native/scene.py`) is retained unmaintained only as an explicit fallback for `scene_import_method="gui"`.

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
| `pvsyst_capabilities` | Inspect installed `PVsystCLI.exe` version and supported CLI commands/flags |
| `pvsyst_license_info` | Query CLI license status and remaining simulation quota (redacts key and Host ID) |

### Admin & Diagnostics (`admin`, opt-in: 4)

| Tool | What it does |
|---|---|
| `pvsyst_license_activate`, `pvsyst_license_deactivate`, `pvsyst_license_sync` | Activate, deactivate, or sync the CLI license (`confirm=true` required) |
| `pvsyst_export_logs` | Export PVsyst diagnostic logs to a new ZIP archive under `Results` |

### Engineering (`engineering`, default: 7)

| Tool | What it does |
|---|---|
| `pvsyst_suggest_sizing` | Read-only sizing assistant deriving recommended string/MPPT/inverter counts and a drop-in capacity envelope from readable `.PAN`/`.OND` files with explicit assumptions |
| `pvsyst_project_handoff` | Read saved equipment, counts, orientation/circuit relationships, weather, load, and storage settings alongside caller declarations and missing inputs |
| `pvsyst_run_engineering_task` | Apply an isolated revision (fixed angles/losses, `UnlimTracking` parameters, or centralized AC storage with hourly load) in a new project and run a full-year simulation |
| `pvsyst_compare_engineering_scenarios` | Compare 2 or 3 completed simulation runs, separating shared conditions from intended/unintended input diffs, and export JSON/CSV/Markdown comparison reports |
| `pvsyst_evaluate_economics` | Pure-Python PV and PV+BESS financial evaluator computing fixed/TOU tariff settlement, 25-year degraded cashflows, LCOE, NPV, IRR, payback, and multi-scenario rankings over SFI or batch CSVs |
| `pvsyst_generate_report` | Generate a single- or multi-scenario interactive Plotly HTML engineering report under `Results` (monthly yield/PR, loss waterfall, typical 24h TOU/storage profile, 25-year cashflow, diagnostics, zh/en) |
| `pvsyst_export_engineering_delivery` | Bundle a completed engineering run (and optional scenario comparison) with its `.PRJ`/`.VCx`, result CSV/PDF, and Markdown handoff summary into a ZIP archive under `Results` |

Detailed guides: [engineering delivery](engineering-delivery.md), [scenario comparison](engineering-scenarios.md), [AC storage engineering](engineering-storage.md), [block project preparation](block-project-preparation.md), and [brief review](engineering-brief.md).

### Projects & Variants (`projects`, default: 10)

| Tool | What it does |
|---|---|
| `pvsyst_list_projects`, `pvsyst_inspect_project` | List workspace projects and inspect `.PRJ`/`.VCx` metadata and SHA-256 hashes |
| `pvsyst_prepare_design_project` | Run component discovery, weather preparation, and project configuration without launching an annual simulation |
| `pvsyst_clone_variant` | Clone a variant into a new `.VCx` (within or across projects) with optional component updates |
| `pvsyst_get_variant_parameters`, `pvsyst_update_variant_parameters` | Read stored variant state and apply guarded parameter updates (angles, series count, MPPT/string counts, losses, horizon, seasonal tilt, temperature, PF, grid limits, P50/P90, system unavailability periods/removal) |
| `pvsyst_create_tracking_variant` | Create a new variant with updated axis/backtracking/limit parameters from an existing tracking template |
| `pvsyst_create_dc_cabling_variant` | Headlessly set DC wiring resistance (`mOhm`) or STC loss (`%`, using 1-diode `.PAN` STC MPP solver) across subarray and `StringNode` entries and save as a new `.VCx` variant (built-in unreadable modules allow `resistance` mode) |
| `pvsyst_create_ac_cabling_variant` | Configure uniform per-inverter AC cable length (`0.1 m` precision) and section in a new variant |
| `pvsyst_create_shed_geometry_variant` | Create a new variant with updated pitch, tilt, count, width, or electrical shading flag on an existing `Sheds` or `SunShields` template |

Detailed guides: [project sources & transactions](project-source-verification.md), [project titles](project-title.md), [grid topology & capacity editing](grid-capacity-verification.md), [stored state inspection](native-state-inspection.md), and [PV scene bindings](native-scene-bindings.md).

### Components (`components`, default: 5)

| Tool | What it does |
|---|---|
| `pvsyst_list_components`, `pvsyst_get_component` | List and inspect `.PAN`, `.OND`, `.BTR`, `.GEN`, and `.REV` files |
| `pvsyst_create_component` | Create a workspace component from UTF-8 `PVObject_` text |
| `pvsyst_build_sourced_component` | Derive a new `.PAN` or `.OND` from a workspace/builtin template using cited datasheet values and units, while explicitly listing inherited fields and curves |
| `pvsyst_update_component` | Edit allowlisted commercial/electrical fields, remarks, or Mode 1 curve tables with SHA-256 checks and automatic backup |

Detailed guides: [sourced components](sourced-components.md), [commercial fields](commercial-verification.md), [Mode 1 curve tables](curve-table-verification.md), [OND efficiency curves](ond-curve-verification.md), [BTR capacity curves](btr-capacity-verification.md), [GUI PAN format](gui-pan-verification.md), and [backup generator settings](generator-verification.md).

### Sites & Weather (`weather`, default: 4)

| Tool | What it does |
|---|---|
| `pvsyst_resolve_location` | Query Open-Meteo geocoding for a place name and return all candidates with `location_id`, coordinates, ISO country code, and IANA timezone |
| `pvsyst_acquire_weather` | Download a full calendar year of Open-Meteo ERA5 hourly irradiance and temperature, aligned to local winter standard time (`LegalTime`), and save the raw JSON, standard CSV, and manifest |
| `pvsyst_prepare_weather` | Convert a validated hourly CSV into new `.SIT`/`.MET` files and record a provenance manifest |
| `pvsyst_review_weather` | Compare an acquired/prepared weather CSV against simulation output and `.MET`/`.SIT` headers |

Detailed guides: [weather input checks](weather-input-verification.md), [weather preparation](weather-preparation.md), and [weather engineering review](weather-engineering-review.md).

### Loads & Storage (`storage`, default: 4)

| Tool | What it does |
|---|---|
| `pvsyst_prepare_load_profile` | Validate a complete 8760/8784-hour load CSV in `UserData` (units, year, fixed standard time) and emit a normalized native CSV + manifest |
| `pvsyst_create_native_load_variant` | Create a PV-only self-consumption variant using a native constant (`kW`), monthly (`12 × kWh`), daily (`24 × kW`), or prepared hourly manifest (`mode="hourly"`) load profile |
| `pvsyst_create_grid_storage_variant` | Update SOC thresholds, AC charge/discharge power limits, efficiency, `PowerShift` 48-slot schedules, `PeakShaving` limits/discharge timing, or `allow_grid_export` on an existing storage variant |
| `pvsyst_create_grid_storage_system` | Add workspace or built-in battery hardware and centralized AC-coupled `SelfConsumption`, `WeakGrid`, `PeakShaving`, or `PowerShift` storage (plus optional hourly load and grid-availability bindings) to a PV-only grid variant |

Detailed guides: [hourly & native loads](storage-loads.md), [grid storage parameters](grid-storage-parameters.md), [load & storage internals](native-load-storage-controls.md), and [peak shaving](native-peak-shaving.md).

### Simulation (`simulation`, default: 7)

| Tool | What it does |
|---|---|
| `pvsyst_build_sfi` | Generate a `.SFI` export variable selection file under `Models` (presets include `all_common`, `shading`, `soiling`, `grid_limit`, `grid_storage`, `bifacial`, etc.; supports `time_step="hour" | "subhour" | "day" | "month"`) |
| `pvsyst_run_simulation` | Run a single or batch simulation via `PVsystCLI.exe` with optional date range, `.SFI`, `.RVT`, `time_step="hour" | "subhour"`, report page selection (`-rp`), and shading recomputation (`-rst`) |
| `pvsyst_run_batch_sweep` | Generate `UserBatch/<name>_BatchParams.CSV` + `_BatchVariables.RVT` from a Cartesian grid or explicit scenario rows (<=100 scenarios), run one `PVsystCLI` batch call, and return ranked scenario results with baseline deltas |
| `pvsyst_run_house_task` | End-to-end 3D house + obstacles workflow: generate `.pvc2`, build native `.VCx` (`scene_import_method="native"` default, `linear` or `module_layout`), and run a full-year CLI simulation with checkpoint resume |
| `pvsyst_simulate_design` | Unified single-design workflow (`fixed_unshaded_grid`, `ground_sheds`, `ground_tracker`, optional `ground_layout` and `bifacial`) combining component lookup, zero-template synthesis, sizing (`capacity`/`electrical_design`/`auto_from_components`), weather preparation, annual simulation, and automatic diagnostics |
| `pvsyst_prepare_sourced_design` | Extract an `electrical_design` envelope from cited datasheet snippets and check whether all required voltage/current/temperature inputs are present |
| `pvsyst_optimize_variant` | Bounded Diagnose -> Clone -> Patch -> Re-simulate loop (`dry_run=true` default previews deterministic fixes and diffs; `confirm=true` iterates up to `max_iterations` 1..5 on cloned variants without mutating baseline) |

Detailed guides: [automatic simulation](automatic-simulation.md), [single-design inputs](single-design-inputs.md), [capacity sizing](capacity-sizing.md), [tracking template flow](tracking-template-flow.md), [tutorial grid workflow](tutorial-grid-cycle.md), [house PV modeling](house-pv-modeling.md), [native house task](native-house-task.md), and [sourced house task](sourced-house-task.md).

### Results (`results`, default: 4)

| Tool | What it does |
|---|---|
| `pvsyst_read_results`, `pvsyst_read_rows` | Parse simulation CSV metadata, units, and summary statistics, or page through hourly rows |
| `pvsyst_read_batch_results` | Parse `SIM_*` summary rows from a batch simulation output CSV |
| `pvsyst_diagnose_results` | Run deterministic physics loss-chain and time-series anomaly diagnostics on an SFI result CSV with optional `.VC0` root-cause attribution |

### Expert & Low-Level (`expert`, opt-in: 25)

Enable with `PVSYST_TOOL_GROUPS=expert` or `PVSYST_TOOL_GROUPS=all`:
- **Projects & scene primitives (15)**: `pvsyst_list_variants`, `pvsyst_create_project`, `pvsyst_update_project_parameters`, `pvsyst_update_project_sources`, `pvsyst_get_variant_components`, `pvsyst_update_variant_components`, `pvsyst_validate_variant_structure`, `pvsyst_clone_subarray`, `pvsyst_remove_subarray`, `pvsyst_review_project_inputs`, `pvsyst_create_mv_night_disconnect_variant`, `pvsyst_create_dome_allocation_variant`, `pvsyst_create_house_pv_scene`, `pvsyst_read_house_pv_scene`, `pvsyst_import_house_pv_scene`
- **Components (3)**: `pvsyst_validate_component`, `pvsyst_compare_components`, `pvsyst_project_components`
- **Weather primitives (4)**: `pvsyst_inspect_weather_csv`, `pvsyst_build_monthly_weather`, `pvsyst_create_site`, `pvsyst_convert_meteo`
- **Simulation & engineering (3)**: `pvsyst_run_sourced_house_task`, `pvsyst_prepare_block_project`, `pvsyst_review_engineering_brief`

See [result comparison](result-comparison-verification.md) for side-by-side diffs of two result CSVs.

**Batch summary naming.** `pvsyst_read_batch_results` accepts both the indexed and the legacy (unindexed) summary names derived from a batch-parameters file: `<name>BatchParams[_<n>].CSV` yields `<name>BatchResults[_<n>].CSV`, where the optional index `n` is a non-negative integer (including `0`). Malformed names, subdirectory paths and path escapes are rejected, and only `UserBatch` is read. The same shared predicate drives the production `pvsyst_run_simulation` batch completion scan: its pre-run snapshot and post-run "exactly one new summary" check both enumerate `UserBatch` with `is_batch_summary_name`, so an indexed `<prefix>Results_<n>.CSV` produced by the run is detected instead of being missed by a legacy `*Results.CSV` glob. Existing summaries still block the run (no overwrite) and pre-existing summaries are excluded from the "new" set. In the original control the summary is written next to its batch-parameters file (the `.CSV` named by `ParamsFileName`), while ordinary product runs place it under `UserBatch`; the two locations differ.

## Parameter & variant guides

`pvsyst_update_variant_parameters` applies one domain update per transaction (optionally paired with `variant_name`) so flag mutations stay isolated:
- Detailed losses: [loss scalars (Uc, LID, quality, mismatch)](grid-capacity-verification.md), [loss GUI roundtrip notes](loss-gui-roundtrip.md), [soiling](soiling-verification.md), [string mismatch, auxiliary profiles & PV-system outage periods & removal](native-detailed-losses.md), [constant auxiliary power](native-auxiliary-power.md), [IAM & ageing](native-model-inputs.md)
- Orientation & horizon: [seasonal tilt](seasonal-orientation.md), [far horizon profile](horizon-profile.md), [plane & horizon notes](plane-horizon-native.md), [orientation types](native-orientation-controls.md)
- Energy management: [inverter temperature](inverter-temperature.md), [power factor & PNom](power-factor.md), [grid injection limit](grid-power-limit.md), [combined PF + power controls](native-power-controls.md), [system flags & units](native-energy-management.md), [P50/P90 parameters](p50-native-parameters.md)

Dedicated variant-creation tools handle structural or multi-object changes:
- Cabling & transformers: [AC cabling](native-ac-cabling.md), [headless DC cabling](native-dc-cabling.md), [MV night disconnect](native-mv-night.md)
- Complex orientations & 3D scenes: [tracking controls](native-tracking-controls.md), [sheds & sun-shields](native-shed-controls.md), [paired dome allocation](native-dome-allocation.md), [two-roof house delivery](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance), [grouped house scenes](grouped-house-scenes.md), [multi-roof mapping](multiroof-native-map.md), [scene identity notes](native-scene-identity-map.md), [project GUI session](native-project-session.md)

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
| Simulation `.CSV`/`.PDF`, delivery `.ZIP`, run manifests | `Results` |

General conventions across all tools:
- **Optimistic locking & backups**: Mutating tools verify `expected_sha256` (or current file tokens), write a backup snapshot before touching existing files, and refuse to overwrite existing target filenames when creating new variants/projects.
- **Date format**: PVsyst CLI dates use dots (`MM.DD`, `YY.MM.DD`, or `YYYY.MM.DD`, e.g. `2024.01.01`), never hyphens.
- **Strict arguments**: Every tool validates its top-level argument names and types before running; unknown keys return an immediate MCP `ToolError` listing the accepted parameter names.

## Upgrading from v0.1.0

The `v0.1.0` release exposed 82 MCP tools. The current surface registers 34 tools by default and 66 tools with `PVSYST_TOOL_GROUPS=all`. `PVSYST_TOOL_GROUPS` is an explicit allow-list: an explicit value replaces the default groups. To retain all defaults and add `expert`, use `setup,projects,components,weather,storage,simulation,results,engineering,expert`.

The following table lists all 32 tools that remain available but moved out of the default groups:

| Tool | Group |
|---|---|
| `pvsyst_export_logs` | `admin` |
| `pvsyst_license_activate` | `admin` |
| `pvsyst_license_deactivate` | `admin` |
| `pvsyst_license_info` | `admin` |
| `pvsyst_license_sync` | `admin` |
| `pvsyst_build_monthly_weather` | `expert` |
| `pvsyst_clone_subarray` | `expert` |
| `pvsyst_compare_components` | `expert` |
| `pvsyst_convert_meteo` | `expert` |
| `pvsyst_create_dome_allocation_variant` | `expert` |
| `pvsyst_create_house_pv_scene` | `expert` |
| `pvsyst_create_mv_night_disconnect_variant` | `expert` |
| `pvsyst_create_project` | `expert` |
| `pvsyst_create_site` | `expert` |
| `pvsyst_export_engineering_delivery` | `expert` |
| `pvsyst_get_variant_components` | `expert` |
| `pvsyst_import_house_pv_scene` | `expert` |
| `pvsyst_inspect_weather_csv` | `expert` |
| `pvsyst_list_variants` | `expert` |
| `pvsyst_prepare_block_project` | `expert` |
| `pvsyst_project_components` | `expert` |
| `pvsyst_project_handoff` | `expert` |
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

The following 16 entry points were removed. The paths below follow the consolidated APIs documented above; where no current MCP tool provides the old operation, the entry is marked explicitly.

| `v0.1.0` tool | Migration path |
|---|---|
| `pvsyst_archive_component` | No direct replacement (removed) |
| `pvsyst_archive_project` | No direct replacement (removed) |
| `pvsyst_archive_variant` | No direct replacement (removed) |
| `pvsyst_backup_component` | `pvsyst_update_component` creates an automatic backup during an update; there is no standalone backup call |
| `pvsyst_clone_component` | `pvsyst_build_sourced_component` for PAN/OND template derivation; generic component cloning was removed |
| `pvsyst_clone_project` | `pvsyst_clone_variant` can clone an individual VC; whole-project cloning has no direct replacement (removed) |
| `pvsyst_copy_component` | `pvsyst_build_sourced_component` for PAN/OND template derivation; generic component copying was removed |
| `pvsyst_create_load_variant` | `pvsyst_create_native_load_variant` with `mode="hourly"` and a prepared load manifest |
| `pvsyst_create_peak_shaving_variant` | `pvsyst_create_grid_storage_variant` with the `PeakShaving` limits or discharge-timing updates |
| `pvsyst_open_native_project` | No direct replacement (removed) |
| `pvsyst_restore_archived_component` | No direct replacement (removed) |
| `pvsyst_restore_component` | No direct replacement (removed) |
| `pvsyst_restore_project_archive` | No direct replacement (removed) |
| `pvsyst_restore_project_sources` | No direct replacement (removed) |
| `pvsyst_restore_variant` | No direct replacement (removed) |
| `pvsyst_simulate_template` | `pvsyst_simulate_design` is the documented unified design workflow with template matching and annual simulation |

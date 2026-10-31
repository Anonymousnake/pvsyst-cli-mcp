# Tool and capability reference

For setup instructions and package layout, see [README.md](../README.md) or [README.zh-CN.md](../README.zh-CN.md).

By default the MCP server exposes **82 tools across 8 groups**. You can restrict a server process to specific groups via the `PVSYST_TOOL_GROUPS` environment variable (for example `setup,engineering,results`). Every tool description starts with its `[Category]` prefix so clients that flatten the catalog can still tell which workflow a tool belongs to. Restart the server after pulling code or schema updates.

## PVsystCLI version differences

`PVsystCLI.exe` runs simulations on existing projects and variants, converts weather files, and manages licenses. Because editing project parameters, component databases, or 3D shading scenes is not part of the official CLI, this package handles those edits by parsing and writing PVsyst's text files directly (or via targeted Win32 GUI automation on PVsyst 8.1.6 rev49278 Chinese at 96 DPI for 3D scene imports and DC cable calculations).

Before running a CLI command, the adapter inspects `PVsystCLI.exe help` on the host machine so unsupported flags fail immediately without burning a simulation run:

| CLI capability | 8.0.6 | 8.1.6 |
|---|---|---|
| Simulate existing project/variant, export CSV/PDF, date range, language, report pages | Yes | Yes |
| Convert weather CSV to `.MET`, license commands, export diagnostic logs | Yes | Yes |
| Batch parameter CSV / `.RVT`, shading table recomputation (`-rs`) | No | Yes |
| Create `.SIT` site file, override project site, generate synthetic weather | No | Yes |
| Sub-hourly / hourly step selection | No | Yes |

Two CLI flags are intentionally omitted from the MCP surface: `-cof` (options file indirection, since all options are passed explicitly) and `-odc` (overwrite default CSV, since every run requires an explicit new output filename).

## Tools by workflow group

### Setup & License (6)

| Tool | What it does |
|---|---|
| `pvsyst_capabilities` | Inspect installed `PVsystCLI.exe` version and supported CLI commands/flags |
| `pvsyst_license_info` | Query CLI license status and remaining simulation quota (redacts key and Host ID) |
| `pvsyst_license_activate`, `pvsyst_license_deactivate`, `pvsyst_license_sync` | Activate, deactivate, or sync the CLI license (`confirm=true` required) |
| `pvsyst_export_logs` | Export PVsyst diagnostic logs to a new ZIP archive under `Results` |

### Engineering (6)

| Tool | What it does |
|---|---|
| `pvsyst_project_handoff` | Read an existing variant's equipment, counts, electrical/orientation links, weather, load, and storage; separate caller declarations from template-inherited values and list missing inputs |
| `pvsyst_prepare_block_project` | Assemble a new `.PRJ`/`.VCx` by repeating complete fixed subarray blocks from a template project by integer counts |
| `pvsyst_review_engineering_brief` | Check structured feasibility figures (module/inverter counts, strings, block schedules) for internal consistency and missing inputs before any `.PRJ` exists |
| `pvsyst_run_engineering_task` | Apply an isolated revision (fixed angles/losses, `UnlimTracking` parameters, or centralized AC storage with hourly load) in a new project and run a full-year simulation |
| `pvsyst_compare_engineering_scenarios` | Compare 2 or 3 completed simulation runs, separating shared conditions from intended/unintended input diffs, and export JSON/CSV/Markdown comparison reports |
| `pvsyst_export_engineering_delivery` | Bundle a completed engineering run (or multi-scenario comparison) with its `.PRJ`/`.VCx`, result CSV/PDF, and readable Markdown handoff notes into a ZIP under `Results` |

Detailed guides: [engineering delivery](engineering-delivery.md), [scenario comparison](engineering-scenarios.md), [AC storage engineering](engineering-storage.md), [block project preparation](block-project-preparation.md), and [brief review](engineering-brief.md).

### Projects & Variants (32)

| Tool | What it does |
|---|---|
| `pvsyst_list_projects`, `pvsyst_list_variants`, `pvsyst_inspect_project` | List workspace projects/variants and inspect file metadata and SHA-256 hashes |
| `pvsyst_create_project`, `pvsyst_clone_project`, `pvsyst_clone_variant` | Create a project from full text or clone projects/variants across the workspace |
| `pvsyst_update_project_parameters` | Update a project's root display title (`Comment`) and optionally add a UTF-8 BOM |
| `pvsyst_update_project_sources`, `pvsyst_restore_project_sources` | Bind a `.SIT`/`.MET` pair across a project and all its variants, or roll back from a saved snapshot |
| `pvsyst_get_variant_components`, `pvsyst_update_variant_components` | Read or swap `.PAN`, `.OND`, `.BTR`, and `.GEN` references inside a variant |
| `pvsyst_get_variant_parameters`, `pvsyst_update_variant_parameters` | Read stored variant state and apply guarded parameter updates (angles, series count, MPPT/string counts, losses, horizon, seasonal tilt, temperature, PF, grid limits, P50/P90) |
| `pvsyst_create_tracking_variant` | Create a new variant with updated axis/backtracking/limit parameters from an existing tracking template |
| `pvsyst_create_native_dc_cabling_variant` | Set DC wiring resistance (`mOhm`) or STC loss (`%`) via the open PVsyst 8.1.6 GUI, trigger native recalculation, and save as a new variant |
| `pvsyst_create_ac_cabling_variant` | Configure uniform per-inverter AC cable length (`0.1 m` precision) and section in a new variant |
| `pvsyst_create_mv_night_disconnect_variant` | Toggle the `NightDisconnect` switch and main-array bit 29 on a single-MV transformer template |
| `pvsyst_create_shed_geometry_variant` | Create a new variant with updated pitch, tilt, count, width, or electrical shading flag on an existing `Sheds` or `SunShields` template |
| `pvsyst_create_dome_allocation_variant` | Rebalance string counts between the two faces of an existing paired `Dome` template |
| `pvsyst_review_project_inputs` | Compare a prepared design manifest against the actual values saved in a `.PRJ`/`.VCx` |
| `pvsyst_prepare_design_project` | Run component discovery, weather preparation, and project configuration without launching an annual simulation |
| `pvsyst_open_native_project` | Cold-launch PVsyst 8.1.6 GUI and load the target project when no conflicting session is active |
| `pvsyst_create_house_pv_scene` | Serialize named house meshes and rooftop PV regions into a standard PVCollada 2.0 (`.pvc2`) file |
| `pvsyst_read_house_pv_scene` | Parse a v1/v2 `.pvc2` XML file back into structured house and PV layout arguments |
| `pvsyst_import_house_pv_scene` | Import a `.pvc2` scene into an already open PVsyst 8.1.6 project via GUI, bind orientations, check validity/shading tables, and save as a new variant |
| `pvsyst_validate_variant_structure`, `pvsyst_clone_subarray`, `pvsyst_remove_subarray` | Check subarray/circuit/MPPT consistency, or clone and remove subarrays and fixed planes |
| `pvsyst_restore_variant` | Roll back a variant file to a previously recorded backup snapshot |
| `pvsyst_archive_project`, `pvsyst_archive_variant`, `pvsyst_restore_project_archive` | Archive projects/variants out of the active workspace or restore them |

Detailed guides: [project sources & transactions](project-source-verification.md), [project titles](project-title.md), [grid topology & capacity editing](grid-capacity-verification.md), [stored state inspection](native-state-inspection.md), and [PV scene bindings](native-scene-bindings.md).

### Components (14)

| Tool | What it does |
|---|---|
| `pvsyst_list_components`, `pvsyst_get_component`, `pvsyst_validate_component` | List, inspect, and statically validate `.PAN`, `.OND`, `.BTR`, and `.GEN` files |
| `pvsyst_create_component`, `pvsyst_copy_component`, `pvsyst_clone_component` | Create a component from full text, copy from the built-in library, or clone an existing file |
| `pvsyst_build_sourced_component` | Derive a new `.PAN` or `.OND` from a workspace template using cited datasheet values and units, while explicitly listing inherited fields and curves |
| `pvsyst_update_component`, `pvsyst_backup_component`, `pvsyst_restore_component` | Edit allowlisted commercial/electrical fields, remarks, or Mode 1 curve tables with SHA-256 checks and backup/restore |
| `pvsyst_archive_component`, `pvsyst_restore_archived_component` | Archive an unreferenced component file or restore it from archive |
| `pvsyst_compare_components`, `pvsyst_project_components` | Diff two component files or list all components referenced by a project |

Detailed guides: [sourced components](sourced-components.md), [commercial fields](commercial-verification.md), [Mode 1 curve tables](curve-table-verification.md), [OND efficiency curves](ond-curve-verification.md), [BTR capacity curves](btr-capacity-verification.md), [GUI PAN format](gui-pan-verification.md), and [backup generator settings](generator-verification.md).

### Sites & Weather (8)

| Tool | What it does |
|---|---|
| `pvsyst_build_monthly_weather`, `pvsyst_create_site` | Build a monthly weather CSV and create a `.SIT` site file (requires CLI 8.1+) |
| `pvsyst_convert_meteo` | Run `PVsystCLI.exe` to convert a weather CSV + `.MEF` + `.SIT` into a binary `.MET` |
| `pvsyst_inspect_weather_csv` | Validate a PVsyst standard hourly weather CSV (headers, units, full-year calendar, leap day, and radiation bounds) |
| `pvsyst_prepare_weather` | Convert a validated hourly CSV into new `.SIT`/`.MET` files and record a provenance manifest |
| `pvsyst_resolve_location` | Query Open-Meteo geocoding for a place name and return all candidates with `location_id`, coordinates, ISO country code, and IANA timezone |
| `pvsyst_acquire_weather` | Download a full calendar year of Open-Meteo ERA5 hourly irradiance and temperature, aligned to local winter standard time (`LegalTime`), and save the raw JSON, standard CSV, and manifest |
| `pvsyst_review_weather` | Compare an acquired/prepared weather CSV against simulation output and `.MET`/`.SIT` headers |

Detailed guides: [weather input checks](weather-input-verification.md), [weather preparation](weather-preparation.md), and [weather engineering review](weather-engineering-review.md).

### Loads & Storage (6)

| Tool | What it does |
|---|---|
| `pvsyst_prepare_load_profile` | Validate a complete 8760/8784-hour load CSV in `UserData` (units, year, fixed standard time) and emit a normalized native CSV + manifest |
| `pvsyst_create_load_variant` | Bind a prepared hourly load into a new variant by encoding the `Energy/MJ` signed-base62 hourly cache inside `pvLoad` |
| `pvsyst_create_native_load_variant` | Create a PV-only self-consumption variant using a native constant (`kW`), monthly (`12 × kWh`), or daily (`24 × kW`) load profile |
| `pvsyst_create_peak_shaving_variant` | Create a new variant with updated discharge timing (`AfterSunset` or hour window) from an existing `PeakShaving` / `PVToGridMin` template |
| `pvsyst_create_grid_storage_variant` | Update SOC thresholds, AC charge/discharge power limits, efficiency, or `allow_grid_export` on an existing storage variant |
| `pvsyst_create_grid_storage_system` | Add battery hardware and centralized AC-coupled `SelfConsumption` or `WeakGrid` storage (plus hourly load and grid-availability bindings) to a PV-only grid variant |

Detailed guides: [hourly & native loads](storage-loads.md), [grid storage parameters](grid-storage-parameters.md), [load & storage internals](native-load-storage-controls.md), and [peak shaving](native-peak-shaving.md).

### Simulation (7)

| Tool | What it does |
|---|---|
| `pvsyst_build_sfi` | Generate a `.SFI` hourly variable selection file under `Models` (presets include `default`, `soiling`, `grid_limit`, `grid_storage`, etc.) |
| `pvsyst_run_simulation` | Run a single or batch simulation via `PVsystCLI.exe` with optional date range, `.SFI`, `.RVT`, report page selection (`-rp`), and shading recomputation (`-rs`) |
| `pvsyst_simulate_template` | Copy a fixed-plane or `UnlimTracking` template, apply component/weather/angle/capacity edits, run a full year, and write a rollback manifest |
| `pvsyst_run_sourced_house_task` | Bind cited datasheet tokens into a design draft, prepare the source project, and hand off to the 3D house simulation pipeline |
| `pvsyst_run_house_task` | End-to-end 3D house workflow: generate `.pvc2`, open PVsyst GUI, import scene, bind orientations, save new variant, close GUI, and run a full-year CLI simulation with checkpoint resume |
| `pvsyst_simulate_design` | Unified single-design workflow combining component lookup, template matching, integer capacity sizing (or declared electrical envelope), weather acquisition/preparation, and annual simulation |
| `pvsyst_prepare_sourced_design` | Extract an `electrical_design` envelope from cited datasheet snippets and check whether all required voltage/current/temperature inputs are present |

Detailed guides: [automatic simulation](automatic-simulation.md), [single-design inputs](single-design-inputs.md), [capacity sizing](capacity-sizing.md), [tracking template flow](tracking-template-flow.md), [tutorial grid workflow](tutorial-grid-cycle.md), [house PV modeling](house-pv-modeling.md), [native house task](native-house-task.md), and [sourced house task](sourced-house-task.md).

### Results (3)

| Tool | What it does |
|---|---|
| `pvsyst_read_results`, `pvsyst_read_rows` | Parse simulation CSV metadata, units, and summary statistics, or page through hourly rows |
| `pvsyst_read_batch_results` | Parse `SIM_*` summary rows from a batch simulation output CSV |

See [result comparison](result-comparison-verification.md) for side-by-side diffs of two result CSVs.

**Batch summary naming.** `pvsyst_read_batch_results` accepts both the indexed and the legacy (unindexed) summary names derived from a batch-parameters file: `<name>BatchParams[_<n>].CSV` yields `<name>BatchResults[_<n>].CSV`, where the optional index `n` is a non-negative integer (including `0`). Malformed names, subdirectory paths and path escapes are rejected, and only `UserBatch` is read. In the original control the summary is written next to its batch-parameters file (the `.CSV` named by `ParamsFileName`), while ordinary product runs place it under `UserBatch`; the two locations differ. This tool only parses an existing summary — GUI-free / headless batch production is not verified here.

## Parameter & variant guides

`pvsyst_update_variant_parameters` applies one domain update per transaction (optionally paired with `variant_name`) so flag mutations stay isolated:
- Detailed losses: [loss scalars (Uc, LID, quality, mismatch)](grid-capacity-verification.md), [loss GUI roundtrip notes](loss-gui-roundtrip.md), [soiling](soiling-verification.md), [string mismatch, auxiliary profiles & PV-system outage removal](native-detailed-losses.md), [constant auxiliary power](native-auxiliary-power.md), [IAM & ageing](native-model-inputs.md)
- Orientation & horizon: [seasonal tilt](seasonal-orientation.md), [far horizon profile](horizon-profile.md), [plane & horizon notes](plane-horizon-native.md), [orientation types](native-orientation-controls.md)
- Energy management: [inverter temperature](inverter-temperature.md), [power factor & PNom](power-factor.md), [grid injection limit](grid-power-limit.md), [combined PF + power controls](native-power-controls.md), [system flags & units](native-energy-management.md), [P50/P90 parameters](p50-native-parameters.md)

Dedicated variant-creation tools handle structural or multi-object changes:
- Cabling & transformers: [AC cabling](native-ac-cabling.md), [DC cabling GUI bridge](native-dc-cabling.md), [MV night disconnect](native-mv-night.md)
- Complex orientations & 3D scenes: [tracking controls](native-tracking-controls.md), [sheds & sun-shields](native-shed-controls.md), [paired dome allocation](native-dome-allocation.md), [two-roof house delivery](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance), [grouped house scenes](grouped-house-scenes.md), [multi-roof mapping](multiroof-native-map.md), [scene identity notes](native-scene-identity-map.md), [project GUI session](native-project-session.md)

## Workspace directories and conventions

For safety, file arguments only accept bare filenames (no path traversal or arbitrary filesystem paths). Every file type maps to its standard PVsyst workspace folder:

| File types | Workspace subfolder |
|---|---|
| `.PRJ`, `.VC0`..`.VCn` | `Projects` |
| `.SFI`, input `.RVT` | `Models` |
| `.PAN`, `.OND`, `.BTR`, `.GEN` | `ComposPV` subdirectories |
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

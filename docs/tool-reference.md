# Tool and capability reference

See [installation](../README.md) or[简体中文](../README.zh-CN.md). The default server exposes 82 tools in eight groups. Optional PVSYST_TOOL_GROUPS selection changes only that server process; for example, setup, engineering, results selects those workflows. Each tool description starts with its category. Restart a running server after code/schema changes.

## Native scope

PVsystCLI runs existing projects/variants. The adapter checks the installed executable's own help before using an option; newer online documentation does not add missing capabilities. The bounded Chinese8.1.6rev49278/DPI96 GUI bridges are separate native entries, not official CLI imports or general desktop automation.

| Feature |8.0.6|8.1.6|
|---|---|---|
| Existing project, CSV/PDF, date range, language, report pages |Yes|Yes|
| Weather conversion, license commands, logs |Yes|Yes|
| Batch params/RVT, shading recomputation |No|Yes|
| Site creation/override, synthetic weather |No|Yes|
| Hour/subhour option |No|Yes|

Options-file indirection(-cof) and default-CSV overwrite(-odc) are omitted: tools expose named options and require new output names. A site file is not a complete design. PVsyst performs physical, shading, probability and storage calculations; supported field editing does not certify hardware or source data.

## Tools by workflow

### Setup & License (6)

| Tools | Purpose |
|---|---|
| `pvsyst_capabilities` | Installed version and command/option support |
| `pvsyst_license_info` | License/quota without key or Host ID |
| `pvsyst_license_activate`, `pvsyst_license_deactivate`, `pvsyst_license_sync` | Explicitly confirmed license changes |
| `pvsyst_export_logs` | New diagnostic ZIP under Results |

### Engineering (6)

| Tools | Purpose |
|---|---|
| `pvsyst_project_handoff` | Read saved inputs and declarations, missing basis and follow-up actions |
| `pvsyst_prepare_block_project` | Prepare new PRJ/VC from explicit counts of complete supported blocks; no simulation |
| `pvsyst_review_engineering_brief` | Review caller-structured cited quantities before a PRJ exists |
| `pvsyst_run_engineering_task` | Isolated fixed/UnlimTracking or existing AC-storage revision and native annual run |
| `pvsyst_compare_engineering_scenarios` | Compare two/three existing native results with input/review separation |
| `pvsyst_export_engineering_delivery` | Export a bound selected run or scenario set as one new ZIP |

See [engineering delivery](engineering-delivery.md),[scenario comparison](engineering-scenarios.md),[AC storage](engineering-storage.md),[block preparation](block-project-preparation.md) and[brief review](engineering-brief.md). Preparation, ZIP creation and successful simulation do not clear engineering review.

### Projects & Variants (32)

| Tools | Purpose |
|---|---|
| `pvsyst_list_projects`, `pvsyst_list_variants`, `pvsyst_inspect_project` | Workspace inventory and project file identities |
| `pvsyst_create_project`, `pvsyst_clone_project`, `pvsyst_clone_variant` | Complete-text creation or guarded copying within/across projects |
| `pvsyst_update_project_parameters` | Existing display title and explicit UTF-8 BOM transaction |
| `pvsyst_update_project_sources`, `pvsyst_restore_project_sources` | Whole-project SIT/MET binding and complete snapshot recovery |
| `pvsyst_get_variant_components`, `pvsyst_update_variant_components` | Scoped PAN/OND/BTR/GEN references |
| `pvsyst_get_variant_parameters`, `pvsyst_update_variant_parameters` | Bounded parameter transactions and stored-state inspection |
| `pvsyst_create_tracking_variant` | New VC from an existing supported tracking record |
| `pvsyst_create_native_dc_cabling_variant` | Confirmed native GUI DC setting, recalculation and SaveAs |
| `pvsyst_create_ac_cabling_variant` | Uniform per-inverter native AC cable configuration |
| `pvsyst_create_mv_night_disconnect_variant` | Supported single-MV night switch/dependency bit |
| `pvsyst_create_shed_geometry_variant` | New Sheds/SunShields VC without type conversion |
| `pvsyst_create_dome_allocation_variant` | Rebalance an existing valid paired Dome template |
| `pvsyst_review_project_inputs` | Read-only comparison with a completed preparation baseline |
| `pvsyst_prepare_design_project` | Guarded source preparation without SFI or annual execution |
| `pvsyst_open_native_project` | Confirmed owned cold launch; existing sessions remain read-only |
| `pvsyst_create_house_pv_scene` | Explicit named house meshes/PV regions into a new standard scene |
| `pvsyst_read_house_pv_scene` | Strict version1/2 XML reconstruction into supported arguments |
| `pvsyst_import_house_pv_scene` | Confirmed native import, association, validity/table and SaveAs |
| `pvsyst_validate_variant_structure`, `pvsyst_clone_subarray`, `pvsyst_remove_subarray` | Scoped reference/count checks and bounded branch lifecycle |
| `pvsyst_restore_variant` | Current-token protected variant snapshot recovery |
| `pvsyst_archive_project`, `pvsyst_archive_variant`, `pvsyst_restore_project_archive` | Confirmed archive/recovery of PRJ/VC members |

Read[project transactions](project-source-verification.md),[titles](project-title.md) and[topology editing](grid-capacity-verification.md) for strict selectors, counts, MPPT, ownership, geometry and recovery. A successful structure check is not electrical sizing. Raw[stored native state](native-state-inspection.md) or[PV table bindings](native-scene-bindings.md) do not expand writers or certify source geometry.

### Components (14)

| Tools | Purpose |
|---|---|
| `pvsyst_list_components`, `pvsyst_get_component`, `pvsyst_validate_component` | PAN/OND/BTR/GEN inventory, inspection and static checks |
| `pvsyst_create_component`, `pvsyst_copy_component`, `pvsyst_clone_component` | Complete-content creation, import or cloning |
| `pvsyst_build_sourced_component` | Explicit template-derived PAN/OND with exact citations/inherited fields and curves |
| `pvsyst_update_component`, `pvsyst_backup_component`, `pvsyst_restore_component` | Allowlisted edit, backup and recovery |
| `pvsyst_archive_component`, `pvsyst_restore_archived_component` | Archive an unreferenced component or restore it |
| `pvsyst_compare_components`, `pvsyst_project_components` | File comparison and project reference lookup |

See [sourced components](sourced-components.md),[native component behavior](commercial-verification.md),[curve tables](curve-table-verification.md),[OND curves](ond-curve-verification.md),[battery capacity](btr-capacity-verification.md) and[generator references](generator-verification.md). Files, curves and static checks are not a model fit or manufacturer certification. Template-derived creation discloses inheritance, requires acknowledgement and retains needs_review.

### Sites & Weather (8)

| Tools | Purpose |
|---|---|
| `pvsyst_build_monthly_weather`, `pvsyst_create_site` | Monthly CSV and native SIT creation |
| `pvsyst_convert_meteo` | Native CSV+MEF+SIT conversion to MET |
| `pvsyst_inspect_weather_csv` | Standard-hourly metadata, units, calendar and observed summaries |
| `pvsyst_prepare_weather` | New normalized CSV/MEF/SIT/MET and source manifest |
| `pvsyst_resolve_location` | All geocoding candidates; no automatic first selection |
| `pvsyst_acquire_weather` | Plan/download one public Open-Meteo ERA5 year and its raw/source files |
| `pvsyst_review_weather` | Read-only provider/CSV/native-hourly and MET/SIT declaration review |

[Weather input](weather-input-verification.md) and[preparation](weather-preparation.md) distinguish calendar checks, LegalTime binding, native conversion and review. Acquisition requires one explicit location ID or complete coordinate/site/country/IANA-zone inputs, not both. It preserves requested site metadata, discloses provider grid/elevation and endpoint_mean temperature approximation, uses a fixed whole-hour standard offset and does not fill unpublished data. Half-hour offsets, changing standard offsets and ambiguous negative-DST winter conventions stop rather than being rebinned. Provider completion limits actual available years.

[Weather engineering review](weather-engineering-review.md) compares current values and declarations without writing, running native software or claiming effective solar-position/climate certification. Source binding checks declared text/tokens, not authenticity of the original PDF.

### Loads & Storage (6)

| Tools | Purpose |
|---|---|
| `pvsyst_prepare_load_profile` | Complete nonnegative 8760/8784-hour load with explicit units/year/standard time |
| `pvsyst_create_load_variant` | New VC with actual normalized-hourly cache binding |
| `pvsyst_create_native_load_variant` | Native constant kW, monthly kWh or 24-hour kW definition |
| `pvsyst_create_peak_shaving_variant` | Existing PeakShaving/PVToGridMin profile and supported timing |
| `pvsyst_create_grid_storage_variant` | Preserve supported source strategy while changing explicit storage scalars |
| `pvsyst_create_grid_storage_system` | Caller-declared native AC-coupled SelfConsumption/WeakGrid and battery hardware |

See [loads](storage-loads.md),[storage parameters](grid-storage-parameters.md),[native strategy dependencies](native-load-storage-controls.md) and[peak shaving](native-peak-shaving.md). Binding replaces the actual native cache, not only a CSV filename. WeakGrid requires fresh complete 0/1 availability with an outage, matching year and standard offset. SOC reserve, hard minimum and native outages remain distinct.

Optional strict allow_grid_export changes only the native injection bit; it is not grid charging or a numeric kW limit. Pack/BMS sizing, hybrid DC-port construction, probability/household generation and arbitrary charging schedules are not supplied by these AC tools. Preserve native precision/residuals and use complete grid_storage flows, including CL_Over; battery-side power is not directly an AC rating.

### Simulation (7)

| Tools | Purpose |
|---|---|
| `pvsyst_build_sfi` | Native variable selection; storage preset includes complete AC-flow columns |
| `pvsyst_run_simulation` | Single/batch native execution with new CSV/PDF and diagnostics |
| `pvsyst_simulate_template` | Preview or guarded copy/configure/annual run; fixed or bounded inherited UnlimTracking |
| `pvsyst_run_sourced_house_task` | Source binding, preparation and native house delivery using existing checkpoints |
| `pvsyst_run_house_task` | Owned native house lifecycle and fresh-CLI annual reload, resume/retry |
| `pvsyst_simulate_design` | Strict single-design inputs, exact assets, declared envelope and weather mode |
| `pvsyst_prepare_sourced_design` | Electrical draft from exact declared value/unit citations, without inventing missing data |

See [automatic simulation](automatic-simulation.md),[design inputs](single-design-inputs.md),[tracking tasks](tracking-template-flow.md),[house modeling](house-pv-modeling.md),[house tasks](native-house-task.md) and[sourced house tasks](sourced-house-task.md). dry_run is not native consumption. Open-Meteo dry-run returns a weather plan, not complete execution preflight. needs_review remains separate from stage=completed; failed outputs and explicit new-name recovery remain visible.

### Results (3)

| Tools | Purpose |
|---|---|
| `pvsyst_read_results`, `pvsyst_read_rows` | Summaries and paged actual CSV values |
| `pvsyst_read_batch_results` | Native SIM_* batch summary fields |

See [result comparison](result-comparison-verification.md). Keep original units, time labels, actual omitted columns and warnings; do not synthesize missing physical outputs or apply the PV-only PR formula to storage.

## Parameter guides

Variant updates use one supported independent transaction at a time, except explicitly allowed name/scalar combinations. Details and exact ranges are in[loss scalars](grid-capacity-verification.md),[soiling](soiling-verification.md),[detailed loss modes](native-detailed-losses.md),[IAM/ageing](native-model-inputs.md),[seasonal](seasonal-orientation.md),[horizon](horizon-profile.md),[inverter temperature](inverter-temperature.md),[PF](power-factor.md),[power limits](grid-power-limit.md),[combined controls](native-power-controls.md) and[P50](p50-native-parameters.md).

New-VC entries separately cover[AC](native-ac-cabling.md),[DC GUI](native-dc-cabling.md),[MV night](native-mv-night.md),[tracking](native-tracking-controls.md),[Sheds](native-shed-controls.md) and[Dome](native-dome-allocation.md). Scene authorship, stored bindings, native validity, source geometry and consumption are independent checks; the[bounded two-roof task](engineering-delivery.md#finite-two-roof-task-bounded-native-acceptance) is not a generic identity converter.

## Files and coordination

File arguments are filenames, not unrestricted paths. Stage inputs under the configured workspace:

| Files | Directory |
|---|---|
| PRJ/VC | Projects |
| SFI, input RVT | Models |
| Weather CSV, MEF, MET, monthly CSV | Meteo |
| SIT | Sites |
| Advanced.dat, load inputs, authored scene | UserData |
| Batch params/RVT/summary | UserBatch |
| Per-scenario hourly CSV | UserHourly |
| Simulation CSV/PDF, ZIP, manifests | Results |

Current-token/file-map guards, backups, new-file conflicts and explicit confirmation are part of each mutating operation. WorkspaceRLock and first-client initialization are process-local, not exclusion of external editors. Expected input/file/native-adapter errors keep their actionable MCP2.x reason. Dates use MM.DD, YY.MM.DD orYYYY.MM.DD, not hyphens. Use the actual tool schema for required arguments; unrecognized keys and wrong strict types stop before execution.

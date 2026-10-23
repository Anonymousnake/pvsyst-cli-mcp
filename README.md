# PVsyst CLI MCP

Unofficial local [Model Context Protocol](https://modelcontextprotocol.io/) server and Python adapter for the **installed Windows PVsystCLI**. Tested with 8.0.6 and 8.1.6. The server wraps the vendor CLI; this repository does not include PVsyst, projects, license material, weather data or simulation output.

## Scope and version differences

PVsystCLI automates simulations of **existing** projects/variants; editing an entire project, shading scene, component database or GUI is outside the official CLI interface. A separate bounded Chinese 8.1.6 GUI bridge imports generated house/PV scenes into an already open matching project; this is not whole-desktop automation or an official CLI import command.

The adapter reads `PVsystCLI.exe help` for command/option availability. Unsupported features fail **before** a simulation consumes an execution:

| Feature | 8.0.6 | 8.1.6 |
|---|---|---|
| Existing project simulation, CSV/PDF, date range, language, pages | Yes | Yes |
| Weather CSV to MET, license commands, logs export | Yes | Yes |
| Batch params/RVT, shading recomputation | No | Yes |
| Site creation, site override, synthetic weather generation | No | Yes |
| Hour/subhour option | No | Yes |

Options-file indirection (`-cof`) and overwriting the CLI's default CSV (`-odc`) are deliberately omitted from the MCP tools: all supported functional options are named explicitly and output filenames must be new. A site file is not a full project. Features not present in the installed executable's own help remain unavailable regardless of the latest online documentation.

## Installation

- Windows and Python 3.10+; install your own PVsystCLI and configure a workspace.
- An existing `.PRJ` project plus `.VC*` variant for simulation.
- Check **the CLI's** license/quota with `pvsyst_license_info` before running jobs. GUI and CLI license states can differ.
- Windows also needs the `tzdata` package from `requirements.txt`: CPython ships no IANA zone database, and the weather-acquisition tool derives a place's fixed standard UTC offset from an IANA zone name. This is a local Python dependency, not a PVsyst capability or native-behaviour change.

```powershell
cd path\to\pvsyst-cli-mcp
py -m pip install -r requirements.txt
$env:PVSYST_CLI = 'C:\Program Files\PVsyst8.1.6\PVsystCLI.exe'
$env:PVSYST_WORKSPACE = 'C:\path\to\PVsyst8.1_Data'
py -m unittest discover -s tests -v
py tests\smoke_stdio.py   # real CLI: capability, license, project queries only
```

Configure a local stdio MCP client, substituting the Python executable and paths on your machine:

```json
{
  "mcpServers": {
    "pvsyst": {
      "command": "C:\\path\\to\\python.exe",
      "args": ["C:\\path\\to\\pvsyst-cli-mcp\\pvsyst_mcp_server.py"],
      "env": {
        "PVSYST_CLI": "C:\\Program Files\\PVsyst8.1.6\\PVsystCLI.exe",
        "PVSYST_WORKSPACE": "C:\\path\\to\\PVsyst8.1_Data"
      }
    }
  }
}
```

Restart the client after editing its MCP configuration. The server requires Python MCP SDK 2.x (`requirements.txt`).

## MCP tools by workflow

The server exposes 74 tools across seven groups. The approved house/native migration is consolidated in the [acceptance summary and explicit limits](docs/native-migration-acceptance.md). Existing tool names and arguments remain compatible. `pvsyst_review_weather` and `pvsyst_prepare_sourced_design` have passed real MCP acceptance; their engineering/source boundaries are documented in [weather engineering review](docs/weather-engineering-review.md). The Components entry, `pvsyst_build_sourced_component`, previews or creates a NEW user-defined PAN/OND from an exact complete template and cited scalar values. It discloses inherited model fields and curves, requires explicit acknowledgement before creation, and does not refit or certify the model. See [sourced component creation](docs/sourced-components.md). Restart an existing MCP server to load the latest tools/schema. Each description starts with its category label, so flat-list clients can still identify the workflow group.

First CLI initialization is serialized inside the MCP process, so concurrent initial requests share one client and the same workspace `RLock` used by CLI, component and variant operations. This is process-local coordination, not an OS/cross-process lock; SHA checks remain optimistic and external editors still require coordination. See [concurrent initialization verification](docs/grid-capacity-verification.md#single-client-concurrent-initialization).

Expected input, missing/existing-file and adapter `PVsystError` exceptions retain their actionable reason in MCP SDK 2.x tool errors, rather than being masked as an unexpected execution failure. The shared registration boundary preserves signatures/strict argument schemas; direct Python calls keep their original exception contract. Internal exceptions are not converted into expected user errors. For example, hyphenated simulation dates report `Date must use MM.DD, YY.MM.DD, or YYYY.MM.DD` before simulation launch; use `1990.01.01`, not `1990-01-01`. See [native preflight verification](docs/grid-capacity-verification.md#mcp-expected-cli-error-feedback).

### Setup & License (6)

| Tools | Purpose |
|---|---|
| `pvsyst_capabilities` | Installed CLI version and supported commands/options |
| `pvsyst_license_info` | License state and quota without key or Host ID |
| `pvsyst_license_activate`, `pvsyst_license_deactivate`, `pvsyst_license_sync` | Confirmed license changes |
| `pvsyst_export_logs` | Diagnostic ZIP under Results |

### Projects & Variants (31)

The original core project/variant block passed a fresh-stdio integrated acceptance exercising its 20 tools, six native CLI 8.1.6 runs and exact full-project recovery. Tracking creation and Dome allocation have separate verification below. See the [delivery checklist, integrated evidence and explicit boundaries](docs/project-variant-acceptance.md). This is not a claim of complete GUI/model or arbitrary-topology parity; those extensions remain in the broader roadmap. Restart an already running MCP server to load the current code/schema.

| Tools | Purpose |
|---|---|
| `pvsyst_list_projects`, `pvsyst_list_variants`, `pvsyst_inspect_project` | Workspace inventory and PRJ/VC hashes |
| `pvsyst_create_project`, `pvsyst_clone_project`, `pvsyst_clone_variant` | Create from complete PRJ/VC text or a workspace template; copy variants within or across projects |
| `pvsyst_update_project_parameters` | Guarded project display-title preview/edit; explicit UTF-8 BOM addition when needed |
| `pvsyst_update_project_sources`, `pvsyst_restore_project_sources` | Persist a workspace SIT/MET pair across a project and every variant; restore a saved source/title file set |
| `pvsyst_get_variant_components`, `pvsyst_update_variant_components` | Inspect and replace scoped PAN/OND/BTR/GEN references |
| `pvsyst_get_variant_parameters`, `pvsyst_update_variant_parameters` | Inspect, preview and edit bounded variant parameters; read native transformer layers, PV outage periods, peak-shaving/scene declarations, ageing templates and IAM tables without widening their writers. See [stored native state](docs/native-state-inspection.md) |
| `pvsyst_create_tracking_variant` | Preview or create a NEW VC from one of nine verified native tracking record types; partial type-owned controls, public read/edit closure, exact precision and ordered source/candidate bounds. Preserve source and unrequested geometry; no replacement tracking/shading model |
| `pvsyst_create_native_dc_cabling_variant` | Set native mOhm resistance or STC loss percent through the already open Chinese8.1.6 GUI; confirmed native SaveAs, coupled DC owner/read/re-edit checks and unchanged non-DC physical records. Software owns all calculation; no DC proportional inversion. See [native DC bridge](docs/native-dc-cabling.md) |
| `pvsyst_create_ac_cabling_variant` | Enable or re-edit native uniform per-inverter AC cables with coupled owner checks, exact 0.1m input precision and public readback; native CableType computes electrical values, while StringNode DC cables remain unchanged. See [native AC cabling](docs/native-ac-cabling.md) |
| `pvsyst_create_mv_night_disconnect_variant` | Toggle only the recorded single-MV night switch and its owning dependency bit; full source/candidate gates, no transformer formula or HV construction. See [native MV night controls](docs/native-mv-night.md) |
| `pvsyst_create_shed_geometry_variant` | Create/read/revise an existing native Sheds or SunShields template with exact stored precision and full shared model gates; no type conversion or replacement shading model. See [native shed controls](docs/native-shed-controls.md) |
| `pvsyst_create_dome_allocation_variant` | Preview or create a NEW VC that rebalances an already-allocated native two-face Dome template; preserve hardware, paired geometry and the native mixed-orientation model |
| `pvsyst_prepare_design_project` | Reuse the strict single-design discovery/weather/configuration flow but stop before SFI/annual simulation; NEW guarded source project, declared/inherited inputs, durable recovery and preserved review flags. `prepared` is not native energy consumption. See [source project preparation](docs/design-project-preparation.md) |
| `pvsyst_open_native_project` | Confirmed cold launch and native project loading in the configured workspace; matching existing windows remain read-only, other/busy sessions stop untouched. No INI edit, variant selection or process close. See [native project session](docs/native-project-session.md) |
| `pvsyst_create_house_pv_scene` | Create an AI-authored house with one or several named PV regions in one PVCollada call; locate invalid parts/triangles for correction, preserve source module totals and retain legacy calls |
| `pvsyst_read_house_pv_scene` | Recover complete house/PV create arguments from supported scene XML, stop rather than drop unknown content, and expose missing context in older AI scenes for explicit revision into a new file |
| `pvsyst_import_house_pv_scene` | Confirmed native GUI import, orientation association, validity/shading table and save as a NEW variant; Chinese 8.1.6 recorded layout, an already open matching project and one or two PV regions only. See [native house import](docs/house-pv-modeling.md#原生自动导入r4748) |
| `pvsyst_validate_variant_structure`, `pvsyst_clone_subarray`, `pvsyst_remove_subarray` | Check references and edit isolated unshaded grid branches |
| `pvsyst_restore_variant` | Restore a SHA-guarded variant backup |
| `pvsyst_archive_project`, `pvsyst_archive_variant`, `pvsyst_restore_project_archive` | Confirmed PRJ/VC archive and recovery |

See [native tracking controls](docs/native-tracking-controls.md) for the bounded template, explicit degree ranges, strict source checks and fresh annual CSV/PDF verification. The native model can produce 2D near-shading losses without an explicit 3D scene; the report's no-Shadings summary label is not a switch-state test. Fixed-plane tools keep their existing scope, and tracking type conversion or arbitrary geometry is not added.

See [native Dome allocation](docs/native-dome-allocation.md) for the verified same-subarray second-orientation workflow and the bounded existing-template API. The original unassigned-pair failure remains reproducible; initial construction still uses PVsyst. Native annual CSV/PDF comparisons, the retained first-run failure and its unchanged-input recheck are documented separately from configuration creation.

See [house and PV modeling](docs/house-pv-modeling.md) for one-shot AI-authored house meshes: named walls, arbitrary roof parts, eaves, door/window geometry, chimney, porch and attached volumes, plus a PV table placed over a named supporting roof. AI supplies the complete geometry; the thin serializer reuses the standard XML/PV references rather than rebuilding a flat/gable template. The original parametric calls remain compatible. It writes only a new `.pvc2`, not private VC shading data; native import, association and calculation remain separate. No trees, surrounding buildings, electrical shading model or replacement irradiance calculation is added.

### Components (14)

| Tools | Purpose |
|---|---|
| `pvsyst_list_components`, `pvsyst_get_component`, `pvsyst_validate_component` | PAN/OND/BTR/GEN inventory, inspection and static checks |
| `pvsyst_create_component`, `pvsyst_copy_component`, `pvsyst_clone_component` | Create, import or clone a component |
| `pvsyst_build_sourced_component` | Preview/create a NEW template-derived PAN/OND with exact numeric/unit citations, inherited model/curve disclosure and a candidate source record; explicit acknowledgement required; always `needs_review`, not a physics fitter |
| `pvsyst_update_component`, `pvsyst_backup_component`, `pvsyst_restore_component` | Allowlisted edit, byte-identical backup and recovery |
| `pvsyst_archive_component`, `pvsyst_restore_archived_component` | Archive an unreferenced component and restore it |
| `pvsyst_compare_components`, `pvsyst_project_components` | File comparison and PRJ/VC reference lookup |

### Sites & Weather (8)

| Tools | Purpose |
|---|---|
| `pvsyst_build_monthly_weather`, `pvsyst_create_site` | Build monthly CSV and create an SIT (8.1+) |
| `pvsyst_convert_meteo` | Convert CSV + MEF + SIT to MET |
| `pvsyst_inspect_weather_csv` | Inspect standard hourly CSV metadata, units, full-year coverage and monthly observations |
| `pvsyst_prepare_weather` | Convert one verified hourly CSV into new SIT/MET files plus a provenance manifest |
| `pvsyst_resolve_location` | Read-only place-name lookup: every candidate with its integer `location_id`, coordinates, ISO country code and IANA zone; no candidate is chosen automatically |
| `pvsyst_acquire_weather` | Plan or download one public Open-Meteo ERA5 year into a new standard hourly CSV plus the raw provider response and a provenance manifest |
| `pvsyst_review_weather` | Read-only review of one prepared CSV against one native result: provider-to-CSV, CSV-to-native hourly values and labels, and declared `.MET`/`.SIT` header fields. No write, hash, licence or simulation (accepted against the corrected legal-time 2024 native run; source-aware review still discloses the temperature approximation and evidence limits) |

### Loads & Storage (6)

| Tools | Purpose |
|---|---|
| `pvsyst_prepare_load_profile` | Check one complete 8760/8784-hour nonnegative load in UserData with explicit W/kW units, year and fixed standard time; preview or create a NEW ASCII native CSV and v1 import record without filling, resampling or yearly normalization |
| `pvsyst_create_load_variant` | Bind current raw/normalized loads to a NEW VC with an explicit Energy/MJ signed-base62 hourly snapshot, remove the old owning load cache, disclose encoding precision and preserve the source variant/system/strategy/site |
| `pvsyst_create_native_load_variant` | Preview or create a NEW PV-only VC using native constant kW, twelve monthly kWh values, or a 24-hour kW profile; enable native self-use without external load generation; preserve source files and reject unsupported load/storage/geometry shapes |
| `pvsyst_create_peak_shaving_variant` | Preview or create a NEW VC from the verified existing native PeakShaving/PVToGridMin profile; select AfterSunset or a native hour window while preserving hardware, SOC, efficiencies, limits and source files |
| `pvsyst_create_grid_storage_variant` | Edit native storage scalars while retaining the source's verified strategy, including PeakShaving's two recorded pairs; shared source/candidate semantics, public read/re-edit and Peak timing interoperability. WeakGrid still requires fresh complete availability; preserve pack/PV/load/site/flags. See [scalar closure](docs/grid-storage-parameters.md#r55-原策略标量闭环) |
| `pvsyst_create_grid_storage_system` | Add caller-declared battery hardware and native AC-coupled SelfConsumption or WeakGrid to a PV-only Grid VC; require complete SOC/power/efficiency/temperature/SOW and prepared hourly-load inputs, plus matching explicit availability for WeakGrid; enable real load consumption and create one NEW variant |

See [storage hourly loads](docs/storage-loads.md) and [native storage parameters](docs/grid-storage-parameters.md). Both storage creation tools accept optional strict `allow_grid_export`: false sets the native no-injection bit, true clears only that bit, and omission preserves the source setting. Public MCP creation and reversible 48-hour native controls verified zero export with unchanged load/SOC; reenabling export reproduced every original numerical column. This is export control, not grid charging. These tools cover load preparation/binding, existing storage parameter edits and PV-to-native-AC-storage construction, not full hybrid DC battery-port construction, automatic pack sizing or grid-charging schedules. WeakGrid accepts a fresh complete 0/1 availability CSV with explicit year/standard time and at least one outage, binds native `pvUnavail` data and preserves reserve SOC while the grid is available. It does not imply grid-charging schedules or native hybrid DC ports are implemented. Merely changing the external CSV path can leave the old load cache in use even when native logs say it was read. Native `E_User` has been exported and compared for all 8784 leap-year hours; later CSV edits require a new binding. Preparation/creation alone are never labelled native consumption certification. PV-only creation can bind WeakGrid directly; load and availability must share the declared year and fixed standard UTC offset. The native `grid_storage` SFI preset includes `CL_Over`: the previously unexplained partial-charge/curtailment hour was resolved by this omitted native output, and a fresh 8784-hour run retained all original result values while bringing the full AC-balance residual within 0.0097 kW CSV precision. No storage model, source input or result column was corrected externally.

Native constant/monthly/daily definitions use the guarded 8.1.6 fixed-plane PV-only subset described in [native load inputs](docs/storage-loads.md#原生常数月度与日曲线). Monthly inputs are declared kWh, not twelve kW values; the adapter writes native fields and leaves hourly allocation and self-consumption to PVsyst. Three fresh annual runs verified changed values, including zero-load months/hours, against `E_User` and PDF totals. Existing hourly CSV tool signatures and flag inheritance remain unchanged. Probability and household generators, native random outages and hybrid DC ports are not added by this tool.

Existing native PeakShaving/PVToGridMin discharge controls are exposed through [bounded peak-shaving configuration](docs/native-peak-shaving.md), without hardware sizing, grid charging or independent scheduling. The original storage APIs retain their SelfConsumption/WeakGrid paths. New windows have passed native annual CSV/PDF and complete AC-balance checks; this does not redefine AfterSunset as a no-sunlight test.


### Simulation (6)

| Tools | Purpose |
|---|---|
| `pvsyst_build_sfi` | Create an hourly export definition; `variables=grid_storage` includes native load/SOC/battery/grid flows, unmet load, unused energy and `CL_Over` converter-limit losses for complete storage balance |
| `pvsyst_run_simulation` | Single or batch CLI simulation with CSV/PDF outputs; redacted diagnostics and warning lines |
| `pvsyst_simulate_template` | Read-only preflight or guarded template copy/configuration, complete-year run and metric/report acceptance |
| `pvsyst_run_house_task` | One complete AI-mesh house task: new guarded project/scene, owned native open/import/association/validity/table/SaveAs, normal owned GUI exit, fresh CLI full-year reload and CSV/PDF; durable resume, explicit new-name retry and source/inheritance disclosure. One/two PV regions, configured Chinese8.1.6/DPI96 profile. See [native house task](docs/native-house-task.md) |
| `pvsyst_simulate_design` | One unified single-design spec: exact component and template resolution, declared electrical envelope, integer capacity planning, weather mode selection and guarded full-year run |
| `pvsyst_prepare_sourced_design` | Assemble one `electrical_design` draft from caller-declared source text, exact value/unit tokens and excerpts, then reuse the envelope; missing fields come back as a draft (**SDK-accepted: 120 passed / 0 failed, exit 0; the real Jinko/Solis case returned `blocked` with 13 missing fields, not a complete manufacturer engineering package**) |

Native constant/monthly soiling is available through `pvsyst_update_variant_parameters(..., soiling_updates=...)`, with current values in `pvsyst_get_variant_parameters` under `main_array.soiling`. Use explicit `mode=none|constant|monthly` and GUI-precision percentages; the bounded 8.1.6 editor preserves flags and reuses preview/backup/restore. The `soiling` SFI preset adds native `GlobSlg`/`SlgLoss` (omitted by native disabled runs). See [native soiling configuration and annual verification](docs/soiling-verification.md).

For a complete native grid-connected cycle, including original-database components and report checks, see the [tutorial-aligned grid workflow](docs/tutorial-grid-cycle.md). Annual preflight checks native MET root date declarations before creating a working project: a year in a filename is not coverage evidence. Generic-1990, missing or unreadable declarations remain disclosed rather than silently re-calendarized.

Native injection-point active-power limiting is available through `pvsyst_update_variant_parameters(..., grid_limit_updates={"mode":"constant","limit_kw":250})`; use `mode=none` to disable. The bounded 8.1.6 PV-only fixed/unshaded editor rejects PF/storage/unknown system modes, uses the existing guarded transaction and never clips exported results externally. Inspection returns `grid_limit`; `variables=grid_limit` exports native limit/curtailment columns. A public MCP annual run matched all 8760 hourly rows and 17 numerical columns of its native GUI reference. See [native power-limit configuration and verification](docs/grid-power-limit.md). Restart the MCP server for the new optional schema.

Existing Uc/LID/quality/mismatch edits have also passed native GUI reload/resave and paired 24-hour CLI controls, including `TArray`. GUI metadata rewrites are disclosed and do not widen the thermal editor's flag scope. See [loss GUI roundtrip evidence](docs/loss-gui-roundtrip.md).

Native operating/night auxiliary powers remain available through `pvsyst_update_variant_parameters(..., auxiliary_updates={"mode":"constant","operating_kw":5,"night_kw":2})`, or `mode=none` to disable. The new `profile` mode adds two verified families: constant operating power with an output threshold, or a proportional W/kW term with its threshold. All five values are explicit, night power is zero in the new mode, and unverified mixed/coupled modes stop before writes. Inspection returns `auxiliaries`; native negative grid values and guarded preview/backup/restore are preserved. See [original constant-power evidence](docs/native-auxiliary-power.md) and [new profile bounds and annual evidence](docs/native-detailed-losses.md).

`string_mismatch_updates={"mode":"constant","loss_percent":2.5}` now edits native string-voltage mismatch, independently of module `PMismMPP`; `{"mode":"default"}` restores the native default bit and removes the manual field. `subarrays[].string_mismatch` reports the mode without inventing a numeric default. Three GUI whole-file matches, 61 unchanged-file negatives and three annual 8760-row/16-column CSV plus all-page PDF comparisons passed for string mismatch and the two auxiliary families. The grouped loss study covers all nine pages and 21 native annual controls without promoting unresolved wind/spectral/circuit dependencies. See [native detailed-loss inputs and evidence](docs/native-detailed-losses.md). Restart the MCP server for the appended optional schema.

Native seasonal orientations expose raw `SummerTilt`, `WinterTilt`, `WinterMonths` and `UsesTiltAver`; `FieldTilt=0` alone is not their effective tilt. The guarded `seasonal_updates` transaction configures explicit summer/winter angles and winter-month selection on a verified single free-horizon Grid plane, preserving azimuth. Fixed-to-seasonal and month updates matched native GUI files byte-for-byte, and the public MCP annual run matched all 8760 rows/14 numerical columns. See [seasonal configuration](docs/seasonal-orientation.md) and [grouped native evidence](docs/plane-horizon-native.md).

The independent `horizon_updates` transaction adds or clears a bounded native far-horizon profile. Supply 4..100 increasing native azimuth/elevation points and a profile name; new geometry drops stale derived factors while name-only edits preserve them. Four- and six-point public configurations matched their GUI native references across 8760 rows/14 numerical columns, with clear/restore and 25 invalid-input checks. No external shading or diffuse calculation is used. See [horizon configuration](docs/horizon-profile.md); restart MCP for the new optional schema.

A grouped native energy-management study verifies temperature and constant PF plus active/apparent grid modes. The independent `inverter_temperature_updates` transaction configures ambient/offset/fixed mode with explicit saved temperature pairs; both controlled modes matched GUI native references across 8760 hours/16 numerical columns. No thermal or derating model is reimplemented. Raw `system_parameters` remain available without defaults or PF conversion. See [inverter temperature configuration](docs/inverter-temperature.md) and [grouped native energy evidence](docs/native-energy-management.md).

The independent `power_factor_updates` transaction configures native constant/monthly signed tangent values and explicit native/active/apparent inverter PNom interpretation. Seven original files matched GUI references; monthly PNom now also has shared read/write validation, actual edit/read/edit/clear contracts and six fresh native annual controls, including bounded active injection limits. No cosine or reactive-power model is reimplemented. See [power factor configuration and evidence](docs/power-factor.md).

`power_controls_updates` now provides a separate bounded PF/grid-limit transaction with explicit active/apparent quantity, injection/inverter point and loss classification. Nineteen GUI files and annual controls, 38 public mode transitions, 57 unchanged-file negatives and seven public annual CSV/PDF comparisons passed. Standalone editor domains stay unchanged. See [native power controls and evidence](docs/native-power-controls.md).

`ageing_updates` edits required year/mean and optional paired native RMS/retention inputs in an existing activated template; `ageing_enabled` separately toggles only its verified enable bit after the same complete template validation. Disabled templates remain readable. Stored factors and warranty lists are preserved; PVsyst alone computes degradation and sampling, and sample-array persistence is not claimed. Zero mean is not a disable switch. The independent `iam_updates` still selects component/ASHRAE without generating curves. Original annual evidence and fresh public roundtrips, malformed-template controls and eight native 48-hour runs are documented in [native model inputs](docs/native-model-inputs.md); restart a long-running MCP server to load the appended schema.

The independent `p50_p90_updates` transaction edits all fourteen native P50 parameters in an existing GUI-produced result template. Four complete configurations matched GUI files and native annual probability-page bodies, while all 8760 rows/16 numerical columns stayed unchanged; 50 invalid-input/source checks preserved files. It does not create P50 blocks, fabricate cached results, or calculate probabilities outside PVsyst. Construction from an input-only variant remains pending. See [P50 native parameters and evidence](docs/p50-native-parameters.md); restart MCP for the new optional schema.

Further tutorial work follows the [native entry map](docs/native-entry-map.md): inventory first, verify persistence and native consumption by functional group, then implement and accept bounded stages. Candidate symbols, saved settings, native runtime evidence and MCP implementation status are tracked separately; this is not a claim of complete tutorial coverage.

Native PDF selection is available as `pvsyst_simulate_template(..., report_pages=["summary", "losses"])` or `pvsyst_simulate_design(spec={..., "report_pages": ["results"]})`; omission keeps the complete default report. Public preflight and three native annual paths verified the selection without changing CSV results. For AI-driven annual runs, start with `pvsyst_simulate_template(..., dry_run=True)` and inspect its `status`/`issues`. Supply a valid single-subarray/fixed-plane grid PRJ/VC template, complete loose text PAN/OND, workspace SIT/MET, tilt, simulation year and a new project filename. Explicit `dry_run=False` creates/configures a working copy, exports CSV/PDF and returns full-year energy, standard PR, `IL_Pmax`, warnings and a persistent reverse-recovery manifest. `needs_review` is not a normally accepted job; native completion alone does not certify physical inputs. Template topology/counts are retained, not automatically sized. Optional `weather_manifest_name` binds a `pvsyst_prepare_weather` manifest to the requested year and SIT/MET names, rechecks the source, normalized, monthly, MEF, SIT and MET hashes before any write, loads the named SIT/MET explicitly in the native run and compares hourly `GlobHor`/`DiffHor`/`T_Amb` against the prepared CSV. The disclosed 0.1 W/m2 and 0.01 degC thresholds are a review heuristic for rounding/conversion, not a claim about native MET storage precision, and a conversion-related warning keeps `needs_review` even when the annual run itself completes. See [automatic simulation, status handling and verification](docs/automatic-simulation.md). Restart an existing MCP server for this new tool.

`pvsyst_resolve_location(query, country_code="")` returns every geocoding candidate for a place name with its integer `location_id`, coordinates, ISO country code, administrative name and IANA zone. `selection_required` stays true and `ambiguous` is set unless exactly one candidate matched, so an AI must pass one explicit `location_id` instead of the tool silently taking the first hit; zero candidates ask for a country/administrative qualifier or explicit coordinates. The lookup is read-only and downloads no weather.

`pvsyst_acquire_weather(csv_name, year, location_id=…|<latitude+longitude+site_name+country_code+timezone_name>, altitude, temperature_method="endpoint_mean", dry_run=True)` plans or downloads one public Open-Meteo ERA5 year (1940–2068, only years the provider has fully published; a `location_id` and explicit coordinates are mutually exclusive, a `location_id` takes its ISO country from the geocoded candidate so `country_code` is optional on that path, and explicit coordinates require `site_name`, `country_code` and `timezone_name` because nothing is inferred from the provider). Irradiance labels are preceding-hour UTC interval ends and `temperature_2m` is an instantaneous UTC reading. The emitted CSV uses one fixed local winter standard offset (`local_standard_interval_start`, `utcoffset - dst`) with hourly interval-start labels, covers both adjacent UTC dates and the leap day, and never fills, zeroes or interpolates an unpublished hour. Hourly Tamb is the disclosed unweighted mean of the two bounding UTC instants (`endpoint_mean`, always returned as a review item), and the provider's 0.25° grid centres and DEM elevation are echoed as evidence while site metadata keeps the requested place. Execution writes three new files (raw response, standard CSV, provenance manifest), refuses to overwrite, and retains them plus the reason on failure. Half-hour IANA offsets and standard offsets that change inside the requested year are blocked instead of rebinned, and the installed CLI profile bound is −12…+12 whole hours; resolving an IANA zone on Windows needs the `tzdata` package. A negative daylight-saving convention is blocked as well: for `Europe/Dublin` the legal standard offset is +1 h while its winter wall-clock offset is 0 h, so no winter convention is guessed (`Africa/Casablanca` uses negative DST during Ramadan, so a negative value is not always a winter signal), while `Europe/Berlin`, `Australia/Sydney`, `Asia/Shanghai` and `Pacific/Honolulu` remain normal.

`pvsyst_simulate_design(spec, dry_run=True)` is the unified input for one design: `spec.new_project`, `spec.year`, `spec.target_dc_kw`, `spec.tilt`, `spec.azimuth`, an exact module/inverter identity (or one explicit workspace filename), a `weather` mode and **exactly one** of `capacity` or `electrical_design`. Every nested object is a strict schema: unknown keys are rejected before the backend runs, and booleans or numeric strings are never accepted as design numbers. Optional `template_project`/`variant` restrict the template scan; without them every eligible workspace member is checked and ranked by fewest inverter changes, then fewest parameter steps, then capacity error, then name, and no eligible template returns the concrete missing item of each candidate instead of inventing a blank PRJ. `electrical_design` accepts declared values only: a coefficient always states its unit and source, a scaled unit such as `mA` or `kV` is rejected while plain `A`/`V` or a value whose key already carries the unit is accepted, a `Pmax` coefficient is never substituted for `beta_vmp`, a whole-converter `IDCMax` is never used as a per-MPPT rating, a module operating temperature is not ambient, a single weather-file annual minimum is not a design extreme, and a coefficient-derived current requires an explicit maximum effective irradiance plus rear-gain source (no STC or rear-gain default). `weather.mode` is `existing`, `csv` or `open_meteo`; `losses` accepts only `LIDLoss`, `PMismMPP`, `ModuleQualityLoss` and `UFactor`. With `dry_run=true` nothing is written, and `weather.mode=open_meteo` returns `ready_for_weather` with `execution_preflight_complete=false` because no CSV exists yet: treat that as a weather plan only, not as a runnable or preflighted design. Execution coordinates the existing acquisition, preparation and template-simulation tools, never overwrites, keeps source artifacts and recovery steps, and reports `needs_review` when a retained approximation or provider-derived source applies. A real `dry_run=False` run has completed natively; see the validation list below. See [the single-design input closure](docs/single-design-inputs.md).

### Results (3)

| Tools | Purpose |
|---|---|
| `pvsyst_read_results`, `pvsyst_read_rows` | Summarize or page through result CSVs |
| `pvsyst_read_batch_results` | Extract SIM_* values from batch summaries |

All MCP file arguments are **filenames, never unrestricted paths**. Inputs must be staged under the configured workspace:

| File | Directory |
|---|---|
| SFI, input RVT | `Models` |
| Weather CSV, MEF, MET, monthly weather CSV | `Meteo` |
| SIT | `Sites` |
| Advanced parameters `.dat` | `UserData` |
| Batch parameters `.csv`, batch RVT, generated batch summary | `UserBatch` |
| Batch per-scenario hourly CSV | `UserHourly` |
| Single-simulation CSV/PDF, diagnostic ZIP | `Results` |

`pvsyst_inspect_weather_csv(csv_name, expected_year)` reads a **PVsyst standard
hourly weather CSV** staged under `Meteo`. It checks required site metadata,
explicit units, finite values, nonnegative radiation/wind, DHI <= GHI, and one
full year of unique, increasing hourly labels including night hours and leap
day. The optional integer `expected_year` prevents accepting a different year;
otherwise the year comes from `#Year` or the data. Results include the input
SHA-256, missing-label examples and observed monthly irradiation/temperature.
Those monthly observations are not gap-filled or certified complete totals.

The supported full-year profile uses interval-start labels from January 1
00:00 through December 31 23:00. Nonzero minutes are reported without guessing
whether the source labels interval starts, centers or ends. Explicit time
references, nonzero shifts and POA inputs disclose unchecked scope. `valid` is
false for detected issues, null for unchecked conditions only, and true when
these scoped CSV checks pass. This is not a general CSV importer or a native
MET validator: MEF/SIT mapping, timezone conversion and clear-sky quality still
need verification. See [weather validation evidence](docs/weather-input-verification.md).

`pvsyst_prepare_weather(csv_name, year, sit_name, met_name, country_code, time_basis, dry_run=True)` converts **one verified standard hourly CSV** into NEW SIT/MET files plus a provenance manifest. It requires `GHI`, `DHI` and `Tamb` only, an explicit uppercase ISO alpha-2 `country_code` so site creation does not query an external service, and `time_basis="local_standard_interval_start"` (fixed local winter standard time, no DST, hourly interval-start labels). Site name, coordinates, altitude, timezone, country label and source come from the verified CSV metadata. UTC, explicit time references, nonzero shifts, POA, missing measurements and any unmapped variable are reported as preflight issues; the tool does not shift time, fill data or drop columns. `dry_run=true` writes nothing. Execution checks the source SHA-256 before and after, reserializes only date columns and units, then creates a normalized import CSV, a monthly CSV, a MEF and the MET under `Meteo`, the SIT under `Sites`, plus a provenance manifest under `Results`; any existing destination stops the call. `status` is `blocked`/`ready` before execution, `prepared` or `needs_review` (retained native conversion warnings) afterwards, and `failed` with the generated artifacts and manifest kept for inspection. `prepared` means the files exist, not that the native MET is verified. Pass the returned `artifacts.manifest` filename as `weather_manifest_name` to `pvsyst_simulate_template`. See [site, weather and year closure](docs/weather-preparation.md). Restart an existing MCP server to load this new tool.

Create a project with `pvsyst_create_project(project, project_content, variants)`, supplying complete UTF-8 PRJ text and a map such as `{"VC0": vc0_text, "VC1": vc1_text}`. No existing source project is required. The tool preserves supplied BOM/newlines and references, validates file envelopes and subarray sections before writing, rejects empty maps, duplicate normalized IDs and existing same-prefix members, and cleans up its created files after handled write failures. Maximum 127 variants, 32 MB per member; project copying shares the same 128-member write boundary. The returned `files` map can be used for project archive. SIT/MET, components and scene dependencies remain external. This is full-content authoring/import, not automatic invention of blank physical defaults or full schema/physical validation. Inspect, validate structure and run a simulation before using the new design. Concurrent external editing and crash-atomic multi-file creation are outside the guarantee. See [content-based creation evidence](docs/project-source-verification.md#content-based-project-creation).

Target selectors `subarray_id`, `source_subarray_id` and `orientation_id` require strict JSON integer literals: use `1`, not `true`, `"1"` or `1.0`. This applies to variant copy, component replacement, subarray copy/removal and parameter edits; defaults stay unchanged. Integer existence/range and ownership checks remain in the backend. This is distinct from numeric quantity values inside `subarray_updates`, which retain their documented integral-value checks. See [integer selector verification](docs/grid-capacity-verification.md#strict-target-id-sdk-verification).

Other variant management operates on workspace `.PRJ` projects and UTF-8 `.VC*` variants. Call `pvsyst_get_variant_components(project, variant)` to inspect `SubArrayId`-scoped PAN/OND and system-scoped BTR/GEN references and obtain `sha256`. `pvsyst_clone_variant(project, source_variant, new_variant, updates, subarray_id, source_project=None)` creates a new variant, preserving all other bytes and refusing to overwrite an existing variant. Optional `source_project` imports a variant from another existing workspace project under the destination prefix; both projects must exist. Source site/meteo snapshots and external references are preserved, not silently synchronized with the destination. Inspect source differences with `pvsyst_inspect_project`; use the existing full-project source update only when intentionally changing all members. See [cross-project verification](docs/project-source-verification.md#cross-project-variant-copy). `pvsyst_update_variant_components` changes only references that already occur exactly once in the selected section; it requires the current `expected_sha256` and saves the previous bytes under `Projects/.mcp-variant-backups`. `pvsyst_restore_variant` requires `confirm=true`, matching backup name and current SHA-256, and backs up the pre-restore state. Supply `updates` such as `{ "PAN": "new.PAN", "OND": "new.OND" }` with `subarray_id` for a specific subarray; BTR/GEN target the existing `pvSystem` regardless of the subarray ID. Replacement component files must exist as loose files in the workspace or the installed DataRO library. Editing a GEN filename does not enable the generator; flags, power and thresholds remain unchanged. Structural or physical compatibility is not inferred from the replacement filename: run a short simulation and inspect its output. These reference tools do not add component fields or edit electrical topology. `pvsyst_clone_project(source_project, new_project)` copies one existing UTF-8 PRJ and all its VC files byte-for-byte; it rejects unknown same-stem sidecars and never overwrites existing files. It preserves embedded site/meteo snapshots, not an independently generated blank design. `pvsyst_inspect_project` returns a SHA-256 for every PRJ/VC member. `pvsyst_get_variant_parameters` exposes the fixed-plane orientations and selected existing subarray settings. `pvsyst_update_variant_parameters` accepts finite, bounded values for `NModSerie` (integer 1-100), `VBkUpEncl_syst` and `VBkUpDecl_syst` (0-1), and for a fixed-plane orientation without a shading table, `FieldTilt` (0-90) and `FieldAzim` (-180 to 180). It requires the current variant SHA-256 and creates a restorable byte-identical backup. Apart from the bounded inverter-group scaling described below, it does not redesign the circuit tree, add subarrays, switch system type, modify the shading scene, or synchronize existing shading tables; such orientation edits are rejected. `pvsyst_archive_project` requires all hashes from `pvsyst_inspect_project`; `pvsyst_archive_variant` requires the variant hash and at least one other variant; both require `confirm=true` and move original files under `Projects/.mcp-project-archive`. `pvsyst_restore_project_archive` checks hashes and destination conflicts before restoring. Site, meteo and component files remain in their own workspace directories. These file-level tools do not replace PVsyst's own project authoring and physical consistency checks.

`pvsyst_update_project_parameters(project, expected_files, project_name, dry_run=False, add_utf8_bom=False)` changes an existing project's display title, not its PRJ/VC prefixes or variant names. Inspection adds `project_name` from the owning header (null if absent/duplicate there) and `utf8_bom` for the PRJ. Require one existing root header Comment before nested objects/PrintSettings and a complete recognized object tree; missing, nested, duplicate or late names stop rather than creating a default. Names share the variant editor's strict 1-80 printable UTF-8 byte limit with no surrounding whitespace. Use the entire inspected `files` map, including unchanged VCs, for conflict checking. Preview/no-op creates no backups; applying snapshots the complete project and changes only PRJ bytes. The shared source/restore commit helper skips byte-identical candidates, preserving unmodified VC bytes, modification times and file IDs. Formal recovery reuses `pvsyst_restore_project_sources(..., backups=edited["backups"], expected_files=edited["files"], confirm=True)`, including recovery of the original BOM state. A non-ASCII title needs UTF-8 BOM: preserve an existing marker, or explicitly opt into `add_utf8_bom=True`; adding it changes decoding of the whole PRJ and is returned as `bom_added`, including a marker-only transaction with the same name. No silent encoding conversion occurs. CLI 8.1.6 controls show BOM-less Chinese PRJ text displaying cp1252-like mojibake, while the same UTF-8 body with BOM produces the exact intended CSV/PDF project title; English titles and all hourly values are preserved. A follow-up GUI 8.1.6 round trip also verifies a BOM-marked Chinese title in the project chooser and project-name field, actual project Save, exit/reopen, fresh MCP readback, correct CSV/PDF title and unchanged sampled hourly results. GUI rewrites some PRJ version/format fields; unchanged VCs and complete formal recovery are verified. The window caption remains the PRJ filename. This is bounded template evidence, not universal encoding/locale/version or VC-ID/reorder validation. See [project-title verification](docs/project-source-verification.md#project-title-and-explicit-utf-8-marker) and [GUI save/reload](docs/project-source-verification.md#project-title-gui-save-and-reload). The [complete title guide](docs/project-title.md) covers the original/candidate file maps, marker-only transactions, reverse recovery and closing/reopening GUI projects around MCP edits. Further [80-byte, BOM-less and GUI/MCP continuation controls](docs/project-source-verification.md#project-title-complete-bounded-acceptance) verify the supported contract. Restart the MCP server to expose the new tool and refreshed inspection schema.

`pvsyst_inspect_project` also reports each PRJ/VC file's embedded SIT/MET filenames, whether they exist in the workspace, and differences between the project and its variants. It does not infer location from the project name or validate the binary MET contents. `pvsyst_update_project_sources(project, site_name, met_name, expected_files)` uses the complete hash map from inspection, requires an existing UTF-8 SIT and MET under `Sites`/`Meteo`, and updates the project's site snapshot and weather filename plus every variant's `SiteSimul`, `SiteMet`, MET filename and site name. All members are validated before any project file changes; backups preserve the original bytes and BOM/newline style is retained. Save the returned `backups` and `files` maps. To undo the change, pass them as `backups` and `expected_files` to `pvsyst_restore_project_sources(..., confirm=true)`. Restore also backs up the state it replaces. After staging candidates, both operations re-inspect the complete PRJ/VC hash map before the first replacement, detecting content changes and added/removed members during preparation without overwriting external edits. Handled replacement errors roll back files already written; this is not a crash-atomic transaction across multiple files, and the in-process lock does not exclude external editors or changes after the final check. See [project source transaction verification](docs/project-source-verification.md). Existing weather origin metadata remains historical. Use dates available in the new MET and verify the resulting CSV's site/weather header and energy output without runtime SIT/MET overrides.

`pvsyst_update_variant_parameters(..., variant_name="East / Tilt35")` edits only the existing owning-header `pvVCalcul.Comment` display name. It does not rename the `.VC*` file, change the variant ID/project association, edit the project's title or touch nested site/orientation/subarray names. `pvsyst_get_variant_parameters` reports `variant_name` from the header (null for an absent/duplicate header field); the writer additionally parses the complete object tree and stops for missing, duplicate or late root names rather than inventing a field. Names require strict strings with 1-80 UTF-8 bytes of printable single-line text and no surrounding whitespace; numeric/bool inputs, controls and oversized text stop without writes. Existing preview, stale-input protection, no-op, backup and exact restore apply; the response adds `name_change.before/after`. Name and ordinary scalar edits may share one transaction, while mode/sharing toggles remain separate. English and Chinese native CSV/PDF titles are verified, with unchanged hourly results. This is display-name management, not file-ID renumbering or GUI reorder support. See [display-name verification](docs/project-source-verification.md#variant-display-name-transaction). Restart the MCP server to refresh the optional arguments in the tool schema.

The variant name path shares the project title's native encoding rule: non-ASCII names require an existing UTF-8 BOM or explicit `add_utf8_bom=True` alongside `variant_name`. Inspection reports the VC's `utf8_bom`; header names are raw UTF-8 text, not evidence of effective native decoding. The option is a strict JSON boolean and is rejected without a name request; scalar-only calls retain their original marker state. Adding the marker affects decoding of the entire VC, not just its name, so it is never silently inserted. Name responses include `bom_added`, including marker-only changes where `name_change.before == name_change.after` and the text diff is empty but `changed=True`. Repeated requests against an already marked file are no-ops; formal variant restore recovers the exact original marker/newline state. Native 8.1.6 VC controls independently confirm BOM-less Chinese-name mojibake and exact BOM-marked CSV/PDF text with identical hourly values; two name/marker transactions and their restores provide four further runs. A final Chinese-name/BOM plus equal-value tilt-formatting transaction and restore bring the total to eight native runs, confirming mixed scalar formatting uses an independent byte-marker variable. See [variant encoding verification](docs/project-source-verification.md#variant-name-and-explicit-utf-8-marker). Tool counts/categories are unchanged.

`pvsyst_update_variant_parameters(..., subarray_updates={"LIDLoss": 3})` edits an existing owning-root LID percentage (0-100, not a fraction), in verified 8.1.0/8.1.6 Grid subarrays without active shading/layout. `pvsyst_get_variant_parameters` reports this raw value. Missing/duplicate root fields and unsupported versions/systems stop before writing; nested metadata is neither mistaken for the owning field nor edited. The shared subarray/system field lookup now uses the existing object parser; component references and numeric declarations likewise ignore nested object decoys. Flags, components, other losses and sizing stay unchanged; no technology selection, inferred defaults or fitting. Native 3-percent and 100-percent controls demonstrate actual consumption; 0 restores baseline output. See [loss scalar controls](docs/grid-capacity-verification.md#lid-loss-scalar-native-verification). Subarray/orientation dictionaries accept strict JSON numbers, including integers, but not booleans or numeric strings; the backend additionally checks finite values and ranges. Existing preview/no-op/backup/restore semantics apply.

`subarray_updates={"ModuleQualityLoss": 5}` sets a semantic module-quality loss percentage for the selected subarray, with adapter range [-5,20] (negative means gain). It synchronizes the existing owning `PQualityModU` and `PQualityModI` fields and clears only the quality-default `Flags` bit `0x00100000`. It is not an invented serialized `ModuleQualityLoss` field: both saved U/I values and one valid hexadecimal owning Flags are required; missing/duplicate fields stop before writes. The raw inspector now includes `PQualityModU/I/P`, but those three are not independent edit keys. Native controls disprove a tempting scalar-only approach: changing `PQualityModP` to 5 is inert in default mode, and after selecting manual mode the unchanged U/I=3 still reports 3%; changing only one U/I from 3 to 5 reports 4%. The paired mapping produces the exact requested -5/0/5/20% PDF value and independently matched hourly results. Existing declared 8.1.0/8.1.6 Grid/no-active-geometry guards, strict numbers, preview/no-op/stale protection and exact restore apply. The response adds `module_quality.default_before/default_after`; `PQualityModP`, remaining flags, other losses, components and topology remain unchanged. Native defaults/tolerance inference, arbitrary independent U/I calibration, other ranges/models and GUI quality-loss round-trip are not claimed. See [quality binding and transactions](docs/grid-capacity-verification.md#module-quality-paired-binding-and-transactions). No new tool or argument: use the existing subarray dictionary and restart the server for refreshed inspection/description.

`subarray_updates={"PMismMPP": 5}` sets the existing owning-root module mismatch percentage (0-100) and explicitly selects manual mismatch by clearing only owning subarray Flags bit `0x00040000`. Declared 8.1.0/8.1.6 Grid subarrays without active shading/layout and with one valid hexadecimal Flags field are supported; missing/duplicate roots and unknown versions stop before writes. `pvsyst_get_variant_parameters` reports saved `PMismMPP` and raw `Flags`; the update response includes `mismatch.default_before/default_after`. A saved scalar alone is inert while the default bit is set: native output still uses its recalculated default. GUI exports and minimal field controls demonstrate that manual 5 percent is consumed. Other flags, components, losses and sizing remain unchanged; this is not statistical fitting, a native-default calculator or a coupled shading editor. Preview includes both value and mode changes; a repeated normalized value in manual mode is a no-op. Restore the transaction snapshot to recover the original default mode and saved value. Native version-declaration controls use CLI 8.1.6 loading 8.1.0/8.1.6 subarrays; they do not establish behavior of a CLI 8.1.0 executable. See [GUI mismatch binding](docs/grid-capacity-verification.md#gui-mismatch-defaultmanual-binding-verification) and [older-declaration transactions](docs/grid-capacity-verification.md#older-declaration-mismatch-transactions).

`pvsyst_update_variant_parameters(..., main_array_updates={"UFactor": 29})` edits the existing constant thermal heat-loss coefficient Uc in W/m2/K for the whole owning `pvMainArray`, not the selected subarray. The adapter accepts 15-100, strict finite JSON numbers, with one main-array `Flags=$00100400` and `Flags2=$082A`, Grid system, all owning subarrays declared 8.1.0/8.1.6 and no active shading/layout. These are bounded verified inputs, not a claim about PVsyst's entire native range or all thermal modes. `pvsyst_get_variant_parameters` exposes `main_array.values` with raw `UFactor`/`Flags`/`Flags2`; nested metadata is excluded. Missing/duplicate owning fields, other mode flags and wrong update scopes stop before writing. Existing preview/no-op/backup/restore are reused; normalized repeated values make no backup. Wind terms, absorptance, saved efficiency, components and model flags stay unchanged: no automatic fitting, inferred fields or transient/wind model editing. Native 15/20/29/100 controls verify changed array temperature and energy, and three writer transactions exactly match their controls and restore. A low-value 1 control has different behavior and is deliberately outside this adapter; its internal semantics are not inferred. See [main-array thermal verification](docs/grid-capacity-verification.md#main-array-constant-thermal-coefficient). Restart the MCP server to refresh the added optional argument in its tool schema.

`pvsyst_update_variant_parameters` also accepts `subarray_updates={"NInverter": 12}` for ordinary grid inverter groups (integer 1-10000), or `{"NStringCh": 80}` for parallel strings (positive 32-bit integer). With only `NInverter`, strings per inverter stay unchanged; with only `NStringCh`, inverter counts stay unchanged. Both may be set together. Relative group sizes and relative string allocation are preserved, and inverter/string `NElements` plus subarray `NInverter`, `NStringCh` and `NStrOrient1` are synchronized. Every resulting group and leaf count must be an integer. Preconditions include complete consistent grid counts, one fixed-plane orientation for the target, `NInvMPPT=1` without the independent `$0400` subarray flag, no explicit MPPT nodes, no shared inverter power and no shading geometry/tables. Empty shading flags `$00`, the official commercial sample's `$00020000` and the residential sample's `$00080000` are supported, still subject to geometry/table checks. Existing preview, backup and restore apply; the response includes `capacity` with before/after counts. Design hints such as `AreaSizing`/`PNomSizing`, cable definitions and component models remain unchanged. This is bounded proportional allocation, not electrical sizing or GUI derived-field fitting. Increasing parallel strings can increase inverter clipping; the editor does not invent voltage/current or DC/AC sizing approval. See [capacity verification](docs/grid-capacity-verification.md).

The existing `NModSerie` scalar changes series-module count, not parallel strings, inputs or mode. A native independent fixed-plane control verifies 15 to 18 modules per string: total modules 30 to 36, operating voltage at 50 C 427 to 512 V with current unchanged at 19 A, and changed hourly results. Preview/apply matches an independently authored reference and formal restore recovers source bytes. Angle edits additionally reuse ordinary-plane checks: a flat `FixedPlane`, explicit `Flags=$00` when present, and absent or unique `False` `UsesTiltAver`/`UsesAzimAver`/`UsesSlopeAver`. Active/ambiguous averaging, unknown/duplicate flags and nested geometry stop before writing; legacy angle records omitting `Flags` remain supported. Topology editing still requires explicit `$00`. See [angle edit guards](docs/grid-capacity-verification.md#non-averaged-fixed-plane-angle-edits). A shared geometry gate stops series, angle, count/mode and clone/remove edits before writes for active shading scenes/tables or any `PVObject_ModuleLayout` block, including a standalone layout with an empty shading scene. It preserves inputs and makes no backup on these stops; it does not silently retain stale module/string maps. An absent shading block remains allowed for series/angle scalar edits only. This is not automatic cold-Voc/MPPT sizing, loss fitting, shading remapping or proof for every system type. See [native series-module edit](docs/grid-capacity-verification.md#series-module-count-native-verification).

For verified independent exclusive layouts, `pvsyst_update_variant_parameters(..., subarray_updates={"NInverter": 2})` also scales each inverter group's multiplicity proportionally. Root `NInverter`, total `NInvMPPT`, `NStringCh` and `NStrOrient1` synchronize; MPPT/parallel-leaf multiplicities, mode, components and loss/nominal fields stay unchanged. Requires Version=8.1.0/8.1.6 `$0400`, explicit unshared `-1`, complete direct-input ownership/counts and all strings in one unshaded fixed plane. Every changed inverter `NElements` must already exist and every group/input/string allocation must remain integral. Do not combine independent inverter scaling with `NInvMPPT` or `NStringCh`; shared parents remain closed. CLI determines the resulting model power; declared diagram quantities are not hardware sizing or electrical certification. See [native independent inverter edit](docs/grid-capacity-verification.md#independent-mppt-inverter-count-edit) and [GUI-saved 8.1.6 quantity controls](docs/grid-capacity-verification.md#gui-saved-816-input-and-inverter-quantity-editing).

For existing independent inputs, `pvsyst_update_variant_parameters(..., subarray_updates={"NInvMPPT": 2})` accepts integers 1-10000 in verified Version=8.1.0/8.1.6 `$0400` subarrays with explicit `PowerSharingInverterId=-1`. It requires complete, consistent, exclusively owned explicit MPPT branches, one unshaded fixed plane and all strings in that plane. Each direct MPPT group's `NElements` scales proportionally while inverter and parallel-string leaf counts remain unchanged; root `NInvMPPT`, `NStringCh` and `NStrOrient1` are synchronized. Changed node counts must already be explicit and every allocation must remain integral. This operation preserves mode, components, saved nominal tags, resistance and loss parameters; it does not switch modes, fit those parameters or certify converter hardware limits. Do not combine it with `NInverter`, `NStringCh` or `NoOrientation`. Preview/backup/restore use the existing transaction, and `capacity` adds `mppt_inputs_before`, `mppt_inputs_after` and `mppt_groups`. See [native edit verification](docs/grid-capacity-verification.md#independent-mppt-quantity-edit) and [GUI-saved 8.1.6 quantity controls](docs/grid-capacity-verification.md#gui-saved-816-input-and-inverter-quantity-editing).

`subarray_updates={"NStringCh": 4}` also proportionally scales explicit parallel-string leaves in verified Version=8.1.0/8.1.6 independent `$0400` mode, retaining all MPPT and inverter multiplicities and the mode. The same unshared/exclusive/fixed-plane conditions apply; changed leaf `NElements` must exist and every allocation must remain integral. Total `NStringCh`/`NStrOrient1` are synchronized, with before/after input counts unchanged in `capacity`. Do not combine it with `NInvMPPT` or `NInverter`; arbitrary unequal input redistribution is not inferred. Resistance/nominal/loss tags remain unchanged, so higher DC losses or clipping may result. See [independent string edit](docs/grid-capacity-verification.md#independent-parallel-string-edit) and [GUI-saved 8.1.6 string controls](docs/grid-capacity-verification.md#gui-saved-816-parallel-string-editing).

`NStringCh` alone also supports a selected member of an existing bounded Version=8.1.0/8.1.6 shared parent, with matching member versions, common unshaded fixed plane, complete consistent owned input/string counts and group 0 or all -1. Each member must reference only that parent, and active group 0 has no outside references. Only the selected member's root string declarations and owned string leaves scale; peer members, MPPT/inverter multiplicities, sharing flags/IDs and saved nominal tags stay unchanged. `capacity.shared_parent_line` identifies this scope. Input count/inverter resizing remains closed for shared parents. CLI's automatic sharing may rebalance effective power even though cached nominal tags are unchanged; this is not a manual budget editor or GUI refit. See [shared member string verification](docs/grid-capacity-verification.md#shared-member-string-edit).

Use `pvsyst_update_variant_parameters(..., subarray_id=1, power_sharing_enabled=true)` or `false` to toggle all members of an existing bounded shared parent, selected through any member ID. This is a separate transaction: complete consistent counts, Version=8.1.0/8.1.6 independent `$0400` members with matching declared versions and identical inverter references, a common unshaded fixed plane and all member strings in that plane are required. Each member must reference only that parent; no outside array may already use group 0. The editor synchronizes `PowerSharingInverterId=0/-1` and the `$0100` flag bit, preserving other bits, topology, counts, nominal tags, components and loss parameters. Native automatic allocation is used; arbitrary groups, manual power allocation, mixed planes and creating/merging parents remain unsupported. Preview/backup/restore are reused; `power_sharing` reports member IDs and enabled state before/after. See [native toggle verification](docs/grid-capacity-verification.md#existing-power-sharing-toggle).

Use `independent_mppt_enabled=true` or `false` in a separate `pvsyst_update_variant_parameters` transaction to switch an existing explicit layout. Requires Version=8.1.0/8.1.6, valid Flags, explicit `PowerSharingInverterId=-1`, complete exclusively owned direct MPPT children, all strings in one unshaded fixed plane, and no shared parent. The editor changes only `$0400` and `NInvMPPT`: enabled uses the total explicit modeled inputs, disabled uses ordinary `1`. Layout, strings, serialized inverter count, components, other flag bits, nominal tags and losses are preserved. This intentionally changes native power interpretation; the serialized inverter count is not refitted into a physical installation count. Implicit/mixed/shared layouts, unsupported modes and simultaneous sharing/scalar updates stop before writes. `mppt_mode` reports before/after state/input declarations, with existing preview/backup/no-op/restore behavior. See [native mode-switch verification](docs/grid-capacity-verification.md#explicit-mppt-mode-switch) and [GUI-saved 8.1.6 mode switching](docs/grid-capacity-verification.md#gui-saved-816-exclusive-mode-switching).

Do not equate arbitrary MPPT diagram redistribution with a physical simulation edit. The [same-subarray allocation control](docs/grid-capacity-verification.md#same-subarray-unequal-leaf-control) compares grouped 2×2 strings, split 2+2 and split 3+1 with identical subarray roots: all structure counts pass, all 48 hourly rows match and the native report still shows `2 * MPPT 50%`. This fixture therefore does not establish unequal per-input physics from node edits. PVsyst models homogeneous subarrays; physically distinct input configurations need appropriate separate subarray modeling. No arbitrary redistribution editor is exposed on the strength of diagram-only changes.

`pvsyst_validate_variant_structure` checks orientation IDs/references, counted `TOrientGroup` lists when present, circuit subarray references/names and serialized inverter/string quantities. Dome/unknown list quantities remain explicitly unchecked. Its `grid_circuit` result lists inverter groups, per-subarray declared and circuit counts, separate module totals, and explicit MPPT-node counts. Multiple inverter groups may belong to one subarray, including unequal string allocations. Missing `NElements` means one in the supported serialization. The [bounded shared-parent format](docs/grid-capacity-verification.md#bounded-shared-parent-structure-inspection) is checked for Version=8.1.0/8.1.6 independent members with matching declared versions and identical inverter references and common group 0 or all -1: direct owned MPPT/string subtrees and canonical aggregate names are required. Shared groups return `subarray_ids`/`power_sharing_id`, member rows return `shared_inverter_lines`, and aggregate inverter counts deduplicate parents rather than summing overlapping member references. Other group IDs/models/schemas or unknown allocations report incomplete checks without misleading aggregate totals; malformed structures and count disagreements report issues. `valid` is false for issues, null for incomplete checks, and true when these scoped checks pass. MPPT nodes alone do not establish independent-MPPT mode; `NInvMPPT` is reported separately. For verified Version=8.1.0/8.1.6 subarrays with the independent `$0400` flag, `independent_mppt_inputs_declared` and `explicit_mppt_inputs_match_declared` compare complete explicit layouts against `NInvMPPT` directly, not `NInverter * NInvMPPT`. Whole/unknown modes and implicit layouts leave the match unset. Version 8.1.6 is recognized by input diagnosis, exclusive mode switching, proportional unshared inverter/input/parallel-string editing and bounded exclusive fixed-plane copying/reassociation/removal; bounded shared-parent diagnosis/construction, sharing toggles, member strings and disabled two-member removal also support homogeneous 8.1.0 or 8.1.6 declarations. Mixed member versions remain unchecked. See [GUI-saved shared lifecycle controls](docs/grid-capacity-verification.md#gui-saved-816-bounded-shared-parent-lifecycle). See [native quantity controls](docs/grid-capacity-verification.md#independent-mppt-quantity-controls) and [GUI-saved version controls](docs/grid-capacity-verification.md#gui-saved-816-independent-input-diagnosis). This does not establish which inconsistent input the simulator consumes, certify electrical sizing, or repair the file. See [grid circuit verification](docs/grid-circuit-verification.md).

Warnings flag shading-table geometry for real simulation review. `pvsyst_clone_subarray` duplicates the selected `pvSubArray` object and all complete, exclusively owned grid inverter groups. It assigns the next ID above the highest currently present project-variant ID, keeps the same fixed-plane orientation and serialized MPPT layout, and saves an original-byte backup. Only owning subarray/node IDs and names are remapped; cable metadata and relative group quantities stay unchanged. `pvsyst_remove_subarray` removes that subarray and all its owned groups only while another subarray still uses the orientation. Multiple-group edits require fully checked consistent grid counts; the existing isolated single-branch layout remains supported. Both require a current variant hash and stop for active shading geometry/tables, mixed/shared branches, shared inverter power anywhere in the variant, external target-ID references or unsupported systems. Empty shading flags `$00`, `$00020000` and `$00080000` are supported. Explicit MPPT nodes may be copied only as part of complete exclusive branches, preserving their saved layout and mode; this does not enable independent MPPT or power sharing. See [MPPT branch preservation](docs/grid-capacity-verification.md#preserving-explicit-mppt-branches) and [mode-bit controls](docs/grid-capacity-verification.md#controlled-mppt-mode-bit-probe). Grid diagnostics include raw root `Version`, `Flags`, `PNomAC_InMPPT` and `PowerSharingInverterId`; saved power tags are not necessarily the effective power used by the native model. The [shared-power controls](docs/grid-capacity-verification.md#cross-subarray-sharing-controls) show group-ID activation and native recomputation of proportional allocation; bounded existing-group/member editing is available below; arbitrary shared-group allocation remains pending. Inputs are re-read before backup and replacement to detect preparation-time external changes; this is optimistic checking, not exclusion of external editors after the final check. Known independent-input declaration conflicts stop structural copying/removal, including an otherwise isolated single-branch layout. Both responses include `inverter_group_count`. They do not redesign inverter/MPPT topology, synchronize 3D geometry, or infer component sizing; validate structure and compare simulation outputs after edits. See [grouped subarray verification](docs/grid-capacity-verification.md#grouped-subarray-copy-and-removal).

Use `pvsyst_clone_subarray(..., share_inverter=true)` to construct a bounded shared parent by copying one source MPPT subtree into its current inverter rather than duplicating the inverter. Requires complete Version=8.1.0/8.1.6 independent `$0400` counts, explicit disabled sharing `-1`, one exclusive inverter and one direct input (both multiplicity 1), all strings in one unshaded fixed plane. The new member uses the existing project-wide fresh ID/name; parent owner becomes 0 and name joins both member Comments. Both members retain disabled power sharing and copied mode/parameters; enabling sharing is a separate parameter transaction. The result adds `shared_parent_created`. It is separate from `clone_orientation`, does not merge arbitrary existing branches, infer available hardware ports or refit losses. Backup/restore remain available; use the explicit `shared_member` deletion option for its bounded inverse. See [native shared-parent construction](docs/grid-capacity-verification.md#shared-parent-construction-by-clone).

Use `pvsyst_remove_subarray(..., shared_member=true)` as the bounded inverse: delete either member of a complete disabled two-member parent with explicit owner 0, one inverter/input per member and common unshaded fixed plane. All power-sharing references must be disabled; target ID references outside its owned snapshot/MPPT subtree stop before writes. The other member's root/input/string quantities are unchanged and the parent owner/name becomes exclusive to it. `remaining_member_id` identifies the survivor (null for ordinary removal). Do not combine with `remove_orientation`; no general active-sharing reallocation, arbitrary group pruning or ID renumbering is performed. Default false retains existing exclusive-branch behavior. See [native member-removal verification](docs/grid-capacity-verification.md#shared-member-removal).

Use `pvsyst_clone_subarray(..., clone_orientation=true)` to append a separate fixed plane for the new subarray and synchronize its `NoOrientation` plus the orientation-list count. It requires complete consistent grid counts, all source strings in one orientation, a flat ordinary `Flags=$00` non-averaged source record and sequential ordered `TOrientGroup` IDs. Source branches can be ordinary without explicit MPPT, or verified Version=8.1.0/8.1.6 independent `$0400`, disabled sharing `-1`, direct exclusive MPPT children and complete declared input counts. Copied input/leaf multiplicities, mode and loss/model fields stay unchanged. Independent-plane copying also stops on circuit-level orientation references. Final-plane removal supports these verified exclusive branches; non-final reindexing also supports these verified independent layouts when all plane/root references are owned as described below. Flat/non-averaged plane, exclusive allocation and owned-reference checks apply even to final-plane removal; only numeric reindexing is conditional. See [independent MPPT plane verification](docs/grid-capacity-verification.md#independent-mppt-fixed-plane-cloning) and [GUI-saved 8.1.6 plane controls](docs/grid-capacity-verification.md#gui-saved-816-exclusive-fixed-plane-lifecycle). The result includes `orientation_id` and `orientation_cloned`; edit that ID with the existing `orientation_updates={"FieldTilt": 60}` / `FieldAzim` fields. Empty shading `$00020000`/`$00080000` is also accepted for angle edits after geometry/table checks. To remove a subarray and its now-unused fixed plane together, pass `remove_orientation=true`; another plane must remain. A non-final deletion also shifts higher `NoOrient` IDs down by one and synchronizes every owning subarray's `NoOrientation`. This path requires all planes flat/non-averaged, complete exclusive branches with all strings in one orientation, either ordinary `NInvMPPT=1` without explicit MPPT or verified Version=8.1.0/8.1.6 independent `$0400`/direct-MPPT/disabled `-1` layouts with consistent inputs. Every orientation field must belong to its owning plane/root; circuit-level and external orientation fields stop before writes. Shared, externally referenced or non-sequential planes stop before writes. Default false preserves the original shared-orientation behavior. The delete result includes `removed_orientation_id` (null by default) and `orientation_id_map` (changed surviving IDs only, empty for final-plane/default deletion). See [non-final deletion verification](docs/grid-capacity-verification.md#non-final-fixed-plane-removal) and [independent MPPT non-final deletion](docs/grid-capacity-verification.md#independent-mppt-non-final-plane-removal). This is template copying and append/prune, not tracking/dome construction, arbitrary plane reordering or mixed-orientation allocation, derived-field fitting or GUI round-trip certification. See [independent fixed-plane verification](docs/grid-capacity-verification.md#independent-fixed-plane-lifecycle).

For an active bounded two-member shared parent, use the existing explicit workflow: save current inspection, call `pvsyst_update_variant_parameters(..., power_sharing_enabled=false)`, then `pvsyst_remove_subarray(..., shared_member=true)` with the returned hash. Direct deletion while sharing is active still stops without writes. To recover the active parent, restore the deletion snapshot first (recreates disabled members), then restore the sharing-toggle snapshot with the returned hash (re-enables the original group). This is two visible guarded transactions, not one atomic or silently disabling deletion; intervening edits must still satisfy each SHA precondition. All other sharing must be disabled and the existing bounded ownership/geometry checks still apply. See [native active-group lifecycle](docs/grid-capacity-verification.md#active-shared-member-explicit-lifecycle).

Reassign an existing subarray with `pvsyst_update_variant_parameters(..., subarray_id=3, subarray_updates={"NoOrientation": 2})`. This requires sequential counted fixed-plane IDs, flat `Flags=$00` non-averaged source/target records, complete consistent grid counts and exclusively owned branches: ordinary `NInvMPPT=1` without explicit MPPT, or verified Version=8.1.0/8.1.6 independent `$0400` with disabled sharing `-1`, direct MPPT children and consistent declared input counts. No circuit-level orientation references are supported; require all strings in one orientation, and no shading geometry/tables. Every defined plane must retain at least one assigned subarray; moving its sole user stops before writing instead of leaving a native-invalid orphan or silently renumbering planes. Preview/backup/restore remain available; `orientation_assignment` reports before/after IDs. Capacity fields may be combined in the same transaction. Reassociation alone preserves list counts, plane records, circuit quantities, components and design hints. Structure validation also diagnoses unused planes in supported counted lists. See [fixed-plane reassociation](docs/grid-capacity-verification.md#fixed-plane-reassociation) and [independent MPPT reassociation](docs/grid-capacity-verification.md#independent-mppt-fixed-plane-reassociation). `NInvMPPT` resizing stays a separate transaction from `NoOrientation`; neither operation reallocates shared inputs, fits losses or certifies hardware pairing.

Component tools support **four types only**: `.PAN` (`ComposPV/PVmodules`), `.OND` (`ComposPV/Inverters`), `.BTR` (`ComposPV/Batteries`) and `.GEN` (`ComposPV/Gensets`). `pvsyst_create_component` takes the complete UTF-8 `PVObject_` text and a new filename; it does not require an existing file. `pvsyst_clone_component` is a shortcut for a complete same-type text template and requires new `Manufacturer` and `Model` values. `pvsyst_copy_component` preserves bytes, including legacy mixed binary `.PAN` files from the installed read-only `DataRO` library. Old `.PAN` files can be listed, compared and copied, but not text-edited. Installed encrypted `*DB.csv` component databases cannot be enumerated; a project reference absent from loose files is reported as `unknown-or-builtin-db`, not as missing. The installed loose-file library is discovered under the CLI installation's `DataRO/*/ComposPV` independently of the user workspace name. If multiple versions are installed there, set `PVSYST_BUILTIN_COMPONENTS` to the full `ComposPV` directory to select one. A `.GEN` file reference alone does not activate a generator; dependency inspection also reports the variant's `$20` flag, `PEffBackUp` value and each `pvSubArray` backup threshold with its line number. Threshold-present booleans summarize whether **any** subarray contains the corresponding field.

`pvsyst_get_component` additionally returns `electrical` for text PAN/OND: reference-point fields with numeric value, unit, owning path and declaration status. PAN power is W; OND Converter power is kW; voltage/current use V/A. Missing, ambiguous or invalid values stay explicit, and `IDCMax=0` is not treated as a positive declared limit. Raw serialized temperature coefficients and per-MPPT current/allocation limits remain unverified. `readable` describes successful object reading, not a passed electrical design. Optional `capacity` on `pvsyst_simulate_template` now plans/configures bounded integer counts against explicitly supplied design voltages/currents, full uniform MPPT allocation, module voltage rating and DC:AC limits. It does not certify the supplied envelope, native fitting, other hardware/wiring constraints or loss refitting. See [capacity planning and evidence](docs/capacity-sizing.md).

`pvsyst_update_component` and `pvsyst_clone_component` allow only the following existing scalar fields; create-from-text accepts a complete component:

| Type | Editable fields |
|---|---|
| PAN | `Manufacturer`, `Model`, `DataSource`, `PNom`, `ISC`/`Isc`, `Voc`, `Imp`, `Vmp`, `MuISC`/`muISC`, `muVocSpec` |
| OND | `Manufacturer`, `Model`, `DataSource`, `PNomConv`, `PMaxOUT`, `VMppMin`, `VMPPMax`, `VAbsMax`, `EfficMax`, `EfficEuro` |
| BTR | `Manufacturer`, `Model`, `DataSource`, `CapNomC10`, `CapaRef`, `AlphaSOC` |
| GEN | `Manufacturer`, `Model`, `DataSource`, `TypeGen`, `PNomGen`, `CFuelHor` |

`pvsyst_update_component` also accepts `expected_sha256` and `dry_run`. Read the
component first, then pass its `sha256` as `expected_sha256` to preview or apply
an edit. `dry_run=true` validates the candidate and returns a bounded `diff`,
`before_sha256`, candidate `sha256`, `changed` and static `validation` without
writing a file or creating a backup. To apply the preview, repeat the same
updates with `dry_run=false` and the **original** `before_sha256`; the candidate
hash is not the precondition. Stale edits are rejected before writing. The
optional hash keeps older scalar calls compatible; `conflict_checked` tells
clients whether that precondition was supplied. No-op updates return
`changed=false` without a `backup_name`. Real edits retain automatic recovery
snapshots and preserve the BOM, line endings and assignment whitespace.
Rechecks also detect intervening file changes during preparation; the local
lock and hash checks do not provide an OS-level lock against external editors.

For text **Version=8.1.6** components of all four types, inspection also returns
`commercial`: existing values, form field types/units, per-field editability,
and up to five remark lines. `pvsyst_update_component` accepts a
`commercial_updates` dictionary and a `remarks` string array. These require
`expected_sha256` and use the same preview, atomic file replacement and backup
as scalar/curve changes. Supply `updates={}` for a form-only edit.

```json
{
  "component_type": "PAN",
  "filename": "custom.PAN",
  "updates": {},
  "commercial_updates": {
    "Manufacturer": "Example", "Model": "New model", "DataSource": "Datasheet",
    "Width": "1.134", "Height": "2.278", "Depth": "0.035", "Weight": "28.6",
    "YearBeg": "2026", "NPieces": "1", "PriceDate": "30/09/26 09:00"
  },
  "remarks": ["User-defined component", "Verified datasheet revision"],
  "expected_sha256": "<sha256 from inspection>",
  "dry_run": true
}
```

Dimensions are in metres and weight is in kilograms. These form fields may
be added when absent; unknown fields and nested commercial objects are refused.
PAN `Width` and `Height` must be positive and also update root `LargApp` and
`LongApp` when those fields exist. GUI 8.1.6 exports may contain only commercial
dimensions; the editor accepts that form and does not add the absent legacy
fields. Inspection reports `paired_root_present`. Other dimensions and weight allow zero
for unspecified values. `YearBeg` is 0–9999, `NPieces` is a positive 32-bit
integer, and nonempty `PriceDate` must be a valid `dd/mm/yy HH:MM` timestamp.
Strings have a 2048-character limit and cannot contain line breaks or controls.
Empty `DataSource` and `PriceDate` are permitted. Identity edits synchronize
the first two fields of a tab-separated root `Comment`, preserving its remaining
status text. A free-form root comment is retained.

PAN `ISC`/`Isc` and `MuISC`/`muISC` are explicit aliases. Either input spelling
updates the existing root field while preserving its serialized key. Inspection
keeps raw field names and also returns `scalar_aliases`. Both spellings in one
file/request are rejected as ambiguous. Physical values are checked in the root
object; manufacturer/model and form dimensions are checked in the commercial
object. Missing or invalid dimensions and duplicate physical aliases still fail
validation. These changes allow create/copy/clone/edit of the observed GUI 8.1.6
PAN format; they do not reproduce GUI fitting, rounding or derived-field updates.
See [GUI PAN compatibility verification](docs/gui-pan-verification.md).

Omitting `remarks` preserves it; `[]` removes its block. A supplied list replaces
the array, count, consecutive row names and closing value together. Same-value
arrays retain original formatting. PAN's fifth remark can encode a model
association or functional option, so this is not always merely descriptive text.
Use `commercial_updates` for identity fields when combining form and other
edits; do not repeat a field in both dictionaries. Seller-specific prices,
currencies, model fitting and GUI round-trip equivalence are outside this form.
See [commercial verification](docs/commercial-verification.md) and the
[GUI capability gap and reverse-engineering inventory](docs/gui-capability-roadmap.md).

`pvsyst_get_component` returns the scalar allowlist as `editable_fields` and a
bounded OND/BTR `curves` inventory. Each curve has its object path
(for example `Converter/ProfilPIO`), line number and points declared effective
by `NPtsEff`; padding after that count is excluded from the displayed points.
Missing or duplicate count/point fields are reported as curve diagnostics.
Inspection is limited to 16 curves and 256 allocated points per curve. These
limits describe this reader, not PVsyst format limits. `structure_complete`
only describes serialized count/point consistency. For OND, `curves.control` reports
the root `Flags`, file version, `source` (`automatic`, `file` or `unknown`),
three-voltage selection and edit restrictions. `editable` and `edit_errors`
describe support for each profile; `simulation_effect="unverified"` means
that the particular inspected model has not been simulated by this reader.

`pvsyst_update_component` accepts `curve_updates` for **OND Version=8.1.6,
single-voltage `Converter/ProfilPIO`, Mode=1 only**. Supply the complete new table
of 4–256 active points as `[input_watts, output_watts]`.
X must strictly increase, and each point must contain
finite numbers with `0 <= output <= input`. Strings and booleans are rejected.
X must be at least `1e-9` and consecutive X values at least `1e-8` apart to
avoid native point elimination. Unrelated curves remain unchanged.
Other file versions, curve modes and three-voltage profiles remain read-only.

When `point_count_editable=true`, the new table can add or remove points.
Changing the active count synchronizes `NPtsMax`, `NPtsEff` and exactly that
many `Point_N` rows, removes the old inactive padding and serialized coefficients,
and preserves `Mode`/`LastCompile`. Native Mode 1 compilation derives the
segment coefficients from the new X/Y table. This requires consecutive,
numerically ordered rows, count/mode/compile fields before the table, and
`LastCompile` absent or hexadecimal `0`, `19`, `008D` or `8089`.
Inspection reports `allocated_point_count`, `point_count_range` and
`point_count_errors`. Previews and writes report `curve_counts_before/after`.
When the active count stays the same, count fields and inactive rows retain
their bytes, and only changed active rows are replaced. Two-value X/Y and
five-value X/Y/a/b/c serialized rows can be inspected; the editor accepts X/Y
input only. See [point-table verification](docs/curve-table-verification.md).

Curve edits require the current `expected_sha256`. For an automatic curve,
explicitly pass `use_file_curve=true` with the supplied points to clear only
bit 4 (`0x10`) of the **root** inverter Flags. Nested commercial Flags and all
other bits are preserved. Without that opt-in, automatic-mode edits are
rejected because the software can ignore the supplied points. An inverter
already using its file curve needs no mode switch. `use_file_curve` is not a
standalone flag editor and cannot re-enable automatic mode; restore the backup
to undo the entire edit. `curve_control_before` and `curve_control_after` make
the switch visible in both previews and applied results.

For example, after inspecting an editable four-point synthetic inverter:

```json
{
  "component_type": "OND",
  "filename": "custom.OND",
  "updates": {},
  "expected_sha256": "<sha256 from inspection>",
  "curve_updates": {
    "Converter/ProfilPIO": [[100, 80], [200, 180], [500, 470], [1000, 950]]
  },
  "use_file_curve": true,
  "dry_run": true
}
```

Scalar and curve changes can share one preview, write and backup. No-op point
edits in file mode retain the original numeric spelling and create no backup.
Editing points does not refit efficiency scalars, thresholds, spline metadata
or other derived parameters. Validate the resulting model with PVsyst.

### Curve CSV import and export

For OND/BTR, call `pvsyst_get_component` with `include_curve_csv=true` to add
`curve_csv`, a map from supported curve paths to CSV text. It contains active
X/Y points only. `curve_csv_errors` explains omitted curves with unknown axes
or incomplete structure; the usual inspection still reports their details.
Exporting a readable curve does not make unsupported models/modes editable.

Pass one entry of that map to `pvsyst_update_component(curve_csv=...)` as an
alternative to `curve_updates`. It shares the same hash, activation, point-count,
model-value, preview and backup checks and can accompany scalar/commercial edits.
Supplying both curve input arguments is rejected. After BTR activation, use the
canonical `CapaCourant` path from the latest inspection/export.

| Curve | Required CSV header |
|---|---|
| OND main power curve | `input_watts,output_watts` |
| BTR capacity curve | `discharge_hours,relative_capacity` |

BTR relative capacity is relative to C10. CSV must contain the exact two-column
header and 4–256 data rows, using comma separators and decimal dots. Finite
decimal/scientific notation, standard CSV quoting, LF/CRLF and an initial UTF-8
BOM are supported. Blank rows/cells, formulas, extra columns, guessed units and
locale-specific separators are rejected. Each table is limited to 64 KiB of
UTF-8 text. These arguments transport CSV **contents**, not a filesystem path;
clients may save/read the text with their file tools.

For example, use this argument with the previous OND target/hash/activation:

```json
{
  "curve_csv": {
    "Converter/ProfilPIO": "input_watts,output_watts\r\n100,80\r\n200,180\r\n500,470\r\n1000,950\r\n"
  }
}
```

An export immediately re-imported in file mode is a byte-identical no-op when
the existing points satisfy the edit restrictions. The CSV form does not bypass
errors in existing points or certify their physical meaning.

### Battery capacity curves

For **BTR Version=8.1.6, `Pb_Sealed_AGM` or `Pb_Sealed_Gel`, Mode=1**, the same
`curve_updates` argument edits the existing root capacity curve. Its points are
`[discharge_duration_hours, capacity_relative_to_C10]`. Use its current path
from inspection: either `CapaCourant` or `Capa_DischRate`. Case-insensitive
technology names and the optional `bt` prefix are accepted; space-separated
technology names and other chemistries are unsupported.

`Capa_DischRate` is not recognized by the tested CLI. To activate it, supply
`use_file_curve=true` **alongside a complete valid set of points**. This renames
only that block to `CapaCourant`. A canonical `CapaCourant` needs no activation.
Both names present, duplicate blocks and unsupported modes
are rejected. No other battery curve tags, chemistry fields or Flags are changed.
After activation, use `CapaCourant` and the new hash for subsequent edits.

Battery capacity edits require the current hash and 4–256 active points.
The same count-change and X-spacing rules above apply. X and Y must be positive,
X strictly increasing, and Y nondecreasing.
The X range must contain 100 hours. The linear interpolation at 100 hours,
`C100/C10`, must lie in **[1.15, 1.45]** for these supported technologies.
Extrapolation is deliberately unsupported by this editor even though the CLI
can extrapolate; simply renaming a short or unsuitable curve may otherwise
make the battery invalid. These checks do not fit or certify a battery model.

BTR inspection returns `cli_tag`, `cli_recognition`, `ratio_100h` and
`value_errors` in addition to points and edit preconditions. `editable` means
the structure can accept replacement points; current-value errors may still
need repair. `curves.control` describes the capacity curve's current path,
recognition, supported technology and ratio interval. Other BTR curves are
inspection-only, including the GUI/CLI name pairs `SelfDisch_Temp`/`IAutoShape`,
`Capa_Temperature`/`CapaTemper`, and `VMaxCharge_Rate`/`VMaxChargeRate`.
`cli_recognition="recognized"` identifies a known tag, not proof of a valid
model or simulation effect.

For example, for an inspected battery with six active capacity points:

```json
{
  "component_type": "BTR",
  "filename": "custom.BTR",
  "updates": {},
  "expected_sha256": "<sha256 from inspection>",
  "curve_updates": {
    "Capa_DischRate": [[5, 0.85], [10, 1], [25, 1.1], [70, 1.25], [130, 1.35], [200, 1.4]]
  },
  "use_file_curve": true,
  "dry_run": true
}
```

This synthetic example illustrates the interface, not a recommended battery
model. Preview first; applying the same arguments with `dry_run=false` uses
the original hash and creates a byte-identical recovery snapshot.

Optional live preview/apply verification is available in
`tests/smoke_live_component_editor.py`. It copies an explicitly selected
workspace into separate baseline/edited directories, halves the selected
inverter's `PNomConv`, runs two two-day simulations, restores the edited file
byte-for-byte and verifies that the source workspace did not change. It keeps
CSV outputs, the preview and `evidence.json` in the specified lab directory.
Set the environment variables documented at the top of that script. This
test requires installed PVsyst, a compatible grid project and simulation quota;
the ordinary unit tests and `tests/smoke_components.py` use synthetic fixtures.

`tests/smoke_live_ond_curves.py` uses the same environment variables to perform
two real MCP stdio simulations: file mode with original ordinates, then file
mode with ordinates multiplied by 0.9. It compares rows with equal DC input and
equal exported non-operating losses, and verifies previews, exact restoration
and unchanged source files. PVsyst 8.1.6 produced seven matched-input rows with
90% AC output within CSV rounding. Total energy also changed, but its direction
is not an acceptance criterion because limiting states can change. See
[the verification record](docs/ond-curve-verification.md) for scope and limits.

`tests/smoke_live_battery_curves.py` performs the corresponding BTR test through
MCP in two independent workspace copies. Supply `PVSYST_EDITOR_BATTERY` and
`PVSYST_BATTERY_POINTS` (a JSON file of all active pairs) with the other variables
documented in the script. Both the supplied curve and its Y×0.9 version must
pass the editor's checks. The test compares hourly effective capacity in Ah,
checks irradiation/timestamps, restores both files and verifies source hashes.
See [the BTR verification record](docs/btr-capacity-verification.md) for results.

Component creation and edits check object structure, required fields and a limited set of numeric constraints. They do **not** certify the physical model: exercise a project variant that actually references the new component and inspect the resulting CSV. `TypeGen` is a free-text field accepted by the tested PVsyst version, unlike the battery technology enum `BattTechnol`, which is deliberately excluded from scalar editing. Inverter curves and battery chemistry are not transformed when cloning; curve edits use the separate `curve_updates` argument with the limits above, and `CFuelHor` must be positive because zero was observed to crash PVsyst. Text cloning and editing retain a UTF-8 BOM when the source has one. Component inspection returns at most 200 lines per page: `page_truncated` indicates more lines after the current page, `line_truncated` indicates a displayed line exceeded 2000 characters, and `lines_truncated` is true if either occurs. Edits automatically create byte-identical backups under `ComposPV/.mcp-backups`; `pvsyst_archive_component` requires `confirm=true`, scans PRJ/VC files recursively under workspace `Projects` (without following linked directories), blocks references and moves the file to `ComposPV/.mcp-archive`. Reference checks stream project files up to 32 MB; larger files or unreadable/symlinked project paths block archiving so that it does not proceed with an incomplete check. Both rollback tools require `confirm=true`. All writes stay in the user workspace; `DataRO` is read-only. No vendor component data is shipped in this repository.

Place an existing SFI under `Models`, or call `pvsyst_build_sfi` first. A CSV with no SFI/RVT export definition may contain dates only. For a single `pvsyst_run_simulation` call, `csv_name="run01"` and `csv_name="run01.csv"` both select `Results/run01.csv`; input filenames such as `sfi_name` still require their extension. Output filenames cannot contain paths, and generated files are never overwritten. For `create-site`, supply altitude, timezone and country code to avoid its optional online location lookups. License mutation tool arguments can be retained by the MCP client; do not echo keys in logs. When `lic-info` provides no usable status or expiration, `pvsyst_license_info` reports `UNSPECIFIED` rather than assuming the license is active.

Project names accept safe Unicode and dots consistently across project, variant and component-dependency tools. Different full stems identify separate projects; unknown same-stem sidecars (including attachments such as `Example.VC0.bak`) still block lifecycle operations. Project and variant inputs **and edited candidates** share the 32,000,000-byte limit; an oversized candidate is rejected before creating backups or changing files. Project archives allow at most 128 PRJ/VC members, checked before any move.

Source-update and source-restore results include `transaction_id` and `transaction_manifest`. Keep that JSON manifest alongside the returned `backups` under `Projects/.mcp-variant-backups`. The existing restore arguments are unchanged: the server identifies the manifest from the backup names and verifies the project identity, complete member map and every original SHA-256 before writing. Mixed transactions, modified backups or missing manifests are rejected. Source backup sets created by earlier versions have no transaction manifest and require manual recovery after checking their provenance; their bytes remain on disk. These local hashes detect mismatches, not malicious replacement of both data and metadata.

Component recovery restores original bytes even if they fail the current model checks; the returned `validation` contains format, structural, missing-field and known-value diagnostics. Creation, import and edits retain their existing model checks. New component snapshots use `filename.v2-<id>.bak` or `.archived` plus a `.json` metadata file recording type, original filename and SHA-256. Keep both files together; recovery verifies them and returns `integrity_verified=true`. Legacy component snapshots remain recoverable with `integrity_verified=false` because no original hash was recorded. A successful byte restore does not certify that the restored model is suitable for simulation.

Structure validation reports missing `pvSystem` sections and missing, empty or duplicate `SystemType` fields as issues. Its `scope` covers orientation, supported grid circuit references and serialized inverter/string counts. `checks_complete` and `unchecked_checks` disclose skipped circuit checks; `valid` is `false` when an issue is found, `null` when checks are incomplete without a known issue, and `true` only when the checks within that scope pass. Shading geometry and physical model verification remain separate.

### Comparing result files

The existing `pvsyst_read_results` tool compares two saved SFI CSVs when
`compare_to` is supplied. The difference is always **target minus baseline**:

```json
{
  "csv_name": "new-design.csv",
  "folder": "Results",
  "compare_to": "reference.csv",
  "compare_folder": "UserHourly",
  "columns": "E_Grid,PR"
}
```

`compare_folder` defaults to `folder`. Both paths remain workspace-scoped.
Select 1–64 distinct numeric columns. Both files must have identical explicit
units, identical timestamps in increasing order and the same row count. Column
order may differ. Units are not converted and timestamps are not resampled.
Malformed timestamps, duplicate column names and extra row values are rejected.

Comparison mode returns `mode="comparison"`, both file hashes, date range,
cadence, and per-column statistics. `paired_*` sums and relative change use only
timestamps where **both** values are finite. Separate baseline/target sample
counts, `missing_pairs` and `paired_coverage` expose missing data. Relative change
is 100 × paired delta sum / paired baseline sum, or null when that denominator
is zero. With no pairs, sums and extrema are null. `max_abs_delta_at` identifies
the first timestamp with the largest absolute difference.

For W/kW columns at a regular cadence, `observed_delta_energy_kwh` integrates
finite pairs; `delta_energy_kwh` is null unless every row has a finite pair.
Irregular or single-row files have no energy integral. Completeness refers to
rows present, not proof of an uninterrupted intended simulation period or
equivalent weather/model inputs. Comparison rechecks source hashes after
reading. It starts no simulation and writes no files. Existing calls without
`compare_to` retain the original summary response. See the
[verification record](docs/result-comparison-verification.md).

### Standalone generator configuration

`pvsyst_get_variant_parameters` returns a `generator` form with supported scope,
current flags, enable state, GEN filename, operating power and per-subarray
thresholds. `pvsyst_update_variant_parameters` accepts `generator_updates` for
text variants with **Version 8.1.0 or 8.1.6 and SystemType=Battery**. File versions
are preserved. Existing generator tool names and arguments remain compatible.

For example, after inspecting a one-subarray variant:

```json
{
  "project": "Example.PRJ",
  "variant": "VC0",
  "expected_sha256": "<sha256 from inspection>",
  "generator_updates": {
    "enabled": true,
    "filename": "custom.GEN",
    "operating_power_kw": 40,
    "thresholds": {
      "1": {"VBkUpEncl_syst": 0.9, "VBkUpDecl_syst": 0.95}
    }
  },
  "dry_run": true
}
```

These are example test values, not recommended system settings. The thresholds
are PVsyst's **normalized voltage thresholds (0..1), not SOC**. Threshold keys
must identify existing subarrays. Enabling requires a valid loose GEN file,
positive finite operating power, and both thresholds in every subarray;
previously stored values can satisfy these requirements. Numbers must be JSON
numbers and `enabled` must be a boolean. Unsupported keys are rejected.

The transaction adds missing generator fields when needed and changes only
bit `0x20` of the owning system Flags. Disabling with `{"enabled": false}` keeps
the saved filename, power and thresholds. GEN nominal power (`PNomGen`) and fuel
consumption (`CFuelHor`) remain editable through `pvsyst_update_component`.
Operating power above nominal power returns a sizing warning. Component and
variant edits are separate transactions; cross-file atomic editing is not provided.

Preview returns a bounded diff, before/candidate SHA-256, before/after generator
state and the selected GEN dependency hash. Applying with the same original
hash creates a recovery backup; no-op generator edits preserve numeric spelling
and make no backup. The variant and GEN dependency are rechecked immediately
before replacement, including a new workspace file shadowing a builtin GEN.
These checks detect observed changes; they do not lock out external editors.
Use `pvsyst_restore_variant` with the backup name and current variant hash to undo.

`dry_run` also previews existing orientation/subarray parameter edits. These can
share a transaction with generator settings; in that case put backup thresholds
under `generator_updates.thresholds`, not `subarray_updates`. Existing fixed-plane
and shading restrictions still apply. See [the generator verification record](docs/generator-verification.md)
for native enabled/disabled controls, restoration evidence and remaining limits.

## Python example

```python
from pvsyst_cli import PVsystCLI, build_sfi, summarize

cli = PVsystCLI(cli_path=r"C:\Program Files\PVsyst8.1.6\PVsystCLI.exe",
                workspace=r"C:\path\to\PVsyst8.1_Data")
print(cli.capabilities()["run-simulation"])
print(cli.license_info())

sfi = build_sfi(cli.workspace / "Models" / "energy.sfi", "energy")
result = cli.run_simulation(
    "MY_PROJECT.PRJ", "VC0", sfi=sfi,
    out_csv=cli.workspace / "Results" / "run01.csv",
    start_date="1990.01.01", end_date="1990.01.02",
    report_pdf=cli.workspace / "Results" / "report01.pdf",
    report_pages=["cover", "summary", "results"],
)
print(result, summarize(result["csv"], "E_Grid"))
```

Python `summarize` and MCP result summaries use the same calculation. `sum`, `mean` and `max` describe finite samples. Each column includes `samples`, `missing_samples`, `coverage` (finite samples / parsed rows), and `complete` (every parsed row has a finite value); Python also returns `rows`. Timestamped rows with missing trailing cells are retained as missing samples. These fields describe rows present in the file, not proof that an intended simulation period has no omitted timestamps. All-missing columns remain in the response with `sum=0`, null `mean`/`max` and null energy.

For regular-cadence `E_Grid` or `EArray` with units W or kW, `observed_energy_kwh` integrates finite samples using `step_minutes`. `energy_kwh` is populated only when every parsed row has a finite sample; otherwise it is `null`. Both energy fields are `null` when cadence cannot be established (including a single row or irregular/duplicate timestamps), units are unsupported, or all values are missing. For example, hourly `10, blank, 10 kW` yields observed energy 20 kWh, coverage 2/3 and null complete energy; the missing value is not inferred. One-minute output has 1440 rows per day, so interpreting its raw sum as kWh would overstate energy 60-fold. Units come from the CSV.

Batch mode differs from a regular run: set `batch_params_name` and `batch_rvt_name`, and **leave `csv_name` and `pdf_name` empty**. The CLI writes a summary to `UserBatch/*Results.CSV` and each requested hourly file to `UserHourly`. The run tool returns `batch_summary`, `batch_hourly` and `batch_runs`; pass the summary filename to `pvsyst_read_batch_results`, or pass a per-scenario CSV to `pvsyst_read_results` with `folder="UserHourly"`. Stage the batch input CSV/RVT under `UserBatch`. Name the parameter file `*Params.CSV`, as in the vendor samples; this lets the bridge predict and protect the generated `*Results.CSV` before the run. Existing per-scenario hourly filenames are also rejected before launch. No source template or results are published here.

Batch automatic output directories and predicted CSV targets use the same resolved workspace boundary checks as explicit MCP paths. External directory links, junctions and escaping file links are rejected before simulation launch, including `UserHourly` and `UserBatch` outputs.

## Validation

- 8.0.6: existing demo project full-year hourly simulation; generated SFI with 8760 numeric rows.
- 8.1.6: full-year simulation with 8760 rows, date-limited simulation with 48 rows and PDF report, official `create-site` generated a SIT, vendor sample CSV+MEF+SIT converted to MET, and a short simulation/results query through a real MCP stdio client.
- Official three-scenario batch sample: generated a UserBatch summary plus three comma-separated UserHourly CSV files in 41 seconds. One scenario's hourly `E_Grid` is in W: integration gave 29624.104 kWh, consistent with the summary's rounded 29624 value.
- A one-minute MET and `--time-step:subhour` produced 1440 semicolon-separated rows for one day; the result reader detected a one-minute cadence and integrated kW values accordingly. A file named `1min` with an hourly internal time step was correctly rejected by the CLI.
- Component tools: an isolated MCP stdio lifecycle (`tests/smoke_components.py`) creates PAN/OND/BTR/GEN, edits and rolls back GEN, rejects `CFuelHor=0`, and archives/restores files. It also exercises project copying, source update/restore, scalar parameters, and project/variant archive recovery through MCP. The real 8.1.6 CLI loaded freshly created PAN/OND in a short grid simulation (48 rows, 6 columns) and freshly created BTR/GEN in a short stand-alone simulation (48 rows, 21 active generator rows). Temporary components, variants and CSVs were removed. Optional repeat scripts are `tests/smoke_live_components.py` and `tests/smoke_live_storage.py`; each consumes one CLI execution and requires the local vendor demo projects and component fixtures.
- Variant tools: isolated MCP stdio checks cover inspection, scoped clone, guarded edit and restore. The installed 8.1.6 CLI loaded a managed clone of `_Demo_PVsystCLI.VC0` referencing a new PAN and produced 48 hourly rows including `E_Grid` (`tests/smoke_live_variants.py`). A managed clone of `_DEMO_StandAlone.VC5` loaded new BTR/GEN files and yielded 48 rows with 21 generator-active rows **after raising the startup threshold only in the disposable variant** (`tests/smoke_live_variant_storage.py`). Temporary variants, components and CSVs were cleaned up; each script consumes one CLI execution.
- Project copy and parameter edits: `tests/smoke_live_project_copy.py` copies `_Demo_PVsystCLI.PRJ` to a disposable new project, edits one fixed-plane tilt and `NModSerie`, and simulates 48 hourly rows including `E_Grid` on the installed 8.1.6 CLI. It cleans up its PRJ, VC, backup and CSV; repeating it consumes one CLI execution. Restart an existing MCP client/server process to load the current tool set and category labels.
- Project sources: `tests/smoke_live_project_sources.py` uses the guarded update API on a disposable demo copy, then runs the real 8.1.6 CLI without source overrides. The CSV identifies the supplied SIT/MET and contains 48 hourly rows. Restoring through the API recovers the complete original PRJ/VC hashes. The optional script requires the local `Wanaluwawa.SIT`, `Wanaluwawa_Nasa_SYN.MET` and `mcp_audit_energy.sfi` fixtures, consumes one execution, and cleans up its project, result and backups. No private weather or vendor fixtures are distributed.
- Structural grid subarray A/B: `tests/smoke_live_structure.py` copied the demo project and ran two 48-hour CLI simulations. Duplicating one full inverter branch raised `EArray` from 98.5016 to 197.0049 and `E_Grid` from 94.4309 to 188.8619; removing it restored the exact original variant SHA-256. The real CLI preserved the edited bytes. The script removes its temporary PRJ, VC, CSVs, and backups; repeating it consumes two executions.
- Curve point tables: `tests/smoke_live_curve_tables.py` used six independent MCP/CLI runs (OND 8/9/7 points and BTR 6/7/5 points), each with 48 hourly rows. Added and removed points measurably changed E_Grid or CapaEff; all components restored exactly and 85 source files were unchanged. See [verification and limits](docs/curve-table-verification.md).
- Curve CSV transport: MCP export/import checks cover strict input, identical array/CSV previews, growth, stale hashes, no-op round-trip and restoration. `tests/smoke_curve_csv_previews.py` reproduced all six native-tested candidate hashes from the retained point-table evidence without new simulations.
- GUI PAN compatibility: the observed 8.1.6 GUI export passed MCP scalar/commercial editing and a 48-row native simulation. Existing aliases and file spelling were preserved; the edited copy restored exactly. See [evidence and boundaries](docs/gui-pan-verification.md).
- Grid circuit inventory: real MCP checks accept unequal inverter groups and distinguish malformed, unsupported and inconsistent counts. Two official sample variants loaded through CLI 8.1.6 and each yielded 48 hourly rows; all 85 original workspace files remained unchanged. See [verification and limits](docs/grid-circuit-verification.md).
- Weather input: real MCP checks cover a full hourly year, known monthly irradiation, missing night records, strict inputs and unverified time references. The installed standard-format example also exposes incomplete/non-aligned raw labels without changing its bytes. See [evidence and scope](docs/weather-input-verification.md).
- Weather preparation: real MCP runs converted the installed official 2022 hourly CSV into new SIT/MET files and produced a complete 8760-row annual output, with a largest hourly temperature difference of 0.0005 degC. A 36-hour irradiance comparison showed larger GHI/DHI differences (max 20.182 and 13.079 W/m2) alongside a native report of a 25-minute offset relative to the clear-sky model, so the case returned `needs_review` rather than claiming pointwise agreement; source CSV bytes were preserved. A low-irradiance MET converted successfully but failed an explicit native load. A separately labelled synthetic 2020 leap-calendar fixture (one installed day repeated across 8784 hours, explicitly not historical meteorology) completed prepare plus a full-year simulation at 38464.7346 kWh and 87.07453376% PR, with hourly maxima of 0.0051667/0.0050167 W/m2 (GHI/DHI) and 0.0005 degC and no review-threshold exceedance; the retained native 32-minute clear-day shift still returned `needs_review`. A 2022 independent-author result compared against the automated run shows seven exported variables (`GlobHor`, `DiffHor`, `GlobInc`, `EArray`, `E_Grid`, `PR`, `IL_Pmax`) over 8760 rows with a zero paired difference and full coverage; a low-irradiance input failed at the native simulation stage and was restored to the byte-identical template copy in two reverse-order steps with the failed records kept. UTC/DST conversion, columns beyond GHI/DHI/Tamb and native physical/climate certification remain unverified. See [site, weather and year closure](docs/weather-preparation.md).
- Single-design native closure (83): one real MCP `pvsyst_simulate_design` call with `dry_run=False` acquired ERA5 Beijing 2024 online, prepared new SIT/MET, auto-selected an eligible template plus exact PAN/OND, planned **21.6 kW** from a declared envelope and completed a 8784-row native year: `E_Grid` 36945.2864 kWh, standard PR 90.610745%, `IL_Pmax` 73.2493 kWh, GlobInc 1887.6679 kWh/m2, 18 series modules x 2 strings, 2 inverters, 36 modules, DC 21.6 kW vs AC 18 kW. `status=needs_review` with `stage=completed` and `execution_preflight_complete=true`: the retained review items are the disclosed `endpoint_mean` approximation and a native 15-minute weather-shift warning, so this is not a `completed` design, and the declared envelope uses integration-fixture design values with an experimental `Generic_9kW` inverter rather than Beijing climate extremes or manufacturer/engineering certification. Native 81 failed and is kept: the native SIT serializes longitude to four decimals (`116.39723 -> 116.3972`). Native 82 probed that serialization (`Lat 39.907567 -> 39.9076`, `Long 116.397234 -> 116.3972`, `Alt 47.75 -> 48`), and `check_site` now states +/-0.0000501 deg and +/-0.500001 m as serialized-precision bounds, with over-tolerance counterexamples still blocked. Evidence: `single-design83-evidence.json`, reports `Results/MCP_DESIGN83_annual_2024.csv`/`.pdf`. A later read-only SDK preflight (89) accepted `electrical_design` with fixture-sourced V/A declarations, matched the exact component identity, hit the existing seed plus already-generated weather, returned `ready` with `execution_preflight_complete=true`, and preserved the requested 30/-15 angles and `LIDLoss 1`/`PMismMPP 2.5`/`ModuleQualityLoss -0.3`/`UFactor 29` while the `Projects` member list stayed unchanged; that is a preflight, not a new native annual run and not an engineering certification. Time-basis caveat for 83/87/88: those numbers hold only for the `SolarTime` inputs used at the time, because the MET-embedded `HLeg` flag was later shown to override the SIT declaration (changing only the MET flag changed `HSol`/`AzSol`), so the old closure interpreted legal labels as solar time - matching hourly numbers do not prove the time model, and `E_Grid` 36945.2864 kWh is not evidence that a true legal-time basis is correct. The time basis was never closed; run 92 has since confirmed that switching the public MEF from `RefTimeS` to `LegalTime` yields `HLeg=True`, `timeshift=0` and no change in any of the 3x8784 yearly hour tokens, with the Jan 29 17h native value moving from 0 (source CSV 7) to 7.0003, All seven low-irradiance outlier hours are restored under the corrected basis, and run 93 then completed a full year on the same 88 source CSV via `mode=csv` (new SIT/MET/project, `LegalTime`, time fields 0, `HLeg=True`, generated guard valid, `execution_status=completed`, 2024, 8784 rows, `E_Grid` 36930.3633 kWh, PR 90.60707390969796%), with a GHI/DHI maximum absolute difference of 0.002 W/m2, temperature 0.0001 degC and zero out-of-threshold items; the now-empty 15-minute clear-sky warning is a natural consequence of the corrected `RefTimeS`, not a suppressed warning. `endpoint_mean` did not become a measured hourly mean and the source-aware review still returns `needs_review`. The 83/87/88 legacy `SolarTime` results remain history and are not correct local-legal engineering results. A standard empty-workspace template fallback is implemented and now natively accepted under the verified root name and layout: with no eligible workspace template, exactly resolved PAN/OND and no caller-pinned `template_project`/`variant`, the flow derives a read-only seed from the installed standard template (keeping the `Version=8.1.0/8.1.6` declaration, the UTF-8 BOM markers and the untouched historical parent `.BatchParams` reference (its retention alone does not prove that file or any batch parameter is loaded or consumed; batch loading is unverified), and `dry_run` plans it in memory without writing. Bootstrap 87 used the canonical root name `PVsyst8.1_Data`, 17 root folders plus 9 under `ComposPV`, two components and no PRJ/SIT/MET: `dry_run` returned `ready_for_weather`, and execution derived the seed, built the working project and acquired fresh ERA5 weather, completing 8784 rows of 2024 at `E_Grid` 36945.2864 kWh and PR 90.610745% (the same values observed in run 83; both metric sets are simply read, with no new comparison or hash test). Runs 85 and 86 failed and are kept (`single-design85-evidence.json`): 85 lacked the layout and had the wrong root name, 86 added the 26 folders but still had the wrong root name, so the root name and the 26-folder layout must both be correct. That 26-folder superset is what a real bootstrap proved, not a trimmed static minimum, and the root is never renamed, moved or replaced. Automatic directory initialisation is now natively accepted too (bootstrap 88: PAN/OND only, no pre-created folders). Its `dry_run` preflight only reported the missing folders and created no `Projects`, while execution's `workspace_init` created 20 new directories - the weather phase had already created `Meteo`/`Sites`/`Results`, and `ComposPV`, `PVmodules`, `Inverters` already existed - so the accurate statement is that the final 26 standard folders were completed, not that init created 26. The same execution completed 8784 rows at `E_Grid` 36945.2864 kWh and PR 90.61074499969226%, with all 26 folders present, a 620743-byte `%PDF-` report and the four source/working PRJ files in place. An initial script `exit 1` came from a bad 87-to-88 bulk replacement turning `8784` into `8884`: that was a harness assertion bug, not a product failure, and the corrected check read the original CSV/PDF only (no native re-run, no hash test). The root name and layout remain proven prerequisites for the local CLI 8.1.6 only. This round's new offline tests all passed: 29 design-envelope, 10 discovery, 32 provider, 22 standard-seed and 42 flow cases (135 total, no skips, including the nested `ComposPV` directory-link-escape and same-name-file preflight that runs before any provider request), and the real SDK lifecycle reported 67 passed / 0 failed; local syntax, pycompile and git diff checks also pass. The seed introduces no new MCP tool: it is an internal fallback, and an explicit caller template is never replaced. See [the single-design input closure](docs/single-design-inputs.md).
- Single-design inputs and Open-Meteo acquisition: real ERA5 downloads for Beijing 2024 (leap year, 8784 rows) and 2025 (8760 rows) produced each new standard hourly CSV, raw provider response and provenance manifest. Offline tests passed this round: 25 design-envelope, 10 discovery (including resolved components), 30 provider and 25 flow cases in `tests/test_design_envelope.py`, `tests/test_design_inputs.py`, `tests/test_weather_acquisition.py` and `tests/test_design_flow.py`; the real SDK lifecycle reported 67 checks passed. These are this round's snapshot, not a fixed total. A subagent mistakenly executed the hash-checking new tests and the full test suite: those newly added hash cases have been removed, those executions are not counted as acceptance this round, and later verification follows the non-hash whitelist (weather review 52, source 23, previous stage 135, SDK 120). Deviation record: the full suite actually ran 6 times and the new hash probes about 9 times; none of it counts as acceptance and it will not recur. Existing hash tests and the product-level conflict protection are retained but are no longer run. The earlier 189 offline regressions belong to the PR#22 record and were not run. See [the single-design input closure](docs/single-design-inputs.md).
- Offline tests mock CLI calls and use temporary files, so they do not consume license executions. They cover candidate growth rejection, byte-preserving component recovery, snapshot integrity, archive member limits, dotted/Unicode names, incomplete system checks, source transaction mixing/tampering, automatic output path confinement, missing-result coverage, curve point tables/CSV, GUI PAN aliases/dimensions, grid circuit inventory, generator configuration and result comparison, alongside lifecycle, partial-write rollback and CLI tests. Optional read-only live protocol checks are in `tests/smoke_stdio.py`.
- License status is reported exactly as inferred from CLI output; successful simulation is not evidence of a particular licensing tier.

Official reference: [PVsystCLI command reference](https://www.pvsyst.com/help-cli/reference/index.html) and [release notes](https://www.pvsyst.com/help-cli/release-notes.html). This source code is MIT licensed and is not affiliated with or endorsed by PVsyst SA.

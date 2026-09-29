# PVsyst CLI MCP

Unofficial local [Model Context Protocol](https://modelcontextprotocol.io/) server and Python adapter for the **installed Windows PVsystCLI**. Tested with 8.0.6 and 8.1.6. The server wraps the vendor CLI; this repository does not include PVsyst, projects, license material, weather data or simulation output.

## Scope and version differences

PVsystCLI automates simulations of **existing** projects/variants; editing an entire project, shading scene, component database or GUI is outside the official CLI interface. This integration does not claim to automate the whole PVsyst desktop UI.

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

The 45 tools are organized into six groups. Existing tool names and arguments remain compatible. Each MCP description begins with a category label matching the groups below; clients that display a flat tool list can still show these labels without a grouping extension.

### Setup & License (6)

| Tools | Purpose |
|---|---|
| `pvsyst_capabilities` | Installed CLI version and supported commands/options |
| `pvsyst_license_info` | License state and quota without key or Host ID |
| `pvsyst_license_activate`, `pvsyst_license_deactivate`, `pvsyst_license_sync` | Confirmed license changes |
| `pvsyst_export_logs` | Diagnostic ZIP under Results |

### Projects & Variants (18)

| Tools | Purpose |
|---|---|
| `pvsyst_list_projects`, `pvsyst_list_variants`, `pvsyst_inspect_project` | Workspace inventory and PRJ/VC hashes |
| `pvsyst_clone_project`, `pvsyst_clone_variant` | Copy existing project or create a variant from an existing one |
| `pvsyst_update_project_sources`, `pvsyst_restore_project_sources` | Persist a workspace SIT/MET pair across a project and every variant; restore the saved file set |
| `pvsyst_get_variant_components`, `pvsyst_update_variant_components` | Inspect and replace scoped PAN/OND/BTR/GEN references |
| `pvsyst_get_variant_parameters`, `pvsyst_update_variant_parameters` | Inspect and edit supported scalar parameters |
| `pvsyst_validate_variant_structure`, `pvsyst_clone_subarray`, `pvsyst_remove_subarray` | Check references and edit isolated unshaded grid branches |
| `pvsyst_restore_variant` | Restore a SHA-guarded variant backup |
| `pvsyst_archive_project`, `pvsyst_archive_variant`, `pvsyst_restore_project_archive` | Confirmed PRJ/VC archive and recovery |

### Components (13)

| Tools | Purpose |
|---|---|
| `pvsyst_list_components`, `pvsyst_get_component`, `pvsyst_validate_component` | PAN/OND/BTR/GEN inventory, inspection and static checks |
| `pvsyst_create_component`, `pvsyst_copy_component`, `pvsyst_clone_component` | Create, import or clone a component |
| `pvsyst_update_component`, `pvsyst_backup_component`, `pvsyst_restore_component` | Allowlisted edit, byte-identical backup and recovery |
| `pvsyst_archive_component`, `pvsyst_restore_archived_component` | Archive an unreferenced component and restore it |
| `pvsyst_compare_components`, `pvsyst_project_components` | File comparison and PRJ/VC reference lookup |

### Sites & Weather (3)

| Tools | Purpose |
|---|---|
| `pvsyst_build_monthly_weather`, `pvsyst_create_site` | Build monthly CSV and create an SIT (8.1+) |
| `pvsyst_convert_meteo` | Convert CSV + MEF + SIT to MET |

### Simulation (2)

| Tools | Purpose |
|---|---|
| `pvsyst_build_sfi` | Create an hourly export definition |
| `pvsyst_run_simulation` | Single or batch CLI simulation with CSV/PDF outputs |

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

Variant management operates on **existing** `.PRJ` projects and UTF-8 `.VC*` variants. Call `pvsyst_get_variant_components(project, variant)` to inspect `SubArrayId`-scoped PAN/OND and system-scoped BTR/GEN references and obtain `sha256`. `pvsyst_clone_variant(project, source_variant, new_variant, updates, subarray_id)` creates a new variant, preserving all other bytes and refusing to overwrite an existing variant. `pvsyst_update_variant_components` changes only references that already occur exactly once in the selected section; it requires the current `expected_sha256` and saves the previous bytes under `Projects/.mcp-variant-backups`. `pvsyst_restore_variant` requires `confirm=true`, matching backup name and current SHA-256, and backs up the pre-restore state. Supply `updates` such as `{ "PAN": "new.PAN", "OND": "new.OND" }` with `subarray_id` for a specific subarray; BTR/GEN target the existing `pvSystem` regardless of the subarray ID. Replacement component files must exist as loose files in the workspace or the installed DataRO library. Editing a GEN filename does not enable the generator; flags, power and thresholds remain unchanged. Structural or physical compatibility is not inferred from the replacement filename: run a short simulation and inspect its output. These reference tools do not add component fields or edit electrical topology. `pvsyst_clone_project(source_project, new_project)` copies one existing UTF-8 PRJ and all its VC files byte-for-byte; it rejects unknown same-stem sidecars and never overwrites existing files. It preserves embedded site/meteo snapshots, not an independently generated blank design. `pvsyst_inspect_project` returns a SHA-256 for every PRJ/VC member. `pvsyst_get_variant_parameters` exposes the fixed-plane orientations and selected existing subarray settings. `pvsyst_update_variant_parameters` accepts finite, bounded values for `NModSerie` (integer 1-100), `VBkUpEncl_syst` and `VBkUpDecl_syst` (0-1), and for a fixed-plane orientation without a shading table, `FieldTilt` (0-90) and `FieldAzim` (-180 to 180). It requires the current variant SHA-256 and creates a restorable byte-identical backup. It does not edit the circuit tree, add subarrays, switch system type, modify the shading scene, or synchronize existing shading tables; such orientation edits are rejected. `pvsyst_archive_project` requires all hashes from `pvsyst_inspect_project`; `pvsyst_archive_variant` requires the variant hash and at least one other variant; both require `confirm=true` and move original files under `Projects/.mcp-project-archive`. `pvsyst_restore_project_archive` checks hashes and destination conflicts before restoring. Site, meteo and component files remain in their own workspace directories. These file-level tools do not replace PVsyst's own project authoring and physical consistency checks.

`pvsyst_inspect_project` also reports each PRJ/VC file's embedded SIT/MET filenames, whether they exist in the workspace, and differences between the project and its variants. It does not infer location from the project name or validate the binary MET contents. `pvsyst_update_project_sources(project, site_name, met_name, expected_files)` uses the complete hash map from inspection, requires an existing UTF-8 SIT and MET under `Sites`/`Meteo`, and updates the project's site snapshot and weather filename plus every variant's `SiteSimul`, `SiteMet`, MET filename and site name. All members are validated before any project file changes; backups preserve the original bytes and BOM/newline style is retained. Save the returned `backups` and `files` maps. To undo the change, pass them as `backups` and `expected_files` to `pvsyst_restore_project_sources(..., confirm=true)`. Restore also backs up the state it replaces. Handled replacement errors roll back files already written; this is not a crash-atomic transaction across multiple files, and the in-process lock does not exclude external editors. Existing weather origin metadata remains historical. Use dates available in the new MET and verify the resulting CSV's site/weather header and energy output without runtime SIT/MET overrides.

`pvsyst_validate_variant_structure` reports missing or duplicate orientation IDs, grid inverter branches with mixed/unknown subarray IDs, and branch names inconsistent with the owning subarray. Its warnings flag shading-table geometry for real simulation review. `pvsyst_clone_subarray` duplicates one complete grid `InverterNode` branch and its matching `pvSubArray` object, assigns the next ID above the highest currently present project-variant ID, keeps the same fixed-plane orientation, and saves an original-byte backup. `pvsyst_remove_subarray` removes one isolated branch and subarray only while another subarray still uses that orientation. Both require a current variant hash and reject active shading scenes, mixed/shared branches, and unsupported systems. They do not generate new orientations, redesign inverter/MPPT topology, synchronize 3D geometry, or infer component sizing; validate structure and compare simulation outputs after edits.

Component tools support **four types only**: `.PAN` (`ComposPV/PVmodules`), `.OND` (`ComposPV/Inverters`), `.BTR` (`ComposPV/Batteries`) and `.GEN` (`ComposPV/Gensets`). `pvsyst_create_component` takes the complete UTF-8 `PVObject_` text and a new filename; it does not require an existing file. `pvsyst_clone_component` is a shortcut for a complete same-type text template and requires new `Manufacturer` and `Model` values. `pvsyst_copy_component` preserves bytes, including legacy mixed binary `.PAN` files from the installed read-only `DataRO` library. Old `.PAN` files can be listed, compared and copied, but not text-edited. Installed encrypted `*DB.csv` component databases cannot be enumerated; a project reference absent from loose files is reported as `unknown-or-builtin-db`, not as missing. The installed loose-file library is discovered under the CLI installation's `DataRO/*/ComposPV` independently of the user workspace name. If multiple versions are installed there, set `PVSYST_BUILTIN_COMPONENTS` to the full `ComposPV` directory to select one. A `.GEN` file reference alone does not activate a generator; dependency inspection also reports the variant's `$20` flag, `PEffBackUp` value and each `pvSubArray` backup threshold with its line number. Threshold-present booleans summarize whether **any** subarray contains the corresponding field.

`pvsyst_update_component` and `pvsyst_clone_component` allow only the following existing scalar fields; create-from-text accepts a complete component:

| Type | Editable fields |
|---|---|
| PAN | `Manufacturer`, `Model`, `DataSource`, `PNom`, `ISC`, `Voc`, `Imp`, `Vmp`, `MuISC`, `muVocSpec` |
| OND | `Manufacturer`, `Model`, `DataSource`, `PNomConv`, `PMaxOUT`, `VMppMin`, `VMPPMax`, `VAbsMax`, `EfficMax`, `EfficEuro` |
| BTR | `Manufacturer`, `Model`, `DataSource`, `CapNomC10`, `CapaRef`, `AlphaSOC` |
| GEN | `Manufacturer`, `Model`, `DataSource`, `TypeGen`, `PNomGen`, `CFuelHor` |

Component creation and edits check object structure, required fields and a limited set of numeric constraints. They do **not** certify the physical model: exercise a project variant that actually references the new component and inspect the resulting CSV. `TypeGen` is a free-text field accepted by the tested PVsyst version, unlike the battery technology enum `BattTechnol`, which is deliberately excluded from scalar editing. Inverter curves and battery chemistry are not transformed when cloning; BTR profile curves are not editable through the scalar-update tool, and `CFuelHor` must be positive because zero was observed to crash PVsyst. Text cloning and editing retain a UTF-8 BOM when the source has one. Component inspection returns at most 200 lines per page: `page_truncated` indicates more lines after the current page, `line_truncated` indicates a displayed line exceeded 2000 characters, and `lines_truncated` is true if either occurs. Edits automatically create byte-identical backups under `ComposPV/.mcp-backups`; `pvsyst_archive_component` requires `confirm=true`, scans PRJ/VC files recursively under workspace `Projects` (without following linked directories), blocks references and moves the file to `ComposPV/.mcp-archive`. Reference checks stream project files up to 32 MB; larger files or unreadable/symlinked project paths block archiving so that it does not proceed with an incomplete check. Both rollback tools require `confirm=true`. All writes stay in the user workspace; `DataRO` is read-only. No vendor component data is shipped in this repository.

Place an existing SFI under `Models`, or call `pvsyst_build_sfi` first. A CSV with no SFI/RVT export definition may contain dates only. For a single `pvsyst_run_simulation` call, `csv_name="run01"` and `csv_name="run01.csv"` both select `Results/run01.csv`; input filenames such as `sfi_name` still require their extension. Output filenames cannot contain paths, and generated files are never overwritten. For `create-site`, supply altitude, timezone and country code to avoid its optional online location lookups. License mutation tool arguments can be retained by the MCP client; do not echo keys in logs. When `lic-info` provides no usable status or expiration, `pvsyst_license_info` reports `UNSPECIFIED` rather than assuming the license is active.

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

`sum` is a raw sum of numeric samples. For regular-cadence `E_Grid` or `EArray` with units W or kW, `energy_kwh` multiplies by the detected `step_minutes` and converts W to kW; a missing/irregular cadence returns `null` instead. One-minute output has 1440 rows per day, so directly interpreting its raw sum as kWh would overstate the energy 60-fold. The unit is taken from the result CSV, not guessed from the variable name.

Batch mode differs from a regular run: set `batch_params_name` and `batch_rvt_name`, and **leave `csv_name` and `pdf_name` empty**. The CLI writes a summary to `UserBatch/*Results.CSV` and each requested hourly file to `UserHourly`. The run tool returns `batch_summary`, `batch_hourly` and `batch_runs`; pass the summary filename to `pvsyst_read_batch_results`, or pass a per-scenario CSV to `pvsyst_read_results` with `folder="UserHourly"`. Stage the batch input CSV/RVT under `UserBatch`. Name the parameter file `*Params.CSV`, as in the vendor samples; this lets the bridge predict and protect the generated `*Results.CSV` before the run. Existing per-scenario hourly filenames are also rejected before launch. No source template or results are published here.

## Validation

- 8.0.6: existing demo project full-year hourly simulation; generated SFI with 8760 numeric rows.
- 8.1.6: full-year simulation with 8760 rows, date-limited simulation with 48 rows and PDF report, official `create-site` generated a SIT, vendor sample CSV+MEF+SIT converted to MET, and a short simulation/results query through a real MCP stdio client.
- Official three-scenario batch sample: generated a UserBatch summary plus three comma-separated UserHourly CSV files in 41 seconds. One scenario's hourly `E_Grid` is in W: integration gave 29624.104 kWh, consistent with the summary's rounded 29624 value.
- A one-minute MET and `--time-step:subhour` produced 1440 semicolon-separated rows for one day; the result reader detected a one-minute cadence and integrated kW values accordingly. A file named `1min` with an hourly internal time step was correctly rejected by the CLI.
- Component tools: an isolated 45-tool MCP stdio lifecycle (`tests/smoke_components.py`) creates PAN/OND/BTR/GEN, edits and rolls back GEN, rejects `CFuelHor=0`, and archives/restores files. It also exercises project copying, source update/restore, scalar parameters, and project/variant archive recovery through MCP. The real 8.1.6 CLI loaded freshly created PAN/OND in a short grid simulation (48 rows, 6 columns) and freshly created BTR/GEN in a short stand-alone simulation (48 rows, 21 active generator rows). Temporary components, variants and CSVs were removed. Optional repeat scripts are `tests/smoke_live_components.py` and `tests/smoke_live_storage.py`; each consumes one CLI execution and requires the local vendor demo projects and component fixtures.
- Variant tools: isolated MCP stdio checks cover inspection, scoped clone, guarded edit and restore. The installed 8.1.6 CLI loaded a managed clone of `_Demo_PVsystCLI.VC0` referencing a new PAN and produced 48 hourly rows including `E_Grid` (`tests/smoke_live_variants.py`). A managed clone of `_DEMO_StandAlone.VC5` loaded new BTR/GEN files and yielded 48 rows with 21 generator-active rows **after raising the startup threshold only in the disposable variant** (`tests/smoke_live_variant_storage.py`). Temporary variants, components and CSVs were cleaned up; each script consumes one CLI execution.
- Project copy and parameter edits: `tests/smoke_live_project_copy.py` copies `_Demo_PVsystCLI.PRJ` to a disposable new project, edits one fixed-plane tilt and `NModSerie`, and simulates 48 hourly rows including `E_Grid` on the installed 8.1.6 CLI. It cleans up its PRJ, VC, backup and CSV; repeating it consumes one CLI execution. Restart an existing MCP client/server process to load the 45-tool set and category labels.
- Project sources: `tests/smoke_live_project_sources.py` uses the guarded update API on a disposable demo copy, then runs the real 8.1.6 CLI without source overrides. The CSV identifies the supplied SIT/MET and contains 48 hourly rows. Restoring through the API recovers the complete original PRJ/VC hashes. The optional script requires the local `Wanaluwawa.SIT`, `Wanaluwawa_Nasa_SYN.MET` and `mcp_audit_energy.sfi` fixtures, consumes one execution, and cleans up its project, result and backups. No private weather or vendor fixtures are distributed.
- Structural grid subarray A/B: `tests/smoke_live_structure.py` copied the demo project and ran two 48-hour CLI simulations. Duplicating one full inverter branch raised `EArray` from 98.5016 to 197.0049 and `E_Grid` from 94.4309 to 188.8619; removing it restored the exact original variant SHA-256. The real CLI preserved the edited bytes. The script removes its temporary PRJ, VC, CSVs, and backups; repeating it consumes two executions.
- 63 offline tests mock CLI calls and use temporary files, so they do not consume license executions. They include multi-variant source update/restore, stale hashes, malformed source rejection, partial-write rollback, orphan destination protection, and valid remaining-variant checks before archiving. Optional read-only live protocol checks are in `tests/smoke_stdio.py`.
- License status is reported exactly as inferred from CLI output; successful simulation is not evidence of a particular licensing tier.

Official reference: [PVsystCLI command reference](https://www.pvsyst.com/help-cli/reference/index.html) and [release notes](https://www.pvsyst.com/help-cli/release-notes.html). This source code is MIT licensed and is not affiliated with or endorsed by PVsyst SA.

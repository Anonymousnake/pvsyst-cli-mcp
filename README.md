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

## MCP tools

| Tool | Function |
|---|---|
| `pvsyst_capabilities` | Version and commands/options detected from the installed executable |
| `pvsyst_license_info` | License state and trial quota; no key or Host ID |
| `pvsyst_license_activate`, `pvsyst_license_deactivate`, `pvsyst_license_sync` | License management; all require `confirm=true` |
| `pvsyst_list_projects`, `pvsyst_list_variants` | Workspace inventory |
| `pvsyst_list_components`, `pvsyst_get_component`, `pvsyst_validate_component` | PAN/OND/BTR/GEN inventory, paged fields and static checks |
| `pvsyst_create_component`, `pvsyst_copy_component`, `pvsyst_clone_component` | Create complete UTF-8 text, copy bytes, or clone from a complete file |
| `pvsyst_update_component`, `pvsyst_backup_component`, `pvsyst_restore_component` | Allowlisted edits with backup and confirmed rollback |
| `pvsyst_archive_component`, `pvsyst_restore_archived_component` | Confirmed reversible removal of unreferenced components |
| `pvsyst_compare_components`, `pvsyst_project_components` | File comparison and PRJ/VC dependency lookup |
| `pvsyst_build_sfi` | New hourly SFI export definition |
| `pvsyst_build_monthly_weather` | New 14-row monthly CSV for site creation |
| `pvsyst_create_site` | New SIT from monthly weather and coordinates (8.1+) |
| `pvsyst_run_simulation` | Single simulation CSV/PDF or batch auto-generated outputs |
| `pvsyst_convert_meteo` | Weather CSV + MEF + SIT into a new MET |
| `pvsyst_export_logs` | Export diagnostic ZIP (may contain private information) |
| `pvsyst_read_results`, `pvsyst_read_rows` | Summary or at most 500 rows from Results or UserHourly |
| `pvsyst_read_batch_results` | SIM_* scenario values from a UserBatch summary |

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

Component tools support **four types only**: `.PAN` (`ComposPV/PVmodules`), `.OND` (`ComposPV/Inverters`), `.BTR` (`ComposPV/Batteries`) and `.GEN` (`ComposPV/Gensets`). `pvsyst_create_component` takes the complete UTF-8 `PVObject_` text and a new filename; it does not require an existing file. `pvsyst_clone_component` is a shortcut for a complete same-type text template and requires new `Manufacturer` and `Model` values. `pvsyst_copy_component` preserves bytes, including legacy mixed binary `.PAN` files from the installed read-only `DataRO` library. Old `.PAN` files can be listed, compared and copied, but not text-edited. Installed encrypted `*DB.csv` component databases cannot be enumerated; a project reference absent from loose files is reported as `unknown-or-builtin-db`, not as missing. A `.GEN` file reference alone does not activate a generator; dependency inspection also reports the variant's `$20` flag and `PEffBackUp` value.

Component creation and edits check object structure, required fields and a limited set of numeric constraints. They do **not** certify the physical model: exercise a project variant that actually references the new component and inspect the resulting CSV. In particular, inverter curves and battery chemistry are not transformed when cloning; BTR profile curves are not editable through the scalar-update tool, and `CFuelHor` must be positive because zero was observed to crash PVsyst. Edits automatically create byte-identical backups under `ComposPV/.mcp-backups`; `pvsyst_archive_component` requires `confirm=true`, blocks references in workspace PRJ/VC files and moves the file to `ComposPV/.mcp-archive`. Both rollback tools require `confirm=true`. All writes stay in the user workspace; `DataRO` is read-only. No vendor component data is shipped in this repository.

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
- Component tools: an isolated 29-tool MCP stdio lifecycle (`tests/smoke_components.py`) created PAN/OND/BTR/GEN, edited and rolled back GEN, rejected `CFuelHor=0`, and archived/restored files. The real 8.1.6 CLI loaded freshly created PAN/OND in a short grid simulation (48 rows, 6 columns) and freshly created BTR/GEN in a short stand-alone simulation (48 rows, 21 active generator rows). Temporary components, variants and CSVs were removed. Optional repeat scripts are `tests/smoke_live_components.py` and `tests/smoke_live_storage.py`; each consumes one CLI execution and requires the local vendor demo projects and component fixtures.
- Offline tests mock CLI calls and use temporary files, so they do not consume license executions. Optional read-only live protocol checks are in `tests/smoke_stdio.py`.
- License status is reported exactly as inferred from CLI output; successful simulation is not evidence of a particular licensing tier.

Official reference: [PVsystCLI command reference](https://www.pvsyst.com/help-cli/reference/index.html) and [release notes](https://www.pvsyst.com/help-cli/release-notes.html). This source code is MIT licensed and is not affiliated with or endorsed by PVsyst SA.

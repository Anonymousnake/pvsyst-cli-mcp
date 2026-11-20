# PVsyst CLI MCP

**English** | [简体中文](README.zh-CN.md)

Local MCP server and Python adapter for driving an installed copy of PVsyst 8 on Windows. It wraps `PVsystCLI.exe` for simulations, weather conversion, and license checks, and provides headless parsers, zero-template synthesizers, diagnostics, TOU economics, bounded auto-optimization, and interactive Plotly HTML reporting across `.PRJ`, `.VCx`, `.PAN`, `.OND`, `.BTR`, `.GEN`, `.SIT`, and 3D `.pvc2` shading scenes.

You need your own PVsyst installation and license on the host machine.

## Common workflows

| What you want to do | Main entry point | Guide |
|---|---|---|
| Run a zero-template fixed-plane or utility ground sheds/tracker PV design (mono/bifacial, finite 3D `site_polygon` + `obstacles` + `base_slope_deg`, optional auto-sizing) | `pvsyst_simulate_design`, `pvsyst_suggest_sizing` | [Single-design inputs](docs/single-design-inputs.md) |
| Filter workspace/builtin components by electrical specs or synthesize custom `.PAN`/`.OND` (single- or 3-voltage) / Li-ion `.BTR` directly from datasheets (or refit `.PAN` single-diode params on STC edits) | `pvsyst_list_components`, `pvsyst_build_sourced_component`, `pvsyst_update_component` | [Sourced components](docs/sourced-components.md) |
| Fetch hourly or TMY weather (`Open-Meteo ERA5`, `PVGIS v5.3 TMY`, `NASA POWER`) and optionally chain into `.SIT`/`.MET` preparation | `pvsyst_resolve_location`, `pvsyst_acquire_weather`, `pvsyst_prepare_weather` | [Weather preparation](docs/weather-preparation.md) |
| Run a parametric sweep (tilt, pitch, albedo, soiling, grid limit) in one CLI call and rank results | `pvsyst_run_batch_sweep` | [Tool reference](docs/tool-reference.md) |
| Audit completed simulation CSVs (+ optional `.VC0` attribution) and run a bounded Diagnose → Patch → Re-simulate loop on cloned variants | `pvsyst_diagnose_results`, `pvsyst_optimize_variant` | [Tool reference](docs/tool-reference.md) |
| Plan TOU 2-charge/2-discharge `PowerShift` 48-slot schedules & run two-stage hybrid PV+BESS sizing optimization (fast grid sweep + optional Stage 2 PVsystCLI verification) | `pvsyst_plan_tou_storage_schedule`, `pvsyst_optimize_bess_sizing` | [Tool reference](docs/tool-reference.md) |
| Evaluate fixed, TOU, and two-part demand-charge tariffs, battery replacement, sensitivity tornadoes, 25-year cashflows, LCOE, NPV, IRR, and payback | `pvsyst_evaluate_economics` | [Tool reference](docs/tool-reference.md) |
| Run a one-shot end-to-end PV/PV+BESS feasibility study (synthesis → simulation/reuse → diagnostics → BESS sizing → economics → interactive Plotly HTML report) | `pvsyst_run_feasibility_study`, `pvsyst_generate_report`, `pvsyst_export_engineering_delivery` | [Engineering delivery](docs/engineering-delivery.md) |
| Build a 3D house + obstacles scene, bind roof orientations, and run annual shading (`linear` or `module_layout`) | `pvsyst_run_house_task` | [House task](docs/native-house-task.md) |
| Prepare hourly or sub-hourly (`5/10/15/30 min`) weather and evaluate AC-coupled storage | `pvsyst_prepare_weather`, `pvsyst_create_grid_storage_system`, `pvsyst_run_simulation` | [Storage engineering](docs/engineering-storage.md) |
| Inspect an existing project, apply an isolated revision, or diff multiple scenarios | `pvsyst_project_handoff`, `pvsyst_run_engineering_task`, `pvsyst_compare_engineering_scenarios` | [Scenario comparison](docs/engineering-scenarios.md) |
| Check feasibility briefs or block assembly (requires `expert`) | `pvsyst_review_engineering_brief`, `pvsyst_prepare_block_project` | [Brief review](docs/engineering-brief.md) |

By default the server registers **46 core tools across 8 default groups**. `PVSYST_TOOL_GROUPS` is an explicit group list: setting `admin` or `expert` alone replaces the defaults and exposes only that group. Set `PVSYST_TOOL_GROUPS=all` to expose all **75 tools**; to keep the defaults and add `expert`, use `setup,projects,components,weather,storage,simulation,results,engineering,expert`. For migrating from `v0.1.0`, see [Upgrading from v0.1.0](docs/tool-reference.md#upgrading-from-v010).

## Installation

Requirements:
- Windows with Python 3.10+
- Installed `PVsystCLI.exe` (tested against 8.0.6, 8.1.6 rev49278, and 8.1.7 rev49382)
- Optional legacy GUI mode only (`scene_import_method="gui"`): PVsyst 8.1.6/8.1.7 GUI in Chinese locale at 96 DPI (100% display scaling); 3D house scenes, obstacles, `module_layout`, and DC cabling run headlessly without driving the desktop GUI by default

From the repository root:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:PVSYST_CLI = 'C:\Program Files\PVsyst8.1.7\PVsystCLI.exe'
$env:PVSYST_WORKSPACE = 'C:\path\to\PVsyst8.1_Data'
.\.venv\Scripts\python.exe -m pvsyst_auto
```

Dependencies (`mcp` 2.x, `pydantic` 2.x, `plotly` for interactive HTML reports, and `tzdata` for Windows IANA timezones) are declared in `pyproject.toml`. Running `pip install -r requirements.txt` installs the same editable package; use `pip install .` for a regular install.

## MCP client configuration

Point your MCP client at the virtualenv Python executable. Launching via `-m pvsyst_auto` (or the installed `pvsyst-mcp` console script) does not depend on the working directory:

```json
{
  "mcpServers": {
    "pvsyst": {
      "command": "C:/path/to/pvsyst-cli-mcp/.venv/Scripts/python.exe",
      "args": ["-m", "pvsyst_auto"],
      "env": {
        "PVSYST_CLI": "C:/Program Files/PVsyst8.1.7/PVsystCLI.exe",
        "PVSYST_WORKSPACE": "C:/path/to/PVsyst8.1_Data"
      }
    }
  }
}
```

`pvsyst_mcp_server.py` at the repo root is kept as a thin wrapper so older client configs still boot. Restart your MCP client after changing environment variables or pulling code updates.

Once connected, the server exposes MCP protocol `instructions`, 6 built-in workflow prompts (`design_fixed_or_utility_pv`, `design_rooftop_3d_pv`, `design_pv_with_storage`, `diagnose_and_optimize_simulation`, `ci_bess_feasibility_study`, `optimize_bess_sizing`), and 5 on-demand resources (`pvsyst://guide/workflows`, `pvsyst://guide/variables`, `pvsyst://workspace/projects`, `pvsyst://workspace/components`, `pvsyst://workspace/results`). Run `pvsyst_capabilities` and `pvsyst_license_info` first to confirm CLI flags and remaining license quota (note that PVsyst tracks CLI and GUI licenses separately; license mutation commands remain opt-in under `admin`).

### Filtering exposed tool groups

By default (when `PVSYST_TOOL_GROUPS` is unset), the server exposes only the **46 core workflow tools** (`setup`, `projects`, `components`, `weather`, `storage`, `simulation`, `results`, `engineering`) so LLM context stays compact. Set `PVSYST_TOOL_GROUPS` in `env` to customize; an explicit list replaces the defaults:

- `"PVSYST_TOOL_GROUPS": "all"` exposes all **75 tools** (the 8 default groups plus `admin` and `expert`).
- `"PVSYST_TOOL_GROUPS": "setup,projects,components,weather,storage,simulation,results,engineering,expert"` keeps the defaults and adds `expert` (71 tools).
- `"PVSYST_TOOL_GROUPS": "setup,engineering,results"` exposes only those 14 tools.
- Valid group keys (case-sensitive): `setup`, `projects`, `components`, `weather`, `storage`, `simulation`, `results`, `engineering`, `admin`, `expert` (or `all`). Unknown, empty, or duplicate group names raise an error at startup so typos fail fast.

## Understanding task statuses

High-level workflow tools return structured status fields so callers can tell the difference between a dry-run check, a file edit, and an actual PVsyst simulation:

| Status / Flag | What it means |
|---|---|
| `ready` | Dry-run preflight passed; no files were written and PVsyst did not run |
| `prepared` | Project or input files were written to disk, but no simulation has run yet |
| `stage=completed` + `native_consumption_verified=true` | PVsyst ran the simulation and output checks (calendar, columns, units) passed |
| `needs_review` | Simulation or preparation finished, but there are caveats to check (e.g. template-inherited fields, weather time-shift warnings, or missing engineering citations) |
| `blocked` / `failed` | Preconditions or execution failed; check `issues` and any retained working files before retrying under a new output name |

## Package layout

```text
src/pvsyst_auto/
├── api/          # MCP server setup, protocol guidance (prompts/resources), strict validation, 8 tool modules
├── workflows/    # End-to-end pipelines (design, utility 3D, batch sweep, optimize loop, house tasks, Plotly HTML report, ZIP delivery)
├── engineering/  # Sizing assistant, physics diagnostics, financial/TOU economics, handoff, brief checks, scenario comparison
├── project/      # .PRJ/.VCx parsing, zero-template synthesizer, pvBiFacial, circuits, templates, and orientations
├── components/   # .PAN/.OND/.BTR/.GEN store + mtime index cache, allowlisted editors, single-diode/Mode-1 datasheet synthesis
├── scene/        # PVCollada (.pvc2) 3D house mesh & finite 3D ground polygon clipper/shading builder
├── energy/       # Load profiles, AC-coupled battery storage, loss scalars, and power factor / grid limits
├── weather/      # Open-Meteo ERA5, PVGIS v5.3 TMY & NASA POWER downloaders, sub-hourly CSV & .SIT/.MET conversion
├── native/       # Subprocess wrapper for PVsystCLI.exe, headless 3D pvShading/module_layout emitter, and legacy Win32 bridge
└── results.py    # Result CSV parser and hourly/sub-hourly aggregation
```

See [architecture.md](docs/architecture.md) for module responsibilities and old-to-new import paths, and [native-entry-map.md](docs/native-entry-map.md) for the support matrix across PVsyst features.

To use the CLI wrapper directly from Python:

```python
from pvsyst_auto import PVsystCLI, PVsystError
```

## Development & tests

Integration and unit test suites live under `tests/integration/` and `tests/unit/`. See the [testing and release guide](docs/testing-and-release.md) for split test slices, CI verification, and release builds. Opt-in real-PVsyst tests live under `tests/e2e/`.

## License

[MIT](LICENSE).

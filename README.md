# PVsyst CLI MCP

**English** | [简体中文](README.zh-CN.md)

Local MCP server and Python adapter for driving an installed copy of PVsyst 8 on Windows. It wraps `PVsystCLI.exe` for simulations, weather conversion, and license checks, and provides headless parsers and builders for PVsyst text files (`.PRJ`, `.VCx`, `.PAN`, `.OND`, `.BTR`, `.GEN`, `.SIT`) including 3D house scenes with obstacles (`.pvc2` -> `.VCx`) and DC wiring calculations.

You need your own PVsyst installation and license on the host machine.

## Common workflows

| What you want to do | Main entry point | Guide |
|---|---|---|
| Run a single fixed-plane PV design + simulation end-to-end | `pvsyst_simulate_design` | [Single-design inputs](docs/single-design-inputs.md) |
| Inspect an existing project, apply an isolated change, and run a full year | `pvsyst_run_engineering_task` | [Engineering delivery](docs/engineering-delivery.md) |
| Build a 3D house + obstacles scene, bind roof orientations, and run annual shading | `pvsyst_run_house_task` | [House task](docs/native-house-task.md) |
| Evaluate centralized AC-coupled storage against an hourly load profile | `pvsyst_create_grid_storage_system`, `pvsyst_run_engineering_task` | [Storage engineering](docs/engineering-storage.md) |
| Diff two or three simulation runs | `pvsyst_compare_engineering_scenarios` | [Scenario comparison](docs/engineering-scenarios.md) |
| Check feasibility briefs, handoffs, block assembly, or ZIP bundles (`expert` group) | `pvsyst_review_engineering_brief`, `pvsyst_project_handoff`, `pvsyst_prepare_block_project`, `pvsyst_export_engineering_delivery` | [Brief review](docs/engineering-brief.md) |

By default the server registers **34 core tools across 8 default groups**. Set `PVSYST_TOOL_GROUPS=all` (or include `admin` / `expert`) to expose all **66 tools** including license administration, block assembly, low-level project/variant creators, and scene-level primitives; see the [tool reference](docs/tool-reference.md).

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

Dependencies (`mcp` 2.x and `tzdata` for Windows IANA timezones) are declared in `pyproject.toml`. Running `pip install -r requirements.txt` installs the same editable package; use `pip install .` for a regular install.

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

Once connected, run `pvsyst_capabilities` first to see which CLI flags your PVsyst version supports (enable `admin` or `all` in `PVSYST_TOOL_GROUPS` to query/manage CLI licenses via `pvsyst_license_info`; note that PVsyst tracks CLI and GUI licenses separately).

### Filtering exposed tool groups

By default (when `PVSYST_TOOL_GROUPS` is unset), the server exposes only the **34 core workflow tools** (`setup`, `projects`, `components`, `weather`, `storage`, `simulation`, `results`, `engineering`) so LLM context stays compact. Set `PVSYST_TOOL_GROUPS` in `env` to customize:

- `"PVSYST_TOOL_GROUPS": "all"` exposes all **66 tools** (the 8 default groups plus `admin` and `expert`).
- `"PVSYST_TOOL_GROUPS": "setup,engineering,results"` exposes only those 6 tools.
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
├── api/          # MCP server setup, strict argument validation, and 8 tool modules
├── workflows/    # End-to-end pipelines (template runs, revisions, house tasks, ZIP delivery)
├── engineering/  # Feasibility brief checks, input handoff, and multi-scenario diffs
├── project/      # .PRJ/.VCx parsing, subarray circuits, templates, and orientations
├── components/   # .PAN/.OND/.BTR/.GEN readers, allowlisted editors, and curve tables
├── scene/        # PVCollada (.pvc2) 3D house mesh generator, XML reader, and coordinate checks
├── energy/       # Load profiles, battery storage, loss scalars, and power factor / grid limits
├── weather/      # Open-Meteo ERA5 downloader, LegalTime alignment, and .SIT/.MET conversion
├── native/       # Subprocess wrapper for PVsystCLI.exe and Win32 GUI automation bridges
└── results.py    # Result CSV parser and hourly aggregation
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

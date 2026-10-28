# PVsyst CLI MCP

**English** | [简体中文](README.zh-CN.md)

Local MCP server and Python adapter for driving an installed copy of PVsyst 8 on Windows. It wraps `PVsystCLI.exe` for simulations, weather conversion, and license checks, and provides direct parsers and editors for PVsyst text files (`.PRJ`, `.VCx`, `.PAN`, `.OND`, `.BTR`, `.GEN`, `.SIT`) along with targeted GUI automation for features that the official CLI does not expose.

You need your own PVsyst installation and license on the host machine.

## Common workflows

| What you want to do | Main entry point | Guide |
|---|---|---|
| Check equipment counts and missing inputs from a feasibility brief | `pvsyst_review_engineering_brief` | [Brief review](docs/engineering-brief.md) |
| Inspect an existing project, apply an isolated change, and run a full year | `pvsyst_project_handoff`, `pvsyst_run_engineering_task` | [Engineering delivery](docs/engineering-delivery.md) |
| Assemble a new project by repeating fixed subarray blocks from a template | `pvsyst_prepare_block_project` | [Block preparation](docs/block-project-preparation.md) |
| Build a 3D house scene, bind roof orientations, and run annual shading | `pvsyst_run_house_task` | [House task](docs/native-house-task.md) |
| Evaluate centralized AC-coupled storage against an hourly load profile | `pvsyst_run_engineering_task` | [Storage engineering](docs/engineering-storage.md) |
| Diff two or three simulation runs and bundle the project + reports into a ZIP | `pvsyst_compare_engineering_scenarios`, `pvsyst_export_engineering_delivery` | [Scenario comparison](docs/engineering-scenarios.md) |

By default the server registers **82 tools across 8 groups**. If you need low-level access to components, variant parameters, circuit trees, weather files, loads, or result CSVs, see the [tool reference](docs/tool-reference.md).

## Installation

Requirements:
- Windows with Python 3.10+
- Installed `PVsystCLI.exe` (tested against 8.0.6 and 8.1.6)
- For features that drive the desktop UI (3D house scene import and DC cable resistance calculation): PVsyst 8.1.6 rev49278 in Chinese locale at 96 DPI (100% display scaling)

From the repository root:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:PVSYST_CLI = 'C:\Program Files\PVsyst8.1.6\PVsystCLI.exe'
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
        "PVSYST_CLI": "C:/Program Files/PVsyst8.1.6/PVsystCLI.exe",
        "PVSYST_WORKSPACE": "C:/path/to/PVsyst8.1_Data"
      }
    }
  }
}
```

`pvsyst_mcp_server.py` at the repo root is kept as a thin wrapper so older client configs still boot. Restart your MCP client after changing environment variables or pulling code updates.

Once connected, run `pvsyst_capabilities` and `pvsyst_license_info` first to see which CLI flags your PVsyst version supports and check your remaining CLI simulation quota. Note that PVsyst tracks CLI and GUI licenses separately.

### Filtering exposed tool groups

Exposing all 82 tools can bloat an LLM's context window. If a session only needs a subset, set `PVSYST_TOOL_GROUPS` in `env`—for example `"PVSYST_TOOL_GROUPS": "setup,engineering,results"` to expose only those 15 tools.

Valid group keys (case-sensitive): `setup`, `projects`, `components`, `weather`, `storage`, `simulation`, `results`, `engineering`. Unknown or duplicate group names raise an error at startup so typos fail fast.

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
└── results.py    # Result CSV/PDF parser and hourly aggregation
```

See [architecture.md](docs/architecture.md) for module responsibilities and old-to-new import paths, and [native-entry-map.md](docs/native-entry-map.md) for the support matrix across PVsyst features.

To use the CLI wrapper directly from Python:

```python
from pvsyst_auto import PVsystCLI, PVsystError
```

## Development & tests

Install the package in editable mode (`pip install -e .`) before running tests. Contract tests live under `tests/contracts/`. Tests that drive real PVsyst project fixtures or `PVsystCLI.exe` expect local environment variables pointing to your workspace and installed binary.

## License

[MIT](LICENSE).

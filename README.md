# PVsyst CLI MCP

**English** | [简体中文](README.zh-CN.md)

A local MCP server and Python adapter for engineering work with **installed PVsyst software**: review inputs, prepare or revise bounded native projects, run native simulations, compare related scenarios and deliver the original project/results with readable handoffs.

PVsyst performs the physical calculations. This unofficial project does not bundle PVsyst, licenses, commercial models, weather assets or a replacement PV/storage/shading simulator.

## Start here

| Task | Entry point | Details |
|---|---|---|
| Review cited feasibility quantities and missing inputs | `pvsyst_review_engineering_brief` | [Brief review](docs/engineering-brief.md) |
| Inspect an existing project and plan an isolated revision | `pvsyst_project_handoff`, `pvsyst_run_engineering_task` | [Engineering delivery](docs/engineering-delivery.md) |
| Prepare a project from complete fixed blocks | `pvsyst_prepare_block_project` | [Block preparation](docs/block-project-preparation.md) |
| Author and consume one bounded two-roof house | `pvsyst_run_house_task` | [House task](docs/native-house-task.md) |
| Evaluate existing native AC storage with hourly load | `pvsyst_run_engineering_task` | [Storage engineering](docs/engineering-storage.md) |
| Compare bound native scenarios and package results | `pvsyst_compare_engineering_scenarios`, `pvsyst_export_engineering_delivery` | [Scenario comparison](docs/engineering-scenarios.md) |

By default, the server exposes **82 tools in eight groups**. Use the [complete tool reference](docs/tool-reference.md) for low-level components, project editing, weather, loads, controls, simulations and result APIs. Existing wire tool names and request schemas are unchanged by the package reorganization.

## Install

Use Windows, Python 3.10+ and your own installed PVsystCLI. Native CLI behavior has evidence for 8.0.6 and 8.1.6; the bounded Chinese GUI paths have their own 8.1.6 rev49278/DPI96 requirements. Check each feature's evidence rather than inferring support from a version number.

From this checkout:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:PVSYST_CLI = 'C:\Program Files\PVsyst8.1.6\PVsystCLI.exe'
$env:PVSYST_WORKSPACE = 'C:\path\to\PVsyst8.1_Data'
.\.venv\Scripts\python.exe -m pvsyst_auto
```

`pyproject.toml` owns package metadata and dependencies, including MCP SDK 2.x and Windows `tzdata`. The existing `pip install -r requirements.txt` route now installs the same editable package. For a non-editable installation, use `pip install .` instead.

## Connect an MCP client

Configure the Python interpreter in which the package is installed. Module launch works independently of the client's current directory:

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

The installed `pvsyst-mcp` command launches the same stdio server. Existing clients targeting the checkout's `pvsyst_mcp_server.py` keep working through one thin compatibility launcher; old root-level implementation modules are not retained as forwarding files. Restart the MCP client after changing its environment or updating the package.

After connecting, use `pvsyst_capabilities` and `pvsyst_license_info` to inspect the **CLI's** actual options and license/quota before a native run. These queries do not run simulations; GUI and CLI licenses can differ. Credentials and license material do not belong in source control.

### Optional focused discovery

By default all 82 tools remain available. To publish only the groups needed by one client, add `PVSYST_TOOL_GROUPS` to its existing `env`, for example `"PVSYST_TOOL_GROUPS": "setup,engineering,results"`. This keeps the current 15 setup/engineering/result tools and avoids advertising the other groups. Omit the variable to keep the full catalog.

Keys are case-sensitive: `setup`, `projects`, `components`, `weather`, `storage`, `simulation`, `results`, `engineering`. Empty, duplicate or unknown entries stop startup rather than silently choosing a different set. Selection is process-local and requires a restart; it changes discovery/call availability, not native execution or workspace/engineering requirements. See [measured performance and limits](docs/performance.md) for catalog sizes, CSV-reading gains and the cold-start measurements that remain inconclusive.

## Read results honestly

| State | Meaning |
|---|---|
| `ready` | Preflight/preview passed; not a native simulation |
| `prepared` | Requested files were created; not necessarily consumed by PVsyst |
| `stage=completed` and `native_consumption_verified=true` | That task's recorded native execution and its input/output checks passed |
| `needs_review` | Keep unresolved source, model, engineering or native-warning conditions visible, even when execution completed |
| `blocked` / `failed` | Inspect issues and retained artifacts; no automatic overwrite or retry is implied |

An export or numerical comparison does not perform another native run. Historical CSV names alone do not establish historical inputs. Models, weather, hardware topology and engineering declarations remain explicit dependencies; a successful run is not equipment or site-design certification.

## Scope

The existing fixed/tracking revision, AC-storage evaluation, two-roof house and fixed-block preparation paths each retain their documented limits. A two-roof anchor0 case does not imply arbitrary geometry or nonzero-anchor support; fixed blocks do not imply terrain, shared transformers or arbitrary topology; AC self-consumption evidence does not imply every hybrid DC port or storage strategy.

Physical behavior always uses native PVsyst entries. Workspace file checks, production conflict guards, process-local locks, original-input preservation and retained failure records remain in place. A process-local lock is not a cross-process desktop lock. Coordinate/version/locale and short-period/full-year evidence are kept separate in the [native entry map](docs/native-entry-map.md).

## Code organization

```text
src/pvsyst_auto/
├── api/          # MCP context, strict request models and eight tool groups
├── workflows/    # Preparation, revision, annual runs and delivery
├── engineering/  # Briefs, input handoffs, reviews and scenario comparison
├── project/      # PRJ/VC, circuits, templates and orientation edits
├── components/   # PAN/OND/BTR, object editors and cited model creation
├── scene/        # House/PV authoring, reading and spatial verification
├── energy/       # Loads, storage, losses and electrical controls
├── weather/      # Acquisition, time basis, preparation and review
├── native/       # Existing CLI and bounded Windows GUI entries
└── results.py    # Shared native result validation and aggregation
```

Start at a workflow for an end-to-end task, a domain module for edits, and `native` for vendor execution. API code delegates to those existing implementations. There is no new plugin/DI framework, dispatch simulator or generic desktop engine. See [architecture and import migration](docs/architecture.md).

The public Python adapter is available as:

```python
from pvsyst_auto import PVsystCLI, PVsystError
```

## Development and evidence

Install the editable package before using Python modules directly. Focus verification on named public contracts under `tests/contracts`; real-input contracts may need an explicit local fixture, and a skip is not native acceptance. This repository's current working agreement excludes full discovery, legacy smoke and hash tests for the active batches; existing production consistency checks remain. See [AGENTS.md](AGENTS.md).

The structural reorganization does not expand native support or add annual runs. Detailed historical acceptance, current feature limits and failure/recovery records remain linked from the [tool reference](docs/tool-reference.md) and [engineering delivery](docs/engineering-delivery.md).

## License

[MIT](LICENSE). PVsyst and third-party inputs retain their own ownership and licensing terms.

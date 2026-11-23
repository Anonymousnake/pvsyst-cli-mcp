# Testing and Release

## Test Layout

Tests are split into fast, independent suites so local changes only need to run the relevant slice:

- `tests/integration/`: Fast MCP tool, schema, variant I/O, scene/component, entrypoint, and CLI integration tests (no PVsyst binary or private fixtures required). Run `--suite mcp` for the in-memory MCP slice (<5s) or `--suite integration` for all integration tests.
- `tests/unit/core/`, `tests/unit/energy/`, `tests/unit/weather/`, `tests/unit/workflows/`: Offline unit tests grouped by module area. Run any slice with `python scripts/run_tests.py --suite <core|energy|weather|workflows|unit>`.
- `tests/e2e/`: Opt-in end-to-end tests that execute a real local Windows `PVsystCLI.exe` installation (`python scripts/run_tests.py --suite e2e`).
  - `tests/e2e/test_mcp_full_workflows_e2e.py`: Full stdio MCP server workflow tests dynamically seeded from `DataRO\PVsyst8.1_Data` in an isolated temp workspace (automatically snapshots & byte-restores `PVsyst.ini` and `PVsystCLI.ini`), covering:
    1. `pvsyst_run_house_task` (`portrait`, positive spacing, `obstacles`, `module_layout`),
    2. DC cabling + `SystemUnavail` + `PowerShift`/`PeakShaving` combo simulation,
    3. Zero-template `ground_tracker` + `auto_from_components` + `pvBiFacial` + `pvsyst_diagnose_results` (`.VC0` attribution) + `pvsyst_run_batch_sweep` + MCP guidance surface (`instructions`, 6 prompts, 5 resources, 48 default tools),
    4. 15-minute sub-hourly weather preparation (`pvsyst_prepare_weather(..., step_minutes=15)`) + `PeakShaving` AC storage `-ts:subhour` vs hourly comparison. Run with:

```sh
PVSYST_E2E_REAL_NATIVE=1 PVSYST_E2E_CLI="C:\Program Files\PVsyst8.1.7\PVsystCLI.exe" python -m unittest tests/e2e/test_mcp_full_workflows_e2e.py -v
```

## Local Verification

Use a clean Python 3.10+ virtual environment, build and install the wheel, and run the CI checks:

```sh
python -m pip install build
python -m build
python -m pip install dist/pvsyst_cli_mcp-0.6.0-py3-none-any.whl
python scripts/ci_checks.py --report dist/ci-report.json
python scripts/run_tests.py --suite workflows -v
```

`scripts/ci_checks.py` verifies the wheel-installed `pvsyst-mcp` console entrypoint alongside checkout launchers and fails if any integration test is skipped or errors.

## CI and Releases

`.github/workflows/ci.yml` runs on pull requests, pushes to `main`, manual dispatches, and `v*` tags across Ubuntu and Windows (Python 3.10 and 3.12). The package version is defined in `pyproject.toml`. Tagging `v<version>` checks that the tag matches `[project].version` and publishes a draft release with the built wheel and sdist once all CI matrix jobs pass.

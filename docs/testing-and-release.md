# Testing and Release

## Test Layout

Tests are split into fast, independent suites so local changes only need to run the relevant slice:

- `tests/integration/`: Fast MCP tool, schema, variant I/O, scene/component, entrypoint, and CLI integration tests (no PVsyst binary or private fixtures required). Run `--suite mcp` for the in-memory MCP slice (<5s) or `--suite integration` for all integration tests.
- `tests/unit/core/`, `tests/unit/energy/`, `tests/unit/weather/`, `tests/unit/workflows/`: Offline unit tests grouped by module area. Run any slice with `python scripts/run_tests.py --suite <core|energy|weather|workflows|unit>`.
- `tests/e2e/`: Opt-in end-to-end tests that execute a real local Windows `PVsystCLI.exe` installation (`python scripts/run_tests.py --suite e2e`).

## Local Verification

Use a clean Python 3.10+ virtual environment, build and install the wheel, and run the CI checks:

```sh
python -m pip install build
python -m build
python -m pip install dist/pvsyst_cli_mcp-0.1.0-py3-none-any.whl
python scripts/ci_checks.py --report dist/ci-report.json
python scripts/run_tests.py --suite workflows -v
```

`scripts/ci_checks.py` verifies the wheel-installed `pvsyst-mcp` console entrypoint alongside checkout launchers and fails if any integration test is skipped or errors.

## CI and Releases

`.github/workflows/ci.yml` runs on pull requests, pushes to `main`, manual dispatches, and `v*` tags across Ubuntu and Windows (Python 3.10 and 3.12). The package version is defined in `pyproject.toml`. Tagging `v<version>` checks that the tag matches `[project].version` and publishes a draft release with the built wheel and sdist once all CI matrix jobs pass.

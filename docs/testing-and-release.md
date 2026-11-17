# Testing and Release

## Evidence Boundaries

`stored`/self-reader consistency is not proof that PVsyst loaded or consumed an input.
The portable CI gate below checks public package/MCP behavior, not a PVsyst model,
physical accuracy, shading parity, or a complete replacement of the application.
It needs neither an installed PVsyst binary nor a private sample directory.

The CI selects portable public contract classes explicitly. They cover installed
console/module/legacy launch, tool-group discovery, strict arguments, scene metadata,
report-page validation, result and batch reading, invalid requests that must not
launch the CLI or write outputs, and the acceptance CLI's offline validation and
zero-Native failed-precheck boundary. The package class checks a wheel-installed console;
its optional installed case is enabled, not skipped. The gate fails on any skip, import
error, failure, or empty selection. It clears inherited `PVSYST_*` and `PYTHONPATH`
settings so local native opt-ins and private samples cannot activate other classes.

Existing opt-in native tests and fixture-dependent contracts remain unchanged and
outside this gate. They are **NOTRUN**, not passed by association. This first CI
selection is not the whole test suite. Extend it with genuinely portable external
contracts as private fixture dependencies are removed; do not silently weaken or
remove their existing acceptance criteria. Legacy mock-heavy tests are not relabelled
as end-to-end tests by being run in CI.

## Test Layout & Split Execution

Tests are split into independent layers and domain slices so local edits only need to run the affected slice instead of the whole suite:

- `tests/contracts/`: Fast MCP tool, schema, variant, scene/component, entrypoint, and acceptance-CLI boundaries (no private workspace fixtures or repeated CLI subprocess overhead). Run `--suite mcp` for the <5s in-process MCP slice, `--suite contracts` for all contracts, or `python scripts/check_contracts.py` for the zero-skip release gate.
- `tests/unit/core/`, `tests/unit/energy/`, `tests/unit/weather/`, `tests/unit/workflows/`: Offline unit tests split by domain. Run any individual slice via `python scripts/run_tests.py --suite <core|energy|weather|workflows|unit>`.
- `tests/e2e/`: Opt-in tests targeting a real local Windows `PVsystCLI.exe` installation (`python scripts/run_tests.py --suite e2e`).

## Local Reproduction

Use a clean Python 3.10+ virtual environment, then run from the checkout:

```sh
python -m pip install build
python -m build
python -m pip install dist/pvsyst_cli_mcp-0.1.0-py3-none-any.whl
python scripts/check_contracts.py --report dist/contracts.json
python scripts/run_tests.py --suite workflows -v
```

The installed package is exercised alongside checkout entrypoints from temporary working directories. The report records actual test/error/skip counts.

## CI and Releases

`.github/workflows/contracts.yml` runs on pull requests, main pushes, manual requests, and `v*` tags. It builds wheel and source distributions, installs the wheel, and runs `scripts/check_contracts.py` plus the `workflows` slice on Windows and Linux with Python 3.10 and 3.12.

The version source remains `[project].version` in `pyproject.toml`; it is `0.1.0`
until an intentional release change. For a release:

1. Update the version and user-facing change notes in a reviewed pull request.
2. Merge normally and require the Portable Contracts jobs to pass. Repository branch
   protection must be configured separately; the workflow does not configure it.
3. Create and push a `v<version>` tag pointing to the reviewed commit. Tag/package
   mismatch fails before release; no version is inferred from an unreviewed branch.
4. Only after all matrix gates pass, create a GitHub **draft** release containing the
   tested Linux/Python-3.12 wheel and source distribution. Review the documented native
   evidence/support boundaries before publishing the draft.

Creating a tag/draft is a separate publication action, not a side effect
of local verification. Native acceptance needs its own explicit machine, version,
fixture provenance, independent reference, execution budget, raw artifacts, and
pass/fail/NOTRUN decision.

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

## Local Reproduction

Use a clean Python 3.10+ virtual environment, then run from the checkout:

```sh
python -m pip install build
python -m build
python -m pip install dist/pvsyst_cli_mcp-0.1.0-py3-none-any.whl
python scripts/check_contracts.py --report dist/contracts.json
```

The installed package is exercised alongside checkout entrypoints, from temporary
working directories. The report records actual test/error/skip counts and explicitly
sets `native_acceptance` to `not_run`. Standard unittest output contains the individual
failure tracebacks. Retain both if a check fails.

The current stale-CLI-adapter regression family is also run in CI using the installed
wheel and `PYTHONPATH=tests`: `DesignFlowTests`, `StandardTemplateTests`, the simulation
and revision terminal-state classes, and `ExplicitCountLineTests`. These existing
assertions are unchanged; only their fake CLI signatures now accept the real adapter's
optional per-request window policy. The 64-error reproduction and the corrected
70-test run are separate evidence, not a claim that every legacy test passed.
The separately reported two longstanding failures have not yet been identified from
a reproducible command or retained traceback and remain an open issue.

## CI and Releases

`.github/workflows/contracts.yml` runs on pull requests, main pushes, manual requests,
and `v*` tags. It builds wheel and source distributions, installs the wheel, and runs
the portable gate plus the affected regression family on Windows and Linux with
Python 3.10 and 3.12. It retains distributions and the JSON result even after a test
failure. A workflow file in a worktree is not proof that remote CI has run.

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

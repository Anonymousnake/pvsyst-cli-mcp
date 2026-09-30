# Result comparison verification

The existing `pvsyst_read_results` tool accepts `compare_to` and optional
`compare_folder`, returning target (`csv_name`) minus baseline (`compare_to`).
Existing single-file calls keep their response. No new tool name, native
execution, file write or decompilation is involved.

## Verified behavior

- Both files must contain 1–64 selected distinct columns with matching explicit
  units, matching row counts and identical strictly increasing timestamps.
  Column order can differ. Comma/semicolon CSVs and UTF-8 BOMs are accepted.
- Comparison uses strict parsing after the SFI header: malformed timestamps,
  extra values and missing units rows are rejected. Missing trailing values,
  NaN, infinity and nonnumeric cells are counted as missing, not zero.
- Sums, relative change and extrema use common finite pairs only. A missing
  value on either side excludes that pair. Marginal sample counts, paired
  sample count, missing pairs and coverage are all reported.
- A zero paired baseline sum gives null percentage change. No paired values
  gives null sums/extrema, not a fictitious zero difference. Extreme finite
  values that overflow arithmetic are rejected before JSON serialization.
- Identical irregular axes permit sample comparison but no energy integral.
  Regular W/kW values integrate using the observed step duration. Complete
  delta energy is null when any pair is missing; the observed partial delta
  remains available. Single rows also have no integral.
- Workspace path checks apply to both Results/UserHourly inputs. SHA-256 is
  taken before and after reading both files; detected changes refuse the result.
  This is a consistency check, not an exclusive lock on external writers.

141 offline tests passed, including 12 result comparison regression cases.
The MCP stdio smoke test verifies common-pair arithmetic, useful error messages
for path/unit/time mismatches, cross-folder input and legacy summary behavior.

## Existing native CSV evidence

The smoke test also copied existing PVsystCLI 8.1.6 output from the generator
VC5 enabled/disabled controls into a disposable workspace and compared it
through the actual MCP server. It did not rerun PVsyst or modify either source.
Both have 48 hourly rows, 1990-01-01 00:00 through 1990-01-02 23:00.

| Column | Paired samples | Target minus baseline sum | Complete delta energy |
|---|---:|---:|---:|
| EArray | 48 | -4.3056 | -4.3056 kWh |
| E_User | 48 | 17.5485 | 17.5485 kWh |
| GlobInc | 48 | 0 | null (W/m², not power) |

These values were checked against independent summation of the retained rows.
They describe the supplied files, not validation of generator dispatch physics.
The intentionally high generator thresholds in that control should not be used
as design recommendations.

Baseline SHA-256:
`58eaaecb8f7d287cbe3b89467954fb86c0a81242e6496e0ed8ef970df9a67060`.
Target SHA-256:
`899910511bea100d591d384a09c41e3e79b081016c46ffe1f7232c1d6820a1e3`.
These are the existing `mcp-generator-293a6f184767` CSVs. No native data is
included in the repository.

## Reproduction and remaining work

```powershell
python -m unittest discover -s tests -q
python tests/smoke_results.py
```

For optional native-file checks, set PVSYST_COMPARE_BASELINE and
PVSYST_COMPARE_TARGET to existing local CSV paths, and PVSYST_COMPARE_COLUMNS to
common columns (default EArray,E_User,GlobInc), then run the smoke script. Its
temporary input copies are removed on exit and both source hashes are verified.

This completes the comparison/statistics portion of roadmap N9. Visualization,
comparison export, report retrieval, unit conversion and timeline resampling
are not implemented. Matching timestamps do not prove matching weather, model
inputs, timezone or complete coverage of an intended simulation period.

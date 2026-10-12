# Standard hourly weather input checks

Verified on 2026-09-30. `pvsyst_inspect_weather_csv` adds read-only input checks
to the Sites & Weather MCP group. It does not change existing CSV conversion
behavior or claim that the entire weather preparation workflow is complete.

## Format and returned evidence

The implementation follows the installed official help page
`meteo-database/import-meteo-data/pvsyst-standard-format.html`, with time-label
context from `meteo-database/custom-meteo-files/time-shift.html`.

- UTF-8 (optional BOM), semicolon/comma separator, decimal point, distinct
  named columns and a matching units row. Limits: 32 MiB, 64 columns, date
  header in the first 200 records, 20,000 data records. These are reader limits.
- Identifiers `#Weather data hourly data` and `#TMY hourly data`; the legacy
  `#Meteo hourly data` observed in the installed official template is accepted.
- Required site/country/source, hourly step, latitude/longitude, altitude and
  timezone metadata. Duplicate known metadata is diagnosed. Year can be a
  column or `#Year`; explicit `expected_year` takes priority for coverage.
- Required GHI and Tamb, with optional DHI, DNI, wind, RH, precipitable water,
  Linke turbidity and aerosol optical depth. POA inputs disclose unchecked
  inverse-transposition/geometry scope. Unrecognized columns are listed.
- Hourly radiation in W/m2 or MJ/m2 converts to observed kWh/m2; temperature
  in deg.C/degC gives an observed mean. Unsupported units produce no mislabeled
  radiation or temperature summaries. Non-hourly steps produce no energy sum.
- Finite numbers, nonnegative radiation/wind, RH 0..100, temperature above
  absolute zero, and DHI <= GHI after unit conversion. Temperatures outside
  -60..60 deg.C are flagged for review; this is not full meteorological QA.

The raw full-year profile requires every interval-start hour from January 1
00:00 to December 31 23:00, including night records: 8760 records, or 8784 in a
leap year. Gaps, duplicate/out-of-order/invalid labels and unexpected labels
are reported, with a bounded list of missing examples. Nonzero minutes remain
unchanged. `missing_hours` counts absent labels on that expected **whole-hour
grid**, not measurements absent on every possible shifted time convention.

`valid` is false with detected issues, null when only unchecked conditions
remain, and true when this profile's checks pass. Explicit time references,
nonzero shifts, summarization periods and POA require additional interpretation.
The source SHA-256 is returned, and a second byte read rejects changes during
inspection. At most 50 issue details are returned; `issue_count` and
`issues_truncated` disclose the remainder. Monthly quantities are explicitly
observed values and sample counts, not repaired or certified complete totals.

## Verification

Offline fixtures include normal/leap years, a missing night hour or leap-day
hour, duplicate/reversed records, a different requested year, blank/NaN/Inf
values, incorrect units, invalid dates and metadata, shifted labels, POA,
optional weather variables and source changes during reading.

An independent arithmetic oracle uses 100 W/m2 for 12 hours per day. January
must contain 37.2 kWh/m2, and a 0.36 MJ/m2 hourly representation produces the
same monthly values. The complete synthetic year is a format/test fixture,
not real meteorology or a validated project design.

`tests/smoke_weather.py` invokes the actual MCP stdio server for complete,
missing-hour and explicit-UTC cases; it also checks strict integer years,
workspace filenames and unchanged bytes. The existing component/project MCP
lifecycle remains compatible with the 50-tool registration.

The installed official example `METEO_PVsyst_Standard_Geneva_GHI.csv` has
SHA-256 `e82c982441a2deb7394e2dbe0f02df9a1d63b0e16509a9d90b51c9bc56e8f214`.
Inspection through MCP finds 8783 records spanning 2004-01-01 01:30 to
2004-12-31 23:30. All have nonzero minutes, so it fails this raw whole-hour
full-year profile. This does not prove a PVsyst conversion defect: conversion
time handling and the missing initial record require separate investigation.
The original template is read-only; tests inspect a temporary copy and check
both source and copied bytes afterwards.

Reproduce offline checks with:

```text
python -m unittest discover -s tests -q
python tests/smoke_weather.py
python tests/smoke_components.py
```

Optionally set `PVSYST_WEATHER_SAMPLE` to a standard hourly CSV to inspect its
copy and `PVSYST_WEATHER_EVIDENCE` to a new JSON filename for retained evidence.
The script never overwrites an existing evidence file. Final local evidence
is in `component-edit-verification/weather-standard-mcp-final-evidence.json`,
outside this repository. No vendor data is distributed.

## Remaining work

This tool does not validate binary MET contents, generate a MEF, certify
MEF-column/unit mapping, compare a SIT with CSV coordinates/timezone, adjust
UTC/DST/interval-end labels, fill missing observations, derive TMYs or perform
clear-sky/horizon quality tests. It does not certify native conversion or a
full-year PV simulation. Existing `pvsyst_convert_meteo` still requires a
prepared, verified MEF and SIT.

`pvsyst_prepare_weather` now builds those SIT/MEF/MET inputs from one inspected
CSV and keeps its provenance, still without parsing the binary MET. It also
trims the reader's tolerance: that profile accepts `GHI`, `DHI` and `Tamb`
only, requires an explicit uppercase ISO alpha-2 country code and
`local_standard_interval_start`, and stops on UTC, explicit time references,
nonzero shifts, POA, missing measurements or unmapped columns. See
[site, weather and year closure](weather-preparation.md) for the conversion
contract, its preflight/`needs_review` states and the evidence that is still
missing. A labelled synthetic 2020 leap-calendar fixture there completes a full
8784-row prepare/simulate cycle, and that path also records an independent
2022 comparison with zero paired difference across seven variables over 8760
rows, a native `failed` case recovered to the byte-identical template copy, and
the offline/smoke regression state; UTC/DST conversion, extra columns and
native climate certification stay open. Linking those inputs to project templates and native full-year results
remains part of the active grid workflow goal.

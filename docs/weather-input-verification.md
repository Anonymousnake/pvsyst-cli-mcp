# Standard hourly weather input checks

`pvsyst_inspect_weather_csv` reads a PVsyst standard hourly CSV under Meteo. It checks the supported input profile, not native MET payloads, conversion quality, climate suitability or a PV design.

## Format and limits

UTF-8 with optional BOM, semicolon/comma separator, decimal point, distinct named columns and a matching units row are required. Reader limits are 32 MiB, 64 columns, date header within 200 records and at most 20, 000 data records. Accepted identifiers are#Weather data hourly data,#TMY hourly data and the legacy#Meteo hourly data.

Required metadata includes site, country, source, hourly step, latitude/longitude, altitude and timezone; duplicate known keys are diagnosed. Year comes from a column or#Year, with explicit expected_year taking priority for coverage. The expected year is a strict integer, not a filename convention.

GHI and Tamb are required; DHI, DNI, wind, RH, precipitable water, Linke turbidity and aerosol optical depth are optional. POA discloses unchecked inverse-transposition/geometry scope. Unknown columns are listed, not silently interpreted.

Radiation units W/m² or MJ/m² are converted only for observed hourly summaries; temperatures in deg.C/degC produce an observed mean. Unsupported units never produce mislabeled summaries, and non-hourly data does not produce an energy sum. Values must be finite; radiation/wind nonnegative, RH within0..100, temperature above absolute zero, and DHI<=GHI after unit conversion. Temperatures outside-60..60°C trigger review, not a meteorological certification rule.

## Calendar and output

The whole-hour interval-start profile requires January1 00: 00 through December31 23: 00, including night and leap day: 8760 or8784 labels. Gaps, duplicates, reversed/invalid labels and unexpected records are diagnosed with bounded examples. Nonzero minutes remain unchanged. missing_hours means missing labels on that expected whole-hour grid, not every possible shifted convention.

valid isfalse for detected issues, null for unchecked conditions only, true when supported checks pass. Explicit time references, nonzero shifts, summarization periods and POA need separate interpretation. Up to50 issue details are returned, with issue_count and issues_truncated. Monthly summaries disclose actual observed samples, not repaired or certified complete totals.

The result contains the input fingerprint; a second byte read detects concurrent changes. This production consistency guard does not establish physical validity or adjust source data.

## Preparation is narrower

`pvsyst_prepare_weather` accepts only GHI, DHI, Tamb, an explicit uppercase ISO alpha-2 country code and local_standard_interval_start. UTC, DST conversion, interval-end remapping, missing values, POA and unmapped columns stop rather than being shifted, filled or dropped. Existing convert_meteo still needs a verified MEF and SIT. See [weather preparation](weather-preparation.md) and[engineering review](weather-engineering-review.md).

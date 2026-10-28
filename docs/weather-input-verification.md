# Standard hourly weather CSV inspection (`pvsyst_inspect_weather_csv`)

`pvsyst_inspect_weather_csv(csv_name, expected_year=...)` parses and validates a PVsyst standard hourly weather CSV staged under the workspace `Meteo` directory before you convert it into a `.SIT`/`.MET` pair.

## Supported CSV format and reader limits

- **Encoding & layout**: UTF-8 (with or without BOM), comma or semicolon delimiter, `.` decimal separator, up to 32 MiB file size, up to 64 columns, header block within the first 200 lines, and at most 20,000 data rows.
- **Format header line**: Must start with `#Weather data hourly data`, `#TMY hourly data`, or legacy `#Meteo hourly data`.
- **Required metadata comments**: Site name, country, data source, hourly time step, latitude, longitude, altitude, and timezone (duplicate metadata keys are flagged).
- **Columns and units**:
  - Every data column must have a distinct header name and a corresponding entry on the units row immediately below the column header.
  - `GHI` and `Tamb` are required; `DHI`, `DNI`, wind speed, relative humidity (`RH`), precipitable water, Linke turbidity, and aerosol optical depth are optional (if plane-of-array `POA` irradiance is present, it is flagged in the summary since [`pvsyst_prepare_weather`](weather-preparation.md) only converts horizontal `GHI`/`DHI` and `Tamb`).
  - Supported radiation units: `W/m²` or `MJ/m²` (converted to `kWh/m²` for monthly summaries). Supported temperature units: `deg.C` or `degC`.
- **Value range checks**: All values must be finite; irradiance and wind speed must be non-negative; `RH` must be in `[0, 100]`; temperature must be above absolute zero (temperatures outside `[-60, 60] °C` are flagged for review); and diffuse horizontal irradiance must satisfy `DHI <= GHI` after unit normalization.

## Calendar completeness checks

The inspector checks that the timestamps form a complete, strictly increasing whole-hour grid (`00:00` through `23:00`) from January 1 to December 31 of the target year (either from `#Year`, the data rows, or the explicit `expected_year` integer argument):
- `8760` rows for a standard year, or `8784` rows for a leap year (including February 29 and all night hours).
- Missing hours, duplicate timestamps, out-of-order rows, or non-zero minute offsets are reported in `issues` (up to 50 detailed examples, with total `issue_count` and `issues_truncated`).
- Returns `valid=true` when all format, range, and calendar checks pass, `valid=false` when errors are found, or `valid=null` when the file uses time-shift conventions or columns outside the standard whole-hour profile.

Once a CSV passes inspection with `GHI`, `DHI`, `Tamb`, and `local_standard_interval_start` timestamps, pass it to [`pvsyst_prepare_weather`](weather-preparation.md) to generate the `.SIT` and `.MET` files.

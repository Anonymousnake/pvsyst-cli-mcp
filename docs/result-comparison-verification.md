# Comparing simulation result CSVs

`pvsyst_read_results` can compare two simulation CSV files directly when you pass `compare_to` (and optionally `compare_folder` when the baseline lives in `UserHourly` instead of `Results`). It computes `csv_name` (target) minus `compare_to` (baseline) across shared numerical columns without modifying either file.

If you omit `compare_to`, `pvsyst_read_results` returns the standard single-file summary. For higher-level engineering comparisons that also diff `.PRJ`/`.VCx` configurations and export Markdown/CSV reports, see [engineering scenarios](engineering-scenarios.md).

## Timestamp alignment and missing values

- You can compare 1 to 64 distinct columns. Both CSV files must have the same number of rows, identical strictly increasing timestamps at every row, and matching explicit units on each compared column (the column order in the two headers does not need to match).
- Both comma and semicolon delimiters (with or without a UTF-8 BOM) are supported.
- Empty cells, `NaN`/`Inf`, or non-numeric entries are tracked as missing observations (`missing_pairs`) rather than coerced to `0`.
- Sums, deltas, relative percentage changes, and min/max extrema are computed over rows where both files have finite values. If the paired baseline sum is `0`, the relative percentage change is returned as `null`.

## Power-to-energy integration

- For columns with power units (`W` or `kW`) on a regular time step (such as hourly steps), the reader integrates the paired values over the step duration to report total energy and delta energy in `kWh`.
- If any row in a power column is missing a finite value, the full-period energy integral is returned as `null` while the partial integral over observed rows is reported separately.
- Non-power columns (such as irradiance in `W/m²`, temperatures in `°C`, or ratios/SOC in `%`) report sample statistics and extrema without an integrated `kWh` field.
- Both files are hashed before and after reading so concurrent edits to a CSV while it is being parsed are caught immediately.

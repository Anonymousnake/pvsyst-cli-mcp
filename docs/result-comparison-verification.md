# Comparing native result files

`pvsyst_read_results(compare_to=..., compare_folder=...)` compares target csv_name minus baseline compare_to without native execution or writes. Existing single-file calls retain their response. Both inputs stay under Results/UserHourly.

## Alignment and missing values

Select1..64 distinct columns with matching explicit units, row counts and identical strictly increasing timestamps; column order may differ. UTF-8 BOM and comma/semicolon separators are accepted. Malformed timestamps, extra values or missing units rows stop. Missing trailing values, NaN/Inf and nonnumeric cells count as missing, never zero.

Sums, relative changes and extrema use only common finite pairs. Marginal/paired counts, missing pairs and coverage are returned. Zero paired baseline sum gives null percentage; no finite pair gives null sums/extrema, not a fictitious zero difference. Arithmetic overflow stops before JSON serialization.

## Energy interpretation

Identical irregular axes permit sample statistics but no integral. Regular W/kW values integrate over the actual step duration. A missing pair makes complete delta energy null while observed partial delta remains available; one row has no integral. Irradiance W/m² is not electric power and does not acquire a kWh energy field.

Both files are fingerprinted before/after reading; detected changes stop the result. This is consistency checking, not an exclusive lock. Matching labels do not prove matching weather, models, timezone or intended full-period coverage.

This statistics entry does not convert units or resample time. For engineering input binding, Chinese explanations and comparison exports, use[engineering scenarios](engineering-scenarios.md); do not infer dispatch physics or engineering validity from an aggregate delta alone.

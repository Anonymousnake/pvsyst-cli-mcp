# BTR battery capacity curves

`pvsyst_update_component` can read and update the root discharge capacity curve inside text-format PVsyst 8.1.6 `.BTR` files for sealed lead-acid batteries (`Pb_Sealed_AGM` or `Pb_Sealed_Gel`, with or without the `bt` prefix) in linear `Mode=1`.

## Tag quirk: `CapaCourant` vs `Capa_DischRate`

When PVsyst 8.1.6 GUI exports a `.BTR` file, it sometimes writes the discharge curve under the tag `Capa_DischRate`. However, `PVsystCLI.exe` ignores `Capa_DischRate` and only loads `CapaCourant`.

- If the file already uses `CapaCourant`, `pvsyst_update_component` updates the points in place.
- If the file uses `Capa_DischRate`, pass `use_file_curve=true` along with the replacement point table so the editor switches the tag to `CapaCourant`.

## Curve format and validation rules

In `Mode=1`, `X` is the discharge duration in hours and `Y` is the capacity multiplier relative to the 10-hour rating (`C10`).

To pass both the editor and PVsyst's native battery validator:
- Supply more than 3 active points (between 4 and 256 points; see [Mode 1 point-table editing](curve-table-verification.md)).
- `X` and `Y` must be positive, `X` must be strictly increasing, and `Y` must be non-decreasing.
- The `X` range must span `100` hours (because extrapolating a curve that stops at `10 h` can produce negative capacity ratios in PVsystCLI).
- At `X = 100 h`, the interpolated multiplier `Y(100 h)` (i.e. `C100 / C10`) must fall within `[1.15, 1.45]` for AGM and Gel batteries.

## Inspection fields and transactions

`pvsyst_get_component` reports:
- `cli_recognition`: whether the curve tag is recognized by `PVsystCLI.exe`
- `editable`: whether the chemistry, version, and curve mode are supported for editing
- `value_errors`: any coordinate or `C100/C10` ratio violations in the existing points

Updates require the current `expected_sha256` fingerprint, support `dry_run=true` previews, and create a byte-exact backup that can be rolled back with `pvsyst_restore_component`. Same-count edits leave inactive padding rows untouched; changing the point count updates `NPtsMax` and `NPtsEff` together.

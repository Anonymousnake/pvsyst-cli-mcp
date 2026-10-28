# Mode 1 point-table and CSV curve editing

`pvsyst_update_component` supports replacing the full point table (from 4 to 256 points) on linear `Mode=1` curves in PVsyst 8.1.6 text components:
- Single-voltage inverter efficiency curves (`Converter` / `ProfilPIO` in `.OND`; see [OND curve editing](ond-curve-verification.md))
- Sealed lead-acid (`Pb_Sealed_AGM` / `Pb_Sealed_Gel`) discharge capacity curves (`CapaCourant` in `.BTR`; see [BTR capacity curves](btr-capacity-verification.md))

## How point-count changes work in PVsyst files

In `.OND` and `.BTR` files, a `TCubicProfile` block stores both an allocated capacity (`NPtsMax`) and an active point count (`NPtsEff`), followed by `Point_1`..`Point_N` rows. When PVsyst GUI compiles a curve, it may also append precalculated spline/segment coefficients (`x, y, a, b, c`) and a hexadecimal `LastCompile` value.

- **Same-count edits**: If the new table has the same number of active points as `NPtsEff`, the editor updates the active `Point_1`..`Point_N` rows and leaves `NPtsMax`, `NPtsEff`, and any trailing `(0, 0)` padding rows unchanged.
- **Resizing the table (4..256 points)**: When the point count changes, the editor sets both `NPtsMax` and `NPtsEff` to the new length, writes `Point_1`..`Point_N` as clean `X,Y` pairs, and strips old trailing padding rows and stale `a,b,c` coefficients so PVsyst recompiles the segments cleanly at load time.

Preconditions for resizing:
- Existing rows must be numbered consecutively (`Point_1`..`Point_N`) after the header fields (`NPtsMax`, `NPtsEff`, `Mode`, `LastCompile`).
- `LastCompile`, if present, must be one of the known Mode 1 hex flags (`0`, `19`, `008D`, or `8089`).
- Every point must have `X >= 1e-9` and adjacent points must be separated by at least `1e-8` on the X axis so PVsyst does not silently drop duplicate abscissas during compilation.

`pvsyst_get_component` returns `NPtsMax`, `NPtsEff`, `point_count_editable`, and `point_count_errors`, and `pvsyst_update_component` reports `curve_counts_before` and `curve_counts_after`.

## Exporting and importing curves as CSV text

Instead of passing a JSON array in `curve_updates`, you can exchange the curve as CSV text:
- Call `pvsyst_get_component(..., include_curve_csv=true)` to include a ready-to-edit CSV string with explicit unit headers for supported main OND/BTR curves.
- Pass `curve_csv="..."` to `pvsyst_update_component` (mutually exclusive with `curve_updates`). The CSV parser accepts comma-separated decimal values, verifies the unit header, enforces the 4..256 point and 64 KiB limits, and routes the points through the same validation and backup pipeline.

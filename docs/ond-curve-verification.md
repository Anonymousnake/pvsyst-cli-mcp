# OND inverter efficiency curve editing

`pvsyst_update_component` can read and update the main `Converter` / `ProfilPIO` efficiency curve in text-format PVsyst 8.1.6 `.OND` inverter files using single-voltage linear `Mode=1`.

## Units and point-table rules

Regardless of whether PVsyst GUI displays inverter power in `W` or `kW`, the serialized `ProfilPIO` table inside an `.OND` file always stores `X` (DC input power) and `Y` (AC output power) in **watts (W)**.

- You can replace the point table (4 to 256 points) either via `curve_updates` or as CSV text via `curve_csv` (see [Mode 1 point-table editing](curve-table-verification.md)).
- Same-count updates preserve existing padding rows; changing the point count updates `NPtsMax` and `NPtsEff` and strips old precalculated spline coefficients (`a, b, c`) so PVsyst recalculates segment slopes on load.
- Three-voltage efficiency curves (root `Flags` bit 12 / `0x1000`) are read-only and not modified by this editor.

## Gotcha: switching out of automatic curve mode (`Flags` bit 4)

In `.OND` files, bit 4 (`0x10`) of the root `Flags` field tells PVsyst to synthesize an efficiency curve automatically from scalar `EfficMax` and `EfficEuro` values, ignoring the `ProfilPIO` points saved in the file.

If bit `0x10` is set and you only edit the `ProfilPIO` point coordinates, `PVsystCLI.exe` will still run using the automatic curve. Pass `use_file_curve=true` in `pvsyst_update_component` to clear bit `0x10` so PVsyst actually uses your custom point table.

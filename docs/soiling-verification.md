# Native constant and monthly soiling configuration

`pvsyst_get_variant_parameters` reports the current soiling configuration under `main_array.soiling`, and `pvsyst_update_variant_parameters` updates it via `soiling_updates` on PVsyst 8.1.6 unshaded single-subarray fixed-plane grid variants (`pvMainArray` `Flags` `$00100400` or `$01900400`, `Flags2=$082A`).

## Request formats

Pass one of the three supported modes (`constant`, `monthly`, or `none`) along with the current `expected_sha256` token:

```json
{"mode": "constant", "yearly_loss_pct": 2.5}
```

```json
{"mode": "monthly", "monthly_loss_pct": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]}
```

```json
{"mode": "none"}
```

Validation rules:
- Percentages must be finite numbers in `[0, 100]` at `0.1` percentage-point precision (e.g. `2.5`).
- `monthly_loss_pct` requires all 12 monthly values (January through December).
- `soiling_updates` runs as its own parameter transaction (optionally paired with `variant_name`).

## How PVsyst serializes soiling in `.VCx`

Inside `pvMainArray`, soiling is stored in a named `Soiling=Soiling` block (not `PVObject_Soiling`):
- **`constant` (e.g. `2.5%`)**: Writes `IsYearly=True`, `IsMonthly=False`, `IsFromFile=False`, and `YearlyValue=0.025` (stored as a decimal fraction in `[0, 1]`).
- **`monthly`**: Writes `IsYearly=False`, `IsMonthly=True`, `IsFromFile=False`, the 12 monthly fraction values, and retains an inactive `YearlyValue` scalar (which PVsyst ignores while `IsMonthly=True`).
- **`none`**: Removes the `Soiling=Soiling` block from `pvMainArray`.

## Exporting hourly soiling columns

When building an `.SFI` export definition with `pvsyst_build_sfi(variables="soiling")`, PVsyst exports two additional irradiance columns (in `W/m²`):
- `GlobSlg`: Effective global irradiance after soiling
- `SlgLoss`: Irradiance loss due to soiling

Note that when soiling is disabled (`mode="none"`), `PVsystCLI.exe` omits `GlobSlg` and `SlgLoss` from the output CSV even if they were requested in the `.SFI` file.

# Native constant and monthly soiling

This extension follows section 5.4, PDF page 28, of the supplied Grid Connected Systems manual. It configures PVsyst's native soiling input; it does not calculate irradiance, PV production or a substitute loss model.

## Public workflow

Inspect with `pvsyst_get_variant_parameters`; the new `main_array.soiling` entry reports the native mode and percentage values. A legacy or unfamiliar representation reports an issue, not a fictional disabled setting.

Use `pvsyst_update_variant_parameters` with the current inspection token and an explicit `soiling_updates` object. Existing preview, backup, no-op and whole-file restore behavior applies. Clone a source variant first when retaining a separate design comparison.

```json
{"mode": "constant", "yearly_loss_pct": 2.5}
```

```json
{"mode": "monthly", "monthly_loss_pct": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]}
```

```json
{"mode": "none"}
```

These are separate alternatives for `soiling_updates`, not combined parameters. The 1%-12% profile is a deliberately distinct verification fixture, not a climate assumption or recommended installation input.

Percentages must be finite JSON numbers in 0..100 at 0.1 percentage-point increments, matching the GUI's three-decimal fraction serialization. Booleans, numeric strings, wrong month counts, unsupported modes and conflicting fields stop before writes. Soiling edits use a separate transaction from other physical parameter changes; `variant_name` may accompany them. Original flags and unrelated fields are preserved. The existing `pvsyst_restore_variant` restores the full saved file.

Verified editing scope is native 8.1.6 PV-only Grid, one unshaded fixed plane/subarray, main-array Flags `$00100400` or `$01900400` and Flags2 `$082A`. Storage variants, active geometry/layout, legacy soiling, file-based profiles and unknown main-array tag blocks remain outside this bounded editor. No site-dependent default is inferred.

Use `pvsyst_build_sfi(..., variables="soiling")` for the common PV outputs plus native `GlobSlg` and `SlgLoss`, both exported in W/m2 with the standard preset units. The native program omits these two columns when soiling is disabled; the adapter does not fill them with invented zeroes. Compare the common columns for disabled controls.

## Native entry and persistence evidence

An isolated workspace copied the accepted original-database grid design and its existing SIT/MET. The installed 8.1.6 GUI loaded the project and saved new variants without modifying the original files. Its Detailed Losses / Soiling page exposes four modes: none, constant, monthly and from file. Only the first three are exposed here.

The GUI showed percentage units for the annual and all twelve monthly inputs. Its own saved constant 2.5% setting was:

```text
  Soiling=Soiling
    IsYearly=True
    IsMonthly=False
    IsFromFile=False
    YearlyValue=0.025
  EndTags
```

The monthly 1%-12% setting saved `IsYearly=False`, `IsMonthly=True`, `IsFromFile=False`, an inactive `YearlyValue`, and:

```text
    MonthlyValue=0.010,0.020,0.030,0.040,0.050,0.060,0.070,0.080,0.090,0.100,0.110,0.120,
```

Disabling soiling omitted the entire `Soiling` block. This is a native named tag block, not `PVObject_Soiling` and not the old `SoilingLoss` scalar. Its owning object is `PVObject_PVMainArray=pvMainArray`.

Opening and confirming the loss dialog also refreshed unrelated default/cache fields and set extra flags. A separate disabled control retained those refreshed fields/flags. They were therefore not treated as soiling switches; the adapter changes only the named Soiling block. The GUI was closed normally and its temporary workspace key restored before adapter edits or CLI acceptance.

## Native annual references

The same 440 kWp / 360 kWac original-database design, fixed 15/0-degree orientation and explicitly loaded weather were run for all 8760 hours of 2022:

| Native GUI setting | Grid energy, kWh | Standard annual PR |
|---|---:|---:|
| Disabled control | 799980.9366 | 81.2162233% |
| Constant 2.5% | 785183.8022 | 79.7139783% |
| Monthly 1%-12% | 758884.3035 | 77.0439822% |

All three completed with regular hourly coverage and no existing annual-anomaly flags. The disabled result preserves the previous complete-cycle reference. Energy changes need not equal the input percentage: PVsyst itself calculates irradiance, temperature and clipping interactions.

The native program exported `GlobSlg` and `SlgLoss` for active monthly/constant settings. Two other candidate variable identifiers did not appear in these probes and were not added to the preset. This is deliberately narrower than a claim that every soiling variable is available.

## Public MCP acceptance

A fresh SDK stdio client exercised the published schema and existing parameter tool, not a mocked serializer. MCP-created monthly and constant profiles each reproduced the corresponding GUI-created reference for all 8760 timestamps and all 15 numeric columns. Switching from a constant source to a monthly profile also reproduced the GUI monthly control; the inactive YearlyValue was not mistaken for an extra applied loss. Disabling the profile reproduced the native disabled file and its common hourly outputs.

Native 24-hour boundary runs also accepted constant 0% (unchanged common PV outputs) and 100% (zero effective irradiance and grid output in this reference case), without warnings. A default-workspace copy of the GUI controls was provided as `TUTORIAL_SOILING_NATIVE.PRJ`; an MCP update from the pristine `$00100400` template preserved its flags and matched all 15 numeric columns of the GUI constant reference.

Preview created no changes, repeated updates created no backup, and the existing whole-file restore recovered the exact pre-edit bytes. Eleven schema/parameter/legacy negative cases stopped without changing the target. Additional temporary metadata checks covered duplicate blocks, mutually active modes, file profiles, nonfinite values, missing closing tags, legacy mixing and foreign ownership. Constant percentage readback also avoids float noise that would otherwise break a repeated GUI-precision update.

Reading only native `SlgLoss` and `GlobInc` on daylight rows verified consumption of all twelve distinct monthly inputs; observed loss percentages agreed within 0.002 percentage points, a CSV-rounding check rather than a physical-model tolerance. The disabled annual control matched the earlier complete-cycle reference at all 8760 timestamps and all 13 common numeric columns.

Raw GUI-saved variants, screenshots, CLI results and MCP acceptance records remain separate local study artifacts; no commercial models, weather data or simulation files are included in the repository.

# Native constant and monthly soiling

`pvsyst_get_variant_parameters.main_array.soiling` reports native mode and percentages; unfamiliar/legacy forms disclose an issue, not a fictional disabled setting. The writer configures native inputs, not irradiance or a replacement loss model.

## Input

Use the current token with one explicit soiling_updates alternative:

```json
{"mode":"constant","yearly_loss_pct":2.5}
```

```json
{"mode":"monthly","monthly_loss_pct":[1,2,3,4,5,6,7,8,9,10,11,12]}
```

```json
{"mode":"none"}
```

These illustrate shape, not climate/cleaning recommendations. Percentages are finite JSON numbers0..100 in0.1 percentage-point steps; booleans, strings, wrong month counts, mixed modes or conflicting fields stop. Use a separate physical transaction; variant_name may accompany it. Preview, same-value no-op, backup and whole-file restore apply.

## Native storage and scope

Only native8.1.6 PV-only Grid, one unshaded fixed plane/subarray, main Flags=$00100400 or$01900400 and Flags2=$082A are supported. Storage, active geometry/layout, legacy SoilingLoss, file profiles and unknown tag blocks stop.

The owner is pvMainArray's named Soiling=Soiling block, not PVObject_Soiling. Constant2.5% stores IsYearly=True, IsMonthly=False, IsFromFile=False, YearlyValue=0.025. Monthly stores twelve fraction values, its mode flags and an inactive YearlyValue; that inactive scalar is not an extra applied loss. None removes the whole block. Other mode bits/cache refreshed by GUI loss dialogs are not treated as soiling switches.

## Results

The soiling SFI preset adds native GlobSlg and SlgLoss inW/m². Disabled native runs may omit both; compare common columns rather than inventing zeroes. Complete-year native controls exist for constant/monthly modes, but input percentages need not equal energy changes because PVsyst handles temperature,irradiance and clipping interactions. Editing does not establish site assumptions, file-based models or automatic cleaning/snow behavior.

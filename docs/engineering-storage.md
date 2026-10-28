# Native AC-storage engineering scenarios

`pvsyst_run_engineering_task` supports revising and simulating existing centralized AC-coupled battery storage projects alongside its fixed-plane and tracking workflows. It binds a validated hourly load profile, updates storage parameters in an isolated project copy, runs a full calendar year through `PVsystCLI.exe`, and feeds into `pvsyst_compare_engineering_scenarios` and `pvsyst_export_engineering_delivery`.

## Request format

Start from a single-subarray, single-fixed-orientation grid project that already has AC-coupled storage enabled and no active 3D shading scene or Module Layout. Pass `changes.storage` and `load_manifest_name` in addition to the standard engineering revision fields:

```json
{
  "spec": {
    "source_project": "AC_STORAGE_SOURCE.PRJ",
    "source_variant": "VCLOAD",
    "new_project": "AC_STORAGE_CANDIDATE.PRJ",
    "new_variant": "VCSTORAGE",
    "objective": "Compare a 2 kW AC charger input limit against the baseline 3 kW limit",
    "year": 2024,
    "nominal_dc_power_kw": 36,
    "power_source": "Project equipment schedule: 60 modules rated 600 W each.",
    "changes": {"storage": {"max_charge_ac_kw": 2}},
    "load_manifest_name": "LOAD_2024_load.json",
    "report_pages": ["summary", "usersneeds", "results", "losses"]
  },
  "dry_run": true
}
```

Requirements and behavior:
- **Site & timezone alignment**: The source `.PRJ` and `.VCx` must point to the same workspace `.SIT`/`.MET` files, and the site's fixed standard UTC offset must match the timezone offset recorded in `load_manifest_name`.
- **Baseline reference runs**: Passing `changes: {"storage": {}}` keeps the existing `SelfConsumption` storage parameters untouched so you can generate a bound baseline run manifest for scenario comparison.
- **WeakGrid availability**: If the source variant uses the `WeakGrid` strategy, you must also supply the complete 0/1 hourly grid `availability` declaration matching the simulation year and timezone offset.
- **Load cache verification**: Before running, the workflow checks the prepared load manifest against the variant's embedded `Energy/MJ` base62 cache, calendar dates, units, and encoding, and after the simulation finishes it verifies that the exported `E_User` column matches the prepared hourly load hour by hour.

## Exported columns and balance checks

The workflow uses the `grid_storage` `.SFI` variable preset so the annual CSV includes all required AC and DC storage columns:
- Load and grid flows: `E_User`, `EFrGrid`, `E_Grid` / `EToGrid`, `E_Miss`, `EUnused`, `CL_Over` (converter overload / clipping loss)
- Battery DC flows and state of charge: `EBatCh`, `EBatDis`, `SOC_Beg`, `SOC_End` (or `SOC`)

Post-simulation checks verify that:
- The output covers all 8760 or 8784 hours of the requested calendar year with finite values and expected units.
- Initial SOC, minimum/maximum SOC bounds, AC charger input power, and battery inverter AC output power stay within the configured limits (accounting for converter losses rather than treating battery-side DC `EBatCh`/`EBatDis` as AC ratings).
- When all required AC flow columns (`CL_Over`, etc.) are present, the hourly AC energy balance closes within CSV rounding precision.
- Standard PV-only performance ratio (`E_Grid / (GlobInc × PNom)`) is skipped for storage runs because battery charging/discharging and self-consumption decouple grid export from array yield.

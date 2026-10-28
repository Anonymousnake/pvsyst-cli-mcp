# Standalone backup generator settings

For standalone battery systems (`SystemType=Battery` in PVsyst 8.1.0 / 8.1.6 `.VCx` files), `pvsyst_get_variant_parameters` exposes the backup generator configuration under `generator`, and `pvsyst_update_variant_parameters` updates it via `generator_updates`.

## Fields modified in `.VCx`

A `generator_updates` transaction touches four places in the variant file:
- System enable bit: bit `0x20` on `pvSystem.Flags`
- Generator reference: `GensetFile` (pointing to a `.GEN` file in `ComposPV`)
- Operating power: `PEffBackUp` (kW)
- Per-subarray start/stop voltage thresholds: `VBkUpEncl_syst` and `VBkUpDecl_syst`

Requirements and gotchas:
- **Thresholds are normalized voltages, not SOC**: In PVsyst's standalone battery system block, `VBkUpEncl_syst` and `VBkUpDecl_syst` are normalized voltage ratios in `[0, 1]`, not state-of-charge percentages. When enabling the generator, both thresholds must be provided for every subarray in the variant.
- **Operating power vs `.GEN` nominal rating**: `PEffBackUp` must be positive when enabling. If `PEffBackUp` exceeds the nominal power declared inside the `.GEN` file, the tool emits a warning so you can check whether the mismatch was intentional. To edit the `.GEN` file's own nominal rating or fuel consumption parameters, use `pvsyst_update_component`.
- **Disabling**: Setting `enabled=false` clears bit `0x20` on `pvSystem.Flags` while keeping the saved `GensetFile`, `PEffBackUp`, and voltage thresholds in the file.

## Reading generator simulation outputs

When simulating a standalone variant with a backup generator, PVsyst exports:
- `E_BkUp`: Backup generator energy output
- `FuelBU`: Fuel consumption
- `BkUp_ON`: Fractional operating time within each hourly step (a positive value like `0.35` means the generator ran for 35% of that hour, not a full hour)

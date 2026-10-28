# Grid topology, capacity, and loss parameter editing

This guide covers how `pvsyst_update_variant_parameters`, `pvsyst_clone_subarray`, `pvsyst_remove_subarray`, and `pvsyst_validate_variant_structure` inspect and modify `.VCx` subarray quantities, MPPT modes, fixed orientations, and basic loss scalars in PVsyst 8.1.0 / 8.1.6 grid projects.

## Preconditions and transaction rules

1. **No active 3D shading or Module Layout**: Structural and count edits require an empty shading block (`Flags` in `$00`, `$00020000`, or `$00080000`, with no 3D objects, shading tables, or `PVObject_ModuleLayout` sections) so changing string/module counts never leaves a 3D scene or module layout map out of sync. Pure scalar edits (`NModSerie`, `FieldTilt`, `FieldAzim`) also accept legacy variants that omit the shading block entirely.
2. **Strict integer selectors**: `subarray_id`, `source_subarray_id`, and `orientation_id` must be positive JSON integers (`1`, not `"1"`, `1.0`, or `true`).
3. **Optimistic locking & rollback**: Every write checks `expected_sha256`, writes a backup snapshot before modifying the `.VCx` file (unless the update is a no-op with identical values), and returns a token for `pvsyst_restore_variant`.

## Ordinary subarray capacity edits

For standard subarrays without independent MPPT mode (`NInvMPPT=1`, no `$0400` bit, no explicit MPPT child nodes, no power sharing, and all strings on one fixed plane):

```json
{
  "subarray_updates": {
    "NInverter": 12,
    "NStringCh": 80
  }
}
```

- Updating `NInverter` alone scales the inverter group count while keeping the number of strings per inverter constant.
- Updating `NStringCh` alone scales the parallel string leaves while keeping the inverter group count constant.
- Updating both together scales each string leaf's multiplicity by `(new_strings × old_inverters) / (old_strings × new_inverters)` (which must divide evenly to an integer).
- `NElements`, `NInverter`, `NStringCh`, and `NStrOrient1` are updated together. Note that PVsyst omits `NElements` when its value is `1`; if a node's multiplicity needs to change, `NElements` must already be present in the serialized block.

## Structure validation (`pvsyst_validate_variant_structure`)

Checks orientation list counts, subarray-to-orientation references, circuit tree ownership, and inverter/MPPT/string/module totals (see [grid circuit inventory](grid-circuit-verification.md) for node-level details):
- In independent MPPT mode (`Flags` bit `$0400`), `NInvMPPT` is compared directly against the sum of expanded explicit MPPT inputs (not `NInverter × NInvMPPT`).
- For shared inverters (`PowerSharingInverterId=0` with bit `$0100`), parent inverters are deduplicated across homogeneous member subarrays when computing project totals.
- Returns `valid=true` when all checks pass, `valid=false` when a mismatch is found, or `valid=null` if the variant uses an unchecked layout (such as Dome orientations or unfamiliar node types).

## Cloning and removing subarrays

- `pvsyst_clone_subarray`: Copies a subarray along with all of its exclusively owned inverter/MPPT/string groups in the circuit tree, assigning fresh IDs higher than any ID currently used across the project's variants.
- `pvsyst_remove_subarray`: Deletes a subarray and its exclusively owned circuit branches, provided another subarray still uses the assigned orientation plane (or pass `remove_orientation=true` to delete the plane at the same time).

## Fixed-plane lifecycle

You can manage multiple fixed planes (`FixedPlane` with `Flags=$00` and no tilt/azimuth averaging) across standard or exclusive independent-MPPT (`$0400`, `PowerSharingInverterId=-1`) subarrays:
- **Append a plane (`clone_orientation=true`)**: Appends a new sequential `FixedPlane` to the orientation list, increments the list count, and binds the target subarray's `NoOrient` / `NoOrientation` to the new plane ID.
- **Reassign a subarray (`subarray_updates.NoOrientation`)**: Moves a subarray onto another existing fixed plane, provided no existing plane is left orphaned without a subarray.
- **Delete an exclusive plane (`remove_orientation=true`)**: Deletes the subarray and its exclusively used fixed plane. If higher-numbered planes exist after the deleted plane, their IDs (and all subarray `NoOrientation` references pointing to them) are shifted down by 1, and `orientation_id_map` returns the old-to-new ID mapping.

## Independent MPPT (`$0400`) and power sharing (`$0100`)

For variants using explicit MPPT nodes (`Flags` bit `$0400`):

1. **Toggle independent MPPT mode (`independent_mppt_enabled`)**: Standalone boolean transaction for exclusive direct-MPPT subarrays (`PowerSharingInverterId=-1`). Setting `true` enables bit `$0400` and sets root `NInvMPPT` to the sum of explicit MPPT inputs; setting `false` clears `$0400` and resets `NInvMPPT` to `1`.
2. **Scale MPPT input count (`NInvMPPT` only)**: Scales MPPT group multiplicities (`1..10000`) while keeping inverter and string-leaf multiplicities unchanged, synchronizing `NInvMPPT`, `NStringCh`, and `NStrOrient1`.
3. **Scale inverter count (`NInverter` only)**: Scales exclusive inverter-group multiplicities while keeping MPPT and string-leaf multiplicities per inverter unchanged, updating `NInverter`, `NInvMPPT`, `NStringCh`, and `NStrOrient1`.
4. **Scale parallel strings (`NStringCh` only)**: Scales string-leaf multiplicities proportionally while keeping inverter and MPPT counts fixed (also works on an individual member of a shared inverter parent).
5. **Construct a shared inverter parent (`pvsyst_clone_subarray(..., share_inverter=true)`)**: Copies a single-inverter, single-MPPT-input subarray as a second MPPT branch under the *same* physical inverter (setting the parent inverter to `Owner=0` with a combined canonical name, while keeping sharing disabled until toggled).
6. **Toggle power sharing (`power_sharing_enabled`)**: Enables (`PowerSharingInverterId=0`, bit `$0100` set) or disables (`PowerSharingInverterId=-1`, bit `$0100` cleared) automatic power sharing across all member subarrays of a shared inverter parent.
7. **Remove a shared member (`pvsyst_remove_subarray(..., shared_member=true)`)**: Removes one member of a two-member shared inverter parent and returns sole ownership of the inverter to the surviving subarray (`remaining_member_id`). If power sharing is currently active, disable it with `power_sharing_enabled=false` first before removing the member.

## Series module count and fixed angles

- `subarray_updates.NModSerie`: Updates the series module count per string.
- `orientation_updates` (`FieldTilt`, `FieldAzim`): Updates tilt and azimuth on a standard `FixedPlane` (requires `UsesTiltAver`, `UsesAzimAver`, and `UsesSlopeAver` to be absent or `False`).

## Basic array and subarray loss scalars

`pvsyst_update_variant_parameters` supports four basic loss scalars on unshaded 8.1.0 / 8.1.6 grid variants:

| Parameter path | Allowed range | Underlying `.VCx` fields and flag handling |
|---|---|---|
| `subarray_updates.LIDLoss` | `0..100` (%) | Updates `LIDLoss` on the owning subarray |
| `subarray_updates.PMismMPP` | `0..100` (%) | Updates `PMismMPP` and clears default bit `0x00040000` on the subarray `Flags` (if that bit stays set, PVsyst ignores the manual value) |
| `subarray_updates.ModuleQualityLoss` | `[-5, 20]` (%) | Writes the percentage to both `PQualityModU` and `PQualityModI` (negative values represent module positive sorting gain) and clears default bit `0x00100000` while leaving cached `PQualityModP` untouched |
| `main_array_updates.UFactor` | `15..100` (W/m²·K) | Updates constant thermal loss factor `UFactor` on `pvMainArray` (requires standard `Flags=$00100400`, `Flags2=$082A`) |

For notes on how PVsyst GUI rewrites loss flags and cached fields when resaving a variant, see [loss GUI roundtrip notes](loss-gui-roundtrip.md).

# Native feature support summary

This page gives a quick overview of the PVsyst 8.1.6 features supported beyond basic fixed-plane CLI runs. For exact tool names and parameters, see the [tool reference](tool-reference.md) and the [native entry map](native-entry-map.md).

## Feature overview

| Domain | What is supported | Guide |
|---|---|---|
| 3D residential scenes | Author and read back `.pvc2` house/PV meshes, import via the 8.1.6 GUI bridge, bind 1 or 2 roof orientations, verify saved table coordinates, and run full-year CLI simulations | [House PV modeling](house-pv-modeling.md), [Native house task](native-house-task.md) |
| Read-only state inspection | Inspect saved transformer losses, PV unavailability periods, peak-shaving settings, active 3D scene objects, ageing blocks, and IAM profiles via `pvsyst_get_variant_parameters` | [State inspection](native-state-inspection.md) |
| Power factor & PNom | Read, edit, and clear constant or monthly signed `TanPhi`/`TanPhiM` alongside `native`/`active`/`apparent` inverter PNom limits | [Power factor](power-factor.md), [Combined power controls](native-power-controls.md) |
| Ageing & IAM | Edit degradation year/mean/paired-RMS values and toggle bit 20 on existing ageing templates; switch IAM between `component` and `ashrae` | [Model inputs](native-model-inputs.md) |
| PDF report pages | Select specific report sections (`summary`, `losses`, `results`, `horizon`, `near_shadings`, `p50_p90`, etc.) via native CLI `-rp` | [Automatic simulation](automatic-simulation.md) |
| Tracking & sheds | Create variants with updated geometry, axis limits, and backtracking across 9 tracking types, or edit existing `Sheds` / `SunShields` templates | [Tracking controls](native-tracking-controls.md), [Shed controls](native-shed-controls.md) |
| Storage scalar edits | Update SOC thresholds, AC charge/discharge limits, efficiency, and `allow_grid_export` on existing `SelfConsumption`, `WeakGrid`, or `PeakShaving` variants | [Grid storage parameters](grid-storage-parameters.md), [Peak shaving](native-peak-shaving.md) |
| MV night disconnect | Toggle `NightDisconnect` and main-array bit 29 on single-MV transformer variants | [MV night disconnect](native-mv-night.md) |
| AC cabling | Enable or edit uniform per-inverter AC cables (`0.1 m` length precision, inherited wire section and metal) while keeping DC string cables intact | [AC cabling](native-ac-cabling.md) |
| DC cabling GUI bridge | Drive an open PVsyst 8.1.6 GUI session to set DC wiring resistance (`mOhm`) or STC loss (`%`), let PVsyst recalculate coupled fields, and save as a new variant | [DC cabling bridge](native-dc-cabling.md) |

## Implementation notes

- **Why DC cabling uses the GUI bridge**: In PVsyst 8.1.6, changing the DC loss percentage in `.VCx` text without updating the coupled resistance fields across the subarray and circuit tree causes inconsistent state. Rather than reverse-engineering and reimplementing PVsyst's internal wire-sizing math, `pvsyst_create_native_dc_cabling_variant` sets the input in the GUI dialog and lets PVsyst write out the recalculated values.
- **Process coordination**: All MCP tools in a server process share a single workspace `RLock` and validate file SHA-256 hashes before writing. GUI bridge tools check window titles and process ownership before interacting with PVsyst and never force-kill an existing user session or overwrite `PVsyst.ini`.

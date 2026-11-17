# Native feature support summary

This page gives a quick overview of the PVsyst 8.1.6 / 8.1.7 features supported beyond basic fixed-plane CLI runs. For exact tool names and parameters, see the [tool reference](tool-reference.md) and the [native entry map](native-entry-map.md).

## Feature overview

| Domain | What is supported | Guide |
|---|---|---|
| 3D residential scenes & obstacles | Author and read back `.pvc2` house/PV meshes and environment obstacles (`building`, native `oeTree` `tree`, `wall`, `mesh`), build native `.VCx` scenes headlessly (`method="native"` default, `linear` or `module_layout` with explicit `string_layout`), verify saved geometry, and run CLI simulations | [House PV modeling](house-pv-modeling.md), [Native house task](native-house-task.md) |
| Read-only state inspection | Inspect saved transformer losses, PV unavailability periods, peak-shaving settings, active 3D scene objects, ageing blocks, and IAM profiles via `pvsyst_get_variant_parameters` | [State inspection](native-state-inspection.md) |
| Power factor & PNom | Read, edit, and clear constant or monthly signed `TanPhi`/`TanPhiM` alongside `native`/`active`/`apparent` inverter PNom limits | [Power factor](power-factor.md), [Combined power controls](native-power-controls.md) |
| Ageing & IAM | Edit degradation year/mean/paired-RMS values and toggle bit 20 on existing ageing templates; switch IAM between `component` and `ashrae` | [Model inputs](native-model-inputs.md) |
| PDF report pages | Select specific report sections (`summary`, `losses`, `results`, `horizon`, `near_shadings`, `p50_p90`, etc.) via native CLI `-rp` | [Automatic simulation](automatic-simulation.md) |
| Tracking & sheds | Create variants with updated geometry, axis limits, and backtracking across 9 tracking types, or edit existing `Sheds` / `SunShields` templates | [Tracking controls](native-tracking-controls.md), [Shed controls](native-shed-controls.md) |
| Storage scalar edits | Update SOC thresholds, AC charge/discharge limits, efficiency, and `allow_grid_export` on existing `SelfConsumption`, `WeakGrid`, or `PeakShaving` variants | [Grid storage parameters](grid-storage-parameters.md), [Peak shaving](native-peak-shaving.md) |
| MV night disconnect | Toggle `NightDisconnect` and main-array bit 29 on single-MV transformer variants | [MV night disconnect](native-mv-night.md) |
| AC cabling | Enable or edit uniform per-inverter AC cables (`0.1 m` length precision, inherited wire section and metal) while keeping DC string cables intact | [AC cabling](native-ac-cabling.md) |
| Headless DC cabling | Set DC wiring resistance (`mOhm`) or STC loss (`%`, via 1-diode `.PAN` STC MPP solver) headlessly and write a new `.VCx` variant; built-in DB modules allow `resistance` mode without `.PAN` access | [DC cabling](native-dc-cabling.md) |

## Implementation notes

- **Headless DC cabling & 3D scenes**: Both `pvsyst_create_dc_cabling_variant` and the default `"native"` 3D house/obstacle pipeline emit verified PVsyst 8.1.6/8.1.7 `.VCx` records directly without requiring an open PVsyst GUI session or `native/scene.py` (`PVsyst.ini` is never touched). `pvsyst_open_native_project` remains available strictly as an explicit interactive desktop helper.
- **Process coordination**: All MCP tools in a server process share a single workspace `RLock` and validate file SHA-256 hashes before writing. `pvsyst_open_native_project` checks window titles and process ownership before interacting with PVsyst and never force-kills an existing user session or overwrites `PVsyst.ini`.

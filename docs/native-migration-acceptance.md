# Native entry support summary

Native entries reuse PVsyst calculations and existing transaction/recovery helpers. This is a bounded capability summary, not full GUI or tutorial parity. Use the[tool catalog](tool-reference.md) for current names and restart a running server when its schema changes.

| Entry | Supported scope |
|---|---|
| House chain | Explicit house/PV geometry, native import/association, validity/table, new-variant save/reload and annual consumption;[house workflow](house-pv-modeling.md) |
| Read-only state | Owner-aware transformer/outage/peak/scene/ageing/IAM records, with unknown shapes explicit;[inspection](native-state-inspection.md) |
| PF/PNom | Constant/monthly bounded read/edit/clear and native annual consumption;[PF](power-factor.md) |
| Ageing/IAM | Existing full templates and native inputs, not external sampling or model curves;[models](native-model-inputs.md) |
| Report pages | Native template/design page selection, not post-processing a PDF;[reports](automatic-simulation.md) |
| Tracking/Shed | Existing supported native types, exact precision and source/candidate gates;[tracking](native-tracking-controls.md),[Shed](native-shed-controls.md) |
| Storage scalars | Preserve strategy/hardware and support recorded Peak timing; native controls limited to48h;[storage](grid-storage-parameters.md) |
| MV night | One supported default-MV profile and dependency bit, native annual consumption;[MV](native-mv-night.md) |
| AC cables | Coupled main/subarray/direct-inverter owners,0.1m precision and preserved DC; annual copper case plus other48h controls;[AC](native-ac-cabling.md) |
| DC bridge | Confirmed already-open GUI setting, native recalculation and SaveAs/reload, both modes and annual consumption;[DC](native-dc-cabling.md) |

DC proportional-resistance inversion is not included: software computes coupled resistance/loss fields. Owner, version, precision, quantity and source/candidate checks remain feature-specific; a new native flag family does not widen all older writers.

SaveAs, readback, input preservation, geometry agreement and simulation consumption are separate outcomes. Desktop coordination uses a single driver and process-local locks, not OS-wide exclusion. GUI bridges do not silently change INI or take over unrelated sessions.

Arbitrary topology, mixed/custom cable profiles, arbitrary MV/HV quantities/tables, every GUI locale/scale/version, hybrid DC battery ports, grid charging, special/random loads/outages and automatic engineering sizing are not implied. Forty-eight-hour controls remain48h, not annual evidence. See the[native entry map](native-entry-map.md) and each linked guide for exact limits.

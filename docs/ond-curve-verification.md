# OND file-curve editing verification

Verified on 2026-09-30 with PVsystCLI 8.1.6. Binary SHA-256:
`e6526e0bb1b42cfcff62cba862ae54165dc55d89b1ba638a583cb7ae3bd46f4b`.
Vendor component files and project data are not distributed with this repository.

## Supported operation

Edit the existing main `Converter/ProfilPIO` in an OND file declaring
`Version=8.1.6`, with `Mode=1` and unchanged effective point count. Its axes
are input power and output power in watts; display-unit settings do not
change the serialized point units in the tested file.

The root inverter Flags bit 4 (`0x10`) selects the automatic profile. An earlier
ordinate-only experiment with this bit set produced identical simulation
results. Independent native inspection located the parent-flag test in
`UsesProfilAuto`; paired CLI runs then confirmed the file points are consumed
when that bit is cleared. The three-voltage selection uses bit 12 (`0x1000`)
and is explicitly excluded from the editor pending separate validation.

Only the main curve and explicit automatic-to-file switch are exposed. The
editor preserves other flag bits, nested Flags, counts, padding and unrelated
fields. It does not infer or recompute a complete physical model.

## Reproducible integration test

Set `PVSYST_CLI`, `PVSYST_WORKSPACE`, `PVSYST_EDITOR_PROJECT`,
`PVSYST_EDITOR_INVERTER` and `PVSYST_EDITOR_LAB`, then run:

```powershell
python tests/smoke_live_ond_curves.py
```

The script copies the selected source workspace into two independent
directories and verifies that the selected variant references the inverter.
Both cases use real MCP stdio inspection, preview, update, SFI generation,
simulation and recovery. Both use file mode: one keeps the original points;
the other keeps X and multiplies every active Y by 0.9. It retains local
previews, file hashes, output CSVs and `evidence.json` for inspection.

The recorded run used two days, with 48 complete hourly rows per case.
Irradiation and timestamps matched. Seven rows had equal DC input and equal
exported non-operating loss values; their AC outputs matched a factor of 0.9
within 0.0001 kW (CSV rounding). For example:

| DC input (kW) | Original AC (kW) | Edited AC (kW) |
|---:|---:|---:|
| 3.9439 | 3.7890 | 3.4101 |
| 4.2247 | 4.0642 | 3.6578 |
| 3.8859 | 3.7322 | 3.3590 |
| 4.0507 | 3.8937 | 3.5043 |

Both edited copies were restored exactly and the source workspace's file
manifest remained unchanged. Offline tests use synthetic fixtures and cover
ambiguous fields, unsupported modes, strict numeric input, stale hashes,
atomic scalar/curve previews, flag scoping and byte-exact restoration.

## Interpretation limits

The recorded total grid energy increased from 94.9051 to 158.0426 kWh despite
lower ordinates. The source project also exported unusually large limiting
losses, and some operating points changed between cases. These aggregates are
not evidence of better efficiency or engineering validity. The matched-input
rows demonstrate consumption of edited ordinates in this native path.

This does not verify every OND parameter, other PVsyst versions, three-voltage
interpolation, point-count changes or consistency between curves and derived
scalars. The reader therefore leaves each inspected model's
`simulation_effect` as `unverified`; callers must simulate their own changes.

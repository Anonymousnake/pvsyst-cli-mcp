# BTR capacity curve verification

Verified on 2026-09-30 with PVsystCLI 8.1.6. Binary SHA-256:
`e6526e0bb1b42cfcff62cba862ae54165dc55d89b1ba638a583cb7ae3bd46f4b`.
Vendor component files and project data are not distributed with this repository.

## Supported contract

The editor supports one existing root capacity curve in a BTR file declaring
`Version=8.1.6`, using `Pb_Sealed_AGM` or `Pb_Sealed_Gel`, and `Mode=1`.
`CapaCourant` is the CLI tag. The GUI-style `Capa_DischRate` alias is not loaded
by the tested CLI; activating it requires explicit `use_file_curve=true` with
replacement points. This changes only the selected tag and active points.
Other curves, chemistry, Flags, point counts and inactive rows are preserved.

Independent reverse-engineering experiments established that Mode 1 is linear,
X is discharge duration in hours, and Y is a capacity multiplier relative to
C10. The native curve path requires more than three active points and depends
on battery technology. For the supported lead-acid technologies, the native
capacity check compares Y(100 h) with the inclusive interval [1.15, 1.45].
AGM experiments reproduced both the calculated rejection values and this
interval; a Gel experiment produced the same result as the AGM control.
Optional `bt` prefixes are tolerated; space-separated technology values failed.

The editor additionally requires positive X/Y, increasing X, nondecreasing Y
and an X range containing 100 h. These are deliberately narrow edit restrictions.
The CLI can extrapolate beyond the final point, but that operation is excluded:
renaming a curve whose X range ended at 10 h produced a negative capacity ratio
in prior experiments. The editor neither rescales the X axis nor invents a
replacement curve. Other chemistries and cubic modes remain unsupported.

## Reproduction through MCP

Configure `PVSYST_CLI`, `PVSYST_WORKSPACE`, `PVSYST_EDITOR_PROJECT`,
`PVSYST_EDITOR_BATTERY`, `PVSYST_EDITOR_LAB` and `PVSYST_BATTERY_POINTS`.
The last variable names a JSON file with a complete valid point set for the
selected battery's active count. Both the supplied curve and its Y×0.9 version
must pass. Optional variant/date variables are documented in the script.

```powershell
python tests/smoke_live_battery_curves.py
```

The script copies the source workspace twice and verifies the selected
variant's battery reference. Both cases use MCP stdio for inspection, preview,
activation, SFI generation, simulation and recovery. One case uses the supplied
points; the other keeps X and multiplies Y by 0.9. The recorded run preserved
seven active points, and used an AGM battery in an existing standalone project.

| Measurement | Supplied curve | Y × 0.9 |
|---|---:|---:|
| Hourly rows | 48 | 48 |
| Interpolated C100/C10 | 1.424706 | 1.282235 |
| Mean effective capacity (Ah) | 454.000000 | 408.595208 |

The edited-to-control mean capacity ratio was 0.89998945, or a 10.0011%
decrease. Timestamps and irradiation matched. Both copies were restored to
their original SHA-256, and the source workspace manifest remained unchanged.
Local evidence contains previews, candidate hashes and result CSVs.

Offline tests use synthetic fixtures. They cover explicit tag activation,
duplicate or competing aliases, invalid points and ratio boundaries, supported
technology/mode guards, padding preservation, no-op edits, mixed scalar/curve
previews and exact byte restoration. The MCP lifecycle also checks rejected
activation, rejected capacity ratios and stale hashes.

## Limits

This verifies consumption of supplied capacity points in the tested native
path. It does not establish battery aging, electrical compatibility, chemistry
conversion, other curve semantics, other PVsyst versions or GUI round-trip
compatibility of a CLI-specific tag. A successful static edit is not a physical
model certification; simulate each referencing project after editing.

Inspection's `cli_recognition` reports known tag names, while `editable` reports
structural support for replacing points. `value_errors` may still require
repair. Neither tag recognition nor a valid C100/C10 ratio proves all battery
parameters are consistent. The reader retains `simulation_effect="unverified"`
for the particular file being inspected.

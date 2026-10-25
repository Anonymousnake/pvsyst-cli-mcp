# Native AC-storage engineering scenarios

The existing `pvsyst_run_engineering_task` also composes an existing centralized
AC-coupled storage design with its prepared hourly load, native annual run, scenario
comparison and delivery. It reuses `create_grid_storage_variant`, the existing load
serializer and PVsyst itself. No dispatch, battery chemistry or hybrid DC-port model
is implemented here.

## Request

Use an existing one-subarray/one-fixed-orientation Grid storage project with no active
scene or module layout. Explicitly provide `changes.storage`, `load_manifest_name` and
the normal engineering task inputs. The source PRJ/VC must bind the same workspace
SIT/MET. The site's standard UTC offset must match the prepared load. An empty storage
object preserves an existing SelfConsumption design for a new, bound reference run.

```json
{
  "spec": {
    "source_project": "AC_STORAGE_SOURCE.PRJ",
    "source_variant": "VCLOAD",
    "new_project": "AC_STORAGE_CANDIDATE.PRJ",
    "new_variant": "VCSTORAGE",
    "objective": "Compare a 2kW charger AC input limit with the existing 3kW limit",
    "year": 2024,
    "nominal_dc_power_kw": 36,
    "power_source": "Provide the real inherited module count, nameplate power and source",
    "changes": {"storage": {"max_charge_ac_kw": 2}},
    "load_manifest_name": "STORAGE_D4_LOAD_2024_load.json",
    "report_pages": ["summary", "usersneeds", "results", "losses"]
  },
  "dry_run": true
}
```

This is the local acceptance case's shape, not a portable bundled fixture. Supply legal
local assets and actual engineering declarations. All original hardware, topology,
battery model/counts/temperature/SOW and unrequested controls remain inherited. WeakGrid
uses the existing complete `availability` declaration; no-availability empty-update
references only accept the existing SelfConsumption triplet. New WeakGrid/Peak yearly
engineering-task evidence is not claimed by the SelfConsumption acceptance below.

The prepared load is checked against the source VC's actual complete native cache,
dates, DST, renormalization, units and encoding, not just a matching filename. Historical
absolute paths are metadata and are never followed outside the workspace. Native
execution must then reproduce E_User hour by hour. The run binds the load record, raw
and normalized load CSVs, BTR if available, project/VC, models and SIT/MET.

## Result interpretation

The native output must cover the entire hourly year with finite actual columns and
explicit units. Load, grid import/export, battery charge/discharge and SOC are required.
SOC's first sample and declared range, charger AC input and inverter AC output limits
are checked at native export precision. Full AC-flow consistency is checked only when
all its actual columns exist; missing columns are disclosed and never replaced by zero.

EBatCh/EBatDis are native battery-side quantities. AC limit checks use their native
converter-loss columns; they do not compare DC power directly with AC ratings. Export
precision is explicitly reported, separate from engineering/device tolerances. No
capacity-times-SOC formula, optimal sizing claim or hardware/BMS certification is made.
The PV-only E_Grid standard PR formula is not applied to self-consumption/storage.

Comparison exposes actual grid import/export, load, unmet load, battery/DC flows, unused
energy and independent SOC extrema/times. Only bound identical load assets and verified
caches permit normalization of workspace-location metadata; all other input differences
remain visible. Delivery uses the same bound multi-scenario ZIP as fixed projects.

## Acceptance

PVsystCLI 8.1.6 rev49278, Beijing ERA5 2024/LegalTime93, prepared load 8784 hours,
local_standard_interval_start/UTC+8. The original Geneva-bound source is preserved; the
public clone/source-binding/load tools prepare a separate Beijing-bound source first.
The inherited experimental system has 60x600Wp, one 50kWac inverter, a generic 16-unit
lead-acid battery pack, declared monthly20C/SOW1 and SOC0.2..0.9. These are integration
inputs, not a new equipment or climate certification.

| Native output | 3kW AC charging reference | 2kW AC charging candidate |
|---|---:|---:|
| Hours | 8784 | 8784 |
| User load kWh | 33285.6000 | 33285.6000 |
| Grid import kWh | 12452.1871 | 13524.1537 |
| Grid export kWh | 36732.4523 | 38277.0826 |
| Battery DC charge kWh | 7759.9067 | 6297.6991 |
| Battery DC discharge kWh | 6375.1715 | 5254.9418 |
| Unmet load / unused kWh | 0 / 0 | 0 / 0 |
| SOC range, ratio | 0.4137..0.9000 | 0.4110..0.8842 |

Each run executed once. Both return completed/native_consumption_verified=true with
unchanged sources, zero pointwise load difference, initialSOC0.5, observed AC charge
limits3/2kW and discharge5kW. Complete AC-flow residual maxima are0.001/0.0005kW.
Both original three-page native PDFs were read and visually inspected: Beijing/UTC+8,
load filename,60 modules,16 batteries and Self-consumption are shown. Native summary,
main-results and loss pages remain original; omitted usersneeds pages are not fabricated.

The public comparison is fully bound and finds exactly one saved engineering difference:
storage.max_charge_ac_kw3→2. Its Chinese report, native files and input notes are included
in one local ZIP. The new storage public contract passed without skips; it covers early
raw-load/offset rejection and this real comparison/delivery using isolated copies and a
nonexecutable CLI. The affected fixed comparison contract also passed. No additional
native runs, full discovery, legacy smoke, hash tests or performance benchmark were used.

Requests, original failures if any, native outputs and audit remain under local
`pvsyst-engineering-delivery-study/storage-round2`; licensed/source and derived assets
are not committed. Only the public contract in
`tests/contracts/test_engineering_storage_boundary.py` is new permanent verification.

# Native AC-storage engineering scenarios

`pvsyst_run_engineering_task` composes an existing centralized AC-coupled storage design, its prepared hourly load, a native annual run, scenario comparison and delivery. It reuses native storage/load entries and PVsyst calculations, not a separate dispatch, chemistry or hybrid DC-port model.

## Request

Use an existing one-subarray/one-fixed-orientation Grid storage project without an active scene or module layout. Provide `changes.storage`, `load_manifest_name` and the normal engineering-task inputs. Source PRJ and VC must reference the same workspace SIT/MET, and the site's standard UTC offset must match the prepared load. An empty storage object preserves an existing SelfConsumption design for a new bound reference run.

```json
{
  "spec": {
    "source_project": "AC_STORAGE_SOURCE.PRJ",
    "source_variant": "VCLOAD",
    "new_project": "AC_STORAGE_CANDIDATE.PRJ",
    "new_variant": "VCSTORAGE",
    "objective": "Compare an explicit charger AC input limit with the existing limit",
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

Replace the example with actual workspace assets and engineering declarations. Hardware, topology, battery model/counts/temperature/SOW and unrequested controls remain inherited. WeakGrid requires the existing complete `availability` declaration; empty updates without availability accept only the existing SelfConsumption triplet. The SelfConsumption annual path does not establish arbitrary WeakGrid or Peak schedules.

The prepared load is checked against the source VC's complete native cache, dates, DST, renormalization, units and encoding, not merely a filename. Historical absolute paths are metadata, not instructions to access outside the workspace. Native output must reproduce E_User hour by hour. The run binds the load record, raw/normalized CSVs, BTR where available, PRJ/VC, models and SIT/MET.

## Result interpretation

The result must cover the entire hourly year with finite actual columns and explicit units. Load, grid import/export, battery charge/discharge and SOC are required. SOC's first sample and declared range, charger AC input and inverter AC output limits are checked at native export precision. Full AC-flow consistency is checked only when all required native columns exist; missing columns are disclosed, never replaced by zero.

EBatCh/EBatDis are battery-side quantities. AC checks use native converter-loss columns instead of comparing DC power with AC ratings. Export precision is separate from engineering/device tolerances. The adapter does not derive usable energy by capacity-times-SOC, certify hardware/BMS, or apply the PV-only E_Grid PR formula to storage.

Comparison reports actual import/export, load, unmet load, battery/DC flows, unused energy and independent SOC extrema/times. Only bound identical load assets and verified caches permit normalization of workspace-location metadata; other differences remain visible. Delivery uses the bound multi-scenario ZIP described in [engineering delivery](engineering-delivery.md). Successful execution does not clear inherited engineering-review requirements.

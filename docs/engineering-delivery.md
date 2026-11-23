# Existing-project revision and engineering delivery

The engineering workflow tools wrap a common four-step cycle around an existing PVsyst project: inspect what is saved in the `.PRJ`/`.VCx`, clone an isolated project to apply a targeted parameter change and run a full-year simulation, compare two or three runs side by side, and bundle the project and results into a single ZIP archive.

## Workflow steps

1. **Inspect inputs (`pvsyst_project_handoff`)** (requires `expert` in `PVSYST_TOOL_GROUPS`): Read-only check on `(project, variant, declarations?)`. Parses the saved equipment models, inverter/string counts, subarray-to-orientation links, `.SIT`/`.MET` bindings, load profile, and battery storage settings. Caller-provided declarations (such as nominal nameplate power or weather provenance) are kept separate from template-inherited fields, and any missing engineering inputs are returned with concrete follow-up actions.
2. **Preview a revision (`pvsyst_run_engineering_task` with `dry_run=true`)**: Validates the source project and proposed `changes` without writing files. Supported revisions:
   - Fixed-plane grid projects: update `tilt`, `azimuth`, and supported loss scalars on a single-subarray, single-orientation variant (no active 3D scene or Module Layout).
   - `UnlimTracking` projects: update axis/backtracking/angle limits on an existing tracking template.
   - Centralized AC-coupled storage projects: bind a prepared hourly load and update storage scalars (see [engineering-storage.md](engineering-storage.md)).
3. **Execute the revision (`pvsyst_run_engineering_task` with `dry_run=false`)**: Clones the source into `new_project` / `new_variant`, applies the parameter edits, runs a full calendar year through `PVsystCLI.exe`, and verifies the exported CSV/PDF (header context, full 8760/8784-hour calendar, units, finite columns, and standard PR). Writes a run manifest under `Results/<new-project-stem>_engineering_<year>.json`.
4. **Package for delivery (`pvsyst_export_engineering_delivery`)** (requires `expert` in `PVSYST_TOOL_GROUPS`): Bundles `summary.md`, `inputs.md`, the `.PRJ` and selected `.VCx`, the simulation CSV/PDF, and the task manifest into a new `.zip` file under `Results`. If you pass `comparison_name`, the archive also includes the comparison JSON/CSV/Markdown reports and the `.PRJ`/`.VCx`/CSV/PDF assets for all two or three compared scenarios.

Example request for `pvsyst_run_engineering_task`:

```json
{
  "spec": {
    "source_project": "EXISTING_ARRAY.PRJ",
    "source_variant": "VC0",
    "new_project": "TILT_REVISION.PRJ",
    "new_variant": "VCREV",
    "objective": "Compare a 30-degree tilt against the baseline using the same hardware and weather",
    "year": 2024,
    "nominal_dc_power_kw": 21.6,
    "power_source": "Project equipment schedule: 36 modules rated 600 W each.",
    "changes": {"tilt": 30},
    "report_pages": ["summary", "losses"]
  },
  "dry_run": true
}
```

Pass `changes={}` when you want to run an unmodified baseline under the same manifest format so you can feed both runs into `pvsyst_compare_engineering_scenarios`. Note that `nominal_dc_power_kw` must be supplied explicitly from your equipment schedule rather than inferred from cached `.VCx` sizing fields.

## Run manifests and status flags

Each completed revision writes `<new-project-stem>_engineering_<year>.json` in `Results` (`operation="engineering_revision"`, schema version 1). The manifest records the source and working `.PRJ`/`.VCx` hashes, requested vs. applied changes, the runtime input handoff, referenced `.PAN`/`.OND`/`.SIT`/`.MET` files, annual metrics, and output CSV/PDF hashes.

Both `pvsyst_compare_engineering_scenarios` and `pvsyst_export_engineering_delivery` verify this manifest before reading results. If a project file, weather file, loose component file, or output CSV was modified after the run finished, the loader aborts rather than packaging mismatched files.

Key status fields:
- `ready`: Dry-run preview succeeded.
- `stage=completed` and `native_consumption_verified=true`: The CLI run finished and output checks passed.
- `needs_review`: Execution succeeded, but there are open review items (e.g., encrypted built-in database models that cannot be statically inspected, missing caller declarations, or CLI warnings). Exporting a ZIP preserves `task_status` and `task_native_consumption_verified` from the underlying run while keeping the export call's own `native_consumption_verified=false` (since packaging does not run a new simulation).

If a run fails partway through, the working project, manifest, and any partial outputs are left on disk for debugging; pick a fresh `new_project` name when retrying.

### Two-roof house workflow

For residential rooftops with two (or more) roof planes, `pvsyst_run_house_task` chains `.pvc2` scene generation, headless native `.VCx` scene authoring, annual `PVsystCLI.exe` simulation with `recompute_shading=True`, and checkpointed output verification.

Supported structure requirements:
- **Electrical & orientation structure**: One house mesh and paired fixed orientations (`ID 1`, `ID 2`, etc.), with matching subarrays and roof-aligned PV tables. In native multi-orientation layouts, orientation 1 uses `FieldAzim=0` (the `anchor0` reference frame) unless using a supported symmetric/zero-template setup.
- **Native coordinates & inspection**: The authored `.pvc2` scene and native `.VCx` shading/module-layout records are verified before CLI execution, and completed task manifests record `stored_geometry_verified`, `native_consumption_verified`, annual metrics, and the `.pvc2`/`.SFI`/`.PRJ`/`.VCx` artifacts alongside simulation CSV/PDF reports.

# Existing-project revision and engineering delivery

The engineering workflow tools wrap a common four-step cycle around an existing PVsyst project: inspect what is saved in the `.PRJ`/`.VCx`, clone an isolated project to apply a targeted parameter change and run a full-year simulation, compare two or three runs side by side, and bundle the project and results into a single ZIP archive.

## Workflow steps

1. **Inspect inputs (`pvsyst_project_handoff`)**: Read-only check on `(project, variant, declarations?)`. Parses the saved equipment models, inverter/string counts, subarray-to-orientation links, `.SIT`/`.MET` bindings, load profile, and battery storage settings. Caller-provided declarations (such as nominal nameplate power or weather provenance) are kept separate from template-inherited fields, and any missing engineering inputs are returned with concrete follow-up actions.
2. **Preview a revision (`pvsyst_run_engineering_task` with `dry_run=true`)**: Validates the source project and proposed `changes` without writing files. Supported revisions:
   - Fixed-plane grid projects: update `tilt`, `azimuth`, and supported loss scalars on a single-subarray, single-orientation variant (no active 3D scene or Module Layout).
   - `UnlimTracking` projects: update axis/backtracking/angle limits on an existing tracking template.
   - Centralized AC-coupled storage projects: bind a prepared hourly load and update storage scalars (see [engineering-storage.md](engineering-storage.md)).
3. **Execute the revision (`pvsyst_run_engineering_task` with `dry_run=false`)**: Clones the source into `new_project` / `new_variant`, applies the parameter edits, runs a full calendar year through `PVsystCLI.exe`, and verifies the exported CSV/PDF (header context, full 8760/8784-hour calendar, units, finite columns, and standard PR). Writes a run manifest under `Results/<new-project-stem>_engineering_<year>.json`.
4. **Package for delivery (`pvsyst_export_engineering_delivery`)**: Bundles `summary.md`, `inputs.md`, the `.PRJ` and selected `.VCx`, the simulation CSV/PDF, and the task manifest into a new `.zip` file under `Results`. If you pass `comparison_name`, the archive also includes the comparison JSON/CSV/Markdown reports and the `.PRJ`/`.VCx`/CSV/PDF assets for all two or three compared scenarios.

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

### Finite two-roof task: bounded native acceptance

For residential rooftops with two roof planes, `pvsyst_run_house_task` chains `.pvc2` scene generation/reading, GUI scene import, orientation binding, coordinate verification, annual CLI simulation, and ZIP delivery (`operation="engineering_house"`).

Supported layout and environment requirements:
- **Electrical & orientation structure**: One house mesh and two fixed orientations (`ID 1` and `ID 2`), where each orientation owns one exclusive subarray and one PV table using the same `.PAN`/`.OND` models. Orientation 1 must have `FieldAzim=0` (the `anchor0` reference frame).
- **GUI environment**: PVsyst 8.1.6 rev49278 in Chinese locale at 96 DPI, running in an owned workspace session.
- **Coordinate mapping & precision**: During GUI import, PVsyst maps `anchor0` coordinates with inverted X/Y signs and unchanged Z, and can introduce small centimetre-level placement offsets. The bridge opens each PV table's properties dialog to bind the explicit orientation (`#1` / `#2`), exports `.SHD` data to check offsets, applies native position corrections (`Ctrl+B`), and deletes any confirmed empty third orientation group before saving the variant.
- **7-character field check**: Because PVsyst's position inputs accept at most 7 characters, the adapter rejects requested coordinates whose 7-character rounding error exceeds `0.00002 m` (20 µm), and verifies that saved and reloaded table coordinates stay within that `0.00002 m` bound.

Completed house task manifests include `native_reload_verified`, geometry residual checks, and the authored `.pvc2` and `.SFI` files alongside the `.PRJ`/`.VCx` and CSV/PDF reports.

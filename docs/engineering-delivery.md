# Existing-project revision and engineering delivery

Engineering tools organize native PVsyst input handoff, intentional revision, annual execution, comparison and delivery. PVsyst owns physical calculations; caller declarations do not certify equipment, fitted models, weather or site conditions.

## Public workflow

1. `pvsyst_project_handoff(project, variant, declarations?)` reads saved equipment, counts, subarray/orientation/circuit relationships, site/weather, load and storage. It separates declarations from inherited values and reports missing inputs with next actions. It writes nothing and does not invoke the native executable.
2. `pvsyst_run_engineering_task(spec, dry_run=true)` previews an isolated revision. Fixed planes accept explicit tilt, azimuth and supported losses. Existing UnlimTracking templates accept supported tracking controls. Equipment, counts, topology and site/weather remain inherited. The ordinary revision path requires one complete Grid subarray/orientation without active scene or Module Layout. Centralized AC storage additionally requires its bound prepared load; see [storage engineering](engineering-storage.md).
3. Repeat an accepted request with `dry_run=false` to create a new project and VC, reload them through native CLI and run an hourly calendar year. Run-time input handoff and file protections bind the result. Context, complete calendar, units, finite actual columns and the applicable result checks must pass before native consumption is verified. Storage does not use the PV-only PR formula.
4. `pvsyst_export_engineering_delivery(manifest_name, output_name, dry_run=true, comparison_name="")` previews one new Results ZIP. Execution includes readable summary/input notes, selected PRJ/VC and original CSV/PDF. Models, weather and other externally licensed assets remain external dependencies. Other cloned variants are excluded. An explicit bound comparison adds its two/three selected scenarios and JSON/CSV/Chinese Markdown; the primary run must be one of those scenarios, with all runs and report files still consistent.

Use actual workspace assets and engineering declarations:

```json
{
  "spec": {
    "source_project": "EXISTING_ARRAY.PRJ",
    "source_variant": "VC0",
    "new_project": "TILT_REVISION.PRJ",
    "new_variant": "VCREV",
    "objective": "Compare an explicit tilt change with inherited hardware and weather",
    "year": 2024,
    "nominal_dc_power_kw": 21.6,
    "power_source": "Project equipment schedule: 36 modules rated 600 W each.",
    "changes": {"tilt": 30},
    "report_pages": ["summary", "losses"]
  },
  "dry_run": true
}
```

Declare nominal power from the real equipment basis; it is not inferred from filenames or cached sizing. `changes={}` preserves a fixed reference. Tracking requires a nonempty tracking object and its own supported shape/version. Optional declarations provide sourced weather and design-condition notes; missing engineering basis remains visible as review.

## Run binding and status

Results `<new-project-stem>_engineering_<year>.json` records operation `engineering_revision`, schema version1, source/working PRJ/VC, requested/actual changes, run-time input handoff, file/input state, uninspected database references, native context, metrics and output state.

Delivery and scenario comparison use the same run loader. Changed input, output, loose database shadow or mismatched identity stops the binding. Paths stay within controlled workspace/library folders; a manifest does not permit arbitrary file access. These are consistency records, not signed authenticity certificates or frozen native-database copies.

`ready` means preview. `stage=completed` plus `native_consumption_verified=true` means execution and input/output checks completed, not engineering certification. `needs_review` may remain because of database contents, source basis or native warnings. Export itself keeps `native_consumption_verified=false`; `task_status` and `task_native_consumption_verified` separately carry the original task outcome. Comparison review is also reported separately.

Existing names stop before execution. Failures retain the isolated project, manifest and available outputs; there is no automatic retry, overwrite, deletion or rollback. Review them before using a new project name. Historical CSV headers alone do not identify past model contents; unbound results retain review and do not acquire a historical binding from today's project. See [scenario comparison](engineering-scenarios.md).

## Delivery contents

A single-run package includes `summary.md`, `inputs.md`, selected PRJ/VC, original CSV/PDF and its task manifest. House tasks also include their authored PVC2 and SFI. A bound comparison package includes only the selected scenario assets and its three report formats. External dependency and unresolved engineering-basis notes remain explicit; export adds no simulation and never silently clears source review.

### Finite two-roof task: bounded native acceptance

The two-roof path composes `pvsyst_run_house_task`, scene author/reader, native geometry checks and delivery. Input is one house mesh and two ordinary fixed source orientations ID1/2, each owning one complete exclusive subarray and one PV table, using the same PAN/OND. Orientation1 FieldAzim=0 is the anchor. Chinese PVsyst8.1.6rev49278/DPI96 and owned-process/workspace requirements apply.

Nonzero anchor azimuths, additional groups, shared or heterogeneous hardware, Module Layout, electrical shading, storage and arbitrary geometry/topology are outside this path. A successful bounded case does not provide a generic scene identity or coordinate converter.

Per-table native properties select the actual orientation. Native SHD export and position editing establish supported source-to-native correspondence; automatic leaf names or counts alone are not identity. In the anchor0 mapping, source X/Y signs are inverted and Z is unchanged. This rule is not extended to other anchors or scene types.

Native position fields accept seven characters. The adapter accepts a requested position only when its rounding difference at that precision is within0.00002m. Source-to-native checks enforce that bound after native correction and again after save/reload; they do not widen it by module depth or accept import offsets within a depth envelope. Unrepresentable positions or identity/spatial mismatches stop. These are adapter checks, not installation tolerances.

Only an explicitly confirmed empty extra orientation group may be removed. Native validity, saved/reloaded identity, geometry and annual consumption are independent checks. Completed house manifests use `operation=engineering_house` and expose `native_reload_verified`, inherited hardware/circuit information and geometry results. Status may remain needs_review even when phase/stage are completed and native consumption is verified. Successful geometry, annual output or ZIP creation does not erase missing engineering basis or saved-shading review.

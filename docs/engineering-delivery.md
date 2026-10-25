# Existing-project revision and engineering delivery

The Engineering tools organize existing native PVsyst capabilities into input handoff,
intentional revision, native annual execution and delivery. Native PVsyst owns all
physical calculations. Caller declarations do not certify equipment, fitted models,
weather or site design conditions.

Current third-milestone status: **the bounded two-roof public native task and delivery
are accepted for the recorded case**. One house was imported, positioned, associated,
saved/reloaded, consumed by one native annual run and exported as a ZIP. Its completed
stage retains `needs_review` for missing engineering basis and saved-shading review;
the native result is not engineering certification. Fixed, tracking and AC-storage
evidence remains separate.

## Public workflow

1. `pvsyst_project_handoff(project, variant, declarations?)` reads saved equipment,
   counts, subarray/orientation/circuit relationships, site/weather, load and storage.
   Explicit engineering declarations remain separate from inherited values. Missing
   inputs include concrete follow-up actions. A readable handoff does not prove native
   consumption. This operation writes nothing and never calls the native executable.
2. `pvsyst_run_engineering_task(spec, dry_run=true)` previews an isolated revision.
   Fixed planes accept explicit tilt, azimuth and existing supported loss parameters.
   Existing verified UnlimTracking templates accept their supported tracking controls.
   Equipment, counts, electrical topology and site/weather stay inherited. The initial
   revision flow uses one complete Grid subarray/orientation without an active
   scene or module layout. Existing native AC storage also composes a bound prepared
   load and its supported scalar edits; see [storage engineering](engineering-storage.md).
3. Repeat the accepted request with `dry_run=false` to create a new project and a new VC,
   reload through native CLI, run one hourly calendar year and generate native CSV/PDF.
   The task captures its input handoff and existing production input/file protections
   before execution. Output context, complete calendar, units, finite actual columns
   and standard grid PR are checked before marking native consumption verified.
4. `pvsyst_export_engineering_delivery(manifest_name, output_name, dry_run=true, comparison_name="")` previews
   the delivery. Execution writes one new Results ZIP with `summary.md`, `inputs.md`,
   the PRJ, selected VC, and original CSV/PDF. Model, weather and other externally licensed
   assets remain external dependencies. The ZIP does not include other cloned variants.
   An explicit bound comparison adds its two/three selected scenarios and comparison
   JSON/CSV/Chinese Markdown. All referenced runs and report files must remain consistent;
   the primary run must occur in that comparison. Comparison review is reported separately
   from the original primary task status.

Example request shape; substitute actual workspace assets and engineering declarations:

```json
{
  "spec": {
    "source_project": "EXISTING_ARRAY.PRJ",
    "source_variant": "VC0",
    "new_project": "TILT_REVISION.PRJ",
    "new_variant": "VCREV",
    "objective": "Evaluate the explicitly requested tilt change with the existing hardware and weather",
    "year": 2024,
    "nominal_dc_power_kw": 21.6,
    "power_source": "Caller-supplied project schedule: 36 modules at 600Wp; provide the real source",
    "changes": {"tilt": 30},
    "declarations": {
      "weather_basis": {"source": "Provide the actual weather origin and standard time basis"},
      "design_conditions": {"source": "Provide the applicable site and equipment design basis"}
    },
    "report_pages": ["summary", "losses"]
  },
  "dry_run": true
}
```

Nominal power is explicitly declared and cross-checked against the native standard PR;
it is not inferred from device filenames or cached sizing values. A fixed reference
calculation may use `changes={}` to preserve all saved inputs. Tracking uses an explicit
nonempty `tracking` object and retains the recorded native shape/version restrictions.

## Run binding and status

The Results `<new-project-stem>_engineering_<year>.json` records operation
`engineering_revision`, schema version 1, source and working project/VC, requested and
actual changes, the run-time input handoff, working file state, resolved input assets,
uninspected database references, native context, metrics and output file state.

`load_engineering_run` is shared by delivery and scenario comparison. Changed project,
weather/model input, loose database shadow, output or mismatched identity stops use of
that binding. Paths are resolved only inside their controlled workspace/library folders;
the manifest cannot direct the server to arbitrary files. These are local consistency
records, not signed authenticity certificates or frozen copies of the native database.

`ready` means a preview; `stage=completed` plus `native_consumption_verified=true` means
the annual execution and its output/input checks completed. `needs_review` can still be
a completed run with uninspected model contents, missing engineering basis or native
warnings. Export's own `native_consumption_verified` stays false and its `task_status`
and `task_native_consumption_verified` carry the original task state separately.

Existing names stop before execution. Failed tasks retain their isolated project,
manifest and available outputs. No automatic retry, deletion, overwrite or rollback is
introduced. Review failed evidence and use a new project name for a new execution.

Historical CSV headers alone do not identify historical model contents. Unbound results
may be numerically compared with an explicit review state; they cannot acquire a verified
historical input record from today's saved project. See [engineering scenarios](engineering-scenarios.md).

## Milestone progress

The main product is an engineer-led PVsyst evaluation and delivery task. Four business
outputs stay together: a readable input handoff, a comparison of intentional changes,
Chinese explanations of native results and unresolved conditions, and one package with
the native project/results and external-dependency list. New work extends those tasks
through existing native entries rather than adding a second calculation or job platform.

| Milestone | User receives | Current evidence and remaining gate |
|---|---|---|
| 1. Existing fixed/tracking project revision | An isolated revised project, one native year and a Chinese input/change summary | Supported fixed and UnlimTracking public tasks, annual results and selected-file delivery accepted below |
| 2. Purposeful comparison and AC-storage evaluation | Two/three related scenarios, native flow differences, Chinese explanations and a combined ZIP | Fixed and native AC-storage pairs accepted; storage uses its prepared hourly load, SOC and AC balance, not the PV-only PR formula |
| 3. One bounded two-roof house | One authored house, two explicitly mapped PV regions, a saved native project, native year and delivery ZIP | Public task SaveAs/reload, one 2024 native year and ZIP accepted for the anchor0 case below; completed stage retains needs_review |

### Finite two-roof task: bounded native acceptance

This stage composes `pvsyst_run_house_task` with the existing scene author/reader and
delivery consumer. The bounded input is one house mesh and two ordinary fixed source
orientations ID1/2, each owning one complete exclusive subarray and one PV table, with
the same PAN/OND. The tested direction uses orientation1 FieldAzim=0 as its anchor.
Chinese PVsyst8.1.6 rev49278/DPI96 and the existing owned-process/workspace rules remain.
Nonzero anchor azimuths, more groups, shared/heterogeneous hardware, Module Layout,
electrical shading, storage and arbitrary geometry/topology are not opened by this stage.

The local `pvsyst-engineering-delivery-study/roof-round3` input is a single 18m by 10m
house with one shared ridge, two 28-degree roof planes, 9 house meshes, 2 PV tables and
36 modules (18 per source group). It replaces the earlier 70m-separated two-volume
research geometry for this milestone; that earlier case retains its own evidence.

| Layer | Current observation | Meaning |
|---|---|---|
| Per-table native identity | Direct PV-table properties select the actual orientation combo #1 and #2 | Uses native table properties rather than inferred automatic leaf names or counts alone |
| Import position | Native SHD export exposes centimetre offsets before correction; native Ctrl+B position editing removes them | No private shading-record patch or guessed offset model |
| Source/native spatial correspondence | In this anchor0 case native X/Y are source X/Y with both signs inverted and Z unchanged; PV maximum coordinate residual 0.000006m, dimension residual 0.000001m, angle residual 0; all 9 house meshes' directed faces/coordinates match with maximum residual 0 | Recorded serialization observations, not building/engineering tolerances |
| Group validity | Delete only the confirmed empty third group, then native validity=true | Validity alone does not prove saved identity, annual consumption or engineering design |
| Public task closure | Public task saved VC1, switched away and reloaded it with native validity=true, then ran one native year and exported the bound delivery | `phase=stage=completed`, `native_consumption_verified=true`; task `status=needs_review` stays visible |

The local evidence files are `roof-round3/PVsyst8.1_Data/Shadings/ROOF_RAW_IMPORT.SHD`
(imported coordinate/angle differences), `ROOF_PRECISION_PLACED.SHD` (both tables after
native position correction), and `ROOF_FINAL_LINKS.SHD` (only orientations1/2).
`roof-round3/owner-r4/09-snap.png` records the empty third group before deletion and the
native-validity observation. The source request, SHD files and native images remain
local. They record the exploratory position/association evidence; the accepted public
task has its own saved/reloaded verification and annual output below.

The native position fields accept seven characters. The bounded path first checks the
coordinate text at that native precision and allows a requested position only when its
rounding difference is within 0.00002m (20 micrometres). Source-to-native verification
uses that strict coordinate bound after position correction and again after save/reload;
it does not widen the bound by module depth or accept an import offset inside a depth
envelope. An unrepresentable position or an identity/spatial mismatch must stop the task.
These are adapter acceptance gates, not installation or engineering tolerances. The
recorded public task passed them before and after native save/reload; this does not
extend the coordinate transform to nonzero anchors or arbitrary scene types.

The accepted run is recorded by
`roof-round3/PVsyst8.1_Data/Results/TWO_ROOF_DELIVERY_house_task.json` with
`operation=engineering_house`. Its request uses `DUAL_ROOF_SOURCE.PRJ/VCROOF`; the
task creates its own working VC0 and saves `TWO_ROOF_DELIVERY.PRJ/VC1`. The manifest
records `native_reload_verified=true`, preserved hardware, source orientations ID1/2
and circuit, 9 house meshes, 2 PV tables and 36 modules. Reloaded validity is true.
The saved/reloaded geometry has house maximum coordinate residual 0, PV maximum
coordinate residual 0.000006m, dimension residual 0.000001m and angle residual 0.

| Public native result | Recorded value |
|---|---|
| Calendar and actual numeric columns | 2024, 8784 continuous hours, 13 numeric columns |
| E_Grid | 28327.6777kWh |
| EArray | 29233.8248kWh |
| IL_Pmax | 43.8761kWh |
| Standard PR | 90.5920649858% |
| Native attempts | One returned CLI run; recorded seconds=5, not a performance benchmark |
| Task outcome | completed phase/stage, needs_review status, native consumption verified |

The task's actual owned Popen39556 exited normally; native cleanup records no remaining
owned windows. Parent restored INI after the session. The native report has seven pages;
parent read its text and visually inspected every page, including the single shared-ridge
two-roof scene on page4. `TWO_ROOF_ENGINEERING_DELIVERY.zip` was exported with the selected
PRJ/VC1, original CSV/PDF, Chinese summary/input handoff, task manifest, authored PVC2 and
SFI. Models/weather remain external dependencies, and export does not add a simulation.

The completed task keeps `needs_review`: the handoff lacks project engineering-basis
declarations and retains its saved-shading review. The run's native warnings are empty;
neither successful geometry/annual execution nor ZIP creation silently clears the source
review. One `two_roof_delivery_boundary` public contract passed without skip in 3.854s,
using the completed case without another GUI session or annual run. Contract duration is
not native performance evidence.

Exploratory failures remain in the local study: VCROOF was not listed in one GUI source
selection, so that observation used the native-listed VC2; it did not change the public
request's source or the task's own VC0 creation. Asynchronous property-window observations
and the initial truncated position inputs are retained separately. The position correction
and strict precision gates address the latter without treating a centimetre import offset
as acceptable. These earlier diagnostics are not additional successful public tasks.

## Verification evidence

The explicit public contract whitelist for this change is
`tests/contracts/test_engineering_workflow_boundary.py`. Readonly/invalid requests use
an isolated real native-export source and an intentionally nonexecutable CLI. Delivery
uses an explicitly supplied completed native run, packages its selected source bytes,
refuses an existing ZIP and refuses a project changed through the public editor after
the recorded run. Missing fixtures are skips, not passing native evidence.

Native acceptance and any observed failures are recorded separately below; preview,
contract and native execution are distinct evidence.

### Local native acceptance

PVsystCLI 8.1.6 rev49278, 2024 hourly calendar, no GUI or INI operation. Inputs are the
existing experimental public-paper 600Wp PAN/native Generic9kW fixed project and the
existing UnlimTracking database project. Both use the already prepared Beijing ERA5
2024/LegalTime93 weather. These are reproducible integration cases, not manufacturer,
site-climate or optimal-design certifications.

| Task | Explicit change | Native year | E_Grid kWh | Outcome |
|---|---|---:|---:|---|
| ENGINEERING_FIXED/VCRUN | fixed tilt 28 to 30 degrees | 8784 hours | 37119.9650 | completed, native consumption verified |
| ENGINEERING_TRACKING/VCREV | inherited tracker axis 5.1 to 6.1 degrees | 8784 hours | 758715.7390 | needs_review/completed, native consumption verified; database internals uninspected |
| ENGINEERING_REFERENCE/VCBASE | unchanged fixed reference, computed once for comparison | 8784 hours | 36930.3633 | completed, native consumption verified |

Each new task ran once, consumed its requested project/VC/SIT/MET and retained its source.
All actual numeric columns are finite. The fixed outputs have 12 numeric columns and
tracking has 13 including GlobShd; absent native variables are not padded. Original
two-page summary/loss PDFs show 28/0, 30/0 and tracker axis6.1/backtracking/irradiance
optimization respectively. Parent read their text and visually inspected all six pages.

The public fixed comparison binds both complete runs, reports only the declared
orientation change, and obtains +189.6017kWh annual grid energy and +17.0856kWh IL_Pmax.
No result is attributed to a different hardware count, model or weather; no optimality
claim is made. The preserved reference is reused, not recomputed for report export.

The two methods in the engineering workflow contract passed individually without skips:
readonly/rejection behaviour and real-result ZIP plus changed-input refusal. The tests
use isolated copies and a nonexecutable CLI, so they do not add native yearly runs.
Public native responses, requests, manifests, reports and readback remain locally under
`pvsyst-engineering-delivery-study/round1`; source/model/weather and derived native
assets are not uploaded to GitHub.

The scenario contract subsequently passed without skips after the helper correction
below. It covers the bound fixed comparison, unbound review, unit/timestamp refusal,
new-output protection, combined two-scenario ZIP and changed comparison-report refusal.
The only permanent whitelist is the two named engineering contract files, three methods
in total. Necessary compilation and diff review are separate; no full discovery, old
smoke, hash tests, no-hash probes or performance benchmark was run.

Actual public export produced a fixed two-scenario package with comparison JSON/CSV/MD,
both selected PRJ/VC/CSV/PDF sets and Chinese summary/input handoff, plus a tracking
package retaining its needs_review task state. Export runs no new simulation. ZIP member
lists and Chinese summary contents were independently read back. The original reports,
source/model/weather assets and failure evidence remain local.

The first tracking preview selected the older TRACKING_INPUTS template, whose actual
saved references required Hawaii_2020.SIT. That missing input stopped preflight before
any write or simulation. The failed preview is retained; the corrected request uses the
already weather-bound DECLARED_TRACKING_FLOW source. No weather was fabricated or
silently rebound, and the failed preview is not counted as a passing execution.

The first scenario-contract run failed in its disposable CSV time-shift helper: native
CSV contains a blank row after the units, so `header+2` was not the first data row. The
helper raised IndexError before the intended timestamp refusal call. Other subtests
continued, but this run is not accepted as a passing contract. The subTest context also
allowed its outer success cleanup to remove the temporary copy; this diagnostic mistake
is recorded rather than hidden. The original native fixture, exact test input and error
remain reproducible. The helper is changed to find the first actual timestamp row and
negative cases propagate failures to the existing retain-on-failure handler. No product
behaviour or acceptance target is relaxed, and no native year is repeated.

### Final targeted compatibility checks

After the two-roof public contract passed (one test, 3.854s), the existing house-task
and grouped-authoring public contracts both passed without skips (two tests, 6.012s).
The fixed-scenario and AC-storage comparison/delivery contracts also passed without
skips (two tests, 18.404s), using the previously completed native runs; they added no
GUI session or annual simulation. Necessary Python compilation and diff checks passed.
No full discovery, old smoke, hash tests, no-hash probe or performance benchmark ran.

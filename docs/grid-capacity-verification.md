# Ordinary inverter-group capacity editing

The existing `pvsyst_update_variant_parameters` tool accepts
`subarray_updates={"NInverter": 12}` or `{"NStringCh": 80}`; both can be supplied
together. No new tool is introduced.

`NInverter` alone scales ordinary inverter groups, preserving strings per
inverter. `NStringCh` alone scales parallel StringNode multiplicities while
preserving inverter count. When both are supplied, each target is respected:
leaf multiplicity scales by `(new_strings * old_inverters) /
(old_strings * new_inverters)`. All resulting group and leaf counts must be
integers. Relative group and string allocation are preserved. Inverter/string
`NElements`, `NInverter`, `NStringCh`, and `NStrOrient1` are synchronized.
Preview and the existing variant backup/restore remain available.

## Supported boundary

Requires complete consistent grid counts, a fixed-plane target orientation,
all target strings in one orientation, `NInvMPPT=1` without the independent
`$0400` subarray flag, no explicit MPPT branches,
no shared inverter power, and no shading geometry or tables. The supported
empty shading flag spellings include `$00`, `$00020000` (official commercial
sample) and `$00080000` (official residential; branch-preservation proof below). Omitted inverter or string `NElements` is accepted only when its
default quantity of one stays unchanged; changed quantities require explicit
existing fields.

This is proportional allocation in existing ordinary groups, not independent
MPPT allocation or electrical sizing. Component models, cables, losses, and sizing
hints such as `AreaSizing` and `PNomSizing` are preserved, not refitted. It does
not establish GUI round-trip equivalence or arbitrary topology support.

## Behavioral checks

The two existing offline cases cover inverter-only, strings-only and combined
edits of unequal groups, verifying actual per-group circuit counts and string
declarations. Fractional group or leaf allocation, independent/explicit MPPT
and activity-bearing shading flags stop before writes. No additional test
methods or hash-only checks were added for parallel-string editing.

The opt-in `tests/smoke_live_capacity.py` exercises the formal MCP tool and two
PVsystCLI 8.1.6 simulations in a separate copied workspace. The official
8.0.0-format commercial VC0 uses two groups with 2 and 4 inverters, having
6 and 7 strings per inverter respectively. Targeting 12 inverters changes the
groups to 4 and 8 while preserving both per-inverter string allocations.

Both runs use identical Wanaluwawa SIT/MET overrides and January 1-2, 1990.
The source workspace is only read; copies and evidence are retained in a lab.

| Quantity | Before | After |
|---|---:|---:|
| Inverters | 6 | 12 |
| Strings | 40 | 80 |
| Modules | 1000 | 2000 |
| EArray, sum of 48 hourly kW samples | 4383.4912 | 8675.5305 |
| E_Grid, sum of 48 hourly kW samples | 4304.7685 | 8520.0291 |

Native CSV headers declare kW and hourly values. Observed output ratios are
1.9791372 and 1.9792073, not an assumed exact factor of two. These runs show
that the native simulator consumes the changed capacity; they do not certify
full-year accuracy or the electrical design. The reason for the departure
from exact doubling was not isolated.

Retained local evidence outside this repository:
`component-edit-verification/mcp-capacity-14b11c07f462/evidence.json`, copied
inputs, and `capacity_before.csv` / `capacity_after.csv`.

## Parallel strings at fixed inverter count

The same driver with `--mode strings` sets `NStringCh=80` while keeping the
6 inverters (groups 2 and 4). Per-inverter strings change from 6/7 to 12/14;
modules change from 1000 to 2000. The saved diff changes only the two StringNode
multiplicities and `NStringCh`/`NStrOrient1`, not inverter/cable fields. Both
native runs complete with 48 finite hourly samples under the same SIT/MET.

| Output, sum of 48 hourly kW samples | Before | After |
|---|---:|---:|
| EArray | 4383.4912 | 6357.6238 |
| E_Grid | 4304.7685 | 6241.4582 |
| IL_Pmax | 12.951 | 2330.382 |

Native headers declare all three columns in kW. E_Grid rises by 44.9894%,
while the exported inverter power-limit loss increases. This demonstrates
consumption of parallel-string changes at fixed inverter count, not a general
electrical sizing approval. Other losses and design hints are not refitted.

Retained evidence:
`component-edit-verification/mcp-capacity-b40ebe16e391/evidence.json` with the
copied inputs and before/after CSVs. Offline checks remain at 179 after the
previous test cleanup.

## Grouped subarray copy and removal

`pvsyst_clone_subarray` / `pvsyst_remove_subarray` now handle all exclusively
owned inverter groups of the selected subarray, instead of requiring exactly
one branch. Their required arguments and tool categories are unchanged; optional
orientation lifecycle flags are described below. Shared power
anywhere in the variant, mixed/unspecified ownership, external target-ID
references and active shading remain outside the edit boundary. Multiple
groups require complete consistent grid counts; the existing isolated
single-branch path remains supported. Source MPPT layout is preserved, not
redesigned or interpreted as independent-MPPT mode.

The shared circuit parser records node boundaries and direct field positions.
Copies remap only owning IDs/names, not nested cable metadata. The two existing
quantity cases now include unequal-group copy/removal and metadata preservation;
the existing variant lifecycle case also checks a real external file edit
during backup. No new test methods, framework or CLI verification script were
added. Preparation-time file changes stop the operation; this is optimistic
checking, not cross-process locking or a crash-atomic transaction.

The same driver with `--mode subarrays` exercises formal MCP cloning, native
before/after runs and MCP removal in the copied commercial workspace. Each
subarray has two inverter groups (2 and 4), 6 inverters, 40 strings and 1000
modules, sharing fixed orientation 1. The copy produces two subarrays and four
groups: 12 inverters, 80 strings and 2000 modules. Both native outputs contain
48 finite hourly rows under identical SIT/MET overrides.

| Output, sum of 48 hourly kW samples | Before | After |
|---|---:|---:|
| EArray | 4383.4912 | 8766.9734 |
| E_Grid | 4304.7685 | 8609.5020 |
| IL_Pmax | 12.951 | 25.903 |

Observed EArray/E_Grid ratios are 1.99999795 / 1.99999187, approximately double,
not exact equality. Removal deletes both new groups and recovers the baseline
variant bytes exactly, including BOM/newlines. This proves restoration of the
simulation input; no third native simulation was performed after removal.

Evidence is retained outside the repository at
`component-edit-verification/mcp-capacity-02d7afb45729/evidence.json`, with
before/after CSVs and variant snapshots in `Projects/.mcp-variant-backups`.
The current copied VC0 is the restored baseline, not the modified input.

## Independent fixed-plane lifecycle

The existing subarray tools gain optional strict-boolean flags:
`clone_orientation=true` appends a copy of the source fixed plane, remaps its
`NoOrient`, links only the new subarray's `NoOrientation` and increments the
`Orientations, list of=N TOrientGroup` count. `remove_orientation=true` prunes
an unused fixed plane with its owning subarray and decrements the count. The
original native proof below covered final-plane deletion; bounded non-final
removal with synchronized renumbering is described in its own section below.
Defaults preserve shared-plane behavior. No new tool was introduced.

Creation requires fully checked consistent grid counts, all source strings in
one orientation, a flat `Flags=$00` fixed-plane source with no enabled averaging,
and sequential ordered IDs. Removal requires another plane to remain, a
fixed-plane ID in the sequential list and no other `NoOrientation` reference.
Non-final deletion additionally requires the stricter ordinary-grid checks below.
Shared power and active geometry/tables remain excluded. Sparse-ID deletion,
reordering, tracking/dome authoring and mixed-orientation allocation are not exposed.
Bounded reassociation to an existing ordinary fixed plane is described below.
Counts and references in supported orientation lists are now checked; dome or
unknown list counts are reported as unchecked, not as verified.

The existing quantity behavior case verifies independent copy, selected-plane
editing, append/prune count synchronization, shared-plane protection, exact
removal and rejection of a declared-count mismatch. Test methods remain 179.
Angle editing also accepts the official empty `$00020000` shading section while
retaining geometry/table guards. Comments and other derived hints are preserved;
angle edits do not refit the descriptive orientation Comment or certify GUI
round-trip equivalence.

`--mode orientations` uses the commercial sample, two identical unshared
subarrays (12 inverters, 80 strings, 2000 modules), Wanaluwawa overrides and
January 1-2, 1990. Each of three native runs contains 48 finite hourly rows.
Only one tilt field changes at each step; azimuth remains zero.

| Plane 1 / plane 2 tilt | EArray sum | E_Grid sum | IL_Pmax sum |
|---|---:|---:|---:|
| 15 / 15 degrees | 8766.9734 | 8609.5020 | 25.9030 |
| 15 / 60 degrees | 8771.6318 | 8614.6353 | 15.6990 |
| 60 / 60 degrees | 8776.2572 | 8619.7626 | 5.4961 |

All three columns are hourly kW samples. Mixed vs baseline has maximum hourly
EArray/E_Grid differences of 44.70 / 44.03 kW, although E_Grid's accumulated
change is only +0.059624%. Cancellation of hourly differences means a 5% total
energy-change predicate was inappropriate for this sample; the initial run
completed all three simulations but failed that predicate. Its evidence is
retained at `mcp-capacity-025e87f982c6`; no DLL investigation was needed.

The final driver checks a measurable hourly response and the mixed-control
relation `2*mixed - baseline - both_changed`, with relative residuals of
0.0003764% / 0.00006969% for EArray/E_Grid (normalized by baseline). This
supports consumption of the two plane references in this unshared sample,
not exact linearity for every design. The cause of nearly equal totals was
not isolated. A variant-backup restore undoes the control edit; removing the
new subarray and final plane then recovers the original variant bytes exactly.
No native simulation after final removal was performed.

Successful retained evidence:
`component-edit-verification/mcp-capacity-a44a65d19c83/evidence.json`, CSVs,
parameter snapshots and variant backups. Source workspace files stayed read-only.

## Fixed-plane reassociation

`pvsyst_update_variant_parameters` now accepts a positive integer
`subarray_updates={"NoOrientation": 2}` for a selected subarray. It reuses the
existing preview, backup, optimistic conflict checks and restore. The response
includes `orientation_assignment` with subarray ID and before/after plane IDs.
Capacity quantities may be supplied in the same transaction. Reassignment alone
changes only the owning subarray reference; plane records/list counts, component
references, circuit multiplicities and design hints remain unchanged. Repeating
the current assignment preserves bytes and creates no backup.

Requires complete consistent ordinary grid counts, exclusively owned branches,
`NInvMPPT=1`, no explicit MPPT or circuit-level orientation references, all
strings in one orientation, no shading geometry/tables and an empty supported
shading section. Source and target must be flat `Flags=$00` non-averaged fixed
planes in a sequential counted `TOrientGroup` list. Plane checks are shared with
independent-plane cloning. Unknown IDs, fractional IDs, averaging, shared power,
mixed-orientation strings and unsupported circuit layouts stop before writes.
This is not mixed-orientation MPPT redistribution or arbitrary renumbering.

Every defined plane must retain at least one assigned subarray. The initial
native fixture had only two subarrays; moving the sole user of plane 2 to plane
1 left plane 2 unused. CLI 8.1.6 stopped during variant checking with:

```text
You have defined an orientation #2 with Tilt = 60 degrees and azimuth = 0 degrees, but no PV array has been attributed to it !
```

The message above normalizes the degree glyph from captured CLI output. Failed
inputs and the completed baseline CSV remain at `mcp-capacity-44279e6ea44c`.
The shared counted-list check now diagnoses unused planes, so the same saved
input reports `valid=false`, `Orientation has no assigned subarray: [2]`.
Assignment that would orphan a plane also stops before writes. No plane is
silently deleted, renumbered or redirected. This was a missing native validation
rule, not a DLL-analysis obstacle.

The corrected `--mode reassociation` driver creates three identical independent
subarrays with six inverter groups: 18 inverters, 120 strings and 3000 modules.
Plane IDs remain 1 and 2. Under the same Wanaluwawa runtime overrides and January
1-2, 1990, each CLI run has 48 finite hourly samples:

| Case | Plane tilts | References for subarrays 1/2/3 | EArray sum | E_Grid sum | IL_Pmax sum |
|---|---|---|---:|---:|---:|
| Before | 15 / 60 degrees | 1 / 2 / 1 | 13155.1200 | 12919.3838 | 28.6510 |
| After assignment | 15 / 60 degrees | 1 / 2 / 2 | 13159.7450 | 12924.5711 | 18.4480 |
| Control after assignment restore | 60 / 60 degrees | 1 / 2 / 1 | 13164.3268 | 12929.6714 | 8.2441 |

All exported metrics are hourly kW samples. The before/after diff changes only
subarray 3's `NoOrientation=1` to `2`; maximum hourly EArray/E_Grid differences
are 44.70 / 44.03 kW. E_Grid's sample sum changes by +0.0401513%, so daily totals
alone are not a reliable consumption predicate. For this independent identical
sample, `abs(2*after-before-control)/before` is 0.00032839% / 0.00067341% for
EArray/E_Grid. This supports native consumption of the changed association and
consistency with the control, not general linearity or physical-design approval.

The driver restores the assignment before making the control tilt edit, then
restores the control, removes the third shared-plane subarray and removes the
second subarray/final independent plane. Original variant bytes are recovered
exactly. No native simulation after final removal is claimed. Evidence is
retained at `component-edit-verification/mcp-capacity-fc719c292e7a/evidence.json`,
with three CSVs, diffs, parameter snapshots and backups. The source vendor
workspace stays read-only. Existing quantity checks cover combined preview,
exact reference-only write, no-op, orphan protection and restore; their existing
negative case covers the additional scope guards. Offline methods remain 179.

## Non-final fixed-plane removal

`pvsyst_remove_subarray(..., remove_orientation=true)` now removes an exclusively
owned first or middle fixed plane as well as the final plane. For a non-final
plane, every higher `NoOrient` ID shifts down by one and every owning subarray's
`NoOrientation` follows the same mapping. The list count decreases; surviving
plane properties, subarray IDs, component references and circuit quantities do
not change. `orientation_id_map` reports changed surviving IDs as string keys,
for example `{"3": 2}` or `{"2": 1, "3": 2}`. It is empty for default/final
removal. Sequential input IDs and at least one surviving assigned plane are
required; the removed plane must have only its removed subarray as a user.

Non-final removal additionally checks all plane records with the existing
ordinary fixed-plane helper: flat, `Flags=$00`, no enabled averaging. All
subarrays must be ordinary `NInvMPPT=1` with all strings in one orientation;
complete circuit inventory must contain no explicit MPPT. Recognized orientation
fields, including `NoOrient*` and `OrientationId`, must belong to the counted
plane records or direct subarray roots. External references stop before writes.
Existing shared-power, target-branch ownership and shading guards remain active.
The candidate's circuit counts and counted plane references are checked again
before backup/replacement. The complete inventory is computed once; remaining roots are parsed locally
instead of repeating full circuit/ownership scans for every array.

The existing grid behavior case checks first/middle mappings, distinct retained
tilts, exact backup restore, external-reference protection and subsequent final
prune to the original input. No new test method or tool was added. The full 179
methods and formal MCP component/project/variant lifecycle checks pass.

Native checks reuse `tests/smoke_live_capacity.py` and the same controlled
Wanaluwawa SIT/MET overrides, January 1-2, 1990:

```powershell
$env:PVSYST_CLI='C:\Program Files\PVsyst8.1.6\PVsystCLI.exe'
$env:PVSYST_WORKSPACE='C:\Apps\PVsyst8.1_Data'
$env:PVSYST_EDITOR_LAB='C:\Harness\workspace\component-edit-verification'
python tests/smoke_live_capacity.py --mode pruning
python tests/smoke_live_capacity.py --mode pruning --prune-plane 1
```

Each command consumes two CLI executions. The baseline is a matching physical
control, not the three-array pre-deletion state; preparation makes the additional
plane/subarray without running it. Each before/after case has 12 inverters,
80 strings, 2000 modules and 48 finite hourly samples:

| Deleted plane | Pre-deletion tilts | Control and post-delete tilts | Post-delete subarray IDs | ID map | EArray sum | E_Grid sum | IL_Pmax sum |
|---|---|---|---|---|---:|---:|---:|
| Middle 2 | 15 / 60 / 35 | 15 / 35 | 1 / 3 | 3 -> 2 | 9008.8401 | 8846.9190 | 45.6926 |
| First 1 | 15 / 35 / 60 | 35 / 60 | 2 / 3 | 2 -> 1, 3 -> 2 | 9013.4956 | 8852.0863 | 35.4886 |

Sums are hourly kW samples; all three complete hourly sequences, not only sums,
are identical to their respective controls. Retained evidence:
`component-edit-verification/mcp-capacity-0adce18d2b17/evidence.json` (middle) and
`component-edit-verification/mcp-capacity-0200c9f2499e/evidence.json` (first).
These contain preparation parameters, changed IDs, native CSVs and backup names.

Both drivers restore the matching control snapshot, then remove its extra final
subarray/plane. The first-plane case also restores the original single-array
snapshot to undo its control tilt. Both recover the exact original variant
bytes; no native simulation after final cleanup is claimed. Vendor workspace
files remain read-only. The proof covers ordinary fixed-plane reindexing, not
tracking/dome/shading synchronization, arbitrary reordering, mixed-orientation
allocation, electrical sizing or GUI round-trip equivalence. No binary/DLL
analysis was needed.

## Preserving explicit MPPT branches

The existing default subarray copy/remove path can preserve complete exclusively
owned branches with explicit MPPT nodes; it does not switch simulation modes or
redistribute MPPT inputs. The official residential VC0 has an inverter node with
one MPPT child of multiplicity 2 and one string per input, while its subarray
still declares `NInvMPPT=1`. This is concrete evidence that serialized MPPT-node
multiplicity alone is not the independent-MPPT switch. The saved mode, component
references and per-input power fields remain unchanged during branch copying.

Its empty `pvShading` section uses `Flags=$00080000`, which the old editor treated
as unrecognized. This exact spelling is now accepted alongside `$00` and
`$00020000` through one shared constant in the angle/capacity/copy/remove paths.
Geometry/tables, nested shading objects, shared power and ownership checks remain
mandatory. Capacity resizing and independent-plane cloning still exclude explicit
MPPT layouts; non-final plane renumbering still excludes them too.

The existing multiplicity case now copies and removes such a branch under the
new empty-shading flag, verifies all counts and preserved `NInvMPPT=1`, and
recovers original bytes. The offline suite remains 179 methods; no new method,
tool, dependency or framework was added.

Native reproduction reuses the same opt-in driver:

```powershell
$env:PVSYST_CLI='C:\Program Files\PVsyst8.1.6\PVsystCLI.exe'
$env:PVSYST_WORKSPACE='C:\Apps\PVsyst8.1_Data'
$env:PVSYST_EDITOR_LAB='C:\Harness\workspace\component-edit-verification'
python tests/smoke_live_capacity.py --mode mppt_branches
```

The first attempt stopped before simulation because the configured workspace did
not contain `_DEMO_Residential.PRJ`; its evidence is retained at
`component-edit-verification/mcp-capacity-57015be51d2d/evidence.json`. The corrected
driver obtains complete residential PRJ/VC0 text from the CLI installation's
`DataRO/PVsyst8.1_Data/Projects` and creates `MCP_MPPT` through the formal project
creation tool in the isolated workspace. Source vendor and installed inputs are
read-only; no dependency files are invented or rewritten.

Formal copy retains shared fixed-plane orientation 1 and copies one complete
exclusive inverter/MPPT branch. Before/after structure checks pass and each
subarray retains `NInvMPPT=1`. CLI 8.1.6 runs both designs with identical
Wanaluwawa SIT/MET overrides, January 1-2, 1990; each export has 48 finite rows:

| Case | Inverters | Strings | Serialized MPPT inputs | Modules | EArray sum | E_Grid sum | IL_Pmax sum |
|---|---:|---:|---:|---:|---:|---:|---:|
| Before | 1 | 2 | 2 | 30 | 92.3300 | 90.6564 | 0 |
| After complete branch copy | 2 | 4 | 4 | 60 | 184.6602 | 181.3124 | 0 |

Sums are hourly kW samples. EArray/E_Grid ratios are 2.0000021661 / 1.9999955877;
these are near-double outputs, not a claim of exact general linearity. The zero
clipping in these cases does not distinguish independent inputs from shared
nominal power. Formal removal recovers the exact imported VC0 bytes. No native
simulation after removal or independent-mode change was performed. Evidence:
`component-edit-verification/mcp-capacity-a9cee6fcaaa6/evidence.json`, including
created project hashes, copied branch ID, both CSVs, counts and restore outcome.

## Independent MPPT remains a separate task

[Official power-sharing documentation](https://www.pvsyst.com/help/project-design/grid-connected-system-definition/multi-mppt-inverters/power-sharing.html)
separates whole-inverter PNom sharing, independent MPPT inputs and cross-subarray
power sharing. It also describes physical-input and configuration consistency
checks. Those public definitions do not specify the serialized mode flags.

The installed tracking VC2/VC3 use the same circuit MPPT multiplicity 4 but
respectively have `NInvMPPT=1/4`, subarray `Flags=$019E0002/$019E0402`, and
`PNomAC_InMPPT=900.00/225.00`. This is a candidate set of linked settings, not a
controlled proof of the `$0400` bit: the files also differ in shading factors and
other content. Tracking VC5 additionally shares an inverter across two subarrays
with `PowerSharingInverterId=0` and split nominal-power values. No flag or sharing
configuration is exposed based solely on these examples.

The following native black-box controls now establish a narrow mode-bit behavior;
GUI round-trip and broader allocation rules still need their own evidence. The current
CLI help offers simulation, site/weather creation and batch/advanced-parameter
inputs, not a dedicated MPPT design command. The installed public batch example
`CLI/Ressources/Examples/Sources/BatchMode/Demo_PVsystCLI_BatchParams.CSV` labels
its quantity column `Nb inverter` / `or MPPT`; it contains no separate independent
mode selector. That one example does not establish an exhaustive list of native
parameters or prove that changing this column switches modes. No binary/DLL analysis is required
by the observed copy/remove behavior, and none was performed here.

## Controlled MPPT mode-bit probe

This is complete-text input authoring through `pvsyst_create_project`, not a new
mode-editing API. The same installed residential VC0 is used in all four cases.
Before varying the fields, its two strings are consolidated onto one serialized
MPPT input: MPPT `NElements=1`, String `NElements=2`. The PV array remains 30
modules / 9 kWp, one inverter branch, `NInverter=1`, `NInvMPPT=1`, the same
components/orientation/no-shading configuration, and `PowerSharingInverterId=-1`.
Only the root subarray `Flags` and saved `PNomAC_InMPPT` differ between cases;
root-owned field helpers avoid changing nested IAM flags. Each native CLI 8.1.6
run exports 48 finite hourly rows and an English parameter PDF under identical
Wanaluwawa SIT/MET overrides, January 1-2, 1990.

| Case | Subarray Flags | Saved PNomAC_InMPPT | EArray sum | E_Grid sum | IL_Pmax sum |
|---|---|---:|---:|---:|---:|
| whole | $001E0002 | 7.50 | 92.3300 | 90.6564 | 0 |
| nominal_only | $001E0002 | 3.75 | 92.3300 | 90.6564 | 0 |
| flag_only | $001E0402 | 7.50 | 69.1857 | 67.9120 | 23.0968 |
| independent | $001E0402 | 3.75 | 69.1857 | 67.9120 | 23.0968 |

Sums are hourly kW samples. Full parsed hourly records are identical for
whole/nominal_only and flag_only/independent, not just aggregate sums. Whole
reports say `Power sharing within this inverter`, 7.5 kW total, ratio 1.20.
Flagged reports say `1 * MPPT 50%  0.5`, approximately 3.8 kW total (rounded from
3.75), ratio 2.40, and an unused-input fraction. E_Grid falls 25.0885762% and
clipping becomes positive. Flagged reports also add `Strings Mismatch loss`
0.05%; the entire output difference is not attributable solely to clipping.

For this Version=8.1.0 subarray / generic 7.5 kW two-input model consumed by CLI
8.1.6, bit `$0400` therefore selects the partial independent-input behavior.
The saved nominal-power tag alone is ignored in these unshared configurations;
this does not establish its behavior under cross-subarray power sharing.
This is stronger than the original uncontrolled tracking-file difference, but
not GUI round-trip proof, all-version certification, or a complete independent
MPPT allocation algorithm. Model-derived effective power should be read from
native reports, not inferred from a stored nominal tag alone.

Evidence: `component-edit-verification/mcp-capacity-f6aca8c60c84/evidence.json`,
including the four input variants, CSV/PDF paths and extracted report text.
PDF extraction uses already-installed `pypdf` only in the optional probe mode;
no runtime dependency or MCP tool was added.

### Native counterexample without MPPT nodes

A fifth run removes only the MPPT wrapper from the independent input, retaining
two strings and all array/mode fields. Its serialized circuit has zero explicit
MPPT inputs, yet the PDF still reports `MPPT 50%`, and all 48 parsed hourly
records match the independent case above. Thus neither `NInvMPPT=1` nor absence
of MPPT nodes establishes ordinary operation.

The shared ordinary-mode guard now checks the known `$0400` flag in addition to
`NInvMPPT=1`, validates an existing flag spelling, and is used by capacity
resizing, orientation reassignment, independent-plane copying and non-final
plane renumbering. Independent-plane copying additionally stops on explicit
MPPT nodes, matching its documented boundary. Default complete-branch copying
still preserves the saved MPPT mode. Grid diagnostics additionally return raw
root Version/Flags/PNomAC_InMPPT/PowerSharingInverterId; these are declarations,
not derived effective power or electrical sizing approval.

In the fifth native fixture the formal ordinary `NStringCh` edit stops before
writes specifically at the independent-flag check, while the unchanged input
still simulates successfully. Evidence:
`component-edit-verification/mcp-capacity-d9f6b12bf2ca/evidence.json`.
Representative existing tests cover this single-input flag case and preserve
both ordinary/flagged MPPT branches on default copying/removal. The offline
suite remains 179 methods and the formal MCP lifecycle smoke passes. Broader
MPPT count/ownership rules, sharing groups and GUI round-trips remain pending;
no binary/DLL analysis was performed.

## Independent MPPT quantity controls

Four additional complete-text variants use the same root `$001E0402` flag,
Version=8.1.0 subarray, component models, fixed plane and 48-hour Wanaluwawa
weather. The root saved `PNomAC_InMPPT=3.75` and global DC resistance remain
unchanged. Each serialized MPPT leaf has two parallel strings.

| Case | NInverter | NInvMPPT | Circuit MPPTs | PV strings | Reported MPPTs | Reported total AC kW | E_Grid sum | IL_Pmax sum |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| partial | 1 | 1 | 1 | 2 | 1 | 3.8 | 67.9120 | 23.0968 |
| one_group_two_inputs | 1 | 2 | 2 | 4 | 2 | 7.5 | 135.5828 | 44.2165 |
| two_groups_one_input | 2 | 1 | 2 | 4 | 1 | 3.8 | 80.6994 | 100.0178 |
| three_inputs | 1 | 3 | 3 | 6 | 3 | 11.3 | 203.0129 | 63.3585 |

The middle two inputs have identical PV module counts, weather, orientation,
components, resistance and mode; they differ in the quantity encoding and
serialized grouping. They do not produce equivalent outputs: the native
parameter report follows `NInvMPPT`, not `NInverter * NInvMPPT`. This disproves
using the ordinary inverter quantity as a multiplier for independent inputs.
`NInverter` and InverterNode multiplicities still describe serialized diagram
counts; those counts alone do not establish the active model quantity.

The three-input case is accepted by CLI 8.1.6. Its PDF says
`3 * MPPT 50%  1.5`, total 11.3 kW (rounded from 11.25), two inverter units and
0.5 unused equivalent unit. Three inputs on a two-input model therefore do not
by themselves prove invalid overbooking. The original experiment named this
case `overbooked`; the driver now uses the neutral `three_inputs` label. The
historical evidence and its labels are kept unchanged.

Doubling strings with fixed absolute DC resistance changes the native wiring
loss fraction from 1.50% to 3.00%, and six strings yield 4.50%. The one-input /
four-string case also reports 0.10% string mismatch versus 0.05% in the other
cases. Energy is not asserted to scale exactly with input count, nor are those
loss parameters refitted by the driver.

Evidence: `component-edit-verification/mcp-capacity-6daad7b3b7f2/evidence.json`,
with four CSV/PDF exports and native report text. The original pre-fix structure
inspection accepted all four serialized inverter/string totals. Fresh inspection
with the corrected code accepts VC0/VC1/VC3 and diagnoses VC2: independent
`NInvMPPT=1` differs from two explicit circuit MPPT inputs. Native simulation
still accepts VC2; structure consistency and CLI acceptance are separate checks.

For the verified Version=8.1.0 root with `$0400`, diagnostics now expose
`independent_mppt_inputs_declared` and `explicit_mppt_inputs_match_declared`.
Complete explicitly owned MPPT layouts compare against `NInvMPPT` directly,
not the ordinary multiplier. Other schemas/modes and implicit layouts leave
the match unset; shared/unknown ownership remains incomplete. No converter
hardware limits or GUI round-trips are certified by this check. The existing
representative test covers matching, mismatching, whole-mode and implicit
layouts without adding another test method. The 179-method suite and MCP
lifecycle smoke pass. Independent-mode editing and sharing allocation remain
separate work; no binary/DLL analysis was performed.

## Cross-subarray sharing controls

The installed tracking VC5's text identifies a shared inverter parent with no
owning SubArrayId, two MPPT children with distinct owning arrays, common
`PowerSharingInverterId=0`, root `$0400/$0100` bits and saved power values.
Rather than importing its tracking/3D geometry, this probe reuses the complete
unshaded residential text and the same explicit topology pattern. Its two
arrays have 30/15 modules, 9/4.5 kWp, the same inverter, one MPPT each and two/one
parallel strings. The shared parent names both arrays; all leaf IDs/names are
scoped and remapped. All four cases have the same topology and electrical
inputs except the following settings. Each returns 48 finite hourly rows and
an English parameter PDF under the existing Wanaluwawa overrides.

| Case | Subarray Flags | Sharing IDs | Saved per-input kW | Native per-array kW | E_Grid sum | IL_Pmax sum |
|---|---|---|---|---|---:|---:|
| independent | $001E0402 | -1 / -1 | 5.00 / 2.50 | 3.8 / 3.8 | 113.5119 | 23.0968 |
| id_only | $001E0402 | 0 / 0 | 5.00 / 2.50 | 5.0 / 2.5 | 122.4421 | 13.9872 |
| shared | $001E0502 | 0 / 0 | 5.00 / 2.50 | 5.0 / 2.5 | 122.4421 | 13.9872 |
| swapped | $001E0502 | 0 / 0 | 2.50 / 5.00 | 5.0 / 2.5 | 122.4421 | 13.9872 |

All three shared cases have identical full parsed hourly records, not just
sums. Their reports say `Power sharing defined`, `MPPT 67%` / `MPPT 33%`, and
DC:AC ratio 1.80 for both arrays. The independent report says `No power sharing`,
50% / 50%, and array ratios 2.40 / 1.20. The total AC budget remains 7.5 kW.
Sharing increases E_Grid by 7.8671928% and reduces the observed clipping sum.

In this CLI 8.1.6 / Version=8.1.0 / generic two-input fixture, the common group
ID activates shared power; adding `$0100` is not required to change the native
result. Swapping the saved nominal values does not swap the native allocation:
it is recomputed to the arrays' 2:1 rated-PV ratio. Neither arbitrary group-ID
semantics, all-model automatic allocation, manual sharing controls nor GUI
round-trip behavior follows from this one fixture. Those rules need separate
controls before an editing interface is exposed.

At the time of this native experiment the structure validator retained
`valid=null` for the mixed-owner parent and returned no aggregate totals. Those
original records are preserved. The bounded inspection below now recognizes
this explicit format; it does not promote arbitrary shared layouts to complete
validation. Existing structural editors still stop on shared ownership/power.
The new independent-input declaration conflict guard is shared by structural
copy/remove paths, including the isolated single-branch path. A fresh formal
MCP call on quantity-probe VC2 stops copying without modifying the actual native
input. An initial replay omitted PVSYST_CLI; setting both CLI/workspace resolved
that test setup error before the successful guard verification, without a
native simulation or binary investigation.

Evidence: `component-edit-verification/mcp-capacity-c9f69db16f7b/evidence.json`,
including input variants, four CSV/PDF exports, pre-existing incomplete
structure diagnostics and extracted report text. No new public tool, runtime
dependency or test method was introduced. The full 179-method suite and formal
MCP lifecycle smoke pass after the common structural guard change. Shared
allocation validation/editing remains pending; no binary/DLL analysis occurred.

## Bounded shared-parent structure inspection

The validator now recognizes the explicit mixed-owner layout in the sharing
probe and installed tracking VC5. Preconditions: unowned inverter parent
(SubArrayId absent/0/-1), at least two distinct members, direct MPPT children
whose entire string subtrees retain one owning ID/name, Version=8.1.0 members
with `$0400`, identical nonempty GInverter references, and either common
PowerSharingInverterId=0 or all -1. The parent name must be the child-order
member Comments joined by `; `. A second parent for group 0, other group IDs,
mixed IDs/models/versions or unsupported ownership remains explicitly
unchecked. This is a narrow structural format, not a claim about every native
power-sharing group.

Inverter groups with shared membership return `subarray_id=null`, ordered
`subarray_ids`, and `power_sharing_id`. Member rows expose
`shared_inverter_lines`; their `circuit.inverters` are referenced parent
multiplicities, not disjoint ownership. Aggregate inverter totals sum unique
circuit parents, never the overlapping member references. Strings/MPPTs retain
owning IDs and multiplicative counts and are compared to member declarations.
The existing independent-input total check remains active.

Fresh formal MCP inspection of all four previously simulated sharing inputs
returns `valid=true`, one serialized inverter, three strings and two MPPTs.
Both member rows reference that one parent and their input declarations match.
Each attempted structural copy still stops on shared ownership/power and
preserves the actual input bytes. This round runs no extra native simulation;
the previous CSV/PDF evidence remains unchanged. New read-only diagnostics and
editor-stop records are saved separately in
`component-edit-verification/mcp-capacity-c9f69db16f7b/shared-structure-check.json`.
The installed tracking VC5 text additionally passes this scoped grid inspection
with one inverter, 114 strings and four MPPTs; its tracking geometry has not
been newly simulated or certified here.

The existing representative shared-layout method now covers supported shared
and independent-port parents, referenced-versus-unique inverter counts,
different model/group/schema fallback and incorrect aggregate names. The full
suite remains 179 methods and the MCP lifecycle smoke passes. No public tool
or dependency is added. Group-ID generalization, allocation editing, hardware
input limits and GUI round-trip evidence remain pending. No binary/DLL analysis
was performed.

## Independent MPPT quantity edit

The existing parameter transaction now accepts `subarray_updates={"NInvMPPT": 2}`
(integer 1..10000) for the verified Version=8.1.0 independent `$0400` schema.
It requires explicit PowerSharingInverterId=-1, complete consistent exclusively
owned MPPT branches, all strings in one unshaded fixed plane, and direct MPPT
children with explicit edited NElements. It is separate from ordinary
NInverter/NStringCh capacity editing and is not combined with those fields or
NoOrientation. Missing/unknown mode or sharing settings, fractional group
allocation, mixed/shared ownership and unsupported geometry stop before writes.

Only MPPT group multiplicities scale; serialized inverter count and parallel
strings per input stay fixed. Root NInvMPPT, NStringCh and NStrOrient1 move
coherently. The parameter response's existing `capacity` section adds
mppt_inputs_before/after and mppt_groups. Component references, series count,
Flags, nominal tags, DC resistance and other loss parameters are unchanged.
The operation does not infer available converter ports, redistribute hardware,
switch simulation modes or refit electrical/loss parameters.

The existing quantity smoke now clones its one-input VC0 into VC4 through formal
MCP, previews/applies NInvMPPT=2, checks its entire bytes against the separately
authored VC1 control, and runs CLI on the edited VC4 instead of that control.
Four native executions are still used, not five. The edited case produces 48
finite rows: EArray=138.1258, E_Grid=135.5828, IL_Pmax=44.2165 kWh at hourly
cadence; its PDF reports two MPPTs and 7.5 kW. Baseline one-input E_Grid is
67.9120 kWh. Increasing inputs/strings with fixed resistance changes DC losses;
no exact energy multiplier is promised. After the native run, formal restore
recovers the original VC4 bytes exactly, while authored VC1 and CSV/PDF outputs
retain the changed-input evidence.

Evidence: `component-edit-verification/mcp-capacity-b995cee85dc5/evidence.json`,
including `mppt_quantity_edit` preview/apply/restore records, byte-equivalent
control and all four native reports. The representative existing method also
covers six-to-twelve proportional inputs, preview without writes, backup
restore, fractional counts, incompatible updates, unknown versions/modes and
missing/shared group settings. The suite remains 179 methods and MCP lifecycle
smoke passes. No new public tool, runtime dependency or probe mode was added.
Other input-allocation redesign and shared configuration editing remain pending;
no binary/DLL analysis occurred.

## Existing power-sharing toggle

The existing parameter transaction now accepts `power_sharing_enabled=true`
or `false`, selected by a member `subarray_id`. It applies to the entire known
shared parent, not that member alone. Requirements: bounded Version=8.1.0
independent `$0400` format, complete counts, identical inverter references,
all member strings in one common unshaded fixed plane, no additional member
parents/exclusive branches and no outside use of group 0. The strict boolean
is separate from other scalar/generator updates.

Enabling sets every member's PowerSharingInverterId to 0 and `$0100`; disabling
sets -1 and clears only that flag bit. Mode `$0400`, every other flag bit,
topology, counts, components, nominal tags, resistance and loss parameters are
preserved. Return `power_sharing` identifies all members, parent line and enabled
state before/after. Existing preview, backup, no-op and restore semantics apply.
Saved nominal values are not fitted: native automatic sharing remains active.
This does not create/merge a parent, configure arbitrary group IDs, redistribute
manual budgets, switch MPPT modes or certify hardware/GUI behavior.

The sharing smoke clones disabled VC0 into VC4 and enables it through formal
MCP; all edited bytes match independently authored shared VC2. It clones VC2
into VC5 and disables it; all bytes match independent VC0. CLI then actually
runs edited VC4/VC5 in place of their respective controls, with the existing
id-only and swapped-tag cases completing the same four native executions.
Each export has 48 finite hourly rows. Enabled EArray/E_Grid/IL_Pmax are
124.7390/122.4421/13.9872 kWh; disabled values are
115.6294/113.5119/23.0968. The enabled PDF says Power sharing defined and
67%/33% input allocation; disabled says No power sharing and 50%/50%.
The three enabled-case full hourly records still match exactly. Both edited
variants are formally restored to their pre-edit bytes after simulation.

Evidence: `component-edit-verification/mcp-capacity-32554e288fc0/evidence.json`,
with `power_sharing_edits` apply/restore and byte-equivalent controls plus
native CSV/PDF exports. The existing shared-layout method covers whole-group
sync, preview without writes, no-op without backup, disabling, restore, strict
boolean input, combined-update stops, outside-group collision and member
orientation guards. The suite remains 179 methods and MCP lifecycle smoke
passes. No new public tool, dependency or probe mode was introduced.
Broader shared topology/configuration remains pending; no binary/DLL analysis
was performed.

## Explicit MPPT mode switch

`independent_mppt_enabled=true/false` is a separate existing parameter transaction
for Version=8.1.0, explicit unshared PowerSharingInverterId=-1, valid Flags,
complete consistent exclusive direct-MPPT layouts and all strings in one
unshaded fixed plane. It changes only `$0400` and root NInvMPPT. Enabling sets
NInvMPPT to the circuit's expanded explicit input total; disabling sets ordinary
1. Other flags, all node multiplicities, strings/inverters, component models,
nominal tags, resistance and losses are preserved. Return `mppt_mode` describes
states/declarations before/after. This intentionally changes model power
interpretation, not physical-device allocation or derived-field fitting.
Mixed direct strings/MPPTs, implicit layouts, shared ownership/power, unknown
schema and combined scalar/sharing updates stop before writes.

The existing mode smoke clones whole-mode VC0 into VC4, previews/applies enable
and checks every byte against authored flag-only VC2. It clones VC2 into VC5,
applies disable and checks every byte against authored whole VC0. CLI runs the
edited VC4/VC5 instead of those controls, retaining the same four executions.
Whole-mode EArray/E_Grid/IL_Pmax are 92.3300/90.6564/0 kWh; independent values
are 69.1857/67.9120/23.0968 over the 48 finite hourly rows. Reports show whole
inverter sharing versus one 50% MPPT. Whole/nominal-only and flag-only/independent
full hourly records remain identical. Both edited variants are formally
restored to pre-edit bytes after native runs.

Evidence: `component-edit-verification/mcp-capacity-b4941ab25f70/evidence.json`,
with `mppt_mode_edits` apply/restore and byte-equivalent controls, CSV/PDF reports.
The existing representative method also switches a two-inverter/six-explicit-
input serialization, checks NInvMPPT=6 versus ordinary 1 without redrawing,
preview/no-op/recovery, strict boolean and incompatible updates, and stops a
complete but mixed direct-string/MPPT layout. The full suite remains 179 methods
and the MCP lifecycle smoke passes. No new tool, dependency or probe mode is
added. Arbitrary input redistribution, mixed orientation/shared topology and
GUI round-trip evidence remain pending; no binary/DLL analysis occurred.

## Independent parallel-string edit

NStringCh alone now proportionally scales explicit parallel-string leaf counts
in verified Version=8.1.0 independent `$0400` layouts. MPPT/inverter node
multiplicities and root NInvMPPT remain fixed; NStringCh/NStrOrient1 synchronize.
The input-count editor's unshared/exclusive/unshaded fixed-plane scope still
applies. Changed NElements must exist and allocation must remain integral.
NInvMPPT/NInverter combinations, shared/implicit layouts and unknown modes stop
before writes. This is proportional leaf scaling, not arbitrary unequal input
redistribution, voltage/current sizing or loss/resistance refitting.

The quantity smoke adds an authored one-input/four-string VC6. Formal MCP clones
the one-input/two-string VC0 into VC5, previews/applies NStringCh=4, checks all
bytes against VC6, and runs native CLI on edited VC5. There are now five native
quantity executions (mode/sharing probes still use four). Every export has 48
finite rows. Baseline EArray/E_Grid/IL_Pmax are 69.1857/67.9120/23.0968 kWh;
string-edited values are 82.2308/80.6994/100.0178. The saved resistance is not
resized and the added PV capacity is clipped by the same one-input AC budget.
No exact energy scaling is promised. Formal restore recovers original VC5 bytes.

Evidence: `component-edit-verification/mcp-capacity-4cf6987adfae/evidence.json`,
with `independent_string_edit` preview/apply/restore, byte-equivalent control,
and native CSV/PDF exports. The previous input-count edit and deliberate
quantity/layout inconsistency remain in the same run. The existing method also
covers a six-input leaf scaling case, unchanged inputs, preview, recovery and
fractional allocation. The old unknown-version independent-flag case now stops
at the verified-schema guard; implicit flat probe stops at explicit allocation.
The suite remains 179 methods and MCP lifecycle smoke passes. No extra public
tool, dependency or execution mode was introduced. No binary/DLL analysis
occurred; arbitrary redistribution/shared topology work remains pending.

## Same-subarray unequal-leaf control

Before exposing arbitrary redistribution, the optional allocation probe compares
three Version=8.1.0 independent-mode layouts: one MPPT node of multiplicity 2
with two parallel strings, two MPPT nodes with 2+2 strings, and two MPPT nodes
with 3+1 strings. All use one subarray, the same fixed plane/model, NInvMPPT=2,
NStringCh=NStrOrient1=4, and no power sharing. The entire root subarray text is
identical. A separate parsed-input check confirmed actual node multiplicities
`[(2,[2])]`, `[(1,[2]),(1,[2])]`, `[(1,[3]),(1,[1])]` respectively.

All three formal MCP structural checks pass with unique counts 1 inverter,
4 strings and 2 explicit inputs. CLI executes all three, yielding 48 finite
hourly rows per export. Every exported hourly record matches across all cases:
EArray=138.1258, E_Grid=135.5828, IL_Pmax=44.2165 kWh. Every parameter report
still says `2 * MPPT 50%`, 7.5 kW, no power sharing, and 0.05% string mismatch.
Splitting the diagram or redistributing its leaves did not change observed
simulation in this particular single-subarray fixture. This is not a conclusion
for every model, geometry, sharing group or GUI state.

Evidence: `component-edit-verification/mcp-capacity-e0518254bc75/evidence.json`,
with false `node_expansion_changes_hourly_results` and
`unequal_leaf_allocation_changes_hourly_results`, source VC files and CSV/PDFs.
The native probe asserts full parsed hourly equality, not only energy sums.
Reproduce using `python tests/smoke_live_capacity.py --mode mppt_allocation_probe`
(three license executions, existing pypdf installation required).

The [official multi-MPPT guide](https://www.pvsyst.com/help/project-design/grid-connected-system-definition/multi-mppt-inverters/index.html)
defines homogeneous subarrays and recommends separate subarrays for distinct
input configurations on one physical inverter. The native control agrees with
that modeling boundary; it does not prove a decoder or internal implementation.
No arbitrary unequal per-input physics editor is added solely from node edits.
A semantically valid separate-subarray construction/association path remains
work to do. The regular suite remains 179 methods; no dependency or public MCP
tool was added. No binary/DLL analysis occurred.

## Shared member string edit

NStringCh alone now edits a selected member of one existing bounded shared
parent, with Version=8.1.0 independent mode, common unshaded fixed plane,
complete owned inputs and group 0 or disabled -1. Each member references only
that parent. Active group 0 must not be referenced by arrays outside the
parent's member list. Every changed explicit string leaf scales proportionally
with integral counts; root NStringCh/NStrOrient1 synchronize. Peer roots/leaves,
parent/inverter/MPPT multiplicities, sharing flags/IDs, component models,
nominal tags and loss parameters remain unchanged. The response adds
capacity.shared_parent_line. Shared input/inverter count resizing remains closed.

The sharing smoke adds an independently authored more-strings VC6. Formal MCP
clones shared VC2 into VC7, previews/applies member 1 NStringCh=4, verifies all
bytes against VC6 and runs native CLI on edited VC7. The other member retains
one string; total strings move from 3 to 5, inputs stay 2, physical report
inverters stay 1. With the same saved nominal tags 5/2.5 kW, CLI now reports
80%/20% sharing (6/1.5 kW), rather than baseline 67%/33% (5/2.5 kW).
EArray/E_Grid/IL_Pmax move from 124.7390/122.4421/13.9872 to
148.5394/145.7943/80.1539 kWh across 48 finite hourly rows. Full hourly results
change; the previous ID-only/flagged/swapped cases still match each other.
Formal restore recovers original VC7 bytes exactly after native simulation.

Evidence: `component-edit-verification/mcp-capacity-aa2ed14890c8/evidence.json`,
with shared_member_string_edit preview/apply/restore, byte-equivalent control
and CSV/PDFs. The existing sharing probe now needs five native executions;
mode probe remains four. The representative method covers enabled/disabled
shared parents, unchanged peers, complete counts, preview/restore, fractional
allocation, closed inverter/input resizing, outside group references and
member orientation guards. The suite remains 179 methods; full MCP lifecycle
smoke passes. No new public tool, dependency or execution mode is added.

This changes modeled PV capacity within existing separate subarrays, not just
diagram leaf redistribution. Native automatic budgets are allowed to update;
manual effective-budget fields are not edited or fitted. Arbitrary parent
creation/merging, mixed orientations and GUI round-trip proof remain pending.
No binary/DLL analysis occurred.

## Shared-parent construction by clone

`pvsyst_clone_subarray(..., share_inverter=true)` now copies a source owned MPPT
subtree under its existing inverter and duplicates its subarray snapshot. This
constructs a bounded shared parent without cloning the inverter: parent owner
becomes 0, its canonical name joins source/new Comments, and new MPPT/string
owners/names follow the project-wide fresh ID. Both roots preserve independent
mode and disabled PowerSharingInverterId=-1. Enabling sharing remains a separate
parameter transaction. Requires Version=8.1.0 `$0400`, complete counts, exactly
one exclusive inverter/input (multiplicity 1), all strings in the same unshaded
fixed plane and no shared-power references. It is not combined with orientation
cloning, arbitrary parent merging or physical-port sizing. Return field
shared_parent_created identifies this path; backup/restore remains available.

The quantity smoke clones baseline VC0 into VC7 via MCP and constructs its new
member through the formal tool. A separately authored control is preserved
outside the project (so it does not reserve a member ID during allocation).
All constructed VC7 bytes match that control. Structure reports one unique
inverter, two inputs and four strings across two members. CLI runs constructed
VC7, with 48 finite rows and EArray/E_Grid/IL_Pmax
138.3725/135.8239/46.1936 kWh. Its report lists two subarrays, each
`1 * MPPT 50%` (0.5 equivalent inverter, 3.8 kW), total one inverter/7.5 kW,
and No power sharing. Both roots retain the original resistance; the report's
parallel aggregate changes from 378 to 189 mOhm. It is not a loss-refitting
operation or numerically equivalent to consolidating PV strings in one root.
Formal restore recovers pre-construction VC7 bytes after the native run.

Evidence: `component-edit-verification/mcp-capacity-30610f6ba2f5/evidence.json`,
with shared_parent_clone apply/restore, source control and native CSV/PDF.
The quantity probe now uses six native executions; sharing remains five.
The initial attempt `mcp-capacity-8274d0ea6cea` completed native simulation but
stopped at an incorrect single-subarray PDF assertion before final clone restore;
its evidence remains historical. The corrected clean-baseline run completes.
Representative checks cover unique counts/member ownership, disabled sharing,
exact restore, strict flags, orientation combination, wrong mode and quantities.
The suite remains 179 methods and full MCP lifecycle smoke passes; no new public
tool, dependency or probe mode was introduced.

Physical installed-device counts and hardware port availability still require
CLI/model review; the constructor does not infer them from diagram counts.
Shared-member removal, broader merge/split and GUI round-trip proof remain
pending. No binary/DLL analysis occurred.

## Shared-member removal

`pvsyst_remove_subarray(..., shared_member=true)` is the bounded inverse of
shared-parent construction. It requires a complete two-member parent with
explicit owner 0, disabled group -1, one inverter/input per member and all
strings in the common unshaded fixed plane. All power sharing must be disabled.
The selected snapshot and its owned MPPT subtree are deleted; the parent's
owner/name becomes exclusive to the other member. Its root fields, quantities,
mode/components/losses and plane remain unchanged. ID references outside the
owned snapshot/subtree stop before writes. Return remaining_member_id identifies
the survivor (null on ordinary removal). Default false preserves old behavior;
remove_orientation is separate and not combined with this path.

The existing quantity smoke constructs VC7 through MCP, runs it, then deletes
its newly added member 2 through the formal tool. The resulting whole VC bytes
match the original one-member baseline exactly, and structural inspection passes
with [1], one inverter, one explicit input and two strings. CLI runs the deleted
result and every parsed hourly record matches baseline, including EArray/E_Grid/
IL_Pmax=69.1857/67.9120/23.0968 kWh over 48 finite rows. Formal restore of the
delete backup returns the exact constructed control; subsequent clone-backup
restore returns the original baseline again. This adds one native execution,
so quantity smoke now uses seven; sharing remains five.

Evidence: `component-edit-verification/mcp-capacity-feebf1848c46/evidence.json`,
with shared_member_remove apply/inspection/run/restore and full hourly equality,
plus the prior constructed control and outputs. The existing method tests
removing either member, leaving IDs unrenumbered, exact construction/deletion
inverse, snapshot restore, strict boolean and orientation combination, and an
active-sharing pre-write stop. The suite remains 179 methods and full MCP
lifecycle smoke passes. No new public tool, dependency or probe mode is added.

This is not active-sharing budget redistribution, arbitrary parent pruning,
mode switching or physical-device refitting. General merge/split, mixed
orientations and GUI round-trip evidence remain pending. No binary/DLL analysis
occurred.

## Independent MPPT fixed-plane cloning

The existing clone_orientation option also supports a verified Version=8.1.0
independent `$0400` source with disabled sharing -1, complete exclusive direct
MPPT child layouts and consistent declared input counts. Ordinary sources still
require no explicit MPPT. Both retain all prior fixed-plane, empty-shading,
single-string-orientation and sequential-list guards. Copying preserves mode,
input/leaf multiplicities, models and loss fields, remapping only new ownership
and plane references. Shared/mixed-direct/implicit/unknown-mode layouts remain
closed. Final-plane removal recovers the original snapshot; non-final plane
reindexing is still ordinary/no-explicit-MPPT only.

The native smoke reuses the existing three-plane-angle comparison via
`--mode mppt_orientations` (three native executions, installed pypdf). It creates
an isolated Residential project, formally enables independent mode and prepares
source tilt 15, then clones all owned inputs with an independent plane. The
before/after/control cases have tilts 15/15, 15/60, 60/60, references [1,2],
one physical inverter equivalent per subarray, total two inverters, four
strings and four explicit inputs. Every PDF reports two independent arrays
with `2 * MPPT 50%` and No power sharing. Hourly rows are finite and complete.
E_Grid totals are 174.5883/173.1664/171.7451 kWh; maximum target-plane hourly
E_Grid delta is 1.8321 kW. Mixed-plane cumulative residual is 0.0003437%,
consistent with half of the equal-plane control sum. The control angle is
restored, then new member/last plane removed; final bytes equal the prepared
single-plane independent baseline exactly (not the untouched vendor source).

Evidence: `component-edit-verification/mcp-capacity-f927f86658f9/evidence.json`,
with mode/angle setup, clone, all three native CSV/PDFs, parameter ownership,
control restore and baseline_bytes_restored. Early attempts 84bde3ba8e69 and
9662986ce7d1 used stale commercial capacity/group-count assertions; the latter
completed the native comparison before stopping at cleanup. Corrected fresh
run completes native/control/removal recovery. The existing representative
method covers known mode/input counts, appended plane, new ownership and exact
last-plane deletion; existing unknown-mode/ordinary explicit-input cases stay
closed. Full suite remains 179 methods and formal MCP lifecycle smoke passes.
No new public tool or dependency is added; one optional native probe alias
reuses the existing orientation driver.

This establishes multiple separately modeled subarrays with exclusive inputs
and separate fixed planes, not one subarray's arbitrary mixed-plane input map,
shared cross-orientation power allocation, hardware pairing, loss refitting or
GUI round-trip certification. Those broader operations remain pending.
No binary/DLL analysis occurred.

## Independent MPPT fixed-plane reassociation

NoOrientation now supports the same verified exclusive independent mode as
fixed-plane cloning: Version=8.1.0 `$0400`, explicit disabled sharing -1,
complete declared input counts, direct owned MPPT children and all strings in
one plane. Ordinary branches retain their old no-explicit-MPPT rule. A shared
mode gate is reused by both paths rather than duplicating their mode logic.
Sequential counted planes, flat/non-averaged source and target, full counts,
external target-ID and circuit-level orientation reference guards remain.
Every plane must retain a user; no pruning/renumbering occurs. NoOrientation
alone changes only the selected root reference, not geometry, circuit, mode,
input counts, models, nominal tags or losses. NInvMPPT edits remain separate.

The existing native orientation driver adds the optional alias
`--mode mppt_reassociation` (three native runs, installed pypdf). An isolated
Residential source is formally switched to independent mode and tilt 15;
member 2 clones with a separate plane tilted 60, then member 3 clones source
on plane 1. Before/after/control references are [1,2,1]/[1,2,2]/[1,2,1]. The
after case changes member 3's NoOrientation only; the control also makes both
plane tilts 60. Each case reports three inverters, six strings and six explicit
inputs; PDFs list three independent `2 * MPPT 50%` arrays and No power sharing.
E_Grid totals are 260.4613/259.0381/257.6174 kWh over 48 finite rows. Maximum
hourly E_Grid delta is 1.8320 kW; mixed-plane cumulative residual is 0.0009598%,
consistent with both controls. Control/reference restoration followed by
member 3 removal and member 2/final-plane deletion recovers the prepared
single-plane independent source bytes exactly; vendor inputs stay read-only.

Evidence: `component-edit-verification/mcp-capacity-8e7165e1e827/evidence.json`,
including mode/tilt preparation, copies, actual parameters, three CLI CSV/PDFs,
control restore, both removals and baseline_bytes_restored. The existing method
verifies sole-user stop, preview/apply, only one reference-line difference,
[1,2,2] ownership, restore and exact deletion recovery. Unknown independent
format, ordinary explicit MPPT and circuit-level orientation guards stay
closed. Full suite remains 179 methods and formal MCP lifecycle smoke passes.
No new public tool or dependency is added; optional alias reuses the existing
driver. Arbitrary mixed-input association, shared cross-plane redistribution,
loss fitting and GUI round-trip proof remain pending. No binary/DLL analysis
occurred.

## Independent MPPT non-final plane removal

The existing remove_orientation option now reuses the shared plane-mode gate
for verified exclusive independent inputs during non-final reindexing. All
subarrays must own their full branches; ordinary/no-explicit layouts or known
Version=8.1.0 `$0400`/direct-MPPT/disabled -1 layouts are accepted only with
complete consistent counts, single string orientation and flat non-averaged
sequential planes. Every NoOrient*/OrientationId field must be an owned plane
NoOrient or subarray-root NoOrientation; circuit or external references stop
before writes. The removed root/branches/plane are pruned; higher plane/root
references shift by one. Root IDs, input/leaf quantities, modes, components,
nominal tags and loss fields of survivors are not changed. This is not input
redistribution or physical-device fitting.

The optional native alias `--mode mppt_pruning --prune-plane 1|2` reuses the
existing pruning driver, with two native executions per selection. Source is
isolated Residential, formally independent; before controls have two planes,
then a third independent plane/member is appended and the first or middle
member/plane removed. For first-plane deletion surviving source IDs [2,3]
reference [1,2] at tilts [35,60], map {2:1,3:2}; for middle deletion IDs [1,3]
reference [1,2] at [15,35], map {3:2}. Each final case has two inverters, four
strings, four explicit inputs, PDF `2 * MPPT 50%` per member and No power sharing.
All 48 finite hourly records equal the matching before control exactly.
E_Grid before/after is 177.7667 kWh for first-plane deletion and 179.1899 kWh
for middle deletion. Restore/removal recovers the prepared single-plane source
bytes; vendor inputs remain read-only.

Evidence: `component-edit-verification/mcp-capacity-17f8e0a9621c/evidence.json`
and `component-edit-verification/mcp-capacity-abe71f1e512e/evidence.json`, with
actual parameter IDs/angles, orientation_id_map, native CSV/PDF, full-hourly
equality and baseline_bytes_restored. The existing representative method adds
both selections, mode/input/count preservation, exact snapshot recovery and
injected circuit-level OrientationId pre-write stops. A fixture decoder was
corrected to preserve its existing BOM/CRLF instead of introducing a second BOM.
Full suite remains 179 methods and full MCP lifecycle smoke passes. No public
tool or dependency was added; the alias only changes the native fixture source
and reuses the existing pruning flow.

General mixed-input maps, shared cross-plane topology, tracking/dome/shading
geometry, fitting and GUI round-trip proof remain pending. No binary/DLL
analysis occurred.

## Independent MPPT inverter count edit

NInverter alone now supports verified Version=8.1.0 `$0400` exclusive direct
MPPT layouts with explicit unshared -1, complete consistent input counts and
all strings in one unshaded fixed plane. Each inverter group's NElements scales
proportionally; MPPT and string-leaf multiplicities remain unchanged. Root
NInverter, total NInvMPPT, NStringCh and NStrOrient1 synchronize. Each resulting
group/input/string count must be integral and a changed inverter NElements must
already be explicit. Independent NInverter, NInvMPPT and NStringCh operations
stay separate; shared-parent inverter editing is closed. Modes, other flags,
components, nominal tags, resistance and loss parameters remain unchanged;
physical counts/power still require the native model, not just diagram counts.

The existing quantity smoke adds an independently authored two-inverters VC9.
MCP clones authored one-group/two-input VC1 into VC8, previews/applies
NInverter=2 and verifies all edited bytes against VC9. Structure moves from
1 inverter/4 strings/2 inputs to 2/8/4; MPPT multiplicity stays 2 and leaf
multiplicity stays 2. CLI runs edited VC8: 48 finite hourly rows, PDF
`4 * MPPT 50%`, EArray/E_Grid/IL_Pmax=275.2646/270.2011/80.5226 kWh versus
baseline 138.1258/135.5828/44.2165 kWh. Scaling is approximately two, not forced
exact equality because loss fields are not refitted. Formal restore recovers
VC8 source bytes after simulation. Existing quantity, string, shared-parent
construction/removal controls still pass; this mode now needs eight native
executions (seven cases plus one post-removal repeat).

Evidence: `component-edit-verification/mcp-capacity-68a4c61fcfe2/evidence.json`,
with independent_inverter_edit preview/apply/control byte match/restore and
native CSV/PDF. The first attempt ead048acab85 correctly stopped at the missing
explicit inverter count. The authoring fixture now explicitly writes inverter
NElements, including 1; production guards were not weakened. In 22afd8944716,
that insertion exposed stale author-control root offsets; root spans are now
reparsed after topology insertion. The corrected clean-baseline run completes.
No binary/DLL analysis occurred.

The existing method verifies preview/restore, unchanged MPPT grouping, exact
root/inverter-only count changes, closed mixed edits, and fractional multi-group
allocation stops. Full suite remains 179 methods and MCP lifecycle smoke passes.
No public tool, dependency or new probe mode was added. Hardware port sizing,
shared scaling, arbitrary allocations, refitting and GUI round-trip proof
remain pending.

## Series-module count native verification

The existing NModSerie scalar is verified through formal MCP preview/apply and
native simulation, not merely parser acceptance. No production resizing logic
or new public option is added. The quantity probe authors longer_series VCB
from partial VC0 (known independent `$0400`, one input, two parallel strings,
15 series modules), changing only root NModSerie to 18. MCP clones VC0 to VCA,
previews/applies the same scalar and compares all candidate bytes with VCB.

Native structure stays one inverter/two strings/one explicit input; module
count changes from 30 to 36. Parameter PDFs show `2 units x 15`/`2 units x 18`,
nominal DC 9.00/10.80 kWp, and 50 C operating Pmpp/Umpp/Impp
8.10 kWp/427 V/19 A to 9.72 kWp/512 V/19 A. Voltage follows the 18/15 ratio
within PDF rounding (0.5% tolerance), while current stays unchanged. These
values were independently checked from saved native report_text and the
existing driver now retains that assertion/operating_at_50c diagnostic.
All 48 finite hourly rows are available and differ from baseline; cumulative
EArray/E_Grid/IL_Pmax moves from 69.1857/67.9120/23.0968 to
73.3532/71.9992/37.6083 kWh. Energy does not scale by module count alone:
inverter limiting increases and saved loss/model fields are not refitted.
The original mode, input/leaf multiplicities, components, sharing, nominal
inverter tags and losses stay byte-identical outside the scalar. Formal
restore recovers source bytes after the native run.

Evidence: `component-edit-verification/mcp-capacity-61b22897de3b/evidence.json`,
series_module_edit preview/apply/reference match/restore, case longer_series
inspection and native CSV/PDF. Voltage/current checks were also replayed on
these persisted reports. Existing input, inverter, string and shared-parent
controls continue passing; quantity probe now consumes nine native executions
(eight cases and one post-removal repeat). The native CLI accepted VCA/VCB IDs.
179 unit methods and complete formal MCP lifecycle smoke pass, without new
regular tests or dependencies.

This verifies one independent unshaded fixed-plane series edit, not automatic
cold-Voc/MPPT window/current sizing, physical cable/loss refitting, shaded scene
module remapping or every system/version. Broader geometry, shared topology
and GUI round-trip remain pending. No binary/DLL analysis occurred.

## Unified geometry edit guards

A production gap was confirmed in scalar NModSerie: it wrote the root directly
without the geometry gates used by angle/count operations. A separate root
PVObject_ModuleLayout was also absent from those geometry checks. The installed
text sample _DEMO_Commercial.VC8 (lines 858 onward) defines a module layout
with mlDefInverters, linked 3D fields, per-table mlDefStrings and explicit
Module_N string/module mappings. Changing series/counts or removing members
without updating that map can leave inconsistent persisted state even when
root/circuit declarations are coherent. No binary inspection was needed.

One _require_no_geometry gate replaces duplicated checks in series/angle,
capacity/mode/reference, clone and removal paths. Any active/unknown shading
flag, scene/table, nested shading object, malformed/ambiguous section or any
PVObject_ModuleLayout stops before writing. Even a standalone or empty layout
block remains closed until its semantics and remapping have evidence. Series
and angle scalars retain their old absent-shading-section allowance; structural
and capacity/mode paths still require exactly one supported empty section.
The verified empty flags remain $00/$00020000/$00080000. Fixed-plane, ownership,
count, mode, reference and optimistic transaction checks remain separate and
unchanged. This gate does not rebuild geometry or silently discard caches.

Existing representative tests cover series preview/apply against active scenes
and tables, standalone module-layout series/angle/count/reference stops, and
clone/remove/mode stops on an otherwise valid unshaded circuit. They verify
unchanged source bytes and unchanged/no backup inventory. Existing unshaded
Battery scalar edits remain supported; no new test method/tool/dependency was
added. Full suite remains 179 methods, full MCP lifecycle smoke passes.

The complete nine-execution quantity probe was rerun with the shared gate:
all previously verified input/string/inverter/shared-parent controls, native
post-member-removal equality and exact restores pass. Series 15 to 18 still
yields EArray/E_Grid/IL_Pmax=73.3532/71.9992/37.6083 kWh and operating voltage
427 to 512 V at unchanged 19 A. Evidence:
`component-edit-verification/mcp-capacity-5e0034e0de7f/evidence.json`, including
operating_at_50c, reference byte matches and all baseline_bytes_restored fields.
Vendor samples and workspace remain read-only.

Complex module-layout remapping, scene/cache regeneration, tracking/dome
coupling and GUI round-trip remain pending. No claim that model/geometry
synchronization is implemented, and no binary/DLL analysis occurred.

## Non-averaged fixed-plane angle edits

Angle editing previously required FieldType=FixedPlane and clear shading, but
ignored UsesTiltAver/UsesAzimAver/UsesSlopeAver and explicit unknown Flags.
It now reuses _ordinary_fixed_plane rather than adding a parallel validator:
flat record, FixedPlane, unique Flags=$00 when present, and absent or unique
False averaging fields. True/unknown/duplicate averages, unknown/duplicate
flags and nested geometry stop before preview/apply writes or backups. Legacy
scalar records omitting Flags remain accepted; all structural callers retain
their existing explicit-$00 requirement. This does not rewrite averages or
claim averaged/tracking/dome geometries were implemented.

The existing scoped-parameter method checks the negative states, unchanged
bytes/backup inventory, legacy no-Flags edit, explicit non-averaged edit and
exact recovery. No new method, public tool or dependency. All 179 methods and
complete MCP lifecycle smoke pass. Native mppt_orientations (three runs) still
reports two 2 * MPPT 50% arrays with no power sharing and E_Grid totals
174.5883/173.1664/171.7451 kWh for 15/15,15/60,60/60 tilts. Maximum hourly delta
is 1.8321 kW; mixed cumulative residual 0.0003437%; final clone/plane removal
recovers the prepared source bytes. Evidence:
`component-edit-verification/mcp-capacity-eef6d07d1470/evidence.json`.
No binary/DLL analysis occurred; averaging reconstruction and GUI round-trip
remain unverified.

## Active shared member explicit lifecycle

Existing tools compose an active-group lifecycle without changing the deletion
contract: inspect/save active bytes, explicitly power_sharing_enabled=false,
then remove_subarray(shared_member=true) with the toggle's returned hash. Direct
active deletion still stops. Restore deletion first to recreate the disabled
pair, then restore the toggle to recover the active pair. Two separate guarded
transactions remain visible; this is not an atomic or implicit-disable delete.
If another edit intervenes, SHA preconditions still require fresh inspection.
The bounded parent must own two single-input members, explicit owner 0, common
unshaded fixed plane and full counts; all other sharing must be disabled.

The existing sharing probe uses explicit canonical parent owner 0 for its
controls, preserving the previously verified native budgets/results. It also
authors single-member VC9 before adding a peer, with independent mode/unshared
-1 and saved nominal 5.00. Formal MCP clones active VC2 to VC8, verifies direct
delete is stopped without byte changes, disables, deletes member 2 and checks
VC8 against the independently authored VC9 bytes. Structure is one inverter,
two strings, one input; PDF reports `1 * MPPT 50%` (3.8 kWac) rather than the
former automatic 67/33 sharing. Both VC8 and VC9 produce identical 48 finite
hourly records: EArray/E_Grid/IL_Pmax=69.1857/67.9120/23.0968 kWh. Saved nominal,
losses and other survivor fields were not refitted. Restoring deletion then
toggle reproduces both disabled and active snapshots exactly.

Evidence: `component-edit-verification/mcp-capacity-4e9e2d3fb999/evidence.json`,
active_shared_member_lifecycle including direct-delete stop, both transactions,
reference byte match, two native CSV/PDFs, hourly_outputs_identical and both
restores. Source/vendor workspace stays read-only. The initial 7353c7ddaf2c
probe used SDK JSON's isError name instead of Python is_error; 9a8cabde15aa
used hyphen dates instead of the existing supported dotted CLI profile. Both
were fixture errors, corrected to existing client/date conventions; the fresh
full probe passes. Finite-value assertions exclude the timestamp column.

The existing regular shared-layout method adds the same state/restore chain;
179 methods and complete MCP lifecycle smoke pass. No production semantics,
public tool, option, mode or dependency was added. Sharing probe now requires
seven executions (five budget cases and two single-member lifecycle controls).
General merge/split, implicit/atomic active removal, shared cross-plane
allocation and GUI round-trip remain pending. No binary/DLL analysis occurred.

## MCP expected CLI error feedback

The 9a8cabde15aa shared-lifecycle fixture exposed another real boundary issue:
a hyphen date was validated before launch, but raw ValueError was treated as
an unexpected SDK 2.x tool error, so the client received no correction reason.
The categorized_tool registration boundary now wraps the synchronous public
tools with functools.wraps and converts only ValueError, FileNotFoundError,
FileExistsError and PVsystError to ToolError. Existing component/variant ToolError
paths remain intact; unknown internal exceptions still use SDK handling.
Original functions are returned unchanged to direct Python callers; tool names,
descriptions, signatures, strict argument schemas and return data are preserved.
No catch-all, repeated per-tool wrappers, new tool or dependency was added.

The existing invalid-date method calls the public MCP SDK boundary with bad
date format, invalid calendar, unsupported old-CLI option and escaping output
name: each retains its reason, creates no output and never calls _run. Direct
Python validation contracts stay unchanged. All 179 methods and complete stdio
component/project/variant/generator lifecycle pass, including strict bool/number
and curve schema failures. Existing smoke_stdio also checks date feedback when
PVSYST_SMOKE_RUN_PROJECT/VARIANT are set, even with SFI/output unset (no run).

Live 8.1.6 stdio smoke used the retained isolated
mcp-capacity-4e9e2d3fb999/PVsyst8.1_Data, project MCP_MPPT_PROBE.PRJ/VC9.
Capabilities/license/project queries and all 47 registered tools remain valid.
The bad-date call returns `Error executing tool pvsyst_run_simulation: Date
must use MM.DD, YY.MM.DD, or YYYY.MM.DD`; its unique preflight CSV is absent.
No simulation was launched, native controls were not rerun, no vendor source
was edited and no binary/DLL analysis occurred. Whole GUI/geometry coverage
is not implied by this error-feedback correction.

## Single-client concurrent initialization

cli() previously tested the empty global cache without coordination. A bounded
constructor/barrier reproduction forced two cold-start requests to construct
two clients and return two different workspace RLocks. This defeated the
intended shared CLI/component/variant lock even within one MCP process.
The existing factory now holds one standard threading.Lock while checking,
constructing and returning its cached client. All stores continue inheriting
the same client's RLock; no new store factory, runtime option or locking layer
inside operations. This is process-local only, not cross-process exclusion or
crash-atomicity. External editors and writers still need coordination; SHA
checks remain optimistic.

The existing concurrent-output method now drives eight synchronized first
requests through a delayed constructor, requiring one constructor/client,
checks the component store lock identity, and drives two same-output requests
through MCP's Python function with exactly one execution/success. No new test
method. All 179 methods pass. The existing stdio lifecycle's project creation
was moved to its cold-start entry and submitted twice concurrently: one MCP
success, one specific existing-project error, intact original PRJ/VC0 contents,
then full component/project/variant/generator lifecycle success. No additional
project fixture or dependency. Live 8.1.6 stdio capabilities/license/projects
and specific-date preflight still pass, all 47 tools unchanged, no simulation
output. No binary/DLL analysis and no broader GUI/geometry claim.

## Fixed-plane GUI round-trip and native comparison

Public Windows window/accessibility APIs opened the isolated
`gui-roundtrip-7cd385dab330/PVsyst8.1_Data` workspace in GUI 8.1.6. The
MCP_GUI_VERIFY.PRJ/VCX fixture had fixed tilt 35 degrees, azimuth 20 degrees,
18 modules per string, two parallel strings, one inverter and one independent
input. Orientation/system dialogs displayed these values. Save As allocated
VCY; the subsequent Save wrote it. Switching to VCX then back to VCY and
reopening orientation retained numeric 35/20/0 degrees. The orientation Comment
still says 25 degrees: it is a stale display label, not the active numeric tilt.
The GUI reported an undersized-inverter warning; this is not sizing approval.

VCX remains SHA-256 `9ffa2ae1361e835716aee0a445233a325ff76a4632e390f85ce89978db9d060b`;
VCY is `629c6c0dff9b07aa0678998e67fcdf327d7af44918dd67bcfad4bc97d9219edc`.
MCP readback retains the numeric fields, orientation reference, 36 modules,
one serialized inverter, two strings and one explicit input. GUI normalizes
root/orientation/circuit/subarray versions from 8.1.0 to 8.1.6, omits implicit
MPPT NElements=1, adds a cable resistance definition, changes RSCablagePC
1.50 to 1.25 with absolute RSCablageCh=378.105 unchanged, and normalizes empty
shading/accumulation metadata. Generic component migration prompts occurred
in the isolated copy only; filename equality alone is not model equality.

Fresh MCP stdio runs of VCX and VCY used CLI 8.1.6, explicit Wanaluwawa SIT/MET,
1990.01.01 through 1990.01.02, the same three-variable SFI and English parameter
reports. Each has 48 finite hourly rows. EArray/E_Grid/IL_Pmax total
73.4057/72.0554/39.0835 kWh for both; all 144 paired exported values match
exactly (maximum absolute differences and integrated energy differences zero).
This is equality at exported precision for these variables/period, not all
variables, full-year or arbitrary-version equivalence. Reports show 35/20
degrees, 36 modules, 512 V/19 A and one MPPT 50% equivalent 0.5 inverter,
3.8 kWac. Saved PNomAC_InMPPT=5.00 is not its effective nominal power.
CLI reports 1.25% wiring loss for both despite the old saved relative tag.
PRJ/VCX/VCY hashes remain unchanged by both simulations.

Local evidence: `native-round41.json`, `project-loaded.png`,
`target-orientation.png`, `target-system.png`, and
`saved-orientation-reloaded.png` under that lab directory. The earlier
`saved-variant-reloaded.png` captured an overwrite modal and is not reload
success evidence. The switch-success dialog and enabled homepage both show
restoration to `C:\Harness\workspace\iss\PVsyst8.1_Data`; captures
`workspace-restored-message.png` and `home-restored.png` confirm it. The
task-started GUI process then exited normally. Original vendor/ISS project
files were not edited; no binary/DLL analysis, new dependency or test method.

## GUI-saved 8.1.6 independent input diagnosis

Three additional CLI controls derive from VCY in a NEW MCP-created
MCP_GUI_MODE41 project; original VCY stays untouched. All retain 36 modules,
two strings, one inverter, tilt 35/azimuth 20 and identical component/loss tags.

| Control | Root mode / input count | Circuit inputs | EArray / E_Grid / IL_Pmax (kWh, 48 hours) |
|---|---|---|---|
| GUI VCY reference | `$0400` enabled / 1 | 1 (implicit NElements=1) | 73.4057 / 72.0554 / 39.0835 |
| VC0 whole flag-only | only `$0400` cleared / 1 | 1 | 109.5168 / 107.5012 / 3.0302 |
| VC1 two inputs | `$0400` enabled / 2 | 2, one string each | 109.5168 / 107.5012 / 3.0302 |
| VC2 declaration mismatch | only root NInvMPPT 2 to 1 versus VC1 | 2, one string each | 73.4057 / 72.0554 / 39.0835 |

VC0/VC1 parsed hourly output matches exactly; VC2/VCY/VCX parsed output also
matches exactly, not just totals. Reports distinguish one MPPT 50% (3.8 kWac),
whole inverter (7.5 kWac), and two MPPT 50% (7.5 kWac); VC1 explicitly reports
no sharing between MPPTs. Native consumption therefore follows the independent
flag and declared input quantity here, not the number of drawn nodes or saved
PNomAC_InMPPT=5.00. A count mismatch still simulates: CLI acceptance does not
prove consistent serialized design. The initial probe accidentally prefixed
full seed text instead of the optional BOM; creation rejected duplicate IDs
before any file/native run. The corrected fixture parses once and uses the
same retained VCY; this fixture error is not a product defect.

Previously inventory recognized independent input declarations only at
subarray Version=8.1.0, so all 8.1.6 controls returned a null input match and
VC2 incorrectly passed this scoped consistency check. The existing inventory
now recognizes 8.1.0/8.1.6 `$0400` for read diagnosis. Explicit complete layouts
compare total NInvMPPT directly; unknown versions, whole mode and implicit
layouts keep their existing unset-match behavior. No shared-parent recognition,
mode/count writer or plane-reconstruction version gate was widened. Known
input conflicts also stop the existing structural clone/remove path.

The existing representative grid method covers valid/mismatched/whole mode for
both recognized versions plus unknown 8.1.5; no new test method or dependency.
Local `mode-controls-round41.json` retains pre-fix reports, native CSV/PDF and
new project hashes; `diagnostics-round41.json` retains post-fix MCP checks and
no-write boundary checks. Fresh MCP inspection now returns matched inputs for
VCX/VCY/VC1, null for whole VC0 and valid=false/input-match=false for VC2 with
an explicit 1-versus-2 reason. Structural VC2 cloning stops before writes;
8.1.6 count/mode updates still stop at the 8.1.0 writer gates. All project and
backup files remain unchanged. The existing 179-test suite passes (19.597s),
complete stdio component/project/variant/generator lifecycle passes, py_compile
and git diff --check pass; tool count remains 47. No binary/DLL analysis.
Arbitrary topology, shared 8.1.6 allocation, geometry/cache reconstruction and
electrical sizing remain outside this bounded evidence.

## GUI-saved 8.1.6 exclusive mode switching

Following the preceding read-diagnosis/native controls, set_independent_mppt
also accepts subarray Version=8.1.6. This widens only the mode-switch gate;
independent count resizing, plane reconstruction and shared-parent handling
retain their 8.1.0 limits. Complete consistent counts, exclusive direct MPPT
children, explicit disabled sharing -1, a single unshaded fixed-plane assignment
and the existing geometry/system/typed-transaction checks remain required.
Only $0400 and the mode-dependent NInvMPPT declaration change; circuit nodes,
component/nominal/loss tags, quantities and geometry remain untouched.

Fresh stdio calls copied GUI VCY across projects to MCP_GUI_MODE41/VC3 and
the two-input VC1 within that project to VC4. For both copies: disable preview
preserved input bytes, apply matched the preview and independently authored
mode-only bytes, a stale enabled call stopped without writes, re-enable restored
original bytes and the explicit-input match, and repeated enable was a no-op
without another backup. Formal restore from the initial disable snapshot passed
for both. All pre-existing GUI fixture PRJ/VC files remained unchanged.

Three real CLI 8.1.6 runs used the preceding SIT/MET/SFI and 1990.01.01-02:
VC3 off and VC4 off each have 48 finite rows and EArray/E_Grid/IL_Pmax
109.5168/107.5012/3.0302 kWh, exactly matching the earlier whole-mode hourly
control. Reports show one 7.5 kWac inverter with sharing within the inverter.
VC3 on has 73.4057/72.0554/39.0835 kWh, all exported hourly values exactly
matching GUI VCY; its report shows one MPPT 50%, equivalent 0.5 inverter.
VC4 re-enable restores NInvMPPT=2 and its original bytes (the existing two-input
native control); no new VC4-on simulation was run. Homogeneous two-input and
whole-mode energy equality does not establish equivalence for unequal arrays.

Local evidence is `gui-roundtrip-7cd385dab330/mode-switch-round43.json`,
extracted as UTF-8 from the retained two-snapshot native log. The existing
representative grid method exercises both versions and an unknown 8.1.5 stop;
14 grid methods and all 179 methods pass (full suite 21.378s), complete stdio
lifecycle, py_compile and git diff --check pass. No new method, tool, dependency,
GUI workspace switch or binary/DLL analysis. Tool descriptions and README now
distinguish 8.1.6 input diagnosis/exclusive mode switching from still-closed
count/shared/plane editors. Fresh list_tools returns 47 tools and the updated
version descriptions; live MCP mismatch-mode and 8.1.6 count-resize calls stop
before writes with their specific reasons and preserve inputs. This is not
arbitrary topology or electrical sizing.

## GUI-saved 8.1.6 parallel string editing

Only independent unshared NStringCh-only scaling now accepts Version=8.1.6
alongside 8.1.0; inverter/input resizing and shared-parent/plane gates are
unchanged. Existing integral explicit-leaf, complete-count, fixed-plane,
geometry and separate-transaction checks remain in force. The representative
grid method reuses its preview/apply/restore and fractional-allocation cases
for both versions, plus unknown-version and implicit-leaf stops; no new method.

Fresh stdio copies GUI VCY to MCP_GUI_MODE41/VC5 (one input) and VC1 to VC6
(two inputs), then changes strings 2 to 4. Preview preserves bytes; apply
matches independently authored root-string/leaf-only bytes. Mode, inverter,
MPPT multiplicities, nominal/loss/resistance tags and cable metadata are intact.
No-op makes no backup; stale calls, 8.1.6 input/inverter resizing and fractional
VC6 strings stop without writes. Both structures validate with 72 modules.

Four native runs, using the same SIT/MET/SFI and Jan 1-2 1990, each have 48
finite hourly rows. Restoring each initial edit snapshot reproduces original
bytes and all three original exported hourly columns in a fresh simulation.

| Layout | E_Grid baseline -> edited (kWh) | IL_Pmax baseline -> edited (kWh) |
|---|---|---|
| One independent input | 72.0554 -> 83.8368 | 39.0835 -> 137.1237 |
| Two independent inputs | 107.5012 -> 143.9312 | 3.0302 -> 76.0458 |

Reports show 36 to 72 modules, operating Pmpp 9.72 to 19.44 kWp and current
19 to 38 A; voltage stays 512 V and effective converter power stays 3.8/7.5
kWac respectively. Preserved absolute resistance yields native STC wiring
loss 1.25 to 2.50 percent. Extra strings therefore do not double delivered
energy; this editor does not redesign hardware or refit loss parameters.
Result comparisons verify paired units/timestamps, complete coverage and
E_Grid deltas 11.7814/36.4300 kWh (16.3505/33.8880 percent).

Local `gui-roundtrip-7cd385dab330/parallel-strings-round44.json` retains tool
transactions, comparisons, native CSV/PDF references and extracted reports;
all pre-existing GUI fixture PRJ/VC bytes remain unchanged. Existing 14 grid
methods and all 179 methods pass (20.802s); complete stdio lifecycle,
py_compile and git diff --check pass. No new tool/dependency, GUI workspace
switch or binary/DLL analysis. Fresh list_tools returns 47 tools with separate
8.1.6 unshared/8.1.0 shared descriptions. Live input-mismatch and mixed
string/inverter/input updates stop before writes, preserving files and backups.
Broader topology and electrical sizing remain.

## GUI-saved 8.1.6 input and inverter quantity editing

The independent quantity gate now also accepts 8.1.6 NInvMPPT-only and
NInverter-only scaling, reusing the existing allocation algorithm. Changed
NElements must already be explicit; integral/complete/exclusive/unshared
counts, geometry and separate-transaction guards remain. Shared-parent and
plane reconstruction version gates stay at 8.1.0. No hardware refitting.

A new complete-text MCP_GUI_CAP45 project copies the existing GUI-derived
PRJ/VC1. VC0 adds only explicit inverter NElements=1; VC1 authors input
NElements 2 to 4 (root inputs/strings 4, inverter count 1); VC2 instead authors
inverter NElements 1 to 2 (root inverters 2, inputs/strings 4, input NElements
2 unchanged). Three native controls verify explicit 1 equals the earlier
implicit baseline and both edited layouts have identical exported hourly data.

| Control | EArray / E_Grid / IL_Pmax (kWh over 48 hours) |
|---|---|
| Explicit baseline | 109.5168 / 107.5012 / 3.0302 |
| Input count 2 to 4 | 217.8023 / 213.8004 / 4.9839 |
| Inverter count 1 to 2 | 217.8023 / 213.8004 / 4.9839 |

Fresh stdio writer copies VC0 to VC3/VC4 reproduce the authored VC1/VC2 bytes,
match dry-run hashes and pass structure checks. Two edited and two restored
native runs (same SIT/MET/SFI and Jan 1-2 1990) each have 48 finite rows and
exactly match the corresponding authored/baseline control for EArray, E_Grid
and IL_Pmax. Formal snapshot restore preserves original bytes; stale requests
stop without writes and no-op creates no backup. Original source/control files
remain unchanged. Implicit MPPT/inverter resizing, known input disagreement
and mixed quantities stop with their specific reasons and no backup creation.

Reports show 36 to 72 modules, 512 V unchanged, 19 to 38 A and effective
model power 7.5 to 15 kWac. Input-only editing retains serialized NInverter=1
but the native report still derives two equivalent inverters from four modeled
inputs: this is not a hardware-installation-count editor. Result comparison
has matching units/timestamps/full coverage; E_Grid grows 106.2992 kWh
(98.8819 percent), with maximum hourly delta 8.0 kW. Loss/nominal/cable tags
are preserved; native STC wiring loss changes 1.25 to 2.50 percent.

Local quantity-controls-round45.json and quantity-writes-round45.json in the
existing gui-roundtrip-7cd385dab330 lab retain all seven runs, transactions,
reports and boundary reasons. The existing representative grid method now
checks both versions and unknown 8.1.5. All 14 grid methods and 179 existing
methods pass (21.531s); complete stdio lifecycle, py_compile and diff check
pass. No added test method, tool, dependency, GUI switch or binary/DLL analysis.
Fresh list_tools returns 47 tools and the updated quantity/shared/plane version
boundaries. The first metadata probe incorrectly compared source indentation;
normalizing SDK-cleaned docstring whitespace fixes that probe without file edits.
The preceding sections describe the narrower gates at their verification time;
this section and README describe the current exclusive quantity support.
Arbitrary topology, shared 8.1.6 semantics and electrical sizing remain pending.

## GUI-saved 8.1.6 exclusive fixed-plane lifecycle

The shared plane-branch guard now accepts 8.1.0/8.1.6 exclusive independent
$0400 layouts with disabled sharing -1 and complete direct MPPT ownership.
It also owns circuit-level orientation-reference rejection for copying,
reassociation and removal; reassociation's duplicate check was removed.
Geometry, flat Flags=$00/non-averaged planes, single-orientation strings,
sequential counted lists and fully checked allocation requirements remain.
Shared-parent and tracking/dome/averaged geometry support are not extended.

Before changing the guard, three complete-text controls were authored from
GUI-saved VCY in a separate project: three subarrays on planes 35/60 degrees,
subarray 3 moved from plane 1 to 2, then subarray 1/plane 1 removed with
remaining plane/root IDs 2 to 1. All three native controls pass structure
checks and yield distinct hourly behavior for the reference-only edit.

| State | EArray / E_Grid / IL_Pmax (kWh over 48 hours) |
|---|---|
| Three arrays, assigned planes 1/2/1 | 217.9921 / 213.9810 / 112.0909 |
| Same arrays, assigned planes 1/2/2 | 215.7636 / 211.7977 / 106.9288 |
| Remove array 1/plane 1; remaining arrays 2/3 use plane 1 | 142.3549 / 139.7433 / 67.8460 |
| Restore original VCY | 73.4057 / 72.0554 / 39.0835 |

A fresh MCP writer performs independent plane copy, tilt 60, third-array copy,
reassociation and non-final deletion. Each of the three resulting states
matches its authored control bytes and all 48 exported hourly rows for the
same three variables. Five guarded transactions restore in reverse order with
exact intermediate/original bytes; the fourth writer native run matches the
original GUI baseline. Seven native runs total, same SIT/MET/SFI and Jan 1-2
1990. Reference-only E_Grid delta is -2.1833 kWh (-1.0203 percent), maximum
absolute hourly delta 0.6053 kW, no missing pairs. Model/count/loss tags and
implicit circuit multiplicities remain unchanged where not owned by the edit.

A genuine final-plane deletion defect was reproduced before correction: the
no-reindex path skipped average/mode/owned-reference checks. A final averaged
plane was accepted, and an external OrientationId=2 could remain after its
plane was removed. The existing common checks now run even when no numeric
reindex is needed. SDK averaged/unknown-8.1.5/external-reference negatives stop
without file or backup changes; circuit references also stop independent plane
copying. The final code repeats all three positive byte controls and five
restores without spending additional native executions.

Existing representative tests cover both versions, final-plane guards and
non-final first/middle reindexing. All 179 methods pass (20.708s), complete
stdio lifecycle, py_compile and diff check pass. No added test method, tool,
dependency, GUI workspace switch or binary/DLL analysis. Metadata remains 47
tools. Local plane-controls-round46.json, plane-writes-round46-verified.json
and final-plane/reference before/after JSON evidence retain runs and recovery.
The first writer probe expected the wrong orphan-plane error wording; its two
transactions were restored, no native run occurred, and the corrected probe
uses the observed error and existing GUI baseline filename.
Earlier verification sections retain their historical version boundaries;
README and this section describe current exclusive fixed-plane support.
Arbitrary topology, sharing 8.1.6 and electrical reconstruction remain pending.

## GUI-saved 8.1.6 bounded shared-parent lifecycle

Two production version gates were extended after independently authored native
controls: shared-parent recognition accepts homogeneous 8.1.0 or 8.1.6 member
declarations, and single-input/inverter shared construction accepts either.
Existing count/ownership/name/model/group constraints remain. Recognition adds
an explicit same-version check; mixed 8.1.0/8.1.6 and unknown 8.1.5 parents stay
unchecked, with no misleading totals. Toggle, member-string editing and disabled
two-member removal reuse that common inventory, not separate version branches.

Four complete-text controls from original GUI-saved VCY use one canonical owner-0
inverter, two direct owned MPPT children, common fixed plane 35/20 degrees and
one modeled input per member. The two balanced controls disable/enable sharing;
the other two change only member 2 from 2 to 4 parallel strings, then compare
active automatic sharing to disabled sharing. Implicit inverter/input
multiplicities stay implicit; member 2's existing explicit leaf count changes.

| State | EArray / E_Grid / IL_Pmax (kWh over 48 hours) |
|---|---|
| Balanced disabled or active | 146.8123 / 144.1108 / 78.1670 |
| Member 2 has 4 strings; active sharing | 163.2503 / 160.2103 / 171.7997 |
| Same 2/4 strings; disabled sharing | 158.8442 / 155.8922 / 176.2059 |
| Remove member 2 or restore original VCY | 73.4057 / 72.0554 / 39.0835 |

Balanced toggling leaves all exported hourly rows identical. For the unequal
pair, native PDFs show 33/67 percent and 2.5/5.0 kWac with sharing, versus
50/50 percent and rounded 3.8/3.8 kWac without sharing; total is 7.5 kWac.
E_Grid gain from sharing alone is 4.3181 kWh (2.7699 percent), maximum paired
hourly difference 1.1229 kW, all 48 pairs finite. These are this two-day model's
results, not a general performance claim or converter sizing certificate.
The saved PNomAC_InMPPT=5.00 tags, components, mode, resistance and losses
stay unchanged. Native derives 36/72 modules, 19/38 A at 512 V and wiring
fractions 1.25/2.50 percent; no loss/nominal fitting is performed.

A fresh stdio writer creates the bounded parent, previews/enables sharing,
edits member 2 strings, disables sharing, deletes member 2 and reverses all
five guarded transactions. All four intermediate files exactly match authored
controls; six writer native runs match controls or the original GUI baseline
for 48 rows of EArray/E_Grid/IL_Pmax. Ten native runs total, pinned SIT/MET/SFI
and Jan 1-2 1990. Active deletion and shared inverter/input resizing stop without
file/backup changes. Preview/no-op and every intermediate restore are checked;
source files and original GUI bytes remain intact.

The existing representative shared method loops over both versions, retaining
positive counts, member edits, both deletion directions, active-group explicit
lifecycle, invalid allocation/options/collision and mixed/unknown-version cases.
179 methods pass (21.690s), full stdio lifecycle, py_compile and diff checks pass;
no added method, tool or dependency. SDK mixed/unknown-parent probes confirm
null diagnosis/totals and no-write toggle/member/delete responses; an unknown
single-member version stops shared construction. Metadata remains 47 tools.
Local shared-controls-round47-verified.json, shared-writes-round47.json and
shared-boundaries-round47.json retain controls, reports, transactions/recovery
and boundary reasons. Initial import/PowerShell quoting fixture errors occurred
before any native launch or fixture write and were corrected using actual exports.
Earlier sections retain their historical gates. Current bounded shared support
includes 8.1.6; arbitrary merge/split, cross-plane allocation, geometry/electrical
reconstruction and full-type GUI round-trip remain pending. No binary/DLL analysis.

## LID loss scalar native verification

Official [LID documentation](https://www.pvsyst.com/help/project-design/array-and-system-losses/lid-loss.html)
and [module mismatch documentation](https://www.pvsyst.com/help/project-design/array-and-system-losses/array-mismatch-losses/index.html)
define separate losses. GUI-saved VCY contains PMismMPP=2.00 and LIDLoss=0.00.
Complete-text single-variable controls preserve all flags/components/circuit
quantities. Saved PMismMPP=5.000 or 100.000 both produce native 2.00 percent and
exactly the baseline 48-row output. At round 48 that scalar was withheld from
the editor pending GUI default/manual binding evidence. The later mismatch
sections below establish the binding and its bounded production transaction;
no flag mapping or internal algorithm was inferred from the spelling alone.

LIDLoss=3.000 is consumed: the PDF reports 3.00 percent, EArray/E_Grid/IL_Pmax
are 72.6663/71.3298/36.4481 kWh, versus baseline 73.4057/72.0554/39.0835.
The paired E_Grid delta is -0.7256 kWh (-1.0070 percent), maximum hourly
difference 0.1065 kW. Clipping changes as well, so 3 percent is not claimed to
be the final grid-energy percentage. LIDLoss=100.000 gives 48 finite all-zero
export rows and a 100-percent loss report. Declared subarray Version=8.1.0
with 3 percent exactly matches the 8.1.6 declaration under CLI 8.1.6.
The evidence is two days (Jan 1-2 1990), three variables, pinned SIT/MET/SFI
and one module/inverter layout, not a technology applicability certificate.

The existing subarray_updates dictionary gains only LIDLoss in [0,100]
percent, for existing unique owning-root fields in declared 8.1.0/8.1.6 Grid
subarrays with no active geometry/layout. Inspector returns the raw percentage.
No fields/defaults are invented, flags/components/other loss values stay intact.
The common field lookup reuses objects() for subarray/system roots, excluding
nested PVObject metadata from IDs, versions, references and scalar targets.
A child-only LID value is not used as an absent root value.

A real SDK preview with LIDLoss=true exposed float coercion to 1.000 despite
the backend's bool check. Both scalar dictionaries now use existing StrictFloat:
integers/floats remain accepted; booleans and numeric strings stop at the SDK.
Finite/range validation remains in the backend. Three writer edits (3,100,0),
preview/no-op/restore checks, four native writer runs and thirteen type/range/
unsupported/child-only/version/layout SDK guards complete without source drift.
0.00 to 0.000 is an explicit formatting change, not a byte-level no-op; after
normalization a repeated same value creates no backup. Each formal restore
recovers original bytes and the last run exactly recovers baseline rows.

Nine new native runs total: four original controls (including two inert
mismatch probes), one old-declaration control and four writer runs. Local
loss-controls-round48.json, lid-version-round48.json, lid-writes-round48.json
and lid-boundaries-round48.json retain reports, type-coercion reproduction,
corrected guards and recovery. Existing representative tests cover both versions,
numeric bounds, nested/missing/duplicate roots, system/layout gates; two existing
stdio scalar-type probes retain bool/string regression checks. No new test method. 179 methods pass in 22.683s, full stdio lifecycle/compile/diff checks pass,
47 tools unchanged. One existing nested-orientation stop now reports the earlier
root-missing diagnostic instead of accepting the child before a later guard.
The initial fixture used a non-layout tag and was corrected to the actual
PVObject_ModuleLayout guard; preliminary failed test output is retained.
No binary/DLL analysis or GUI round-trip was performed for this scalar.

## Strict target ID SDK verification

Six pre-fix, preview-only real SDK calls establish that true, string "1" and
float 1.0 were coerced into target integer 1 for both subarray_id and
orientation_id. All produced valid modification previews; files/backups stayed
unchanged because dry_run was explicit. Backend bool/type checks alone did not
protect these inputs after framework coercion.

Only six public annotations change to existing StrictInt across five tools:
variant copy, component reference replacement, subarray copy/removal and parameter
edits (both target selectors). Defaults, algorithms, numeric values in parameter
dictionaries and backend range/existence/ownership checks stay unchanged. Strict
integer selection is intentionally different from integral numeric quantities.
A caller must supply the integer literal 1, not true, "1" or 1.0.

Eighteen actual SDK guards cover all six selectors with those three bad types.
Each returns the integer-type validation diagnostic without new project members,
file changes or backups. Positive integer IDs exercise all five tools: a copied
variant with the existing Generic_9kW.OND reference exactly matches an in-place
reference update; LID=3 matches the authored round-48 bytes; a shared-parent clone
matches round-47 bytes; member-2 deletion recovers original GUI bytes. Integer
orientation-1 preview is accepted without a write. Four guarded modifications
are formally restored, including deletion followed by shared-construction restore.

Four native 48-row runs with pinned SIT/MET/SFI and Jan 1-2 1990 succeed:
reference replacement EArray/E_Grid/IL_Pmax 112.4898/109.3709/0.0000 kWh;
LID 72.6663/71.3298/36.4481; balanced shared 146.8123/144.1108/78.1670;
restored original 73.4057/72.0554/39.0835. LID/shared/restored rows exactly
match prior controls, all samples finite, original sources and OND untouched.
The reference-change run verifies loading/consumption, not converter sizing or
certification of that existing demonstration component.

Existing stdio smoke retains both selectors and all three bad input types;
179 methods pass in 34.492s, full lifecycle/compile/diff checks pass, 47 tools
unchanged. No new test method, module, dependency or topology algorithm.
Local id-coercion-before-round49.json and id-controls-after-round49.json retain
previews, validation reasons, positive operations and recovery. This closes an
SDK entry-contract hole, not the remaining loss/default bindings, GUI round-trip,
arbitrary topology or geometry/electrical reconstruction. No binary/DLL analysis.

## GUI mismatch default/manual binding verification

Three GUI exports isolate the owning subarray fields: default 2 percent to
manual 2 percent changes only Flags=$019E0402 to $019A0402; manual 2 to manual
5 changes only PMismMPP=2.00 to 5.00. Reopening the saved manual-5 variant
shows 5 percent and an unchecked default checkbox. Public BM_SETCHECK plus
GUI Confirm/Save As produced the export; attempted click/focus automation did
not persist a toggle and is not counted as evidence. No proprietary binary
or DLL analysis was performed.

The isolated original control clears only 0x00040000 in Flags=$001E0402,
with a separate 5-percent scalar edit. Six CLI 8.1.6 runs, Jan 1-2 1990,
48 finite hourly samples each, pinned SIT/MET/SFI and valid scoped structures:

| Saved source | EArray kWh | E_Grid kWh | IL_Pmax kWh | PDF mismatch |
|---|---:|---:|---:|---:|
| GUI default 2 | 73.4057 | 72.0554 | 39.0835 | 2.00% |
| GUI manual 2 | 73.4057 | 72.0554 | 39.0835 | 2.00% |
| Minimal manual 2 | 73.4057 | 72.0554 | 39.0835 | 2.00% |
| GUI manual 5 | 72.6508 | 71.3146 | 36.3924 | 5.00% |
| Minimal manual 5 | 72.6508 | 71.3146 | 36.3924 | 5.00% |
| Minimal default, saved 5 | 73.4057 | 72.0554 | 39.0835 | 2.00% |

GUI/manual and minimal/manual exports match every hourly value. Default mode
ignores saved 5 percent; clearing this bit consumes the saved scalar. Manual
5 minus manual 2 gives -0.7408 kWh E_Grid (-1.0281 percent), maximum hourly
absolute difference 0.1088 kW, complete finite pair coverage. This explains
the earlier inert scalar probes; it does not establish all versions, module
technologies, shading models or annual behavior. Round-51 work identified
the binding but did not yet add a production mismatch editor.

Local mismatch-consumption-round51.log preserves the UTF-16 raw output;
mismatch-consumption-round51.json is the separate UTF-8 derivative containing
all six run paths, report text, owning-field diffs and the paired comparison.
GUI exports remain unchanged by simulation. The experiment GUI workspace was
restored to the original ISS workspace and its task-created process exited
normally. Original VCX/VCY bytes still match round-41 evidence; no claim is
made about original PRJ byte preservation without a pre-experiment snapshot.
No new test method, test suite or vendor file was added to the repository.

## Manual mismatch parameter transaction

At round 52 the existing subarray_updates contract gained PMismMPP in [0,100]
percent for declared 8.1.6 Grid subarrays without active geometry/layout, with existing
unique owning PMismMPP and one valid hexadecimal Flags field. Setting the value
also clears only 0x00040000, explicitly selecting manual mismatch; silently
writing an inert scalar is not treated as a successful edit. Other bits, loss
fields, components, circuit quantities and nested metadata are preserved.
Inspector returns saved PMismMPP and raw Flags; update returns mismatch with
boolean default_before/default_after. No new mode tool, default calculator,
loss fitting or topology algorithm. The existing snapshot restore recovers the
original default mode as well as the saved value.

Fresh repository MCP stdio verifies preview/apply byte equality, repeated manual
value no-op without a backup, stale input guard, four bool/string/range guards
without writes, and three formal exact restores. Five additional CLI 8.1.6
runs have valid scoped structures and 48 finite hourly rows each:

| Control | EArray kWh | E_Grid kWh | IL_Pmax kWh |
|---|---:|---:|---:|
| Writer manual 0% | 73.9091 | 72.5491 | 40.8775 |
| Writer manual 5% | 72.6508 | 71.3146 | 36.3924 |
| Writer manual 100% | 0.0000 | 0.0000 | 0.0000 |
| Restored default | 73.4057 | 72.0554 | 39.0835 |
| Declared 8.1.0 manual 5%, authored control | 72.6508 | 71.3146 | 36.3924 |

PDFs explicitly report each requested percentage. Writer 5% exactly matches
the round-51 GUI manual-5 hourly export; restoring matches the GUI default-2
export. The declared 8.1.0 single control also matches 5%, but this round does
not expand the writer's version gate: old-declaration mode transitions and
boundary writer transactions remain to be verified. Native evidence remains
a pinned two-day unshaded grid case, not annual/all-technology certification.

Local verify_mismatch52.py replays the fresh-project experiment and writes
mismatch-writes-round52.json with previews, edits, no-ops, diagnostics, formal
restores, run paths and PDF text. Outputs are never overwritten; a second run
requires a fresh project/output prefix. The repository receives no vendor input
or simulation output. Existing representative parameter tests were generalized
to share LID/mismatch cases, adding nested flag/field isolation and invalid mode
binding checks without another test method or file. Existing stdio smoke covers
strict mismatch bool/string values. 179 methods pass in 35.708s; component/project
stdio lifecycle and compile/diff checks pass. Installed CLI capability/license/
project inventory smoke passes with 47 tools. Its first invocation stopped for
an unset PVSYST_CLI environment variable before calling the server; the explicit
installed-path retry passed. No binary/DLL analysis.

## Older declaration mismatch transactions

Three authored controls under CLI 8.1.6 establish the same binding for an owning
subarray declared 8.1.0: default mode with saved 5% matches default 2% hourly
output; manual 0% and manual 100% match the round-52 boundaries exactly. The
pre-expansion SDK preview stopped at its 8.1.6-only version gate without writes.
Only that version whitelist is broadened to 8.1.0/8.1.6. The same field ownership,
strict numeric/range checks, hexadecimal Flags, geometry gates and transaction
algorithm remain in use; no additional tool, mode switch or fitting code.

A fresh post-change SDK process exercises four writer transactions (0/2/5/100%)
plus restored default. Each native run has a valid scoped structure, 48 finite
hourly samples and a PDF reporting the requested mismatch. All rows match the
corresponding round-51/52 controls, not just aggregate energy:

| Condition | EArray kWh | E_Grid kWh | IL_Pmax kWh |
|---|---:|---:|---:|
| Default, saved 5%; manual 2%; restored default | 73.4057 | 72.0554 | 39.0835 |
| Manual 0%, control and writer | 73.9091 | 72.5491 | 40.8775 |
| Manual 5%, writer | 72.6508 | 71.3146 | 36.3924 |
| Manual 100%, control and writer | 0.0000 | 0.0000 | 0.0000 |

The seed deliberately uses PMismMPP=2.000 while default mode is set. Setting 2
changes only Flags=$001E0402 to $001A0402, proving a real mode-only transaction
rather than mistaking an unchanged saved value for a no-op. A repeated manual
value then produces no change/backup. All four previews equal applied bytes;
stale edits stop; four SDK bool/string/range guards leave files/backups intact;
four formal restores recover the exact default-mode seed. Original GUI source
bytes remain untouched.

Local mismatch-controls-round53.json and mismatch-writer-round53.json retain
eight total native runs, reports and transactions. verify_mismatch53.py records
the two-stage experiment: controls with the old gate, then writer with the
expanded gate; all project/output names are new and never overwritten. This is
CLI 8.1.6 loading an 8.1.0 declaration, not a CLI 8.1.0 executable test or a GUI
8.1.0 round-trip. The pinned two-day unshaded model remains the evidence boundary.
One existing test loop gains the 8.1.0 positive case and drops its obsolete
unsupported-version case; unknown 8.1.5 remains a negative case. 179 methods pass
in 35.449s; SDK lifecycle/compile/diff checks pass. No new repository test method,
module, dependency, vendor input or binary/DLL analysis.

## Main-array constant thermal coefficient

The [official steady-state model](https://www.pvsyst.com/help/project-design/array-and-system-losses/array-thermal-losses/steady-state-thermal-model.html)
defines Uc in W/m2/K and a separate wind term. The GUI-saved seed places UFactor
under pvMainArray, not pvSubArray; no wind-coefficient field is present in this
seed. Independently authored UFactor controls are consumed by CLI 8.1.6, both
in the PDF Uc entry and hourly TArray/energy values. All other bytes are retained
apart from normalized UFactor and the explicit old-declaration control.

| UFactor / condition | Maximum TArray C | EArray kWh | E_Grid kWh | IL_Pmax kWh |
|---|---:|---:|---:|---:|
| 20, seed/restored | 67.495 | 73.4057 | 72.0554 | 39.0835 |
| 15, authored/writer | 81.395 | 72.9963 | 71.6544 | 34.5647 |
| 29, authored/writer; authored 8.1.0 declaration | 54.866 | 73.7819 | 72.4235 | 43.1501 |
| 100, authored/writer | 35.517 | 74.3653 | 72.9940 | 49.3004 |
| 1, exploratory control only | 28.500 | 74.5998 | 73.2237 | 51.7234 |

The low-value 1 control does not follow the temperature trend of the ordinary
controls. The run/report are retained, but no internal explanation or nominal
full-range support is claimed. No binary/DLL analysis is needed for the bounded
editor. Adapter range [15,100] is explicit, not an asserted native minimum or
maximum; low ranges and additional thermal modes remain pending.

The existing parameter tool gains only optional main_array_updates={"UFactor":29}
and the inspector adds main_array.values (UFactor, Flags, Flags2). A separate
scope is necessary because this coefficient governs the whole main array, not
the supplied subarray selector. One owning main array, unique existing UFactor,
Grid system and all subarrays declared 8.1.0/8.1.6 are required. The observed
main-array Flags=$00100400 and Flags2=$082A pair is required and remains unchanged;
unknown modes, active geometry/layout, wrong scopes and missing/duplicate fields
stop without saving or backing up. The existing object parser excludes nested
field/flag decoys. The current bounded parser accepts only one complete main
array; it does not merge duplicated blocks. Wind terms, Alpha, UFAverEffic,
components and topology remain untouched. No extra tool, model calculator,
fitting code, dependency or defaults are introduced.

Six native controls plus three SDK writer runs and one restored baseline give
ten runs, each with a valid scoped structure and 48 finite hourly rows for
EArray/E_Grid/IL_Pmax/TArray. Every writer matches its independent control in all
exported rows; its preview equals applied bytes, repeated normalized value is
no-op, stale edit stops and formal restore recovers the seed exactly. Four SDK
bool/string/range guards preserve files/backups. The original GUI source is
unchanged. Local verify_thermal54.py controls/writer and thermal-controls-round54.json /
thermal-writer-round54.json retain the full experiment, diffs and PDF text; names
are new and existing outputs are never overwritten. Native runtime is CLI 8.1.6,
not the CLI 8.1.0 executable; the old declaration is only an authored control.
Evidence is the pinned two-day unshaded grid case, not annual/model-wide certification.

The existing representative parameter test loop now covers main-array ownership,
both declarations, nested decoys, flags/missing field/range/system/layout stops,
wrong dictionary scope and mode-transaction isolation. Existing SDK strict-value
checks gain two main-array cases. No new test method or module: 179 methods pass
in 31.119s, with SDK lifecycle and compile/diff checks passing. The tool count
remains 47; fresh installed-CLI inventory/capability/license/project smoke passes.
A running MCP client needs a server restart to discover the new optional argument
rather than assuming host metadata refreshes itself.

The initial reduced-directory fixture failed workspace loading. Copying the
original workspace directory structure, as the existing grid smoke already
does, resolved the fixture issue. That failed attempt never tested capacity.

```powershell
$env:PVSYST_CLI='C:\Program Files\PVsyst8.1.6\PVsystCLI.exe'
$env:PVSYST_WORKSPACE='C:\Apps\PVsyst8.1_Data'
$env:PVSYST_EDITOR_LAB='C:\Harness\workspace\component-edit-verification'
python tests/smoke_live_capacity.py                 # scale inverter groups
python tests/smoke_live_capacity.py --mode strings  # fixed inverters, more parallel strings
python tests/smoke_live_capacity.py --mode subarrays  # copy grouped subarray, simulate, remove
python tests/smoke_live_capacity.py --mode orientations  # independent plane and both controls
python tests/smoke_live_capacity.py --mode reassociation  # reference-only edit and tilt control
python tests/smoke_live_capacity.py --mode mppt_branches  # retain exclusive MPPT layout, simulate, remove
python tests/smoke_live_capacity.py --mode mppt_mode_probe  # four clipping/PDF controls; installed pypdf
python tests/smoke_live_capacity.py --mode mppt_flat_probe  # one node-absence counterexample; installed pypdf
python tests/smoke_live_capacity.py --mode mppt_quantity_probe  # nine runs including series/inverter/shared clone/remove; installed pypdf
python tests/smoke_live_capacity.py --mode mppt_sharing_probe  # seven budget/member/lifecycle controls; installed pypdf
```

Each mode consumes two CLI executions, except orientations/reassociation which
consume three, mppt_mode_probe which consumes four,
mppt_sharing_probe which consumes seven, mppt_quantity_probe which consumes nine and mppt_flat_probe which
consumes one. Vendor files and simulation outputs are
not included in the repository. No binary or DLL analysis was performed.

## Module quality paired binding and transactions

The [official module-quality loss documentation](https://www.pvsyst.com/help/project-design/array-and-system-losses/module-quality-losses.html)
allows user loss/gain and explains tolerance-based defaults, including -0.75%
for 0...+3% sorting. It does not document the serialized binding. The GUI-saved
mono grid seed stores PQualityModU/I/P=3.00 and default Flags=$001E0402, while the
native report uses -0.75%. Independent controls are required before exposing a
setter; a plausible PQualityModP-only field/default-bit guess was refuted.

| Authored change, other bytes preserved | Native reported quality % | EArray kWh |
|---|---:|---:|
| P=5, original default Flags | -0.75 | 73.4057 |
| P=5, clear only 0x00020000 | -0.75 | 73.4183 |
| P=5, clear only 0x00080000 | -0.75 | 73.4057 |
| P=5, clear only 0x00100000, U/I remain 3 | 3 | 72.4884 |
| I=5, U/P remain 3, clear 0x00100000 | 4 | 72.2436 |
| U=5, I/P remain 3, clear 0x00100000 | 4 | 72.2436 |
| I=0, U/P remain 3, clear 0x00100000 | 1.5 | 72.8553 |

These controls disprove treating P, U or I alone as a direct quality percentage.
The 0x00020000 change has a measurable energy effect despite unchanged quality
report; no meaning for that other mode bit is inferred. Failed harness assertions
are retained as refuted expectations, not grounds for forcing a field binding.
Two initial pre-run fixture attempts also failed because an unscoped Version
search matched multiple owned objects; independent construction now bounds the
exact saved subarray envelope and whole field lines. These pre-run failures
created no project and consumed no native simulation. Seven completed original
runs were captured from their preserved CSV/PDF outputs instead of overwritten
or rerun; quality-binding2-round59.json records actual results and refutations.

Subsequent independent controls synchronize owning U/I to the same requested
percentage, clear only 0x00100000, and leave P/other fields/flags intact:

| Paired requested quality % | Declared subarray version | EArray kWh | E_Grid kWh | IL_Pmax kWh |
|---|---|---:|---:|---:|
| -5 | 8.1.6 | 74.4453 | 73.0745 | 42.7883 |
| 0 | 8.1.6 | 73.2222 | 71.8754 | 38.4297 |
| 20 | 8.1.6 | 68.0115 | 66.7642 | 21.2986 |
| 5 | 8.1.0 and 8.1.6 | 71.9991 | 70.6751 | 34.0696 |
| Restore default seed | 8.1.6 | 73.4057 | 72.0554 | 39.0835 |

All five paired controls have valid scoped structure and 48 finite hourly
samples. PDF quality values are exactly the requested -5/0/5/20%; the two 5%
declarations have identical complete exported rows. This is native CLI 8.1.6
loading an authored 8.1.0 declaration, not execution of CLI 8.1.0. Adapter
[-5,20] is a deliberate verified range, not a universal native limit. Other
ranges/technologies/models, independent U/I calibration and GUI quality-loss
round-trip remain pending; no proprietary binary/DLL analysis was needed.

The existing subarray_updates dictionary adds semantic ModuleQualityLoss rather
than exposing single raw legacy fields or inventing a serialized root. Both
owning PQualityModU/I and one hexadecimal owning Flags must already occur once;
Grid, known declarations and absent active geometry/layout are required. The
raw inspector adds U/I/P. The writer synchronizes U/I and clears only the quality
default bit, preserving P, topology, component references and other losses.
Existing mismatch default-bit handling is reused as a small two-entry loop;
combined quality/mismatch requests clear their two distinct bits and preserve
other flags. Results add module_quality default_before/default_after. No
native tolerance/default calculator, fitting, new dependency, tool or argument.

Fresh SDK writer requests for -5/0/20/5 each match independently authored VC
bytes, PDF value and complete hourly control rows. Preview leaves the seed
intact, repeated normalized/manual settings produce no backup, stale requests
stop, and formal restoration recovers original bytes/defaults four times. A
final native restored baseline matches the original 48 rows exactly. Four SDK
boolean/string/out-of-range guards preserve files and backups. Original GUI
source remains unchanged. Five writer runs plus five range controls and seven
binding counterexamples total seventeen executions; quality-range-round59.json,
quality-writer-round59.json and local verify_quality59.py retain evidence.

The existing representative parameter loop adds both declarations, nested
U/I/P/Flags decoys, missing/duplicate U/I, invalid ranges/types, wrong systems/
versions/layout, shared default-bit preservation, combined mismatch/quality
transactions and exact recovery. No new test method/module. An initial suite
failed only inside the extended fixture because a read-only-key loop shadowed
the saved bytes variable; that loop local is now raw_field and the suite is
rerun: all 179 methods pass in 24.316s. SDK lifecycle, compile/diff checks and
fresh installed-CLI inventory/capability/license/project checks pass; tool counts
remain 48 total / 20 Projects & Variants. Restart the
host MCP server for updated inspection and tool description.

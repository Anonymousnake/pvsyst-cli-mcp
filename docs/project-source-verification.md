# Project source transaction verification

## Scope

`pvsyst_update_project_sources` updates an existing project's PRJ and all VC
members to a workspace SIT/MET pair. `pvsyst_restore_project_sources` restores
the entire snapshot set using its transaction manifest and backup hashes.
Neither operation creates a blank physical design or interprets binary MET
contents. Historical weather-origin fields are preserved.

## Preparation conflict regression

Previously, source update and restore checked original hashes while preparing
each candidate but did not recheck the file set after backups and staging.
An external editor's changes in that interval could be overwritten.

The shared commit helper now re-inspects all PRJ/VC hashes immediately before
the first replacement. Different content or member sets stop the transaction
before project files are replaced. Its temporary candidate files are removed;
backup snapshots remain available.

Offline checks inject these changes during backup preparation:

- Modify a sibling VC during update and during restore. Its external bytes
  survive, all other original members stay unchanged, and no temporary file
  remains. A subsequent normal transaction and restore still succeed.
- Add a valid sibling VC during update. The new member survives, and none of
  the original members is replaced.

Existing checks also cover malformed source objects, stale preconditions,
partial replacement failure, incomplete or mixed backup sets, manifest and
backup tampering, and exact-byte undo/redo.

This is optimistic conflict detection, not a cross-process lock. External
changes after the final check and process/machine failure during a multi-file
replacement remain outside the guarantee. Handled replacement exceptions
attempt rollback of already-written members. Avoid concurrent external edits
while applying these operations.

## Native verification

Command:

```powershell
$env:PVSYST_CLI='C:\Program Files\PVsyst8.1.6\PVsystCLI.exe'
$env:PVSYST_WORKSPACE='C:\Apps\PVsyst8.1_Data'
python tests/smoke_live_project_sources.py
```

The opt-in script copies `_Demo_PVsystCLI.PRJ`, calls the formal update API with
`Wanaluwawa.SIT` and `Wanaluwawa_Nasa_SYN.MET`, and runs January 1-2, 1990 without
runtime SIT/MET overrides. It verifies 48 hourly rows with `E_Grid`, and verifies
that the native CSV explicitly names both new input files. Formal restore then
recovers the original hash map exactly. Disposable projects, CSVs and returned
backup/manifest files are cleaned.

Observed result after the preparation-conflict fix:

```text
Guarded source update, CLI and byte-identical restore: site=Wanaluwawa.SIT, met=Wanaluwawa_Nasa_SYN.MET, rows=48: OK
```

An earlier attempt reported a missing `input-sfi-file` option before simulation.
A subsequent direct `help run-simulation` and fresh adapter capture both showed
that option, and the fresh native run above passed. The transient cause was
not established; the failure is not evidence that the option is unsupported.

This two-day test proves input selection and exact file recovery, not full-year
physical accuracy or GUI round-trip behavior. No binary or DLL analysis was
performed for this change. Vendor inputs and simulation output are not shipped.

## Cross-project variant copy

`pvsyst_clone_variant` accepts optional `source_project`; omitted or null keeps
same-project behavior. Both PRJs must exist in the workspace. The source VC is
copied to the destination project prefix and new VC ID, without changing the
source PRJ/VC or destination PRJ. Optional scoped component replacements retain
the existing behavior. The response includes `source_project`.

This is import-by-copy, not a move, blank physical authoring or automatic source
synchronization. Embedded SIT/MET, component references and any external scene
references are preserved. Referenced dependencies are not copied or rebuilt.
Use `pvsyst_inspect_project` for embedded source differences and availability;
use the existing source update transaction only when intentionally switching
all members to a common source. File operations are not cross-process locked.

The existing scoped-copy test additionally checks cross-project import, source
and destination PRJ preservation, duplicate destination protection, missing
source and escaping names. The existing stdio lifecycle imports a third variant
before its source-update/restore and whole-project archive/restore sequence.
The offline suite remains 179 methods; no hash-only case was added.

Native reproduction uses the existing isolated driver:

```powershell
$env:PVSYST_CLI='C:\Program Files\PVsyst8.1.6\PVsystCLI.exe'
$env:PVSYST_WORKSPACE='C:\Apps\PVsyst8.1_Data'
$env:PVSYST_EDITOR_LAB='C:\Harness\workspace\component-edit-verification'
python tests/smoke_live_capacity.py --mode project_variants
```

The formal MCP tools copy the Hawaii demo PRJ and its VC0 as `MCP_Import`, then
import the California commercial VC0 as `MCP_Import.VC9`. The resulting PRJ/VC0
retain Hawaii references while VC9 retains California references. Inspection
reports both site and meteo differences instead of silently replacing them.
The copied lab intentionally omits original MET files; availability warnings
also appear. No native test of those missing original weather inputs is claimed.

Both source commercial VC0 and imported VC9 are run through CLI 8.1.6 with
identical explicit Wanaluwawa SIT/MET overrides, January 1-2, 1990. Structure
checks pass with 6 inverters, 40 strings and 1000 modules. Each run has 48 finite
hourly rows; all exported EArray/E_Grid/IL_Pmax samples are identical. Hourly-kW
sample sums are 4383.4912 / 4304.7685 / 12.951 for both designs. This establishes
native loading under the destination prefix and retained design behavior with
controlled weather, not independent consumption of differing embedded sources.
The no-runtime-override source-selection test is documented above.

Formal variant archive removes VC9 while VC0 remains; formal archive restore
recovers the imported bytes exactly. The source VC stays unchanged. There was
no native simulation after restore. Evidence, copied inputs, CSVs and the
archive manifest remain outside the repository at
`component-edit-verification/mcp-capacity-0272693767c2/evidence.json`.
This mode consumes two CLI executions. No binary or DLL analysis was performed.

## Content-based project creation

`pvsyst_create_project(project, project_content, variants)` creates a new PRJ and
1..127 VC members from complete supplied UTF-8 text. `variants` is a map from VC
identifiers to full contents; IDs normalize to uppercase and duplicate normalized
IDs stop before writes. Text BOM/newlines and all references are preserved.
No existing project is required; no hidden/default physical design is generated.

Project envelopes and variant envelopes/subarray sections are checked in memory
before the first write. Each member is bounded at 32 MB. The shared new-file
writer now serves both creation and project copying, checking destination members
and limiting the set to 128 PRJ/VC files. It exclusively creates each file and
removes its created files after handled errors. The response includes PRJ hash,
variant IDs and the complete `files` hash map for subsequent archive operations.
Unknown same-prefix sidecars, orphan VCs and existing projects remain conflicts.

This is not a complete schema validator, physical solver, dependency importer,
cross-process lock or crash-atomic multi-file transaction. Referenced SIT/MET,
components and scenes must be available separately. Inspect the created set,
validate structure and simulate before relying on it. Historical comments and
design hints remain exactly as supplied.

The existing project-copy case additionally exercises content-based creation,
BOM/newline preservation, empty input, normalized duplicate IDs, malformed later
VC, escaping ID, overwrite protection and an injected failure writing the second
variant. The failed set leaves no created members and the original source stays
unchanged. The existing stdio lifecycle now creates its initial project through
MCP instead of writing fixture files directly. Offline methods remain 179; no
hash-only test or new framework was added. The result-comparison script's stale
fixed total-tool-count assertion was deleted; its actual comparison checks pass.
The server now has 47 tools across the same six functional groups.

Native reproduction reuses the isolated driver:

```powershell
$env:PVSYST_CLI='C:\Program Files\PVsyst8.1.6\PVsystCLI.exe'
$env:PVSYST_WORKSPACE='C:\Apps\PVsyst8.1_Data'
$env:PVSYST_EDITOR_LAB='C:\Harness\workspace\component-edit-verification'
python tests/smoke_live_capacity.py --mode creation
```

The driver captures full commercial PRJ/VC0 text after its baseline simulation,
then formally archives the source PRJ, VC0 and VC1 out of the copied workspace.
It creates `MCP_Create.PRJ` plus VC0 and VC9 through the new formal MCP tool using
only text payloads. Source VC0 is absent during creation and new-project loading;
no source-project lookup is passed to the tool. Both new VCs preserve the supplied
commercial design byte-for-byte and inspection reports consistent embedded source
references. The lab's omitted California SIT/MET produces availability warnings;
those original weather inputs were not tested.

CLI 8.1.6 loads the new project and VC9 under their new prefix. Baseline and new
runs use identical Wanaluwawa SIT/MET runtime overrides, January 1-2, 1990. Each
has 48 finite hourly samples; EArray/E_Grid/IL_Pmax samples are identical, with
hourly-kW sample sums 4383.4912 / 4304.7685 / 12.951. This proves new-project
loading without source PRJ/VC files and retained design under controlled weather,
not GUI round-trip behavior, full-year accuracy or automatic physical defaults.

Formal VC9 archive/restore succeeds while VC0 remains. Whole new-project
archive/restore recovers its complete creation file map. Source project archive
restore then recovers its members and original VC0 bytes. No native simulation
after final recovery was performed. Retained external evidence:
`component-edit-verification/mcp-capacity-0aa315066aa2/evidence.json`, copied
inputs, before/after CSVs and archive manifests. Vendor workspace files stayed
read-only. This mode consumes two CLI executions; no binary/DLL analysis occurred.

## Variant display-name transaction

[Official variant management](https://www.pvsyst.com/help/project-design/project-definition/variants-system-definitions/variants-management.html)
distinguishes variants within a project. A GUI-saved VC records its display name
as the owning pvVCalcul header Comment; PRJ Comment is a separate project title.
Nested sites, orientations and subarrays also have Comment fields, so a global
text substitution is not a name editor.

The existing parameter tool gains optional strict-string variant_name and the
inspector returns variant_name from the header, null if missing/duplicate there.
The writer requires exactly one owning header Comment in a complete parsed tree;
missing, duplicate and late root names stop without writes or backups. Its
read-only parser view normalizes the pvOrient End of TOrientGroup alias, retaining
line indices; the actual orientation tags and all non-name bytes remain untouched.
The reader stays header-only so inspecting names does not impose new complete-tree
requirements on existing diagnostic fixtures. It does not claim a header value
is the effective native name of a malformed file with duplicate late roots.

Names require printable single-line text with no surrounding whitespace and
1..80 UTF-8 bytes (an explicit adapter bound, not a claimed native universal limit).
Bool/numeric SDK inputs, empty/oversized/control-containing text stop before
writing. The update response adds name_change before/after; existing candidate
preview, no-op, stale guard, backup and formal restore are reused. Display name
may accompany ordinary scalar edits; mode/sharing changes stay separate. This
never renames VC files or IDs, changes project association/title or rewrites
nested names. It adds no mode/model/shading remapping or blank field default.

Two independently authored English/Chinese controls and two fresh SDK writer runs
plus a restored baseline pass CLI 8.1.6: five fully verified runs, each with a
valid scoped structure and 48 finite hourly rows. PDF Variant and UTF-8 CSV
Simulation variant descriptions contain the requested name exactly. All hourly
EArray/E_Grid/IL_Pmax values match the prior unshaded baseline, with totals
73.4057 / 72.0554 / 39.0835 kWh. Both previews equal applied bytes; repeated names
produce no backup; stale updates stop; two formal restores recover source bytes
and the whole PRJ/VC member set. Eight SDK type/text/bound guards preserve files
and backup sets. The original GUI source remains unchanged.

An initial English native run was valid, but its verification harness split CSV
context at the date substring in File date and stopped before the context assertion.
The original MCP_GUI_NAME55 project/output pair is preserved. The fixed harness
uses a line-start date header and new MCP_GUI_NAME55_RETRY / gui_name55_retry
prefixes rather than overwriting the failed attempt. Its two controls and three
writer/restored runs are the five fully verified runs above; the initial attempt
is one additional CLI execution, not an unexplained simulation failure.
Local verify_name55.py, name-controls-round55.json and name-writer-round55.json
retain the independent inputs, diffs, typed diagnostics, CSV context and PDF text.
Existing representative parameter and SDK checks cover mixed scalar/name edits,
missing/nested/duplicate/late-root isolation, controls/UTF-8 byte limits, no-op
and restore. 179 methods pass in 32.843s; SDK lifecycle and compile/diff checks pass.
Fresh installed-CLI capability/license/project inventory smoke also passes.
No new test method/module/dependency/tool: there remain 47 tools in six categories.
A running MCP server needs a restart to expose the optional name argument.
Evidence is CLI 8.1.6 on a pinned two-day grid design, not GUI round-trip, annual
coverage or VC-ID/reorder validation. Project display-name editing and ID-level
rename/reordering remain distinct pending work. No binary/DLL analysis.

## Project title and explicit UTF-8 marker

The [official file-format description](https://www.pvsyst.com/help/getting-started/file-organisation/user-data/format-of-pvsyst-files.html)
identifies UTF-8 international-character support from 6.80 and gives pvProject
Comment as the project heading. It does not establish native BOM-less decoding;
that behavior is verified separately here, rather than inferred from the docs.

The new pvsyst_update_project_parameters tool changes one existing owning header
Comment, not PRJ/VC prefixes, IDs, associations, source snapshots or VC names.
The name shares the variant editor's strict 1..80 printable UTF-8 byte bound;
controls/empty/padded/oversized names and numeric/bool inputs stop without writes.
The common header selector additionally stops at PrintSettings, preventing a
Comment in that non-PVObject block from becoming a missing project's name.
Full object validation stops nested/duplicate/late-root names. Inspection adds
project_name from the raw UTF-8 header (null if absent/duplicate there) and
utf8_bom for the PRJ; it does not assert effective native decoding or resolve
malformed late-root ambiguity.

Project edits require the complete inspected PRJ/VC hash map and reuse full-set
backup manifests, final conflict checks and formal restore_project_sources.
Preview/repeated names produce no backups. The shared commit helper now stages
only changed bytes, retaining unmodified VC bytes, modification times and file
IDs during both apply and recovery. Snapshots and guards still include those VCs:
a sibling's external change during backup preparation stops the name transaction
before PRJ replacement and leaves the external bytes intact. This is optimistic
conflict checking, not cross-process locking or multi-file crash atomicity.

The original GUI PRJ/VC source begins EF BB BF. The earlier independently created
ASCII PRJ has no BOM because its supplied text omitted it; ASCII remains readable.
The first title controls created an ASCII title and a Chinese title in that
BOM-less UTF-8 PRJ. Both native simulations and all numeric comparisons succeed,
but the Chinese PDF/CSV title equals cp1252 reinterpretation of the UTF-8 bytes,
not the requested Unicode text. The initial control assertion correctly failed.
Original C0/C1 files and outputs remain untouched; the revised harness verifies
those exact captured values and adds independent C2 with the identical Chinese
PRJ body plus EF BB BF. C2 produces the exact intended Chinese PDF/CSV title.
No unrelated bytes are used to explain this difference and no runtime decoder
implementation or general platform default is inferred.

Non-ASCII project_name therefore requires an existing UTF-8 BOM or explicit
add_utf8_bom=true. No automatic marker insertion occurs. Adding it changes
UTF-8 decoding of the whole PRJ, not just the title, and is visible as bom_added
in previews/responses even when the name itself is unchanged. Existing BOM and
newline style are retained. Formal recovery restores the original marker state.
This is an explicit adapter contract from CLI 8.1.6 evidence, not a universal
version or locale guarantee. BOM-only same-name behavior has offline transaction
coverage, not an additional dedicated native run.

Three controls (ASCII, observed BOM-less mojibake, correct BOM Chinese), two fresh
SDK writes and one restored baseline consume six native executions, each with
48 finite hourly samples and valid scoped circuit/orientation checks. The five
correct-title cases and the deliberate decoding counterexample retain the same
EArray/E_Grid/IL_Pmax samples, totals 73.4057 / 72.0554 / 39.0835 kWh, and unchanged
First simulation VC name. Two full snapshot restores recover all original bytes;
VC0/VC1 modification times/file IDs are unchanged throughout. Twelve strict/text/
encoding/boolean guards leave files and backup sets intact. The production writer
and independent controls use fresh repository stdio, not cached host tool schemas.

Local verify_project_name56.py, project-name-controls-round56.json and
project-name-writer-round56.json retain inputs, raw/corrected titles, CSV/PDF
text, diffs, guards and recovery. Their round56 prefix is retained while the
encoding follow-up completed during round57. Existing source/parameter checks
cover marker-only transactions, missing/duplicate/late names, PrintSettings
ownership and external-edit protection. A first negative fixture used LF to
match Windows CRLF and failed to construct the intended malformed input; it
now selects the actual owning line with its saved ending. The subsequent full
179-method suite passes in 24.080s; SDK lifecycle, compile and diff checks pass.
Fresh installed-CLI tool inventory/capability/license/project smoke also passes.
No new test method/module/dependency. There are now 48 tools in six categories,
20 in Projects & Variants, with one project-level parameter entrypoint rather
than separate tools for each display/encoding field. Restart the MCP server to
load it. GUI round-trip, broader encoding/locale/version coverage and BOM-less
VC-name behavior remain unverified at this checkpoint; the follow-up below
records the round58 controls. No proprietary binary/DLL analysis occurred.

## Variant name and explicit UTF-8 marker

Two independently authored VC1/VC2 controls have the identical Unicode roof
variant name and UTF-8 body; only the BOM differs. Native CLI 8.1.6 PDF/CSV text
is the intended name with BOM and the exact cp1252-like reinterpretation without
BOM. Both retain 48 finite samples identical to the pinned baseline. The project
title remains Residential DEMO. This proves the prior Unicode VC-name evidence
requires a marked file; a raw UTF-8 inspection alone is not native-decoding proof.

The project and variant name setters now share one small marker-prefix guard.
The existing variant parameter tool adds optional StrictBool add_utf8_bom=False,
requiring variant_name for explicit marker insertion; ordinary non-name edits
preserve their prior marker state. Inspection adds utf8_bom for the VC. Non-ASCII
names need an existing BOM or explicit opt-in. Whole-VC decoding may change, not
just the name; no implicit prefix insertion or legacy ANSI transcoding occurs.
Name results include bom_added, and existing previews/conflict guards/snapshots/
formal variant recovery are reused. No new tool, dependency or test method.

A fresh SDK writer exercises name-plus-marker and same-name-marker-only
transactions from a BOM-less VC0. Previews leave bytes/backups intact. A marker-
only change has empty text diff and equal before/after names but changed=true
and bom_added=true; repeated requests on the marked file are no-ops and create
no backup. Stale snapshots stop before writes. Five no-write guards cover
non-ASCII default insertion, string/numeric flags, marker insertion without a
name and combination with a separate mode switch. Both formal restores recover
all original bytes, including the absent marker; project/sibling bytes,
modification times and file IDs stay intact. Two writes plus two restored
baselines add four native runs to the two controls, with identical hourly values
and totals EArray/E_Grid/IL_Pmax 73.4057 / 72.0554 / 39.0835 kWh.

Existing mixed-parameter tests caught a real implementation regression before
completion: a new byte-prefix local reused the scalar formatter's string prefix
name. Four unittest cases and the SDK mixed-scalar flow failed with TypeError.
The encoding local is now bom_prefix, separate from assignment formatting.
The existing representative name test includes a BOM-less Unicode/angle combined
transaction, and the full suite plus SDK flow are rerun. The isolated native
mixed stage also edits the Chinese name/BOM plus tilt 35.0 -> 35.000 (same physical
value), compares exact independently authored bytes, and validates the unchanged
48-hour results before formal restoration and another native baseline. Those
two additional runs verify the final implementation's formatting branch; this
is not evidence for a different tilt's physical effect, covered elsewhere.

Local verify_variant_bom58.py and variant-bom-controls/writer/mixed-round58.json
retain all eight native runs, five guards and three exact restores. Source vendor
inputs and prior outputs are preserved. Evidence remains the declared 8.1.6
unshaded fixed-plane model loaded by CLI 8.1.6, not universal version/locale or GUI
round-trip support. Broader encoding coverage and GUI name/marker recovery remain
pending. Host MCP restart is needed for the optional flag and inspection field.
The final full 179-method suite passes in 26.131s, SDK component/project/variant/
generator lifecycle passes, and compile/diff checks pass. Fresh installed-CLI
inventory/capability/license/project checks also pass. Tool inventory stays 48
in six categories, including 20 Projects & Variants; no binary/DLL analysis
was performed.

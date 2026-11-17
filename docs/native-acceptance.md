# Repeatable Native Acceptance

The [acceptance CLI](../scripts/native_acceptance.py) separates **comparison of
existing artifacts** from **a newly executed native calculation**. It calls the
public `PVsystCLI` Python adapter and the original software. It neither builds a
replacement physical model nor treats the project's file reader as a physical oracle.
The CLI is checkout tooling; install this checkout's package before using it.

## Case and Reference

Each case supplies a standalone workspace, an independently obtained native CSV,
explicit provenance, and an input-equivalence evidence file. Provenance is recorded
as `caller_declared_not_attested`: a string saying "GUI" does not prove that a GUI
produced the file. Maintain the original GUI/CLI export, exact input declaration,
equipment/weather/version basis, raw capture and acquisition steps outside the
repository when vendor/private data cannot be redistributed. Do not commit private
workspace copies, licenses or workstation-specific paths as permanent test fixtures.

The self-contained external CLI contracts test validator behavior only. They use
synthetic CSVs and explicitly assert zero new native consumption. They are not native
acceptance and do not certify cross-version or GUI/headless physical parity.

A case is UTF-8 JSON with unique keys and schema version 1. Relative file paths resolve
against the case file's directory. `period.end` is exclusive; samples must cover every
hour of every declared day, in the native 1969..2068 date window. For example:

```json
{
  "schema_version": 1,
  "case_id": "fixed-layout-reference",
  "feature": "Explicit layout, native energy and electrical shading loss",
  "native_version": "8.1.7.49382",
  "workspace": "fixtures/PVsyst8.1_Data",
  "fixture_provenance": "Original saved input and reproducible headless declaration; see evidence",
  "input_equivalence": {
    "basis": "Same hardware, weather, period and intended geometry; independently reviewed",
    "evidence": "evidence/input-equivalence.txt"
  },
  "baseline": {
    "csv": "reference/native-gui.csv",
    "producer": "native_gui",
    "version": "8.1.7.49382",
    "provenance": "Original GUI export, with reproducible acquisition record",
    "context": {
      "Project": "Reference.PRJ",
      "Simulation variant": "Reference.VC1",
      "Geographical Site": "Example.SIT",
      "Weather data": "Example.MET"
    }
  },
  "period": {"start": "1990-06-21", "end": "1990-06-23"},
  "columns": {
    "E_Grid": {"unit": "kW", "role": "energy", "abs_tol": 0.0001,
               "rel_tol": 0, "total_abs_tol": 0.001, "total_rel_tol": 0},
    "ShdElec": {"unit": "kW", "role": "loss", "abs_tol": 0.0001,
                "rel_tol": 0, "total_abs_tol": 0.001, "total_rel_tol": 0}
  },
  "request": {"project": "Candidate.PRJ", "variant": "VC0", "sfi_name": "acceptance.sfi"},
  "context": {
    "Project": "Candidate.PRJ",
    "Simulation variant": "Candidate.VC0",
    "Geographical Site": "Example.SIT",
    "Weather data": "Example.MET"
  }
}
```

The tolerance values above are illustrative, not universal engineering limits. Freeze
case-specific tolerances from native export precision and independent acceptance
requirements **before** executing; do not relax them after a mismatch. Declare at
least one `energy` column (W/kW/Wh/kWh) and one `loss` column, with optional auxiliary
columns. No grid-only energy name is required. Disabled native outputs may legitimately
be absent; select an applicable loss output and corresponding baseline explicitly,
rather than replacing missing data with zero. Every selected cell must be finite.
Known strict units must match in both files; no implicit unit conversion rescues a
mismatch. kWh totals are derived from the declared hourly power/energy samples only.

The four context values identify each artifact independently; reference and candidate
project names may differ. Native metadata rows may append dates and descriptions
after the identifying filename. Duplicate context keys, missing selected columns,
gapped or out-of-period timestamps, unequal periods, nonfinite values, unit mismatch,
pointwise differences and aggregate differences fail acceptance. Comparing a file
with itself is rejected. Source/reference identity is checked byte-for-byte, not by
new digests.

## Offline Comparison

```sh
python -m pip install .
python scripts/native_acceptance.py compare case.json --candidate retained/native-result.csv
```

This launches no PVsyst process, queries no license/version/help, and writes no files.
It returns JSON with `status=compared` and exit 0 only when all numeric checks pass.
`native_completed` and `native_consumption_verified` remain false; persistence is
`not_run`. A mismatch returns `failed` and exit 1. Retain stdout and stderr on failure.
Two archived outputs passing comparison is never a claim that this command reran
PVsyst or proved that the input declarations are physically equivalent.

## One Explicit Execution

Before calling `run`, the operator must register a new, nonrefundable request in the
acceptance ledger and freeze the case, provenance, reference, code and budget. This
CLI is not an authorization or global quota service. Failed prechecks and zero-launch
failures still consume the registered request. Every parsed `run` invocation reports
`request_budget_spent=1`, including invalid cases and output-directory collisions;
retain its stdout even when no evidence directory could be created. An invalid
output path is checked before creating it inside a protected source workspace.

```sh
python scripts/native_acceptance.py run case.json \
  --cli "C:/Program Files/PVsyst8.1.7/PVsystCLI.exe" \
  --attempt evidence/new-attempt \
  --attempt-id REGISTERED_REQUEST_ID \
  --max-cli-commands 3
```

The evidence directory must be NEW and separate from the source. Reusing it fails
without overwriting its report. Once the location/case are readable, the CLI writes
and fsyncs its spent-budget record before checking/launching Native. It completely
validates reference context, coverage, units and finite samples before any command.
It verifies the executable's Windows file-version resource without running a version command, rejects
unknown/mismatched versions, and requires an explicit 1..3 adapter-command ceiling.
It archives the exact case, independent reference, equivalence evidence and source
workspace bytes, then makes and verifies a standalone working copy. Links/junctions
in the source are rejected. Originals are rechecked in `finally`, including failures.
No automatic cleanup, rollback, retry or alternate output attempt occurs.

The native path is license preflight, cached option help, then one plain hourly CSV
simulation with shading recomputation. Each launcher attempt spends a command slot
and persists an intent record **before** the unchanged native launcher is called.
Raw returned stdout/stderr (license-identifying lines redacted) and return code are
saved before the adapter interprets them. Timeouts/errors are retained too. Commands
use the existing private-desktop launcher with `allow_window_creation=False`.
Command slots count adapter launch attempts, **not** all descendant OS processes or
internal simulation loops; this CLI does not claim independent OS window observation.

Only fresh output passing the declared context/calendar/finite/unit/tolerance checks
can set `native_consumption_verified=true`. Returned native warnings yield
`needs_review` and nonzero exit, not an unconditional passing result. Source-integrity
failure overrides success; evidence is retained. Native-saved normalization may change
the isolated copy but never permits changing the original source.

## Version and Scope Boundaries

| Executable File Version | Execution Gate | Validation Evidence |
|---|---|---|
| 8.1.6.49278 | Explicit allowlist, exact manifest match | New harness execution NOTRUN |
| 8.1.7.49382 | Explicit allowlist, exact manifest match | Metadata reader checked; new harness execution NOTRUN |
| Any other version, including 8.2 | Stops before native launch | No support inherited from another version |

The allowlist is an execution boundary, not a statement that every feature is accepted
on either version. Baseline and candidate versions are declared separately; preserve
per-feature/per-version results. Missing versions/references are NOTRUN, not passing
matrix cells. Neither allowlisting nor an unknown-version rejection replaces the
later production-wide version policy.

This first gate covers a plain hourly native simulation through the public Python
CLI adapter, not the MCP workflow's own state/confirmation contract. It does not
certify arbitrary model construction, hardware/geometry semantic equivalence, native
database contents, hidden global writes, all system families, GUI independence of
every command, PDF loss diagrams, or saved-result persistence/reload. In particular,
`persistence_acceptance=not_run` stays separate from CSV success. The recovered native
`SaveAs VCi=Y` batch experiment remains a distinct future budgeted gate; it is not
implemented by relabelling an hourly CSV as a saved VC.

The verifier keeps byte snapshots in memory to avoid adding digest checks. Use a
focused standalone acceptance fixture rather than a whole production data library;
streamed byte comparison/archive can replace this when measured fixture size warrants
it. Existing product conflict-token mechanisms are unchanged.

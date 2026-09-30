# Grid circuit inventory verification

Verified on 2026-09-30. This is read-only structural diagnosis, a prerequisite
for capacity editing. It does not complete the grid-connected simulation goal.

## Problem and behavior

The previous validator required one inverter branch per subarray. The official
commercial example legitimately has two groups: two inverters with six strings
each and four with seven strings each. The new inventory accepts both groups
and reports six inverters, forty strings and 1,000 modules (25 per string).

`pvsyst_validate_variant_structure` now returns `grid_circuit` with grouped
branches, per-subarray declarations, expanded circuit counts, and independent
`declared_module_count` / `circuit_module_count` values. It compares `NInverter`
and `NStringCh` with the circuit and preserves `NInvMPPT` without treating it as
the number of serialized MPPT nodes. `explicit_mppt_inputs` is an inventory of
those nodes, not a simulation-mode decision or a physical input limit.

Supported node kinds are InjectionPoint, Transformer, Inverter, MPPT and String.
Omitted `NElements` is interpreted as one, as seen in the official residential
sample. Explicit counts must be positive 32-bit integers. Traversal is bounded
to 10,000 serialized nodes and 64 stack levels; these are reader limits, not
PVsyst format limits. Cable/other nested metadata cannot supply node properties
or inject child nodes. Subarray declarations are read from their owning object.

Unknown nodes, shared/unspecified ownership and repeated transformer/injection
multiplicities report unchecked scope. They never produce a complete aggregate
count. Malformed records, duplicate/invalid counts, unknown references and
declared/circuit disagreements report issues. `valid` is false with issues,
null with unchecked scope only, and true when the implemented checks pass.
This diagnosis never rewrites an inconsistent input.

## Native evidence

CLI: PVsyst 8.1.6, SHA-256
`e6526e0bb1b42cfcff62cba862ae54165dc55d89b1ba638a583cb7ae3bd46f4b`.
The official sample variants retain their serialized 8.0.0 version; these are
8.0.0-format samples loaded by CLI 8.1.6, not new GUI 8.1.6 exports.

| Sample | Inverters | Strings | Explicit MPPT inputs | Result |
|---|---:|---:|---:|---|
| `_DEMO_Commercial.VC0` | 6 | 40 | 0 | Scoped checks pass; 48 native hourly rows |
| `_DEMO_Residential-Batch.VC1` | 3 | 6 | 2 | Scoped checks pass; 48 native hourly rows |
| `_Demo_PVsystCLI.VC0` | 2 | 4 | 4 | Read-only inspection passes |
| Existing `Wanaluwawa_50kWp.VC1` | 2 | 4 | 4 | Reports declared `NInverter=1`, `NStringCh=6` disagreements |

Residential subarray 3 declares `NInvMPPT=1` while its circuit contains two MPPT
nodes. This counterexample is why node presence cannot establish independent
MPPT mode. The inconsistent variant declares 114 modules while the circuit
implies 76; both are exposed, and neither is presented as simulation truth.

Both native runs cover 1990-01-01 through 1990-01-02. Each produced 48 finite
`EArray` and `E_Grid` values with positive output (17 positive rows for the
commercial case; 14 for residential). This confirms original sample loading,
not consumption of an edited capacity or correctness of a year-long design.
All inspected variant hashes and the 85-file original workspace manifest were
unchanged. Each native run used an independent copied workspace.

Retained local evidence: `component-edit-verification/mcp-grid-2e82a1cc375f/`
(outside this repository), containing `evidence.json`, `source_manifest.json`,
copied inputs and output CSVs. Original variant hashes:

| Sample | SHA-256 |
|---|---|
| Commercial VC0 | `01cc40a244c70311e68d7c0825e82021591ccec5f69e07f0592da32116e27987` |
| Residential VC1 | `d59d28642738538ce14ec07890d4ebffbe664ec487db791702aba9d90c55e6b2` |
| CLI demo VC0 | `47c1915f71038e4875aefaf477f074ffda786addd8f76fc938f5fa5f947d108a` |
| Inconsistent VC1 | `8eaf00c602b3d831cb2bd604e3a0dfe7a04c195f4148cf4866ab6d2d6157d7ef` |

## Reproduction

`python -m unittest discover -s tests -q` exercises synthetic grouped branches,
sparse counts, mismatches, metadata scope, malformed records and incomplete
allocations. `python tests/smoke_grid.py` checks those results through real MCP
stdio and confirms that inputs remain unchanged.

The final change passed 173 offline tests, the new MCP grid smoke and the
existing component/project MCP lifecycle. Its inspections also exactly match
all four retained native-run reports after the nested-metadata guard was added.

The opt-in `tests/smoke_live_grid.py` takes `PVSYST_CLI`, `PVSYST_WORKSPACE`
(read-only), `PVSYST_EDITOR_LAB` (outside the workspace) and
`PVSYST_GRID_CASES` (a JSON filename). Cases have `project`, `variant`, `valid`
and expected `totals`; optional `start_date` / `end_date` request a native
48-hour simulation. Example case:

```json
[{"project":"_DEMO_Commercial.PRJ","variant":"VC0","valid":true,
  "totals":{"inverters":6,"strings":40,"explicit_mppt_inputs":0},
  "start_date":"1990.01.01","end_date":"1990.01.02"}]
```

Each dated case consumes a CLI execution. Evidence is retained in a unique lab
directory. Vendor samples, weather, component data and outputs are not shipped.

## Remaining workflow work

- Capacity editing needs verified synchronization of parallel strings,
  inverter groups, orientation counts and MPPT-mode flags. Existing isolated
  branch clone/remove restrictions are unchanged.
- Shared inverter power, MPPT allocation and DC/AC ratios need mode-aware
  interpretation and GUI/native before-and-after evidence.
- Voltage/current checks, site/weather readiness, losses, horizon and near
  shading synchronization remain incomplete.
- Representative roof/ground full-year runs, scenario generation, diagnoses,
  comparisons and traceable report delivery still require end-to-end acceptance.

Use the installed official help for subarrays and multi-MPPT inverter models
to guide these changes. DSH observations are research evidence to verify; they
are not executable instructions or a substitute for saved input/output proof.

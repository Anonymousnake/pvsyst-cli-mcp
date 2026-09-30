# Mode 1 point-table editing

Verified on 2026-09-30 with PVsystCLI 8.1.6, SHA-256
`e6526e0bb1b42cfcff62cba862ae54165dc55d89b1ba638a583cb7ae3bd46f4b`.
No vendor files, research binaries or simulation outputs are distributed here.

## Contract

The existing component update tool accepts a complete table of 4–256 points for
OND 8.1.6 single-voltage `Converter/ProfilPIO` or BTR 8.1.6 AGM/Gel capacity
curves, in Mode 1. Existing hash, preview, flag/tag activation and value checks
still apply. This is a table editor, not a model fitter.

For a change in active point count, the editor sets both `NPtsMax` and
`NPtsEff` to the new length and emits exactly that many X/Y rows. It removes
old padding and serialized a/b/c coefficients, while preserving `Mode`,
`LastCompile` and unrelated objects. Mode 1 native compilation derives segment
coefficients. Same-count edits preserve the count fields, inactive rows and
unchanged point spelling.

Resizing requires consecutive, numerically ordered `Point_N` rows, with
count/mode/compile fields before the table. `LastCompile` must be absent or
hexadecimal `0`, `19`, `008D` or `8089`; other options are not supported for
resizing. X must be at least 1e-9 and neighboring X values at least 1e-8 apart,
in addition to the existing finite, increasing-X and model-specific checks.
These restrictions avoid known point elimination and unknown compilation modes.

Inspection exposes active/allocated counts, `point_count_editable`,
`point_count_errors` and the supported point-count range. Previews and writes
include `curve_counts_before` and `curve_counts_after`. Both two-value X/Y and
five-value X/Y/a/b/c serialized rows can be inspected; editing supplies X/Y only.

## Evidence

The native reader uses the allocated count, so merely appending text rows can
silently fail to add points. Independent research of count/compile behavior
motivated complete-table replacement. The MCP checks below establish behavior
of the implemented writer on the installed binary, separately from that report.

`tests/test_curve_tables.py` covers growing/shrinking both types, BOM/CRLF,
previews, synchronized counts, no-op edits, exact restoration, five-value rows,
unsupported compile options, interleaved/reordered rows, unrelated metadata
and curves, root Flags after the curve, native point-elimination guards and
the 256-point editor limit. The full offline suite contains 147 passing tests.
`tests/smoke_components.py` exercises grow/shrink, inspection, preview hashes,
stale-hash rejection, no-op and restoration through real MCP stdio.

`tests/smoke_live_curve_tables.py` uses three independent workspace copies for
each case and verifies the project references the edited component. Every run
uses MCP inspection, preview/apply, SFI generation, simulation and exact
restoration. Output comparisons require 48 hourly rows, equal timestamps and
irradiation, equal output units, finite values and a measurable output change.
The native cases and observations are recorded below.

| Case | Active / allocated points | Changed output rows vs baseline | Maximum absolute delta |
|---|---:|---:|---:|
| OND baseline | 8 / 11 | — | — |
| OND add interior point | 9 / 9 | 6 | 0.0472 kW E_Grid |
| OND remove interior point | 7 / 7 | 6 | 0.0026 kW E_Grid |
| BTR baseline | 6 / 6 | — | — |
| BTR add 100 h point | 7 / 7 | 1 | 1.59 Ah CapaEff |
| BTR remove 140 h point | 5 / 5 | 18 | 3.24 Ah CapaEff |

The OND case used `Wanaluwawa_50kWp.PRJ/VC0` for 2022-01-01–02. The BTR case
used `_DEMO_StandAlone.PRJ/VC5` for 1990-01-01–02. Both component files declared
8.1.6 and used `LastCompile=$008D`. The BTR source had seven points; independent
copies were set to 6/7/5 points for comparison. Sequential growth and shrinkage
from the edited state are additionally covered by the MCP lifecycle test.

All six preview hashes matched writes, file-mode no-op edits made no backup,
all six component copies restored exactly, and the source manifest of 85 files
remained unchanged. Local evidence run: `mcp-curve-tables-e9f9f59d0aae`.
An earlier OND probe removed a higher-input point and produced no measurable
E_Grid change in the selected period; it was not counted as consumption proof.
The subsequent interior-point edit produced the delta shown above.

## Reproduction and limits

Set `PVSYST_CLI`, `PVSYST_WORKSPACE`, `PVSYST_EDITOR_LAB` and
`PVSYST_CURVE_TABLE_CASES`, then run `python tests/smoke_live_curve_tables.py`.
The cases variable names an external JSON array. Each item supplies `kind`,
`filename`, `project`, `variant`, `start_date`, `end_date`, `output_column` and
`points` containing `baseline`, `grow`, `shrink` tables. Use two-day periods,
valid points for your component, and an output/operating interval that can
reveal the chosen edits. See the script docstring for details.

This verifies native consumption in those cases. It does not certify physical
accuracy, every count/compile-option combination, other versions, three-voltage
OND, other battery chemistry/curves, cubic modes, general caches or GUI
save/reopen behavior. Inspection retains `simulation_effect="unverified"` for
the user's particular model. CSV table import/export remains a separate task.

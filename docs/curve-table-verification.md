# Mode1 point-table editing

The component update tool accepts complete4..256 point tables for OND8.1.6 single-voltage Converter/ProfilPIO and BTR8.1.6 AGM/Gel capacity curves in Mode1. Existing source guards, preview, activation and value checks apply. This is a table editor, not a physical fitter or general curve compiler.

## Point counts and rows

A changed active count sets NPtsMax and NPtsEff to the new length and emits exactly that many X/Y rows, removing old padding and serialized a/b/c coefficients. Mode, LastCompile and unrelated objects remain. Native Mode1 compilation produces segment coefficients. Same-count edits preserve count fields, inactive rows and unchanged point spelling.

Resizing requires consecutive numerically ordered Point_N rows, with count/mode/compile fields before the table. LastCompile must be absent or hexadecimal0,19,008D,8089. Other compilation modes stop. X is at least1e-9, neighboring X separation at least1e-8, in addition to finite increasing X and model-specific checks; no point elimination is silently accepted.

Inspection exposes allocated/active counts, point_count_editable, point_count_errors and the supported range. Updates report curve_counts_before/after. Both X/Y and X/Y/a/b/c stored rows can be read; editing supplies only X/Y. Appending text rows without synchronizing allocated count is not a supported way to add points.

## CSV transport

`pvsyst_get_component(include_curve_csv=true)` returns CSV contents for complete main OND/BTR capacity curves with known axes. `pvsyst_update_component(curve_csv=...)` accepts one such table, mutually exclusive with curve_updates. Use explicit-unit headers, comma-separated decimal values,4..256 points and at most64KiB UTF-8 text. Parsed values use the same guarded curve path; export does not expand edit support.

Files are selected/saved by the client; tools exchange CSV text. Unknown axes are omitted with reasons, not exported under guessed units. See[OND activation](ond-curve-verification.md) and[BTR capacity](btr-capacity-verification.md).

Native consumption is bounded to supported models and operating conditions, not all compile-option/count combinations, other chemistries, three-voltage OND, nonlinear modes, general caches or GUI save/reopen. Inspection retains simulation_effect=unverified for the particular file; simulate its referencing design after editing.

# OND file-curve editing

The component editor supports the existing main Converter/ProfilPIO in text Version8.1.6 OND files, Mode1 and single voltage. X/Y are input/output power in watts, independent of GUI display units. Supply the complete supported point table; see[point counts and CSV](curve-table-verification.md).

## File mode

Root Flags bit4(0x10) selects the automatic profile. Editing point ordinates alone while automatic mode is active need not affect native output. Explicit use_file_curve=true selects the supported file-curve path, clearing only that bit. Three-voltage bit12(0x1000) remains outside the editor.

Other root/nested flags and unrelated fields stay unchanged. Same-count edits preserve padding/counts; resizing synchronizes the full table. Current-source checks, preview, no-op, backup and restore use the normal component transaction.

## Interpretation

Curve consumption does not establish consistency of every derived scalar or every operating regime. A changed curve can change operating points and limiting losses; aggregate energy is not a direct efficiency metric or engineering approval. Do not infer a general output multiplier from ordinate scaling.

Other versions, three-voltage interpolation, automatic physical refitting and full GUI resave behavior remain separate. The returned simulation_effect stays unverified for the user's model; verify the actual referencing project and original native units/results after editing.

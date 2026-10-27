# Native DC cabling bridge

## Supported entry

`pvsyst_create_native_dc_cabling_variant` operates an already open Chinese PVsyst8.1.6rev49278 GUI at DPI96. It uses the detailed-loss window, resistance/STC-percent controls, native confirmation and SaveAs. PVsyst calculates coupled subarray resistance/percent and StringNode resistance; the adapter does not implement cable or loss equations.

Open the requested project with DataPath equal to the MCP workspace. The tool does not start or exit PVsyst, edit INI, use the clipboard, load another project or directly patch DC records. Preview is read-only; execution requires both `dry_run=false` and `confirm=true`. Use one GUI driver/server and leave the desktop untouched during execution. The shared in-process lock is not a cross-process desktop lock.

The source and candidate must be native8.1.6, one ordinary fixed-plane/subarray PV-only Grid, with complete circuit counts, inactive AC and no MV/HV, load, storage, geometry, horizon or P50. Only uniform direct StringNode resistances consistent with the subarray's stored0.001mOhm summary are accepted. Custom, mixed or partial states stop. DC mode flags are019E1002 for resistance and019E1003 for percent; accepting both here does not widen other editors'019E1002 domain.

## Input and persistence

`dc` contains `mode: resistance|percent` and strict numeric `value`. Resistance is in mOhm; percent is STC loss percent. Values must be finite, nonnegative, exactly representable at0.01 precision and at most20 formatted characters; percent must not exceed100. These are adapter constraints, not manufacturer limits. Readback preserves native decimal strings and higher-precision circuit resistance.

`variant_name` is a display title, not a requested VC id. Native SaveAs chooses the id; use the actual path/id returned. Source PRJ/VC snapshots must remain unchanged before input, before save and after save.

After asynchronously written candidate contents are ready, the adapter verifies mode, requested value, owner consistency, title and non-DC physical records. Only owning DC subarray flag/Ch/PC, direct StringNode resistances and display comments/BOM may differ. Native saves may add a UTF-8 BOM or re-encode an existing unmarked non-ASCII display comment; physical lines must remain equal after whitespace normalization. The adapter does not rewrite source comments externally.

## Failure and recovery

A failed native action may leave its modal open for inspection. If SaveAs created a file, the response retains `variant_created=true` and its path even if postchecks stop. Failed artifacts are not silently deleted. Inspect the actual GUI and saved file before choosing a fresh target; saved state is not simulation-consumption certification.

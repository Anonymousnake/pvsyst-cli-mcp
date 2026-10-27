# BTR capacity curves

The component editor supports an existing root capacity curve in Version8.1.6 BTR files using Pb_Sealed_AGM or Pb_Sealed_Gel and Mode1. CapaCourant is the recognized CLI tag. A GUI Capa_DischRate alias requires explicit use_file_curve=true with replacement points; the editor does not silently rename it.

## Inputs

X is discharge duration in hours and Y a capacity multiplier relative to C10. Require positive X/Y, increasing X, nondecreasing Y, more than three active points and a range covering100h. For these supported technologies, native checking uses Y(100h) in the inclusive range[1.15,1.45]. Optional bt technology prefixes are accepted; space-separated aliases are not interchangeable.

The editor does not extrapolate an incomplete range, rescale X or invent a replacement curve. Other chemistry and nonlinear modes remain outside its scope. Same-count edits preserve inactive rows/counts; resizing follows[the complete table contract](curve-table-verification.md),4..256 points and exact supported compilation modes.

## Activation and recovery

Only the selected tag and supported points change; flags, chemistry and unrelated curves remain. Use the actual current fingerprint, preview, backup and guarded restore. Native consumption controls for this path are limited to48h, not ageing, full-year physical behavior, chemistry conversion or BMS/electrical compatibility.

cli_recognition indicates known tags, editable indicates structural replacement support, and value_errors reports remaining value defects. Tag recognition or an acceptable C100/C10 ratio does not prove complete model consistency. simulation_effect stays unverified for each inspected file; run the native referencing design after editing. CLI-specific tag support does not establish a full GUI round trip.

# DC cabling GUI bridge (`pvsyst_create_native_dc_cabling_variant`)

In PVsyst 8.1.6, changing DC wiring resistance (`mOhm`) or STC wiring loss (`%`) updates coupled fields across the subarray (`RSCablage`, loss fraction, and mode flags) and every `StringNode` in the circuit tree. Instead of reimplementing PVsyst's internal wire-sizing math in Python, `pvsyst_create_native_dc_cabling_variant` drives an already open PVsyst 8.1.6 GUI session, enters your target resistance or loss percentage in the Detailed Losses dialog, lets PVsyst recalculate all coupled fields natively, and saves the result as a new variant via `SaveAs`.

## Prerequisites

- **Desktop environment**: PVsyst 8.1.6 rev49278 in Chinese locale at 96 DPI (100% display scaling), with the target project already open in the GUI and `PVsyst.ini` `DataPath` pointing to the MCP workspace.
- **Template layout**: Single-subarray, single-fixed-plane unshaded PV-only grid variant (`8.1.6`) with complete circuit counts, uniform direct `StringNode` resistances matching the subarray summary, and no active AC cabling, MV/HV transformers, horizon, P50, load, or storage.
- **Subarray DC mode flags**: Supports switching freely between resistance mode (`Flags=$019E1002`) and STC loss-percentage mode (`Flags=$019E1003`).

## Parameters and behavior

Pass `dc` with `mode` and `value`:
- `mode`: `"resistance"` (in `mOhm`) or `"percent"` (STC loss in `%`, up to `100`).
- `value`: Non-negative finite number with at most 2 decimal places (`0.01` precision) and at most 20 formatted characters.
- `variant_name`: Display title (`Comment`) for the new variant. Note that PVsyst's native `SaveAs` dialog assigns the next free `.VCx` filename automatically; the tool returns the actual created `variant` ID and file path.
- `dry_run` / `confirm`: `dry_run=true` validates the open project and variant state without touching the GUI; executing requires both `dry_run=false` and `confirm=true`. Leave the mouse and keyboard untouched while the GUI bridge is running.

## Post-save verification

Once PVsyst finishes writing the new `.VCx` file, the bridge re-parses both the source and new variant files to verify that:
- The source `.PRJ` and `.VCx` bytes were not modified.
- The new `.VCx` has the expected DC mode flag (`$019E1002` or `$019E1003`), updated subarray DC resistance/percentage fields, and updated `StringNode` resistances.
- All non-DC physical blocks remain identical to the source variant (accounting for PVsyst GUI adding a UTF-8 BOM or normalizing whitespace on save).

If a GUI dialog check fails after `SaveAs` has already written the file, the response still reports `variant_created=true` and the new file path so you can inspect the saved `.VCx` and the open GUI window before retrying.

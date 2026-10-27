# Controlled native project opening

`pvsyst_open_native_project(project, dry_run=true, confirm=false)` opens a requested project through Chinese PVsyst8.1.6rev49278 at DPI96. Native INI DataPath must already equal the MCP workspace. The tool does not change INI, VC records or clipboard, and does not close or force-terminate processes.

## Preview and confirmed launch

Preview reads project/version/process/window state only. A cold launch requires both `dry_run=false` and `confirm=true`; without a process, preview or unconfirmed requests return ready_to_open or confirmation_required. Execution launches the GUI beside the configured CLI, navigates only the actual Popen PID, uses the native chooser and verifies the exact loaded basename plus source PRJ preservation. First-row/filter navigation alone is not proof of loading the requested project.

Toolhelp32 inventory includes background and starting instances. Inventory errors, unreadable same-name executable paths, another installation or multiple instances stop before launch. Unsupported window classes, metrics or modal states stop without blind clicks. Use one GUI driver/server and leave the desktop untouched; the workspace RLock is not an OS-wide desktop lock.

## Existing sessions and failures

An existing matching enabled project returns `already_open`, `owned=false`, `existing_session_read_only=true`, `automation_ready=false` and `existing_loaded_path_verified=false`. It is not activated, navigated, reloaded, edited or closed. Matching caption and current INI do not prove an earlier loaded path or saved in-memory work. Other or ambiguous sessions remain untouched.

Confirmed cold launch returns `owned=true` and its actual PID. A failure preserves PID, stage and action history for inspection rather than killing the process or discarding work. `opened` means project loading only, not house import, geometry validity, SaveAs or simulation consumption.

The [house-task controller](native-house-task.md) separately manages its owned GUI process, native stages and checkpoints. Its complete workflow is not supplied by the project opener alone; native one/two-region and fixed-unshaded design boundaries remain separate.

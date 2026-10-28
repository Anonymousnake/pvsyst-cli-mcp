# Controlled native GUI project session (`pvsyst_open_native_project`)

`pvsyst_open_native_project(project, dry_run=true, confirm=false)` launches a clean PVsyst 8.1.6 rev49278 GUI session (Chinese locale, 96 DPI) and loads the requested `.PRJ` project from the configured MCP workspace.

## Prerequisites and safety checks

Before launching PVsyst, the tool inspects running Windows processes (via `Toolhelp32` so background or starting instances are not missed) and checks `PVsyst.ini`:
- `PVsyst.ini` `DataPath` must already point to the MCP workspace (the tool never edits `PVsyst.ini`).
- If multiple PVsyst instances or an executable from a different installation directory are running, the tool aborts before launching.
- **Dry-run vs confirmed launch**: `dry_run=true` (or `confirm=false`) only inspects the project and running window state and returns `ready_to_open` or `confirmation_required`. Starting a new GUI process requires both `dry_run=false` and `confirm=true`.

## How cold launch and existing sessions are handled

- **Cold launch (no PVsyst window running)**: Spawns `PVsyst.exe` from the directory alongside `PVsystCLI.exe`, drives only that owned `Popen` PID through the Grid project chooser and file filter, verifies that the loaded project basename matches `project` and that the source `.PRJ` bytes on disk were not modified, and returns `owned=true` with the new `pid`.
- **Existing session with the same project open**: If a PVsyst window is already open with a matching title, the tool does **not** take it over or click inside it (since a window title alone cannot prove there are no unsaved edits in memory). Instead, it returns `already_open`, `owned=false`, `existing_session_read_only=true`, and `automation_ready=false`.
- **Failure handling**: If an unexpected modal dialog or window state appears during launch, the tool leaves the process open and returns the PID, stage, and action log so you can inspect the window on the desktop; it never force-kills `PVsyst.exe`.

For the complete residential workflow that manages its own GUI session, imports a `.pvc2` scene, saves the variant, closes the owned GUI process, and runs a full-year CLI simulation, see [native house task](native-house-task.md).

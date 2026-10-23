# Controlled Native Project Opening

## Public Entry

`pvsyst_open_native_project(project, dry_run=true, confirm=false)` removes the manual native project-opening step for the recorded Chinese PVsyst8.1.6rev49278/DPI96 path. The configured native INI DataPath must already equal the MCP workspace. The library never changes INI, private VC data or the clipboard, and it does not close or force-terminate any process. It reuses the existing Win32 helpers, source/path validation, component lock and the already proved native Grid/Load/filter/confirm flow; no desktop framework or replacement PV calculation is introduced.

Preview reads project/version/process/window state only. A cold start requires `dry_run=false` AND `confirm=true`; without a process it returns `ready_to_open` or `confirmation_required` before launching. Execution launches the GUI next to the configured CLI, navigates only that actual Popen PID, uses the native project chooser and verifies the exact final project basename and unchanged source PRJ. First-row/filter navigation is never accepted on its own as proof that the requested project loaded.

Native process inventory uses Toolhelp32, including starting/background instances with no form yet. Inventory failures, unreadable same-name executable paths, another PVsyst installation or multiple instances stop before launch. Coordination is the existing server/workspace RLock, not an OS-wide mutex: use one GUI driver/server and leave the desktop alone during confirmed operations. Unsupported window classes/metrics/modal states stop rather than blindly clicking or dismissing a warning.

## Existing Work

An already open matching enabled project returns `already_open`, `owned=false`, `existing_session_read_only=true`, `automation_ready=false` and `existing_loaded_path_verified=false`. No activation, variant selection, reload, navigation or close is performed. Matching caption plus current INI is not proof of the path loaded earlier in an existing process, nor proof that its in-memory work is saved. This return does not hand the window to an automated editor. Other/busy/ambiguous sessions remain untouched.

A confirmed cold launch returns `owned=true` and its actual PID. An execution failure preserves that PID/stage/action history for inspection; it is not silently killed and no unobserved work is discarded. `opened` certifies only project loading, not house import, geometric validity, native SaveAs or simulation consumption. The separately accepted [house-task controller](native-house-task.md) retains its actual process ownership, concentrates the GUI stages and handles checkpoints/cleanup; that complete task is not claimed by this opener alone.

## Acceptance

The public non-hash `test_native_project_boundary.py` contract checks the exact schema, strict booleans/unknown keys, path/missing-project stops, failed preflight without native actions and unchanged source files, using an explicit fixture workspace and a non-executable CLI placeholder. Absent fixture data skips rather than certifies execution. No full discovery, old smoke or hash test is part of this phase.

One parent-controlled public GUI batch created a separate `HOUSE_NATIVE_SESSION.PRJ/VC0` from complete declared reference data, then exercised:

- Cold preview and unconfirmed execution with no GUI process created.
- Confirmed cold start and native loading of the exact project, with source PRJ preserved.
- Matching-project preview and execution returning read-only state with no GUI actions.
- A different-project request blocked while preserving the current caption/session.
- A changed, unconfirmed native DC loss modal: project opening stopped without changing the edit text; only the parent cancelled its own benchmark edit afterward.

Source and new PRJ/VC bytes stayed unchanged through the native load tests. The parent closed only the process returned as its new owned launch and waited on its original process handle; it exited normally. Only the parent benchmark temporarily selected/restored DataPath, preserving unrelated native INI updates. The public library performs neither operation. The proof is the local `pvsyst-house-delivery-study/native-session-r1/public-open.json`; commercial inputs and runtime artifacts are not committed.

The final conservative same-name/other-installation stop and explicit existing-path/automation disclosure were added after that batch; these do not broaden successful operation. No live second-installation experiment is claimed. Full house-task orchestration, durable checkpoints and source/inheritance disclosure now have [separate acceptance](native-house-task.md), retaining the existing one/two-region native house bounds and the old fixed-unshaded design tool's separate domain.

"""Setup & License MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

import re

from pydantic import StrictBool, StrictStr
from pvsyst_auto.api.context import categorized_tool, cli


@categorized_tool()
def pvsyst_capabilities(command: StrictStr | None = None) -> dict:
    """List commands and option names actually available in the installed CLI (or one named command)."""
    cmd: str | None = None
    if command is not None:
        cmd = command.strip()
        if not cmd or not re.fullmatch(r"[a-z][a-z-]+", cmd):
            raise ValueError("command must be a valid PVsystCLI subcommand name (e.g. 'run-simulation')")
    current = cli()
    commands = current.capabilities() if cmd is None else current.capabilities(cmd)
    return {"version": current.version(), "commands": commands}

@categorized_tool()
def pvsyst_license_info() -> dict:
    """Read CLI license state and quota without returning keys or Host ID."""
    return cli().license_info()

@categorized_tool()
def pvsyst_license_activate(key: str, confirm: StrictBool = False) -> dict:
    """Activate a CLI license using a supplied key (requires confirm=true)."""
    return cli().license_activate(key, confirm=confirm)

@categorized_tool()
def pvsyst_license_deactivate(customer_id: str, confirm: StrictBool = False) -> dict:
    """Deactivate this computer's CLI license (requires confirm=true)."""
    return cli().license_deactivate(customer_id, confirm=confirm)

@categorized_tool()
def pvsyst_license_sync(confirm: StrictBool = False) -> dict:
    """Synchronize CLI license information with its server (requires confirm=true)."""
    return cli().license_sync(confirm=confirm)

@categorized_tool()
def pvsyst_export_logs() -> dict:
    """Export CLI diagnostic logs to a ZIP archive under workspace/Results."""
    root = cli().workspace.resolve(strict=True)
    folder = (root / "Results").resolve()
    if root not in folder.parents:
        raise ValueError("Results folder resolves outside the workspace")
    return {"zip": str(cli().export_logs(folder))}

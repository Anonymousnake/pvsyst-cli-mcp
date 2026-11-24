"""Compose selected tool groups and launch the stdio server."""
from importlib import import_module

from .context import (ACTIVE_TOOL_GROUPS, ACTIVE_TOOL_NAMES,
                      _strict_arguments, create_mcp_server, import_modules_for_groups, mcp)

for module in import_modules_for_groups(ACTIVE_TOOL_GROUPS):
    import_module(f".{module}", __package__)

if set(_strict_arguments) != ACTIVE_TOOL_NAMES:
    raise RuntimeError(f"Unregistered strict entry points: {sorted(ACTIVE_TOOL_NAMES - set(_strict_arguments))}")


def main():
    mcp.run(transport="stdio")

"""Compose selected tool groups and launch the stdio server."""
from importlib import import_module

from .context import (ACTIVE_TOOL_GROUPS, ACTIVE_TOOL_NAMES, STRICT_ARGUMENT_TOOLS,
                      _strict_arguments, create_mcp_server, import_modules_for_groups, mcp)

for module in import_modules_for_groups(ACTIVE_TOOL_GROUPS):
    import_module(f".{module}", __package__)

_expected_strict = set(STRICT_ARGUMENT_TOOLS) & ACTIVE_TOOL_NAMES
if set(_strict_arguments) != _expected_strict:
    raise RuntimeError(f"Unregistered strict entry points: {sorted(_expected_strict - set(_strict_arguments))}")


def main():
    mcp.run(transport="stdio")

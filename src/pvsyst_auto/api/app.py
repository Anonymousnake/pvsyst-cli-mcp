"""Compose the eight tool groups and launch the stdio server."""
from .context import mcp, STRICT_ARGUMENT_TOOLS, _strict_arguments
from . import setup, projects, components, weather, storage, simulation, results, engineering

if set(_strict_arguments) != set(STRICT_ARGUMENT_TOOLS):
    raise RuntimeError(f"Unregistered strict entry points: {sorted(set(STRICT_ARGUMENT_TOOLS) - set(_strict_arguments))}")

def main():
    mcp.run(transport="stdio")

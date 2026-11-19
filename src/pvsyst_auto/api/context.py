"""Shared MCP registration, strict arguments and workspace-scoped clients."""
from __future__ import annotations

import inspect
import os
from collections.abc import Mapping
from importlib import import_module
from threading import Lock
from typing import Any
from functools import wraps
from pathlib import Path
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pvsyst_auto.native.cli import PVsystCLI, PVsystError
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.project.paths import workspace_file
from pvsyst_auto.api.guidance import SERVER_INSTRUCTIONS, register_guidance


mcp = MCPServer("pvsyst-cli", instructions=SERVER_INSTRUCTIONS)
register_guidance(mcp)

_cli: PVsystCLI | None = None
_cli_key: tuple[str | None, str | None] | None = None
_cli_owner: PVsystCLI | None = None

_cli_init_lock = Lock()

TOOL_GROUPS = {
    "Setup & License": ("pvsyst_capabilities", "pvsyst_license_info"),
    "Admin & Diagnostics": (
        "pvsyst_license_activate", "pvsyst_license_deactivate",
        "pvsyst_license_sync", "pvsyst_export_logs",
    ),
    "Projects & Variants": (
        "pvsyst_list_projects", "pvsyst_inspect_project", "pvsyst_prepare_design_project",
        "pvsyst_clone_variant", "pvsyst_get_variant_parameters", "pvsyst_update_variant_parameters",
        "pvsyst_create_tracking_variant", "pvsyst_create_shed_geometry_variant",
        "pvsyst_create_ac_cabling_variant", "pvsyst_create_dc_cabling_variant",
    ),
    "Components": (
        "pvsyst_list_components", "pvsyst_get_component",
        "pvsyst_create_component", "pvsyst_update_component", "pvsyst_build_sourced_component",
    ),
    "Sites & Weather": (
        "pvsyst_resolve_location", "pvsyst_acquire_weather",
        "pvsyst_prepare_weather", "pvsyst_review_weather",
    ),
    "Loads & Storage": (
        "pvsyst_prepare_load_profile", "pvsyst_create_native_load_variant",
        "pvsyst_create_grid_storage_variant", "pvsyst_create_grid_storage_system",
    ),
    "Simulation": (
        "pvsyst_build_sfi", "pvsyst_run_simulation", "pvsyst_simulate_design",
        "pvsyst_prepare_sourced_design", "pvsyst_run_house_task",
    ),
    "Results": ("pvsyst_read_results", "pvsyst_read_rows", "pvsyst_read_batch_results", "pvsyst_diagnose_results"),
    "Engineering": (
        "pvsyst_suggest_sizing", "pvsyst_project_handoff",
        "pvsyst_run_engineering_task", "pvsyst_compare_engineering_scenarios",
        "pvsyst_export_engineering_delivery",
    ),
    "Expert & Low-Level": (
        "pvsyst_list_variants", "pvsyst_create_project", "pvsyst_update_project_parameters",
        "pvsyst_update_project_sources", "pvsyst_get_variant_components",
        "pvsyst_update_variant_components", "pvsyst_validate_variant_structure",
        "pvsyst_clone_subarray", "pvsyst_remove_subarray", "pvsyst_review_project_inputs",
        "pvsyst_create_mv_night_disconnect_variant", "pvsyst_create_dome_allocation_variant",
        "pvsyst_create_house_pv_scene", "pvsyst_read_house_pv_scene", "pvsyst_import_house_pv_scene",
        "pvsyst_validate_component", "pvsyst_compare_components", "pvsyst_project_components",
        "pvsyst_inspect_weather_csv", "pvsyst_build_monthly_weather",
        "pvsyst_create_site", "pvsyst_convert_meteo",
        "pvsyst_run_sourced_house_task",
        "pvsyst_prepare_block_project", "pvsyst_review_engineering_brief",
    ),
}

TOOL_CATEGORY = {name: category for category, names in TOOL_GROUPS.items() for name in names}

if len(TOOL_CATEGORY) != sum(map(len, TOOL_GROUPS.values())):
    raise RuntimeError("Duplicate tool category assignment")

# Every tool rejects unknown arguments: a misspelt option must never be dropped before a write.
STRICT_ARGUMENT_TOOLS = tuple(TOOL_CATEGORY)

GROUP_MODULES = {
    "setup": "Setup & License",
    "projects": "Projects & Variants",
    "components": "Components",
    "weather": "Sites & Weather",
    "storage": "Loads & Storage",
    "simulation": "Simulation",
    "results": "Results",
    "engineering": "Engineering",
    "admin": "Admin & Diagnostics",
    "expert": "Expert & Low-Level",
}
DEFAULT_TOOL_GROUPS = (
    "setup", "projects", "components", "weather",
    "storage", "simulation", "results", "engineering",
)
GROUP_IMPORT_MODULES = {
    "setup": ("setup",),
    "admin": ("setup",),
    "projects": ("projects",),
    "components": ("components",),
    "weather": ("weather",),
    "storage": ("storage",),
    "simulation": ("simulation",),
    "results": ("results",),
    "engineering": ("engineering",),
    "expert": ("projects", "components", "weather", "simulation", "engineering"),
}


def resolve_tool_groups(requested: str | None) -> tuple[str, ...]:
    if requested is None:
        return DEFAULT_TOOL_GROUPS
    groups = [group.strip() for group in requested.split(",")]
    if groups == ["all"]:
        return tuple(GROUP_MODULES)
    if (any(group not in GROUP_MODULES for group in groups)
            or len(set(groups)) != len(groups)):
        raise ValueError("PVSYST_TOOL_GROUPS requires 'all' or distinct nonempty keys from: "
                         + ", ".join(GROUP_MODULES))
    return tuple(group for group in GROUP_MODULES if group in groups)


def tool_names_for_groups(groups: tuple[str, ...]) -> frozenset[str]:
    return frozenset(name for group in groups for name in TOOL_GROUPS[GROUP_MODULES[group]])


def import_modules_for_groups(groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(mod for group in groups for mod in GROUP_IMPORT_MODULES[group]))


ACTIVE_TOOL_GROUPS = resolve_tool_groups(os.environ.get("PVSYST_TOOL_GROUPS"))
ACTIVE_TOOL_NAMES = tool_names_for_groups(ACTIVE_TOOL_GROUPS)

_strict_arguments: dict[str, frozenset[str]] = {}
_tool_registry: dict[str, tuple[str, Any, frozenset[str]]] = {}


def make_strict_middleware(strict_map: Mapping[str, frozenset[str]]):
    async def reject_unknown_tool_arguments(ctx: ServerRequestContext[Any, Any],
                                            call_next: CallNext) -> HandlerResult:
        """Refuse unknown top-level arguments for every registered tool."""
        if ctx.method == "tools/call" and isinstance(ctx.params, Mapping):
            name = ctx.params.get("name")
            arguments = ctx.params.get("arguments")
            # A malformed name/arguments value is left to the SDK's own params validation.
            accepted = strict_map.get(name) if isinstance(name, str) else None
            if accepted is not None and isinstance(arguments, Mapping):
                unknown = sorted(set(arguments) - accepted, key=str)
                if unknown:
                    text = (f"Unknown argument(s) for {name}: {', '.join(map(str, unknown))}. "
                            f"Accepted arguments: {', '.join(sorted(accepted))}")
                    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)
        return await call_next(ctx)

    return reject_unknown_tool_arguments


reject_unknown_tool_arguments = make_strict_middleware(_strict_arguments)
mcp.middleware.append(reject_unknown_tool_arguments)

# Failures whose message is returned verbatim, as before.
_VERBATIM_ERRORS = (ValueError, FileNotFoundError, FileExistsError, PVsystError)
# Everything mapped to a ToolError. Any other OSError (a CSV/PDF locked by another program,
# a full disk, a launcher failure) would otherwise reach the client as the SDK's bare
# "Error executing tool" with its message dropped.
_REPORTED_ERRORS = (ValueError, OSError, PVsystError)


def _tool_error(exc: Exception) -> ToolError:
    if isinstance(exc, _VERBATIM_ERRORS):
        return ToolError(str(exc))
    return ToolError(f"{type(exc).__name__}: {exc}")


def categorized_tool():
    """Label MCP descriptions without changing established wire-level tool names."""
    def register(func):
        category = TOOL_CATEGORY[func.__name__]
        params = frozenset(inspect.signature(func).parameters)
        @wraps(func)
        def guarded(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except _REPORTED_ERRORS as exc:
                raise _tool_error(exc) from None
        description = f"[{category}] {inspect.getdoc(func)}"
        _tool_registry[func.__name__] = (description, guarded, params)
        if func.__name__ in ACTIVE_TOOL_NAMES:
            if func.__name__ in STRICT_ARGUMENT_TOOLS:
                # Typos are rejected against the published signature, so the whitelist cannot drift.
                _strict_arguments[func.__name__] = params
            mcp.tool(description=description)(guarded)
        return func
    return register


def create_mcp_server(tool_groups: str | None = None) -> MCPServer:
    """Create an isolated MCPServer populated for the requested PVSYST_TOOL_GROUPS spec."""
    groups = resolve_tool_groups(tool_groups)
    names = tool_names_for_groups(groups)
    for mod in import_modules_for_groups(groups):
        import_module(f".{mod}", __package__)
    missing = sorted((set(STRICT_ARGUMENT_TOOLS) & names) - set(_tool_registry))
    if missing:
        raise RuntimeError(f"Unregistered strict entry points: {missing}")
    server = MCPServer("pvsyst-cli", instructions=SERVER_INSTRUCTIONS)
    register_guidance(server)
    strict_map: dict[str, frozenset[str]] = {}
    server.middleware.append(make_strict_middleware(strict_map))
    for name in sorted(names):
        description, guarded, params = _tool_registry[name]
        if name in STRICT_ARGUMENT_TOOLS:
            strict_map[name] = params
        server.tool(description=description)(guarded)
    return server

def cli() -> PVsystCLI:
    global _cli, _cli_key, _cli_owner
    with _cli_init_lock:
        key = (os.environ.get("PVSYST_CLI"), os.environ.get("PVSYST_WORKSPACE"))
        if _cli is None or (_cli is _cli_owner and _cli_key != key):
            _cli = _cli_owner = None
            _cli_key = None
            _cli = _cli_owner = PVsystCLI(*key)
            _cli_key = key
        return _cli

def components() -> ComponentStore:
    current = cli()
    configured = os.environ.get("PVSYST_BUILTIN_COMPONENTS")
    if configured:
        builtin = Path(configured).resolve(strict=True)
        if not builtin.is_dir() or builtin.name != "ComposPV":
            raise ValueError("PVSYST_BUILTIN_COMPONENTS must be a ComposPV directory")
    else:
        data_ro = current.cli.parent / "DataRO"
        candidates = (sorted(path / "ComposPV" for path in data_ro.iterdir()
                             if path.is_dir() and (path / "ComposPV").is_dir())
                      if data_ro.is_dir() else [])
        if len(candidates) > 1:
            raise ValueError("Multiple DataRO component libraries; set PVSYST_BUILTIN_COMPONENTS")
        builtin = candidates[0] if candidates else None
    return ComponentStore(current.workspace, builtin, current._lock)

def component_call(method: str, *args, **kwargs) -> dict:
    try:
        return getattr(components(), method)(*args, **kwargs)
    except _REPORTED_ERRORS as exc:
        raise _tool_error(exc) from None

def variant_store() -> VariantStore:
    store = components()
    return VariantStore(store.workspace, store, store.lock)

def variant_call(method: str, *args, **kwargs) -> dict:
    try:
        return getattr(variant_store(), method)(*args, **kwargs)
    except _REPORTED_ERRORS as exc:
        raise _tool_error(exc) from None

def workspace_path(folder: str, filename: str, extension: str) -> Path:
    """Allow a filename only within a named directory of the configured workspace."""
    return workspace_file(cli().workspace, folder, filename, extension)

def optional_path(folder: str, filename: str, extension: str) -> Path | None:
    return workspace_path(folder, filename, extension) if filename else None

def simulation_csv_path(filename: str) -> Path | None:
    """Accept a bare single-run output name while preserving workspace checks."""
    if not filename:
        return None
    if filename not in (".", "..") and not filename.endswith(".") and not Path(filename).suffix:
        filename += ".csv"
    return workspace_path("Results", filename, ".csv")

def result_path(folder: str, csv_name: str) -> Path:
    if folder not in ("Results", "UserHourly"):
        raise ValueError("Results folder must be Results or UserHourly")
    return workspace_path(folder, csv_name, ".csv")

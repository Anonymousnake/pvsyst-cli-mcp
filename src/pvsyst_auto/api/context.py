"""Shared MCP registration, strict arguments and workspace-scoped clients."""
from __future__ import annotations

import inspect
import os
from collections.abc import Mapping
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


mcp = MCPServer("pvsyst-cli")

_cli: PVsystCLI | None = None

_cli_init_lock = Lock()

TOOL_GROUPS = {
    "Setup & License": (
        "pvsyst_capabilities", "pvsyst_license_info", "pvsyst_license_activate",
        "pvsyst_license_deactivate", "pvsyst_license_sync", "pvsyst_export_logs",
    ),
    "Projects & Variants": (
        "pvsyst_list_projects", "pvsyst_list_variants", "pvsyst_inspect_project", "pvsyst_prepare_design_project", "pvsyst_review_project_inputs",
        "pvsyst_create_project", "pvsyst_clone_project", "pvsyst_update_project_parameters", "pvsyst_update_project_sources",
        "pvsyst_restore_project_sources", "pvsyst_archive_project", "pvsyst_archive_variant",
        "pvsyst_restore_project_archive", "pvsyst_get_variant_components",
        "pvsyst_clone_variant", "pvsyst_update_variant_components",
        "pvsyst_get_variant_parameters", "pvsyst_update_variant_parameters",
        "pvsyst_validate_variant_structure", "pvsyst_clone_subarray",
        "pvsyst_remove_subarray", "pvsyst_restore_variant", "pvsyst_create_tracking_variant",
        "pvsyst_create_dome_allocation_variant", "pvsyst_create_house_pv_scene",
        "pvsyst_read_house_pv_scene", "pvsyst_import_house_pv_scene",
        "pvsyst_create_shed_geometry_variant", "pvsyst_create_mv_night_disconnect_variant", "pvsyst_create_ac_cabling_variant", "pvsyst_create_native_dc_cabling_variant", "pvsyst_open_native_project",
    ),
    "Components": (
        "pvsyst_list_components", "pvsyst_get_component", "pvsyst_validate_component",
        "pvsyst_create_component", "pvsyst_copy_component", "pvsyst_clone_component",
        "pvsyst_update_component", "pvsyst_backup_component", "pvsyst_restore_component",
        "pvsyst_archive_component", "pvsyst_restore_archived_component",
        "pvsyst_compare_components", "pvsyst_project_components", "pvsyst_build_sourced_component",
    ),
    "Sites & Weather": (
        "pvsyst_build_monthly_weather", "pvsyst_create_site", "pvsyst_convert_meteo",
        "pvsyst_inspect_weather_csv", "pvsyst_prepare_weather", "pvsyst_resolve_location",
        "pvsyst_acquire_weather", "pvsyst_review_weather",
    ),
    "Loads & Storage": ("pvsyst_prepare_load_profile", "pvsyst_create_load_variant",
                        "pvsyst_create_native_load_variant",
                        "pvsyst_create_peak_shaving_variant",
                        "pvsyst_create_grid_storage_variant", "pvsyst_create_grid_storage_system"),
    "Simulation": ("pvsyst_build_sfi", "pvsyst_run_simulation", "pvsyst_simulate_template",
                   "pvsyst_simulate_design", "pvsyst_prepare_sourced_design", "pvsyst_run_house_task", "pvsyst_run_sourced_house_task"),
    "Results": ("pvsyst_read_results", "pvsyst_read_rows", "pvsyst_read_batch_results"),
    "Engineering": ("pvsyst_prepare_block_project", "pvsyst_review_engineering_brief", "pvsyst_project_handoff", "pvsyst_run_engineering_task",
                    "pvsyst_compare_engineering_scenarios", "pvsyst_export_engineering_delivery"),
}

TOOL_CATEGORY = {name: category for category, names in TOOL_GROUPS.items() for name in names}

if len(TOOL_CATEGORY) != sum(map(len, TOOL_GROUPS.values())):
    raise RuntimeError("Duplicate tool category assignment")

STRICT_ARGUMENT_TOOLS = ("pvsyst_prepare_weather", "pvsyst_resolve_location",
                         "pvsyst_acquire_weather", "pvsyst_review_weather",
                         "pvsyst_simulate_template", "pvsyst_simulate_design", "pvsyst_run_house_task", "pvsyst_run_sourced_house_task", "pvsyst_prepare_design_project", "pvsyst_review_project_inputs",
                         "pvsyst_prepare_sourced_design", "pvsyst_build_sourced_component",
                          "pvsyst_prepare_load_profile", "pvsyst_create_load_variant",
                          "pvsyst_create_native_load_variant",
                          "pvsyst_create_tracking_variant",
                           "pvsyst_create_shed_geometry_variant", "pvsyst_create_mv_night_disconnect_variant", "pvsyst_create_ac_cabling_variant", "pvsyst_create_native_dc_cabling_variant", "pvsyst_open_native_project",
                          "pvsyst_create_dome_allocation_variant", "pvsyst_read_house_pv_scene", "pvsyst_import_house_pv_scene",
                          "pvsyst_create_house_pv_scene",
                          "pvsyst_create_peak_shaving_variant",
                          "pvsyst_create_grid_storage_variant", "pvsyst_create_grid_storage_system",
                          "pvsyst_prepare_block_project", "pvsyst_review_engineering_brief", "pvsyst_project_handoff", "pvsyst_run_engineering_task",
                          "pvsyst_compare_engineering_scenarios", "pvsyst_export_engineering_delivery")

GROUP_MODULES = {
    "setup": "Setup & License", "projects": "Projects & Variants",
    "components": "Components", "weather": "Sites & Weather",
    "storage": "Loads & Storage", "simulation": "Simulation",
    "results": "Results", "engineering": "Engineering",
}
_requested_groups = os.environ.get("PVSYST_TOOL_GROUPS")
if _requested_groups is None:
    ACTIVE_TOOL_GROUPS = tuple(GROUP_MODULES)
else:
    _groups = [group.strip() for group in _requested_groups.split(",")]
    if (any(group not in GROUP_MODULES for group in _groups)
            or len(set(_groups)) != len(_groups)):
        raise ValueError("PVSYST_TOOL_GROUPS requires distinct nonempty keys from: "
                         + ", ".join(GROUP_MODULES))
    ACTIVE_TOOL_GROUPS = tuple(group for group in GROUP_MODULES if group in _groups)
ACTIVE_TOOL_NAMES = frozenset(name for group in ACTIVE_TOOL_GROUPS
                              for name in TOOL_GROUPS[GROUP_MODULES[group]])

_strict_arguments: dict[str, frozenset[str]] = {}

async def reject_unknown_tool_arguments(ctx: ServerRequestContext[Any, Any],
                                        call_next: CallNext) -> HandlerResult:
    """Refuse unknown top-level arguments for the strict entry points only."""
    if ctx.method == "tools/call" and isinstance(ctx.params, Mapping):
        name = ctx.params.get("name")
        arguments = ctx.params.get("arguments")
        # A malformed name/arguments value is left to the SDK's own params validation.
        accepted = _strict_arguments.get(name) if isinstance(name, str) else None
        if accepted is not None and isinstance(arguments, Mapping):
            unknown = sorted(set(arguments) - accepted, key=str)
            if unknown:
                text = (f"Unknown argument(s) for {name}: {', '.join(map(str, unknown))}. "
                        f"Accepted arguments: {', '.join(sorted(accepted))}")
                return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)
    return await call_next(ctx)

mcp.middleware.append(reject_unknown_tool_arguments)

def categorized_tool():
    """Label MCP descriptions without changing established wire-level tool names."""
    def register(func):
        category = TOOL_CATEGORY[func.__name__]
        if func.__name__ not in ACTIVE_TOOL_NAMES:
            return func
        if func.__name__ in STRICT_ARGUMENT_TOOLS:
            # Typos are rejected against the published signature, so the whitelist cannot drift.
            _strict_arguments[func.__name__] = frozenset(inspect.signature(func).parameters)
        @wraps(func)
        def guarded(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except (ValueError, FileNotFoundError, FileExistsError, PVsystError) as exc:
                raise ToolError(str(exc)) from None
        mcp.tool(description=f"[{category}] {inspect.getdoc(func)}")(guarded)
        return func
    return register

def cli() -> PVsystCLI:
    global _cli
    with _cli_init_lock:
        if _cli is None:
            _cli = PVsystCLI(os.environ.get("PVSYST_CLI"), os.environ.get("PVSYST_WORKSPACE"))
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
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        raise ToolError(str(exc)) from None

def variant_call(method: str, *args, **kwargs) -> dict:
    try:
        store = components()
        return getattr(VariantStore(store.workspace, store, store.lock), method)(*args, **kwargs)
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        raise ToolError(str(exc)) from None

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

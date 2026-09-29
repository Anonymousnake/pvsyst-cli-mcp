"""Workspace-scoped stdio MCP bridge for PVsystCLI 8.0/8.1 (SDK 2.x).

Set PVSYST_CLI and PVSYST_WORKSPACE in the client's MCP server environment.
Only supported CLI options discovered from the installed binary are used.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from pvsyst_cli import (PVsystCLI, VAR_GROUPS, build_monthly_weather_csv,
                        build_sfi, iter_result_csv, parse_batch_results, result_units)
from pvsyst_components import ComponentStore
from pvsyst_variants import VariantStore

mcp = MCPServer("pvsyst-cli")
_cli: PVsystCLI | None = None


def cli() -> PVsystCLI:
    global _cli
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
    if (not filename or filename in (".", "..") or Path(filename).name != filename
            or "\\" in filename or "/" in filename
            or not filename.lower().endswith(extension)):
        raise ValueError(f"Expected a {extension} filename without directories")
    root = cli().workspace.resolve(strict=True)
    parent = (root / folder).resolve()
    if parent != root and root not in parent.parents:
        raise ValueError("Workspace folder resolves outside the workspace")
    target = (parent / filename).resolve()
    if target.parent != parent:
        raise ValueError("File resolves outside its workspace folder")
    return target


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


@mcp.tool()
def pvsyst_capabilities() -> dict:
    """List commands and option names actually available in the installed CLI."""
    return {"version": cli().version(), "commands": cli().capabilities()}


@mcp.tool()
def pvsyst_license_info() -> dict:
    """Read CLI license state and quota without returning keys or Host ID."""
    return cli().license_info()


@mcp.tool()
def pvsyst_license_activate(key: str, confirm: bool = False) -> dict:
    """Activate a CLI license using a supplied key. Requires confirm=true;
    MCP clients may retain tool arguments, so handle license keys accordingly."""
    return cli().license_activate(key, confirm=confirm)


@mcp.tool()
def pvsyst_license_deactivate(customer_id: str, confirm: bool = False) -> dict:
    """Deactivate this computer's CLI license. Requires confirm=true."""
    return cli().license_deactivate(customer_id, confirm=confirm)


@mcp.tool()
def pvsyst_license_sync(confirm: bool = False) -> dict:
    """Synchronize CLI license information with its server. Requires confirm=true."""
    return cli().license_sync(confirm=confirm)


@mcp.tool()
def pvsyst_list_projects() -> list[str]:
    """List PRJ filenames in the configured workspace."""
    return cli().list_projects()


@mcp.tool()
def pvsyst_list_variants(project: str) -> list[str]:
    """List variant IDs belonging to an existing project."""
    return cli().list_variants(project)


@mcp.tool()
def pvsyst_get_variant_components(project: str, variant: str) -> dict:
    """Read component references in a variant and its current SHA-256 for guarded edits."""
    return variant_call("inspect", project, variant)


@mcp.tool()
def pvsyst_clone_variant(project: str, source_variant: str, new_variant: str,
                         updates: dict[str, str] | None = None, subarray_id: int = 1) -> dict:
    """Copy an existing variant as a NEW VC file. Optional PAN/OND edits target one
    SubArrayId; BTR/GEN edits target the existing pvSystem reference only."""
    return variant_call("clone", project, source_variant, new_variant, updates, subarray_id)


@mcp.tool()
def pvsyst_update_variant_components(project: str, variant: str,
                                     updates: dict[str, str], expected_sha256: str,
                                     subarray_id: int = 1) -> dict:
    """Replace existing PAN/OND/BTR/GEN references only, with backup and SHA guard.
    Use pvsyst_get_variant_components to obtain the current expected_sha256."""
    return variant_call("update", project, variant, updates, expected_sha256, subarray_id)


@mcp.tool()
def pvsyst_restore_variant(project: str, variant: str, backup_name: str,
                           expected_sha256: str, confirm: bool = False) -> dict:
    """Restore a variant snapshot after verifying its current hash; backs up the
    pre-restore state and requires confirm=true."""
    return variant_call("restore", project, variant, backup_name, expected_sha256, confirm)


@mcp.tool()
def pvsyst_list_components(component_type: str, library: str = "workspace",
                           query: str = "", offset: int = 0, limit: int = 100) -> dict:
    """List PAN, OND, BTR or GEN loose files in workspace or installed DataRO.
    The encrypted built-in component database is not enumerable."""
    return component_call("list", component_type, library, query, offset, limit)


@mcp.tool()
def pvsyst_get_component(component_type: str, filename: str,
                         library: str = "workspace", offset: int = 0,
                         limit: int = 100) -> dict:
    """Read paged UTF-8 component text and fields; legacy binary PAN is metadata only."""
    return component_call("inspect", component_type, filename, library, offset, limit)


@mcp.tool()
def pvsyst_validate_component(component_type: str, filename: str,
                              library: str = "workspace") -> dict:
    """Check format and known fields; only a simulation verifies PVsyst model behavior."""
    return component_call("validate", component_type, filename, library)


@mcp.tool()
def pvsyst_copy_component(component_type: str, source_name: str, new_name: str,
                          source_library: str = "workspace") -> dict:
    """Copy a PAN/OND/BTR/GEN into the workspace without overwrite. Legacy PAN
    bytes are preserved. Source library is workspace or read-only builtin."""
    return component_call("copy", component_type, source_name, new_name, source_library)


@mcp.tool()
def pvsyst_create_component(component_type: str, filename: str, content: str) -> dict:
    """Create a NEW PAN/OND/BTR/GEN from complete UTF-8 PVObject_ text in the
    workspace without a template. Checks structure, required fields and known
    hazardous numeric values; only a referencing simulation verifies physics.
    Legacy binary PAN must be imported with pvsyst_copy_component instead."""
    return component_call("create", component_type, filename, content)


@mcp.tool()
def pvsyst_clone_component(component_type: str, source_name: str, new_name: str,
                           updates: dict[str, str], source_library: str = "workspace") -> dict:
    """Create a new text component from a complete existing template. New
    Manufacturer and Model are required; only verified scalar fields are editable.
    Run a simulation with a variant referencing the new component to verify it."""
    return component_call("clone", component_type, source_name, new_name, updates, source_library)


@mcp.tool()
def pvsyst_update_component(component_type: str, filename: str,
                            updates: dict[str, str]) -> dict:
    """Edit allowlisted text fields in one workspace component with automatic
    backup. Legacy PAN is read-only; BTR curve fields are not editable."""
    return component_call("update", component_type, filename, updates)


@mcp.tool()
def pvsyst_backup_component(component_type: str, filename: str) -> dict:
    """Save a byte-identical backup under workspace/ComposPV/.mcp-backups."""
    return component_call("backup", component_type, filename)


@mcp.tool()
def pvsyst_restore_component(component_type: str, filename: str,
                             backup_name: str, confirm: bool = False) -> dict:
    """Restore a matching backup to a workspace component; confirm=true required.
    The current file is itself backed up before replacement."""
    return component_call("restore", component_type, filename, backup_name, confirm)


@mcp.tool()
def pvsyst_archive_component(component_type: str, filename: str,
                             confirm: bool = False) -> dict:
    """Reversibly remove an unreferenced workspace component to .mcp-archive.
    Requires confirm=true; rejects references from workspace PRJ/VC files."""
    return component_call("archive", component_type, filename, confirm)


@mcp.tool()
def pvsyst_restore_archived_component(component_type: str, filename: str,
                                      archive_name: str, confirm: bool = False) -> dict:
    """Restore an archived component as a NEW file, preserving the archived
    snapshot; requires confirm=true and never overwrites existing files."""
    return component_call("restore_archive", component_type, filename, archive_name, confirm)


@mcp.tool()
def pvsyst_compare_components(component_type: str, first: str, second: str,
                              second_library: str = "workspace") -> dict:
    """Compare two same-type components: bounded text diff or binary PAN hashes."""
    return component_call("compare", component_type, first, second, second_library)


@mcp.tool()
def pvsyst_project_components(project: str, variant: str) -> dict:
    """Inspect PRJ/VC references to PAN, OND, BTR and GEN. Unresolved loose files
    may still exist inside the encrypted built-in database."""
    return component_call("dependencies", project, variant)


@mcp.tool()
def pvsyst_build_sfi(filename: str, variables: str = "all_common") -> dict:
    """Create a NEW hourly export definition in workspace/Models."""
    target = workspace_path("Models", filename, ".sfi")
    names = (VAR_GROUPS[variables] if variables in VAR_GROUPS
             else [part.strip() for part in variables.split(",")])
    build_sfi(target, list(names))
    return {"sfi": str(target), "variables": list(names)}


@mcp.tool()
def pvsyst_build_monthly_weather(filename: str, global_h: list[float],
                                 temperature: list[float],
                                 diffuse_h: list[float] | None = None,
                                 wind_velocity: list[float] | None = None) -> dict:
    """Create the 14-row monthly weather CSV for create-site in workspace/Meteo."""
    target = workspace_path("Meteo", filename, ".csv")
    build_monthly_weather_csv(target, global_h, temperature, diffuse_h, wind_velocity)
    return {"weather_csv": str(target)}


@mcp.tool()
def pvsyst_create_site(site_name: str, latitude: float, longitude: float,
                       weather_csv_name: str, sit_name: str,
                       altitude: float | None = None, timezone: float | None = None,
                       country_code: str = "", region: str = "", source: str = "",
                       albedo: float | None = None, log_level: int | None = None) -> dict:
    """Use CLI create-site to create a NEW SIT in workspace/Sites (8.1+).
    Stage or generate monthly weather CSV in workspace/Meteo. If altitude,
    timezone or country are omitted, the CLI may query an external service."""
    weather = workspace_path("Meteo", weather_csv_name, ".csv")
    target = workspace_path("Sites", sit_name, ".sit")
    output = cli().create_site(site_name, latitude, longitude, weather, target,
                               altitude=altitude, timezone=timezone,
                               country_code=country_code, region=region, source=source,
                               albedo=albedo, log_level=log_level)
    return {"site": str(output)}


@mcp.tool()
def pvsyst_run_simulation(project: str, variant: str,
                          sfi_name: str = "", csv_name: str = "", pdf_name: str = "",
                          met_name: str = "", rvt_name: str = "",
                          params_name: str = "", batch_params_name: str = "",
                          batch_rvt_name: str = "", site_name: str = "",
                          synthetic_weather: bool = False,
                          time_step: str = "", start_date: str = "", end_date: str = "",
                          report_language: str = "", report_pages: str = "",
                          recompute_shading: bool | None = None,
                          log_level: int | None = None) -> dict:
    """Run an existing project/variant. Regular CSV/PDF go to Results.
    For a single run, csv_name accepts a bare filename (e.g. run01) or a .csv
    filename (run01.csv); both create Results/run01.csv. Existing outputs are
    never overwritten. For batch_params_name, leave csv_name/pdf_name empty:
    the CLI generates UserBatch/UserHourly outputs automatically.
    Optional inputs live in Models (SFI,RVT), Meteo (MET), UserData (DAT),
    UserBatch (batch CSV/RVT), or Sites (SIT). Unsupported options fail early.
    Each run can consume a license execution."""
    license_state = cli().license_info()
    if license_state.get("status") == "TRIAL" and license_state.get("remaining_executions") == 0:
        raise ValueError("PVsystCLI reports no remaining trial executions")
    return cli().run_simulation(
        project, variant,
        sfi=optional_path("Models", sfi_name, ".sfi"),
        out_csv=simulation_csv_path(csv_name),
        report_pdf=optional_path("Results", pdf_name, ".pdf"),
        met=optional_path("Meteo", met_name, ".met"),
        rvt=optional_path("Models", rvt_name, ".rvt"),
        params=optional_path("UserData", params_name, ".dat"),
        batch_params=optional_path("UserBatch", batch_params_name, ".csv"),
        batch_rvt=optional_path("UserBatch", batch_rvt_name, ".rvt"),
        site=optional_path("Sites", site_name, ".sit"),
        synthetic_weather=synthetic_weather, time_step=time_step or None,
        start_date=start_date or None, end_date=end_date or None,
        report_language=report_language or None,
        report_pages=[p.strip() for p in report_pages.split(",")] if report_pages else None,
        recompute_shading=recompute_shading, log_level=log_level,
    )


@mcp.tool()
def pvsyst_convert_meteo(csv_name: str, mef_name: str, sit_name: str,
                         met_name: str, timeshift: int = 0,
                         log_level: int | None = None) -> dict:
    """Convert a staged CSV + MEF in Meteo and SIT in Sites to a NEW MET."""
    source = workspace_path("Meteo", csv_name, ".csv")
    mef = workspace_path("Meteo", mef_name, ".mef")
    sit = workspace_path("Sites", sit_name, ".sit")
    target = workspace_path("Meteo", met_name, ".met")
    return {"met": str(cli().convert_meteo(source, mef, sit, target, timeshift,
                                            log_level=log_level))}


@mcp.tool()
def pvsyst_export_logs() -> dict:
    """Export CLI diagnostic logs to a ZIP under workspace/Results.
    The archive may contain private data; only its path is returned."""
    root = cli().workspace.resolve(strict=True)
    folder = (root / "Results").resolve()
    if root not in folder.parents:
        raise ValueError("Results folder resolves outside the workspace")
    return {"zip": str(cli().export_logs(folder))}


@mcp.tool()
def pvsyst_read_results(csv_name: str, columns: str = "E_Grid,PR",
                        folder: str = "Results") -> dict:
    """Summarize hourly or subhour SFI rows in Results or UserHourly.
    Energy is integrated only for a regular cadence and E_Grid/EArray power."""
    path = result_path(folder, csv_name)
    requested = [column.strip() for column in columns.split(",") if column.strip()]
    indices = None
    stats = {name: {"samples": 0, "sum": 0.0, "max": -math.inf} for name in requested}
    count = 0
    first = last = previous = step_minutes = None
    regular = True
    for headers, row in iter_result_csv(path):
        if indices is None:
            if not requested or any(name not in headers[1:] for name in requested):
                raise ValueError(f"Choose columns from {headers[1:]}")
            indices = {name: headers.index(name) for name in requested}
            first = str(row[0])
        if previous is not None:
            delta = (row[0] - previous).total_seconds() / 60
            if delta <= 0 or (step_minutes is not None and delta != step_minutes):
                regular = False
            elif step_minutes is None:
                step_minutes = delta
        previous = row[0]
        count += 1
        last = str(row[0])
        for name, index in indices.items():
            value = row[index]
            if math.isfinite(value):
                stats[name]["samples"] += 1
                stats[name]["sum"] += value
                stats[name]["max"] = max(stats[name]["max"], value)
    step = step_minutes if regular else None
    units = result_units(path)
    summary = {}
    for name, state in stats.items():
        if state["samples"]:
            unit = units.get(name, "")
            energy = None
            if name in ("E_Grid", "EArray") and step and unit in ("W", "kW"):
                energy = state["sum"] * step / 60 * (0.001 if unit == "W" else 1)
            summary[name] = {**state, "mean": state["sum"] / state["samples"],
                             "unit": unit, "energy_kwh": energy}
    return {"columns": headers, "rows": count, "first": first, "last": last,
            "step_minutes": step, "summary": summary}


@mcp.tool()
def pvsyst_read_rows(csv_name: str, columns: str = "E_Grid,PR",
                     offset: int = 0, limit: int = 100,
                     folder: str = "Results") -> dict:
    """Read a bounded page from Results or batch UserHourly CSVs."""
    if offset < 0 or not 1 <= limit <= 500:
        raise ValueError("Offset must be >= 0 and limit between 1 and 500")
    selected = [s.strip() for s in columns.split(",") if s.strip()]
    indices = None
    total = 0
    page: list[list] = []
    path = result_path(folder, csv_name)
    for headers, row in iter_result_csv(path):
        if indices is None:
            if not selected or any(name not in headers[1:] for name in selected):
                raise ValueError(f"Choose columns from {headers[1:]}")
            indices = [headers.index(name) for name in selected]
        if offset <= total < offset + limit:
            page.append([str(row[0]), *(row[i] if math.isfinite(row[i]) else None
                                        for i in indices)])
        total += 1
    units = result_units(path)
    return {"total": total, "offset": offset, "columns": ["date", *selected],
            "units": {name: units.get(name, "") for name in selected},
            "rows": page}


@mcp.tool()
def pvsyst_read_batch_results(summary_name: str,
                              columns: str = "E_Grid,PR") -> dict:
    """Read SIM_* scenario values from a UserBatch/*Results.CSV summary."""
    if not summary_name.lower().endswith("results.csv"):
        raise ValueError("Expected a batch Results.CSV filename")
    target = workspace_path("UserBatch", summary_name, ".csv")
    requested = tuple(s.strip() for s in columns.split(",") if s.strip())
    if not requested:
        raise ValueError("Select at least one batch result column")
    return parse_batch_results(target, requested)


if __name__ == "__main__":
    mcp.run(transport="stdio")

"""Workspace-scoped stdio MCP bridge for PVsystCLI 8.0/8.1 (SDK 2.x).

Set PVSYST_CLI and PVSYST_WORKSPACE in the client's MCP server environment.
Only supported CLI options discovered from the installed binary are used.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from pvsyst_cli import (PVsystCLI, VAR_GROUPS, build_monthly_weather_csv,
                        build_sfi, iter_result_csv, parse_batch_results, result_units)

mcp = MCPServer("pvsyst-cli")
_cli: PVsystCLI | None = None


def cli() -> PVsystCLI:
    global _cli
    if _cli is None:
        _cli = PVsystCLI(os.environ.get("PVSYST_CLI"), os.environ.get("PVSYST_WORKSPACE"))
    return _cli


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

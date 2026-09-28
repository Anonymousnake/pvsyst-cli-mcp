"""Local stdio MCP bridge to PVsystCLI 8.0.6 (MCP Python SDK 2.x).

Configure PVSYST_CLI and PVSYST_WORKSPACE in your MCP client's server env.
The server exposes only typed, workspace-scoped operations; it never exposes
arbitrary CLI commands or the machine's Host ID.
"""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from pvsyst_cli import PVsystCLI, VAR_GROUPS, build_sfi, parse_result_csv

mcp = MCPServer("pvsyst-cli")
_cli: PVsystCLI | None = None


def cli() -> PVsystCLI:
    global _cli
    if _cli is None:
        _cli = PVsystCLI(os.environ.get("PVSYST_CLI"), os.environ.get("PVSYST_WORKSPACE"))
    return _cli


def workspace_path(folder: str, filename: str, extension: str) -> Path:
    """Keep tool-controlled file access inside one named workspace directory."""
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


@mcp.tool()
def pvsyst_license_info() -> dict:
    """Read PVsystCLI license status and remaining quota, without Host ID."""
    return cli().license_info()


@mcp.tool()
def pvsyst_list_projects() -> list[str]:
    """List existing PRJ filenames in the configured PVsyst workspace."""
    return cli().list_projects()


@mcp.tool()
def pvsyst_list_variants(project: str) -> list[str]:
    """List existing variant IDs for an existing PRJ filename."""
    return cli().list_variants(project)


@mcp.tool()
def pvsyst_build_sfi(filename: str, variables: str = "all_common") -> dict:
    """Create a new hourly SFI in workspace/Models; select a verified group
    (energy, inverter_losses, all_common) or comma-separated vi* identifiers.
    This operation does not consume a simulation execution."""
    target = workspace_path("Models", filename, ".sfi")
    names = (VAR_GROUPS[variables] if variables in VAR_GROUPS
             else [part.strip() for part in variables.split(",")])
    build_sfi(target, list(names))
    return {"sfi": str(target), "variables": list(names)}


@mcp.tool()
def pvsyst_run_simulation(project: str, variant: str, sfi_name: str,
                          csv_name: str, pdf_name: str = "") -> dict:
    """Run a pre-existing project variant with a workspace/Models SFI.
    Write a NEW CSV (and optionally PDF) to workspace/Results. A successful
    run consumes the CLI license's simulation execution quota."""
    sfi = workspace_path("Models", sfi_name, ".sfi")
    out_csv = workspace_path("Results", csv_name, ".csv")
    report_pdf = workspace_path("Results", pdf_name, ".pdf") if pdf_name else None
    if not sfi.is_file():
        raise ValueError("SFI does not exist; create it first")
    return cli().run_simulation(project, variant, sfi=sfi, out_csv=out_csv,
                                report_pdf=report_pdf)


@mcp.tool()
def pvsyst_convert_meteo(csv_name: str, mef_name: str, sit_name: str,
                         met_name: str, timeshift: int = 0) -> dict:
    """Convert staged workspace/Meteo CSV+MEF and workspace/Sites SIT into
    a NEW workspace/Meteo MET file. This may consume a CLI execution."""
    source = workspace_path("Meteo", csv_name, ".csv")
    mef = workspace_path("Meteo", mef_name, ".mef")
    sit = workspace_path("Sites", sit_name, ".sit")
    target = workspace_path("Meteo", met_name, ".met")
    result = cli().convert_meteo(source, mef, sit, target, timeshift)
    return {"met": str(result)}


@mcp.tool()
def pvsyst_read_results(csv_name: str, columns: str = "E_Grid,PR") -> dict:
    """Summarize specified columns of a workspace/Results CSV. Numeric `sum`
    is a sample sum; for hourly kW data its unit is kWh."""
    target = workspace_path("Results", csv_name, ".csv")
    headers, rows = parse_result_csv(target)
    requested = [column.strip() for column in columns.split(",") if column.strip()]
    if not requested or any(column not in headers[1:] for column in requested):
        raise ValueError(f"Choose columns from {headers[1:]}")
    summary = {}
    for column in requested:
        values = [row[headers.index(column)] for row in rows]
        import math
        finite = [value for value in values if math.isfinite(value)]
        if finite:
            summary[column] = {"samples": len(finite), "sum": sum(finite),
                               "mean": sum(finite) / len(finite), "max": max(finite)}
    return {"columns": headers, "rows": len(rows), "first": str(rows[0][0]),
            "last": str(rows[-1][0]), "summary": summary}


if __name__ == "__main__":
    mcp.run(transport="stdio")

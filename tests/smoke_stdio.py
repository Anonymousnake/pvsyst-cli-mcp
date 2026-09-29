"""Optional live MCP check: read-only unless PVSYST_SMOKE_RUN_* is set.

Set PVSYST_CLI and PVSYST_WORKSPACE first, then:
    python tests/smoke_stdio.py
"""
import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main():
    for name in ("PVSYST_CLI", "PVSYST_WORKSPACE"):
        if not os.environ.get(name):
            raise SystemExit(f"Set {name} before running this smoke check")
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve().parents[1] / "pvsyst_mcp_server.py")],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [tool.name for tool in tools.tools]
            print("Tools:", ", ".join(names))
            capability_result = await session.call_tool("pvsyst_capabilities", {})
            license_result = await session.call_tool("pvsyst_license_info", {})
            project_result = await session.call_tool("pvsyst_list_projects", {})
            if any(r.is_error for r in (capability_result, license_result, project_result)):
                raise RuntimeError("MCP tool reported an error")
            # Avoid printing license material or private project names.
            print("Capabilities, license, projects: OK")
            kind = os.environ.get("PVSYST_SMOKE_COMPONENT_TYPE")
            component_name = os.environ.get("PVSYST_SMOKE_COMPONENT_NAME")
            if kind and component_name:
                for name, arguments in (
                    ("pvsyst_list_components", {"component_type": kind}),
                    ("pvsyst_get_component", {"component_type": kind,
                                               "filename": component_name, "limit": 5}),
                    ("pvsyst_validate_component", {"component_type": kind,
                                                    "filename": component_name}),
                ):
                    result = await session.call_tool(name, arguments)
                    if result.is_error:
                        raise RuntimeError(f"{name} reported an error")
                print("Component inventory and validation: OK")
            csv_name = os.environ.get("PVSYST_SMOKE_CSV")
            if csv_name:
                for name, arguments in (
                    ("pvsyst_read_results", {"csv_name": csv_name}),
                    ("pvsyst_read_rows", {"csv_name": csv_name, "limit": 2}),
                ):
                    result = await session.call_tool(name, arguments)
                    if result.is_error:
                        raise RuntimeError(f"{name} reported an error")
                print("Results summary and rows: OK")
            project = os.environ.get("PVSYST_SMOKE_RUN_PROJECT")
            variant = os.environ.get("PVSYST_SMOKE_RUN_VARIANT")
            sfi = os.environ.get("PVSYST_SMOKE_RUN_SFI")
            output = os.environ.get("PVSYST_SMOKE_RUN_CSV")
            if all((project, variant, sfi, output)):
                result = await session.call_tool("pvsyst_run_simulation", {
                    "project": project, "variant": variant,
                    "sfi_name": sfi, "csv_name": output,
                    "start_date": "1990.01.01", "end_date": "1990.01.02",
                })
                if result.is_error:
                    raise RuntimeError("MCP simulation reported an error")
                print("MCP simulation: OK")
            batch = os.environ.get("PVSYST_SMOKE_RUN_BATCH_PARAMS")
            batch_rvt = os.environ.get("PVSYST_SMOKE_RUN_BATCH_RVT")
            if all((project, variant, batch, batch_rvt)):
                result = await session.call_tool("pvsyst_run_simulation", {
                    "project": project, "variant": variant,
                    "batch_params_name": batch, "batch_rvt_name": batch_rvt,
                })
                if result.is_error:
                    raise RuntimeError("MCP batch simulation reported an error")
                print("MCP batch simulation: OK")
            batch_summary = os.environ.get("PVSYST_SMOKE_BATCH_SUMMARY")
            if batch_summary:
                result = await session.call_tool("pvsyst_read_batch_results", {
                    "summary_name": batch_summary,
                })
                if result.is_error:
                    raise RuntimeError("MCP batch summary reported an error")
                print("MCP batch summary: OK")
            batch_hourly = os.environ.get("PVSYST_SMOKE_BATCH_HOURLY")
            if batch_hourly:
                result = await session.call_tool("pvsyst_read_results", {
                    "csv_name": batch_hourly, "folder": "UserHourly",
                })
                if result.is_error:
                    raise RuntimeError("MCP batch hourly CSV reported an error")
                print("MCP batch hourly CSV: OK")


if __name__ == "__main__":
    asyncio.run(main())

"""Optional live MCP smoke check; does not invoke a simulation.

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
            license_result = await session.call_tool("pvsyst_license_info", {})
            project_result = await session.call_tool("pvsyst_list_projects", {})
            if license_result.is_error or project_result.is_error:
                raise RuntimeError("MCP tool reported an error")
            # Avoid printing the underlying license response or private project names.
            print("License query: OK; project query: OK")
            csv_name = os.environ.get("PVSYST_SMOKE_CSV")
            if csv_name:
                result = await session.call_tool("pvsyst_read_results", {"csv_name": csv_name})
                if result.is_error:
                    raise RuntimeError("Results parser reported an error")
                print("Results query: OK")


if __name__ == "__main__":
    asyncio.run(main())

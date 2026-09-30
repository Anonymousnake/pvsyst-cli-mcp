"""Read-only real MCP checks on synthetic grouped grid circuits."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_grid import ARRAY, GROUP, grid_text


async def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        folder = root / "Projects"
        folder.mkdir()
        (folder / "Grid.PRJ").write_text("PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        unequal = GROUP + GROUP.replace("NElements=2", "NElements=1").replace("NElements=3", "NElements=4")
        arrays = ARRAY.replace("NInverter=2", "NInverter=3").replace("NStringCh=6", "NStringCh=10")
        samples = [grid_text(unequal, arrays), grid_text(arrays=ARRAY.replace("NStringCh=6", "NStringCh=8")),
                   grid_text(GROUP.replace("StringNode", "OptimizerNode"))]
        for i, value in enumerate(samples):
            (folder / f"Grid.VC{i}").write_text(value, encoding="utf-8")
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
        params = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / "pvsyst_mcp_server.py")],
            env={**os.environ, "PVSYST_CLI": sys.executable, "PVSYST_WORKSPACE": str(root)})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                reports = []
                for i in range(len(samples)):
                    answer = await session.call_tool("pvsyst_validate_variant_structure", {"project": "Grid.PRJ", "variant": f"VC{i}"})
                    assert not answer.is_error, answer.content
                    reports.append(json.loads(answer.content[0].text))
                assert reports[0]["valid"] is True
                assert reports[0]["grid_circuit"]["totals"]["strings"] == 10
                assert reports[1]["valid"] is False and reports[1]["grid_circuit"]["subarrays"][0]["circuit"]["strings"] == 6
                assert reports[2]["valid"] is None and reports[2]["unchecked_checks"]
        assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
        print("MCP grid circuit: uneven groups, mismatched counts, unknown node scope and unchanged inputs: OK")


if __name__ == "__main__":
    asyncio.run(main())

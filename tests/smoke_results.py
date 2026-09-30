"""Real MCP comparisons in disposable copies; no native simulation is run.

Optionally set PVSYST_COMPARE_BASELINE and PVSYST_COMPARE_TARGET to existing
native CSVs and PVSYST_COMPARE_COLUMNS to comma-separated column names.
"""
import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvsyst_cli import parse_result_csv
from pvsyst_results import fingerprint


async def main():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        for folder in ("Results", "UserHourly"):
            (root / folder).mkdir()
        baseline, target = root / "UserHourly" / "baseline.csv", root / "Results" / "target.csv"
        baseline.write_text("date;E_Grid\n;kW\n01/01/90 00:00;10\n01/01/90 01:00;\n01/01/90 02:00;30\n")
        target.write_text("date;E_Grid\n;kW\n01/01/90 00:00;15\n01/01/90 01:00;100\n01/01/90 02:00;37\n")
        params = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / "pvsyst_mcp_server.py")],
            env={**os.environ, "PVSYST_WORKSPACE": str(root), "PVSYST_CLI": sys.executable})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 45
                async def call(args, error=None):
                    response = await session.call_tool("pvsyst_read_results", args)
                    assert response.is_error == bool(error), response.content
                    if error:
                        assert error in str(response.content)
                    return None if error else json.loads(response.content[0].text)
                args = {"csv_name": target.name, "columns": "E_Grid", "compare_to": baseline.name, "compare_folder": "UserHourly"}
                result = await call(args)
                state = result["summary"]["E_Grid"]
                assert state["paired_delta_sum"] == 12 and state["paired_samples"] == 2
                assert state["observed_delta_energy_kwh"] == 12 and state["delta_energy_kwh"] is None
                single = await call({"csv_name": target.name, "columns": "E_Grid"})
                assert single["summary"]["E_Grid"]["sum"] == 152
                await call(args | {"compare_to": "../baseline.csv"}, error="without directories")
                target.write_text(target.read_text().replace(";kW", ";W"))
                await call(args, error="identical explicit units")
                target.write_text(target.read_text().replace(";W", ";kW").replace("02:00", "03:00"))
                await call(args, error="different timestamps")
                print("MCP result comparison: paired missing-data statistics, unit/time/path rejection, legacy summary: OK")
                paths = [os.environ.get(key) for key in ("PVSYST_COMPARE_BASELINE", "PVSYST_COMPARE_TARGET")]
                if any(paths):
                    if not all(paths):
                        raise ValueError("Set both baseline and target CSV paths")
                    sources = [Path(p).resolve(strict=True) for p in paths]
                    before = [fingerprint(p) for p in sources]
                    for source, dest in zip(sources, (baseline, target)):
                        shutil.copyfile(source, dest)
                    columns = os.environ.get("PVSYST_COMPARE_COLUMNS", "EArray,E_User,GlobInc").split(",")
                    result = await call(args | {"columns": ",".join(columns)})
                    ha, ra = parse_result_csv(baseline)
                    hb, rb = parse_result_csv(target)
                    assert len(ra) == len(rb) == result["rows"]
                    assert [r[0] for r in ra] == [r[0] for r in rb]
                    for key in columns:
                        pairs = [(a[ha.index(key)], b[hb.index(key)]) for a, b in zip(ra, rb)
                                 if math.isfinite(a[ha.index(key)]) and math.isfinite(b[hb.index(key)])]
                        state = result["summary"][key]
                        assert state["paired_samples"] == len(pairs)
                        expected = math.fsum(b - a for a, b in pairs) if pairs else None
                        assert expected is None and state["paired_delta_sum"] is None or math.isclose(expected, state["paired_delta_sum"], abs_tol=1e-9)
                    assert before == [fingerprint(p) for p in sources]
                    assert [result[k]["sha256"] for k in ("baseline", "target")] == before
                    print(json.dumps({"native_csv_comparison": result, "source_unchanged": True}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

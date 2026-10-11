"""Real MCP weather inspection; optional read-only official CSV sample evidence."""
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
from test_weather import weather_text


async def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        meteo = root / "Meteo"
        meteo.mkdir()
        for name, value in (("complete.csv", weather_text()),
                            ("gap.csv", weather_text(skip=0)),
                            ("shift.csv", weather_text().replace("#Site;", "#Time reference;UT\n#Site;"))):
            (meteo / name).write_text(value, encoding="utf-8-sig")
        sample = Path(os.environ["PVSYST_WEATHER_SAMPLE"]) if os.environ.get("PVSYST_WEATHER_SAMPLE") else None
        sample_before = sample.read_bytes() if sample else None
        if sample:
            (meteo / "official.csv").write_bytes(sample_before)
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in meteo.iterdir()}
        params = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / "pvsyst_mcp_server.py")],
            env={**os.environ, "PVSYST_CLI": sys.executable, "PVSYST_WORKSPACE": str(root)})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()

                async def inspect(name, **extra):
                    response = await session.call_tool("pvsyst_inspect_weather_csv", {"csv_name": name, **extra})
                    assert not response.is_error, response.content
                    return json.loads(response.content[0].text)

                complete = await inspect("complete.csv", expected_year=2023)
                assert complete["valid"] is True and complete["rows"] == 8760
                assert abs(complete["months"][0]["GHI"]["observed_kwh_m2"] - 37.2) < 1e-9
                gap = await inspect("gap.csv")
                assert gap["valid"] is False and gap["missing_hours"] == 1
                shift = await inspect("shift.csv")
                assert shift["valid"] is None and shift["unchecked_checks"]
                for args in ({"csv_name": "../escape.csv"}, {"csv_name": "complete.csv", "expected_year": True},
                             {"csv_name": "complete.csv", "expected_year": "2023"}):
                    rejected = await session.call_tool("pvsyst_inspect_weather_csv", args)
                    assert rejected.is_error, args
                evidence = {"complete": complete, "gap": gap, "shift": shift}
                if sample:
                    evidence["official_sample"] = await inspect("official.csv")
                    assert sample.read_bytes() == sample_before
        assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in meteo.iterdir()}
        evidence["inputs_unchanged"] = True
        if os.environ.get("PVSYST_WEATHER_EVIDENCE"):
            path = Path(os.environ["PVSYST_WEATHER_EVIDENCE"])
            with path.open("x", encoding="utf-8") as stream:
                json.dump(evidence, stream, indent=2, allow_nan=False)
        print("MCP weather: full year, known monthly energy, missing night hour, unverified time reference, strict inputs and unchanged files: OK")
        if sample:
            report = evidence["official_sample"]
            print(f"Official sample: valid={report['valid']}, rows={report['rows']}, "
                  f"nonzero-minute records={report['nonzero_minute_records']}, sha256={report['sha256']}")


if __name__ == "__main__":
    asyncio.run(main())

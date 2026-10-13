"""Public MCP load import contract; no native execution or digest assertions."""
import asyncio
import calendar
import csv
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SERVER = Path(__file__).resolve().parents[1] / "pvsyst_mcp_server.py"


def data(year=2024, unit="W", bad=None, delimiter=";"):
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=delimiter, lineterminator="\n")
    writer.writerow(["Date", "P Load"])
    writer.writerow(["", unit])
    start = datetime(year, 1, 1)
    for hour in range(24 * (366 if calendar.isleap(year) else 365)):
        if bad == "gap" and hour == 10:
            continue
        stamp = start + timedelta(hours=hour - (1 if bad == "duplicate" and hour == 10 else 0))
        value = "nan" if bad == "nan" and hour == 10 else "-2" if bad == "negative" and hour == 10 else "2500"
        writer.writerow([stamp.strftime("%Y-%m-%d %H:%M"), value])
    return output.getvalue()


async def main():
    checks = 0
    def check(condition, detail):
        nonlocal checks
        if not condition:
            raise AssertionError(detail)
        checks += 1

    with tempfile.TemporaryDirectory(prefix="pvsyst-load-contract-") as directory:
        root = Path(directory)
        (root / "UserData").mkdir()
        for name, body in {"good.csv": data(), "regular.csv": data(2023),
                           "gap.csv": data(bad="gap"), "duplicate.csv": data(bad="duplicate"),
                           "nan.csv": data(bad="nan"), "negative.csv": data(bad="negative"),
                           "unit.csv": data(unit="kWh"), "comma.csv": data(delimiter=","), "large.csv": data(unit="kW")}.items():
            (root / "UserData" / name).write_text(body, encoding="utf-8", newline="")
        original = (root / "UserData/good.csv").read_bytes()
        (root / "Projects").mkdir()
        (root / "Projects/LoadFixture.PRJ").write_text("PVObject_=pvProject\n  Comment=Load fixture\nEnd of PVObject pvProject\n", encoding="utf-8")
        template = ("PVObject_=pvVCalcul\n  Comment=Load fixture\n  Version=8.1.6\n"
                    "  PVObject_=pvSubArray\n    SubArrayId=1\n    NModSerie=10\n  End of PVObject pvSubArray\n"
                    "  PVObject_System=pvSystem\n    SystemType=Grid\n    Flags=$10010040\n  End of PVObject pvSystem\n"
                    "  PVObject_UserNeeds=pvLoad\n    Flags=$4000\n    LoadType=HourlyFile\n    Day_0101=+oldcache\n  End of PVObject pvLoad\n"
                    "End of PVObject pvVCalcul\n")
        (root / "Projects/LoadFixture.VC0").write_text(template, encoding="utf-8", newline="")
        env = {**os.environ, "PVSYST_CLI": sys.executable, "PVSYST_WORKSPACE": str(root)}
        async with stdio_client(StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=env)) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                registry = await session.list_tools()
                tool, = [tool for tool in registry.tools if tool.name == "pvsyst_prepare_load_profile"]
                check(tool.description.startswith("[Loads & Storage]"), "published load tool category")
                check(tool.input_schema["properties"]["dry_run"]["type"] == "boolean", "strict bool schema")
                base = {"csv_name": "good.csv", "year": 2024, "output_name": "new.csv",
                        "utc_offset_hours": 8, "time_basis": "local_standard_interval_start"}
                async def call(**changes):
                    response = await session.call_tool(tool.name, {**base, **changes})
                    return response, (None if response.is_error else json.loads(response.content[0].text))
                response, preview = await call()
                check(not response.is_error and preview["status"] == "ready", "dry run ready")
                check(not (root / "UserHourlyParams").exists() and not (root / "Results").exists(), "dry run writes nothing")
                check(preview["inspection"]["rows"] == 8784 and preview["inspection"]["missing_hours"] == 0, "full leap year")
                check(preview["inspection"]["annual_load_kwh"] == 8784 * 2.5, "explicit W to kW conversion")
                check(not preview["native_consumption_verified"], "preparation is not native consumption")
                response, created = await call(dry_run=False)
                check(not response.is_error and created["status"] == "prepared", "create native CSV")
                output = Path(created["artifacts"]["csv"]).read_bytes()
                check(output.startswith(b"Date;P Load\r\n;kW\r\n01.01.2024 00:00;2.5\r\n"), "native ASCII format")
                check(b"\r\r\n" not in output and output.count(b"\r\n") == 8786, "one CRLF per row")
                check(b"29.02.2024 00:00;2.5\r\n" in output, "leap day retained")
                check((root / "UserData/good.csv").read_bytes() == original, "source preserved")
                saved = json.loads(Path(created["artifacts"]["manifest"]).read_text(encoding="utf-8"))
                check(saved["status"] == "prepared" and not saved["native_consumption_verified"], "honest persisted import record")
                _, repeated = await call(dry_run=False)
                check(repeated["status"] == "blocked", "no overwrite")
                for name in ("gap.csv", "duplicate.csv", "nan.csv", "negative.csv", "unit.csv"):
                    _, result = await call(csv_name=name, output_name="invalid.csv", dry_run=False)
                    check(result["status"] == "blocked" and result["issues"], "invalid input blocks: " + name)
                    check(not (root / "UserHourlyParams/invalid.csv").exists(), "invalid input writes nothing: " + name)
                _, regular = await call(csv_name="regular.csv", year=2023, output_name="regular-out.csv")
                check(regular["status"] == "ready" and regular["inspection"]["rows"] == 8760, "regular year")
                _, comma = await call(csv_name="comma.csv", output_name="comma-out.csv")
                check(comma["status"] == "ready", "declared comma separator accepted")
                for changes in ({"year": True}, {"year": "2024"}, {"utc_offset_hours": 8.5},
                                {"dry_run": "false"}, {"time_basis": "UTC"}, {"extra": 1}):
                    response, _ = await call(**changes)
                    check(response.is_error, "strict malformed option: " + repr(changes))
                _, escaped = await call(csv_name="../good.csv", output_name="escape.csv", dry_run=False)
                check(escaped["status"] == "blocked", "input path stays in UserData")
                _, escaped = await call(output_name="../escape.csv", dry_run=False)
                check(escaped["status"] == "blocked", "output path stays in UserHourlyParams")
                _, mismatch = await call(year=2023, output_name="year-mismatch.csv")
                check(mismatch["status"] == "blocked", "year never silently ignored by importer")
                a, b = await asyncio.gather(call(output_name="race.csv", dry_run=False), call(output_name="race.csv", dry_run=False))
                check(sorted([a[1]["status"], b[1]["status"]]) == ["blocked", "prepared"], "same-name creation has one winner")
                binding = {"project": "LoadFixture.PRJ", "source_variant": "VC0", "new_variant": "VC1",
                           "load_manifest_name": "new_load.json"}
                response = await session.call_tool("pvsyst_create_load_variant", binding)
                check(not response.is_error, "load binding tool published")
                preview = json.loads(response.content[0].text)
                check(preview["status"] == "ready" and not (root / "Projects/LoadFixture.VC1").exists(), "load variant preview writes nothing")
                check(preview["load_encoding"]["rows"] == 8784 and preview["load_encoding"]["day_count"] == 366, "complete leap snapshot")
                check(not preview["native_consumption_verified"], "binding is not consumption certification")
                response = await session.call_tool("pvsyst_create_load_variant", {**binding, "dry_run": False})
                created_variant = json.loads(response.content[0].text)
                check(not response.is_error and created_variant["status"] == "created", "create load variant")
                modified = (root / "Projects/LoadFixture.VC1").read_text(encoding="utf-8")
                check("+oldcache" not in modified and "Day_2902=" in modified, "old load removed and leap day present")
                check("StoredUnit=MJ" in modified and "IsRenormalized=False" in modified, "explicit native energy and no annual scale")
                check((root / "Projects/LoadFixture.VC0").read_text(encoding="utf-8") == template, "source variant retained")
                response = await session.call_tool("pvsyst_create_load_variant", {**binding, "dry_run": False})
                check(json.loads(response.content[0].text)["status"] == "blocked", "existing load variant retained")
                _, large = await call(csv_name="large.csv", output_name="large-out.csv", dry_run=False)
                check(large["status"] == "prepared" and large["inspection"]["max_load_kw"] == 2500, "high load not clipped during import")
                response = await session.call_tool("pvsyst_create_load_variant", {**binding, "new_variant": "VC3", "load_manifest_name": "large-out_load.json"})
                high = json.loads(response.content[0].text)
                check(high["status"] == "ready" and high["load_encoding"]["WRFactor"] < 1000, "high load adjusts checked token scale")
                check(high["load_encoding"]["max_encoding_error_kw"] <= high["load_encoding"]["encoding_error_bound_kw"] + 1e-10, "encoding rounding explicitly bounded")
                Path(created["artifacts"]["csv"]).write_bytes(output.replace(b"00:00;2.5", b"00:00;2.6", 1))
                response = await session.call_tool("pvsyst_create_load_variant", {**binding, "new_variant": "VC2", "dry_run": False})
                check(json.loads(response.content[0].text)["status"] == "blocked" and not (root / "Projects/LoadFixture.VC2").exists(), "changed normalized load blocked before writes")
    print(f"{checks} public MCP load import checks passed; 0 native runs")


if __name__ == "__main__":
    asyncio.run(main())

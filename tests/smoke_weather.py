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

                async def prepare(**arguments):
                    response = await session.call_tool("pvsyst_prepare_weather", arguments)
                    assert not response.is_error, response.content
                    return json.loads(response.content[0].text)

                def tree():
                    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in root.rglob("*") if p.is_file()}

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

                # 天气闭环工具：schema、严格类型和默认预检的"无写入、无原生命令"证据
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert "pvsyst_prepare_weather" in tools, sorted(tools)
                assert tools["pvsyst_prepare_weather"].description.startswith("[Sites & Weather]")
                schema = tools["pvsyst_prepare_weather"].input_schema
                assert set(schema["properties"]) == {"csv_name", "year", "sit_name", "met_name",
                                                     "country_code", "time_basis", "dry_run"}, schema
                assert schema["properties"]["year"]["anyOf"] == [{"type": "integer"}, {"type": "null"}], schema
                assert schema["properties"]["dry_run"]["type"] == "boolean", schema
                weather_properties = tools["pvsyst_simulate_template"].input_schema["properties"]
                assert "weather_manifest_name" in weather_properties, sorted(weather_properties)
                assert weather_properties["year"]["anyOf"][0]["type"] == "integer", weather_properties["year"]
                for arguments in ({"csv_name": "complete.csv", "year": True},
                                  {"csv_name": "complete.csv", "year": "2023"},
                                  {"csv_name": "complete.csv", "year": 2023.0},
                                  {"csv_name": "complete.csv", "dry_run": 1},
                                  {"csv_name": "complete.csv", "dry_run": "true"},
                                  {"csv_name": 3}):
                    rejected = await session.call_tool("pvsyst_prepare_weather", arguments)
                    assert rejected.is_error, arguments
                request = {"csv_name": "complete.csv", "year": 2023, "sit_name": "P.SIT",
                           "met_name": "P.MET", "country_code": "TT",
                           "time_basis": "local_standard_interval_start"}
                root_before = tree()
                for changes in ({"time_basis": "UTC"}, {"country_code": "us"}, {"year": 2021},
                                {"csv_name": "gap.csv"}, {"csv_name": "shift.csv"},
                                {"csv_name": "complete.MET"}, {"csv_name": "../escape.csv"},
                                {"csv_name": "absent.csv"}):
                    blocked = await prepare(**{**request, **changes})
                    assert blocked["status"] == "blocked" and blocked["issues"], blocked
                    # 反例在触碰原生 CLI 之前就被拒绝
                    assert "create-site" not in json.dumps(blocked), blocked
                # 合法入口只会停在原生能力查询：PVSYST_CLI 指向 sys.executable，预检不得假装就绪
                gated = await prepare(**request)
                assert gated["status"] == "blocked" and "create-site" in json.dumps(gated), gated
                # 未知顶层键必须在工具执行前被拒绝；SDK 2.x 的普通参数模型会静默丢弃它们
                unknown = await session.call_tool("pvsyst_prepare_weather", {**request, "unknown_option": 1})
                assert unknown.is_error, unknown.content
                unknown_text = unknown.content[0].text
                assert unknown_text.startswith(
                    "Unknown argument(s) for pvsyst_prepare_weather: unknown_option."), unknown_text
                assert "Accepted arguments:" in unknown_text, unknown_text
                # 拦截发生在 SDK 参数验证之前：严格类型错误不会抢先，未知键仍被点名
                mixed = await session.call_tool("pvsyst_prepare_weather",
                                                {"csv_name": "complete.csv", "year": True, "unknown_option": 1})
                assert mixed.is_error and "unknown_option" in mixed.content[0].text, mixed.content
                # 真实拼写错误 weather_manifest_name -> weather_manifest 不得退化为"不绑定来源"
                typo = await session.call_tool("pvsyst_simulate_template",
                                               {"template_project": "T.PRJ", "weather_manifest": "W.MANIFEST"})
                assert typo.is_error, typo.content
                typo_text = typo.content[0].text
                assert typo_text.startswith(
                    "Unknown argument(s) for pvsyst_simulate_template: weather_manifest."), typo_text
                assert "weather_manifest_name" in typo_text, typo_text
                # 只封闭这两个入口：其余工具保持 SDK 既有的未知键忽略行为
                assert await inspect("complete.csv", unknown_option=1) == complete
                assert root_before == tree(), tree()
                evidence["prepare_contract"] = {"tool_parameters": sorted(schema["properties"]),
                                                "valid_input_status": gated["status"],
                                                "unknown_key_rejected": unknown_text,
                                                "manifest_typo_rejected": typo_text,
                                                "other_tools_unchanged": True, "workspace_unchanged": True}
        assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in meteo.iterdir()}
        evidence["inputs_unchanged"] = True
        if os.environ.get("PVSYST_WEATHER_EVIDENCE"):
            path = Path(os.environ["PVSYST_WEATHER_EVIDENCE"])
            with path.open("x", encoding="utf-8") as stream:
                json.dump(evidence, stream, indent=2, allow_nan=False)
        print("MCP weather: full year, known monthly energy, missing night hour, unverified time reference, strict inputs and unchanged files: OK")
        print("MCP weather preparation: tool schema, strict year/bool, rejected unknown top-level keys, "
              "blocked counterexamples before any native command, no writes: OK")
        if sample:
            report = evidence["official_sample"]
            print(f"Official sample: valid={report['valid']}, rows={report['rows']}, "
                  f"nonzero-minute records={report['nonzero_minute_records']}, sha256={report['sha256']}")


if __name__ == "__main__":
    asyncio.run(main())

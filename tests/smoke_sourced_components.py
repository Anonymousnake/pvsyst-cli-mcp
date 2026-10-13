"""Public MCP component-building contracts; synthetic data, no native run or digest tests."""
import asyncio
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_design_envelope import PAN_TEXT

OND_TEXT = """PVObject_=pvGInverter
  Version=8.1.6
  Flags=$00
  PVObject_Commercial=pvCommercial
    Manufacturer=Fixture
    Model=Inv 9K
    DataSource=Test fixture
  End of PVObject pvCommercial
  Converter=TConverter
    PNomConv=9
    PMaxOUT=9.9
    VMppMin=200
    VMPPMax=850
    VAbsMax=1000
    EfficMax=97.5
    EfficEuro=96.8
    ModeOper=MPPT
    ProfilPIO=TCubicProfile
      NPtsMax=4
      NPtsEff=4
      Mode=1
      Point_1=44,0
      Point_2=900,863
      Point_3=5400,5249
      Point_4=9000,8785
    End of TCubicProfile
  End of TConverter
  NbInputs=2
  NbMPPT=1
End of PVObject pvGInverter
"""


def request(kind="PAN", name="Built.PAN"):
    model = "Mono 440W half-cell" if kind == "PAN" else "Inv 9K"
    rows = [("PNom", 440, "W", "440", "Wp"), ("Depth", 35, "mm", "35", "mm")]
    if kind == "OND":
        rows = [("PNomConv", 9, "kW", "9", "kW"), ("EfficMax", 97.8, "%", "97.8", "%")]
    text = model + "\n" + "\n".join(number + unit for _, _, _, number, unit in rows)
    return {"component_type": kind, "template_name": "Seed." + kind, "new_name": name,
            "manufacturer": "Fixture", "model": model,
            "sources": {"doc": {"kind": "assumption", "title": "Explicit test fixture",
                "locator": "fixture://component", "model": model, "text": text}},
            "declarations": [{"field": field, "value": value, "unit": unit,
                "source_id": "doc", "excerpt": number + quoted_unit,
                "source_value_text": number, "source_unit_text": quoted_unit}
                for field, value, unit, number, quoted_unit in rows]}


async def main():
    checks = 0
    def check(condition, detail):
        nonlocal checks
        if not condition:
            raise AssertionError(detail)
        checks += 1

    with tempfile.TemporaryDirectory(prefix="pvsyst-sourced-component-") as directory:
        root = Path(directory)
        pan_dir = root / "ComposPV/PVmodules"
        ond_dir = root / "ComposPV/Inverters"
        pan_dir.mkdir(parents=True)
        ond_dir.mkdir()
        (pan_dir / "Seed.PAN").write_text(PAN_TEXT.replace("DataSource=Reference module",
            "DataSource=Reference module\n    Width=1.032\n    Height=1.996"), encoding="utf-8")
        (ond_dir / "Seed.OND").write_text(OND_TEXT, encoding="utf-8")
        params = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / "pvsyst_mcp_server.py")],
            env={**os.environ, "PVSYST_CLI": sys.executable, "PVSYST_WORKSPACE": str(root)})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listed = await session.list_tools()
                check(any(tool.name == "pvsyst_build_sourced_component" for tool in listed.tools), "Published builder")
                async def call(args, *, error=False):
                    response = await session.call_tool("pvsyst_build_sourced_component", args)
                    check(bool(response.is_error) == error, response)
                    return json.loads(response.content[0].text) if not error else None

                seed_bytes = (pan_dir / "Seed.PAN").read_bytes()
                args = request()
                preview = await call(args)
                check(preview["status"] == "needs_review" and preview["stage"] == "preview", preview)
                check(all(item["reference_match"] for item in preview["provenance"]), "Cited values bind")
                check(preview["requested_fields"]["Depth"] == "0.035", "Explicit mm -> m")
                check(not preview["model_refitted"], "No invented model fitting")
                check("MuISC" in preview["inherited_fields"]["root"], "Serialized coefficient remains inherited")
                check(not (pan_dir / "Built.PAN").exists() and not (root / "Results").exists(), "Preview writes nothing")
                refused = await call({**args, "dry_run": False})
                check(refused["status"] == "blocked" and not (root / "Results").exists(), "Creation needs explicit acknowledgement")
                created = await call({**args, "dry_run": False, "acknowledge_inherited_model": True})
                check(created["status"] == "needs_review" and created["component_created"], created)
                built_bytes = (pan_dir / "Built.PAN").read_bytes()
                check(b"Depth=0.035" in built_bytes and b"User-defined from Seed.PAN" in built_bytes, "New model is visibly user-defined")
                record = json.loads(Path(created["artifacts"]["source_record"]).read_text(encoding="utf-8"))
                check(record["provenance"] == created["provenance"] and "component_created" not in record,
                      "Candidate record is not execution certification")
                check((pan_dir / "Seed.PAN").read_bytes() == seed_bytes, "Original template unchanged")
                collision = await call({**args, "dry_run": False, "acknowledge_inherited_model": True})
                check(collision["status"] == "blocked" and (pan_dir / "Built.PAN").read_bytes() == built_bytes, "Never overwrite")

                for key, value in (("dry_run", "false"), ("acknowledge_inherited_model", 1), ("unexpected", True)):
                    await call({**args, key: value}, error=True)
                await call({**args, "declarations": []}, error=True)
                for label, mutate in (
                        ("unit", lambda value: value["declarations"][0].update(unit="kW")),
                        ("duplicate", lambda value: value["declarations"].append(value["declarations"][0].copy())),
                        ("unknown", lambda value: value["declarations"][0].update(field="MuISC")),
                        ("model", lambda value: value["sources"]["doc"].update(model="Other")),
                        ("quote", lambda value: value["declarations"][0].update(excerpt="Not in document"))):
                    bad = copy.deepcopy(args)
                    bad["new_name"] = label + ".PAN"
                    mutate(bad)
                    response = await call(bad)
                    check(response["status"] == "blocked" and not (pan_dir / bad["new_name"]).exists(), label)
                escape = await call({**args, "new_name": "../escape.PAN"})
                check(escape["status"] == "blocked", "Path escape blocked")

                concurrent_args = {**args, "new_name": "Concurrent.PAN", "dry_run": False,
                                   "acknowledge_inherited_model": True}
                concurrent = await asyncio.gather(call(concurrent_args), call(concurrent_args))
                check(sum(item["component_created"] for item in concurrent) == 1
                      and sorted(item["status"] for item in concurrent) == ["blocked", "needs_review"],
                      "Concurrent same-name creation has one winner")
                validation_response = await session.call_tool("pvsyst_validate_component",
                    {"component_type": "PAN", "filename": "Concurrent.PAN"})
                check(not validation_response.is_error, "Concurrent output is readable")
                concurrent_validation = json.loads(validation_response.content[0].text)
                check(not concurrent_validation["structural_errors"]
                      and not concurrent_validation["known_value_errors"], "Concurrent output is complete")

                inverted = request("OND", "Built.OND")
                original_ond = (ond_dir / "Seed.OND").read_bytes()
                inconsistent = copy.deepcopy(inverted)
                inconsistent["new_name"] = "InvalidEfficiency.OND"
                inconsistent["sources"]["doc"]["text"] += "\n90%"
                inconsistent["declarations"][1].update(value=90, source_value_text="90", excerpt="90%")
                wrong_efficiency = await call(inconsistent)
                check(wrong_efficiency["status"] == "blocked"
                      and not (ond_dir / "InvalidEfficiency.OND").exists(), "Euro efficiency never exceeds declared maximum")
                inv = await call({**inverted, "dry_run": False, "acknowledge_inherited_model": True})
                check(inv["component_created"] and inv["status"] == "needs_review", inv)
                inv_text = (ond_dir / "Built.OND").read_text(encoding="utf-8-sig")
                check("EfficMax=97.8" in inv_text and "Point_4=9000,8785" in inv_text,
                      "Efficiency scalar does not silently scale inherited profile")
                check((ond_dir / "Seed.OND").read_bytes() == original_ond, "Original inverter unchanged")
                check(bool(inv["inherited_curves"]["items"]), "Inherited curve is disclosed")
                print(json.dumps({"checks_passed": checks, "native_executions": 0,
                                  "scope": "Public MCP creation, provenance, preview and error contracts"}))


if __name__ == "__main__":
    asyncio.run(main())

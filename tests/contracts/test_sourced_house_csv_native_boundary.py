"""Public sourced-house CSV-native end-to-end boundary (cited-input source -> child year).

This pins only externally observable public behavior of ``pvsyst_run_sourced_house_task``
through the real stdio MCP server -- no internal job field, helper, call order or checkpoint
snapshot:

* the published ``report_pdf`` wire field on the sourced tool is a nullable boolean and a
  truthy string/number is rejected (StrictBool, no coercion);
* the real 0-CLI ``pvsyst_prepare_sourced_design`` binding turns the declared excerpts into a
  concrete electrical draft (needs_review retained; every non-datasheet scalar is a labelled
  caller assumption, never a vendor fact / certification);
* one opt-in real-native full chain: ``confirm=True`` prepares the source project and delivers
  one native child year (2024 leap-year -> 8784-hourly CSV, NO PDF), then a completed
  ``resume=True`` re-verifies the same CSV artifact with no new simulation and byte-identical
  source/CSV, no new run.

The sourced dry path is licence-first, so this file never sends a valid ``dry_run`` request:
only the pure schema/wire and the 0-CLI prepare binding truly execute here. The single native
E2E method is budgeted at exactly TWO public confirm requests -- (1) the compute ``confirm=True,
resume=False`` and (2) a completed ``resume=True`` issued ONLY after (1) reports a
completed/needs_review run with native consumption verified. A blocked/failed compute never
resumes; resume never retries (``retry_simulation`` stays False) and appends no second run.
There is no loop, no scenario re-run and no extra native call.

Env (all required, else skip -> nothing runs by default):
  PVSYST_CONTRACT_CLI=<path>            real installed PVsystCLI.exe (never spawned here)
  PVSYST_CONTRACT_WORKSPACE=<dir>       real PVsyst8.1_Data workspace with the loose PAN/OND
                                        and the source SIT/MET
  PVSYST_CONTRACT_SOURCED_JSON=<file>   full SourcedHouseTaskRequest body: design(module/inverter
                                        file_name, weather existing), new_project(s), house, layout,
                                        string_layout, module_geometry, sources, declarations, goals;
                                        ``report_pdf`` and unique project names are owned by this test
  PVSYST_CONTRACT_REAL_NATIVE=1         opt-in for the real native E2E class only
"""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import unittest
import uuid

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]
TOOL = "pvsyst_run_sourced_house_task"
PREPARE = "pvsyst_prepare_sourced_design"


def _payload(reply):
    return json.loads("\n".join(item.text for item in reply.content if hasattr(item, "text")))


def _find_property(schema, key):
    """Locate a named property subschema anywhere in a JSON schema tree (incl. $defs)."""
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict) and key in props:
            return props[key]
        for value in schema.values():
            found = _find_property(value, key)
            if found is not None:
                return found
    elif isinstance(schema, list):
        for value in schema:
            found = _find_property(value, key)
            if found is not None:
                return found
    return None


def _permitted_types(subschema):
    kinds = set()
    if not isinstance(subschema, dict):
        return kinds
    declared = subschema.get("type")
    if isinstance(declared, str):
        kinds.add(declared)
    elif isinstance(declared, list):
        kinds.update(declared)
    for option in subschema.get("anyOf", []) or subschema.get("oneOf", []):
        if isinstance(option, dict) and isinstance(option.get("type"), str):
            kinds.add(option["type"])
    return kinds


class _FixtureCase(unittest.TestCase):
    """Loads the explicit sourced fixture; skips cleanly when any env part is absent."""

    def load_fixture(self):
        cli = os.environ.get("PVSYST_CONTRACT_CLI")
        workspace = os.environ.get("PVSYST_CONTRACT_WORKSPACE")
        sourced_json = os.environ.get("PVSYST_CONTRACT_SOURCED_JSON")
        if not cli or not Path(cli).is_file():
            self.skipTest("Set PVSYST_CONTRACT_CLI to a real installed PVsystCLI.exe")
        if not workspace or not Path(workspace).is_dir():
            self.skipTest("Set PVSYST_CONTRACT_WORKSPACE to a real PVsyst8.1_Data workspace")
        if not sourced_json or not Path(sourced_json).is_file():
            self.skipTest("Set PVSYST_CONTRACT_SOURCED_JSON to the fixture SourcedHouseTaskRequest body")
        try:
            spec = json.loads(Path(sourced_json).read_text(encoding="utf-8"))
        except ValueError as exc:
            self.fail("PVSYST_CONTRACT_SOURCED_JSON is not valid JSON: %s" % exc)
        if not isinstance(spec, dict) or not all(
                spec.get(key) for key in ("design", "new_project", "variant_name", "house", "layout")):
            self.fail("PVSYST_CONTRACT_SOURCED_JSON needs design/new_project/variant_name/house/layout")
        if spec.get("scene_import_method") != "native":
            self.skipTest("This contract targets the native import route; fixture declares %r"
                          % spec.get("scene_import_method"))
        self.cli = cli
        self.workspace = Path(workspace)
        self.spec = spec

    def server(self):
        env = dict(os.environ, PVSYST_CLI=self.cli, PVSYST_WORKSPACE=str(self.workspace),
                   PYTHONDONTWRITEBYTECODE="1")
        env.pop("PVSYST_TOOL_GROUPS", None)
        env.pop("PVSYST_BUILTIN_COMPONENTS", None)
        return StdioServerParameters(command=sys.executable,
                                     args=[str(REPO / "pvsyst_mcp_server.py")],
                                     cwd=str(REPO), env=env)

    def fresh_spec(self):
        """A request whose source AND final project are always new (dry runs write nothing)."""
        value = copy.deepcopy(self.spec)
        value.pop("report_pdf", None)
        value["new_project"] = "SRC-CONTRACT-HOUSE-%s.PRJ" % uuid.uuid4().hex[:10]
        value["design"]["new_project"] = "SRC-CONTRACT-SRC-%s.PRJ" % uuid.uuid4().hex[:10]
        return value

    def input_assets(self):
        """Whole-byte snapshot of the read-only loose PAN/OND plus every workspace SIT/MET."""
        design = self.spec["design"]
        paths = []
        for kind, folder in (("module", "ComposPV/PVmodules"), ("inverter", "ComposPV/Inverters")):
            name = design[kind]["file_name"]
            self.assertEqual(Path(name).name, name)
            paths.append(self.workspace / folder / name)
        for folder, suffix in (("Sites", ".sit"), ("Meteo", ".met")):
            directory = self.workspace / folder
            if directory.is_dir():
                paths += [p for p in directory.rglob("*")
                          if p.is_file() and p.suffix.lower() == suffix]
        return {p: p.read_bytes() for p in paths if p.is_file()}


class SourcedHouseCsvNativeWirePrepare(_FixtureCase):
    """Public wire + 0-CLI prepare binding. No sourced dry (licence-first) is ever sent."""

    def setUp(self):
        self.load_fixture()

    def test_report_pdf_wire_schema_and_strict_bool(self):
        asyncio.run(self._check_wire())

    async def _check_wire(self):
        async with stdio_client(self.server()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                tool = next(t for t in tools if t.name == TOOL)
                self.assertEqual(set(tool.input_schema.get("properties", {})),
                                 {"spec", "dry_run", "confirm", "resume", "retry_simulation",
                                  "accept_source_review"})
                subschema = _find_property(tool.input_schema, "report_pdf")
                self.assertIsNotNone(subschema, "report_pdf must be published on the sourced spec")
                self.assertLessEqual({"boolean", "null"}, _permitted_types(subschema),
                                     "report_pdf must be a nullable boolean")
                if "default" in subschema:
                    self.assertIsNone(subschema["default"], "report_pdf default must be None")
                # A truthy string/number is never coerced: rejected before any workflow runs.
                for bad in ("false", "true", "False", 0, 1):
                    spec = self.fresh_spec()
                    spec["report_pdf"] = bad
                    reply = await session.call_tool(TOOL, {"spec": spec})
                    self.assertTrue(reply.is_error, (bad, reply.content))

    def test_prepare_binding_is_needs_review_with_labelled_assumptions(self):
        asyncio.run(self._check_prepare())

    async def _check_prepare(self):
        # 0-CLI: pvsyst_prepare_sourced_design reads the loose PAN/OND and binds the declared
        # text; it constructs no PVsystCLI, so this really executes with no licence spawn.
        before = self.input_assets()
        spec = self.spec
        design = spec["design"]
        arguments = {
            "pan_name": design["module"]["file_name"],
            "ond_name": design["inverter"]["file_name"],
            "sources": spec["sources"],
            "declarations": spec["declarations"],
        }
        if spec.get("goals"):
            arguments["design"] = spec["goals"]
        async with stdio_client(self.server()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                reply = await session.call_tool(PREPARE, arguments)
                self.assertFalse(reply.is_error, reply.content)
                value = _payload(reply)
        self.assertEqual(value["status"], "needs_review", value.get("issues"))
        self.assertTrue(value["needs_review"])
        self.assertIsInstance(value["electrical_design"], dict)
        self.assertIsInstance(value["capacity"], dict)
        self.assertEqual(value["missing_fields"], [])
        self.assertIsNotNone(value["capacity"].get("target_dc_kw"))
        # Every non-datasheet scalar must be a labelled assumption, not a vendor fact.
        assumption_fields = sorted({d["field"] for d in spec["declarations"]
                                    if d.get("source_id") == "assume"})
        self.assertTrue(assumption_fields, "fixture must declare labelled caller assumptions")
        warning_fields = {w.get("field") for w in value.get("warnings", [])}
        self.assertIn("review", warning_fields)
        for field in assumption_fields:
            self.assertIn(field, warning_fields,
                          "assumption %r must stay in the review warnings" % field)
        # A 0-CLI prepare writes and mutates nothing.
        self.assertEqual(before, self.input_assets())


class SourcedHouseCsvNativeE2E(_FixtureCase):
    """Opt-in real sourced full chain: source prepare -> child native CSV year -> completed resume.

    Budget: EXACTLY TWO public confirm requests, never a third:
      (1) compute ``dry_run=False, confirm=True, resume=False, retry_simulation=False,
          accept_source_review=True`` -> prepare one fresh source + one native child year
          (2024 leap-year -> 8784 hourly rows, CSV-only, no PDF).
      (2) resume  ``dry_run=False, confirm=True, resume=True, retry_simulation=False,
          accept_source_review=True`` -> ONLY after (1) returns completed/needs_review with
          native consumption verified; revalidates the same artifact with no new run.
    A blocked compute raises before (2); resume never retries; no loop, no extra native call.
    """

    def setUp(self):
        if os.name != "nt":
            self.skipTest("native sourced CSV delivery contract is Windows-only")
        if os.environ.get("PVSYST_CONTRACT_REAL_NATIVE") != "1":
            self.skipTest("Set PVSYST_CONTRACT_REAL_NATIVE=1 to opt into the real-native sourced gate")
        self.load_fixture()
        if self.spec.get("design", {}).get("year") != 2024:
            self.skipTest("The CSV 8784-hour gate requires a 2024 leap-year MET fixture")

    def test_sourced_csv_native_confirm_then_completed_resume(self):
        asyncio.run(self._check())

    async def _check(self):
        before_assets = self.input_assets()
        spec = self.fresh_spec()
        spec["report_pdf"] = False  # explicit CSV-only: fresh_spec() omits it, default None would emit a PDF
        async with stdio_client(self.server()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # (1) compute: exactly one cold sourced confirm, no retry and no resume folded in.
                reply = await session.call_tool(
                    TOOL, {"spec": spec, "dry_run": False, "confirm": True,
                           "resume": False, "retry_simulation": False,
                           "accept_source_review": True})
                self.assertFalse(reply.is_error, reply.content)
                value = _payload(reply)
                self.assertIn(value.get("status"), ("completed", "needs_review"), value.get("issues"))
                self.assertTrue(value.get("native_consumption_verified"), value)
                # Source review is retained, never auto-approved.
                self.assertTrue(value["source_preparation"]["needs_review"], value["source_preparation"])
                source_manifest = Path(value["source_preparation"]["manifest"])
                self.assertTrue(source_manifest.is_file())
                source_manifest_bytes = source_manifest.read_bytes()

                house = value["house"]
                self.assertIn(house.get("status"), ("completed", "needs_review"), house.get("issues"))
                self.assertTrue(house.get("native_consumption_verified"), house)
                self.assertEqual(house.get("hourly_rows"), 8784)

                csv_path = house.get("artifacts", {}).get("csv")
                self.assertIsInstance(csv_path, str)
                self.assertTrue(csv_path.lower().endswith(".csv"))
                self.assertTrue(Path(csv_path).is_file() and Path(csv_path).stat().st_size > 0)
                # CSV-only: no PDF in any public projection and no PDF artifact on disk.
                self.assertNotIn("pdf", house.get("artifacts", {}))
                self.assertEqual(set(house.get("output_guards", {})), {"csv"})
                self.assertFalse(Path(csv_path).with_suffix(".pdf").exists())
                compute_runs = len(house.get("runs", []))
                csv_before = Path(csv_path).read_bytes()

                # (2) completed resume: the SECOND and final public request, only after success.
                resumed = await session.call_tool(
                    TOOL, {"spec": spec, "dry_run": False, "confirm": True,
                           "resume": True, "retry_simulation": False,
                           "accept_source_review": True})
                self.assertFalse(resumed.is_error, resumed.content)
                after = _payload(resumed)
                self.assertIn(after.get("status"), ("completed", "needs_review"), after.get("issues"))
                self.assertTrue(after.get("native_consumption_verified"), after)
                self.assertTrue(after["source_preparation"]["needs_review"])
                house2 = after["house"]
                self.assertTrue(house2.get("native_consumption_verified"), house2)
                self.assertEqual(house2.get("hourly_rows"), 8784)
                # Same artifact, byte-identical CSV, still no PDF anywhere.
                self.assertEqual(house2.get("artifacts", {}).get("csv"), csv_path)
                self.assertEqual(Path(csv_path).read_bytes(), csv_before)
                self.assertEqual(set(house2.get("output_guards", {})), {"csv"})
                self.assertNotIn("pdf", house2.get("artifacts", {}))
                self.assertFalse(Path(csv_path).with_suffix(".pdf").exists())
                # Completed resume runs no new simulation and appends no new run.
                self.assertEqual(len(house2.get("runs", [])), compute_runs)
                # The prepared source checkpoint is byte-identical across the completed resume.
                self.assertEqual(source_manifest.read_bytes(), source_manifest_bytes)

        # Read-only input assets are byte-identical after compute AND resume (whole-file, not size).
        self.assertEqual(before_assets, self.input_assets())


if __name__ == "__main__":
    unittest.main()

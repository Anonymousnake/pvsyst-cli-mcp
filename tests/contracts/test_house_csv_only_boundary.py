"""Public CSV-only house delivery boundary for the ``report_pdf`` switch.

``report_pdf`` is the CSV-only selector on the two public house tools
(``pvsyst_run_house_task`` and, by passthrough, ``pvsyst_run_sourced_house_task``).
It is an optional ``StrictBool``:

* omitted / ``None`` / ``True`` keep the ORIGINAL full CSV+PDF report (the legacy
  PDF default stays ``True``);
* ``False`` selects a CSV-only hourly run that reuses the existing plain-CSV
  ``out_csv``-only path (no ``-rpf``), i.e. the ``_is_plain_csv_run`` window-denial
  entry, and produces the hourly CSV only -- no PDF file, no PDF output/guard entry.

Only externally observable public behavior through the real stdio MCP server is
pinned here: the published wire field, strict-boolean rejection (no truthy
string/number coercion), the request serialization that keeps the old shape when
the flag is omitted (``exclude_none`` -> no new key) versus persisting ``False``,
a 0-CLI dry-plan proof for both modes, and one opt-in real-native full-house
CSV-only E2E (2024 leap-year MET -> 8784 hourly rows, PDF absent, output mode
consistent, source bytes unchanged) followed by a completed-CSV-only resume that
re-verifies the same artifact with no new simulation, still no PDF and a
byte-identical CSV. No internal helper/class, call order, Mock, internal
checkpoint field/order or implementation snapshot is bound.

The dry contract exercises the pure native import preview (``scene_import_method=
"native"``) so it launches no CLI at all; the acceptance fixture must be a native
house recipe. The E2E then runs that same native import plus the CSV-only CLI year.

This phase only PREPARES/COMPILES the contract. Default is skip, never green; the
real native execution is a later, separately budgeted H lease and is NOT requested
here. The single native E2E method below is budgeted at exactly TWO public confirm
requests: (1) the H compute ``confirm=True, resume=False`` and (2) a completed
CSV-only resume ``confirm=True, resume=True`` issued only after (1) reports a
completed/needs_review run with native consumption verified. A blocked/failed
compute never resumes, resume never retries or appends a second run, and no loop or
extra native call is made.

Env (all required, else skip -- nothing runs by default):
  PVSYST_CONTRACT_CLI=<path>         real installed PVsystCLI.exe
  PVSYST_CONTRACT_WORKSPACE=<dir>    real PVsyst8.1_Data workspace with the fixture source
  PVSYST_CONTRACT_HOUSE_JSON=<file>  full HouseTaskRequest body (source_project/source_variant/
                                     new_project/variant_name/year=2024/house/layout/module_geometry,
                                     optional shading_mode/scene_import_method). ``report_pdf`` and a
                                     unique ``new_project`` are owned by this test.
  PVSYST_CONTRACT_REAL_NATIVE=1      opt-in for the real native E2E class only.
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
TOOL = "pvsyst_run_house_task"


def _payload(reply):
    return json.loads("\n".join(item.text for item in reply.content if hasattr(item, "text")))


def _find_property(schema, key):
    """Locate a named property subschema anywhere in a JSON schema tree."""
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
    """Collect the JSON types a (possibly anyOf) property schema permits."""
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
    """Loads the explicit acceptance fixture; skips cleanly when it is absent."""

    def load_fixture(self):
        cli = os.environ.get("PVSYST_CONTRACT_CLI")
        workspace = os.environ.get("PVSYST_CONTRACT_WORKSPACE")
        house_json = os.environ.get("PVSYST_CONTRACT_HOUSE_JSON")
        if not cli or not Path(cli).is_file():
            self.skipTest("Set PVSYST_CONTRACT_CLI to a real installed PVsystCLI.exe")
        if not workspace or not Path(workspace).is_dir():
            self.skipTest("Set PVSYST_CONTRACT_WORKSPACE to a real PVsyst8.1_Data workspace")
        if not house_json or not Path(house_json).is_file():
            self.skipTest("Set PVSYST_CONTRACT_HOUSE_JSON to the fixture HouseTaskRequest body")
        try:
            spec = json.loads(Path(house_json).read_text(encoding="utf-8"))
        except ValueError as exc:
            self.fail("PVSYST_CONTRACT_HOUSE_JSON is not valid JSON: %s" % exc)
        if not isinstance(spec, dict) or not all(
                spec.get(key) for key in ("source_project", "source_variant", "new_project")):
            self.fail("PVSYST_CONTRACT_HOUSE_JSON needs source_project/source_variant/new_project")
        if spec.get("scene_import_method") not in (None, "native"):
            self.skipTest("CSV-only contract targets the native import route; fixture declares %r"
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

    def source_bytes(self):
        """Byte snapshot of the read-only source project plus every workspace SIT/MET."""
        paths = [self.workspace / "Projects" / self.spec["source_project"],
                 self.workspace / "Projects" / self.spec["source_variant"]]
        for folder, suffix in (("Sites", ".sit"), ("Meteo", ".met")):
            directory = self.workspace / folder
            if directory.is_dir():
                paths += [p for p in directory.rglob("*")
                          if p.is_file() and p.suffix.lower() == suffix]
        return {p: p.read_bytes() for p in paths if p.is_file()}

    def fresh_spec(self, **changes):
        """A request whose working project is always new (dry runs write nothing)."""
        stem = Path(self.spec["new_project"]).stem
        value = copy.deepcopy(self.spec)
        value.pop("report_pdf", None)
        value["new_project"] = "%s-%s.PRJ" % (stem, uuid.uuid4().hex[:10])
        value.update(changes)
        return value


class HouseCsvOnlyWireContract(_FixtureCase):
    """Public wire type + 0-CLI dry plan for ``report_pdf``; no native execution."""

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
                subschema = _find_property(tool.input_schema, "report_pdf")
                self.assertIsNotNone(subschema, "report_pdf must be published on the public tool schema")
                self.assertLessEqual({"boolean", "null"}, _permitted_types(subschema),
                                     "report_pdf must be a nullable boolean")
                if "default" in subschema:
                    self.assertIsNone(subschema["default"], "report_pdf default must be None")

                # Both specs expose the identical wire field; the sourced tool is never called
                # here (its dry path licenses first), only its published schema is read.
                sourced = next((t for t in tools if t.name == "pvsyst_run_sourced_house_task"), None)
                if sourced is not None:
                    sourced_subschema = _find_property(sourced.input_schema, "report_pdf")
                    self.assertIsNotNone(sourced_subschema,
                                         "report_pdf must be published on the sourced tool schema")
                    self.assertLessEqual({"boolean", "null"}, _permitted_types(sourced_subschema))

                # None/nullable is the old default; a truthy string/number is never coerced.
                for bad in ("false", "true", "False", 0, 1, [], {}):
                    reply = await session.call_tool(TOOL, {"spec": dict(self.fresh_spec(), report_pdf=bad)})
                    self.assertTrue(reply.is_error, (bad, reply.content))

    def test_report_pdf_dry_plan_serialization(self):
        asyncio.run(self._check_dry())

    async def _check_dry(self):
        before_sources = self.source_bytes()
        before_projects = sorted(self.workspace.glob("Projects/*"))
        before_results = sorted(self.workspace.glob("Results/*"))
        base = self.fresh_spec(scene_import_method="native")
        async with stdio_client(self.server()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def dry(payload):
                    reply = await session.call_tool(TOOL, {"spec": payload})
                    self.assertFalse(reply.is_error, reply.content)
                    return _payload(reply)

                # One fresh working name is reused: every dry plan below writes nothing, so
                # their request summaries differ only by the report_pdf key itself.
                omitted = await dry(base)
                explicit_none = await dry(dict(base, report_pdf=None))
                explicit_true = await dry(dict(base, report_pdf=True))
                explicit_false = await dry(dict(base, report_pdf=False))

        for value in (omitted, explicit_none, explicit_true, explicit_false):
            self.assertEqual(value.get("status"), "ready", value.get("issues"))
            self.assertFalse(value.get("native_consumption_verified"), value)

        # Omitted keeps the legacy request shape: no new key. None collapses to the
        # same shape (exclude_none), so the old canonical/checkpoint payload is intact.
        self.assertNotIn("report_pdf", omitted["request_summary"])
        self.assertNotIn("report_pdf", explicit_none["request_summary"])
        self.assertEqual(omitted["request_summary"], explicit_none["request_summary"])

        # True keeps the original PDF plan; False is persisted and selects CSV-only.
        self.assertIs(explicit_true["request_summary"].get("report_pdf"), True)
        self.assertIs(explicit_false["request_summary"].get("report_pdf"), False)

        # A dry plan writes nothing and never mutates the read-only sources.
        self.assertEqual(before_projects, sorted(self.workspace.glob("Projects/*")))
        self.assertEqual(before_results, sorted(self.workspace.glob("Results/*")))
        self.assertEqual(before_sources, self.source_bytes())


class HouseCsvOnlyNativeE2E(_FixtureCase):
    """Opt-in real native full-house CSV-only delivery + completed CSV-only resume.

    Budget: this single method issues EXACTLY TWO public confirm requests against the
    real tool, and never a third:
      (1) H compute  ``dry_run=False, confirm=True, resume=False, retry_simulation=False``
          -> one real native CSV-only run (2024 leap-year MET -> 8784 hourly rows, no PDF).
      (2) H resume   ``dry_run=False, confirm=True, resume=True,  retry_simulation=False``
          -> issued ONLY after (1) returns completed/needs_review with native consumption
             verified. A completed checkpoint is revalidated; it must run NO new
             GUI/simulation and append NO new run (0 original CLI on resume).
    A blocked/failed (1) never resumes (its assertions raise first); resume never retries
    (retry stays False); there is no loop, no scenario-based re-run and no extra call.
    Only public projections are bound (status/artifacts/output_guards/runs/hourly_rows and
    the on-disk CSV); no internal checkpoint field naming/order is treated as contract.
    """

    def setUp(self):
        if os.name != "nt":
            self.skipTest("native CSV-only delivery contract is Windows-only")
        if os.environ.get("PVSYST_CONTRACT_REAL_NATIVE") != "1":
            self.skipTest("Set PVSYST_CONTRACT_REAL_NATIVE=1 to opt into the real-native CSV-only gate")
        self.load_fixture()
        if self.spec.get("year") != 2024:
            self.skipTest("The CSV-only 8784-hour gate requires a 2024 leap-year MET fixture")

    def test_csv_only_full_house_confirm_then_completed_resume(self):
        asyncio.run(self._check())

    async def _check(self):
        before_sources = self.source_bytes()
        # Concrete author source to preserve byte-exactly: the real PRJ + its VC0 variant
        # (the class helper's Projects/<variant> path does not resolve A2S2_R4.VC0) + all SIT/MET.
        variant_file = self.workspace / "Projects" / (
            Path(self.spec["source_project"]).stem + "." + self.spec["source_variant"])
        guarded = [self.workspace / "Projects" / self.spec["source_project"], variant_file]
        for folder, suffix in (("Sites", ".sit"), ("Meteo", ".met")):
            directory = self.workspace / folder
            if directory.is_dir():
                guarded += [p for p in directory.rglob("*")
                            if p.is_file() and p.suffix.lower() == suffix]
        before_guarded = {p: p.read_bytes() for p in guarded if p.is_file()}
        self.assertIn(variant_file, before_guarded,
                      "author source variant must exist to be guarded")
        spec = self.fresh_spec(report_pdf=False, scene_import_method="native")
        async with stdio_client(self.server()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # (1) H compute: exactly one cold confirm, no retry and no resume folded in.
                reply = await session.call_tool(
                    TOOL, {"spec": spec, "dry_run": False, "confirm": True,
                           "resume": False, "retry_simulation": False})
                self.assertFalse(reply.is_error, reply.content)
                value = _payload(reply)

                # The compute must really complete before any resume attempt is made.
                self.assertIn(value.get("status"), ("completed", "needs_review"), value.get("issues"))
                self.assertTrue(value.get("native_consumption_verified"), value)
                self.assertEqual(value.get("hourly_rows"), 8784)

                csv_path = value.get("artifacts", {}).get("csv")
                self.assertIsInstance(csv_path, str)
                self.assertTrue(csv_path.lower().endswith(".csv"))
                self.assertTrue(Path(csv_path).is_file() and Path(csv_path).stat().st_size > 0)

                # No PDF in any public projection and no PDF artifact on disk (CSV-only).
                self.assertIsNone(value.get("pdf"))
                self.assertNotIn("pdf", value.get("artifacts", {}))
                self.assertEqual(set(value.get("output_guards", {})), {"csv"})
                self.assertFalse(value.get("runs", [{}])[-1].get("pdf"))
                self.assertFalse(Path(csv_path).with_suffix(".pdf").exists())
                compute_runs = len(value.get("runs", []))
                csv_before = Path(csv_path).read_bytes()

                # (2) H completed resume: the SECOND and final public request, only after the
                # compute succeeded. Identical spec + resume=True; retry stays False.
                resumed = await session.call_tool(
                    TOOL, {"spec": spec, "dry_run": False, "confirm": True,
                           "resume": True, "retry_simulation": False})
                self.assertFalse(resumed.is_error, resumed.content)
                after = _payload(resumed)

                # A completed CSV-only checkpoint revalidates without a new simulation.
                self.assertIn(after.get("status"), ("completed", "needs_review"), after.get("issues"))
                self.assertTrue(after.get("native_consumption_verified"), after)
                self.assertEqual(after.get("hourly_rows"), 8784)

                # Same CSV artifact, byte-identical content, still no PDF anywhere.
                self.assertEqual(after.get("artifacts", {}).get("csv"), csv_path)
                self.assertTrue(Path(csv_path).is_file())
                self.assertEqual(Path(csv_path).read_bytes(), csv_before)
                self.assertEqual(set(after.get("output_guards", {})), {"csv"})
                self.assertIsNone(after.get("pdf"))
                self.assertNotIn("pdf", after.get("artifacts", {}))
                self.assertFalse(Path(csv_path).with_suffix(".pdf").exists())

                # The completed resume must not retry: no new attempt/run is appended.
                self.assertEqual(len(after.get("runs", [])), compute_runs)

        # Source-byte guard: every read-only source asset is byte-identical (not just size/hash).
        # The concrete author PRJ + VC0 + SIT/MET must be byte-exact after compute AND resume.
        self.assertEqual(before_guarded, {p: p.read_bytes() for p in guarded if p.is_file()})
        self.assertEqual(before_sources, self.source_bytes())


if __name__ == "__main__":
    unittest.main()

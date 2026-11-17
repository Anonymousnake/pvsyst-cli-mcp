"""Existing house CSV wire and dry-plan cases using the shared synthetic project."""
import asyncio
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from test_house_csv_only_boundary import (
    _FixtureCase, _payload, _find_property, _permitted_types, TOOL,
)
from test_per_source_pan_geometry_boundary import _Workspace, HET, _tables, _house


class HouseCsvOnlyWireContract(_FixtureCase):
    """Public wire type + 0-CLI dry plan for ``report_pdf``; no native execution."""

    def setUp(self):
        ws = _Workspace(HET, with_site_met=True)
        self.addCleanup(ws.cleanup, True)
        self.workspace, self.cli = ws.root, str(ws.cli)
        tables = _tables(HET)
        self.spec = {"source_project": "Demo.PRJ", "source_variant": "VC1",
                     "new_project": "Delivery.PRJ", "variant_name": "DeliveryHouse",
                     "year": 2020, "house": _house(tables, HET),
                     "layout": {"tables": tables}, "scene_import_method": "native"}

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


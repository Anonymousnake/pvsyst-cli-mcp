"""In-memory MCP integration tests for tool schemas, results, briefs, and window policy."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest

from pvsyst_auto.api.schemas import HouseTaskRequest, SourcedHouseTaskRequest
from tests.integration._session import decode_reply, mcp_session


SCENE = """  PVObject_Ombrage=pvShading
    Flags=$00000100
    ListeObjets, list of=1 TShdObject
      PVObject_1=pvShdGenObject
        Comment=Protocol fixture
      End of PVObject pvShdGenObject
    End of ListeObjets
    Groups, list of=1
      PVObject_=pvShdObjectGroup
        Comment=Group metadata
      End of PVObject pvShdObjectGroup
    End of Groups
  End of PVObject pvShading
"""

BATCH_SUMMARY = ("Ident;Create hourly;Error;E_Grid;PR\n"
                 "SIM_1;sim_1.csv;;29624;0.8105\n"
                 "SIM_2;sim_2.csv;;29564;0.8083\n")


def _variant(scene):
    return ("PVObject_=pvVCalcul\n  Version=8.1.6\n  Comment=Read test\n"
            "  PVObject_=pvSubArray\n    SubArrayId=1\n"
            "  End of PVObject pvSubArray\n" + scene + "End of PVObject pvVCalcul\n")


class McpToolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_scene_list_excludes_group_metadata_and_preserves_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "Projects"
            projects.mkdir()
            (projects / "READ.PRJ").write_text("PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
            scenarios = {"VC0": "", "VC1": SCENE, "VC2": SCENE.replace("list of=1 TShdObject", "list of=2 TShdObject"), "VC3": SCENE + SCENE}
            for name, scene in scenarios.items():
                (projects / f"READ.{name}").write_text(_variant(scene), encoding="utf-8")
            originals = {p: p.read_bytes() for p in projects.iterdir()}
            cli = root / "PVsystCLI.exe"
            cli.write_bytes(b"fake")
            async with mcp_session(root, cli) as session:
                for name in scenarios:
                    err, data = decode_reply(await session.call_tool("pvsyst_get_variant_parameters", {"project": "READ.PRJ", "variant": name}))
                    self.assertFalse(err, data)
                    scene = data["shading_scene"]
                    if name == "VC1":
                        self.assertEqual((scene["mode"], scene["object_count"], scene["object_types"]), ("active", 1, ["pvShdGenObject"]))
                    else:
                        self.assertIsNone(scene["mode"])
                        self.assertIn("reason" if name == "VC0" else "issue", scene)
            self.assertTrue(all(p.read_bytes() == raw for p, raw in originals.items()))

    async def test_report_pages_preflight_stops_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = root / "PVsystCLI.exe"
            cli.write_bytes(b"fake")
            async with mcp_session(root, cli) as session:
                tools = {item.name: item for item in (await session.list_tools()).tools}
                self.assertIn("pvsyst_simulate_design", tools)
                self.assertNotIn("pvsyst_simulate_template", tools)
                design_schema = tools["pvsyst_simulate_design"].input_schema
                defs = design_schema.get("$defs") or {}
                self.assertIn("report_pages", defs.get("SingleDesignRequest", {}).get("properties", {}))
                for pages in (["summary", "losses"], [], ["SUMMARY"], [1], "summary"):
                    args = {"spec": {"report_pages": pages}}
                    err, data = decode_reply(await session.call_tool("pvsyst_simulate_design", args))
                    if isinstance(pages, str) or pages == [1]:
                        self.assertTrue(err)
                        continue
                    self.assertFalse(err, data)
                    self.assertEqual(data["status"], "blocked")
                    page_issues = [item for item in data["issues"] if item.get("field") == "report_pages"]
                    if pages == ["summary", "losses"]:
                        self.assertFalse(page_issues)
                    else:
                        self.assertTrue(page_issues)
            self.assertEqual(set(root.iterdir()), {cli})

    async def test_result_and_batch_reading_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = root / "Results"
            batch = root / "UserBatch"
            results.mkdir()
            batch.mkdir()
            cli = root / "PVsystCLI.exe"
            cli.write_bytes(b"fake")
            rows = [("01/01/24 00:00", "1", "0.5"), ("1/1/24 1:0", "2", "0.6"), ("29/02/24 23:59", "", "0.7"),
                    ("31/12/68 23:00", "3", ""), ("01/01/69 00:00", "4", "0.8"), ("29/02/23 12:00", "999", "1"), ("2024-01-01T00:00", "999", "1")]
            for name, sep in (("semicolon.csv", ";"), ("comma.csv", ",")):
                text = sep.join(("date", "E_Grid", "PR")) + "\n" + sep.join(("", "kW", "ratio")) + "\n\n"
                text += "\n".join(sep.join(r) for r in rows) + "\n"
                (results / name).write_text(text, encoding="utf-8-sig")
            malformed = "date;E_Grid;PR\n;kW;ratio\n01/01/24 00:00;1;0.5\n29/02/23 00:00;2;0.5\n"
            for name in ("strict-a.csv", "strict-b.csv"):
                (results / name).write_text(malformed, encoding="utf-8")
            indexed = "_DEMO_BatchResults_0.CSV"
            unindexed = "_DEMO_BatchResults.CSV"
            for name in (indexed, unindexed, "notes.csv"):
                (batch / name).write_text(BATCH_SUMMARY, encoding="utf-8")
            async with mcp_session(root, cli) as session:
                for name in ("semicolon.csv", "comma.csv"):
                    err, page = decode_reply(await session.call_tool("pvsyst_read_rows", {"csv_name": name, "offset": 1, "limit": 2}))
                    self.assertFalse(err, page)
                    self.assertEqual(page["total"], 5)
                    self.assertEqual(page["rows"], [["2024-01-01 01:00:00", 2.0, 0.6], ["2024-02-29 23:59:00", None, 0.7]])
                    err, summary = decode_reply(await session.call_tool("pvsyst_read_results", {"csv_name": name}))
                    self.assertFalse(err, summary)
                    self.assertEqual((summary["rows"], summary["summary"]["E_Grid"]["sum"], summary["summary"]["E_Grid"]["missing_samples"]), (5, 10, 1))
                err, msg = decode_reply(await session.call_tool("pvsyst_read_results", {"csv_name": "strict-a.csv", "compare_to": "strict-b.csv"}))
                self.assertTrue(err)
                self.assertIn("Invalid timestamp", msg)
                for name in (indexed, unindexed):
                    err, data = decode_reply(await session.call_tool("pvsyst_read_batch_results", {"summary_name": name}))
                    self.assertFalse(err, data)
                    self.assertEqual([r["id"] for r in data["scenarios"]], ["SIM_1", "SIM_2"])
                for bad in ("notes.csv", "_DEMO_BatchParams_0.CSV", "../" + indexed):
                    err, _ = decode_reply(await session.call_tool("pvsyst_read_batch_results", {"summary_name": bad}))
                    self.assertTrue(err)

    async def test_engineering_brief_and_sourced_preserve_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = root / "PVsystCLI.exe"
            cli.write_bytes(b"fake")
            brief = {
                "sources": [{"id": "report", "title": "120MW Brief", "locator": "p14"}],
                "claims": [
                    {"id": "m", "field": "module_count", "scope": "project", "value": 218690, "unit": "count", "source_id": "report", "pdf_page": 15, "excerpt": "218690"},
                    {"id": "s", "field": "modules_per_string", "scope": "project", "value": 26, "unit": "count", "source_id": "report", "pdf_page": 15, "excerpt": "26"},
                    {"id": "p", "field": "module_power_w", "scope": "project", "value": 550, "unit": "W", "source_id": "report", "pdf_page": 15, "excerpt": "550"},
                    {"id": "i", "field": "inverter_count", "scope": "project", "value": 320, "unit": "count", "source_id": "report", "pdf_page": 15, "excerpt": "320"},
                    {"id": "u", "field": "inverter_power_kw", "scope": "project", "value": 300, "unit": "kW", "source_id": "report", "pdf_page": 15, "excerpt": "300"},
                ],
                "blocks": [{"id": "b1", "count": 24, "strings_per_block": 270, "inverters_per_block": 10, "source_id": "report", "pdf_page": 105, "excerpt": "24 x 270 x 10"}],
            }
            house = {"kind": "mesh", "meshes": [{"name": "m", "vertices": [[0, 0, 0], [1, 0, 0], [1, 1, 0]], "triangles": [[0, 1, 2]]}]}
            layout = {"module_rows": 1, "module_columns": 1, "orientation": "portrait", "row_spacing_m": 0, "column_spacing_m": 0, "origin_m": [0, 0, 0], "roof_mesh": "m"}
            async with mcp_session(root, cli, tool_groups="all") as session:
                err, data = decode_reply(await session.call_tool("pvsyst_review_engineering_brief", {"brief": copy.deepcopy(brief)}))
                self.assertFalse(err, data)
                self.assertEqual(data["status"], "blocked")
                checks = {item["code"]: item for item in data["checks"]}
                self.assertEqual(checks["whole_strings"]["full_strings"], 8411)
                self.assertEqual(checks["module_dc_capacity"]["calculated"], 120279.5)
                self.assertNotIn("engineering_certified", data)
                # Preserve source hardware mode reaches native_preflight and writes nothing
                spec = {"new_project": "House.PRJ", "variant_name": "v", "house": house, "layout": layout,
                        "prepare_mode": "preserve_source_hardware", "source_project": "Src.PRJ", "source_variant": "Src.VC0", "design": {"year": 1990}}
                err, preserve = decode_reply(await session.call_tool("pvsyst_run_sourced_house_task", {"spec": spec, "dry_run": True}))
                self.assertFalse(err, preserve)
                self.assertEqual((preserve["status"], preserve["stage"]), ("blocked", "native_preflight"))
                self.assertEqual(list(root.iterdir()), [cli])

    def test_window_policy_and_obstacle_schemas(self):
        for model in (HouseTaskRequest, SourcedHouseTaskRequest):
            self.assertIsNone(model.model_fields["allow_window_creation"].default)
            self.assertIsNone(model.model_fields["report_pdf"].default)
            self.assertIsNone(model.model_fields["scene_import_method"].default)
            self.assertEqual(model.model_construct(allow_window_creation=False).model_dump(exclude_none=True)["allow_window_creation"], False)
            self.assertNotIn("allow_window_creation", model.model_construct(allow_window_creation=None).model_dump(exclude_none=True))
            self.assertNotIn("report_pdf", model.model_construct(report_pdf=None).model_dump(exclude_none=True))
            with self.assertRaises(Exception):
                model(source_project="A.PRJ", source_variant="VC0", new_project="B.PRJ", variant_name="V",
                      year=2020, house={"kind": "mesh", "meshes": []}, layout={}, report_pdf="false")
        valid_obs = [
            {"kind": "building", "name": "B1", "origin_m": [0, -10, 0], "length_m": 6, "width_m": 4, "height_m": 5},
            {"kind": "tree", "name": "T1", "origin_m": [5, -8, 0], "top_height_m": 8, "middle_height_m": 6,
             "bottom_height_m": 3, "trunk_height_m": 2, "crown_diameter_m": 4, "trunk_diameter_m": 0.4},
            {"kind": "wall", "name": "W1", "start_m": [0, -3, 0], "end_m": [8, -3, 0], "height_m": 1.5},
            {"kind": "mesh", "name": "M1", "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]], "triangles": [[0, 1, 2]]},
        ]
        parsed = HouseTaskRequest(
            source_project="A.PRJ", source_variant="VC0", new_project="B.PRJ", variant_name="V", year=2020,
            house={"kind": "mesh", "meshes": [{"name": "r", "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]], "triangles": [[0, 1, 2]]}]},
            layout={"module_rows": 1, "module_columns": 1, "orientation": "landscape", "row_spacing_m": 0, "column_spacing_m": 0,
                    "origin_m": [0, 0, 0], "roof_mesh": "r"},
            obstacles=valid_obs,
        )
        self.assertEqual(len(parsed.obstacles), 4)
        with self.assertRaises(Exception):
            HouseTaskRequest(
                source_project="A.PRJ", source_variant="VC0", new_project="B.PRJ", variant_name="V", year=2020,
                house={"kind": "mesh", "meshes": []}, layout={}, obstacles=[{"kind": "tree", "name": "Bad", "unexpected": 1}],
            )


if __name__ == "__main__":
    unittest.main()

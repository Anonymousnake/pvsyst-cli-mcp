"""In-memory MCP integration tests for tool schemas, results, briefs, and window policy."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from pvsyst_auto.api import context as api_context
from pvsyst_auto.api.schemas import (
    CapacityRequest,
    EngineeringRevisionRequest,
    GeneratorUpdates,
    HouseTaskRequest,
    SingleDesignRequest,
    SoilingUpdates,
    SourcedHouseTaskRequest,
)
from tests._fixtures import build_grid_vc0, init_workspace, roof_mesh, write_utf8_crlf
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

    def test_cli_context_recreates_on_env_change_and_rejects_nonfinite_schemas(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws1, ws2 = root / "ws1", root / "ws2"
            ws1.mkdir()
            ws2.mkdir()
            cli1, cli2 = root / "cli1.exe", root / "cli2.exe"
            cli1.write_bytes(b"fake1")
            cli2.write_bytes(b"fake2")
            api_context._cli = None
            self.addCleanup(setattr, api_context, "_cli", None)
            with patch.dict(os.environ, {"PVSYST_CLI": str(cli1), "PVSYST_WORKSPACE": str(ws1)}):
                first = api_context.cli()
                self.assertIs(api_context.cli(), first)
                self.assertEqual(first.workspace, ws1.resolve())
                os.environ["PVSYST_WORKSPACE"] = str(ws2)
                second = api_context.cli()
                self.assertIsNot(second, first)
                self.assertEqual(second.workspace, ws2.resolve())
                os.environ["PVSYST_CLI"] = str(cli2)
                third = api_context.cli()
                self.assertIsNot(third, second)
                self.assertEqual(third.cli, cli2.resolve())
                api_context._cli = None
                fourth = api_context.cli()
                self.assertIsNot(fourth, third)

        for nonfinite in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValidationError):
                SingleDesignRequest(target_dc_kw=nonfinite)
            with self.assertRaises(ValidationError):
                CapacityRequest(target_dc_kw=nonfinite)
            with self.assertRaises(ValidationError):
                SoilingUpdates(mode="constant", yearly_loss_pct=nonfinite)
            with self.assertRaises(ValidationError):
                GeneratorUpdates(operating_power_kw=nonfinite)
            with self.assertRaises(ValidationError):
                EngineeringRevisionRequest(
                    source_project="A.PRJ", source_variant="VC0", new_project="B.PRJ", new_variant="VC1",
                    objective="Check", year=2024, nominal_dc_power_kw=nonfinite, power_source="Spec", changes={},
                )

        gen = GeneratorUpdates(operating_power_kw=12, thresholds={"1": {"VBkUpEncl_syst": 0, "VBkUpDecl_syst": 1}})
        self.assertEqual(gen.operating_power_kw, 12)
        self.assertEqual(gen.thresholds["1"]["VBkUpDecl_syst"], 1)
        with self.assertRaises(ValidationError):
            GeneratorUpdates(operating_power_kw=True)

    async def test_strict_boundary_types_and_workspace_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for folder in ("Projects", "Results", "Models", "Meteo", "Sites", "UserData", "ComposPV/Inverters"):
                (root / folder).mkdir(parents=True)
            cli = root / "PVsystCLI.exe"
            cli.write_bytes(b"fake")
            (root / "Results" / "sample.csv").write_text("date;E_Grid;PR\n;kW;ratio\n01/01/24 00:00;1;0.8\n", encoding="utf-8")
            async with mcp_session(root, cli, tool_groups="all") as session:
                tools = {item.name: item for item in (await session.list_tools()).tools}
                self.assertIn("system unavailability periods/random/removal",
                              tools["pvsyst_update_variant_parameters"].description)

                # Representative strict-type boundary checks (StrictBool, StrictInt, StrictNum, nested collections)
                rejected_calls = (
                    ("pvsyst_license_activate", {"key": "k", "confirm": 1}),
                    ("pvsyst_run_simulation", {"project": "A.PRJ", "variant": "VC0", "csv_name": "out", "allow_window_creation": "false"}),
                    ("pvsyst_read_rows", {"csv_name": "sample.csv", "offset": True}),
                    ("pvsyst_read_rows", {"csv_name": "sample.csv", "limit": 10.0}),
                    ("pvsyst_create_site", {"site_name": "S", "latitude": True, "longitude": 10, "weather_csv_name": "m.csv", "sit_name": "s.sit"}),
                    ("pvsyst_build_monthly_weather", {"filename": "m.csv", "global_h": [True] * 12, "temperature": [20] * 12}),
                    ("pvsyst_update_component", {"component_type": "OND", "filename": "a.OND", "updates": {},
                                                 "curve_updates": {"Converter/ProfilPIO": [[True, 0], [1, 1], [2, 2], [3, 3]]}}),
                    ("pvsyst_update_variant_parameters", {"project": "A.PRJ", "variant": "VC0",
                                                          "subarray_updates": {"NModSerie": True}}),
                )
                for tool_name, arguments in rejected_calls:
                    with self.subTest(tool=tool_name, arguments=arguments):
                        err, msg = decode_reply(await session.call_tool(tool_name, arguments))
                        self.assertTrue(err, msg)

                # Integer values in StrictNum parameters pass schema validation cleanly
                err, monthly = decode_reply(await session.call_tool("pvsyst_build_monthly_weather", {
                    "filename": "monthly_int.csv", "global_h": [100] * 12, "temperature": [20] * 12,
                }))
                self.assertFalse(err, monthly)
                self.assertTrue((root / "Meteo" / "monthly_int.csv").is_file())

                err, sfi = decode_reply(await session.call_tool("pvsyst_build_sfi", {"filename": "check.sfi"}))
                self.assertFalse(err, sfi)
                self.assertTrue((root / "Models" / "check.sfi").is_file())

                # Verify workspace RLock is held across multi-step operations
                current_cli = api_context.cli()
                lock_states = []
                with patch("pvsyst_auto.api.projects.read_house_pv_scene",
                           side_effect=lambda *_a, **_k: (lock_states.append(current_cli._lock._is_owned()),
                                                          {"create_arguments": {"string_layout": {"tables": []}}})[1]):
                    err, blocked = decode_reply(await session.call_tool("pvsyst_import_house_pv_scene", {
                        "scene_name": "s.pvc2", "variant_name": "V", "shading_mode": "linear",
                    }))
                self.assertFalse(err, blocked)
                self.assertEqual(blocked["status"], "blocked")
                self.assertEqual(lock_states, [True])

    async def test_bifacial_and_utility_design_mcp_roundtrip(self):
        from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN, _capacity_spec
        from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT

        with tempfile.TemporaryDirectory() as tmp:
            root = init_workspace(Path(tmp) / "PVsyst8.1_Data")
            cli_exe = Path(tmp) / "PVsystCLI.exe"
            cli_exe.write_bytes(b"fake")
            write_utf8_crlf(root / "Sites" / "Geneva_MN90.SIT", SITE_TEXT)
            write_utf8_crlf(root / "Meteo" / "Geneva_MN90_SYN.MET", MET_TEXT)
            write_utf8_crlf(root / "ComposPV/PVmodules/Lab440.PAN", UTILITY_PAN)
            write_utf8_crlf(root / "ComposPV/Inverters/Lab50.OND", UTILITY_OND)

            async with mcp_session(root, cli_exe) as session:
                # 1) Build bifacial SFI via default tool set
                err, sfi = decode_reply(await session.call_tool("pvsyst_build_sfi", {
                    "filename": "bifi_mcp.sfi", "variables": "bifacial",
                }))
                self.assertFalse(err, sfi)
                self.assertIn("viGlobBak", sfi["variables"])

                # 2) Prepare a zero-template 3D ground_tracker + bifacial project via MCP
                fake_cal = {"issues": [], "warnings": [], "year": 2024, "rows": 8784}
                with patch("pvsyst_auto.native.cli.PVsystCLI.version", return_value="8.1.7"), \
                     patch("pvsyst_auto.native.cli.PVsystCLI.license_info", return_value={"status": "LICENSED"}), \
                     patch("pvsyst_auto.workflows.utility.inspect_met_calendar", return_value=fake_cal):
                    err, prep = decode_reply(await session.call_tool("pvsyst_prepare_design_project", {
                        "spec": {
                            "scope": "ground_tracker",
                            "new_project": "McpTracker.PRJ",
                            "year": 2024,
                            "target_dc_kw": 100.0,
                            "module": {"file_name": "Lab440.PAN"},
                            "inverter": {"file_name": "Lab50.OND"},
                            "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                            "capacity": _capacity_spec(),
                            "ground_layout": {"model_mode": "3d", "pitch_m": 12.0, "shed_count": 6, "module_columns": 19,
                                              "min_phi_deg": -55.0, "max_phi_deg": 55.0, "backtracking": True},
                            "bifacial": {"ground_albedo": 0.30, "bifaciality_factor": 0.80, "height_above_ground_m": 2.4},
                        },
                        "dry_run": False,
                    }))
                self.assertFalse(err, prep)
                self.assertEqual((prep["status"], prep["stage"], prep["prepared"]), ("prepared", "prepared", True))
                self.assertEqual(prep["utility_summary"]["field_type"], "TrackHorizNSAxis")

                # 3) Inspect and update bifacial parameters via MCP
                err, info = decode_reply(await session.call_tool("pvsyst_get_variant_parameters", {
                    "project": "McpTracker.PRJ", "variant": "VC0",
                }))
                self.assertFalse(err, info)
                self.assertEqual(info["orientations"][0]["bifacial"]["model"], "Trackers2D")

                err, upd = decode_reply(await session.call_tool("pvsyst_update_variant_parameters", {
                    "project": "McpTracker.PRJ", "variant": "VC0",
                    "expected_sha256": info["sha256"],
                    "bifacial_updates": {"ground_albedo": 0.42},
                    "dry_run": False,
                }))
                self.assertFalse(err, upd)
                self.assertEqual(upd["bifacial"]["after"]["ground_albedo"], 0.42)

                # 4) Reject unknown ground_layout keys at strict MCP boundary
                err, bad = decode_reply(await session.call_tool("pvsyst_simulate_design", {
                    "spec": {
                        "scope": "ground_sheds", "new_project": "Bad.PRJ", "year": 2024,
                        "target_dc_kw": 100.0, "tilt": 20.0, "azimuth": 0.0,
                        "ground_layout": {"unknown_key": 1},
                    },
                    "dry_run": True,
                }))
                self.assertTrue(err, bad)


REV_FIXTURE = (
    "PVObject_=pvAddress\r\n  Comment=Contract Test Seller\r\n  Version=6.78\r\n"
    "  Flags=$80008022\r\n  Person=Alice Example\r\n  Address=1 Test Street\r\n"
    "  Town=Testville\r\n  Country=Testland\r\nEnd of PVObject pvAddress\r\n"
)
BOM = b"\xef\xbb\xbf"
DEG = "\u00b0"
MOJIBAKE = "\u00c2\u00b0"


def _source_vc(orientations, subarrays):
    return build_grid_vc0(
        comment="Scene Source", version="8.1.6", main_flags="$00", main_flags2=None,
        base_node_wrapper=True, subarrays_wrapper=False,
        orientations=[{"id": i, "tilt": tilt, "azim": az} for i, tilt, az in orientations],
        subarrays=[
            {"id": sid, "orient": orient, "name": f"Array {sid}", "pan": pan, "ond": "Generic_60kW.OND",
             "n_mod": nmod, "n_str": nstr, "flags": "$00"}
            for sid, orient, pan, nmod, nstr in subarrays
        ],
    )


def _roof_mesh(name, ox, oy, tilt_deg, rot_deg):
    return roof_mesh(name, ox, oy, tilt_deg, rot_deg, x_bounds=(-2.0, 25.0), y_bounds=(-2.0, 25.0))


class SceneComponentTests(unittest.IsolatedAsyncioTestCase):
    async def test_rev_sellers_component_crud_and_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sellers = root / "ComposPV" / "Sellers"
            sellers.mkdir(parents=True)
            (root / "Projects").mkdir()
            (sellers / "Retailer.REV").write_bytes(REV_FIXTURE.encode("utf-8"))
            cli = root / "PVsystCLI.exe"
            cli.write_bytes(b"fake")
            async with mcp_session(root, cli) as session:
                err, listed = decode_reply(await session.call_tool("pvsyst_list_components", {"component_type": "REV"}))
                self.assertFalse(err, listed)
                self.assertIn("Retailer.REV", {i["name"] for i in listed["items"]})
                err, got = decode_reply(await session.call_tool("pvsyst_get_component", {"component_type": "REV", "filename": "Retailer.REV"}))
                self.assertFalse(err, got)
                self.assertEqual(got["fields"]["Person"], ["Alice Example"])
                self.assertEqual(got["curves"]["items"], [])
                err, _ = decode_reply(await session.call_tool("pvsyst_update_component", {
                    "component_type": "REV", "filename": "Retailer.REV", "updates": {},
                    "commercial_updates": {"Manufacturer": "X"}, "expected_sha256": got["sha256"]}))
                self.assertTrue(err)
                err, upd = decode_reply(await session.call_tool("pvsyst_update_component", {
                    "component_type": "REV", "filename": "Retailer.REV", "updates": {"Person": "Bob"},
                    "expected_sha256": got["sha256"], "dry_run": False}))
                self.assertFalse(err, upd)
                self.assertIn("Person=Bob", (sellers / "Retailer.REV").read_text(encoding="utf-8"))

    async def test_per_source_pan_geometry_and_same_orientation_owners(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Projects").mkdir()
            (root / "Sites").mkdir()
            (root / "Meteo").mkdir()
            (root / "Projects" / "Demo.PRJ").write_text(
                "PVObject_=pvProject\n  SiteFile=Demo.SIT\n  MeteoFileName=Demo.MET\nEnd of PVObject pvProject\n", encoding="utf-8")
            (root / "Sites" / "Demo.SIT").write_text("PVObject_=pvSite\n  Latitude=45\nEnd of PVObject pvSite\n", encoding="utf-8")
            (root / "Meteo" / "Demo.MET").write_text("PVObject_=pvMeteo\nEnd of PVObject pvMeteo\n", encoding="utf-8")
            (root / "Projects" / "Demo.VC1").write_text(
                _source_vc([(1, 25.0, 20.0), (2, 25.0, -70.0)], [(1, 1, "Mono300.PAN", 30, 1), (2, 2, "Mono440.PAN", 30, 1)]), encoding="utf-8")
            (root / "Projects" / "Demo.VC2").write_text(
                _source_vc([(1, 25.0, 20.0)], [(1, 1, "Mono300.PAN", 18, 2), (2, 1, "Mono300.PAN", 18, 2)]), encoding="utf-8")
            cli = root / "PVsystCLI.exe"
            cli.write_bytes(b"fake")
            het_tables = [
                {"name": "A", "orientation_id": 1, "module_rows": 3, "module_columns": 10, "orientation": "landscape",
                 "row_spacing_m": 0, "column_spacing_m": 0, "origin_m": [0, 0, 0], "roof_mesh": "roof_1",
                 "module_geometry": {"model_name": "Mono 300", "nom_power_wp": 300.0, "width_m": 0.992, "length_m": 1.640, "geometry_depth_m": 0.035}},
                {"name": "B", "orientation_id": 2, "module_rows": 3, "module_columns": 10, "orientation": "landscape",
                 "row_spacing_m": 0, "column_spacing_m": 0, "origin_m": [20, 0, 0], "roof_mesh": "roof_2",
                 "module_geometry": {"model_name": "Mono 440", "nom_power_wp": 440.0, "width_m": 1.052, "length_m": 2.115, "geometry_depth_m": 0.035}},
            ]
            house = {"kind": "mesh", "meshes": [_roof_mesh("roof_1", 0, 0, 25.0, 0.0), _roof_mesh("roof_2", 20, 0, 25.0, 90.0)]}
            async with mcp_session(root, cli, tool_groups="all") as session:
                err, created = decode_reply(await session.call_tool("pvsyst_create_house_pv_scene", {
                    "project": "Demo.PRJ", "variant": "VC1", "output_name": "Mixed.pvc2",
                    "house": house, "layout": {"tables": het_tables}, "dry_run": False}))
                self.assertFalse(err, created)
                self.assertEqual(created["status"], "created", created)
                err, read_scene = decode_reply(await session.call_tool("pvsyst_read_house_pv_scene", {"scene_name": "Mixed.pvc2"}))
                self.assertFalse(err, read_scene)
                self.assertEqual([t["module_geometry"]["model_name"] for t in read_scene["create_arguments"]["layout"]["tables"]], ["Mono 300", "Mono 440"])
                err, imp = decode_reply(await session.call_tool("pvsyst_import_house_pv_scene", {
                    "scene_name": "Mixed.pvc2", "variant_name": "MixedOut", "dry_run": True}))
                self.assertFalse(err, imp)
                self.assertEqual(imp["status"], "ready_to_import", imp)

                same_tables = [
                    {k: v for k, v in het_tables[0].items() if k != "module_geometry"} | {"orientation_id": 1, "module_rows": 18, "module_columns": 2},
                    {k: v for k, v in het_tables[1].items() if k != "module_geometry"} | {"orientation_id": 1, "module_rows": 18, "module_columns": 2, "origin_m": [10, 0, 0]},
                ]
                err, blocked = decode_reply(await session.call_tool("pvsyst_create_house_pv_scene", {
                    "project": "Demo.PRJ", "variant": "VC2", "output_name": "Amb.pvc2",
                    "house": house, "layout": {"tables": same_tables},
                    "module_geometry": copy.deepcopy(het_tables[0]["module_geometry"]), "dry_run": False}))
                self.assertFalse(err, blocked)
                self.assertEqual(blocked["status"], "blocked")
                self.assertIn("ambiguous", " ".join(blocked["issues"]).lower())

    async def test_obstacles_default_native_import_and_headless_dc_cabling_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for sub in ("Projects", "Sites", "Meteo", "UserData", "ComposPV/PVmodules", "ComposPV/Inverters"):
                (root / sub).mkdir(parents=True)
            (root / "Projects" / "Demo.PRJ").write_text(
                "PVObject_=pvProject\n  SiteFile=Demo.SIT\n  MeteoFileName=Demo.MET\nEnd of PVObject pvProject\n", encoding="utf-8")
            (root / "Sites" / "Demo.SIT").write_text("PVObject_=pvSite\n  Latitude=45\nEnd of PVObject pvSite\n", encoding="utf-8")
            (root / "Meteo" / "Demo.MET").write_text("PVObject_=pvMeteo\nEnd of PVObject pvMeteo\n", encoding="utf-8")
            vc = build_grid_vc0(
                comment="Scene Source", version="8.1.6", main_flags="$00100400", main_flags2="$082A",
                mv_nb_transfos=0, base_node_wrapper=True, subarrays_wrapper=False,
                orientations=[{"id": 1, "tilt": 25.0, "azim": 20.0}],
                subarrays=[{
                    "id": 1, "orient": 1, "name": "Array 1", "pan": "Mono600.PAN", "ond": "Generic_60kW.OND",
                    "n_mod": 15, "n_str": 2, "flags": "$001E0402", "flags2": "$20",
                    "extra_lines": ("RSCablageCh=378.105", "RSCablagePC=1.50", "ACWireLength=0.0"),
                }],
            )
            (root / "Projects" / "Demo.VC0").write_text(vc, encoding="utf-8")
            pan = (
                "PVObject_=pvModule\n  Version=7.2\n  Flags=$00900043\n"
                "  PVObject_Commercial=pvCommercial\n    Flags=$0041\n"
                "    Manufacturer=Demo\n    Model=Mono600\n    Width=1.134\n    Height=2.465\n    Depth=0.030\n"
                "  End of PVObject pvCommercial\n"
                "  Technol=mtSiMono\n  NCelS=78\n  NCelP=2\n  NDiode=3\n  SubModuleLayout=slTwinHalfCells\n"
                "  GRef=1000\n  TRef=25.0\n  PNom=600\n  Isc=13.87\n  Voc=55.03\n  Imp=13.26\n  Vmp=45.25\n"
                "  RShunt=1100\n  RSerie=0.255\n  PVObject_IAM=pvIAM\n    Flags=$00\n  End of PVObject pvIAM\n"
                "End of PVObject pvModule\n"
            )
            (root / "ComposPV" / "PVmodules" / "Mono600.PAN").write_text(pan, encoding="utf-8")
            cli = root / "PVsystCLI.exe"
            cli.write_bytes(b"fake")
            house = {"kind": "mesh", "meshes": [_roof_mesh("roof_1", 0, 0, 25.0, 0.0)]}
            layout = {"tables": [{"name": "T1", "module_rows": 3, "module_columns": 10, "orientation": "landscape",
                                  "row_spacing_m": 0, "column_spacing_m": 0, "origin_m": [0, 0, 0], "roof_mesh": "roof_1"}]}
            obstacles = [
                {"kind": "tree", "name": "Oak1", "origin_m": [5.0, -8.0, 0.0],
                 "top_height_m": 9.0, "middle_height_m": 6.5, "bottom_height_m": 3.0,
                 "trunk_height_m": 2.0, "crown_diameter_m": 4.5, "trunk_diameter_m": 0.4},
                {"kind": "wall", "name": "Wall1", "start_m": [0.0, -4.0, 0.0], "end_m": [10.0, -4.0, 0.0], "height_m": 1.5},
            ]
            async with mcp_session(root, cli, tool_groups="all") as session:
                tool_names = {t.name for t in (await session.list_tools()).tools}
                self.assertIn("pvsyst_create_dc_cabling_variant", tool_names)
                self.assertNotIn("pvsyst_create_native_dc_cabling_variant", tool_names)

                err, sc = decode_reply(await session.call_tool("pvsyst_create_house_pv_scene", {
                    "project": "Demo.PRJ", "variant": "VC0", "output_name": "ObsScene.pvc2",
                    "house": house, "layout": layout, "obstacles": obstacles,
                    "module_geometry": {"model_name": "Mono600", "nom_power_wp": 600.0, "width_m": 1.134, "length_m": 2.465, "geometry_depth_m": 0.03},
                    "dry_run": False}))
                self.assertFalse(err, sc)
                self.assertEqual(sc["status"], "created", sc)

                err, imp = decode_reply(await session.call_tool("pvsyst_import_house_pv_scene", {
                    "scene_name": "ObsScene.pvc2", "variant_name": "WithObs", "dry_run": False, "confirm": True}))
                self.assertFalse(err, imp)
                self.assertEqual((imp["status"], imp["method"]), ("native_variant_created", "native"), imp)
                self.assertEqual((imp["obstacle_count"], imp["tree_count"], imp["obstacle_mesh_count"]), (2, 1, 1))

                err, params = decode_reply(await session.call_tool("pvsyst_get_variant_parameters", {"project": "Demo.PRJ", "variant": imp["variant"]}))
                self.assertFalse(err, params)
                self.assertIn("pvShdElemObject", params["shading_scene"]["object_types"])

                err, dc = decode_reply(await session.call_tool("pvsyst_create_dc_cabling_variant", {
                    "project": "Demo.PRJ", "source_variant": "VC0", "new_variant": "VC2",
                    "dc": {"mode": "percent", "value": 1.5}, "dry_run": False, "variant_name": "DC 1.5pct"}))
                self.assertFalse(err, dc)
                self.assertEqual((dc["status"], dc["dc"]["mode"], dc["dc"]["loss_percent"]), ("created", "percent", "1.50"))


class VariantIOTests(unittest.IsolatedAsyncioTestCase):
    async def test_817_syntax_aliases_and_saved_results_readback(self):
        vc = (
            "PVObject_=pvVCalcul\r\n  Version=8.1.7\r\n  Flags=$00104001\r\n  Comment=Fixture\r\n"
            "  Orientations, list of=1 TOrientGroup\r\n  PVObject_=pvOrient\r\n    NoOrient=1\r\n"
            "    Flags=$00\r\n    FieldType=FixedPlane\r\n    FieldTilt=25.0\r\n    FieldAzim=20.0\r\n"
            "    BaseSlope=0\r\n    UsesTiltAver=False\r\n    UsesAzimAver=False\r\n    UsesSlopeAver=False\r\n"
            "  End of TOrientGroup\r\n  End of Orientations\r\n"
            "  PVObject_=pvSubArray\r\n    SubArrayId=1\r\n    NoOrientation=1\r\n    Version=8.1.6\r\n"
            "    PVModule=module.PAN\r\n    GInverter=inverter.OND\r\n    NModSerie=15\r\n    NStringCh=2\r\n"
            "    NInverter=1\r\n    NInvMPPT=1\r\n  End of PVObject pvSubArray\r\n"
            "  PVObject_System=pvSystem\r\n    SystemType=Grid\r\n  End of PVObject pvSystem\r\n"
            "  PVObject_Ageing=pvAgeing\r\n    Flags=$00\r\n  End of TAgeing\r\n"
            "  PVObject_Simul=pvSimulFile\r\n    FileNameWr=Sim.CSV\r\n    VarIdentWr=viGlobInc,viE_Grid\r\n"
            "    Units=uikWh,uiW_m2,\r\n    TypeValW=Hour\r\n  End of TSimulFich\r\n"
            "  VarList, list of=2 TVarSimul\r\n"
            "    VarSimul_1=ShdElec\r\n      VarIdent=viShdElec\r\n      VarType=Energy\r\n"
            "      ValMonth=1.5,2.25,0,0,0,0,0,0,0,0,0,0,\r\n    End of VarSimul=ShdElec\r\n"
            "    VarSimul_2=GlobInc\r\n      VarIdent=viGlobInc\r\n      Day_0101=+0+0+2Ly\r\n"
            "      StoredWeighted=203223.9\r\n    End of VarSimul=GlobInc\r\n"
            "  End of the List VarList\r\n"
            "End of PVObject pvVCalcul\r\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Projects").mkdir()
            (root / "Projects" / "D.PRJ").write_bytes(b"PVObject_=pvProject\r\nEnd of PVObject pvProject\r\n")
            (root / "Projects" / "D.VC0").write_bytes(BOM + vc.encode("utf-8"))
            cli = root / "PVsystCLI.exe"
            cli.write_bytes(b"fake")
            async with mcp_session(root, cli) as session:
                err, default = decode_reply(await session.call_tool("pvsyst_get_variant_parameters", {"project": "D.PRJ", "variant": "VC0"}))
                self.assertFalse(err, default)
                self.assertNotIn("saved_results", default)
                err, full = decode_reply(await session.call_tool("pvsyst_get_variant_parameters", {"project": "D.PRJ", "variant": "VC0", "include_saved_results": True}))
                self.assertFalse(err, full)
                saved = full["saved_results"]
                self.assertTrue(saved["present"])
                self.assertEqual(saved["pv_simul_file"]["file_name_written"], "Sim.CSV")
                self.assertEqual([e["ident"] for e in saved["var_list"]["entries"]], ["ShdElec", "GlobInc"])
                err, updated = decode_reply(await session.call_tool("pvsyst_update_variant_parameters", {
                    "project": "D.PRJ", "variant": "VC0", "expected_sha256": default["sha256"],
                    "variant_name": "Été 25" + DEG, "dry_run": False}))
                self.assertFalse(err, updated)
                raw = (root / "Projects" / "D.VC0").read_bytes()
                self.assertTrue(raw.startswith(BOM))
                self.assertNotIn(MOJIBAKE, raw.decode("utf-8-sig"))

    async def test_economics_dcgrid_and_topology_readback(self):
        econ_vc = (
            "PVObject_=pvVCalcul\n  Version=8.1.0\n  Flags=$00100000\n"
            "  EconomicEval=TEcoEvaluation\n    ProjectLife=20\n    Currency=USD\n"
            "    DiscountRate=0.0100\n    DiscountRate=0.0200\n"
            "    InvestmentCosts, list of=1 InvestmentCost\n      InvestmentCost=1\n      EndTags\n    End of List InvestmentCosts\n"
            "  End of TEcoEvaluation\n"
            "  PVObject_SystemCircuit=pvCircuit\n    Version=8.1.0\n    BaseNode=Start\n"
            "      InjectionPointNode Start;\n        NElements=1\n        SubArrayId=0\n"
            "        Children=Start\n          InverterNode Start;\n            NElements=1\n"
            "            SubArrayId=1\n            FutureMarker=PRESERVE_ME\n"
            "          InverterNode End;\n        Children=End\n"
            "      InjectionPointNode End;\n    BaseNode=End\n  End of PVObject pvCircuit\n"
            "  PVObject_=pvSubArray\n    SubArrayId=1\n    Version=8.1.0\n    PVModule=M.PAN\n    GInverter=I.OND\n"
            "    NModSerie=10\n    NStringCh=2\n    NInverter=1\n    NInvMPPT=1\n    NoOrientation=1\n  End of PVObject pvSubArray\n"
            "  PVObject_System=pvSystem\n    SystemType=Grid\n  End of PVObject pvSystem\n"
            "End of PVObject pvVCalcul\n"
        )
        dcgrid_vc = (
            "PVObject_=pvVCalcul\n  Version=8.0.0\n  Flags=$00010000\n"
            "  PVObject_=pvSubArray\n    SubArrayId=1\n    Version=8.0.0\n    PVModule=Old.PAN\n"
            "    NModSerie=23\n    NStringCh=35\n    NoOrientation=1\n    NInverter=1\n    NInvMPPT=1\n"
            "    LIDLoss=0.00\n  End of PVObject pvSubArray\n"
            "  PVObject_System=pvSystem\n    SystemType=DCGrid\n    TypeRegul=2\n    VNomAlim=650.0\n"
            "  End of PVObject pvSystem\nEnd of PVObject pvVCalcul\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Projects").mkdir()
            (root / "Projects" / "E.PRJ").write_text("PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
            (root / "Projects" / "E.VC0").write_text(econ_vc, encoding="utf-8")
            (root / "Projects" / "E.VC1").write_text(dcgrid_vc, encoding="utf-8")
            (root / "Projects" / "E.VC2").write_text(econ_vc.replace("End of List InvestmentCosts\n", ""), encoding="utf-8")
            cli = root / "PVsystCLI.exe"
            cli.write_bytes(b"fake")
            async with mcp_session(root, cli) as session:
                err, data = decode_reply(await session.call_tool("pvsyst_get_variant_parameters", {
                    "project": "E.PRJ", "variant": "VC0", "include_economics": True, "include_topology": True}))
                self.assertFalse(err, data)
                self.assertEqual(data["economics"]["scalars"]["DiscountRate"], ["0.0100", "0.0200"])
                self.assertNotIn("fields_scope", data["economics"])
                self.assertTrue(data["topology"]["present"])
                self.assertEqual(data["topology"]["roots"][0]["children"][0]["fields"].get("FutureMarker"), ["PRESERVE_ME"])

                err, bad = decode_reply(await session.call_tool("pvsyst_get_variant_parameters", {
                    "project": "E.PRJ", "variant": "VC2", "include_economics": True}))
                self.assertTrue(err)

                err, dc = decode_reply(await session.call_tool("pvsyst_get_variant_parameters", {"project": "E.PRJ", "variant": "VC1"}))
                self.assertFalse(err, dc)
                self.assertEqual(dc["legacy_read_scope"]["system_declarations"]["VNomAlim"], "650.0")
                err, refuse = decode_reply(await session.call_tool("pvsyst_update_variant_parameters", {
                    "project": "E.PRJ", "variant": "VC1", "expected_sha256": dc["sha256"],
                    "subarray_updates": {"LIDLoss": 1.0}}))
                self.assertTrue(refuse if isinstance(refuse, bool) else err)

    async def test_uv_factor_and_pnomac_inmppt_edits(self):
        vc = (
            "PVObject_=pvVCalcul\n  Comment=Edit\n  Version=8.1.0\n  Flags=$050A0001\n"
            "  PVObject_PVMainArray=pvMainArray\n    Flags=$01900400\n    Flags2=$082A\n"
            "    UFactor=25.0\n    UVFactor=1.2\n  End of PVObject pvMainArray\n"
            "  PVObject_=pvSubArray\n    SubArrayId=1\n    Version=8.1.0\n    PVModule=Mono.PAN\n"
            "    GInverter=Inv.OND\n    NModSerie=15\n    NStringCh=2\n    NInverter=1\n"
            "    NInvMPPT=1\n    NoOrientation=1\n    PowerSharingInverterId=-1\n    PNomAC_InMPPT=7.50\n"
            "  End of PVObject pvSubArray\n"
            "  PVObject_System=pvSystem\n    SystemType=Grid\n  End of PVObject pvSystem\n"
            "  ListeObjets, list of=80 PVObject_TShdObject\n"
            "  VarList, list of=1 TVarSimul\n    VarSimul_1=E_Grid\n    End of VarSimul=E_Grid\n  End of the List VarList\n"
            "End of PVObject pvVCalcul\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Projects").mkdir()
            (root / "Projects" / "U.PRJ").write_text("PVObject_=pvProject\n  Comment=Original\nEnd of PVObject pvProject\n", encoding="utf-8")
            (root / "Projects" / "U.VC0").write_text(vc, encoding="utf-8")
            cli = root / "PVsystCLI.exe"
            cli.write_bytes(b"fake")
            async with mcp_session(root, cli, tool_groups="all") as session:
                err, info = decode_reply(await session.call_tool("pvsyst_get_variant_parameters", {"project": "U.PRJ", "variant": "VC0"}))
                self.assertFalse(err, info)
                self.assertEqual(info["subarrays"][0]["inverter_state"]["PNomAC_InMPPT"], 7.50)
                self.assertNotIn("note", info["source_metadata"])

                err, _ = decode_reply(await session.call_tool("pvsyst_update_variant_parameters", {
                    "project": "U.PRJ", "variant": "VC0", "expected_sha256": info["sha256"],
                    "main_array_updates": {"UFactor": 26.0, "UVFactor": 2.0}}))
                self.assertTrue(err)
                err, _ = decode_reply(await session.call_tool("pvsyst_update_variant_parameters", {
                    "project": "U.PRJ", "variant": "VC0", "expected_sha256": info["sha256"],
                    "subarray_id": 1, "subarray_updates": {"PNomAC_InMPPT": 0.004}}))
                self.assertTrue(err)

                err, res = decode_reply(await session.call_tool("pvsyst_update_variant_parameters", {
                    "project": "U.PRJ", "variant": "VC0", "main_array_updates": {"UVFactor": 2.0}}))
                self.assertFalse(err, res)
                self.assertTrue(res["derived_cache"]["var_list_removed"])
                text = (root / "Projects" / "U.VC0").read_text(encoding="utf-8")
                self.assertIn("UVFactor=2.000", text)
                self.assertIn("Flags=$050A0000", text)
                self.assertNotIn("VarList, list of=", text)

                err, renamed = decode_reply(await session.call_tool("pvsyst_update_project_parameters", {
                    "project": "U.PRJ", "project_name": "Updated Title"}))
                self.assertFalse(err, renamed)
                self.assertEqual(renamed["name_change"]["after"], "Updated Title")


if __name__ == "__main__":
    unittest.main()

"""Fast MCP contracts for REV components, heterogeneous PAN geometry, and shared orientation owners."""
from __future__ import annotations

import copy
import math
from pathlib import Path
import tempfile
import unittest

from tests.contracts._session import decode_reply, mcp_session

REV_FIXTURE = (
    "PVObject_=pvAddress\r\n  Comment=Contract Test Seller\r\n  Version=6.78\r\n"
    "  Flags=$80008022\r\n  Person=Alice Example\r\n  Address=1 Test Street\r\n"
    "  Town=Testville\r\n  Country=Testland\r\nEnd of PVObject pvAddress\r\n"
)


def _orient(idx, tilt, az):
    return (f"  PVObject_=pvOrient\n    NoOrient={idx}\n    Flags=$00\n    FieldType=FixedPlane\n"
            f"    FieldTilt={tilt}\n    FieldAzim={az}\n    BaseSlope=0\n"
            "    UsesTiltAver=False\n    UsesAzimAver=False\n    UsesSlopeAver=False\n  End of TOrientGroup\n")


def _subarray(sid, orient, pan, nmod=18, nstr=2):
    return (f"  PVObject_=pvSubArray\n    Comment=Array {sid}\n    SubArrayId={sid}\n"
            f"    NoOrientation={orient}\n    Version=8.1.6\n    Flags=$00\n    PVModule={pan}\n"
            f"    GInverter=Generic_60kW.OND\n    NModSerie={nmod}\n    NStringCh={nstr}\n"
            f"    NStrOrient1={nstr}\n    NInverter=1\n    NInvMPPT=1\n    PowerSharingInverterId=-1\n"
            "  End of PVObject pvSubArray\n")


def _source_vc(orients, arrays):
    branches = ""
    for i, text in enumerate(arrays, 1):
        nstr = next(line.split("=")[1].strip() for line in text.splitlines() if "NStringCh=" in line)
        branches += (
            f"InverterNode Start;\n  NElements=1\n  SubArrayId={i}\n  SubArrayName=Array {i}\n"
            f"  Children=Start\n    StringNode Start;\n      NElements={nstr}\n      SubArrayId={i}\n"
            f"      SubArrayName=Array {i}\n    StringNode End;\n  Children=End\nInverterNode End;\n"
        )
    return ("PVObject_=pvVCalcul\n  Version=8.1.6\n  Flags=$00100000\n  Comment=Scene Source\n"
            "  PVObject_SiteSimul=pvSite\n    NomF=Demo.SIT\n    Site=Demo\n    Latitude=45.0\n    Longitude=5.0\n    Altitude=10.0\n  End of PVObject pvSite\n"
            "  PVObject_MeteoSimul=pvMeteo\n    NomF=Demo.MET\n    SiteM=Demo\n"
            "    PVObject_SiteMet=pvSite\n      NomF=Demo.SIT\n      Site=Demo\n      Latitude=45.0\n      Longitude=5.0\n      Altitude=10.0\n    End of PVObject pvSite\n"
            "  End of PVObject pvMeteo\n"
            + f"  Orientations, list of={len(orients)} TOrientGroup\n"
            + "".join(orients) + "  End of Orientations\n"
            + "  PVObject_PVMainArray=pvMainArray\n    Flags=$00\n"
            + "    PVObject_SystemCircuit=pvCircuit\n      Version=8.1.6\n      BaseNode=Start\n"
            + "        InjectionPointNode Start;\n          NElements=1\n          SubArrayId=0\n"
            + "          Children=Start\n" + branches + "          Children=End\n"
            + "        InjectionPointNode End;\n      BaseNode=End\n    End of PVObject pvCircuit\n"
            + "".join(arrays) + "  End of PVObject pvMainArray\n"
            + "  PVObject_Ombrage=pvShading\n    Flags=$00\n  End of PVObject pvShading\n"
            + "  PVObject_System=pvSystem\n    SystemType=Grid\n  End of PVObject pvSystem\n"
            + "End of PVObject pvVCalcul\n")


def _roof_mesh(name, ox, oy, tilt_deg, rot_deg):
    t, r = math.radians(tilt_deg), math.radians(rot_deg)
    def pt(x, y):
        dy, dz = y * math.cos(t), y * math.sin(t)
        return [round(ox + x * math.cos(r) - dy * math.sin(r), 6),
                round(oy + x * math.sin(r) + dy * math.cos(r), 6), round(dz, 6)]
    return {"name": name, "vertices": [pt(-2, -2), pt(25, -2), pt(25, 25), pt(-2, 25)], "triangles": [[0, 1, 2], [0, 2, 3]]}


class SceneAndComponentContracts(unittest.IsolatedAsyncioTestCase):
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
                # Curve and commercial form edits must fail on REV
                err, _ = decode_reply(await session.call_tool("pvsyst_update_component", {
                    "component_type": "REV", "filename": "Retailer.REV", "updates": {},
                    "commercial_updates": {"Manufacturer": "X"}, "expected_sha256": got["sha256"]}))
                self.assertTrue(err)
                # Text field update succeeds
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
                "PVObject_=pvProject\n  SiteFile=Demo.SIT\n  MeteoFileName=Demo.MET\nEnd of PVObject pvProject\n",
                encoding="utf-8")
            (root / "Sites" / "Demo.SIT").write_text("PVObject_=pvSite\n  Latitude=45\nEnd of PVObject pvSite\n", encoding="utf-8")
            (root / "Meteo" / "Demo.MET").write_text("PVObject_=pvMeteo\nEnd of PVObject pvMeteo\n", encoding="utf-8")
            (root / "Projects" / "Demo.VC1").write_text(
                _source_vc([_orient(1, 25.0, 20.0), _orient(2, 25.0, -70.0)],
                           [_subarray(1, 1, "Mono300.PAN", 30, 1), _subarray(2, 2, "Mono440.PAN", 30, 1)]),
                encoding="utf-8")
            (root / "Projects" / "Demo.VC2").write_text(
                _source_vc([_orient(1, 25.0, 20.0)],
                           [_subarray(1, 1, "Mono300.PAN", 18, 2), _subarray(2, 1, "Mono300.PAN", 18, 2)]),
                encoding="utf-8")
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
            house = {"kind": "mesh", "meshes": [
                _roof_mesh("roof_1", 0, 0, 25.0, 0.0),
                _roof_mesh("roof_2", 20, 0, 25.0, 90.0),
            ]}
            async with mcp_session(root, cli) as session:
                # 1) Mixed-PAN scene authoring + native preview bind distinct module_geometry per table
                err, created = decode_reply(await session.call_tool("pvsyst_create_house_pv_scene", {
                    "project": "Demo.PRJ", "variant": "VC1", "output_name": "Mixed.pvc2",
                    "house": house, "layout": {"tables": het_tables}, "dry_run": False}))
                self.assertFalse(err, created)
                self.assertEqual(created["status"], "created", created)
                err, read_scene = decode_reply(await session.call_tool("pvsyst_read_house_pv_scene", {"scene_name": "Mixed.pvc2"}))
                self.assertFalse(err, read_scene)
                self.assertEqual([t["module_geometry"]["model_name"] for t in read_scene["create_arguments"]["layout"]["tables"]], ["Mono 300", "Mono 440"])
                err, imp = decode_reply(await session.call_tool("pvsyst_import_house_pv_scene", {
                    "scene_name": "Mixed.pvc2", "variant_name": "MixedOut", "method": "native", "dry_run": True}))
                self.assertFalse(err, imp)
                self.assertEqual(imp["status"], "ready_to_import", imp)

                # 2) Same-orientation multi-owner without source_subarray_id is refused as ambiguous
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


if __name__ == "__main__":
    unittest.main()

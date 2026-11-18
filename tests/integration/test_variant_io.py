"""MCP integration tests for variant readback, edits, syntax recovery, topology, and BOM encoding."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests.integration._session import decode_reply, mcp_session

BOM = b"\xef\xbb\xbf"
DEG = "\u00b0"
MOJIBAKE = "\u00c2\u00b0"


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

                # Combined UFactor + UVFactor or tiny PNomAC rounding to 0.00 must be refused
                err, _ = decode_reply(await session.call_tool("pvsyst_update_variant_parameters", {
                    "project": "U.PRJ", "variant": "VC0", "expected_sha256": info["sha256"],
                    "main_array_updates": {"UFactor": 26.0, "UVFactor": 2.0}}))
                self.assertTrue(err)
                err, _ = decode_reply(await session.call_tool("pvsyst_update_variant_parameters", {
                    "project": "U.PRJ", "variant": "VC0", "expected_sha256": info["sha256"],
                    "subarray_id": 1, "subarray_updates": {"PNomAC_InMPPT": 0.004}}))
                self.assertTrue(err)

                # UVFactor edit invalidates VarList and clears root bit 0 while keeping scene markers
                err, res = decode_reply(await session.call_tool("pvsyst_update_variant_parameters", {
                    "project": "U.PRJ", "variant": "VC0",
                    "main_array_updates": {"UVFactor": 2.0}}))
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

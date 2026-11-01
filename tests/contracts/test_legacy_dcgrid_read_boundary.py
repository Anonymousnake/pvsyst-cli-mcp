"""External contract for the bounded read-only legacy 8.0 DCGrid declaration read.

Reached exactly as a caller reaches it: through the MCP stdio server. A synthetic
self-contained workspace and a fake ``PVsystCLI.exe`` are used, so ZERO PVsyst
CLI/Native/GUI runs and no private case fixture is required.

Pinned behaviour of ``pvsyst_get_variant_parameters`` on a genuine legacy source
(root ``Version`` 8.0.x with a single ``SystemType=DCGrid``):

- The default payload gains a bounded ``legacy_read_scope`` block: the source
  version and the stored DCGrid declarations (``TypeRegul``/``VNomAlim``/
  ``VPVMax``/``RGridPV``/``RGridAlim``) read verbatim. It invents no inverter or
  battery default.
- An 8.1 Grid payload keeps its prior shape: no ``legacy_read_scope`` and no
  DCGrid declarations are added.
- An 8.0 non-DCGrid system and an 8.1 DCGrid source are NOT claimed (the scope
  only covers the exact legacy 8.0 DCGrid form).
- The getter never writes the variant file (byte-pure).
- The write path stays gated: a legal LIDLoss subarray update on the legacy 8.0
  source is still rejected and the file is left unchanged (no writer-gate
  broadening).
- A structurally truncated source is reported as an error, never a fabricated
  empty read.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]
GET_TOOL = "pvsyst_get_variant_parameters"
UPDATE_TOOL = "pvsyst_update_variant_parameters"
FAKE_CLI = b"Not an executable: this contract must not run PVsyst"


def _legacy_vc(version="8.0.0", system_type="DCGrid", dcgrid_decls=True, close=True):
    """A minimal legal pvVCalcul mirroring the genuine 8.0 DCGrid declaration shape."""
    parts = ["PVObject_=pvVCalcul\n", "  Comment=Legacy DCGrid fixture\n",
             f"  Version={version}\n", "  Flags=$00010000\n", "  SystemKindCustom=False\n\n",
             "  PVObject_PVMainArray=pvMainArray\n", "    Flags=$00100040\n", "  Flags2=$0800\n",
             "  UFactor=29.0\n",
             "  PVObject_SystemCircuit=pvCircuit\n", "    Version=8.0.0\n", "    Flags=$00\n",
             "    BaseNode=Start\n", "      InjectionPointNode Start;\n", "      InjectionPointNode End;\n",
             "    BaseNode=End\n", "  End of PVObject pvCircuit\n",
             "  SubArrays=Start\n", "    PVObject_=pvSubArray\n", "      Comment=PV Array\n",
             "      Version=8.0.0\n", "      Flags=$00180005\n", "      Flags2=$0802\n",
             "      SubArrayId=1\n", "      PVModule=Generic_Poly_250W.PAN\n",
             "      NModSerie=23\n", "      NStringCh=35\n", "      NoOrientation=1\n",
             "      NInverter=1\n", "      NInvMPPT=1\n", "      PowerSharingInverterId=-1\n",
             "      PNomAC_InMPPT=0.00\n", "      PQualityModU=-0.80\n", "      PQualityModI=-0.80\n",
             "      PQualityModP=-0.80\n", "      PMismMPP=1.00\n", "      LIDLoss=0.00\n",
             "      PVObject_IAM=pvIAM\n", "        Flags=$00\n", "        IAMMode=Default\n",
             "        B0_IAM=0.050\n", "      End of PVObject pvIAM\n",
             "    End of PVObject pvSubArray\n", "  EndTags\n", "  End of PVObject pvMainArray\n\n",
             "  PVObject_System=pvSystem\n", "    Flags=$0002\n", f"    SystemType={system_type}\n"]
    if dcgrid_decls:
        parts += ["    TypeRegul=2\n", "    VNomAlim=650.0\n", "    VPVMax=900.0\n",
                  "    RGridPV=0.0500\n", "    RGridAlim=0.0400\n"]
    parts += ["  End of PVObject pvSystem\n"]
    if close:
        parts.append("End of PVObject pvVCalcul\n")
    return "".join(parts)


def _payload(result):
    return json.loads("\n".join(item.text for item in result.content if hasattr(item, "text")))


class LegacyDcGridReadContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pvsyst-legacy-dcgrid-contract-")
        root = Path(self.tmp.name)
        self.projects = root / "Projects"
        self.projects.mkdir()
        (self.projects / "T.PRJ").write_text(
            "PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        self.legacy = self._write("T.VC0", _legacy_vc())
        self._write("T.VC1", _legacy_vc(version="8.1.0", system_type="Grid", dcgrid_decls=False))
        self._write("T.VC2", _legacy_vc(version="8.0.0", system_type="Grid", dcgrid_decls=False))
        self._write("T.VC3", _legacy_vc(version="8.1.0"))
        self._write("T.VC4", _legacy_vc(close=False))
        (root / "PVsystCLI.exe").write_bytes(FAKE_CLI)
        environment = dict(os.environ, PVSYST_WORKSPACE=str(root),
                           PVSYST_CLI=str(root / "PVsystCLI.exe"))
        environment.pop("PVSYST_BUILTIN_COMPONENTS", None)
        self._params = StdioServerParameters(
            command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
            cwd=str(REPO), env=environment)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, text):
        path = self.projects / name
        path.write_bytes(text.encode("utf-8"))
        return path

    async def _get(self, session, variant="VC0"):
        result = await session.call_tool(GET_TOOL, {"project": "T.PRJ", "variant": variant})
        self.assertFalse(result.is_error, result.content)
        return _payload(result)

    async def test_legacy_dcgrid_scope_read_verbatim_and_byte_pure(self):
        before = self.legacy.read_bytes()
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                body = await self._get(session)
        self.assertEqual(self.legacy.read_bytes(), before)  # a getter read is byte-pure
        scope = body["legacy_read_scope"]
        self.assertTrue(scope["readonly"])
        self.assertEqual(scope["format_family"], "DCGrid")
        self.assertEqual(scope["source_version"], "8.0.0")
        decl = scope["system_declarations"]
        self.assertEqual(decl["SystemType"], "DCGrid")
        self.assertEqual(decl["TypeRegul"], "2")
        self.assertEqual(decl["VNomAlim"], "650.0")
        self.assertEqual(decl["VPVMax"], "900.0")
        self.assertEqual(decl["RGridPV"], "0.0500")
        self.assertEqual(decl["RGridAlim"], "0.0400")
        # The existing stored subarray declaration is unchanged and not default-filled.
        sub = body["subarrays"][0]
        self.assertEqual(sub["values"]["NModSerie"], "23")
        self.assertEqual(sub["values"]["NStringCh"], "35")
        self.assertEqual(sub["inverter_state"]["PVModule"], "Generic_Poly_250W.PAN")

    async def test_8_1_grid_payload_keeps_prior_shape(self):
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                body = await self._get(session, variant="VC1")
        self.assertNotIn("legacy_read_scope", body)
        self.assertNotIn("topology", body)  # default opt-in blocks stay opt-in
        self.assertEqual(body["system_parameters"]["SystemType"], "Grid")
        self.assertNotIn("VNomAlim", body["system_parameters"])

    async def test_other_8_0_system_and_8_1_dcgrid_not_claimed(self):
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                other_8_0 = await self._get(session, variant="VC2")
                dcgrid_8_1 = await self._get(session, variant="VC3")
        self.assertNotIn("legacy_read_scope", other_8_0)  # 8.0 non-DCGrid: no claim
        self.assertNotIn("legacy_read_scope", dcgrid_8_1)  # exact 8.0 only, not 8.1

    async def test_write_guard_still_rejects_legacy_edit(self):
        before = self.legacy.read_bytes()
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                body = await self._get(session)
                result = await session.call_tool(UPDATE_TOOL, {
                    "project": "T.PRJ", "variant": "VC0",
                    "expected_sha256": body["sha256"],
                    "subarray_updates": {"LIDLoss": 1.0}})
        self.assertTrue(result.is_error)
        text = " ".join(getattr(item, "text", "") for item in result.content).lower()
        self.assertIn("8.1", text)  # rejected by the unchanged 8.1 write gate
        self.assertEqual(self.legacy.read_bytes(), before)  # no partial write

    async def test_truncated_source_reports_error(self):
        async with stdio_client(self._params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(GET_TOOL, {"project": "T.PRJ", "variant": "VC4"})
        self.assertTrue(result.is_error)


if __name__ == "__main__":
    unittest.main()

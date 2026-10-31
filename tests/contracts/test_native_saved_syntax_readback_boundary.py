"""Public contract: read back a native-saved variant (PVsyst 8.1.7) and recover it.

Regression boundary for the PVsyst-8.1.7 saved syntax that round17 produced:

- The ``pvSimulFile`` result-cache object is closed with its Delphi class name
  ``End of TSimulFich`` (``TSimulFich``), not ``End of pvSimulFile``.  A loader that
  knows only ``TAgeing``/``TOrientGroup``/``TDomeOrientGroup`` leaves ``pvSimulFile``
  open, so the whole-file object scan hits the root ``End of PVObject pvVCalcul`` with
  a non-matching top of stack and raises ``Unmatched object closing tag``.  That error
  is swallowed into ``shading_scene`` ``needs_review``/``unsupported`` on a pure read
  and raised by the display-name (``variant_name``) update path -- i.e. the saved file
  is neither inspectable nor recoverable.
- The saved file carries a UTF-8 BOM and CRLF line endings and extra/unknown objects
  (``pvSimulFile``, a future block) that must survive a read and a display-name save.
- The fixture also carries the other Delphi-class close aliases so one public
  read/save/re-read proves the whole alias set still closes: ``End of TAgeing``
  (pvAgeing), ``End of TOrientGroup`` (pvOrient) and ``End of TDomeOrientGroup``
  (pvDomeOrient), alongside ``End of TSimulFich`` (pvSimulFile).

Everything runs through a real stdio-MCP server with a synthetic self-contained
workspace (no vendor files, no private paths, no DB sample) and ZERO PVsyst
CLI/Native/GUI: the dummy ``PVsystCLI.exe`` is never executed by these read/plan
calls.  The fixture reproduces the original native-saved *syntax shape* only; it does
not model or assert any physical result.
"""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]
FAKE_CLI = b"Not an executable: these read/plan contracts must not run PVsyst"
READ_VARIANT_TOOL = "pvsyst_get_variant_parameters"
UPDATE_VARIANT_TOOL = "pvsyst_update_variant_parameters"


def _variant_text():
    """Minimal pvVCalcul variant that mirrors the 8.1.7 native-saved syntax shape."""
    lines = []

    def add(line):
        lines.append(line)

    add("PVObject_=pvVCalcul"); add("  Version=8.1.7"); add("  Flags=$00104001"); add("  Comment=Fixture Scene")
    add("  PVObject_SiteSimul=pvSite"); add("    Latitude=45.0"); add("    Longitude=8.0")
    add("    Altitude=250"); add("  End of PVObject pvSite")
    add("  Orientations, list of=3 TOrientGroup")
    for number, azimuth in ((1, 20.0), (2, -70.0), (3, 110.0)):
        add("  PVObject_=pvOrient"); add("    NoOrient=%d" % number); add("    Flags=$00")
        add("    FieldType=FixedPlane"); add("    FieldTilt=25.0"); add("    FieldAzim=%s" % azimuth)
        add("    BaseSlope=0"); add("    UsesTiltAver=False"); add("    UsesAzimAver=False")
        add("    UsesSlopeAver=False"); add("  End of TOrientGroup")
    add("  End of Orientations")
    add("  PVObject_PVMainArray=pvMainArray")
    add("  PVObject_SystemCircuit=pvCircuit"); add("  Version=8.1.6"); add("  BaseNode=Start")
    add("InjectionPointNode Start;"); add("Children=Start"); add("  NElements=1")
    add("  SubArrayId=1"); add("  SubArrayName=Array A")
    add("InjectionPointNode End;"); add("  BaseNode=End"); add("  End of PVObject pvCircuit")
    for number, azimuth in ((1, 20.0), (2, -70.0), (3, 110.0)):
        add("  PVObject_=pvSubArray"); add("    Comment=Array %d" % number)
        add("    SubArrayId=%d" % number); add("    NoOrientation=%d" % number)
        add("    Version=8.1.6"); add("    PVModule=module.PAN"); add("    GInverter=inverter.OND")
        add("    NModSerie=15"); add("    NStringCh=2"); add("    NStrOrient1=2")
        add("    NInverter=1"); add("    NInvMPPT=1"); add("  End of PVObject pvSubArray")
    add("  End of PVObject pvMainArray")
    add("  PVObject_System=pvSystem"); add("    SystemType=Grid"); add("  End of PVObject pvSystem")
    add("  PVObject_Ombrage=pvShading"); add("    Flags=$20100900"); add("    Precision=1")
    add("    ListeObjets, list of=3 TShdObject")
    for number, azimuth in ((1, 20.0), (2, -70.0), (3, 110.0)):
        add("      PVObject_=pvShdArrayTable"); add("        NoOrient=%d" % number)
        add("        NModInHeight=15"); add("        NModInWidth=2")
        add("        DOrigine=%d.0,0.0,0.0" % number); add("        DInclin=25.0")
        add("        DAzimut=%s" % azimuth); add("        BaseSlope=0")
        add("      End of PVObject pvShdArrayTable")
    add("    End of ListeObjets"); add("  End of PVObject pvShading")
    add("  PVObject_ModuleLayout=pvModuleLayout"); add("    Flags=$0B00")
    add("    ModLayoutGlobState=mlDefStrings")
    for number in (1, 2, 3):
        add("    PVObject_=pvMLTable"); add("      Comment=MLTable subarray %d" % number)
        add("      Flags=$05"); add("      ModLayoutState=mlDefStrings")
        add("    End of PVObject pvMLTable")
    add("  End of PVObject pvModuleLayout")
    # The other Delphi-class close aliases must keep working alongside TSimulFich:
    # ageing closes with `End of TAgeing`; a dome orientation record closes with
    # `End of TDomeOrientGroup`; the three fixed orientations above already close with
    # `End of TOrientGroup`. A missing alias leaves a container open and breaks the
    # whole-file scan with `Unmatched object closing tag`.
    add("  PVObject_Ageing=pvAgeing"); add("    Flags=$00"); add("  End of TAgeing")
    add("  PVObject_=pvDomeOrient"); add("    NoOrient=9"); add("    IsMainOrient=False")
    add("  End of TDomeOrientGroup")
    # 8.1.7 result cache: opened as pvSimulFile, closed by its Delphi class name.
    add("  PVObject_SimulFich=pvSimulFile"); add("    Flags=$00"); add("    FileNameWr=Sim.CSV")
    add("  End of TSimulFich")
    # Unknown/future object: must be tolerated and preserved, not dropped.
    add("  PVObject_Future=pvFutureThing"); add("    UnknownField=anything")
    add("  End of PVObject pvFutureThing")
    add("  VarList, list of=0 TVarInit"); add("  End of the List VarList")
    add("End of PVObject pvVCalcul")
    return "\r\n".join(lines) + "\r\n"


class _Workspace:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pvsyst-native-saved-"))
        self.root = self.tmp / "PVsyst8.1_Data"
        for folder in ("Projects", "ComposPV", "Results"):
            (self.root / folder).mkdir(parents=True)
        (self.root / "Projects" / "Demo.PRJ").write_bytes(
            b"PVObject_=pvProject\r\nEnd of PVObject pvProject\r\n")
        # Native persistence writes a UTF-8 BOM and CRLF; keep them for the fixture.
        (self.root / "Projects" / "Demo.VC0").write_bytes(
            b"\xef\xbb\xbf" + _variant_text().encode("utf-8"))
        self.cli = self.root / "PVsystCLI.exe"
        self.cli.write_bytes(FAKE_CLI)

    def params(self):
        import os
        return StdioServerParameters(command=sys.executable,
                                     args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO),
                                     env=dict(os.environ, PVSYST_WORKSPACE=str(self.root),
                                              PVSYST_CLI=str(self.cli)))

    def variant_bytes(self):
        return (self.root / "Projects" / "Demo.VC0").read_bytes()

    def cleanup(self, ok):
        if ok:
            shutil.rmtree(self.tmp, ignore_errors=True)
        else:
            print("Retained native-saved readback fixture: " + str(self.tmp), file=sys.stderr)


def _payload(response):
    if response.is_error:
        raise AssertionError(response.content)
    return json.loads("\n".join(item.text for item in response.content if hasattr(item, "text")))


class NativeSavedSyntaxReadbackBoundary(unittest.TestCase):
    def test_native_saved_variant_read_and_display_name_recovery(self):
        asyncio.run(self._run())

    async def _run(self):
        ws = _Workspace()
        before = ws.variant_bytes()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    # 1) Pure public read of the native-saved variant.
                    params = await call(READ_VARIANT_TOOL, {"project": "Demo.PRJ", "variant": "VC0"})
                    self.assertEqual([(o["id"], o["values"]["FieldTilt"], o["values"]["FieldAzim"])
                                      for o in params["orientations"]],
                                     [(1, "25.0", "20.0"), (2, "25.0", "-70.0"), (3, "25.0", "110.0")],
                                     params["orientations"])
                    self.assertEqual([(s["id"], s["values"]["NModSerie"], s["values"]["NStringCh"])
                                      for s in params["subarrays"]],
                                     [(1, "15", "2"), (2, "15", "2"), (3, "15", "2")], params["subarrays"])
                    # 3 subarrays x 2 strings in parallel = 6 global strings.
                    self.assertEqual(sum(int(s["values"]["NStringCh"]) for s in params["subarrays"]), 6)
                    self.assertIn("shading_scene", params)
                    self.assertEqual(params["shading_scene"]["mode"], "active", params["shading_scene"])
                    # The regression: an unhandled `End of TSimulFich` leaks a parser error here.
                    self.assertNotIn("Unmatched object closing tag", json.dumps(params))

                    # 2) Display-name update (plan) must parse the whole saved file.
                    sha256 = params["sha256"]
                    planned = await call(UPDATE_VARIANT_TOOL, {
                        "project": "Demo.PRJ", "variant": "VC0", "expected_sha256": sha256,
                        "variant_name": "Renamed Scene", "dry_run": True})
                    self.assertEqual(planned["name_change"],
                                     {"before": "Fixture Scene", "after": "Renamed Scene"}, planned)

                    # 3) Real recovery save, then read back: only the display-name line changes,
                    #    the pvSimulFile cache and the unknown block survive, BOM/CRLF stay.
                    saved = await call(UPDATE_VARIANT_TOOL, {
                        "project": "Demo.PRJ", "variant": "VC0", "expected_sha256": sha256,
                        "variant_name": "Renamed Scene", "dry_run": False})
                    self.assertEqual(saved["name_change"],
                                     {"before": "Fixture Scene", "after": "Renamed Scene"}, saved)
                    after = ws.variant_bytes()
                    old_lines = before.split(b"\r\n")
                    new_lines = after.split(b"\r\n")
                    self.assertEqual(len(old_lines), len(new_lines))
                    changed = [(i, old_lines[i], new_lines[i])
                               for i in range(len(old_lines)) if old_lines[i] != new_lines[i]]
                    self.assertEqual(len(changed), 1, changed)
                    self.assertEqual(changed[0][1], b"  Comment=Fixture Scene", changed)
                    self.assertEqual(changed[0][2], b"  Comment=Renamed Scene", changed)
                    self.assertTrue(after.startswith(b"\xef\xbb\xbf"))
                    self.assertIn(b"\r\n", after[:512])
                    text = after.decode("utf-8-sig")
                    self.assertIn("PVObject_SimulFich=pvSimulFile", text)
                    self.assertIn("End of TSimulFich", text)
                    self.assertIn("pvFutureThing", text)
                    # The remaining Delphi-class close aliases survive the read/save too.
                    self.assertIn("PVObject_Ageing=pvAgeing", text)
                    self.assertIn("End of TAgeing", text)
                    self.assertIn("PVObject_=pvDomeOrient", text)
                    self.assertIn("End of TDomeOrientGroup", text)

                    replayed = await call(READ_VARIANT_TOOL, {"project": "Demo.PRJ", "variant": "VC0"})
                    self.assertEqual([(o["id"], o["values"]["FieldAzim"]) for o in replayed["orientations"]],
                                     [(1, "20.0"), (2, "-70.0"), (3, "110.0")], replayed)
                    self.assertNotIn("Unmatched object closing tag", json.dumps(replayed))
            ok = True
        finally:
            ws.cleanup(ok)


if __name__ == "__main__":
    unittest.main()

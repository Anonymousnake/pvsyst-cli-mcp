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


def _indexed_shading_variant_text():
    """Minimal 8.1.7 shape with `#N`-indexed shading factor tables.

    PVsyst serializes repeated shading-factor-table objects under keys such as
    ``PVObject_ShdDiffuseFactTable#1``.  The key spelling is not an identifier; only the
    right-hand ``pvShdFTable`` value names the logical object, and the object closes with
    ``End of PVObject pvShdFTable``.  Each table nests a ``TInterpole2D`` interpolation
    grid (the factor-interpolation cache) closed by ``End of TInterpole2D``.  A tokenizer
    that rejects ``#`` in the key misses the block open, so the close hits the enclosing
    ``pvOrient`` frame and the whole-file scan raises ``Unmatched object closing tag``.
    """
    lines = []

    def add(line):
        lines.append(line)

    add("PVObject_=pvVCalcul"); add("  Version=8.1.7"); add("  Flags=$00104001")
    add("  Comment=Indexed Shading Fixture")
    add("  PVObject_SiteSimul=pvSite"); add("    Latitude=45.0"); add("    Longitude=8.0")
    add("  End of PVObject pvSite")
    add("  Orientations, list of=1 TOrientGroup")
    add("    PVObject_=pvOrient"); add("      Comment=Fixed plane with indexed factor tables")
    add("      Version=8.1.6"); add("      Flags=$00"); add("      NoOrient=1")
    add("      FieldType=FixedPlane"); add("      FieldTilt=25.0"); add("      FieldAzim=20.0")
    add("      BaseSlope=0"); add("      UsesTiltAver=False"); add("      UsesAzimAver=False")
    add("      UsesSlopeAver=False")
    for key in ("PVObject_ShdTableLin", "PVObject_ShdDiffuseFactTable#1",
                "PVObject_ShdDiffuseFactTable#2"):
        add("      %s=pvShdFTable" % key)
        add("        Flags=$00"); add("        CalcVersion=8.1.0"); add("        FOCalcul=foLinear")
        add("        SFTable=TInterpole2D"); add("          NBinX=2,2,"); add("          ExtX=0,1,")
        add("          XDeb=0.0,1.0,"); add("          YDeb=0.0,1.0,")
        add("          Line_2=0.0000,0.0000,"); add("          Line_1=0.0000,1.0000,")
        add("        End of TInterpole2D")
        add("      End of PVObject pvShdFTable")
    add("    End of TOrientGroup")
    add("  End of Orientations")
    add("  PVObject_PVMainArray=pvMainArray")
    add("  PVObject_SystemCircuit=pvCircuit"); add("  Version=8.1.6"); add("  BaseNode=Start")
    add("InjectionPointNode Start;"); add("Children=Start"); add("  NElements=1")
    add("  SubArrayId=1"); add("  SubArrayName=Array A")
    add("InjectionPointNode End;"); add("  BaseNode=End"); add("  End of PVObject pvCircuit")
    add("  PVObject_=pvSubArray"); add("    Comment=Array A"); add("    SubArrayId=1")
    add("    NoOrientation=1"); add("    Version=8.1.6"); add("    PVModule=module.PAN")
    add("    GInverter=inverter.OND"); add("    NModSerie=15"); add("    NStringCh=2")
    add("    NStrOrient1=2"); add("    NInverter=1"); add("    NInvMPPT=1")
    add("  End of PVObject pvSubArray")
    add("  End of PVObject pvMainArray")
    add("  PVObject_System=pvSystem"); add("    SystemType=Grid"); add("  End of PVObject pvSystem")
    # The four Delphi-class close aliases must all keep closing around the indexed tables.
    add("  PVObject_Ageing=pvAgeing"); add("    Flags=$00"); add("  End of TAgeing")
    add("  PVObject_=pvDomeOrient"); add("    NoOrient=9"); add("    IsMainOrient=False")
    add("  End of TDomeOrientGroup")
    add("  PVObject_SimulFich=pvSimulFile"); add("    Flags=$00"); add("    FileNameWr=Sim.CSV")
    add("  End of TSimulFich")
    # Unknown/future object: tolerated and preserved, never dropped.
    add("  PVObject_Future=pvFutureThing"); add("    UnknownField=anything")
    add("  End of PVObject pvFutureThing")
    add("End of PVObject pvVCalcul")
    return "\r\n".join(lines) + "\r\n"


def _var_simul_block(close_last=True):
    """A VarList TVarSimul body in the 8.1.7 saved shape.

    Index 1 is absent and the header over-declares by one, exactly as the real
    native save does: a reader must report declared vs parsed instead of
    reconciling them. Entries are month-resolution (plain ``ValMonth`` numbers),
    hour-resolution (untouched packed ``Day_*`` rows) and association-only.
    """
    lines = []

    def add(line):
        lines.append(line)

    add("    VarSimul_2=ShdElec"); add("      Varident=ShdElec"); add("      VarAcc=Month")
    add("      VarType=Energy"); add("      VarStore=Simul"); add("      VarModeSS=Accum")
    add("      DateBeg=01/01/90 00:00"); add("      DateEnd=31/12/90 23:00")
    add("      ValMonth=1.5,2.25,-3.75,4.0,5.5,6.25,7.0,8.5,9.75,10.0,11.25,12.5,")
    add("    End of VarSimul=viShdElec")
    add("    VarSimul_3=GlobInc"); add("      Varident=GlobInc"); add("      VarAcc=Hour")
    add("      VarType=Irrad"); add("      VarStore=Simul")
    add("      DateBeg=01/01/90 00:00"); add("      DateEnd=31/12/90 23:00")
    add("      WRFactor=203223.9")
    add("        Day_0101=+0+0+2Ly+81H"); add("        Day_0201=+0+0+2MM+82Q")
    add("      End of Data")
    add("    End of VarSimul=viGlobInc")
    add("    VarSimul_4=PRTAux"); add("      Varident=PRTAux"); add("      VarAcc=Hour")
    add("      VarType=Factor"); add("      VarStore=Simul")
    add("      VarAssoc1=E_Grid"); add("      VarAssoc2=EArray")
    add("      DateBeg=01/01/90 00:00"); add("      DateEnd=31/12/90 23:00")
    if close_last:
        add("    End of VarSimul=viPRTAux")
    return "\r\n".join(lines) + "\r\n"


def _saved_results_variant_text(declared=4, close_last=True):
    """The same 8.1.7 saved variant with a real TVarSimul result cache written in.

    Adds the two result-cache layers a batch save produces besides the VarList:
    two ``THistogramme`` plot blocks and one ``pvShdFTable`` factor table.
    """
    empty = "  VarList, list of=0 TVarInit\r\n  End of the List VarList\r\n"
    saved = ("  SimulPlots=Start\r\n"
             "    PVObject_=pvSimulPlot\r\n"
             "      Comment=Fixture plot one\r\n"
             "      Histogram=THistogramme\r\n"
             "        Flags=$00\r\n"
             "      End of THistogramme\r\n"
             "    End of PVObject pvSimulPlot\r\n"
             "    PVObject_=pvSimulPlot\r\n"
             "      Comment=Fixture plot two\r\n"
             "      Histogram=THistogramme\r\n"
             "        Flags=$00\r\n"
             "      End of THistogramme\r\n"
             "    End of PVObject pvSimulPlot\r\n"
             "  EndTags\r\n"
             "  PVObject_ShdTableLin=pvShdFTable\r\n"
             "    Flags=$00\r\n"
             "    FOCalcul=foLinear\r\n"
             "  End of PVObject pvShdFTable\r\n"
             "  VarList, list of=%d TVarSimul\r\n" % declared
             + _var_simul_block(close_last)
             + "  End of the List VarList\r\n")
    text = _variant_text()
    assert empty in text
    text = text.replace(
        "  PVObject_SimulFich=pvSimulFile\r\n    Flags=$00\r\n    FileNameWr=Sim.CSV\r\n",
        "  PVObject_SimulFich=pvSimulFile\r\n    Flags=$00\r\n    FileNameWr=Sim.CSV\r\n"
        "    VarIdentWr=viGlobInc,viE_Grid,\r\n"
        "    Units=uikWh,uiW_m2,uim3_h,uimeterW,\r\n"
        "    TypeValW=Hour\r\n    NCarCol=6\r\n")
    return text.replace(empty, saved)


class _Workspace:
    def __init__(self, text=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="pvsyst-native-saved-"))
        self.root = self.tmp / "PVsyst8.1_Data"
        for folder in ("Projects", "ComposPV", "Results"):
            (self.root / folder).mkdir(parents=True)
        (self.root / "Projects" / "Demo.PRJ").write_bytes(
            b"PVObject_=pvProject\r\nEnd of PVObject pvProject\r\n")
        # Native persistence writes a UTF-8 BOM and CRLF; keep them for the fixture.
        body = text if text is not None else _variant_text()
        (self.root / "Projects" / "Demo.VC0").write_bytes(
            b"\xef\xbb\xbf" + body.encode("utf-8"))
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


    def test_indexed_shading_table_read_and_display_name_save(self):
        asyncio.run(self._run_indexed())

    async def _run_indexed(self):
        ws = _Workspace(_indexed_shading_variant_text())
        before = ws.variant_bytes()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    # 1) Pure read must not surface the unmatched-close parser error.
                    params = await call(READ_VARIANT_TOOL, {"project": "Demo.PRJ", "variant": "VC0"})
                    self.assertNotIn("Unmatched object closing tag", json.dumps(params))
                    self.assertEqual([(o["id"], o["values"]["FieldTilt"], o["values"]["FieldAzim"])
                                      for o in params["orientations"]], [(1, "25.0", "20.0")],
                                     params["orientations"])
                    self.assertEqual([(s["id"], s["values"]["NModSerie"], s["values"]["NStringCh"])
                                      for s in params["subarrays"]], [(1, "15", "2")], params["subarrays"])

                    # 2) Display-name plan parses the whole file, indexed keys included.
                    sha256 = params["sha256"]
                    planned = await call(UPDATE_VARIANT_TOOL, {
                        "project": "Demo.PRJ", "variant": "VC0", "expected_sha256": sha256,
                        "variant_name": "Renamed Indexed", "dry_run": True})
                    self.assertEqual(planned["name_change"],
                                     {"before": "Indexed Shading Fixture", "after": "Renamed Indexed"}, planned)

                    # 3) Real save: only the Comment line changes; indexed table keys, the
                    #    nested TInterpole2D grid and the unknown block survive byte-for-byte.
                    await call(UPDATE_VARIANT_TOOL, {
                        "project": "Demo.PRJ", "variant": "VC0", "expected_sha256": sha256,
                        "variant_name": "Renamed Indexed", "dry_run": False})
                    after = ws.variant_bytes()
                    old_lines, new_lines = before.split(b"\r\n"), after.split(b"\r\n")
                    self.assertEqual(len(old_lines), len(new_lines))
                    changed = [(i, old_lines[i], new_lines[i])
                               for i in range(len(old_lines)) if old_lines[i] != new_lines[i]]
                    self.assertEqual(len(changed), 1, changed)
                    self.assertEqual(changed[0][1], b"  Comment=Indexed Shading Fixture", changed)
                    self.assertEqual(changed[0][2], b"  Comment=Renamed Indexed", changed)
                    self.assertTrue(after.startswith(b"\xef\xbb\xbf"))
                    self.assertEqual(after.count(b"\r\n"), after.count(b"\n"))
                    text = after.decode("utf-8-sig")
                    for token in ("PVObject_ShdDiffuseFactTable#1=pvShdFTable",
                                  "PVObject_ShdDiffuseFactTable#2=pvShdFTable",
                                  "SFTable=TInterpole2D", "End of TInterpole2D",
                                  "Line_1=0.0000,1.0000,", "pvFutureThing",
                                  "End of TOrientGroup", "End of TAgeing",
                                  "End of TDomeOrientGroup", "End of TSimulFich"):
                        self.assertIn(token, text)
                    self.assertEqual(text.count("End of TInterpole2D"), 3)
                    self.assertEqual(text.count("End of PVObject pvShdFTable"), 3)

                    # 4) Re-read after save: same values, no parser error.
                    replayed = await call(READ_VARIANT_TOOL, {"project": "Demo.PRJ", "variant": "VC0"})
                    self.assertNotIn("Unmatched object closing tag", json.dumps(replayed))
                    self.assertEqual([(o["id"], o["values"]["FieldTilt"], o["values"]["FieldAzim"])
                                      for o in replayed["orientations"]], [(1, "25.0", "20.0")], replayed)
                    self.assertEqual([(s["id"], s["values"]["NModSerie"], s["values"]["NStringCh"])
                                      for s in replayed["subarrays"]], [(1, "15", "2")], replayed)
            ok = True
        finally:
            ws.cleanup(ok)


    def test_saved_results_readback_from_a_native_save(self):
        asyncio.run(self._run_saved_results())

    async def _run_saved_results(self):
        ws = _Workspace(_saved_results_variant_text())
        before = ws.variant_bytes()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    default = await call(READ_VARIANT_TOOL, {"project": "Demo.PRJ", "variant": "VC0"})
                    # Default keeps the prior response shape: no saved_results block.
                    self.assertNotIn("saved_results", default)
                    self.assertEqual(default["variant_name"], "Fixture Scene")

                    params = await call(READ_VARIANT_TOOL,
                                        {"project": "Demo.PRJ", "variant": "VC0",
                                         "include_saved_results": True})
                    saved = params["saved_results"]
                    # Opt-in only adds the block; every existing key is unchanged.
                    self.assertEqual(params["variant_name"], "Fixture Scene")
                    self.assertEqual(len(params["orientations"]), 3)
                    self.assertEqual(len(params["subarrays"]), 3)
                    self.assertEqual(
                        {k: v for k, v in params.items() if k != "saved_results"}, default)

                    self.assertTrue(saved["present"], saved)
                    self.assertEqual(saved["root_flags"], "$00104001")
                    self.assertEqual(saved["pv_simul_file"]["file_name_written"], "Sim.CSV")
                    self.assertEqual(saved["pv_simul_file"]["var_idents_written"],
                                     ["viGlobInc", "viE_Grid"])
                    self.assertEqual(saved["pv_simul_file"]["native_unit_codes"],
                                     ["uikWh", "uiW_m2", "uim3_h", "uimeterW"])
                    self.assertEqual(saved["pv_simul_file"]["type_val_written"], "Hour")

                    # Declared vs parsed is reported, never silently reconciled.
                    self.assertEqual(saved["var_list"]["declared_count"], 4)
                    self.assertEqual(saved["var_list"]["parsed_count"], 3)
                    self.assertEqual(saved["var_list"]["missing_indices"], [1])
                    self.assertEqual(saved["issues"],
                                     ["VarList declares 4 TVarSimul but 3 parsed"])

                    entries = {entry["ident"]: entry for entry in saved["var_list"]["entries"]}
                    self.assertEqual(list(entries), ["ShdElec", "GlobInc", "PRTAux"],
                                     "stored file order is preserved")

                    # Month resolution: the stored numbers themselves are recoverable.
                    month = entries["ShdElec"]
                    self.assertEqual(month["native_ident"], "viShdElec")
                    self.assertEqual((month["declared_type"], month["accumulator"], month["mode_ss"]),
                                     ("Energy", "Month", "Accum"))
                    self.assertEqual(month["date_beg"], "01/01/90 00:00")
                    self.assertEqual(month["values_scope"], "month")
                    self.assertEqual(round(sum(month["values"]), 2), 74.75)
                    # No unit is declared, so none is assumed from the native type code.
                    self.assertIsNone(month["unit"])
                    self.assertEqual(month["unit_source"], "not_declared_in_varlist")

                    # Hour resolution: the untouched packed declaration is recoverable.
                    hour = entries["GlobInc"]
                    self.assertIsNone(hour["values"])
                    self.assertEqual(hour["day_rows"], 2)
                    self.assertEqual(hour["stored_weighted_factor"], 203223.9)
                    self.assertEqual(hour["value_declaration"],
                                     "Day_0101=+0+0+2Ly+81H\nDay_0201=+0+0+2MM+82Q")

                    # Association-only entry: nothing is invented for it.
                    assoc = entries["PRTAux"]
                    self.assertIsNone(assoc["values"])
                    self.assertIsNone(assoc["value_declaration"])
                    self.assertEqual(assoc["associations"], ["E_Grid", "EArray"])

                    self.assertEqual(saved["histograms"],
                                     {"count": 2, "types": {"THistogramme": 2}})
                    self.assertEqual(saved["shading_factor_tables"], {"count": 1})

                    # A pure read never rewrites the variant.
                    self.assertEqual(ws.variant_bytes(), before)
            ok = True
        finally:
            ws.cleanup(ok)

    def test_saved_results_absent_malformed_and_duplicate_are_reported(self):
        asyncio.run(self._run_saved_results_edges())

    async def _run_saved_results_edges(self):
        async def read(workspace):
            async with stdio_client(workspace.params()) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return _payload(await session.call_tool(
                        READ_VARIANT_TOOL, {"project": "Demo.PRJ", "variant": "VC0",
                                            "include_saved_results": True}))

        # 1) Cold variant: a leftover pvSimulFile header is not a stored result.
        cold = _Workspace()
        ok = False
        try:
            saved = (await read(cold))["saved_results"]
            self.assertFalse(saved["present"], saved)
            self.assertTrue(saved["pv_simul_file"]["present"], saved)
            self.assertEqual(saved["pv_simul_file"]["file_name_written"], "Sim.CSV")
            self.assertEqual(saved["var_list"],
                             {"declared_count": None, "parsed_count": 0,
                              "missing_indices": [], "entries": []})
            self.assertEqual(saved["histograms"], {"count": 0, "types": {}})
            self.assertEqual(saved["shading_factor_tables"], {"count": 0})
            self.assertEqual(saved["issues"], [])
            ok = True
        finally:
            cold.cleanup(ok)

        # 2) Truncated cache: the unclosed entry is reported, the rest still parses.
        broken = _Workspace(_saved_results_variant_text(close_last=False))
        ok = False
        try:
            saved = (await read(broken))["saved_results"]
            self.assertEqual(saved["var_list"]["parsed_count"], 3)
            self.assertTrue(any("unclosed VarSimul_4" in issue for issue in saved["issues"]),
                            saved["issues"])
            self.assertEqual([entry["ident"] for entry in saved["var_list"]["entries"]],
                             ["ShdElec", "GlobInc", "PRTAux"])
            self.assertIsNone(saved["var_list"]["entries"][2]["native_ident"])
            self.assertEqual(saved["var_list"]["entries"][0]["values"][:2], [1.5, 2.25])
            ok = True
        finally:
            broken.cleanup(ok)

        # 3) Duplicate VarSimul index: both entries are kept in file order, none overwritten.
        dup_text = _saved_results_variant_text().replace(
            "VarSimul_3=GlobInc", "VarSimul_2=GlobInc")
        dup = _Workspace(dup_text)
        ok = False
        try:
            saved = (await read(dup))["saved_results"]
            entries = saved["var_list"]["entries"]
            self.assertEqual([entry["index"] for entry in entries], [2, 2, 4])
            self.assertEqual([entry["ident"] for entry in entries],
                             ["ShdElec", "GlobInc", "PRTAux"])
            self.assertEqual(saved["var_list"]["parsed_count"], 3)
            ok = True
        finally:
            dup.cleanup(ok)


if __name__ == "__main__":
    unittest.main()

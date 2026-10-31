"""Public contract: native variant text-encoding integrity (UTF-8 BOM) -- round18.

The original PVsyst reader treats a variant file with NO UTF-8 BOM as Windows-1252. The
library always writes UTF-8; when the serialized content already carries non-ASCII -- the
fixed-plane ``Comment=Fixed, Tilt 25.0°, Azim. ...°`` degree signs a genuine 8.0 source
ships -- a BOM-less file is re-read by the original software as ``Â°`` and re-saved
double-encoded (``C3 82 C2 B0``). Round17's native save exposed exactly that on a derived
target that had lost its BOM upstream.

This contract pins the writer fix through a real stdio-MCP session on a synthetic,
self-contained workspace (no vendor files, no private paths) and ZERO PVsyst
CLI/Native/GUI -- the ``PVsystCLI.exe`` is an inert placeholder and the native import path
never launches it:

- a NEW native variant authored from a BOM-less UTF-8 source whose orientation comments
  carry a degree is published WITH a UTF-8 BOM: ``utf-8-sig`` reads single-degree bytes
  ``C2 B0`` and the ``C3 82 C2 B0`` mojibake never appears;
- an ASCII-only source keeps its legacy BOM-less shape (no gratuitous upgrade of every
  legacy file);
- the source variant stays byte-identical and the electrical/geometry declarations are
  carried through unchanged;
- a following MCP display-name update (non-ASCII name) keeps the BOM and the degree bytes
  intact while changing only the header Comment line -- the native-saved edit boundary;
- a representative SIBLING new-variant writer (``pvsyst_create_ac_cabling_variant``) that shares
  the same ``_display_name_prefix`` boundary is pinned too: an ASCII display name over a
  BOM-less source whose body carries a degree still gets the BOM (content, not the name,
  decides), while an ASCII body stays BOM-less -- so the fix is not scoped to the house-import
  path alone.

No internal builder/reader helper is asserted; only the produced file bytes and the public
tool responses.
"""
import asyncio
import calendar
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# Reuse the synthetic self-contained fixture builders of the sibling MCP contract instead
# of duplicating the house/table/scene builders.
from test_multi_orientation_native_boundary import (
    _Workspace, _source_text, _tables, _binding, _house, _payload,
    GROUPS3, MODULE, CREATE_TOOL, IMPORT_TOOL, READ_VARIANT_TOOL,
)

UPDATE_VARIANT_TOOL = "pvsyst_update_variant_parameters"
AC_TOOL = "pvsyst_create_ac_cabling_variant"
PREPARE_LOAD_TOOL = "pvsyst_prepare_load_profile"
LOAD_TOOL = "pvsyst_create_load_variant"
STORAGE_TOOL = "pvsyst_create_grid_storage_variant"
STORAGE_SYSTEM_TOOL = "pvsyst_create_grid_storage_system"
DEG = "\u00b0"          # U+00B0, UTF-8 C2 B0
MOJIBAKE = "\u00c2\u00b0"  # 'Â°' after a BOM-less UTF-8 file is read as Windows-1252
BOM = b"\xef\xbb\xbf"

# Direct-writer fixtures (load / grid-storage / grid-storage-system). These three writers build
# their whole candidate text themselves instead of calling _display_name_prefix, so their BOM
# branch is the last place the same BOM-less-degree class can still be re-emitted. The synthetic
# declarations mirror the unit fixtures; only the inherited root Comment carries the degree.
_DIRECT_ROOT = "PVObject_=pvVCalcul\n  Comment={comment}\n  Version=8.1.6\n  Flags={flags}\n"
_DIRECT_ARRAY = "  PVObject_=pvSubArray\n    SubArrayId=1\n    NModSerie=10\n  End of PVObject pvSubArray\n"
_DIRECT_PV_SYSTEM = "  PVObject_System=pvSystem\n    Flags=$00\n    SystemType=Grid\n  End of PVObject pvSystem\n"
_DIRECT_STORAGE_SYSTEM = (
    "  PVObject_System=pvSystem\n    Flags=$10010040\n    SystemType=Grid\n"
    "    BatteryFile=Fixture.BTR\n    NBattSerie=1\n    NBattParall=2\n  End of PVObject pvSystem\n")
_DIRECT_GRID_STORAGE = (
    "  PVObject_GridStorage=pvGridStorage\n    Flags=$00\n    Strategy=SelfConsumption\n"
    "    ChargingStrategy=PVAvailable\n    DischStrategy=Immediate\n    BattSoCInit=0.6\n"
    "    SOCChDecl=0.95\n    SOCDischDecl=0.2\n    SOCDischSelf=0.5\n    PMaxCharge=380\n"
    "    PMaxDisch=170\n    BattRectMaxEffic=97\n    BattRectEuroEffic=95\n    BattInvMaxEffic=97\n"
    "    BattInvEuroEffic=95\n  End of PVObject pvGridStorage\n")
_DIRECT_CLOSE = "End of PVObject pvVCalcul\n"
_BTR = ("PVObject_=pvBattery\n  Version=8.1.6\n  Flags=$00\n  BattTechnol=Pb_Sealed_AGM\n"
        "  CapNomC10=160\n  AlphaSOC=0.2\n  PVObject_Commercial=pvCommercial\n"
        "    Manufacturer=Fixture\n    Model=12V160Ah\n  End of PVObject pvCommercial\n"
        "End of PVObject pvBattery\n")
_BATTERY = {"file_name": "Fixture.BTR", "series_count": 4, "parallel_count": 4,
            "monthly_temperature_c": [20] * 12, "cycle_sow": 1, "static_sow": 1}
_STORAGE = {"strategy": "self_consumption", "soc_min": 0.2, "soc_max": 0.9,
            "initial_soc": 0.5, "reserve_soc": 0.2, "max_charge_ac_kw": 3,
            "max_discharge_ac_kw": 5, "charger_max_efficiency_pct": 97,
            "charger_euro_efficiency_pct": 95, "battery_inverter_max_efficiency_pct": 97,
            "battery_inverter_euro_efficiency_pct": 95}


def _direct_workspace(source):
    ws = _Workspace([], scene_source=source)
    (ws.root / "Projects" / "Demo.VC1").write_bytes(source.encode("utf-8"))
    return ws


def _write_battery(ws):
    folder = ws.root / "ComposPV" / "Batteries"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Fixture.BTR").write_bytes(_BTR.encode("utf-8"))


def _write_raw_load(ws, year=2025):
    start = datetime(year, 1, 1)
    hours = 24 * (366 if calendar.isleap(year) else 365)
    rows = ["Date;P Load", ";W"]
    rows.extend((start + timedelta(hours=h)).strftime("%Y-%m-%d %H:%M") + ";2500"
                for h in range(hours))
    (ws.root / "UserData" / "raw.csv").write_bytes(("\n".join(rows) + "\n").encode("ascii"))


def _degree_source(groups):
    """Synthetic source whose fixed-plane orientation comments carry a degree sign.

    Adds the owning header Comment the native import save needs to set the display name,
    then injects a ``Comment=...25.0°, Azim. 20.0°`` line into every orientation record.
    """
    text = _source_text(groups)
    text = text.replace("  Version=8.1.6\n  Flags=$00100001\n",
                        "  Version=8.1.6\n  Flags=$00100001\n  Comment=Round18 Scene\n", 1)
    return text.replace(
        "    UsesSlopeAver=False\n",
        "    UsesSlopeAver=False\n    Comment=Fixed, Tilt 25.0%s, Azim. 20.0%s\n" % (DEG, DEG))


def _native_ac_source(degree):
    """One-subarray inactive-AC source in the exact native flag families the AC writer verifies.

    Self-contained (no vendor files); the optional degree lives in the fixed-plane comment, i.e.
    the body -- not the display name -- carries the non-ASCII, which is what the fix keys on.
    """
    comment = ("    Comment=Fixed, Tilt 25.0%s, Azim. 20.0%s\n" % (DEG, DEG)) if degree else ""
    return (
        "PVObject_=pvVCalcul\n  Version=8.1.6\n  Flags=$00000000\n  Comment=Round18 Scene\n"
        "  PVObject_SiteSimul=pvSite\n    Latitude=45.0\n    Longitude=8.0\n    Altitude=250\n"
        "  End of PVObject pvSite\n"
        "  Orientations, list of=1 TOrientGroup\n"
        "  PVObject_=pvOrient\n    NoOrient=1\n    Flags=$00\n    FieldType=FixedPlane\n"
        "    FieldTilt=25.0\n    FieldAzim=20.0\n    BaseSlope=0\n"
        "    UsesTiltAver=False\n    UsesAzimAver=False\n    UsesSlopeAver=False\n"
        + comment +
        "  End of TOrientGroup\n  End of Orientations\n"
        "  PVObject_PVMainArray=pvMainArray\n    Flags=$01900400\n    Flags2=$082A\n"
        "    MVNbTransfos=0\n    UFAverEffic=False\n"
        "  PVObject_SystemCircuit=pvCircuit\n  Version=8.1.6\n  Flags=$00\n  BaseNode=Start\n"
        "InjectionPointNode Start;\nChildren=Start\nInverterNode Start;\n  NElements=1\n"
        "  SubArrayId=1\n  SubArrayName=Array A\n  Children=Start\n    StringNode Start;\n"
        "      NElements=2\n      SubArrayId=1\n      SubArrayName=Array A\n    StringNode End;\n"
        "  Children=End\nInverterNode End;\nChildren=End\nInjectionPointNode End;\n"
        "  BaseNode=End\n  End of PVObject pvCircuit\n"
        "  PVObject_=pvSubArray\n    Comment=Array A\n    SubArrayId=1\n    NoOrientation=1\n"
        "    Version=8.1.6\n    Flags=$019E1002\n    Flags2=$20\n    PowerSharingInverterId=-1\n"
        "    PVModule=module.PAN\n    GInverter=inverter.OND\n    NModSerie=15\n    NStringCh=2\n"
        "    NStrOrient1=2\n    NInverter=1\n    NInvMPPT=1\n    ACWireLength=0.0\n"
        "  End of PVObject pvSubArray\n  End of PVObject pvMainArray\n"
        "  PVObject_System=pvSystem\n    Flags=$00\n    SystemType=Grid\n"
        "  End of PVObject pvSystem\n"
        "  PVObject_Ombrage=pvShading\n    Flags=$00\n  End of PVObject pvShading\n"
        "  SimulPlots=Start\n  SimulPlots=End\n"
        "  VarList, list of=0 TVarInit\n  End of the List VarList\nEnd of PVObject pvVCalcul\n"
    )


class NativeTextWriterEncodingBoundary(unittest.TestCase):
    def test_non_ascii_native_variant_is_utf8_bom(self):
        asyncio.run(self._run(degree=True, expect_bom=True))

    def test_ascii_native_variant_stays_bomless(self):
        asyncio.run(self._run(degree=False, expect_bom=False))

    def test_sibling_ac_writer_bom_follows_content_not_name(self):
        # A representative sibling new-variant writer that shares _display_name_prefix: the
        # display name is ASCII, the source has no BOM, yet the BODY carries a degree. The old
        # source-BOM/name-only rule returns b"" here (BOM-less UTF-8 -> mojibake on 8.1.7 save);
        # the content-aware rule must emit the BOM.
        asyncio.run(self._ac_run(degree=True, expect_bom=True))

    def test_sibling_ac_writer_ascii_body_stays_bomless(self):
        asyncio.run(self._ac_run(degree=False, expect_bom=False))

    async def _ac_run(self, *, degree, expect_bom):
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        groups = [{"id": 1, "name": "Array A", "tilt": 25.0, "azimuth": 20.0}]
        ws = _Workspace(groups, scene_source=_native_ac_source(degree))
        before = ws.project_bytes()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    made = await call(AC_TOOL, {
                        "project": "Demo.PRJ", "source_variant": "VC1", "new_variant": "VC2",
                        "ac": {"length_m": 100}, "variant_name": "R18_ASCII", "dry_run": False})
                    self.assertEqual(made["status"], "created", made)
                    # The display name is ASCII and the source had no BOM: only the body decides.
                    self.assertEqual(made["name_change"]["after"], "R18_ASCII", made)
                    out = Path(made["artifacts"]["variant"]).read_bytes()
                    self.assertEqual(out.startswith(BOM), expect_bom,
                                     "BOM presence %r must follow non-ASCII body=%r" % (out[:3], degree))
                    text = out.decode("utf-8-sig")
                    self.assertIn("NModSerie=15", text)
                    self.assertIn("NStringCh=2", text)
                    self.assertIn("FieldTilt=25.0", text)
                    self.assertIn("Flags=$01B00400", text)  # AC enabled on the main array
                    if degree:
                        self.assertNotIn(MOJIBAKE, text, "degree must not become Â°")
                        self.assertEqual(out.count(b"\xc2\xb0"), 2)
                        self.assertNotIn(b"\xc3\x82\xc2\xb0", out)
                    self.assertEqual(ws.project_bytes(), before, "source variant must stay byte-identical")
            ok = True
        finally:
            ws.cleanup(ok)

    async def _run(self, *, degree, expect_bom):
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        ws = _Workspace(GROUPS3, scene_source=(
            _degree_source(GROUPS3) if degree else
            _source_text(GROUPS3).replace(
                "  Version=8.1.6\n  Flags=$00100001\n",
                "  Version=8.1.6\n  Flags=$00100001\n  Comment=Round18 Scene\n", 1)))
        before = ws.project_bytes()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    tables = _tables(GROUPS3)
                    create_args = {"project": "Demo.PRJ", "variant": "VC1",
                                   "output_name": "Encoding.pvc2",
                                   "house": _house(tables, GROUPS3),
                                   "layout": {"tables": tables},
                                   "module_geometry": MODULE,
                                   "string_layout": _binding(tables, GROUPS3)}
                    created = await call(CREATE_TOOL, dict(create_args, dry_run=False))
                    self.assertEqual(created["status"], "created", created)

                    imported = await call(IMPORT_TOOL, {
                        "scene_name": "Encoding.pvc2", "variant_name": "Round18 Encoding",
                        "method": "native", "shading_mode": "module_layout",
                        "dry_run": False, "confirm": True})
                    self.assertEqual(imported["status"], "native_variant_created", imported)

                    out = Path(imported["file"]).read_bytes()
                    self.assertEqual(out.startswith(BOM), expect_bom,
                                     "BOM presence %r must match non-ASCII content=%r" % (out[:3], degree))
                    text = out.decode("utf-8-sig")
                    # Native import never launches PVsyst: real consumption flags stay false.
                    for key in ("imported", "native_consumption_verified",
                                "native_geometry_verified", "native_module_layout_verified"):
                        self.assertFalse(imported[key], (key, imported))
                    # Geometry/electrical declarations are carried through untouched.
                    self.assertEqual(text.count("=pvShdArrayTable"), 3)
                    self.assertIn("NModSerie=15", text)
                    self.assertIn("NStringCh=2", text)
                    self.assertIn("FieldTilt=25.0", text)
                    if degree:
                        self.assertNotIn(MOJIBAKE, text, "degree must not become Â°")
                        self.assertIn("Tilt 25.0%s" % DEG, text)
                        self.assertEqual(out.count(b"\xc2\xb0"), 6)
                        self.assertNotIn(b"\xc3\x82\xc2\xb0", out)
                    self.assertEqual(ws.project_bytes(), before, "source variant must stay byte-identical")

                    # Read back through the public variant surface: BOM flag and values intact.
                    params = await call(READ_VARIANT_TOOL, {"project": "Demo.PRJ",
                                                            "variant": imported["variant"]})
                    self.assertEqual(params["utf8_bom"], expect_bom, params.get("utf8_bom"))
                    self.assertEqual([o["values"]["FieldTilt"] for o in params["orientations"]],
                                     ["25.0", "25.0", "25.0"], params["orientations"])

                    if degree:
                        # A non-ASCII display-name edit keeps the BOM and the degree bytes and
                        # changes only the owning header Comment line (native-saved edit shape).
                        out_before = Path(imported["file"]).read_bytes()
                        saved = await call(UPDATE_VARIANT_TOOL, {
                            "project": "Demo.PRJ", "variant": imported["variant"],
                            "expected_sha256": params["sha256"], "variant_name": "Été 25%s" % DEG,
                            "dry_run": False})
                        self.assertTrue(saved["name_change"]["after"])
                        out_after = Path(imported["file"]).read_bytes()
                        self.assertTrue(out_after.startswith(BOM), "BOM must survive a display-name edit")
                        self.assertNotIn(MOJIBAKE, out_after.decode("utf-8-sig"))
                        old_lines = out_before.split(b"\n")
                        new_lines = out_after.split(b"\n")
                        changed = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
                        self.assertEqual(len(changed), 1, changed)
                        self.assertIn(b"Comment=", changed[0][0])
                        self.assertEqual(ws.project_bytes(), before)
            ok = True
        finally:
            ws.cleanup(ok)


    def test_load_variant_bom_follows_body_content(self):
        # A BOM-less source whose body already carries a degree: the load block itself is ASCII,
        # so the old source/block rule emits a BOM-less UTF-8 file (mojibake on 8.1.7 save).
        asyncio.run(self._direct_run("load", degree=True))

    def test_grid_storage_variant_bom_follows_body_content(self):
        # Scalar storage edit: the candidate keeps the inherited degree comment; only the final
        # body content can decide the BOM, and the old source-only rule returns no BOM.
        asyncio.run(self._direct_run("storage", degree=True))

    def test_grid_storage_system_bom_follows_body_content(self):
        # Constructed storage system: source BOM/battery/profile are all ASCII, the inherited body
        # degree is the only non-ASCII, and the old rule still emits no BOM.
        asyncio.run(self._direct_run("system", degree=True))

    def test_direct_writer_ascii_body_stays_bomless(self):
        # No gratuitous upgrade: an ASCII body on a BOM-less source keeps the legacy no-BOM shape.
        asyncio.run(self._direct_run("storage", degree=False))

    async def _direct_run(self, kind, *, degree):
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        comment = ("Fixture 25.0%s" % DEG) if degree else "Fixture"
        if kind == "storage":
            source = (_DIRECT_ROOT.format(comment=comment, flags="$00") + _DIRECT_ARRAY
                      + _DIRECT_STORAGE_SYSTEM + _DIRECT_GRID_STORAGE + _DIRECT_CLOSE)
        elif kind == "system":
            source = (_DIRECT_ROOT.format(comment=comment, flags="$00100001") + _DIRECT_ARRAY
                      + _DIRECT_PV_SYSTEM + _DIRECT_CLOSE)
        else:
            source = (_DIRECT_ROOT.format(comment=comment, flags="$00000000") + _DIRECT_ARRAY
                      + _DIRECT_PV_SYSTEM + _DIRECT_CLOSE)
        ws = _direct_workspace(source)
        before = ws.project_bytes()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    if kind in ("load", "system"):
                        _write_raw_load(ws)
                        prepared = await call(PREPARE_LOAD_TOOL, {
                            "csv_name": "raw.csv", "year": 2025, "output_name": "load.csv",
                            "utc_offset_hours": 8,
                            "time_basis": "local_standard_interval_start", "dry_run": False})
                        self.assertEqual(prepared["status"], "prepared", prepared)
                    if kind == "load":
                        made = await call(LOAD_TOOL, {
                            "project": "Demo.PRJ", "source_variant": "VC1", "new_variant": "VC2",
                            "load_manifest_name": "load_load.json", "dry_run": False})
                    elif kind == "storage":
                        made = await call(STORAGE_TOOL, {
                            "project": "Demo.PRJ", "source_variant": "VC1", "new_variant": "VC2",
                            "updates": {"initial_soc": 0.55}, "dry_run": False})
                    else:
                        _write_battery(ws)
                        made = await call(STORAGE_SYSTEM_TOOL, {
                            "project": "Demo.PRJ", "source_variant": "VC1", "new_variant": "VC2",
                            "battery": _BATTERY, "storage": _STORAGE,
                            "load_manifest_name": "load_load.json", "dry_run": False})
                    self.assertEqual(made["status"], "created", made)
                    out = Path(made["artifacts"]["variant"]).read_bytes()
                    self.assertEqual(out.startswith(BOM), degree,
                                     "%s: BOM %r must follow non-ASCII body=%r"
                                     % (kind, out[:3], degree))
                    text = out.decode("utf-8-sig")
                    if degree:
                        self.assertNotIn(MOJIBAKE, text, "degree must not become Â°")
                        self.assertEqual(out.count(b"\xc2\xb0"), 1)
                        self.assertNotIn(b"\xc3\x82\xc2\xb0", out)
                    self.assertEqual(ws.project_bytes(), before,
                                     "source variant must stay byte-identical")
            ok = True
        finally:
            ws.cleanup(ok)


if __name__ == "__main__":
    unittest.main()

"""Public stdio-MCP contract for per-source PAN geometry inheritance (round23).

Everything runs through a real MCP server session over a synthetic self-contained workspace
(no vendor files, no private paths) and ZERO PVsyst CLI/Native/GUI: the ``PVsystCLI.exe`` is an
inert placeholder and the native import is only requested with ``dry_run=True`` (in-memory).

Pinned external boundary:

- Two ordinary fixed-plane subarrays that reference a DIFFERENT genuine PAN model now author one
  grouped scene whose two named tables each carry their own module geometry (feed-in for the
  opaque built-in PAN reference; the released single-plane / homogeneous path is unchanged).
  The per-table ``PanneauPVName`` is written and verified from each own orientation's geometry,
  not the first PAN spread over every table. ``NoSubArrayPar``/``NoField3D``/``NoField3DCreated``
  bind each module-layout table to its own source subarray (never the orientation angle).
- The source variant bytes are never modified.
- A distinct PAN without per-source geometry is refused (missing geometry, not a silent reuse of
  the first PAN). Same-PAN grouped authoring keeps its existing behaviour (covered elsewhere).
"""
import asyncio
import copy
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]
CREATE_TOOL = "pvsyst_create_house_pv_scene"
READ_SCENE_TOOL = "pvsyst_read_house_pv_scene"
IMPORT_TOOL = "pvsyst_import_house_pv_scene"
NATIVE_NOT_LOADED = ("imported", "native_consumption_verified",
                     "native_geometry_verified", "native_module_layout_verified")
FAKE_CLI = b"Not an executable: this contract must not run PVsyst"
NMOD, NSTR = 30, 1

# Genuine commercial PanneauPVName token0 + metres from the installed original records.
HET = [
    {"id": 1, "name": "Array A", "tilt": 25.0, "azimuth": 20.0,
     "pan": "Generic_Mono_300W.PAN",
     "geo": {"model_name": "Mono 300 Wp 60 cells", "nom_power_wp": 300.0,
             "width_m": 0.992, "length_m": 1.640, "geometry_depth_m": 0.035}},
    {"id": 2, "name": "Array B", "tilt": 25.0, "azimuth": -70.0,
     "pan": "Generic_Mono_440W_Half.PAN",
     "geo": {"model_name": "Mono 440 Wp Twin 144 half-cells", "nom_power_wp": 440.0,
             "width_m": 1.052, "length_m": 2.115, "geometry_depth_m": 0.035}},
]

ORIENT = """  PVObject_=pvOrient
    NoOrient={id}
    Flags=$00
    FieldType=FixedPlane
    FieldTilt={tilt}
    FieldAzim={azimuth}
    BaseSlope=0
    UsesTiltAver=False
    UsesAzimAver=False
    UsesSlopeAver=False
  End of TOrientGroup
"""
BRANCH = """InverterNode Start;
  NElements=1
  SubArrayId={id}
  SubArrayName={name}
  Children=Start
    StringNode Start;
      NElements={nstr}
      SubArrayId={id}
      SubArrayName={name}
    StringNode End;
  Children=End
InverterNode End;
"""
ARRAY = """  PVObject_=pvSubArray
    Comment={name}
    SubArrayId={id}
    NoOrientation={orient}
    Version=8.1.6
    PowerSharingInverterId=-1
    PVModule={pan}
    GInverter=inverter.OND
    NModSerie={nmod}
    NStringCh={nstr}
    NStrOrient1={nstr}
    NInverter=1
    NInvMPPT=1
  End of PVObject pvSubArray
"""


def _pan_text(model, width, height, pnom):
    """Clone of the installed text PAN format with the genuine R22 300/440 commercial facts.

    This is the original loose-PAN file layout (pvModule + pvCommercial), not a fabricated
    vendor I-V curve: only Model/Width/Height/PNom carry the genuine commercial identity so
    ``module_profile`` can read them from a readable file and validate the caller's explicit data.
    """
    return ("PVObject_=pvModule\n  Version=7.2\n  Flags=$00900043\n\n"
            "  PVObject_Commercial=pvCommercial\n    Manufacturer=Generic\n"
            "    Model=%s\n    Width=%s\n    Height=%s\n    Depth=0.035\n"
            "  End of PVObject pvCommercial\n\n  PNom=%s\nEnd of PVObject pvModule\n"
            % (model, width, height, pnom))


def _site_text(latitude=45.0, longitude=8.0, altitude=250.0):
    return ("PVObject_=pvSite\n  Latitude=%s\n  Longitude=%s\n  Altitude=%s\n"
            "End of PVObject pvSite\n" % (latitude, longitude, altitude))


def _prj_text():
    return ("PVObject_=pvProject\n  Version=7.2\n  MeteoFileName=Demo.MET\n"
            "  PVObject_SitePrj=pvSite\n    NomF=Demo.SIT\n  End of PVObject pvSite\n"
            "End of PVObject pvProject\n")


def _source_text(groups, site_ref=None, met_ref=None):
    text = "PVObject_=pvVCalcul\n  Version=8.1.6\n  Flags=$00100001\n"
    text += "  PVObject_SiteSimul=pvSite\n    Latitude=45.0\n    Longitude=8.0\n    Altitude=250\n"
    if site_ref:
        text += "    NomF=%s\n" % site_ref
    text += "  End of PVObject pvSite\n"
    if met_ref:
        text += ("  PVObject_MeteoSimul=pvMeteo\n    NomF=%s\n  End of PVObject pvMeteo\n" % met_ref)
    text += "  Orientations, list of=%d TOrientGroup\n" % len(groups)
    for g in groups:
        text += ORIENT.format(id=g["id"], tilt=g["tilt"], azimuth=g["azimuth"])
    text += "  End of Orientations\n"
    text += ("  PVObject_PVMainArray=pvMainArray\n  PVObject_SystemCircuit=pvCircuit\n"
             "  Version=8.0.0\n  BaseNode=Start\nInjectionPointNode Start;\nChildren=Start\n")
    text += "".join(BRANCH.format(nstr=NSTR, **g) for g in groups)
    text += "Children=End\nInjectionPointNode End;\n  BaseNode=End\n  End of PVObject pvCircuit\n"
    for g in groups:
        text += ARRAY.format(id=g["id"], name=g["name"], orient=g["id"], pan=g["pan"], nmod=NMOD, nstr=NSTR)
    text += "  End of PVObject pvMainArray\n"
    text += "  PVObject_System=pvSystem\n    SystemType=Grid\n  End of PVObject pvSystem\n"
    text += "  PVObject_Ombrage=pvShading\n    Flags=$00\n  End of PVObject pvShading\n"
    text += "  VarList, list of=0 TVarInit\n  End of the List VarList\nEnd of PVObject pvVCalcul\n"
    return text


def _rotate_z(point, degrees):
    r = math.radians(degrees)
    x, y, z = point
    return [x * math.cos(r) - y * math.sin(r), x * math.sin(r) + y * math.cos(r), z]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _normal(a, b, c):
    n = _cross([b[i] - a[i] for i in range(3)], [c[i] - a[i] for i in range(3)])
    h = math.hypot(*n)
    return [v / h for v in n]


def _tables(groups):
    out = []
    for idx, g in enumerate(groups):
        out.append({"name": "Table %s" % g["name"][-1], "orientation_id": g["id"],
                    "module_rows": 15, "module_columns": 2, "orientation": "landscape",
                    "row_spacing_m": 0.0, "column_spacing_m": 0.0,
                    "origin_m": [idx * 40.0, 0.0, 10.0], "roof_mesh": "Roof %s" % g["name"][-1],
                    "module_geometry": g["geo"]})
    return out


def _house(tables, groups):
    az0 = min(groups, key=lambda g: g["id"])["azimuth"]
    meshes = []
    for idx, g in enumerate(groups):
        w, length = g["geo"]["width_m"], g["geo"]["length_m"]
        tilt = math.radians(g["tilt"])
        # Landscape: column span = 2 * length, slope span = 15 * width.
        span, slope = 2 * length, 15 * w
        down, drop = slope * math.cos(tilt), slope * math.sin(tilt)
        local = [[0.0, 0.0, 0.0], [span, 0.0, 0.0], [span, down, drop], [0.0, down, drop]]
        r = az0 - g["azimuth"]
        origin = tables[idx]["origin_m"]
        corners = [[origin[k] + _rotate_z(p, r)[k] for k in range(3)] for p in local]
        cx = [sum(c[k] for c in corners) / 4.0 for k in range(3)]
        wide = [[cx[k] + (c[k] - cx[k]) * 3.0 for k in range(3)] for c in corners]
        n = _normal(wide[0], wide[1], wide[2])
        wide = [[p[k] - 0.5 * n[k] for k in range(3)] for p in wide]
        meshes.append({"name": "Roof %s" % g["name"][-1], "vertices": wide,
                       "triangles": [[0, 1, 2], [0, 2, 3]]})
    return {"kind": "mesh", "meshes": meshes}


def _payload(response):
    if response.is_error:
        raise AssertionError(response.content)
    return json.loads("\n".join(item.text for item in response.content if hasattr(item, "text")))


class _Workspace:
    def __init__(self, groups, pan_files=None, with_site_met=False):
        self.tmp = Path(tempfile.mkdtemp(prefix="pvsyst-per-source-pan-"))
        self.root = self.tmp / "PVsyst8.1_Data"
        for folder in ("Projects", "UserData", "Results", "Sites", "Meteo", "ComposPV"):
            (self.root / folder).mkdir(parents=True)
        modules = self.root / "ComposPV" / "PVmodules"
        modules.mkdir()
        for name, text in (pan_files or {}).items():
            (modules / name).write_text(text, encoding="utf-8")
        prj = _prj_text() if with_site_met else "PVObject_=pvProject\nEnd of PVObject pvProject\n"
        (self.root / "Projects" / "Demo.PRJ").write_text(prj, encoding="utf-8")
        source = (_source_text(groups, site_ref="Demo.SIT", met_ref="Demo.MET") if with_site_met
                  else _source_text(groups))
        (self.root / "Projects" / "Demo.VC1").write_text(source, encoding="utf-8")
        if with_site_met:
            (self.root / "Sites" / "Demo.SIT").write_text(_site_text(), encoding="utf-8")
            (self.root / "Meteo" / "Demo.MET").write_bytes(b"Non-native met fixture")
        self.cli = self.root / "PVsystCLI.exe"
        self.cli.write_bytes(FAKE_CLI)

    def project_bytes(self):
        return (self.root / "Projects" / "Demo.VC1").read_bytes()

    def params(self):
        return StdioServerParameters(command=sys.executable,
                                     args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO),
                                     env=dict(os.environ, PVSYST_WORKSPACE=str(self.root),
                                              PVSYST_CLI=str(self.cli)))

    def cleanup(self, ok):
        if ok:
            shutil.rmtree(self.tmp)
        else:
            print("Retained per-source PAN failure fixture: " + str(self.tmp), file=sys.stderr)


class PerSourcePanGeometryBoundary(unittest.TestCase):
    def test_heterogeneous_pan_per_owner_geometry(self):
        asyncio.run(self._hetero())

    async def _hetero(self):
        ws = _Workspace(HET)
        before = ws.project_bytes()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    tables = _tables(HET)
                    args = {"project": "Demo.PRJ", "variant": "VC1", "output_name": "HeteroPan.pvc2",
                            "house": _house(tables, HET), "layout": {"tables": tables}}
                    preview = await call(CREATE_TOOL, args)
                    self.assertEqual(preview["status"], "ready", preview)
                    self.assertEqual(ws.project_bytes(), before, "preview must not write the source")
                    created = await call(CREATE_TOOL, dict(args, dry_run=False))
                    self.assertEqual(created["status"], "created", created)
                    self.assertEqual(ws.project_bytes(), before, "source variant must be unchanged")
                    # Public response carries each group's own commercial model + metres.
                    groups_out = created["source"]["orientations"]
                    self.assertEqual([g["id"] for g in groups_out], [1, 2])
                    self.assertEqual([g["module_geometry"]["name"] for g in groups_out],
                                     ["Mono 300 Wp 60 cells", "Mono 440 Wp Twin 144 half-cells"])
                    self.assertAlmostEqual(groups_out[0]["module_geometry"]["width_m"], 0.992, places=6)
                    self.assertAlmostEqual(groups_out[0]["module_geometry"]["length_m"], 1.640, places=6)
                    self.assertAlmostEqual(groups_out[1]["module_geometry"]["width_m"], 1.052, places=6)
                    self.assertAlmostEqual(groups_out[1]["module_geometry"]["length_m"], 2.115, places=6)

                    loaded = await call(READ_SCENE_TOOL, {"scene_name": "HeteroPan.pvc2"})
                    self.assertEqual(loaded["status"], "ready", loaded)
                    loaded_tables = loaded["create_arguments"]["layout"]["tables"]
                    # The recipe keeps the caller-declared (raw) geometry; names are model_name.
                    self.assertEqual([t["module_geometry"]["model_name"] for t in loaded_tables],
                                     ["Mono 300 Wp 60 cells", "Mono 440 Wp Twin 144 half-cells"])
                    self.assertEqual([t["orientation_id"] for t in loaded_tables], [1, 2])

                    got = await call(IMPORT_TOOL, {"scene_name": "HeteroPan.pvc2",
                                                   "variant_name": "HeteroPan", "method": "native",
                                                   "shading_mode": "module_layout", "dry_run": True})
                    self.assertEqual(got["status"], "ready_to_import", got)
                    self.assertTrue(got["precheck_passed"], got)
                    ver = got["verification"]
                    self.assertEqual(ver["pv_bindings"], "stored_consistent", ver)
                    self.assertEqual(ver["module_layout"]["status"], "stored_consistent", ver)
                    self.assertEqual(ver["module_layout"]["module_count"], 2 * NMOD, ver)
                    hw = ver["hardware"]
                    self.assertEqual(hw["source_orientation_ids"], [1, 2], hw)
                    # Per-subarray PAN reference preserved (genuine, not collapsed to the first).
                    self.assertEqual([row["pan"] for row in hw["hardware_mapping"]],
                                     ["Generic_Mono_300W.PAN", "Generic_Mono_440W_Half.PAN"], hw)
                    for key in NATIVE_NOT_LOADED:
                        self.assertFalse(got[key], key)
                    self.assertEqual(list((ws.root / "Results").iterdir()), [])
                    self.assertEqual(ws.project_bytes(), before)
            ok = True
        finally:
            ws.cleanup(ok)

    def test_distinct_pan_without_geometry_is_refused(self):
        asyncio.run(self._missing_geometry())

    async def _missing_geometry(self):
        ws = _Workspace(HET)
        before = ws.project_bytes()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    tables = _tables(HET)
                    for table in tables:
                        table.pop("module_geometry")
                    args = {"project": "Demo.PRJ", "variant": "VC1", "output_name": "NoGeom.pvc2",
                            "house": _house(tables, HET), "layout": {"tables": tables}}
                    blocked = await call(CREATE_TOOL, dict(args, dry_run=False))
                    self.assertEqual(blocked["status"], "blocked", blocked)
                    self.assertIn("module geometry", " ".join(blocked["issues"]).lower())
                    self.assertEqual(ws.project_bytes(), before,
                                     "a refused create must not touch the source variant")
            ok = True
        finally:
            ws.cleanup(ok)

    def test_readable_same_pan_multigroup_no_regression(self):
        asyncio.run(self._same_pan_multigroup())

    async def _same_pan_multigroup(self):
        pan_files = {"Generic_Mono_300W.PAN": _pan_text("Mono 300 Wp 60 cells", 0.992, 1.640, 300.0)}
        for count in (2, 3):
            groups = [{"id": i + 1, "name": "Array %s" % "ABC"[i], "tilt": 25.0,
                       "azimuth": (20.0, -70.0, 110.0)[i], "pan": "Generic_Mono_300W.PAN",
                       "geo": {"model_name": "Mono 300 Wp 60 cells", "nom_power_wp": 300.0,
                               "width_m": 0.992, "length_m": 1.640, "geometry_depth_m": 0.035}}
                      for i in range(count)]
            ws = _Workspace(groups, pan_files=pan_files)
            before = ws.project_bytes()
            ok = False
            try:
                async with stdio_client(ws.params()) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()

                        async def call(tool, args):
                            return _payload(await session.call_tool(tool, args))

                        tables = _tables(groups)
                        args = {"project": "Demo.PRJ", "variant": "VC1",
                                "output_name": "SamePan%d.pvc2" % count,
                                "house": _house(tables, groups), "layout": {"tables": tables}}
                        created = await call(CREATE_TOOL, dict(args, dry_run=False))
                        self.assertEqual(created["status"], "created", created)
                        self.assertEqual(ws.project_bytes(), before)
                        groups_out = created["source"]["orientations"]
                        self.assertEqual([g["id"] for g in groups_out], list(range(1, count + 1)))
                        # A readable SAME PAN is read per group (never rejected, never spread stale).
                        for group in groups_out:
                            self.assertEqual(group["module_geometry"]["name"], "Mono 300 Wp 60 cells")
                            self.assertEqual(group["module_geometry"]["width_m"], 0.992)
                        self.assertEqual(created["source"]["module_total"], count * NMOD)
                        loaded = await call(READ_SCENE_TOOL, {"scene_name": "SamePan%d.pvc2" % count})
                        self.assertEqual(loaded["status"], "ready", loaded)
                ok = True
            finally:
                ws.cleanup(ok)

    def test_opaque_mixed_three_group_preview(self):
        asyncio.run(self._mixed_three_group())

    async def _mixed_three_group(self):
        specs = [("Generic_Mono_300W.PAN", "Mono 300 Wp 60 cells", 300.0, 0.992, 1.640, 20.0),
                 ("Generic_Mono_440W_Half.PAN", "Mono 440 Wp Twin 144 half-cells", 440.0, 1.052, 2.115, -70.0),
                 ("Generic_Mono_550W.PAN", "Mono 550 Wp High Efficiency", 550.0, 1.134, 2.279, 110.0)]
        groups = [{"id": i + 1, "name": "Array %s" % "ABC"[i], "tilt": 25.0, "azimuth": az,
                   "pan": pan,
                   "geo": {"model_name": model, "nom_power_wp": pnom, "width_m": w,
                           "length_m": length, "geometry_depth_m": 0.035}}
                  for i, (pan, model, pnom, w, length, az) in enumerate(specs)]
        ws = _Workspace(groups)
        before = ws.project_bytes()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    tables = _tables(groups)
                    args = {"project": "Demo.PRJ", "variant": "VC1", "output_name": "Mixed3.pvc2",
                            "house": _house(tables, groups), "layout": {"tables": tables}}
                    created = await call(CREATE_TOOL, dict(args, dry_run=False))
                    self.assertEqual(created["status"], "created", created)
                    self.assertEqual(ws.project_bytes(), before)
                    groups_out = created["source"]["orientations"]
                    # N>=3 heterogeneous owners each carry their own geometry/PAN, never collapsed.
                    self.assertEqual([g["module_geometry"]["name"] for g in groups_out],
                                     [spec[1] for spec in specs])
                    self.assertEqual([g["module_geometry"]["nom_power"] for g in groups_out],
                                     [spec[2] for spec in specs])
                    self.assertEqual(created["source"]["module_total"], 3 * NMOD)
                    loaded = await call(READ_SCENE_TOOL, {"scene_name": "Mixed3.pvc2"})
                    self.assertEqual(loaded["status"], "ready", loaded)
                    self.assertEqual([t["orientation_id"] for t in loaded["create_arguments"]["layout"]["tables"]],
                                     [1, 2, 3])
            ok = True
        finally:
            ws.cleanup(ok)

    def test_readable_pan_explicit_mismatch_refused(self):
        asyncio.run(self._mismatch())

    async def _mismatch(self):
        # A foreign/stale explicit geometry (Model 440 named) against a readable 300W PAN must be
        # refused, never trusted over the genuine readable file, and must not touch the source.
        pan_files = {"Generic_Mono_300W.PAN": _pan_text("Mono 300 Wp 60 cells", 0.992, 1.640, 300.0)}
        groups = [{"id": 1, "name": "Array A", "tilt": 25.0, "azimuth": 20.0,
                   "pan": "Generic_Mono_300W.PAN",
                   "geo": {"model_name": "Mono 440 Wp Twin 144 half-cells", "nom_power_wp": 440.0,
                           "width_m": 1.052, "length_m": 2.115, "geometry_depth_m": 0.035}},
                  {"id": 2, "name": "Array B", "tilt": 25.0, "azimuth": -70.0,
                   "pan": "Generic_Mono_440W_Half.PAN",
                   "geo": {"model_name": "Mono 440 Wp Twin 144 half-cells", "nom_power_wp": 440.0,
                           "width_m": 1.052, "length_m": 2.115, "geometry_depth_m": 0.035}}]
        ws = _Workspace(groups, pan_files=pan_files)
        before = ws.project_bytes()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    tables = _tables(groups)
                    args = {"project": "Demo.PRJ", "variant": "VC1", "output_name": "Mismatch.pvc2",
                            "house": _house(tables, groups), "layout": {"tables": tables}}
                    blocked = await call(CREATE_TOOL, dict(args, dry_run=False))
                    self.assertEqual(blocked["status"], "blocked", blocked)
                    self.assertIn("disagree", " ".join(blocked["issues"]).lower())
                    self.assertEqual(ws.project_bytes(), before)
            ok = True
        finally:
            ws.cleanup(ok)

    def test_highlevel_native_mixed_prepare_read(self):
        asyncio.run(self._highlevel_mixed())

    async def _highlevel_mixed(self):
        # High-level HouseTask, scene_import_method=native, mixed 2-group opaque PAN pair: the
        # prepare stage must forward per-table geometry, sum each owner's real nameplate, and NOT
        # blanket-reject the heterogeneous pair. dry_run never launches native/exe.
        ws = _Workspace(HET, with_site_met=True)
        before = ws.project_bytes()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    tables = _tables(HET)
                    spec = {"source_project": "Demo.PRJ", "source_variant": "VC1",
                            "new_project": "Delivery.PRJ", "variant_name": "DeliveryHouse",
                            "year": 2020, "house": _house(tables, HET),
                            "layout": {"tables": tables}, "scene_import_method": "native"}
                    result = await call("pvsyst_run_house_task", {"spec": spec, "dry_run": True})
                    self.assertIn(result["status"], ("ready",), result)
                    self.assertEqual(result["plan"]["nominal_dc_kw"], 0.3 * NMOD + 0.44 * NMOD,
                                     result["plan"]["nominal_dc_kw"])
                    self.assertEqual(result["plan"]["pv_tables"], 2, result["plan"])
                    self.assertEqual(ws.project_bytes(), before,
                                     "a native preview must not write the source variant")
                    self.assertFalse((ws.root / "Projects" / "Delivery.PRJ").exists())
            ok = True
        finally:
            ws.cleanup(ok)


    def test_highlevel_native_three_group_prepare(self):
        asyncio.run(self._highlevel_three_group())

    async def _highlevel_three_group(self):
        # High-level HouseTask, native module_layout with an explicit per-cell binding, THREE
        # heterogeneous owners (distinct PAN/geometry/nameplate). The prepare stage must walk the
        # general N-group path (no ">2 same PAN" or high-level blanket reject) and sum nameplate.
        specs = [("Generic_Mono_300W.PAN", "Mono 300 Wp 60 cells", 300.0, 0.992, 1.640, 20.0),
                 ("Generic_Mono_440W_Half.PAN", "Mono 440 Wp Twin 144 half-cells", 440.0, 1.052, 2.115, -70.0),
                 ("Generic_Mono_550W.PAN", "Mono 550 Wp High Efficiency", 550.0, 1.134, 2.279, 110.0)]
        groups = [{"id": i + 1, "name": "Array %s" % "ABC"[i], "tilt": 25.0, "azimuth": az,
                   "pan": pan,
                   "geo": {"model_name": model, "nom_power_wp": pnom, "width_m": w,
                           "length_m": length, "geometry_depth_m": 0.035}}
                  for i, (pan, model, pnom, w, length, az) in enumerate(specs)]
        tables = _tables(groups)
        binding = {"tables": [{"name": t["name"], "subarray_id": t["orientation_id"],
                               "cells": [{"row": r, "col": c, "global_string": t["orientation_id"]}
                                         for r in range(1, t["module_rows"] + 1)
                                         for c in range(1, t["module_columns"] + 1)]}
                              for t in tables]}
        ws = _Workspace(groups, with_site_met=True)
        before = ws.project_bytes()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    spec = {"source_project": "Demo.PRJ", "source_variant": "VC1",
                            "new_project": "Delivery3.PRJ", "variant_name": "DeliveryHouse3",
                            "year": 2020, "house": _house(tables, groups),
                            "layout": {"tables": tables}, "scene_import_method": "native",
                            "shading_mode": "module_layout", "string_layout": binding}
                    result = await call("pvsyst_run_house_task", {"spec": spec, "dry_run": True})
                    self.assertIn(result["status"], ("ready",), result)
                    self.assertEqual(result["plan"]["nominal_dc_kw"],
                                     sum(spec[2] for spec in specs) * NMOD / 1000.0,
                                     result["plan"]["nominal_dc_kw"])
                    self.assertEqual(result["plan"]["pv_tables"], 3, result["plan"])
                    self.assertEqual(ws.project_bytes(), before)
            ok = True
        finally:
            ws.cleanup(ok)


if __name__ == "__main__":
    unittest.main()

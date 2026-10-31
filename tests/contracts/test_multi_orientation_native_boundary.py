"""Public stdio-MCP contract for multi fixed-orientation house scenes (round17).

External, persisted behaviour only; no internal helper import. Everything runs through a real
MCP server session with a synthetic self-contained workspace (no vendor files, no private
paths) and ZERO PVsyst CLI/Native/GUI: the ``PVsystCLI.exe`` here is an inert placeholder that
cannot run, and native import is only requested with ``dry_run=True`` (in-memory precheck).

Pinned boundary (the new capability and its legacy compatibility):

- ``pvsyst_create_house_pv_scene`` authors a homogeneous-fixed GROUPED scene from one source
  variant with THREE ordinary fixed planes (ids 1/2/3, same PAN/OND, one exclusive subarray
  each) and three landscape tables, each bound to one source group, with an explicit per-cell
  ``string_layout`` (6 global strings = 2 per group, 15 modules per string). It stores version-2
  metadata and returns the three source orientations and their module totals.
- ``pvsyst_read_house_pv_scene`` round-trips the same three orientations and three named
  tables losslessly (``orientation_id`` per table preserved, table names unique).
- ``pvsyst_import_house_pv_scene(method='native', shading_mode='module_layout', dry_run=True)``
  builds and verifies the native records IN MEMORY: ``ready_to_import`` with
  ``precheck_passed`` and a ``stored_consistent`` binding/module-layout of the full module total.
  Nothing is written; native consumption flags stay false.
- Legacy routing is unchanged: a single-subarray scene (no ``orientation_id`` -> version 1) and a
  two-group scene still create/read/import-preview; a sparse-SubArrayId source (subarray ids
  7 then 2, orientations 1/2) keeps appearance-order identity and the source-list global prefix.
- Invalid shapes are refused before any variant is written: an undeclared ``orientation_id`` and a
  per-cell ``global_string`` outside its source subarray's range stop the create/import.

A separate opt-in test consumes the adapter-authored grouped scene with a REAL PVsyst install
(import save -> ONE numeric batch run -> TWO pure reads: a post-run variant persistence re-read
+ the batch summary) only when ``PVSYST_MULTI_ORIENT_NATIVE_CONFIG`` names a JSON config with
``workspace``/``cli``/``project`` and a ``batch_params_name`` (+ optional ``batch_rvt_name``,
``scene_name``/create arguments, or a pre-saved ``native_case_variant``); otherwise it is
skipped, never silently green. When ``PVSYST_MULTI_ORIENT_RAW_DIR`` is set the recorder persists
the FULL ``CallToolResult.model_dump()`` envelope (incl. ``is_error``) BEFORE any is_error
handling or payload parse; it never fabricates ``type``/``is_error`` and never reuses a parsed
payload in place of the raw envelope. One numeric + two pure readers matches the frozen flow
(only the variant read is moved after the numeric run; the reader count is unchanged, and the
Native numeric cap of one is not exceeded).
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
READ_VARIANT_TOOL = "pvsyst_get_variant_parameters"
NATIVE_NOT_LOADED = ("imported", "native_consumption_verified",
                     "native_geometry_verified", "native_module_layout_verified")
FAKE_CLI = b"Not an executable: this contract must not run PVsyst"

ORIENT = """  PVObject_=pvOrient
    NoOrient={id}
    Flags=$00
    FieldType={field_type}
    FieldTilt={tilt}
    FieldAzim={azimuth}
    BaseSlope=0
    UsesTiltAver={tilt_aver}
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
      NElements=2
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
    NModSerie=15
    NStringCh=2
    NStrOrient1=2
    NInverter=1
    NInvMPPT=1
  End of PVObject pvSubArray
"""
GROUPS3 = [{"id": 1, "name": "Array A", "tilt": 25.0, "azimuth": 20.0},
           {"id": 2, "name": "Array B", "tilt": 25.0, "azimuth": -70.0},
           {"id": 3, "name": "Array C", "tilt": 25.0, "azimuth": 110.0}]
MODULE = {"model_name": "Mono 300 Wp", "nom_power_wp": 300.0,
          "width_m": 0.992, "length_m": 1.640, "geometry_depth_m": 0.035}


def _source_text(groups):
    text = "PVObject_=pvVCalcul\n  Version=8.1.6\n  Flags=$00100001\n"
    text += "  PVObject_SiteSimul=pvSite\n    Latitude=45.0\n    Longitude=8.0\n    Altitude=250\n"
    text += "  End of PVObject pvSite\n"
    text += "  Orientations, list of=%d TOrientGroup\n" % len(groups)
    for g in groups:
        text += ORIENT.format(id=g.get("orient", g["id"]), field_type=g.get("field_type", "FixedPlane"),
                              tilt=g["tilt"], azimuth=g["azimuth"], tilt_aver=g.get("tilt_aver", "False"))
    text += "  End of Orientations\n"
    text += ("  PVObject_PVMainArray=pvMainArray\n  PVObject_SystemCircuit=pvCircuit\n"
             "  Version=8.0.0\n  BaseNode=Start\nInjectionPointNode Start;\nChildren=Start\n")
    text += "".join(BRANCH.format(**g) for g in groups)
    text += "Children=End\nInjectionPointNode End;\n  BaseNode=End\n  End of PVObject pvCircuit\n"
    for g in groups:
        text += ARRAY.format(id=g["id"], name=g["name"], orient=g.get("orient", g["id"]),
                             pan=g.get("pan", "module.PAN"))
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
        out.append({"name": "Table %s" % g["name"][-1], "orientation_id": g.get("orient", g["id"]),
                    "module_rows": 15, "module_columns": 2, "orientation": "landscape",
                    "row_spacing_m": 0.0, "column_spacing_m": 0.0,
                    "origin_m": [idx * 40.0, 0.0, 10.0], "roof_mesh": "Roof %s" % g["name"][-1]})
    return out


def _binding(tables, groups):
    entries = []
    for idx, table in enumerate(tables):
        first = idx * 2 + 1
        cells = [{"row": r, "col": c, "global_string": first + (c - 1)}
                 for r in range(1, 16) for c in (1, 2)]
        entries.append({"name": table["name"], "subarray_id": groups[idx]["id"], "cells": cells})
    return {"tables": entries}


def _house(tables, groups):
    # Three separate planar roof quads, each matching its table's tilted plane. rack local
    # footprint is identical across groups (same rows/cols/orientation/tilt/dimensions).
    tilt = math.radians(groups[0]["tilt"])
    span, down, drop = 2 * 1.640, 15 * 0.992 * math.cos(tilt), 15 * 0.992 * math.sin(tilt)
    local = [[0.0, 0.0, 0.0], [span, 0.0, 0.0], [span, down, drop], [0.0, down, drop]]
    az0 = min(groups, key=lambda g: g.get("orient", g["id"]))["azimuth"]
    meshes = []
    for idx, g in enumerate(groups):
        r = az0 - g["azimuth"]
        origin = tables[idx]["origin_m"]
        corners = [[origin[k] + _rotate_z(p, r)[k] for k in range(3)] for p in local]
        cx = [sum(c[k] for c in corners) / 4.0 for k in range(3)]
        wide = [[cx[k] + (c[k] - cx[k]) * 3.0 for k in range(3)] for c in corners]
        n = _normal(wide[0], wide[1], wide[2])
        wide = [[w[k] - 0.5 * n[k] for k in range(3)] for w in wide]
        meshes.append({"name": "Roof %s" % g["name"][-1], "vertices": wide,
                       "triangles": [[0, 1, 2], [0, 2, 3]]})
    return {"kind": "mesh", "meshes": meshes}


def _payload(response):
    if response.is_error:
        raise AssertionError(response.content)
    return json.loads("\n".join(item.text for item in response.content if hasattr(item, "text")))


class _Workspace:
    def __init__(self, groups, scene_source=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="pvsyst-multi-orient-"))
        self.root = self.tmp / "PVsyst8.1_Data"
        for folder in ("Projects", "UserData", "Results", "Sites", "Meteo", "ComposPV"):
            (self.root / folder).mkdir(parents=True)
        (self.root / "Projects" / "Demo.PRJ").write_text(
            "PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        (self.root / "Projects" / "Demo.VC1").write_text(
            scene_source if scene_source is not None else _source_text(groups), encoding="utf-8")
        self.cli = self.root / "PVsystCLI.exe"
        self.cli.write_bytes(FAKE_CLI)

    def project_bytes(self):
        return (self.root / "Projects" / "Demo.VC1").read_bytes()

    def params(self, root=None, cli=None):
        return StdioServerParameters(command=sys.executable,
                                     args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO),
                                     env=dict(os.environ, PVSYST_WORKSPACE=str(root or self.root),
                                              PVSYST_CLI=str(cli or self.cli)))

    def cleanup(self, ok):
        if ok:
            shutil.rmtree(self.tmp)
        else:
            print("Retained multi-orientation failure fixture: " + str(self.tmp), file=sys.stderr)


class MultiOrientationNativeBoundary(unittest.TestCase):
    def test_multi_fixed_public_create_read_import_preview(self):
        asyncio.run(self._grouped())

    async def _grouped(self):
        ws = _Workspace(GROUPS3)
        before = ws.project_bytes()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    tables = _tables(GROUPS3)
                    args = {"project": "Demo.PRJ", "variant": "VC1", "output_name": "Multi3.pvc2",
                            "house": _house(tables, GROUPS3), "layout": {"tables": tables},
                            "module_geometry": MODULE, "string_layout": _binding(tables, GROUPS3)}
                    created = await call(CREATE_TOOL, dict(args, dry_run=False))
                    self.assertEqual(created["status"], "created", created)
                    self.assertEqual([o["id"] for o in created["source"]["orientations"]], [1, 2, 3])
                    self.assertEqual([o["module_total"] for o in created["source"]["orientations"]], [30, 30, 30])
                    self.assertEqual(created["geometry"]["pv_table_count"], 3)
                    self.assertEqual(len({t["name"] for t in created["geometry"]["tables"]}), 3)
                    self.assertEqual(ws.project_bytes(), before, "source variant must be unchanged")

                    loaded = await call(READ_SCENE_TOOL, {"scene_name": "Multi3.pvc2"})
                    self.assertEqual(loaded["status"], "ready", loaded)
                    self.assertEqual([o["id"] for o in loaded["source"]["orientation_geometry"]], [1, 2, 3])
                    self.assertEqual([o["module_total"] for o in loaded["source"]["orientation_geometry"]], [30, 30, 30])
                    self.assertEqual([t["orientation_id"] for t in loaded["create_arguments"]["layout"]["tables"]],
                                     [1, 2, 3])
                    self.assertEqual(loaded["create_arguments"]["string_layout"]["tables"][2]["cells"][0]["global_string"], 5)

                    preview = await call(IMPORT_TOOL, {"scene_name": "Multi3.pvc2", "variant_name": "Multi3",
                                                       "method": "native", "shading_mode": "module_layout",
                                                       "dry_run": True})
                    self.assertEqual(preview["status"], "ready_to_import", preview)
                    self.assertTrue(preview["precheck_passed"], preview)
                    self.assertFalse(preview["native_variant_created"], preview)
                    ver = preview["verification"]
                    self.assertEqual(ver["pv_bindings"], "stored_consistent", ver)
                    self.assertEqual(ver["module_layout"]["status"], "stored_consistent", ver)
                    self.assertEqual(ver["module_layout"]["module_count"], 90, ver)
                    self.assertEqual(ver["hardware"]["source_orientation_ids"], [1, 2, 3], ver)
                    for key in NATIVE_NOT_LOADED:
                        self.assertFalse(preview[key], key)

                    # Negative: an undeclared orientation_id is refused by the create tool.
                    foreign = copy.deepcopy(args)
                    foreign["layout"]["tables"][1]["orientation_id"] = 9
                    foreign["output_name"] = "Foreign.pvc2"
                    blocked = await call(CREATE_TOOL, dict(foreign, dry_run=False))
                    self.assertEqual(blocked["status"], "blocked", blocked)
                    self.assertIn("orientation", " ".join(blocked["issues"]).lower())

                    # Negative: a per-cell global_string outside its source subarray's range is refused
                    # by the native precheck before any variant is written.
                    bad = copy.deepcopy(args)
                    bad["string_layout"]["tables"][0]["cells"][0]["global_string"] = 3
                    bad["output_name"] = "Overcap.pvc2"
                    made = await call(CREATE_TOOL, dict(bad, dry_run=False))
                    self.assertEqual(made["status"], "created", made)
                    over = await call(IMPORT_TOOL, {"scene_name": "Overcap.pvc2", "variant_name": "Overcap",
                                                     "method": "native", "shading_mode": "module_layout",
                                                     "dry_run": True})
                    self.assertEqual(over["status"], "blocked", over)
                    self.assertFalse(over["native_variant_created"], over)

                    self.assertEqual(list((ws.root / "Results").iterdir()), [])
            ok = True
        finally:
            ws.cleanup(ok)

    def test_legacy_single_two_and_sparse_still_route(self):
        asyncio.run(self._legacy())

    async def _legacy(self):
        cases = [
            ("Single", [{"id": 1, "name": "Array A", "tilt": 25.0, "azimuth": 20.0}],
             [{"name": "Table A", "module_rows": 15, "module_columns": 2, "orientation": "landscape",
               "row_spacing_m": 0.0, "column_spacing_m": 0.0, "origin_m": [0.0, 0.0, 10.0],
               "roof_mesh": "Roof A"}], "linear"),
            ("Two", GROUPS3[:2], None, "module_layout"),
            ("Sparse", [{"id": 7, "orient": 1, "name": "Array A", "tilt": 25.0, "azimuth": 20.0},
                        {"id": 2, "orient": 2, "name": "Array B", "tilt": 25.0, "azimuth": -70.0}],
             None, "module_layout"),
        ]
        for label, groups, fixed_tables, shading in cases:
            with self.subTest(label=label):
                ws = _Workspace(groups)
                ok = False
                try:
                    async with stdio_client(ws.params()) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()

                            async def call(tool, args):
                                return _payload(await session.call_tool(tool, args))

                            tables = fixed_tables or _tables(groups)
                            args = {"project": "Demo.PRJ", "variant": "VC1",
                                    "output_name": label + ".pvc2",
                                    "house": _house(tables, groups), "layout": {"tables": tables},
                                    "module_geometry": MODULE}
                            if fixed_tables is None:
                                args["string_layout"] = _binding(tables, groups)
                            created = await call(CREATE_TOOL, dict(args, dry_run=False))
                            self.assertEqual(created["status"], "created", created)
                            loaded = await call(READ_SCENE_TOOL, {"scene_name": label + ".pvc2"})
                            self.assertEqual(loaded["status"], "ready", loaded)
                            if fixed_tables is not None:
                                self.assertNotIn("orientations", loaded["source"])
                                self.assertNotIn("orientation_id",
                                                 loaded["create_arguments"]["layout"]["tables"][0])
                            else:
                                self.assertEqual([o["id"] for o in loaded["source"]["orientation_geometry"]],
                                                 [g.get("orient", g["id"]) for g in groups])
                                self.assertEqual(
                                    [t["subarray_id"] for t in loaded["create_arguments"]["string_layout"]["tables"]],
                                    [g["id"] for g in groups])
                            preview = await call(IMPORT_TOOL, {"scene_name": label + ".pvc2",
                                                               "variant_name": label, "method": "native",
                                                               "shading_mode": shading, "dry_run": True})
                            self.assertEqual(preview["status"], "ready_to_import", preview)
                            self.assertTrue(preview["precheck_passed"], preview)
                    ok = True
                finally:
                    ws.cleanup(ok)

    def test_real_native_consumption_opt_in(self):
        """Consume the grouped scene with a REAL PVsyst install: import save -> ONE numeric batch -> TWO pure reads.

        Opt-in only: skipped unless PVSYST_MULTI_ORIENT_NATIVE_CONFIG points to a JSON file
        ``{"workspace","cli","project", "batch_params_name","batch_rvt_name", optional
        "scene_name"/"variant_name" or "create_args", optional "native_case_variant"}`` describing a
        genuine source project/variant. The guard rejects a fake/placeholder CLI so the real boundary is
        never reported green by an inert sentinel. The numeric run uses the ACTUAL saved VC id returned by
        the import (or a pre-saved ``native_case_variant`` consistent with the workspace) and a batch
        parameters/rvt pair, never the display name and never a guessed ``year``.
        """
        config_path = os.environ.get("PVSYST_MULTI_ORIENT_NATIVE_CONFIG")
        if not config_path:
            self.skipTest("real native NOTRUN: set PVSYST_MULTI_ORIENT_NATIVE_CONFIG (this stage is prep only)")
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        workspace, cli = Path(config["workspace"]), Path(config["cli"])
        if not cli.is_file() or cli.stat().st_size < 4096:
            self.fail("NoFakePAN/NoFakeCLI: real PVsyst CLI required, got %r" % (cli,))
        asyncio.run(self._native(workspace, cli, config))

    async def _native(self, workspace, cli, config):
        params = StdioServerParameters(command=sys.executable,
                                       args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO),
                                       env=dict(os.environ, PVSYST_WORKSPACE=str(workspace),
                                                PVSYST_CLI=str(cli)))
        raw_dir = os.environ.get("PVSYST_MULTI_ORIENT_RAW_DIR")
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(tool, args, tag):
                    reply = await session.call_tool(tool, args)
                    if raw_dir:                       # persist the FULL SDK reply BEFORE any handling
                        Path(raw_dir).mkdir(parents=True, exist_ok=True)
                        envelope = (reply.model_dump(mode="json") if hasattr(reply, "model_dump")
                                    else {"unmodeled": True})
                        Path(raw_dir, tag + ".sdk.json").write_text(
                            json.dumps(envelope, ensure_ascii=False, sort_keys=True, default=str),
                            encoding="utf-8")
                    return _payload(reply)            # raises on is_error; errors are never swallowed

                project = config.get("project") or (config.get("create_args") or {}).get("project")
                self.assertIsNotNone(project, "config needs 'project' (or create_args.project)")
                # Stage A (0 CLI): obtain the REAL saved native VC id, or reuse a pre-saved one.
                if config.get("native_case_variant"):
                    variant = config["native_case_variant"]
                else:
                    if config.get("scene_name"):
                        scene_name, variant_name = config["scene_name"], config.get("variant_name", "MultiNative")
                    else:
                        created = await call(CREATE_TOOL, dict(config["create_args"], dry_run=False), "create")
                        self.assertEqual(created["status"], "created", created)
                        scene_name, variant_name = config["create_args"]["output_name"], "MultiNative"
                    made = await call(IMPORT_TOOL, {"scene_name": scene_name, "variant_name": variant_name,
                                                    "method": "native", "shading_mode": "module_layout",
                                                    "dry_run": False, "confirm": True}, "import")
                    self.assertEqual(made["status"], "native_variant_created", made)
                    self.assertTrue(made["stored_module_layout_verified"], made)
                    for key in NATIVE_NOT_LOADED:      # import writes STORED only, no consumption
                        self.assertFalse(made[key], key)
                    project, variant = made["project"], made["variant"]     # actual returned VC id
                # ONE numeric BATCH run (real native consumption) on the SAVED variant id. Native
                # legitimately rewrites the DERIVED target (8.0/8.1.6 -> 8.1.7 SaveAs); that write is
                # the allowed business output and is judged by structure, never by whole-byte identity.
                run = await call("pvsyst_run_simulation", {
                    "project": project, "variant": variant,
                    "batch_params_name": config["batch_params_name"],
                    "batch_rvt_name": config.get("batch_rvt_name", "")}, "run")
                self.assertIsNone(run.get("pdf"), run)
                self.assertGreaterEqual(run.get("batch_runs") or 0, 1, run)
                for path in run.get("batch_hourly") or []:
                    self.assertTrue(Path(path).is_file(), "missing hourly CSV: %s" % path)
                # Pure re-read #1, AFTER consumption: the persisted source data must survive the native
                # save. The 8.1.7 after-file's stored layout classification may move off
                # stored_consistent (mlShd3DCalculated / non-PVCollada source), so assert the STATIC
                # SCENE NOTE (source orientation geometry, 90 modules / 6 strings capacity, active
                # scene, configured layout), never byte identity or a stored_consistent flag.
                saved = await call(READ_VARIANT_TOOL, {"project": project, "variant": variant},
                                   "readvariant")
                self.assertEqual([o["id"] for o in saved["orientations"]], [1, 2, 3])
                self.assertEqual([round(float(o["values"]["FieldTilt"]), 3) for o in saved["orientations"]],
                                 [25.0, 25.0, 25.0])
                self.assertEqual([round(float(o["values"]["FieldAzim"]), 3) for o in saved["orientations"]],
                                 [20.0, -70.0, 110.0])
                self.assertEqual([s["id"] for s in saved["subarrays"]], [1, 2, 3])
                self.assertEqual([int(s["values"]["NModSerie"]) for s in saved["subarrays"]], [15, 15, 15])
                self.assertEqual([int(s["values"]["NStringCh"]) for s in saved["subarrays"]], [2, 2, 2])
                self.assertEqual([int(s["values"]["NInvMPPT"]) for s in saved["subarrays"]], [1, 1, 1])
                self.assertEqual(sum(int(s["values"]["NModSerie"]) * int(s["values"]["NStringCh"])
                                     for s in saved["subarrays"]), 90)
                self.assertEqual(sum(int(s["values"]["NStringCh"]) for s in saved["subarrays"]), 6)
                scene = saved["shading_scene"]
                self.assertEqual(scene["mode"], "active", scene)
                self.assertTrue(scene["module_layout"]["configured"], scene)
                # Pure re-read #2: the produced batch summary (finite energy/PR). Same two pure readers
                # as the frozen flow; the variant read simply moved after the numeric run.
                summary_name = Path(run["batch_summary"]).name
                data = await call("pvsyst_read_batch_results", {"summary_name": summary_name}, "readbatch")
                self.assertTrue(data["scenarios"], data)
                for row in data["scenarios"]:
                    for column in ("E_Grid", "PR"):
                        value = row["values"][column]
                        self.assertIsInstance(value, (int, float))
                        self.assertTrue(math.isfinite(value), "%s not finite" % column)


if __name__ == "__main__":
    unittest.main()

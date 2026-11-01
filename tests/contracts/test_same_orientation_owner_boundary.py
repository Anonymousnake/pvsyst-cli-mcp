"""Public contract for two source subarrays that share ONE declared NoOrientation.

This is the new same-orientation multi-owner boundary. Two source ``pvSubArray`` records
(SubArrayId 1 and 2) both reference ``NoOrientation=1``; identity is the subarray/owner id,
never the orientation id, angle, or name. The authoring path must bind each named PV table to
its real owner through the optional ``source_subarray_id``; a table that names only the shared
orientation is refused as ambiguous, never resolved by first/numeric/name guess.

External, persisted behaviour only: a real MCP server session over a synthetic self-contained
workspace (no vendor files, no private paths) and ZERO PVsyst CLI/Native/GUI. ``PVsystCLI.exe``
is an inert placeholder; native import is requested only with ``dry_run=True`` (in-memory
precheck). Pinned boundary:

- ``pvsyst_create_house_pv_scene`` authors a version-3 recipe (two owners on one orientation,
  each owning one 18x2 landscape table) and returns two orientation profiles ``[1, 1]`` with
  per-owner module totals ``[36, 36]``.
- ``pvsyst_read_house_pv_scene`` round-trips both owner tables losslessly (``source_subarray_id``
  ``[1, 2]`` and ``orientation_id`` ``[1, 1]`` preserved) and keeps the shared orientation once.
- ``pvsyst_import_house_pv_scene(method='native', shading_mode='module_layout', dry_run=True)``
  builds and verifies the native records IN MEMORY: ``ready_to_import`` with the full 72-module
  layout stored_consistent, one distinct orientation id ``[1]`` and two source owners. Nothing
  is written; native consumption flags stay false.
- Refusals before/at write: a table without ``source_subarray_id`` under a shared orientation is
  ``ambiguous_source_owner``; a string binding that names the wrong owner for a table, and a
  table whose source owner disagrees with its orientation, are both refused.

No permanent helper/class/order/Mock/snapshot framework: only this one public boundary.
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
    GInverter={ond}
    NModSerie=18
    NStringCh=2
    NStrOrient1=2
    NInverter=1
    NInvMPPT=1
  End of PVObject pvSubArray
"""
# Two owners, ONE shared orientation (NoOrientation=1 for both), each its own inverter branch.
SAMEGROUPS = [
    {"id": 1, "orient": 1, "name": "Array A", "tilt": 28.0, "azimuth": 0.0, "ond": "inverterA.OND"},
    {"id": 2, "orient": 1, "name": "Array B", "tilt": 28.0, "azimuth": 0.0, "ond": "inverterB.OND"},
]
MODULE = {"model_name": "JKM600 600 Wp", "nom_power_wp": 600.0,
          "width_m": 1.134, "length_m": 2.278, "geometry_depth_m": 0.035}


def _source_text(groups, cold=False):
    """One pvOrient per DISTINCT orientation id, one pvSubArray per owner.

    ``cold`` models a valid, never-simulated source: root Flags=$00100000 and no VarList cache
    block. The default is the warm, unsimulated-flag-but-cache-bearing template the other tests
    use. Both are legal original inputs; the cold one must import identically.
    """
    order = []
    for g in groups:
        if g["orient"] not in order:
            order.append(g["orient"])
    text = "PVObject_=pvVCalcul\n  Version=8.1.6\n  Flags=%s\n" % (
        "$00100000" if cold else "$00100001")
    text += "  PVObject_SiteSimul=pvSite\n    Latitude=45.0\n    Longitude=8.0\n    Altitude=250\n"
    text += "  End of PVObject pvSite\n"
    text += "  Orientations, list of=%d TOrientGroup\n" % len(order)
    for oid in order:
        spec = next(g for g in groups if g["orient"] == oid)
        text += ORIENT.format(id=oid, tilt=spec["tilt"], azimuth=spec["azimuth"])
    text += "  End of Orientations\n"
    text += ("  PVObject_PVMainArray=pvMainArray\n  PVObject_SystemCircuit=pvCircuit\n"
             "  Version=8.0.0\n  BaseNode=Start\nInjectionPointNode Start;\nChildren=Start\n")
    text += "".join(BRANCH.format(**g) for g in groups)
    text += "Children=End\nInjectionPointNode End;\n  BaseNode=End\n  End of PVObject pvCircuit\n"
    for g in groups:
        text += ARRAY.format(id=g["id"], name=g["name"], orient=g["orient"],
                             pan="module.PAN", ond=g["ond"])
    text += "  End of PVObject pvMainArray\n"
    text += "  PVObject_System=pvSystem\n    SystemType=Grid\n  End of PVObject pvSystem\n"
    text += "  PVObject_Ombrage=pvShading\n    Flags=$00\n  End of PVObject pvShading\n"
    if not cold:
        text += "  VarList, list of=0 TVarInit\n  End of the List VarList\n"
    text += "End of PVObject pvVCalcul\n"
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
        out.append({"name": "Table %s" % g["name"][-1], "orientation_id": g["orient"],
                    "source_subarray_id": g["id"], "module_rows": 18, "module_columns": 2,
                    "orientation": "landscape", "row_spacing_m": 0.0, "column_spacing_m": 0.0,
                    "origin_m": [idx * 40.0, 0.0, 10.0], "roof_mesh": "Roof %s" % g["name"][-1]})
    return out


def _binding(tables):
    entries = []
    for idx, table in enumerate(tables):
        first = idx * 2 + 1
        cells = [{"row": r, "col": c, "global_string": first + (c - 1)}
                 for r in range(1, 19) for c in (1, 2)]
        entries.append({"name": table["name"], "subarray_id": table["source_subarray_id"], "cells": cells})
    return {"tables": entries}


def _house(tables, groups):
    tilt = math.radians(groups[0]["tilt"])
    span, down, drop = 2 * MODULE["length_m"], 18 * MODULE["width_m"] * math.cos(tilt), \
        18 * MODULE["width_m"] * math.sin(tilt)
    local = [[0.0, 0.0, 0.0], [span, 0.0, 0.0], [span, down, drop], [0.0, down, drop]]
    az0 = groups[0]["azimuth"]
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
    def __init__(self, groups, cold=False):
        self.tmp = Path(tempfile.mkdtemp(prefix="pvsyst-same-orient-"))
        self.root = self.tmp / "PVsyst8.1_Data"
        for folder in ("Projects", "UserData", "Results", "Sites", "Meteo", "ComposPV"):
            (self.root / folder).mkdir(parents=True)
        (self.root / "Projects" / "Demo.PRJ").write_text(
            "PVObject_=pvProject\nEnd of PVObject pvProject\n", encoding="utf-8")
        (self.root / "Projects" / "Demo.VC1").write_text(_source_text(groups, cold=cold), encoding="utf-8")
        self.cli = self.root / "PVsystCLI.exe"
        self.cli.write_bytes(FAKE_CLI)

    def project_bytes(self):
        return (self.root / "Projects" / "Demo.VC1").read_bytes()

    def corrupt_source(self, old, new):
        """Byte-level substitution on the source variant (never text-mode, keeps CRLF intact)."""
        path = self.root / "Projects" / "Demo.VC1"
        data = path.read_bytes()
        assert old in data, "corruption anchor not present"
        path.write_bytes(data.replace(old, new, 1))

    def params(self):
        return StdioServerParameters(command=sys.executable,
                                     args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO),
                                     env=dict(os.environ, PVSYST_WORKSPACE=str(self.root),
                                              PVSYST_CLI=str(self.cli)))

    def cleanup(self, ok):
        if ok:
            shutil.rmtree(self.tmp)
        else:
            print("Retained same-orientation failure fixture: " + str(self.tmp), file=sys.stderr)


class SameOrientationOwnerBoundary(unittest.TestCase):
    def test_same_orientation_two_owner_create_read_import_preview(self):
        asyncio.run(self._positive())

    async def _positive(self):
        ws = _Workspace(SAMEGROUPS)
        before = ws.project_bytes()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    tables = _tables(SAMEGROUPS)
                    args = {"project": "Demo.PRJ", "variant": "VC1", "output_name": "SameOri.pvc2",
                            "house": _house(tables, SAMEGROUPS), "layout": {"tables": tables},
                            "module_geometry": MODULE, "string_layout": _binding(tables)}
                    created = await call(CREATE_TOOL, dict(args, dry_run=False))
                    self.assertEqual(created["status"], "created", created)
                    self.assertEqual([o["id"] for o in created["source"]["orientations"]], [1, 1])
                    self.assertEqual([o["module_total"] for o in created["source"]["orientations"]],
                                     [36, 36])
                    self.assertEqual(created["geometry"]["pv_table_count"], 2)
                    self.assertEqual(ws.project_bytes(), before, "source variant must be unchanged")

                    loaded = await call(READ_SCENE_TOOL, {"scene_name": "SameOri.pvc2"})
                    self.assertEqual(loaded["status"], "ready", loaded)
                    self.assertEqual([o["id"] for o in loaded["source"]["orientation_geometry"]], [1, 1])
                    self.assertEqual([t["source_subarray_id"]
                                      for t in loaded["create_arguments"]["layout"]["tables"]], [1, 2])
                    self.assertEqual([t["orientation_id"]
                                      for t in loaded["create_arguments"]["layout"]["tables"]], [1, 1])

                    preview = await call(IMPORT_TOOL, {"scene_name": "SameOri.pvc2",
                                                       "variant_name": "SameOri", "method": "native",
                                                       "shading_mode": "module_layout", "dry_run": True})
                    self.assertEqual(preview["status"], "ready_to_import", preview)
                    self.assertTrue(preview["precheck_passed"], preview)
                    self.assertFalse(preview["native_variant_created"], preview)
                    ver = preview["verification"]
                    self.assertEqual(ver["pv_bindings"], "stored_consistent", ver)
                    self.assertEqual(ver["module_layout"]["status"], "stored_consistent", ver)
                    self.assertEqual(ver["module_layout"]["module_count"], 72, ver)
                    self.assertEqual(ver["hardware"]["source_orientation_ids"], [1], ver)
                    self.assertEqual([row["subarray_id"] for row in ver["hardware"]["hardware_mapping"]],
                                     [1, 2], ver)
                    for key in NATIVE_NOT_LOADED:
                        self.assertFalse(preview[key], key)
                    self.assertEqual(list((ws.root / "Results").iterdir()), [])
            ok = True
        finally:
            ws.cleanup(ok)

    def test_valid_cold_source_create_import_read(self):
        asyncio.run(self._cold_positive())

    async def _cold_positive(self):
        """A valid never-simulated source (root Flags=$00100000, no VarList) must import identically."""
        ws = _Workspace(SAMEGROUPS, cold=True)
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    tables = _tables(SAMEGROUPS)
                    created = await call(CREATE_TOOL, {
                        "project": "Demo.PRJ", "variant": "VC1", "output_name": "Cold.pvc2",
                        "house": _house(tables, SAMEGROUPS), "layout": {"tables": tables},
                        "module_geometry": MODULE, "string_layout": _binding(tables), "dry_run": False})
                    self.assertEqual(created["status"], "created", created)
                    self.assertEqual([o["module_total"] for o in created["source"]["orientations"]],
                                     [36, 36])
                    loaded = await call(READ_SCENE_TOOL, {"scene_name": "Cold.pvc2"})
                    self.assertEqual(loaded["status"], "ready", loaded)
                    self.assertEqual([t["source_subarray_id"]
                                      for t in loaded["create_arguments"]["layout"]["tables"]], [1, 2])
                    preview = await call(IMPORT_TOOL, {
                        "scene_name": "Cold.pvc2", "variant_name": "Cold", "method": "native",
                        "shading_mode": "module_layout", "dry_run": True})
                    self.assertEqual(preview["status"], "ready_to_import", preview)
                    self.assertTrue(preview["precheck_passed"], preview)
                    ver = preview["verification"]
                    self.assertEqual(ver["module_layout"]["status"], "stored_consistent", ver)
                    self.assertEqual(ver["module_layout"]["module_count"], 72, ver)
                    self.assertEqual(ver["hardware"]["source_orientation_ids"], [1], ver)
                    self.assertFalse(preview["native_variant_created"], preview)
                    self.assertEqual(list((ws.root / "Results").iterdir()), [])
            ok = True
        finally:
            ws.cleanup(ok)

    def test_invalid_root_and_bad_cache_are_refused(self):
        asyncio.run(self._cold_negatives())

    async def _cold_negatives(self):
        # (a) A source whose root Flags line is missing is refused before any import (invalid root).
        ws = _Workspace(SAMEGROUPS, cold=True)
        ok = False
        try:
            ws.corrupt_source(b"\r\n  Flags=$00100000\r\n", b"\r\n")
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    tables = _tables(SAMEGROUPS)
                    bad_root = await call(CREATE_TOOL, {
                        "project": "Demo.PRJ", "variant": "VC1", "output_name": "BadRoot.pvc2",
                        "house": _house(tables, SAMEGROUPS), "layout": {"tables": tables},
                        "module_geometry": MODULE, "string_layout": _binding(tables), "dry_run": False})
                    self.assertEqual(bad_root["status"], "blocked", bad_root)
                    self.assertIn("flags", " ".join(bad_root["issues"]).lower(), bad_root)
                    self.assertEqual(list((ws.root / "Results").iterdir()), [])
            ok = True
        finally:
            ws.cleanup(ok)

        # (b) An unclosed VarList cache is malformed: import-preview refuses it, writing nothing.
        ws2 = _Workspace(SAMEGROUPS, cold=True)
        ok2 = False
        try:
            ws2.corrupt_source(b"\r\nEnd of PVObject pvVCalcul",
                               b"\r\n  VarList, list of=2 TVarInit\r\nEnd of PVObject pvVCalcul")
            before = ws2.project_bytes()
            async with stdio_client(ws2.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    tables = _tables(SAMEGROUPS)
                    made = await call(CREATE_TOOL, {
                        "project": "Demo.PRJ", "variant": "VC1", "output_name": "BadCache.pvc2",
                        "house": _house(tables, SAMEGROUPS), "layout": {"tables": tables},
                        "module_geometry": MODULE, "string_layout": _binding(tables), "dry_run": False})
                    self.assertEqual(made["status"], "created", made)
                    preview = await call(IMPORT_TOOL, {
                        "scene_name": "BadCache.pvc2", "variant_name": "BadCache", "method": "native",
                        "shading_mode": "module_layout", "dry_run": True})
                    self.assertEqual(preview["status"], "blocked", preview)
                    self.assertIn("unclosed", " ".join(preview["issues"]).lower(), preview)
                    self.assertFalse(preview["native_variant_created"], preview)
                    self.assertEqual(ws2.project_bytes(), before, "a refused import must not touch the source")
            ok2 = True
        finally:
            ws2.cleanup(ok2)

    def test_ambiguous_and_wrong_owner_are_refused(self):
        asyncio.run(self._negatives())

    async def _negatives(self):
        ws = _Workspace(SAMEGROUPS)
        before = ws.project_bytes()
        ok = False
        try:
            async with stdio_client(ws.params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, args):
                        return _payload(await session.call_tool(tool, args))

                    tables = _tables(SAMEGROUPS)

                    # (a) A table that names only the shared orientation is ambiguous, not guessed.
                    ambiguous = {"project": "Demo.PRJ", "variant": "VC1", "output_name": "Ambiguous.pvc2",
                                 "house": _house(tables, SAMEGROUPS),
                                 "layout": {"tables": [{k: v for k, v in t.items() if k != "source_subarray_id"}
                                                       for t in tables]},
                                 "module_geometry": MODULE, "string_layout": _binding(tables)}
                    stopped = await call(CREATE_TOOL, dict(ambiguous, dry_run=False))
                    self.assertEqual(stopped["status"], "blocked", stopped)
                    self.assertIn("ambiguous", " ".join(stopped["issues"]).lower(), stopped)
                    self.assertEqual(ws.project_bytes(), before, "a refused create must not touch the source")

                    # (b) A table whose source owner disagrees with its declared orientation is refused.
                    wrong_orient = copy.deepcopy(tables)
                    wrong_orient[1]["orientation_id"] = 2
                    bad_orient = {"project": "Demo.PRJ", "variant": "VC1", "output_name": "WrongOri.pvc2",
                                  "house": _house(tables, SAMEGROUPS), "layout": {"tables": wrong_orient},
                                  "module_geometry": MODULE, "string_layout": _binding(tables)}
                    stopped = await call(CREATE_TOOL, dict(bad_orient, dry_run=False))
                    self.assertEqual(stopped["status"], "blocked", stopped)
                    self.assertIn("orientation", " ".join(stopped["issues"]).lower(), stopped)

                    # (c) A string binding that names the wrong owner for a table is refused by the
                    #     native precheck before any variant is written. Table A belongs to owner 1
                    #     but is bound to owner 2; the cells use owner 2's global-string range so the
                    #     refusal is the owner mismatch, not an out-of-range cell.
                    good = {"project": "Demo.PRJ", "variant": "VC1", "output_name": "Good.pvc2",
                            "house": _house(tables, SAMEGROUPS), "layout": {"tables": tables},
                            "module_geometry": MODULE, "string_layout": _binding(tables)}
                    made = await call(CREATE_TOOL, dict(good, dry_run=False))
                    self.assertEqual(made["status"], "created", made)
                    swap = _binding(tables)
                    # Table A -> owner 2 with globals 3..4; Table B -> owner 1 with globals 1..2.
                    swap["tables"][0]["subarray_id"] = 2
                    swap["tables"][0]["cells"] = [{"row": r, "col": c, "global_string": 2 + c}
                                                  for r in range(1, 19) for c in (1, 2)]
                    swap["tables"][1]["subarray_id"] = 1
                    swap["tables"][1]["cells"] = [{"row": r, "col": c, "global_string": 0 + c}
                                                  for r in range(1, 19) for c in (1, 2)]
                    misbound = dict(good, output_name="Misbound.pvc2", string_layout=swap)
                    mis = await call(CREATE_TOOL, dict(misbound, dry_run=False))
                    self.assertEqual(mis["status"], "created", mis)
                    over = await call(IMPORT_TOOL, {"scene_name": "Misbound.pvc2", "variant_name": "Misbound",
                                                    "method": "native", "shading_mode": "module_layout",
                                                    "dry_run": True})
                    self.assertEqual(over["status"], "blocked", over)
                    self.assertIn("owner", " ".join(over["issues"]).lower(), over)
                    self.assertFalse(over["native_variant_created"], over)
                    self.assertEqual(list((ws.root / "Results").iterdir()), [])
            ok = True
        finally:
            ws.cleanup(ok)


if __name__ == "__main__":
    unittest.main()

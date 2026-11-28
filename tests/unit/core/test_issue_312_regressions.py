"""Regression tests for GitHub Issue #312."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pvsyst_auto.api import context as api_context
from pvsyst_auto.api.projects import IamUpdates, OrientationConvertParams, SpectralUpdates, pvsyst_convert_orientation
from pvsyst_auto.api.schemas import SingleDesignRequest
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene import cad_import
from pvsyst_auto.scene.dxf_import import parse_ascii_dxf
from pvsyst_auto.workflows.design import prepare_design_project
from tests._fixtures import init_full_workspace
from tests.unit.core.test_bifacial_utility import UTILITY_MONO_PAN, UTILITY_OND, _FakeUtilityCLI, _capacity_spec
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT


def _nested_pvc_xml(depth: int) -> str:
    opens = "".join(f"<node id=\"n{i}\" name=\"n{i}\">" for i in range(depth))
    closes = "</node>" * depth
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">'
        '<asset><unit name="meter" meter="1.0"/></asset>'
        '<library_geometries/>'
        f'<library_visual_scenes><visual_scene id="Scene">{opens}{closes}</visual_scene></library_visual_scenes>'
        '<scene><instance_visual_scene url="#Scene"/></scene>'
        '</COLLADA>'
    )


def _terrain_pvc_xml(pt_count: int, inst_count: int) -> str:
    pts = " ".join(f"{i % 50} {i // 50} 0" for i in range(pt_count))
    insts = "".join('<instance_geometry url="#TerrainMesh" />' for _ in range(inst_count))
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">'
        '<asset><unit name="meter" meter="1.0"/></asset>'
        '<library_geometries>'
        '<geometry id="TerrainMesh" name="terrain_surface"><mesh>'
        f'<source id="pos"><float_array id="pos-arr" count="{pt_count * 3}">{pts}</float_array>'
        f'<technique_common><accessor source="#pos-arr" count="{pt_count}" stride="3">'
        '<param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>'
        '</accessor></technique_common></source>'
        '<vertices id="v"><input semantic="POSITION" source="#pos"/></vertices>'
        '<triangles count="1"><input semantic="VERTEX" source="#v" offset="0"/><p>0 1 2</p></triangles>'
        '</mesh></geometry>'
        '</library_geometries>'
        f'<library_visual_scenes><visual_scene id="Scene"><node id="Terrain" name="terrain">{insts}</node></visual_scene></library_visual_scenes>'
        '<scene><instance_visual_scene url="#Scene"/></scene>'
        '</COLLADA>'
    )


def _minsert_dxf(vertices_per_poly: int, rows: int, cols: int) -> str:
    poly_verts = "".join(f"10\n{float(i)}\n20\n{float(i % 10)}\n" for i in range(vertices_per_poly))
    return (
        "0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n6\n0\nENDSEC\n"
        "0\nSECTION\n2\nBLOCKS\n"
        "0\nBLOCK\n8\nterrain\n2\nBIGPOLY\n10\n0.0\n20\n0.0\n30\n0.0\n"
        f"0\nLWPOLYLINE\n8\nterrain\n90\n{vertices_per_poly}\n70\n0\n{poly_verts}"
        "0\nENDBLK\n0\nENDSEC\n"
        "0\nSECTION\n2\nENTITIES\n"
        f"0\nINSERT\n8\nterrain\n2\nBIGPOLY\n10\n0.0\n20\n0.0\n30\n0.0\n70\n{cols}\n71\n{rows}\n44\n1.0\n45\n1.0\n"
        "0\nENDSEC\n0\nEOF\n"
    )


class Issue312RegressionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = init_full_workspace(Path(self._tmp.name))
        (self.ws / "Sites" / "Geneva_MN90.SIT").write_text(SITE_TEXT, encoding="utf-8")
        (self.ws / "Meteo" / "Geneva_MN90_SYN.MET").write_text(MET_TEXT, encoding="utf-8")
        (self.ws / "ComposPV" / "PVmodules" / "Mono440.PAN").write_text(UTILITY_MONO_PAN, encoding="utf-8")
        (self.ws / "ComposPV" / "Inverters" / "Inv50_2MPPT.OND").write_text(UTILITY_OND, encoding="utf-8")
        self.comp = ComponentStore(self.ws)
        self.store = VariantStore(self.ws, self.comp)
        self.cli = _FakeUtilityCLI(self.ws, year=2024, nominal_dc_kw=52.8, use_sfi_writer=False)

    def test_readonly_component_listing_and_dry_run_do_not_write_index_file(self) -> None:
        index_path = self.ws / "ComposPV" / ".mcp-component-index.json"
        self.assertFalse(index_path.exists())

        res = self.comp.list("PAN")
        self.assertEqual(res["total"], 1)
        self.assertFalse(index_path.exists())

        with patch.dict("os.environ", {"PVSYST_WORKSPACE": str(self.ws)}):
            async def _read_comp():
                return await api_context.mcp.read_resource("pvsyst://workspace/components")
            raw = asyncio.run(_read_comp())
        self.assertIn("Mono440.PAN", raw[0].content)
        self.assertFalse(index_path.exists())

        dry = prepare_design_project(
            self.cli, self.store, dry_run=True,
            spec={
                "scope": "ground_sheds",
                "new_project": "NoIdxDry.PRJ",
                "year": 2024,
                "target_dc_kw": 52.8,
                "tilt": 20.0,
                "azimuth": 0.0,
                "module": {"file_name": "Mono440.PAN"},
                "inverter": {"file_name": "Inv50_2MPPT.OND"},
                "capacity": _capacity_spec(target_dc_kw=52.8),
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                "ground_layout": {"model_mode": "unlimited", "pitch_m": 6.0, "shed_count": 4, "module_rows": 2, "module_columns": 15},
            },
        )
        self.assertEqual(dry["status"], "ready")
        self.assertFalse(index_path.exists())

        self.comp.list("PAN", persist_index=True)
        self.assertTrue(index_path.is_file())
        self.assertIn("PAN", json.loads(index_path.read_text(encoding="utf-8")))

    def test_pvc_depth_instance_terrain_and_dxf_minsert_vertex_bounds(self) -> None:
        # 1. Deep COLLADA node nesting raises ValueError instead of RecursionError
        (self.ws / "UserData" / "deep.pvc").write_text(_nested_pvc_xml(cad_import.MAX_PVC_NODE_DEPTH + 5), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "nesting depth"):
            cad_import.parse_pvc_141(self.ws, "deep.pvc")

        # 2. Non-PV terrain amplification exceeds MAX_PVC_TERRAIN_POINTS before allocating all points
        (self.ws / "UserData" / "terrain_bomb.pvc").write_text(_terrain_pvc_xml(2000, 260), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "terrain point count"):
            cad_import.parse_pvc_141(self.ws, "terrain_bomb.pvc")

        # 3. Non-PV instance count exceeds MAX_PVC_INSTANCES
        (self.ws / "UserData" / "inst_bomb.pvc").write_text(_terrain_pvc_xml(10, 6), encoding="utf-8")
        with patch.object(cad_import, "MAX_PVC_INSTANCES", 5):
            with self.assertRaisesRegex(ValueError, "instance count"):
                cad_import.parse_pvc_141(self.ws, "inst_bomb.pvc")

        # 4. DXF MINSERT vertex bomb exceeds MAX_DXF_VERTICES cleanly (within entity step budget)
        (self.ws / "UserData" / "minsert_bomb.dxf").write_text(_minsert_dxf(600, 100, 10), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "expanded vertex count"):
            parse_ascii_dxf(self.ws, "minsert_bomb.dxf")

    def test_docs_and_context_guidance_match_schemas(self) -> None:
        root = Path(__file__).resolve().parents[3]
        entry_map = (root / "docs" / "native-entry-map.md").read_text(encoding="utf-8")
        tool_ref = (root / "docs" / "tool-reference.md").read_text(encoding="utf-8")
        instructions = api_context.SERVER_INSTRUCTIONS
        guide = api_context.WORKFLOW_GUIDE_MD

        # IamUpdates actual field
        self.assertIn("ashrae_b0", IamUpdates.model_fields)
        self.assertIn("ashrae_b0", entry_map)

        # SpectralUpdates actual fields vs removed legacy wording
        for field_name in ("enabled", "coeff_set", "use_default_coeffs", "use_rel_hum", "coefficients"):
            self.assertIn(field_name, SpectralUpdates.model_fields)
            self.assertIn(field_name, entry_map)
        self.assertNotIn('mode="first_solar"', entry_map)
        self.assertNotIn('mode="user_monthly"', entry_map)

        # OrientationConvertParams + dome + ground_dome synced in docs & context guidance
        self.assertIn("dome", entry_map)
        self.assertNotIn("拒绝 `dome`", entry_map)
        self.assertIn("dome", tool_ref)
        self.assertNotIn("dome conversions are rejected", tool_ref)
        self.assertNotIn("dome conversions are not supported", tool_ref)
        self.assertIn("ground_dome", repr(SingleDesignRequest.model_fields["scope"].annotation))
        self.assertIn("ground_dome", instructions)
        self.assertIn("dome", instructions)
        self.assertIn("dome", guide)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for PR 1: 3D ground partition-based electrical shading."""
from __future__ import annotations

import asyncio
import math
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from pvsyst_auto.api.context import TOOL_GROUPS, create_mcp_server
from pvsyst_auto.api.schemas import (
    ElectricalShadingSpec,
    GroundLayoutRequest,
    SingleDesignRequest,
)
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.template import (
    _resolve_pan_module_dimensions,
    synthesize_utility_project,
)
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.cad_import import (
    build_pvc_3d_shading,
    parse_pvc_141,
    resolve_cad_synthesis_inputs,
)
from pvsyst_auto.scene.dome import build_dome_3d_shading, synthesize_ground_dome_shading
from pvsyst_auto.scene.partition import (
    ELECTRICAL_SHADING_KEYS,
    SHADING_FLAGS_LINEAR,
    SHADING_FLAGS_PARTITION,
    compute_table_partition_dims,
    is_partition_shading_requested,
    normalize_electrical_shading,
    shading_header_kwargs,
)
from pvsyst_auto.scene.reader import (
    build_ground_3d_shading,
    plan_ground_polygon_layout,
    pv_shading_header,
)
from pvsyst_auto.scene.terrain import build_terrain_mesh, build_terrain_tracker_shading
from pvsyst_auto.scene.tracker_layout import build_tracker_3d_shading
from pvsyst_auto.workflows.design import prepare_design_project, simulate_design
from pvsyst_auto.workflows.utility import (
    _extract_electrical_shading_metrics,
    resolve_utility_field_type,
    validate_utility_spec,
)
from tests._fixtures import FakeCliBase, init_full_workspace, write_sfi_csv, write_utf8_crlf
from tests.unit.core.test_bifacial_utility import (
    UTILITY_OND,
    UTILITY_PAN,
    _capacity_spec,
)
from tests.unit.core.test_cad_pvc_dxf_import import (
    _dxf_text,
    _make_pvc_fixed,
    _make_pvc_tracker,
)
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT
from tests.unit.core.test_tracker_3d_polygon import _tracker_dem_csv

HALF_CUT_PAN = UTILITY_PAN.replace(
    "  Technol=mtSiMono\n",
    "  Technol=mtSiMono\n  SubModuleLayout=slTwinHalfCells\n",
    1,
)
FULL_CELL_PAN = UTILITY_PAN.replace(
    "  Technol=mtSiMono\n",
    "  Technol=mtSiMono\n  SubModuleLayout=slInLength\n",
    1,
)


def _write_partition_annual_csv(
    csv_path: Path,
    *,
    project: str,
    variant: str,
    site: str,
    met: str,
    year: int,
    nominal_dc_kw: float,
    bifacial: bool = False,
    shd_elec: bool = False,
    shd_elec_kw: float = 2.5,
) -> None:
    headers = ["date", "GlobInc", "GlobEff", "EArray", "E_Grid", "PR", "IL_Pmax", "GlobShd"]
    units = ["", "W/m\u00b2", "W/m\u00b2", "kW", "kW", "ratio", "kW", "W/m\u00b2"]
    if bifacial:
        headers.extend(["GlobGnd", "DiffGnd", "GlobBak", "BeamBak", "DifSBak", "BmSFBak", "MismBak", "PRBifi"])
        units.extend(["W/m\u00b2", "W/m\u00b2", "W/m\u00b2", "W/m\u00b2", "W/m\u00b2", "ratio", "kW", "ratio"])
    if shd_elec:
        headers.append("ShdElec")
        units.append("kW")
    lines = [
        f"Project;{project}",
        f"Simulation variant;{Path(project).stem}.{variant}",
        f"Geographical Site;{site}",
        f"Weather data;{met}",
        "",
        ";".join(headers),
        ";".join(units),
    ]
    cur = datetime(year, 1, 1)
    end = datetime(year + 1, 1, 1)
    pr_val = 0.88 if bifacial else 0.84
    while cur < end:
        stamp = cur.strftime("%d/%m/%y %H:%M")
        if 8 <= cur.hour <= 16:
            gi, ge = 500.0, 480.0
            egrid = (gi * 0.001) * nominal_dc_kw * pr_val
            earr = egrid * 1.02
            row = [stamp, f"{gi:.2f}", f"{ge:.2f}", f"{earr:.3f}", f"{egrid:.3f}", f"{pr_val:.4f}", "0.000", "490.00"]
            if bifacial:
                row.extend(["400.00", "150.00", "50.00", "20.00", "30.00", "0.800", "0.100", "0.8200"])
            if shd_elec:
                row.append(f"{shd_elec_kw:.3f}")
        else:
            row = [stamp, "0.00", "0.00", "0.000", "0.000", "", "0.000", "0.00"]
            if bifacial:
                row.extend(["0.00", "0.00", "0.00", "0.00", "0.00", "0.000", "0.000", ""])
            if shd_elec:
                row.append("0.000")
        lines.append(";".join(row))
        cur += timedelta(hours=1)
    csv_path.write_bytes(("\ufeff" + "\r\n".join(lines) + "\r\n").encode("utf-8"))


class _PartitionFakeCLI(FakeCliBase):
    """Fake CLI that emits ShdElec only when viShdElec is in the .SFI (unless disabled)."""

    def __init__(self, workspace: Path, *, year: int = 2024, emit_shd_elec: bool = True, shd_elec_kw: float = 2.5):
        super().__init__(workspace, version="8.1.7", license_status="TRIAL")
        self.year = year
        self.emit_shd_elec = emit_shd_elec
        self.shd_elec_kw = shd_elec_kw
        self.site = "Geneva_MN90.SIT"
        self.met = "Geneva_MN90_SYN.MET"
        self.runs: list[dict] = []

    def command_help(self, *args, **kwargs) -> str:
        return "Available language: [en, zh]"

    def run_simulation(self, project, variant, *, sfi=None, out_csv=None, report_pdf=None, **kwargs):
        sfi_text = Path(sfi).read_text(encoding="utf-8") if sfi else ""
        self.runs.append({"project": project, "variant": variant, "sfi": sfi, "sfi_text": sfi_text})
        vc_text = (self.workspace / "Projects" / f"{Path(project).stem}.{variant}").read_text(encoding="utf-8-sig")
        n_ser = int(re.search(r"NModSerie=(\d+)", vc_text).group(1))
        n_str = int(re.search(r"NStringCh=(\d+)", vc_text).group(1))
        nom_kw = n_ser * n_str * 440.0 / 1000.0
        _write_partition_annual_csv(
            Path(out_csv),
            project=project,
            variant=variant,
            site=self.site,
            met=self.met,
            year=self.year,
            nominal_dc_kw=nom_kw,
            bifacial="viGlobBak" in sfi_text,
            shd_elec=("viShdElec" in sfi_text) and self.emit_shd_elec,
            shd_elec_kw=self.shd_elec_kw,
        )
        if report_pdf is not None:
            Path(report_pdf).write_bytes(b"%PDF-1.4\n%%EOF\n")
        return {"exit_code": 0, "warnings": [], "sfi_variables": sfi_text}


class _BadModelDump:
    def model_dump(self) -> dict:
        raise RuntimeError("boom")


def _extract_object_block(vc_text: str, prefix: str) -> str:
    start = vc_text.index(prefix)
    end = vc_text.index("End of PVObject pvShd", start)
    return vc_text[start:end]


class GroundPartitionShadingTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = init_full_workspace(Path(tmp.name).resolve())
        write_utf8_crlf(self.ws / "Sites" / "Geneva_MN90.SIT", SITE_TEXT)
        write_utf8_crlf(self.ws / "Meteo" / "Geneva_MN90_SYN.MET", MET_TEXT)
        write_utf8_crlf(self.ws / "ComposPV" / "PVmodules" / "FullCell440.PAN", UTILITY_PAN)
        write_utf8_crlf(self.ws / "ComposPV" / "PVmodules" / "HalfCut440.PAN", HALF_CUT_PAN)
        write_utf8_crlf(self.ws / "ComposPV" / "Inverters" / "Inv50.OND", UTILITY_OND)
        self.components = ComponentStore(self.ws)
        self.store = VariantStore(self.ws, self.components)

    def test_partition_helpers_constants_normalization_and_header_kwargs(self) -> None:
        self.assertEqual(SHADING_FLAGS_LINEAR, "$000A0100")
        self.assertEqual(SHADING_FLAGS_PARTITION, "$000A0300")
        self.assertEqual(
            ELECTRICAL_SHADING_KEYS,
            frozenset(("mode", "partitions_x", "partitions_y", "electrical_effect_fraction", "auto_half_cut")),
        )

        # is_partition_shading_requested: valid & malformed never raise
        self.assertFalse(is_partition_shading_requested(None))
        self.assertFalse(is_partition_shading_requested("linear"))
        self.assertFalse(is_partition_shading_requested(ElectricalShadingSpec(mode="linear")))
        self.assertFalse(is_partition_shading_requested({"mode": "linear"}))
        self.assertFalse(is_partition_shading_requested(123))
        self.assertFalse(is_partition_shading_requested(["partition"]))
        self.assertFalse(is_partition_shading_requested({"mode": "invalid"}))
        self.assertFalse(is_partition_shading_requested(_BadModelDump()))
        self.assertTrue(is_partition_shading_requested("partition"))
        self.assertTrue(is_partition_shading_requested(ElectricalShadingSpec()))
        self.assertTrue(is_partition_shading_requested({"mode": "partition"}))
        self.assertTrue(is_partition_shading_requested({"partitions_y": 4}))

        # normalize_electrical_shading + idempotency
        for raw, exp_mode in ((None, "linear"), ("linear", "linear"), ({"mode": "linear"}, "linear")):
            norm1 = normalize_electrical_shading(raw)
            norm2 = normalize_electrical_shading(norm1)
            self.assertEqual(norm1["mode"], exp_mode)
            self.assertEqual(norm1, norm2)
            self.assertEqual(shading_header_kwargs(norm1), {"flags": "$000A0100", "frac_ombre_mod": 1.0})

        norm_p1 = normalize_electrical_shading("partition")
        self.assertEqual(norm_p1, normalize_electrical_shading(norm_p1))
        self.assertEqual(shading_header_kwargs(norm_p1), {"flags": "$000A0300", "frac_ombre_mod": 1.0})

        spec = ElectricalShadingSpec(partitions_x=3, partitions_y=4, electrical_effect_fraction=0.75, auto_half_cut=False)
        norm_p2 = normalize_electrical_shading(spec)
        self.assertEqual(norm_p2, normalize_electrical_shading(norm_p2))
        self.assertEqual(shading_header_kwargs(norm_p2), {"flags": "$000A0300", "frac_ombre_mod": 0.75})
        self.assertEqual(shading_header_kwargs({"electrical_effect_fraction": 0.0}), {"flags": "$000A0300", "frac_ombre_mod": 0.0})

        # pv_shading_header defaults to linear $000A0100
        default_hdr = "\n".join(pv_shading_header("Linear test", 1))
        self.assertIn("Flags=$000A0100", default_hdr)
        self.assertIn("FracOmbreMod=1.000", default_hdr)
        part_hdr = "\n".join(pv_shading_header("Partition test", 1, **shading_header_kwargs({"electrical_effect_fraction": 0.65})))
        self.assertIn("Flags=$000A0300", part_hdr)
        self.assertIn("FracOmbreMod=0.650", part_hdr)

        # Rejections
        for bad in (
            "unlimited",
            123,
            {"mode": "bogus"},
            {"unknown_key": 1},
            {"partitions_x": True},
            {"partitions_x": 1.5},
            {"partitions_x": 0},
            {"partitions_x": 1001},
            {"partitions_y": 0},
            {"partitions_y": 1001},
            {"electrical_effect_fraction": -0.1},
            {"electrical_effect_fraction": 1.1},
            {"electrical_effect_fraction": float("nan")},
            {"auto_half_cut": "yes"},
            {"mode": "linear", "partitions_x": 1},
            {"mode": "linear", "partitions_y": 2},
            {"mode": "linear", "electrical_effect_fraction": 0.5},
            {"mode": "linear", "auto_half_cut": False},
        ):
            with self.assertRaises(ValueError, msg=repr(bad)):
                normalize_electrical_shading(bad)

    def test_compute_table_partition_dims_rules_and_span_guard(self) -> None:
        # Fixed shed: DX = long_shed / px, DY = larg_shed / py; portrait half-cut auto-doubles py
        dx, dy, px, py = compute_table_partition_dims(
            long_shed_m=28.0,
            larg_shed_m=4.0,
            module_rows=2,
            module_columns=28,
            module_orient="moPortrait",
            is_tracker=False,
            electrical_shading="partition",
            series_modules=14,
            is_half_cut=True,
        )
        self.assertEqual((px, py), (2, 4))
        self.assertAlmostEqual(dx, 14.0)
        self.assertAlmostEqual(dy, 1.0)

        # Portrait half-cut does NOT double when auto_half_cut=False or is_half_cut=False or landscape or explicit py
        _, _, px2, py2 = compute_table_partition_dims(
            long_shed_m=28.0, larg_shed_m=4.0, module_rows=2, module_columns=28,
            module_orient="moPortrait", electrical_shading={"auto_half_cut": False},
            series_modules=14, is_half_cut=True,
        )
        self.assertEqual((px2, py2), (2, 2))
        _, _, _, py_no_hc = compute_table_partition_dims(
            long_shed_m=28.0, larg_shed_m=4.0, module_rows=2, module_columns=28,
            module_orient="moPortrait", electrical_shading="partition", series_modules=14, is_half_cut=False,
        )
        self.assertEqual(py_no_hc, 2)
        _, _, _, py_land = compute_table_partition_dims(
            long_shed_m=28.0, larg_shed_m=4.0, module_rows=2, module_columns=28,
            module_orient="moLandscape", electrical_shading="partition", series_modules=14, is_half_cut=True,
        )
        self.assertEqual(py_land, 2)
        _, _, _, py_exp = compute_table_partition_dims(
            long_shed_m=28.0, larg_shed_m=4.0, module_rows=2, module_columns=28,
            module_orient="moPortrait", electrical_shading={"partitions_y": 3}, series_modules=14, is_half_cut=True,
        )
        self.assertEqual(py_exp, 3)

        # series_modules=None or columns < series_modules clamps auto px to 1
        _, _, px_none, _ = compute_table_partition_dims(
            long_shed_m=10.0, larg_shed_m=4.0, module_rows=2, module_columns=10,
            module_orient="portrait", electrical_shading="partition", series_modules=None,
        )
        self.assertEqual(px_none, 1)
        _, _, px_clamp, _ = compute_table_partition_dims(
            long_shed_m=10.0, larg_shed_m=4.0, module_rows=2, module_columns=10,
            module_orient="portrait", electrical_shading="partition", series_modules=14,
        )
        self.assertEqual(px_clamp, 1)

        # Explicit partitions_x overrides auto px
        dx_o, dy_o, px_o, py_o = compute_table_partition_dims(
            long_shed_m=12.0, larg_shed_m=4.0, module_rows=2, module_columns=28,
            module_orient="moPortrait", electrical_shading={"partitions_x": 3}, series_modules=14, is_half_cut=True,
        )
        self.assertEqual((px_o, py_o), (3, 4))
        self.assertAlmostEqual(dx_o, 4.0)
        self.assertAlmostEqual(dy_o, 1.0)

        # Tracker axis swap: DXModule = long_shed / py, DYModule = larg_shed / px
        dx_t, dy_t, px_t, py_t = compute_table_partition_dims(
            long_shed_m=4.0, larg_shed_m=28.0, module_rows=2, module_columns=28,
            module_orient="moPortrait", is_tracker=True, electrical_shading="partition",
            series_modules=14, is_half_cut=True,
        )
        self.assertEqual((px_t, py_t), (2, 4))
        self.assertAlmostEqual(dx_t, 1.0)
        self.assertAlmostEqual(dy_t, 14.0)

        # Reject partition span < 0.01 m, accept exact 0.01 m boundary
        dx_ok, dy_ok, _, _ = compute_table_partition_dims(
            long_shed_m=0.3, larg_shed_m=0.1, module_rows=1, module_columns=30,
            module_orient="moPortrait", electrical_shading={"partitions_x": 30, "partitions_y": 10},
        )
        self.assertAlmostEqual(dx_ok, 0.01)
        self.assertAlmostEqual(dy_ok, 0.01)
        with self.assertRaisesRegex(ValueError, "0.01m"):
            compute_table_partition_dims(
                long_shed_m=0.29, larg_shed_m=1.0, module_rows=1, module_columns=30,
                module_orient="moPortrait", electrical_shading={"partitions_x": 30, "partitions_y": 1},
            )

    def test_pydantic_schemas_and_utility_validation(self) -> None:
        # Roundtrip for string, linear ElectricalShadingSpec, and partition ElectricalShadingSpec
        spec_obj = ElectricalShadingSpec(partitions_x=2, partitions_y=4, electrical_effect_fraction=0.85, auto_half_cut=False)
        for es_val in ("linear", "partition", ElectricalShadingSpec(mode="linear"), spec_obj):
            req = SingleDesignRequest(
                scope="ground_sheds",
                new_project="SchemaTest.PRJ",
                year=2024,
                target_dc_kw=100.0,
                tilt=20.0,
                azimuth=0.0,
                module={"file_name": "HalfCut440.PAN"},
                inverter={"file_name": "Inv50.OND"},
                weather={"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                capacity=_capacity_spec(),
                ground_layout={"electrical_shading": es_val},
            )
            dumped = req.model_dump(exclude_none=True)
            roundtrip = SingleDesignRequest.model_validate(dumped)
            self.assertEqual(roundtrip.model_dump(exclude_none=True), dumped)

        # Schema rejections
        for bad_kw in (
            {"extra": 1},
            {"mode": "invalid"},
            {"partitions_x": 0},
            {"partitions_y": True},
            {"electrical_effect_fraction": 1.2},
            {"auto_half_cut": "true"},
            {"mode": "linear", "partitions_x": 2},
            {"mode": "linear", "partitions_y": 2},
            {"mode": "linear", "electrical_effect_fraction": 0.5},
            {"mode": "linear", "auto_half_cut": False},
        ):
            with self.assertRaises(ValidationError, msg=repr(bad_kw)):
                ElectricalShadingSpec.model_validate(bad_kw)

        with self.assertRaises(ValidationError):
            GroundLayoutRequest.model_validate({"electrical_shading": "invalid"})

        # resolve_utility_field_type upgrades only for partition, never for linear/None
        self.assertEqual(resolve_utility_field_type("ground_sheds", None), ("Sheds", False))
        self.assertEqual(resolve_utility_field_type("ground_sheds", {"electrical_shading": "linear"}), ("Sheds", False))
        self.assertEqual(resolve_utility_field_type("ground_sheds", {"electrical_shading": "partition"}), ("RegularSheds", True))
        self.assertEqual(resolve_utility_field_type("ground_sheds", {"electrical_shading": {"partitions_y": 4}}), ("RegularSheds", True))
        self.assertEqual(resolve_utility_field_type("ground_tracker", {"electrical_shading": "linear"}), ("UnlimTracking", False))
        self.assertEqual(resolve_utility_field_type("ground_tracker", {"electrical_shading": "partition"}), ("TrackHorizNSAxis", True))

        def _issues_for(scope: str, gl: dict | None, tilt: float | None = 20.0, azim: float | None = 0.0) -> list[dict]:
            issues: list[dict] = []
            spec: dict = {"scope": scope, "ground_layout": gl}
            if tilt is not None:
                spec["tilt"] = tilt
            if azim is not None:
                spec["azimuth"] = azim
            validate_utility_spec(spec, scope, issues)
            return issues

        # Rejected on explicit unlimited and implicit unlimited ("linear" without 3D)
        for gl in (
            {"model_mode": "unlimited", "electrical_shading": "partition"},
            {"model_mode": "unlimited", "electrical_shading": "linear"},
            {"electrical_shading": "linear"},
        ):
            iss = _issues_for("ground_sheds", gl)
            self.assertTrue(any(i["field"] == "ground_layout.electrical_shading" and "requires 3D layout" in i["message"] for i in iss), gl)

        # Invalid dict/value inside utility_spec
        iss_bad = _issues_for("ground_sheds", {"model_mode": "3d", "electrical_shading": {"mode": "linear", "partitions_y": 2}})
        self.assertTrue(any(i["field"] == "ground_layout.electrical_shading" for i in iss_bad), iss_bad)

        # Accepted cases
        self.assertEqual(_issues_for("ground_sheds", {"electrical_shading": "partition"}), [])
        self.assertEqual(_issues_for("ground_sheds", {"model_mode": "3d", "electrical_shading": "partition"}), [])
        self.assertEqual(_issues_for("ground_sheds", {"model_mode": "3d", "electrical_shading": "linear"}), [])
        self.assertEqual(_issues_for("ground_tracker", {"electrical_shading": "partition"}, tilt=None), [])
        self.assertEqual(_issues_for("ground_dome", {"electrical_shading": "linear"}, tilt=12.0), [])

    def test_pan_half_cut_detection(self) -> None:
        write_utf8_crlf(
            self.ws / "ComposPV" / "PVmodules" / "MixedCaseHC.PAN",
            HALF_CUT_PAN.replace("SubModuleLayout=slTwinHalfCells", "SubModuleLayout=SLTwinHalfCells"),
        )
        write_utf8_crlf(self.ws / "ComposPV" / "PVmodules" / "FullInLen.PAN", FULL_CELL_PAN)

        self.assertTrue(_resolve_pan_module_dimensions(self.store, "HalfCut440.PAN")["is_half_cut"])
        self.assertTrue(_resolve_pan_module_dimensions(self.store, "MixedCaseHC.PAN")["is_half_cut"])
        self.assertEqual(_resolve_pan_module_dimensions(self.store, "MixedCaseHC.PAN")["sub_module_layout"], "SLTwinHalfCells")
        self.assertFalse(_resolve_pan_module_dimensions(self.store, "FullInLen.PAN")["is_half_cut"])
        self.assertFalse(_resolve_pan_module_dimensions(self.store, "FullCell440.PAN")["is_half_cut"])

    def test_scene_builders_and_utility_synthesis_linear_vs_partition(self) -> None:
        _tracker_dem_csv(self.ws / "UserData" / "dem.csv")
        dem_mesh = build_terrain_mesh({"csv_name": "dem.csv"}, workspace=self.ws)
        poly = [[0.0, 0.0], [32.0, 0.0], [32.0, 26.0], [0.0, 26.0]]

        # Unlimited layout omits utility_summary["electrical_shading"]
        unlim = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="Sheds", pan_name="HalfCut440.PAN", ond_name="Inv50.OND", include_3d_scene=False,
        )
        self.assertNotIn("electrical_shading", unlim["utility_summary"])

        # Regular Shed: linear default ($000A0100) vs partition ($000A0300, half-cut py doubled 2 -> 4)
        shed_common = dict(
            site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="RegularSheds", pan_name="HalfCut440.PAN", ond_name="Inv50.OND",
            n_mod_serie=14, n_string_ch=8, n_inverter=1, n_inv_mppt=2, independent_mppt=True, pnom_ac_in_mppt=25.0,
            pitch_m=10.0, tilt_deg=20.0, shed_count=4, module_rows=2, module_columns=28,
            module_width_m=1.0, module_length_m=2.0, spacing_x_m=0.0, spacing_y_m=0.0,
        )
        shed_lin = synthesize_utility_project(self.store, **shed_common)
        self.assertIn("Flags=$000A0100", shed_lin["vc0_text"])
        self.assertIn("DXModule=1.000", shed_lin["vc0_text"])
        self.assertIn("DYModule=2.000", shed_lin["vc0_text"])
        self.assertEqual(shed_lin["utility_summary"]["electrical_shading"]["mode"], "linear")

        shed_part = synthesize_utility_project(
            self.store, **shed_common, electrical_shading={"electrical_effect_fraction": 0.8},
        )
        self.assertIn("Flags=$000A0300", shed_part["vc0_text"])
        self.assertIn("FracOmbreMod=0.800", shed_part["vc0_text"])
        self.assertIn("DXModule=14.000", shed_part["vc0_text"])
        self.assertIn("DYModule=1.000", shed_part["vc0_text"])
        es_s = shed_part["utility_summary"]["electrical_shading"]
        self.assertEqual((es_s["partitions_x"], es_s["partitions_y"], es_s["dx_module_m"], es_s["dy_module_m"]), (2, 4, 14.0, 1.0))
        self.store.create_project("PartShed.PRJ", shed_part["prj_text"], {"VC0": shed_part["vc0_text"]})
        self.assertTrue(self.store.validate_structure("PartShed.PRJ", "VC0")["valid"])

        # Shed auto_half_cut=False and explicit partitions_x/partitions_y
        shed_no_hc = synthesize_utility_project(
            self.store, **shed_common, electrical_shading={"auto_half_cut": False, "partitions_x": 4, "partitions_y": 2},
        )
        self.assertIn("DXModule=7.000", shed_no_hc["vc0_text"])
        self.assertIn("DYModule=2.000", shed_no_hc["vc0_text"])

        # Polygon + terrain shed via build_ground_3d_shading + synthesize_utility_project (DXModule/DYModule omitted in linear, placed after ModuleOrient in partition)
        shed_layout = plan_ground_polygon_layout(
            poly, tilt_deg=20.0, azimuth_deg=0.0, pitch_m=10.0, module_width_m=1.0, module_length_m=2.0,
            module_rows=2, module_orientation="portrait", spacing_x_m=0.0, spacing_y_m=0.0,
        )
        tbls = shed_layout["tables"]
        poly_lin = "\n".join(build_ground_3d_shading(
            tbls, module_name="440W", module_width_m=1.0, module_length_m=2.0, terrain_mesh=dem_mesh,
        ))
        self.assertIn("Flags=$000A0100", poly_lin)
        self.assertNotIn("DXModule=", poly_lin)
        poly_part = "\n".join(build_ground_3d_shading(
            tbls, module_name="440W", module_width_m=1.0, module_length_m=2.0, terrain_mesh=dem_mesh,
            electrical_shading="partition", series_modules=14, is_half_cut=True,
        ))
        self.assertIn("Flags=$000A0300", poly_part)
        self.assertRegex(poly_part, r"ModuleOrient=moPortrait\n\s+DXModule=\d+\.\d{3}\n\s+DYModule=1\.000\n\s+LongShed=")

        # Regular Tracker: axis swap DXModule = long_shed / py = 1.0, DYModule = larg_shed / px = 14.0
        trk_common = dict(
            site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="TrackHorizNSAxis", pan_name="HalfCut440.PAN", ond_name="Inv50.OND",
            n_mod_serie=14, n_string_ch=8, n_inverter=1, n_inv_mppt=2, independent_mppt=True, pnom_ac_in_mppt=25.0,
            pitch_m=8.0, shed_count=4, module_rows=2, module_columns=28,
            module_width_m=1.0, module_length_m=2.0, spacing_x_m=0.0, spacing_y_m=0.04,
        )
        trk_lin = synthesize_utility_project(self.store, **trk_common)
        self.assertIn("Flags=$000A0100", trk_lin["vc0_text"])
        self.assertIn("DXModule=2.040", trk_lin["vc0_text"])
        trk_part = synthesize_utility_project(self.store, **trk_common, electrical_shading="partition")
        self.assertIn("Flags=$000A0300", trk_part["vc0_text"])
        self.assertIn("DXModule=1.010", trk_part["vc0_text"])  # 4.04 / 4
        self.assertIn("DYModule=14.000", trk_part["vc0_text"])  # 28.0 / 2
        self.store.create_project("PartTrk.PRJ", trk_part["prj_text"], {"VC0": trk_part["vc0_text"]})
        self.assertTrue(self.store.validate_structure("PartTrk.PRJ", "VC0")["valid"])

        # Terrain tracker direct builder
        ter_trk_lines, _ = build_terrain_tracker_shading(
            dem_mesh, tracker_count=3, pitch_ew_m=8.0, module_rows=2, module_columns=28,
            module_width_m=1.0, module_length_m=2.0, spacing_x_m=0.0, spacing_y_m=0.04,
            electrical_shading="partition", series_modules=14, is_half_cut=True,
        )
        ter_trk_txt = "\n".join(ter_trk_lines)
        self.assertIn("Flags=$000A0300", ter_trk_txt)
        self.assertIn("DXModule=1.010", ter_trk_txt)
        self.assertIn("DYModule=14.000", ter_trk_txt)

        # Variable-column polygon tracker: cols=28 (px=2 -> DY=14.0) vs cols=42 (px=3 -> DY=15.0)
        var_tbls = [
            {
                "name": "Trk_28", "row_index": 1, "module_rows": 2, "module_columns": 28, "module_count": 56,
                "long_shed_m": 4.0, "larg_shed_m": 28.0, "origin_m": [0.0, 0.0, 1.5], "axis_azimuth_deg": 0.0,
            },
            {
                "name": "Trk_42", "row_index": 2, "module_rows": 2, "module_columns": 42, "module_count": 84,
                "long_shed_m": 4.0, "larg_shed_m": 45.0, "origin_m": [8.0, 0.0, 1.5], "axis_azimuth_deg": 0.0,
            },
        ]
        var_lines, _ = build_tracker_3d_shading(
            var_tbls, module_name="440W", module_width_m=1.0, module_length_m=2.0,
            spacing_x_m=0.0, spacing_y_m=0.0, electrical_shading="partition", series_modules=14, is_half_cut=True,
        )
        var_txt = "\n".join(var_lines)
        self.assertIn("DYModule=14.000", var_txt)  # 28 / (28 // 14 = 2) = 14.0
        self.assertIn("DYModule=15.000", var_txt)  # 45 / (42 // 14 = 3) = 15.0

        # Regular Dome + Polygon/Terrain Dome: both front & back pair objects emit partition DXModule/DYModule
        dome_part = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="Dome", pan_name="HalfCut440.PAN", ond_name="Inv50.OND", bifacial=None,
            n_mod_serie=14, n_string_ch=8, n_inverter=1, n_inv_mppt=2, independent_mppt=True, pnom_ac_in_mppt=25.0,
            tilt_deg=10.0, azimuth_deg=90.0, pitch_m=6.0, top_spacing_m=0.2, shed_count=4,
            module_rows=1, module_columns=28, module_width_m=1.0, module_length_m=2.0, spacing_x_m=0.0, spacing_y_m=0.0,
            electrical_shading="partition",
        )
        self.assertIn("Flags=$000A0300", dome_part["vc0_text"])
        front_blk = _extract_object_block(dome_part["vc0_text"], "PVObject_1=pvShdArrayDome")
        back_blk = _extract_object_block(dome_part["vc0_text"], "PVObject_2=pvShdArrayDome")
        for blk in (front_blk, back_blk):
            self.assertIn("DXModule=14.000", blk)
            self.assertIn("DYModule=1.000", blk)  # 1 row portrait half-cut -> py=2 -> 2.0/2=1.0
        self.store.create_project("PartDome.PRJ", dome_part["prj_text"], {"VC0": dome_part["vc0_text"]})
        self.assertTrue(self.store.validate_structure("PartDome.PRJ", "VC0")["valid"])

        reg_dome_lin = "\n".join(synthesize_ground_dome_shading(shed_count=2, pitch_m=6.0, electrical_shading="linear"))
        self.assertIn("Flags=$000A0100", reg_dome_lin)
        poly_dome_part, _ = build_dome_3d_shading(
            site_polygon=poly, terrain_mesh=dem_mesh, terrain_opts=None, shed_count=2, pitch_m=6.0,
            tilt_deg=10.0, azimuth_deg=90.0, base_slope_deg=0.0, top_spacing_m=0.2,
            module_rows=1, module_columns=10, module_width_m=1.0, module_length_m=2.0, module_name="440W",
            module_orient="moPortrait", spacing_x_m=0.0, spacing_y_m=0.0, setback_m=0.5,
            ground_clearance_m=0.5, max_table_columns=None,
            electrical_shading={"partitions_x": 2, "partitions_y": 2},
        )
        poly_dome_txt = "\n".join(poly_dome_part)
        self.assertIn("Flags=$000A0300", poly_dome_txt)
        self.assertIn("DYModule=1.000", poly_dome_txt)

        # CAD .pvc fixed & tracker + .dxf with partition mode
        write_utf8_crlf(self.ws / "UserData" / "FixedCad.pvc", _make_pvc_fixed(rows=1, cols=2), bom=False)
        parsed_fixed = parse_pvc_141(self.ws, "FixedCad.pvc")
        pvc_fix_lines = build_pvc_3d_shading(
            parsed_fixed, module_name="440W", electrical_shading={"partitions_x": 2, "partitions_y": 2},
        )
        pvc_fix_txt = "\n".join(pvc_fix_lines)
        self.assertIn("Flags=$000A0300", pvc_fix_txt)
        self.assertIn("DXModule=", pvc_fix_txt)

        write_utf8_crlf(self.ws / "UserData" / "TrkCad.pvc", _make_pvc_tracker(count=2), bom=False)
        parsed_trk = parse_pvc_141(self.ws, "TrkCad.pvc")
        pvc_trk_resolved = resolve_cad_synthesis_inputs(
            self.store, field_type="TrackHorizNSAxis", pvc_name="TrkCad.pvc", dxf_name=None,
            dxf_layers=None, dxf_unit_scale=None, tilt_deg=0.0, azimuth_deg=0.0, axis_azimuth_deg=0.0,
            pitch_m=7.0, tilt_explicit=False, azimuth_explicit=False, obstacles=None, terrain=None,
            module_name="440W", electrical_shading={"partitions_x": 1, "partitions_y": 2}, version="8.1.7",
        )
        self.assertIn("first_table", pvc_trk_resolved)
        pvc_trk_txt = "\n".join(pvc_trk_resolved["shading_lines"])
        self.assertIn("Flags=$000A0300", pvc_trk_txt)
        self.assertIn("DXModule=1.042", pvc_trk_txt)

        write_utf8_crlf(self.ws / "UserData" / "SiteCad.dxf", _dxf_text(insunits=6, scale=1.0, rect=(33.0, 15.0)), bom=False)
        dxf_part = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="RegularSheds", pan_name="HalfCut440.PAN", ond_name="Inv50.OND",
            n_mod_serie=14, n_string_ch=8, n_inverter=1, n_inv_mppt=2, independent_mppt=True, pnom_ac_in_mppt=25.0,
            pitch_m=10.0, tilt_deg=20.0, azimuth_deg=0.0, dxf_name="SiteCad.dxf",
            electrical_shading="partition",
        )
        self.assertIn("Flags=$000A0300", dxf_part["vc0_text"])
        self.assertEqual(dxf_part["utility_summary"]["electrical_shading"]["mode"], "partition")

    def test_simulate_design_and_extract_electrical_shading_metrics(self) -> None:
        fake_cal = {"issues": [], "warnings": [], "year": 2024, "rows": 8784}

        def _spec(project: str, electrical_shading) -> dict:
            gl = {
                "model_mode": "3d",
                "pitch_m": 10.0,
                "shed_count": 6,
                "module_rows": 2,
                "module_columns": 19,
            }
            if electrical_shading is not None:
                gl["electrical_shading"] = electrical_shading
            return {
                "scope": "ground_sheds",
                "new_project": project,
                "year": 2024,
                "target_dc_kw": 100.0,
                "tilt": 20.0,
                "azimuth": 0.0,
                "module": {"file_name": "HalfCut440.PAN"},
                "inverter": {"file_name": "Inv50.OND"},
                "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
                "capacity": _capacity_spec(),
                "ground_layout": gl,
            }

        cli = _PartitionFakeCLI(self.ws, year=2024, emit_shd_elec=True, shd_elec_kw=2.5)
        with patch("pvsyst_auto.workflows.utility.inspect_met_calendar", return_value=fake_cal):
            # 1) Partition mode adds viShdElec and populates electrical_shading_metrics
            part_res = simulate_design(
                cli, self.store, spec=_spec("PartSim.PRJ", {"electrical_effect_fraction": 0.9}), dry_run=False,
            )
            self.assertEqual(part_res["status"], "completed", part_res)
            self.assertIn("viShdElec", cli.runs[-1]["sfi_text"])
            self.assertEqual(part_res["utility_summary"]["electrical_shading"]["mode"], "partition")
            # 366 leap year days * 9 daytime hours (8..16 inclusive) * 2.5 kW = 8235.0 kWh
            self.assertEqual(part_res["electrical_shading_metrics"]["shd_elec_unit"], "kW")
            self.assertAlmostEqual(part_res["electrical_shading_metrics"]["shd_elec_kwh"], 366 * 9 * 2.5, places=3)

            # 2) Linear 3D omits viShdElec and leaves electrical_shading_metrics absent/None
            lin_res = simulate_design(cli, self.store, spec=_spec("LinSim.PRJ", "linear"), dry_run=False)
            self.assertEqual(lin_res["status"], "completed", lin_res)
            self.assertNotIn("viShdElec", cli.runs[-1]["sfi_text"])
            self.assertEqual(lin_res["utility_summary"]["electrical_shading"]["mode"], "linear")
            self.assertIsNone(lin_res.get("electrical_shading_metrics"))

            # 3) Missing ShdElec column in partition mode fails cleanly
            cli_missing = _PartitionFakeCLI(self.ws, year=2024, emit_shd_elec=False)
            fail_res = simulate_design(cli_missing, self.store, spec=_spec("MissingShd.PRJ", "partition"), dry_run=False)
            self.assertEqual(fail_res["status"], "failed", fail_res)
            self.assertIn("ShdElec", str(fail_res))

        # Direct unit tests for sub-hourly 15-min kW and Wh energy units + unit guards
        p15 = self.ws / "Results" / "sub15.csv"
        write_sfi_csv(
            p15,
            [("GlobInc", "W/m\u00b2"), ("ShdElec", "kW")],
            [
                ["01/01/24 08:00", 100.0, 4.0],
                ["01/01/24 08:15", 100.0, 4.0],
                ["01/01/24 08:30", 100.0, 8.0],
                ["01/01/24 08:45", 100.0, 4.0],
            ],
        )
        m15 = _extract_electrical_shading_metrics(p15, {"ShdElec": "kW"})
        self.assertEqual(m15["shd_elec_unit"], "kW")
        self.assertAlmostEqual(m15["shd_elec_kwh"], (4.0 + 4.0 + 8.0 + 4.0) * 0.25, places=6)

        p_wh = self.ws / "Results" / "energy_wh.csv"
        write_sfi_csv(
            p_wh,
            [("GlobInc", "W/m\u00b2"), ("ShdElec", "Wh")],
            [
                ["01/01/24 08:00", 100.0, 1500.0],
                ["01/01/24 08:15", 100.0, 2500.0],
            ],
        )
        m_wh = _extract_electrical_shading_metrics(p_wh, {"ShdElec": "Wh"})
        self.assertEqual(m_wh["shd_elec_unit"], "Wh")
        self.assertAlmostEqual(m_wh["shd_elec_kwh"], 4.0, places=6)

        with self.assertRaisesRegex(ValueError, "lacks ShdElec"):
            _extract_electrical_shading_metrics(p15, {})
        with self.assertRaisesRegex(ValueError, "Unsupported ShdElec unit"):
            _extract_electrical_shading_metrics(p15, {"ShdElec": "MVA"})

    def test_repo_guards_line_count_and_mcp_tool_budget(self) -> None:
        root = Path(__file__).resolve().parents[3]
        for py in (root / "src" / "pvsyst_auto").rglob("*.py"):
            n_lines = len(py.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(n_lines, 900, f"{py} has {n_lines} lines (> 900)")

        async def _counts() -> dict[str | None, int]:
            out: dict[str | None, int] = {}
            for grp in (None, "all", "admin", "expert"):
                tools = await create_mcp_server(tool_groups=grp).list_tools()
                out[grp] = len(tools)
            return out

        counts = asyncio.run(_counts())
        self.assertEqual(counts, {None: 48, "all": 75, "admin": 4, "expert": 23})
        self.assertEqual(len(TOOL_GROUPS["Admin & Diagnostics"]), 4)
        self.assertEqual(len(TOOL_GROUPS["Expert & Low-Level"]), 23)


if __name__ == "__main__":
    unittest.main()

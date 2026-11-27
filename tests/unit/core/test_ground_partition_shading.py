"""Unit tests for PR 1: 3D ground partition-based electrical shading."""
from __future__ import annotations

import asyncio
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
from pvsyst_auto.project.template import synthesize_utility_project
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.cad_import import (
    build_pvc_3d_shading,
    parse_pvc_141,
    resolve_cad_synthesis_inputs,
)
from pvsyst_auto.scene.dome import build_dome_3d_shading, synthesize_ground_dome_shading
from pvsyst_auto.scene.reader import (
    build_ground_3d_shading,
    plan_ground_polygon_layout,
    pv_shading_header,
)
from pvsyst_auto.scene.terrain import build_terrain_mesh, build_terrain_tracker_shading
from pvsyst_auto.scene.tracker_layout import build_tracker_3d_shading
from pvsyst_auto.workflows.design import simulate_design
from pvsyst_auto.workflows.utility import (
    resolve_utility_field_type,
    validate_utility_spec,
)
from tests._fixtures import FakeCliBase, init_full_workspace, write_utf8_crlf
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
    """Fake CLI emitting ShdElec (annual hourly or custom sample rows) when requested in .SFI."""

    def __init__(
        self,
        workspace: Path,
        *,
        year: int = 2024,
        emit_shd_elec: bool = True,
        shd_elec_kw: float = 2.5,
        custom_shd: tuple[str, list[tuple[str, float]]] | None = None,
    ):
        super().__init__(workspace, version="8.1.7", license_status="TRIAL")
        self.year = year
        self.emit_shd_elec = emit_shd_elec
        self.shd_elec_kw = shd_elec_kw
        self.custom_shd = custom_shd
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
        if self.custom_shd is not None:
            unit, samples = self.custom_shd
            headers = ["date", "GlobInc", "GlobEff", "EArray", "E_Grid", "PR", "IL_Pmax", "GlobShd", "ShdElec"]
            units = ["", "W/m\u00b2", "W/m\u00b2", "kW", "kW", "ratio", "kW", "W/m\u00b2", unit]
            lines = [
                f"Project;{project}",
                f"Simulation variant;{Path(project).stem}.{variant}",
                f"Geographical Site;{self.site}",
                f"Weather data;{self.met}",
                "",
                ";".join(headers),
                ";".join(units),
            ]
            for stamp, val in samples:
                egrid = 0.5 * nom_kw * 0.84
                lines.append(";".join([stamp, "500.00", "480.00", f"{egrid * 1.02:.3f}", f"{egrid:.3f}", "0.8400", "0.000", "490.00", f"{val:.3f}"]))
            Path(out_csv).write_bytes(("\ufeff" + "\r\n".join(lines) + "\r\n").encode("utf-8"))
        else:
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

    def test_schemas_validation_and_field_type_routing(self) -> None:
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
            self.assertEqual(SingleDesignRequest.model_validate(dumped).model_dump(exclude_none=True), dumped)

        for bad_kw in (
            {"extra": 1},
            {"mode": "invalid"},
            {"unknown_key": 1},
            {"partitions_x": 0},
            {"partitions_x": 1001},
            {"partitions_y": 0},
            {"partitions_y": True},
            {"electrical_effect_fraction": -0.1},
            {"electrical_effect_fraction": 1.2},
            {"electrical_effect_fraction": float("nan")},
            {"auto_half_cut": "true"},
            {"mode": "linear", "partitions_x": 2},
            {"mode": "linear", "partitions_y": 2},
            {"mode": "linear", "electrical_effect_fraction": 0.5},
            {"mode": "linear", "auto_half_cut": False},
        ):
            with self.assertRaises(ValidationError, msg=repr(bad_kw)):
                ElectricalShadingSpec.model_validate(bad_kw)

        for bad_gl in ({"electrical_shading": "invalid"}, {"electrical_shading": 123}):
            with self.assertRaises(ValidationError, msg=repr(bad_gl)):
                GroundLayoutRequest.model_validate(bad_gl)

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

        for gl in (
            {"model_mode": "unlimited", "electrical_shading": "partition"},
            {"model_mode": "unlimited", "electrical_shading": "linear"},
            {"electrical_shading": "linear"},
        ):
            iss = _issues_for("ground_sheds", gl)
            self.assertTrue(any(i["field"] == "ground_layout.electrical_shading" and "requires 3D layout" in i["message"] for i in iss), gl)

        iss_bad = _issues_for("ground_sheds", {"model_mode": "3d", "electrical_shading": {"mode": "linear", "partitions_y": 2}})
        self.assertTrue(any(i["field"] == "ground_layout.electrical_shading" for i in iss_bad), iss_bad)

        for scope, gl, tilt in (
            ("ground_sheds", {"electrical_shading": "partition"}, 20.0),
            ("ground_sheds", {"model_mode": "3d", "electrical_shading": "partition"}, 20.0),
            ("ground_sheds", {"model_mode": "3d", "electrical_shading": "linear"}, 20.0),
            ("ground_tracker", {"electrical_shading": "partition"}, None),
            ("ground_dome", {"electrical_shading": "linear"}, 12.0),
        ):
            self.assertEqual(_issues_for(scope, gl, tilt=tilt), [], (scope, gl))

        # pv_shading_header defaults to linear $000A0100 and FracOmbreMod=1.000
        default_hdr = "\n".join(pv_shading_header("Linear test", 1))
        self.assertIn("Flags=$000A0100", default_hdr)
        self.assertIn("FracOmbreMod=1.000", default_hdr)

    def test_pan_half_cut_and_partition_rules_via_public_builders(self) -> None:
        write_utf8_crlf(
            self.ws / "ComposPV" / "PVmodules" / "MixedCaseHC.PAN",
            HALF_CUT_PAN.replace("SubModuleLayout=slTwinHalfCells", "SubModuleLayout=SLTwinHalfCells"),
        )
        write_utf8_crlf(self.ws / "ComposPV" / "PVmodules" / "FullInLen.PAN", FULL_CELL_PAN)

        base = dict(
            site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="RegularSheds", ond_name="Inv50.OND",
            n_mod_serie=14, n_string_ch=8, n_inverter=1, n_inv_mppt=2, independent_mppt=True, pnom_ac_in_mppt=25.0,
            pitch_m=10.0, tilt_deg=20.0, shed_count=4, module_rows=2, module_columns=28,
            module_width_m=1.0, module_length_m=2.0, spacing_x_m=0.0, spacing_y_m=0.0,
            electrical_shading="partition",
        )
        for pan_name, exp_hc, exp_dy in (
            ("HalfCut440.PAN", True, "DYModule=1.000"),
            ("MixedCaseHC.PAN", True, "DYModule=1.000"),
            ("FullInLen.PAN", False, "DYModule=2.000"),
            ("FullCell440.PAN", False, "DYModule=2.000"),
        ):
            res = synthesize_utility_project(self.store, pan_name=pan_name, **base)
            self.assertEqual(res["utility_summary"]["electrical_shading"]["is_half_cut"], exp_hc, pan_name)
            self.assertIn(exp_dy, res["vc0_text"], pan_name)

        # Landscape modules with half-cut keep py=n_rows; portrait with cols < series_modules clamps px=1
        land_res = synthesize_utility_project(
            self.store, pan_name="HalfCut440.PAN", module_orient="moLandscape", **{k: v for k, v in base.items() if k not in ("module_rows", "module_columns")},
            module_rows=2, module_columns=28,
        )
        self.assertEqual((land_res["utility_summary"]["electrical_shading"]["partitions_x"],
                          land_res["utility_summary"]["electrical_shading"]["partitions_y"]), (2, 2))

        clamp_res = synthesize_utility_project(
            self.store, pan_name="HalfCut440.PAN", **{k: v for k, v in base.items() if k != "module_columns"}, module_columns=10,
        )
        self.assertEqual(clamp_res["utility_summary"]["electrical_shading"]["partitions_x"], 1)

        # Partition span < 0.01 m rejected; exact 0.01 m boundary accepted
        ok_lines = build_ground_3d_shading(
            [{"name": "ExactMin", "origin_m": [0.0, 0.0, 0.5], "tilt_deg": 20.0, "azimuth_deg": 0.0,
              "module_rows": 1, "module_columns": 30, "long_shed_m": 0.30, "larg_shed_m": 0.10}],
            module_name="440W", module_width_m=0.01, module_length_m=0.10, spacing_x_m=0.0, spacing_y_m=0.0,
            electrical_shading={"partitions_x": 30, "partitions_y": 10},
        )
        self.assertIn("DXModule=0.010", "\n".join(ok_lines))
        with self.assertRaisesRegex(ValueError, "0.01m"):
            build_ground_3d_shading(
                [{"name": "TooTiny", "origin_m": [0.0, 0.0, 0.5], "tilt_deg": 20.0, "azimuth_deg": 0.0,
                  "module_rows": 1, "module_columns": 30, "long_shed_m": 0.29, "larg_shed_m": 1.0}],
                module_name="440W", module_width_m=0.009, module_length_m=1.0, spacing_x_m=0.0, spacing_y_m=0.0,
                electrical_shading={"partitions_x": 30, "partitions_y": 1},
            )

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

        # Polygon + terrain shed via build_ground_3d_shading
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

        # Regular Tracker: axis swap DXModule = long_shed / py = 1.01, DYModule = larg_shed / px = 14.0
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
        self.assertIn("DXModule=1.010", trk_part["vc0_text"])
        self.assertIn("DYModule=14.000", trk_part["vc0_text"])
        self.store.create_project("PartTrk.PRJ", trk_part["prj_text"], {"VC0": trk_part["vc0_text"]})
        self.assertTrue(self.store.validate_structure("PartTrk.PRJ", "VC0")["valid"])

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
        self.assertIn("DYModule=14.000", var_txt)
        self.assertIn("DYModule=15.000", var_txt)

        # Regular Dome + Polygon/Terrain Dome
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
            self.assertIn("DYModule=1.000", blk)
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

    def test_simulate_design_electrical_shading_end_to_end(self) -> None:
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

        # Full annual-length checks for Wh and unsupported unit on ShdElec
        cli_wh = _PartitionFakeCLI(
            self.ws, year=2024,
            custom_shd=("Wh", [(dt.strftime("%d/%m/%y %H:%M"), 1000.0 if dt.hour == 12 else 0.0)
                               for dt in (datetime(2024, 1, 1) + timedelta(hours=h) for h in range(8784))]),
        )
        cli_mva = _PartitionFakeCLI(
            self.ws, year=2024,
            custom_shd=("MVA", [(dt.strftime("%d/%m/%y %H:%M"), 1.0)
                                for dt in (datetime(2024, 1, 1) + timedelta(hours=h) for h in range(8784))]),
        )
        with patch("pvsyst_auto.workflows.utility.inspect_met_calendar", return_value=fake_cal):
            wh_res = simulate_design(cli_wh, self.store, spec=_spec("WhSim.PRJ", "partition"), dry_run=False)
            self.assertEqual(wh_res["status"], "completed", wh_res)
            self.assertEqual(wh_res["electrical_shading_metrics"]["shd_elec_unit"], "Wh")
            self.assertAlmostEqual(wh_res["electrical_shading_metrics"]["shd_elec_kwh"], 366.0, places=3)

            mva_res = simulate_design(cli_mva, self.store, spec=_spec("MvaSim.PRJ", "partition"), dry_run=False)
            self.assertEqual(mva_res["status"], "failed", mva_res)
            self.assertIn("Unsupported ShdElec unit", str(mva_res))

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

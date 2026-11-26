"""Unit tests for pure-text zero-template synthesis, auto .PAN geometry, and zero-template house tasks."""
from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from pvsyst_auto.api.schemas import HouseTaskRequest
from pvsyst_auto.components.store import ComponentStore, electrical_inventory
from pvsyst_auto.project.synthesize import (
    DEFAULT_OND_NAME,
    DEFAULT_PAN_NAME,
    SYNTH_VERSION,
    synthesize_project_and_variant,
)
from pvsyst_auto.project.template import prepare_standard_template
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.scene.author import DEFAULT_MODULE_DEPTH_M, resolve_modules
from pvsyst_auto.workflows.house import run_house_task
from tests._fixtures import init_workspace, roof_mesh, write_utf8_crlf
from tests.unit.core.test_components import OND, PAN
from tests.unit.workflows.test_standard_template import CAPACITY, FakeCLI, FixtureComponents, PAN_TEXT, ond_text


SITE_TEXT = """PVObject_=pvSite
  Comment=Geneva;Switzerland;Europe;
  Version=8.1.0
  Flags=$0006283F
  NomF=Geneva_MN90.SIT
  Site=Geneva
  Country=Switzerland
  Region=Europe
  Latitude=46.2400
  Longitude=6.1100
  Altitude=425
  TimeZone=1.0
  HLeg=True
  UnitsDisp=kWh_m2_m
  Albedo=0.20
End of PVObject pvSite
"""

MET_TEXT = """PVObject_=pvMeteo
  Comment=Geneva;Meteonorm;Synthetic
  Version=8.1.0
  Flags=$010084E1
  NomF=Geneva_MN90_SYN.MET
  SiteM=Geneva
  TypeM=Synthetic
  UsesPerez=True
  PasAccum=60
  PVObject_SiteMet=pvSite
    NomF=Geneva_MN90.SIT
    Site=Geneva
    Latitude=46.2400
    Longitude=6.1100
    Altitude=425
    TimeZone=1.0
    HLeg=True
  End of PVObject pvSite
End of PVObject pvMeteo
"""


def _roof_mesh(name: str, ox: float, oy: float, tilt_deg: float, rot_deg: float = 0.0) -> dict:
    return roof_mesh(name, ox, oy, tilt_deg, rot_deg, x_bounds=(-1.0, 22.0), y_bounds=(-1.0, 14.0), z_offset=4.0)


def _south_roof_house_and_layout() -> tuple[dict, dict]:
    house = {"kind": "mesh", "meshes": [_roof_mesh("SouthRoof", 0.0, 0.0, 25.0, 0.0)]}
    layout = {
        "tables": [{
            "name": "SouthArray", "module_rows": 2, "module_columns": 6,
            "orientation": "portrait", "row_spacing_m": 0.02, "column_spacing_m": 0.02,
            "origin_m": [1.0, 1.0, 4.0 + math.tan(math.radians(25.0)) + 0.1],
            "roof_mesh": "SouthRoof",
        }]
    }
    return house, layout


class ZeroTemplateSynthesizerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = init_workspace(Path(tmp.name).resolve())
        write_utf8_crlf(self.root / "Sites" / "Geneva_MN90.SIT", SITE_TEXT)
        write_utf8_crlf(self.root / "Meteo" / "Geneva_MN90_SYN.MET", MET_TEXT)
        write_utf8_crlf(self.root / "ComposPV/PVmodules/Lab440.PAN", PAN)
        write_utf8_crlf(
            self.root / "ComposPV/PVmodules/Lab440Depth.PAN",
            PAN.replace("    Model=440W", "    Model=440W\n    Depth=0.030"),
        )
        write_utf8_crlf(self.root / "ComposPV/Inverters/Lab50.OND", OND)
        self.components = ComponentStore(self.root)
        self.store = VariantStore(self.root, self.components)

    def test_synthesize_single_and_multi_orientation_with_power_sharing(self):
        # Single-plane default fallback uses default PAN/OND when omitted.
        single = synthesize_project_and_variant(
            self.store,
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
        )
        self.assertTrue(single["prj_text"].startswith("\ufeff"))
        self.assertTrue(single["vc0_text"].startswith("\ufeff"))
        self.assertIn("\r\n", single["prj_text"])
        self.assertIn(f"PVModule={DEFAULT_PAN_NAME}", single["vc0_text"])
        self.assertIn(f"GInverter={DEFAULT_OND_NAME}", single["vc0_text"])
        self.assertIn("Latitude=46.2400", single["prj_text"])
        self.assertIn("Latitude=46.2400", single["vc0_text"])
        self.store.create_project("SingleSynth.PRJ", single["prj_text"], {"VC0": single["vc0_text"]})
        validated = self.store.validate_structure("SingleSynth.PRJ", "VC0")
        self.assertTrue(validated["valid"], validated)
        self.assertEqual(validated["system_type"], "Grid")

        # Multi-orientation + PowerSharingInverterId=0
        multi = synthesize_project_and_variant(
            self.store,
            project_comment="Dual Roof PowerSharing",
            variant_comment="East-West Dual",
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
            orientations=[
                {"id": 1, "tilt_deg": 25.0, "azimuth_deg": -90.0},
                {"id": 2, "tilt_deg": 25.0, "azimuth_deg": 90.0},
            ],
            subarrays=[
                {
                    "subarray_id": 1, "orientation_id": 1, "comment": "East Array",
                    "pan_name": "Lab440.PAN", "ond_name": "Lab50.OND",
                    "n_mod_serie": 12, "n_string_ch": 1, "n_inverter": 1, "n_inv_mppt": 1,
                    "independent_mppt": True, "power_sharing_inverter_id": 0,
                },
                {
                    "subarray_id": 2, "orientation_id": 2, "comment": "West Array",
                    "pan_name": "Lab440.PAN", "ond_name": "Lab50.OND",
                    "n_mod_serie": 12, "n_string_ch": 1, "n_inverter": 1, "n_inv_mppt": 1,
                    "independent_mppt": True, "power_sharing_inverter_id": 0,
                },
            ],
        )
        self.store.create_project("DualSynth.PRJ", multi["prj_text"], {"VC0": multi["vc0_text"]})
        v_multi = self.store.validate_structure("DualSynth.PRJ", "VC0")
        self.assertTrue(v_multi["valid"], v_multi)
        self.assertEqual(v_multi["orientation_ids"], [1, 2])
        self.assertEqual(v_multi["subarray_ids"], [1, 2])
        self.assertIn("PowerSharingInverterId=0", multi["vc0_text"])
        self.assertIn("SubArrayName=East Array; West Array", multi["vc0_text"])

    def test_prepare_standard_template_allow_synthesize_without_dataro(self):
        # No DataRO directory exists next to FakeCLI.
        install = self.root / "no_dataro_install"
        install.mkdir()
        (install / "PVsystCLI.exe").write_bytes(b"stub")
        (self.root / "ComposPV/PVmodules/Generic_Mono_300W.PAN").write_bytes(
            PAN_TEXT.replace("\n", "\r\n").encode("utf-8")
        )
        (self.root / "ComposPV/Inverters/Generic_7.5kW.OND").write_bytes(
            ond_text(4).replace("\n", "\r\n").encode("utf-8")
        )
        cli = FakeCLI(install / "PVsystCLI.exe", self.root)
        store = VariantStore(self.root, FixtureComponents(self.root))
        ond_elec = electrical_inventory("OND", ond_text(4).replace("\n", "\r\n"))

        preview = prepare_standard_template(
            cli, store, ond_electrical=ond_elec,
            pan_name="Generic_Mono_300W.PAN", ond_name="Generic_7.5kW.OND",
            new_project="ZeroTemplate_template.PRJ",
            dry_run=True, capacity=CAPACITY, allow_synthesize=True,
        )
        self.assertEqual((preview["status"], preview["source"]["mode"], preview["source"]["version"]),
                         ("ready", "synthesized", SYNTH_VERSION), preview)
        self.assertEqual(list((self.root / "Projects").iterdir()), [])

        created = prepare_standard_template(
            cli, store, ond_electrical=ond_elec,
            pan_name="Generic_Mono_300W.PAN", ond_name="Generic_7.5kW.OND",
            new_project="ZeroTemplate_template.PRJ",
            dry_run=False, capacity=CAPACITY, allow_synthesize=True,
        )
        self.assertEqual(created["status"], "created", created)
        check = store.validate_structure("ZeroTemplate_template.PRJ", "VC0")
        self.assertTrue(check["valid"], check)

    def test_auto_pan_geometry_resolution_and_opaque_fail_closed(self):
        # Readable loose PAN without Depth -> auto dimensions + default frame depth 0.035m
        mods, snap = resolve_modules(self.components, "Lab440.PAN", None)
        self.assertIsNotNone(snap)
        self.assertEqual((mods["width_m"], mods["length_m"], mods["nom_power"], mods["depth_m"]),
                         (1.032, 1.996, 440.0, DEFAULT_MODULE_DEPTH_M))
        self.assertEqual((mods["metadata_source"], mods["depth_source"]), ("source-pan", "default-frame-depth"))

        # Readable loose PAN with PVObject_Commercial/Depth -> uses PAN commercial Depth
        mods_d, _ = resolve_modules(self.components, "Lab440Depth.PAN", None)
        self.assertEqual((mods_d["depth_m"], mods_d["depth_source"]), (0.030, "pan-commercial-depth"))

        # Explicit module_geometry mismatch still fails closed
        with self.assertRaisesRegex(ValueError, "disagrees with the loose PAN"):
            resolve_modules(self.components, "Lab440.PAN", {
                "model_name": "440W", "nom_power_wp": 500.0,
                "width_m": 1.032, "length_m": 1.996, "geometry_depth_m": 0.035,
            })

        # Missing / opaque encrypted DB PAN without explicit module_geometry still fails closed
        with self.assertRaisesRegex(ValueError, "module_geometry is required"):
            resolve_modules(self.components, "OpaqueBuiltIn.PAN", None)

    def test_house_task_schema_and_zero_template_end_to_native_saved(self):
        house, layout = _south_roof_house_and_layout()

        # Schema rejects mixing source_project and zero-template fields, or omitting site/met.
        with self.assertRaises(ValidationError):
            HouseTaskRequest(
                new_project="ZT.PRJ", variant_name="V1", year=2024,
                house=house, layout=layout,
            )
        with self.assertRaises(ValidationError):
            HouseTaskRequest(
                source_project="A.PRJ", source_variant="VC0", site_name="Geneva_MN90.SIT",
                new_project="ZT.PRJ", variant_name="V1", year=2024,
                house=house, layout=layout,
            )

        # Valid zero-template request with NO source_project, NO source_variant, NO module_geometry!
        req_model = HouseTaskRequest(
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN",
            ond_name="Lab50.OND",
            new_project="ZeroHouse.PRJ",
            variant_name="Zero Template South",
            year=1990,
            house=house,
            layout=layout,
            report_pdf=False,
            shading_mode="linear",
        )
        req = req_model.model_dump(exclude_none=True)

        # Dry run writes nothing and auto-resolves PAN geometry.
        cli = FakeCLI(self.root / "PVsystCLI.exe", self.root)
        dry = run_house_task(self.store, self.components, cli, request=req, dry_run=True)
        self.assertEqual(dry["status"], "ready", dry)
        self.assertTrue(dry["plan"]["synthesized"])
        self.assertEqual(dry["plan"]["modules"]["depth_source"], "default-frame-depth")
        self.assertAlmostEqual(dry["plan"]["nominal_dc_kw"], 12 * 0.440, places=6)
        self.assertEqual(list((self.root / "Projects").iterdir()), [])

        # Invalid native precision must fail before the synthesized project is written.
        bad_layout = {"tables": [dict(layout["tables"][0], row_spacing_m=0.0005)]}
        before = set(self.root.rglob("*"))
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                blocked = run_house_task(self.store, self.components, cli, request={**req, "layout": bad_layout},
                                         dry_run=dry_run, confirm=not dry_run)
                self.assertEqual(blocked["status"], "blocked", blocked)
                self.assertRegex(blocked["issues"][0], r"SouthArray.*row_spacing_m=0\.0005 m.*0\.001 m")
                self.assertEqual(set(self.root.rglob("*")), before)

        # Multi-roof zero-template dry run with two distinct roof orientations and auto PAN dimensions.
        house_dual = {
            "kind": "mesh",
            "meshes": [
                _roof_mesh("EastRoof", 0.0, 0.0, 25.0, 0.0),
                _roof_mesh("WestRoof", 30.0, 0.0, 25.0, 180.0),
            ],
        }
        layout_dual = {
            "tables": [
                {
                    "name": "EastTable", "orientation_id": 1, "module_rows": 2, "module_columns": 6,
                    "orientation": "portrait", "row_spacing_m": 0.02, "column_spacing_m": 0.02,
                    "origin_m": [1.0, 1.0, 4.0 + math.tan(math.radians(25.0)) + 0.1],
                    "roof_mesh": "EastRoof",
                },
                {
                    "name": "WestTable", "orientation_id": 2, "module_rows": 2, "module_columns": 6,
                    "orientation": "portrait", "row_spacing_m": 0.02, "column_spacing_m": 0.02,
                    "origin_m": [29.0, -1.0, 4.0 + math.tan(math.radians(25.0)) + 0.1],
                    "roof_mesh": "WestRoof",
                },
            ]
        }
        req_dual = HouseTaskRequest(
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN",
            ond_name="Lab50.OND",
            anchor_azimuth_deg=-90.0,
            new_project="ZeroDualHouse.PRJ",
            variant_name="Zero Template Dual",
            year=1990,
            house=house_dual,
            layout=layout_dual,
            report_pdf=False,
            shading_mode="linear",
        ).model_dump(exclude_none=True)
        dry_dual = run_house_task(self.store, self.components, cli, request=req_dual, dry_run=True)
        self.assertEqual(dry_dual["status"], "ready", dry_dual)
        self.assertEqual(len(dry_dual["plan"]["source"]["orientations"]), 2)
        self.assertEqual([o["field_azimuth_deg"] for o in dry_dual["plan"]["source"]["orientations"]],
                         [-90.0, 90.0])
        self.assertAlmostEqual(dry_dual["plan"]["nominal_dc_kw"], 24 * 0.440, places=6)

    def test_zero_template_mixed_pan_sizes_require_table_geometry(self):
        wider_pan = (PAN.replace("Model=440W", "Model=480W").replace("PNom=440", "PNom=480")
                     .replace("LargApp=1.032", "LargApp=1.150").replace("LongApp=1.996", "LongApp=2.200"))
        (self.root / "ComposPV/PVmodules/Lab480.PAN").write_bytes(
            wider_pan.replace("\n", "\r\n").encode("utf-8-sig")
        )
        house = {"kind": "mesh", "meshes": [
            _roof_mesh("EastRoof", 0.0, 0.0, 25.0, 0.0),
            _roof_mesh("WestRoof", 30.0, 0.0, 25.0, 180.0),
        ]}
        z = 4.0 + math.tan(math.radians(25.0)) + 0.1
        tables = [
            {"name": name + "Table", "orientation_id": i, "module_rows": 2, "module_columns": 6,
             "orientation": "portrait", "row_spacing_m": 0.02, "column_spacing_m": 0.02,
             "origin_m": origin, "roof_mesh": name + "Roof"}
            for i, name, origin in ((1, "East", [1.0, 1.0, z]), (2, "West", [29.0, -1.0, z]))
        ]
        request = HouseTaskRequest(
            site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET", ond_name="Lab50.OND",
            anchor_azimuth_deg=-90.0, new_project="MixedHouse.PRJ", variant_name="Mixed PANs",
            year=1990, house=house, layout={"tables": tables}, report_pdf=False,
            subarrays=[{"subarray_id": i, "orientation_id": i, "pan_name": pan,
                        "n_mod_serie": 12, "n_string_ch": 1}
                       for i, pan in ((1, "Lab440.PAN"), (2, "Lab480.PAN"))],
        ).model_dump(exclude_none=True)
        cli = FakeCLI(self.root / "PVsystCLI.exe", self.root)
        before = set(self.root.rglob("*"))
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                blocked = run_house_task(self.store, self.components, cli, request=request,
                                         dry_run=dry_run, confirm=not dry_run)
                self.assertEqual(blocked["status"], "blocked", blocked["issues"])
                self.assertRegex(blocked["issues"][0], r"WestTable.*module_geometry")
                self.assertEqual(set(self.root.rglob("*")), before)

        west = dict(tables[1], module_geometry={"model_name": "480W", "nom_power_wp": 480.0,
                                               "width_m": 1.150, "length_m": 2.200,
                                               "geometry_depth_m": DEFAULT_MODULE_DEPTH_M})
        explicit = dict(request, new_project="ExplicitGeometryHouse.PRJ", layout={"tables": [tables[0], west]})
        ready = run_house_task(self.store, self.components, cli, request=explicit, dry_run=True)
        self.assertEqual(ready["status"], "ready", ready["issues"])

    def test_zero_template_preserves_embedded_sitemet_and_cp1252_met_headers(self):
        # 1. Embedded PVObject_SiteMet in the .MET must stay distinct from the project .SIT.
        hawaii_sit = (
            "PVObject_=pvSite\n"
            "  Comment=Hawaii_2020;United States;North_America;\n"
            "  Version=8.0.0\n"
            "  Flags=$0037\n"
            "  NomF=Hawaii_2020.SIT\n"
            "  Site=Hawaii_2020\n"
            "  Latitude=19.7281\n"
            "  Longitude=-156.0589\n"
            "  Altitude=4\n"
            "  TimeZone=-10.0\n"
            "  HLeg=True\n"
            "  DateBeg=01/01/20 00:00\n"
            "  DateEnd=31/12/20 23:00\n"
            "  GlobalH=140.2,156.1,166.8,199.6,209.2,200.4,204.6,209.1,199.3,177.3,128.6,145.0,2136.1,\n"
            "End of PVObject pvSite\n"
        )
        hawaii_met = (
            "PVObject_=pvMeteo\n"
            "  Comment=Hawaii_2020;Custom file;Imported\n"
            "  Version=8.0.0\n"
            "  Flags=$04000043\n"
            "  NomF=Hawaii_2020_1min_ClipCorr.MET\n"
            "  SiteM=Hawaii_2020\n"
            "  TypeM=Imported\n"
            "  UsesPerez=True\n"
            "  DateDebF=01/01/22 00:00\n"
            "  DateFinF=31/12/22 23:00\n"
            "  PasAccum=1\n"
            "  PVObject_SiteMet=pvSite\n"
            "    Comment=Hawaii_2020;United States;North_America;\n"
            "    Version=8.0.0\n"
            "    Flags=$0037\n"
            "    Site=Hawaii_2020\n"
            "    Latitude=19.7281\n"
            "    Longitude=-156.0589\n"
            "    Altitude=4\n"
            "    TimeZone=-10.0\n"
            "    HLeg=True\n"
            "    DateBeg=01/01/22 00:00\n"
            "    DateEnd=31/12/22 23:00\n"
            "    GlobalH=140.2,149.5,170.3,197.6,207.0,200.1,207.7,206.6,199.3,178.3,129.2,146.4,2132.4,\n"
            "  End of PVObject pvSite\n"
            "  VarMeteo, list of=1 TVarSimul\n"
            "End of PVObject pvMeteo\n"
        )
        write_utf8_crlf(self.root / "Sites" / "Hawaii_2020.SIT", hawaii_sit)
        write_utf8_crlf(self.root / "Meteo" / "Hawaii_2020_1min_ClipCorr.MET", hawaii_met)
        synth = synthesize_project_and_variant(
            self.store,
            site_name="Hawaii_2020.SIT",
            met_name="Hawaii_2020_1min_ClipCorr.MET",
        )
        vc0 = synth["vc0_text"]
        site_simul = vc0.split("PVObject_SiteSimul=pvSite", 1)[1].split("End of PVObject pvSite", 1)[0]
        site_met = vc0.split("PVObject_SiteMet=pvSite", 1)[1].split("End of PVObject pvSite", 1)[0]
        self.assertIn("DateBeg=01/01/20 00:00", site_simul)
        self.assertIn("DateEnd=31/12/20 23:00", site_simul)
        self.assertIn("2136.1,", site_simul)
        self.assertIn("DateBeg=01/01/22 00:00", site_met)
        self.assertIn("DateEnd=31/12/22 23:00", site_met)
        self.assertIn("2132.4,", site_met)
        self.assertIn("DateBeg=01/01/20 00:00", synth["prj_text"])

        # 2. Legacy CP1252 bytes in a .MET header must not erase readable declarations.
        cp1252_met = (
            "PVObject_=pvMeteo\n"
            "  Comment=Geneva;M\u00e9t\u00e9onorm;Synthetic\n"
            "  Version=8.1.0\n"
            "  Flags=$010084E1\n"
            "  NomF=Geneva_CP1252.MET\n"
            "  SiteM=Geneva\n"
            "  TypeM=Synthetic\n"
            "  DateDebF=01/01/90 00:00\n"
            "  DateFinF=31/12/90 23:00\n"
            "  UsesPerez=True\n"
            "  PasAccum=60\n"
            "  PVObject_SiteMet=pvSite\n"
            "    NomF=Geneva_MN90.SIT\n"
            "    Site=Gen\u00e8ve\n"
            "    Latitude=46.2400\n"
            "    Longitude=6.1100\n"
            "    Altitude=425\n"
            "    TimeZone=1.0\n"
            "    HLeg=True\n"
            "  End of PVObject pvSite\n"
            "  VarMeteo, list of=1 TVarSimul\n"
            "End of PVObject pvMeteo\n"
        )
        (self.root / "Meteo" / "Geneva_CP1252.MET").write_bytes(
            cp1252_met.replace("\n", "\r\n").encode("cp1252")
        )
        synth_cp = synthesize_project_and_variant(
            self.store,
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_CP1252.MET",
        )
        meteo_section = synth_cp["vc0_text"].split("PVObject_MeteoSimul=pvMeteo", 1)[1].split(
            "End of PVObject pvMeteo", 1
        )[0]
        for expected in (
            "TypeM=Synthetic",
            "DateDebF=01/01/90 00:00",
            "DateFinF=31/12/90 23:00",
            "PasAccum=60",
            "Site=Gen\u00e8ve",
        ):
            self.assertIn(expected, meteo_section)

    def test_zero_template_preserves_sub_tenth_tilt_and_explicit_loss_flags(self):
        cli = FakeCLI(self.root / "PVsystCLI.exe", self.root)

        # 3. Sub-tenth tilts (25.049 deg and 89.96 deg) stay precise to 3 decimals and pass house preflight.
        for label, tilt, origin in (
            ("Near25", 25.049, [1.0, 0.0, 4.001]),
            ("Near90", 89.96, [1.0, 0.0, 4.001]),
        ):
            house = {"kind": "mesh", "meshes": [_roof_mesh("SouthRoof", 0.0, 0.0, tilt, 0.0)]}
            layout = {
                "tables": [{
                    "name": "SouthArray", "module_rows": 2, "module_columns": 6,
                    "orientation": "portrait", "row_spacing_m": 0.02, "column_spacing_m": 0.02,
                    "origin_m": origin, "roof_mesh": "SouthRoof",
                }]
            }
            for explicit_orient in (True, False):
                with self.subTest(label=label, explicit_orient=explicit_orient):
                    kwargs = dict(
                        site_name="Geneva_MN90.SIT",
                        met_name="Geneva_MN90_SYN.MET",
                        pan_name="Lab440.PAN",
                        ond_name="Lab50.OND",
                        new_project=f"ZeroTilt_{label}_{explicit_orient}.PRJ",
                        variant_name=f"Tilt {tilt}",
                        year=1990,
                        house=house,
                        layout=layout,
                        report_pdf=False,
                        shading_mode="linear",
                    )
                    if explicit_orient:
                        kwargs["orientations"] = [{"id": 1, "tilt_deg": tilt, "azimuth_deg": 0.0}]
                    req = HouseTaskRequest(**kwargs).model_dump(exclude_none=True)
                    dry = run_house_task(self.store, self.components, cli, request=req, dry_run=True)
                    self.assertEqual(dry["status"], "ready", dry["issues"])
                    self.assertAlmostEqual(dry["plan"]["source"]["tilt_deg"], tilt, places=3)

        # 4. Explicit pmism_mpp and module_quality_loss clear their default bits; omitted fields keep them.
        house, layout = _south_roof_house_and_layout()
        cases = (
            ("both_omitted", {}, "$001E0002", True, True),
            ("only_mism", {"pmism_mpp": 5.0}, "$001A0002", False, True),
            ("only_qual", {"module_quality_loss": -2.0}, "$000E0002", True, False),
            ("both_explicit", {"pmism_mpp": 5.0, "module_quality_loss": -2.0}, "$000A0002", False, False),
        )
        for name, overrides, expected_flags, mism_default_before, qual_default_before in cases:
            with self.subTest(case=name):
                from pvsyst_auto.project.synthesize import synthesize_house_source

                sub_spec = {
                    "subarray_id": 1, "orientation_id": 1,
                    "n_mod_serie": 12, "n_string_ch": 1,
                    **overrides,
                }
                req = HouseTaskRequest(
                    site_name="Geneva_MN90.SIT",
                    met_name="Geneva_MN90_SYN.MET",
                    pan_name="Lab440.PAN",
                    ond_name="Lab50.OND",
                    new_project=f"LossFlags_{name}.PRJ",
                    variant_name=f"Loss {name}",
                    year=1990,
                    house=house,
                    layout=layout,
                    subarrays=[sub_spec],
                    report_pdf=False,
                    shading_mode="linear",
                ).model_dump(exclude_none=True)
                synth = synthesize_house_source(self.store, self.components, req)
                self.store.create_project(f"LossFlags_{name}.PRJ", synth["prj_text"], {"VC0": synth["vc0_text"]})
                params = self.store.inspect_parameters(f"LossFlags_{name}.PRJ", "VC0")
                sub_params = params["subarrays"][0]["values"]
                self.assertEqual(sub_params["Flags"], expected_flags)
                if "pmism_mpp" in overrides:
                    self.assertEqual(sub_params["PMismMPP"], "5.00")
                if "module_quality_loss" in overrides:
                    self.assertEqual(sub_params["PQualityModU"], "-2.00")
                    self.assertEqual(sub_params["PQualityModI"], "-2.00")

                upd = self.store.update_parameters(
                    f"LossFlags_{name}.PRJ", "VC0",
                    subarray_id=1,
                    subarray_updates={"PMismMPP": 5.0, "ModuleQualityLoss": -2.0},
                    dry_run=True,
                )
                self.assertEqual(upd["mismatch"]["default_before"], mism_default_before)
                self.assertFalse(upd["mismatch"]["default_after"])
                self.assertEqual(upd["module_quality"]["default_before"], qual_default_before)
                self.assertFalse(upd["module_quality"]["default_after"])

    def test_house_task_refused_attempt_marks_failed_and_nonzero_base_slope_blocked_in_preflight(self):
        """Issues #191 & #193: refused CLI attempt must mark status='failed'; non-zero base_slope rejected preflight."""
        import json
        from pvsyst_auto.native.cli import PVsystError

        house, layout = _south_roof_house_and_layout()

        # Non-zero base_slope_deg must fail in both dry_run=True and confirm=True before any writes
        cli = FakeCLI(self.root / "PVsystCLI.exe", self.root)
        for bad_slope in (2.0, -0.01):
            with self.subTest(base_slope_deg=bad_slope):
                proj = f"BadSlope_{str(bad_slope).replace('-', 'm').replace('.', 'p')}.PRJ"
                req = HouseTaskRequest(
                    site_name="Geneva_MN90.SIT",
                    met_name="Geneva_MN90_SYN.MET",
                    pan_name="Lab440.PAN",
                    ond_name="Lab50.OND",
                    new_project=proj,
                    variant_name="Bad Slope",
                    year=1990,
                    house=house,
                    layout=layout,
                    orientations=[{"id": 1, "tilt_deg": 25.0, "azimuth_deg": 0.0, "base_slope_deg": bad_slope}],
                    report_pdf=False,
                    shading_mode="linear",
                ).model_dump(exclude_none=True)
                dry = run_house_task(self.store, self.components, cli, request=req, dry_run=True)
                self.assertEqual(dry["status"], "blocked")
                self.assertTrue(any("base_slope_deg" in msg for msg in dry["issues"]), dry["issues"])
                confirmed = run_house_task(self.store, self.components, cli, request=req, dry_run=False, confirm=True)
                self.assertEqual(confirmed["status"], "blocked")
                self.assertFalse((self.root / "Projects" / proj).exists())

        # Refused CLI simulation must mark runs[-1].status == 'failed' in response and checkpoint
        class RefusingCLI(FakeCLI):
            def run_simulation(self, *args, **kwargs):
                raise PVsystError("Simulation cannot be run.\nRefused by native CLI.")

        refusing_cli = RefusingCLI(self.root / "PVsystCLI.exe", self.root)
        req = HouseTaskRequest(
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN",
            ond_name="Lab50.OND",
            new_project="RefusedHouse.PRJ",
            variant_name="Refused Run",
            year=1990,
            house=house,
            layout=layout,
            report_pdf=False,
            shading_mode="linear",
        ).model_dump(exclude_none=True)
        res1 = run_house_task(self.store, self.components, refusing_cli, request=req, dry_run=False, confirm=True)
        self.assertEqual(res1["status"], "needs_attention")
        self.assertEqual(len(res1["runs"]), 1)
        self.assertEqual(res1["runs"][0]["status"], "failed")
        manifest_path = Path(res1["artifacts"]["manifest"])
        saved1 = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(saved1["runs"][0]["status"], "failed")

        # Resume without retry_simulation must block without adding a second attempt
        res_block = run_house_task(self.store, self.components, refusing_cli, request=req,
                                   dry_run=False, confirm=True, resume=True)
        self.assertEqual(res_block["status"], "needs_attention")
        self.assertEqual(len(res_block["runs"]), 1)
        self.assertEqual(res_block["runs"][0]["status"], "failed")

        # Explicit retry that also refuses must mark run02 as 'failed' too
        res2 = run_house_task(self.store, self.components, refusing_cli, request=req,
                              dry_run=False, confirm=True, resume=True, retry_simulation=True)
        self.assertEqual(res2["status"], "needs_attention")
        self.assertEqual(len(res2["runs"]), 2)
        self.assertEqual([r["status"] for r in res2["runs"]], ["failed", "failed"])
        saved2 = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual([r["status"] for r in saved2["runs"]], ["failed", "failed"])

    def test_house_task_rechecks_protected_inputs_after_native_save(self):
        import json
        from datetime import datetime, timedelta
        from unittest.mock import patch

        from pvsyst_auto.native.cli import PVsystError
        from pvsyst_auto.workflows import house as house_flow
        from tests._fixtures import write_sfi_csv

        house, layout = _south_roof_house_and_layout()
        request = HouseTaskRequest(
            site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN", ond_name="Lab50.OND", new_project="GuardHouse.PRJ",
            variant_name="Guard", year=1990, house=house, layout=layout,
            report_pdf=False, shading_mode="linear",
        ).model_dump(exclude_none=True)
        cli = FakeCLI(self.root / "PVsystCLI.exe", self.root)
        save = house_flow._save
        snapshot = None

        def edit_sfi_after_save(path, record, **kwargs):
            nonlocal snapshot
            save(path, record, **kwargs)
            if record["phase"] == "native_saved" and snapshot is None:
                sfi = Path(record["artifacts"]["sfi"])
                original = sfi.read_bytes()
                snapshot = (sfi, original, dict(record["guards"]))
                sfi.write_bytes(original.replace(b"House native hourly results", b"Edited SFI"))

        with patch.object(house_flow, "_save", side_effect=edit_sfi_after_save), \
                patch.object(cli, "run_simulation", create=True, side_effect=PVsystError("Unexpected simulation attempt")) as simulate:
            result = run_house_task(self.store, self.components, cli, request=request, dry_run=False, confirm=True)
        simulate.assert_not_called()
        self.assertEqual((result["status"], result["phase"], result["runs"]), ("needs_attention", "native_saved", []), result)
        self.assertIn("A protected source or prepared artifact changed", result["issues"][0])
        sfi, original, guards = snapshot
        manifest = Path(result["artifacts"]["manifest"])
        saved = manifest.read_bytes()
        saved_record = json.loads(saved)
        self.assertEqual((saved_record["phase"], saved_record["guards"], saved_record["runs"]), ("native_saved", guards, []))

        sfi.write_bytes(original.replace(b"House native hourly results", b"Edited while stopped"))
        with patch.object(cli, "run_simulation", create=True) as simulate:
            resumed = run_house_task(self.store, self.components, cli, request=request,
                                     dry_run=False, confirm=True, resume=True)
        simulate.assert_not_called()
        self.assertEqual((resumed["status"], resumed["phase"], resumed["runs"]), ("blocked", "native_saved", []), resumed)
        self.assertIn("A protected source or prepared artifact changed", resumed["issues"][-1])
        self.assertEqual(manifest.read_bytes(), saved)

        def simulate_annual(project, variant, *, out_csv, site, met, **kwargs):
            start = datetime(1990, 1, 1)
            columns = [("GlobInc", "W/m2"), ("GlobShd", "W/m2"), ("E_Grid", "kW"),
                       ("EArray", "kW"), ("IL_Pmax", "kW"), ("PR", "ratio")]
            rows = []
            for offset in range(8760):
                stamp = start + timedelta(hours=offset)
                values = (500, 450, 2.112, 2.2, 0, 0.8) if 8 <= stamp.hour <= 16 else (0, 0, 0, 0, 0, 0)
                rows.append([stamp.strftime("%d/%m/%y %H:%M"), *values])
            write_sfi_csv(out_csv, columns, rows, project=project, site=site.name, met=met.name,
                          variant=f"{Path(project).stem}.{variant}")
            return {"warnings": []}

        sfi.write_bytes(original)
        with patch.object(cli, "run_simulation", create=True, side_effect=simulate_annual) as simulate:
            completed = run_house_task(self.store, self.components, cli, request=request,
                                       dry_run=False, confirm=True, resume=True)
        simulate.assert_called_once()
        self.assertIn(completed["status"], ("completed", "needs_review"), completed)
        self.assertEqual(completed["phase"], "completed")
        self.assertTrue(completed["native_consumption_verified"])

    def test_house_v070_compat_scene_import_and_gui_closed_resume(self):
        """Issue #274: v0.7.0 scene_import_method='native' and phase='gui_closed' resume cleanly."""
        import json
        from unittest.mock import patch
        from pvsyst_auto.api.projects import pvsyst_import_house_pv_scene
        from pvsyst_auto.api.schemas import SourcedHouseTaskRequest
        from pvsyst_auto.api.simulation import pvsyst_run_house_task, pvsyst_run_sourced_house_task
        from pvsyst_auto.native.cli import PVsystError

        house, layout = _south_roof_house_and_layout()

        # 1. Schemas accept scene_import_method='native' and reject legacy non-native values.
        model_native = HouseTaskRequest(
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN",
            ond_name="Lab50.OND",
            new_project="V070House.PRJ",
            variant_name="V070 Compat",
            year=1990,
            house=house,
            layout=layout,
            scene_import_method="native",
            report_pdf=False,
            shading_mode="linear",
        )
        sourced_native = SourcedHouseTaskRequest(
            prepare_mode="preserve_source_hardware",
            source_project="V070House.PRJ",
            source_variant="VC0",
            design={"year": 1990},
            new_project="V070Sourced.PRJ",
            variant_name="V070 Sourced",
            house=house,
            layout=layout,
            scene_import_method="native",
            report_pdf=False,
            shading_mode="linear",
        )
        self.assertEqual(model_native.scene_import_method, "native")
        self.assertEqual(sourced_native.scene_import_method, "native")
        with self.assertRaises(ValidationError):
            HouseTaskRequest(**{**model_native.model_dump(exclude_none=True), "scene_import_method": "gui"})
        with self.assertRaises(ValidationError):
            SourcedHouseTaskRequest(**{**sourced_native.model_dump(exclude_none=True), "scene_import_method": "gui"})

        # 2. pvsyst_import_house_pv_scene accepts method='native' and rejects non-native.
        cli = FakeCLI(self.root / "PVsystCLI.exe", self.root)
        with patch("pvsyst_auto.api.projects.variant_store", return_value=self.store), \
             patch("pvsyst_auto.api.projects.cli", return_value=cli):
            with self.assertRaisesRegex(ValueError, "native"):
                pvsyst_import_house_pv_scene("Missing.pvc2", "Var1", dry_run=True, method="gui")

        # 3. Create checkpoint at simulation boundary, rewrite as v0.7.0 phase='gui_closed', and resume.
        class RefusingCLI(FakeCLI):
            def run_simulation(self, *args, **kwargs):
                raise PVsystError("Interrupted before run")

        ref_cli = RefusingCLI(self.root / "PVsystCLI.exe", self.root)
        saved = run_house_task(
            self.store, self.components, ref_cli,
            request=model_native.model_dump(exclude_none=True),
            dry_run=False, confirm=True,
        )
        manifest_path = Path(saved["artifacts"]["manifest"])
        record = json.loads(manifest_path.read_text(encoding="utf-8"))
        record["phase"] = "gui_closed"
        record["runs"] = []
        record["request"]["scene_import_method"] = "native"
        manifest_path.write_text(json.dumps(record, indent=2), encoding="utf-8")

        model_omitted = HouseTaskRequest(
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN",
            ond_name="Lab50.OND",
            new_project="V070House.PRJ",
            variant_name="V070 Compat",
            year=1990,
            house=house,
            layout=layout,
            report_pdf=False,
            shading_mode="linear",
        )
        class SimCLI(FakeCLI):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.sim_calls = []

            def run_simulation(self, *args, **kwargs):
                from datetime import datetime, timedelta
                from tests._fixtures import write_sfi_csv

                self.sim_calls.append((args, kwargs))
                out_csv = kwargs["out_csv"]
                proj, var = args[0], args[1]
                start = datetime(1990, 1, 1, 0, 0)
                rows = []
                for step in range(8760):
                    dt = start + timedelta(hours=step)
                    day = 8 <= dt.hour < 16
                    rows.append([
                        dt.strftime("%d/%m/%y %H:%M"),
                        500.0 if day else 0.0,
                        5.0 if day else 0.0,
                        2.2704 if day else 0.0,
                        2.2176 if day else 0.0,
                        0.0,
                        0.84 if day else 0.0,
                    ])
                write_sfi_csv(
                    out_csv,
                    [
                        ("GlobInc", "W/m2"),
                        ("GlobShd", "W/m2"),
                        ("EArray", "kW"),
                        ("E_Grid", "kW"),
                        ("IL_Pmax", "kW"),
                        ("PR", "ratio"),
                    ],
                    rows,
                    project=proj,
                    variant=f"{Path(proj).stem}.{var}",
                    site="Geneva_MN90.SIT",
                    met="Geneva_MN90_SYN.MET",
                )
                return {"csv": str(out_csv), "stdout": "Simulation done", "warnings": [], "returncode": 0}

        run_cli = SimCLI(self.root / "PVsystCLI.exe", self.root)
        with patch("pvsyst_auto.api.simulation.variant_store", return_value=self.store), \
             patch("pvsyst_auto.api.simulation.cli", return_value=run_cli):
            resumed_omit = pvsyst_run_house_task(model_omitted, dry_run=False, confirm=True, resume=True)
            self.assertIn(resumed_omit["status"], ("completed", "needs_review"), resumed_omit)
            self.assertEqual(resumed_omit["phase"], "completed")
            self.assertEqual(len(run_cli.sim_calls), 1)

            # Reset checkpoint to gui_closed again and resume with explicit scene_import_method='native'
            Path(resumed_omit["artifacts"]["csv"]).unlink(missing_ok=True)
            record2 = json.loads(manifest_path.read_text(encoding="utf-8"))
            record2["phase"] = "gui_closed"
            record2["runs"] = []
            record2["request"]["scene_import_method"] = "native"
            manifest_path.write_text(json.dumps(record2, indent=2), encoding="utf-8")
            resumed_native = pvsyst_run_house_task(model_native, dry_run=False, confirm=True, resume=True)
            self.assertIn(resumed_native["status"], ("completed", "needs_review"), resumed_native)
            self.assertEqual(resumed_native["phase"], "completed")
            self.assertEqual(len(run_cli.sim_calls), 2)

            # pvsyst_run_sourced_house_task preserves native scene_import_method cleanly.
            sourced_dry = pvsyst_run_sourced_house_task(sourced_native, dry_run=True)
            self.assertEqual(sourced_dry["status"], "ready", sourced_dry)
            self.assertFalse(sourced_dry["house"]["plan"]["synthesized"])

        with patch("pvsyst_auto.api.projects.variant_store", return_value=self.store):
            imp_ok = pvsyst_import_house_pv_scene(saved["plan"]["scene_name"], "V070 Import", dry_run=True, method="native")
            self.assertEqual(imp_ok["method"], "native", imp_ok)
            self.assertEqual(imp_ok["status"], "ready_to_import", imp_ok)


if __name__ == "__main__":
    unittest.main()

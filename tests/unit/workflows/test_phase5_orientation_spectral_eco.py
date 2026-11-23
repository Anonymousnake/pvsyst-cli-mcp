"""Unit tests for v0.6.0 Phase 5: spectral correction, cross-orientation conversion, and native TEcoEvaluation."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.project.orientation_convert import convert_orientation
from pvsyst_auto.project.synthesize import synthesize_project_and_variant
from pvsyst_auto.project.variants import VariantStore
from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT


class Phase5OrientationSpectralEcoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "PVsyst8.1_Data"
        for sub in ("Projects", "Sites", "Meteo", "Results", "Models", "ComposPV/PVmodules", "ComposPV/Inverters"):
            (self.ws / sub).mkdir(parents=True, exist_ok=True)
        (self.ws / "Sites" / "Geneva_MN90.SIT").write_text(SITE_TEXT, encoding="utf-8")
        (self.ws / "Meteo" / "Geneva_MN90_SYN.MET").write_text(MET_TEXT, encoding="utf-8")
        (self.ws / "ComposPV/PVmodules/Lab440.PAN").write_text(UTILITY_PAN, encoding="utf-8")
        (self.ws / "ComposPV/Inverters/Lab50.OND").write_text(UTILITY_OND, encoding="utf-8")
        self.components = ComponentStore(self.ws)
        self.store = VariantStore(self.ws, self.components)
        synth = synthesize_project_and_variant(
            self.store,
            project_comment="Phase5Test",
            variant_comment="Base",
            site_name="Geneva_MN90.SIT",
            met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN",
            ond_name="Lab50.OND",
            n_mod_serie=20,
            n_string_ch=10,
            n_inverter=1,
            n_inv_mppt=1,
            tilt_deg=25.0,
            azimuth_deg=0.0,
        )
        self.store.create_project("Phase5Test.PRJ", synth["prj_text"], {"VC0": synth["vc0_text"]})
        self.project = "Phase5Test.PRJ"
        self.variant = "VC0"

    def test_spectral_correction_enable_custom_disable(self) -> None:
        res = self.store.update_parameters(
            self.project,
            self.variant,
            spectral_updates={"enabled": True, "coeff_set": "CdTe", "use_rel_hum": False},
            dry_run=False,
        )
        self.assertTrue(res["changed"])
        self.assertEqual(res["spectral"]["after"]["enabled"], True)
        self.assertEqual(res["spectral"]["after"]["coeff_set"], "CdTe")
        self.assertEqual(res["spectral"]["after"]["use_rel_hum"], False)

        # Custom 6-coefficient polynomial
        res2 = self.store.update_parameters(
            self.project,
            self.variant,
            spectral_updates={
                "enabled": True,
                "coeff_set": "Custom",
                "coefficients": [0.85, -0.04, -0.005, 0.14, 0.004, -0.003],
            },
            dry_run=False,
        )
        text = (self.ws / "Projects" / "Phase5Test.VC0").read_text(encoding="latin-1")
        self.assertIn("CoeffSet=Custom", text)
        self.assertIn("Coefficients=0.850000,-0.040000,-0.005000,0.140000,0.004000,-0.003000,", text)
        self.assertEqual(res2["spectral"]["after"]["use_default_coeffs"], False)

        # Disable
        res3 = self.store.update_parameters(
            self.project,
            self.variant,
            spectral_updates={"enabled": False},
            dry_run=False,
        )
        self.assertEqual(res3["spectral"]["after"]["enabled"], False)

    def test_convert_orientation_all_targets(self) -> None:
        targets = [
            ("fixed", {"tilt_deg": 28.0, "azimuth_deg": 10.0}, "FieldType=FixedPlane"),
            ("seasonal", {"summer_tilt_deg": 15.0, "winter_tilt_deg": 45.0, "winter_months": [1, 2, 11, 12]}, "FieldType=SeasonalTilt"),
            ("sheds", {"tilt_deg": 20.0, "pitch_m": 8.0, "collector_width_m": 4.0, "shed_count": 12}, "FieldType=Sheds"),
            ("tracker_ns", {"min_phi_deg": -55.0, "max_phi_deg": 55.0, "pitch_m": 6.0, "collector_width_m": 2.2, "backtracking": True}, "FieldType=UnlimTracking"),
            ("tracker_2axis", {"min_tilt_deg": 15.0, "max_tilt_deg": 75.0, "min_phi_deg": -120.0, "max_phi_deg": 120.0}, "FieldType=TrackTwoAxis"),
        ]
        for idx, (target, params, expected_marker) in enumerate(targets, start=1):
            new_vc = f"VC{idx}"
            res = convert_orientation(
                self.store,
                project=self.project,
                source_variant=self.variant,
                new_variant=new_vc,
                target_type=target,
                params=params,
                dry_run=False,
            )
            self.assertFalse(res["dry_run"], target)
            self.assertEqual(res["orientation_conversion"]["target_type"], target)
            txt = (self.ws / "Projects" / f"Phase5Test.{new_vc}").read_text(encoding="latin-1")
            self.assertIn(expected_marker, txt)
            self.assertNotIn("PVObject_Ombrage=pvShading", txt)

    def test_public_dome_conversion_blocks_unallocated_faces(self) -> None:
        from unittest.mock import patch
        from pvsyst_auto.api.projects import pvsyst_convert_orientation

        source = self.ws / "Projects" / "Phase5Test.VC0"
        text = source.read_text(encoding="utf-8-sig")
        text = text.replace("NoOrient=1", "NoOrient=10").replace("NoOrientation=1", "NoOrientation=10")
        source.write_text(text, encoding="utf-8")
        with patch("pvsyst_auto.api.projects.variant_store", return_value=self.store):
            result = pvsyst_convert_orientation(self.project, "VC0", "VC1", target_type="dome", dry_run=False)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse((self.ws / "Projects" / "Phase5Test.VC1").exists())

    def test_public_conversion_rejects_paired_subarray(self) -> None:
        from unittest.mock import patch
        from pvsyst_auto.api.projects import pvsyst_convert_orientation

        source = self.ws / "Projects" / "Phase5Test.VC0"
        text = source.read_text(encoding="utf-8-sig")
        text = text.replace("NoOrientation=1", "NoOrientation=2\n      NoOrientation2=3")
        source.write_text(text, encoding="utf-8")
        with patch("pvsyst_auto.api.projects.variant_store", return_value=self.store):
            result = pvsyst_convert_orientation(self.project, "VC0", "VC1", target_type="fixed", dry_run=False)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse((self.ws / "Projects" / "Phase5Test.VC1").exists())

    def test_public_conversion_blocks_pvDomeOrient_and_schema_omits_dome(self) -> None:
        from typing import get_args, get_type_hints
        from unittest.mock import patch
        from pvsyst_auto.api.projects import pvsyst_convert_orientation

        hints = get_type_hints(pvsyst_convert_orientation)
        self.assertNotIn("dome", get_args(hints["target_type"]))

        source = self.ws / "Projects" / "Phase5Test.VC0"
        text = source.read_text(encoding="utf-8-sig")
        text = text.replace("FieldType=FixedPlane", "FieldType=Dome\n  PVObject_Dome=pvDomeOrient")
        source.write_text(text, encoding="utf-8")
        with patch("pvsyst_auto.api.projects.variant_store", return_value=self.store):
            result = pvsyst_convert_orientation(self.project, "VC0", "VC1", target_type="fixed", dry_run=False)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse((self.ws / "Projects" / "Phase5Test.VC1").exists())

    def test_native_economics_write_and_remove(self) -> None:
        res = self.store.update_parameters(
            self.project,
            self.variant,
            native_economics_updates={
                "mode": "set",
                "capex_total": 450000.0,
                "annual_opex": 8000.0,
                "subsidies": 50000.0,
                "currency": "EUR",
                "project_life_years": 25,
                "discount_rate_pct": 6.0,
                "sale_tariff": 0.09,
                "consumption_tariff": 0.22,
            },
            dry_run=False,
        )
        self.assertTrue(res["changed"])
        self.assertTrue(res["native_economics"]["configured"])
        txt = (self.ws / "Projects" / "Phase5Test.VC0").read_text(encoding="latin-1")
        self.assertIn("EconomicEval=TEcoEvaluation", txt)
        self.assertIn("Currency=EUR", txt)
        self.assertIn("UnitPrice=450000.00000", txt)
        self.assertIn("OwnFunds=400000.00", txt)
        self.assertIn("Subsidies=50000.00", txt)
        self.assertIn("End of TEcoEvaluation", txt)

        # Remove native economics
        res2 = self.store.update_parameters(
            self.project,
            self.variant,
            native_economics_updates={"mode": "none"},
            dry_run=False,
        )
        self.assertFalse(res2["native_economics"]["configured"])
        txt2 = (self.ws / "Projects" / "Phase5Test.VC0").read_text(encoding="latin-1")
        self.assertNotIn("EconomicEval=TEcoEvaluation", txt2)


if __name__ == "__main__":
    unittest.main()

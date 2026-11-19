"""Unit tests for Phase 2: read-only sizing assistant (suggest_sizing) and auto_from_components mode."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pvsyst_auto.api.guidance import SERVER_INSTRUCTIONS, WORKFLOW_GUIDE_MD, _variables_guide_md
from pvsyst_auto.api.schemas import SingleDesignRequest
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.engineering.suggest import suggest_sizing
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.workflows.design import EXPECTED_WORKSPACE_BASENAME, prepare_design_project, simulate_design
from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN, _FakeUtilityCLI
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT


class SuggestSizingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / EXPECTED_WORKSPACE_BASENAME
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in ("Projects", "Sites", "Meteo", "Models", "Results",
                    "ComposPV/PVmodules", "ComposPV/Inverters"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        (self.root / "Sites" / "Geneva_MN90.SIT").write_bytes(
            b"\xef\xbb\xbf" + SITE_TEXT.replace("\n", "\r\n").encode("utf-8"))
        (self.root / "Meteo" / "Geneva_MN90_SYN.MET").write_bytes(
            b"\xef\xbb\xbf" + MET_TEXT.replace("\n", "\r\n").encode("utf-8"))
        (self.root / "ComposPV/PVmodules/Lab440.PAN").write_bytes(
            b"\xef\xbb\xbf" + UTILITY_PAN.replace("\n", "\r\n").encode("utf-8"))
        (self.root / "ComposPV/Inverters/Lab50.OND").write_bytes(
            b"\xef\xbb\xbf" + UTILITY_OND.replace("\n", "\r\n").encode("utf-8"))
        self.components = ComponentStore(self.root)
        self.store = VariantStore(self.root, self.components)

    def test_suggest_sizing_derives_realistic_dc_ac_and_drop_in_capacity(self):
        res = suggest_sizing(
            self.store,
            pan_name="Lab440.PAN",
            ond_name="Lab50.OND",
            target_dc_kw=105.0,
        )
        self.assertEqual(res["status"], "ready", res["issues"])
        self.assertTrue(res["estimated"])
        self.assertTrue(res["requires_review"])
        rec = res["recommended"]
        # Must NOT pick 7 oversized inverters (DC/AC = 0.30); must honor min_dc_ac_ratio >= 0.85
        self.assertEqual(rec["inverter_count"], 2)
        self.assertGreaterEqual(rec["dc_ac_ratio"], 0.85)
        self.assertLessEqual(rec["dc_ac_ratio"], 1.25)
        self.assertGreaterEqual(rec["voltage_checks"]["hot_string_vmp_v"], 200.0)
        self.assertLessEqual(rec["voltage_checks"]["cold_string_voc_v"], 1500.0)
        self.assertIn("min_dc_ac_ratio", res["capacity"])
        self.assertTrue(res["assumptions"])

    def test_suggest_sizing_rejects_impossible_target_or_missing_component(self):
        missing = suggest_sizing(self.store, pan_name="Nope.PAN", ond_name="Lab50.OND", target_dc_kw=100.0)
        self.assertEqual(missing["status"], "blocked")
        bad_temp = suggest_sizing(
            self.store, pan_name="Lab440.PAN", ond_name="Lab50.OND", target_dc_kw=100.0,
            min_ambient_c=30.0, min_operating_c=10.0,
        )
        self.assertEqual(bad_temp["status"], "blocked")

    def test_simulate_design_auto_from_components_marks_needs_review_with_assumptions(self):
        cli = _FakeUtilityCLI(self.root, year=2024)
        fake_cal = {"issues": [], "warnings": [], "year": 2024, "rows": 8784}

        # Reject combining auto_from_components with explicit capacity
        bad = simulate_design(cli, self.store, spec={
            "scope": "ground_tracker", "sizing_mode": "auto_from_components",
            "new_project": "Conflict.PRJ", "year": 2024, "target_dc_kw": 105.0,
            "module": {"file_name": "Lab440.PAN"}, "inverter": {"file_name": "Lab50.OND"},
            "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            "capacity": {"target_dc_kw": 105.0},
        }, dry_run=True)
        self.assertEqual((bad["status"], bad["stage"]), ("blocked", "spec"))

        # Valid auto_from_components utility tracker + bifacial run
        req = SingleDesignRequest(
            scope="ground_tracker",
            sizing_mode="auto_from_components",
            new_project="AutoTracker.PRJ",
            year=2024,
            target_dc_kw=105.0,
            module={"file_name": "Lab440.PAN"},
            inverter={"file_name": "Lab50.OND"},
            weather={"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            ground_layout={"model_mode": "3d", "pitch_m": 12.0, "shed_count": 6},
            bifacial={"ground_albedo": 0.30, "bifaciality_factor": 0.80, "height_above_ground_m": 2.4},
        )
        with patch("pvsyst_auto.workflows.utility.inspect_met_calendar", return_value=fake_cal):
            dry = simulate_design(cli, self.store, spec=req.model_dump(exclude_none=True), dry_run=True)
            self.assertEqual((dry["status"], dry["stage"]), ("ready", "preflight"))
            self.assertTrue(dry["sizing_assumptions"])

            ran = simulate_design(cli, self.store, spec=req.model_dump(exclude_none=True), dry_run=False)
            self.assertEqual((ran["status"], ran["stage"]), ("needs_review", "completed"))
            self.assertTrue(ran["native_consumption_verified"])
            self.assertTrue(ran["sizing_assumptions"])
            self.assertEqual(ran["preview"]["utility_capacity"]["inverter_count"], 2)

            prep_req = req.model_copy(update={"new_project": "AutoTrackerPrep.PRJ"})
            prep = prepare_design_project(cli, self.store, spec=prep_req.model_dump(exclude_none=True), dry_run=False)
            self.assertEqual((prep["status"], prep["stage"], prep["prepared"]), ("needs_review", "prepared", True))

    def test_guidance_resources_contain_key_workflows_and_variables(self):
        self.assertIn("pvsyst_suggest_sizing", SERVER_INSTRUCTIONS)
        self.assertIn("pvsyst://guide/workflows", SERVER_INSTRUCTIONS)
        self.assertIn("auto_from_components", WORKFLOW_GUIDE_MD)
        vguide = _variables_guide_md()
        self.assertIn("bifacial", vguide)
        self.assertIn("viGlobBak", vguide)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for HouseHold & Probability native loads, random SystemUnavail, and generated WeakGrid outages."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.energy.availability import native_availability
from pvsyst_auto.energy.unavailability import replace_system_unavailability, system_unavail_state
from pvsyst_auto.native.load import flat_load, native_load_block
from pvsyst_auto.project.variants import VariantStore


class SpecialLoadsAndOutagesTests(unittest.TestCase):
    def test_household_and_probability_load_serialization_and_roundtrip(self) -> None:
        # 1. Yearly HouseHold load
        hh_yearly = native_load_block({
            "mode": "household",
            "modulation": "yearly",
            "appliances": [
                {"name": "LED Lighting", "count": 8, "power_w": 15.0, "hours": [18, 19, 20, 21, 22]},
                {"name": "Fridge", "count": 1, "power_w": 120.0, "hours": list(range(24))},
            ],
        }, "\r\n")
        self.assertIn("LoadType=HouseHold", hh_yearly)
        self.assertIn("UsesModul=Yearly", hh_yearly)
        self.assertIn("HoursDefined=16777215,16777215,", hh_yearly)
        # Verify flat_load round-trip recognizes HouseHold inside a root pvVCalcul wrapper
        wrapped_hh = f"PVObject_=pvVCalcul\r\n{hh_yearly}End of PVObject pvVCalcul\r\n"
        parsed_hh = flat_load(wrapped_hh.splitlines(keepends=True))
        self.assertIsNotNone(parsed_hh)
        self.assertEqual(parsed_hh[2], "HouseHold")

        # 2. Seasonal HouseHold load
        app = [{"name": "Lamp", "count": 2, "power_w": 20.0, "half_hours": [36, 37, 38, 39]}]
        hh_seas = native_load_block({
            "mode": "household",
            "modulation": "seasonal",
            "seasons": {"Summer": app, "Autumn": app, "Winter": app, "Spring": app},
        }, "\n")
        self.assertIn("UsesModul=Seasonal", hh_seas)
        self.assertIn("Appliances for period=Winter", hh_seas)

        # 3. Probability load (deterministically sampled into native embedded HourlyFile snapshot)
        prob_text = native_load_block({
            "mode": "probability",
            "power_step_kw": 2.5,
            "probabilities_pct": [10.0, 25.0, 35.0, 20.0, 10.0],
            "seed": 42,
        }, "\r\n")
        self.assertIn("LoadType=HourlyFile", prob_text)
        self.assertIn("Day_0101=", prob_text)
        wrapped_pr = f"PVObject_=pvVCalcul\r\n{prob_text}End of PVObject pvVCalcul\r\n"
        parsed_pr = flat_load(wrapped_pr.splitlines(keepends=True))
        self.assertIsNotNone(parsed_pr)
        self.assertEqual(parsed_pr[2], "HourlyFile")

        with self.assertRaisesRegex(ValueError, "sum to 100.0%"):
            native_load_block({"mode": "probability", "power_step_kw": 2.0, "probabilities_pct": [30.0, 30.0]}, "\n")

    def test_generated_weak_grid_outage_availability_is_deterministic(self) -> None:
        decl = {
            "mode": "generated",
            "year": 2024,
            "outage_count": 20,
            "min_duration_hours": 3,
            "max_duration_hours": 6,
            "peak_hours": [18, 19, 20],
            "seed": 123,
        }
        txt1, meta1 = native_availability(None, decl, "\r\n")
        txt2, meta2 = native_availability(None, decl, "\r\n")
        self.assertEqual(txt1, txt2)
        self.assertEqual(meta1["rows"], 8784)  # 2024 leap year
        self.assertGreater(meta1["unavailable_hours"], 40)
        self.assertIn("PVObject_GridUnavail=pvUnavail", txt1)
        self.assertIn("+0", txt1)
        self.assertIn("+G8", txt1)

    def test_random_system_unavailability_roundtrip(self) -> None:
        lines = [
            "PVObject_System=pvSystem\r\n",
            "  Flags=$00\r\n",
            "  SystemType=Grid\r\n",
            "End of PVObject pvSystem\r\n",
        ]
        res = replace_system_unavailability(
            lines, (0, 3),
            {
                "mode": "random",
                "time_fraction_pct": 2.5,
                "period_count": 6,
                "min_duration_hours": 4,
                "max_duration_hours": 24,
                "random_seed": 42,
            },
        )
        self.assertEqual(res["after"]["mode"], "Periods")
        self.assertEqual(res["after"]["period_count"], 6)
        self.assertEqual(res["after"]["settings"]["NbPeriods"], "6")
        self.assertEqual(res["after"]["settings"]["TimeFraction"], "0.0250")
        joined = "".join(lines)
        self.assertIn("UnavailPeriods=Start", joined)
        self.assertIn("RandomInit =42", joined)
        self.assertIn("Flags=$00010000", joined)
        # Removal cleanly restores state
        rm = replace_system_unavailability(lines, (0, len(lines) - 1), {"mode": "none"})
        self.assertFalse(rm["after"]["configured"])


if __name__ == "__main__":
    unittest.main()

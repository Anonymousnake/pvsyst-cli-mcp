"""Seasonal tilt edit atomicity regression tests."""
import unittest
from unittest.mock import patch

from pvsyst_auto.project import seasonal

ORIENT = ["  PVObject_=pvOrient\r\n", "    Version=8.1.6\r\n", "    Flags=$00\r\n",
          "    NoOrient=1\r\n", "    FieldType=FixedPlane\r\n", "    FieldTilt=20.0\r\n",
          "    FieldAzim=0.0\r\n", "    BaseSlope=0.0\r\n", "    UsesTiltAver=False\r\n",
          "    UsesAzimAver=False\r\n", "    UsesSlopeAver=False\r\n", "  End of TOrientGroup\r\n"]
UPDATES = {"summer_tilt": 10.0, "winter_tilt": 50.0, "winter_months": [1, 2, 12]}


class ReplaceSeasonalTest(unittest.TestCase):
    def test_failure_after_base_slope_removal_leaves_lines_untouched(self):
        lines = list(ORIENT)
        real = seasonal._read
        calls = []

        def failing_read(target, bounds):
            calls.append(bounds)
            if len(calls) > 1:
                raise ValueError("forced")
            return real(target, bounds)

        with patch.object(seasonal, "_read", failing_read):
            with self.assertRaisesRegex(ValueError, "forced"):
                seasonal.replace_seasonal(lines, (0, len(lines) - 1), UPDATES)
        self.assertEqual(lines, ORIENT)

    def test_success_commits_seasonal_tilt(self):
        lines = list(ORIENT)
        change = seasonal.replace_seasonal(lines, (0, len(lines) - 1), UPDATES)
        self.assertEqual(change["after"]["winter_months"], [1, 2, 12])
        self.assertNotIn("    BaseSlope=0.0\r\n", lines)
        self.assertIn("    FieldType=SeasonalTilt\r\n", lines)
        self.assertIn("    WinterMonths=$0803\r\n", lines)


if __name__ == "__main__":
    unittest.main()

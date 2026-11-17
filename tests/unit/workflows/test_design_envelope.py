"""Design-envelope preparation tests: declared inputs only, no fitting or guessing.

These tests use in-memory component text and the read-only electrical inventory.
They perform no file access, no hashing, no SDK call and no native simulation.
"""
import unittest

from pvsyst_auto.components.store import electrical_inventory
from pvsyst_auto.engineering.envelope import prepare_design_envelope
from pvsyst_auto.engineering.sizing import NUMBERS as CAPACITY_NUMBERS


PAN_TEXT = """PVObject_=pvModule
  Comment=Generic\tMono 440W half-cell\tUser device
  Version=8.1.6
  Flags=$00

  PVObject_Commercial=pvCommercial
    Manufacturer=Generic
    Model=Mono 440W half-cell
    DataSource=Reference module
  End of PVObject pvCommercial

  Technol=mtSiMono
  NCelS=72
  NCelP=1
  NDiode=3
  PNom=440.0
  GRef=1000.0
  TRef=25.0
  Absorb=0.900
  VMaxIEC=1500
  VMaxUL=1000
  ISC=11.050
  Voc=49.600
  Imp=10.450
  Vmp=42.100
  MuISC=0.005500
  muVocSpec=-0.148500
End of PVObject pvModule
"""

OND_TEXT = """PVObject_=pvGInverter
  Comment=Solis S6-EH3P50K-H\t50 kW three-phase hybrid inverter\tUser device from flyer
  Version=8.1.6
  Flags=$0570

  PVObject_Commercial=pvCommercial
    Manufacturer=Solis
    Model=S6-EH3P50K-H
    DataSource=Solis flyer
  End of PVObject pvCommercial

  Transfo=Without

  Converter=TConverter
    PNomConv=50.000
    PMaxOUT=50.000
    VOutConv=400.0
    VMppMin=150
    VMPPMax=850
    VAbsMax=1000
    IDCMax=0.000
    ModeOper=MPPT
    CompPMax=Lim
    CompVMax=Lim
  End of TConverter
  NbInputs=4
  NbMPPT=4
End of PVObject pvGInverter
"""


def base_request(**overrides):
    request = {
        "manufacturer_model": "Generic Mono 440W half-cell",
        "target_dc_kw": 21.6,
        "max_dc_ac_ratio": 1.3,
        "voltage_standard": "IEC",
        "temperatures": {
            "min_ambient_c": -10.0,
            "min_ambient_basis": "IEC 62548-1 lowest temperature observed on site",
            "min_module_operating_c": 20.0,
            "max_module_operating_c": 60.0,
        },
        "coefficients": {
            "beta_voc": {"value": -0.25, "unit": "pct_per_degC", "source": "manufacturer datasheet 2024"},
            "beta_vmp": {"value": -0.30, "unit": "pct_per_degC", "source": "manufacturer datasheet 2024"},
            "alpha_isc": {"value": 0.045, "unit": "pct_per_degC", "source": "manufacturer datasheet 2024"},
        },
        "module_limits": {
            "max_operating_current_a": {"value": 12.0, "source": "manufacturer maximum operating current"},
            "max_short_circuit_current_a": {"value": 12.6, "source": "manufacturer maximum short-circuit current"},
        },
        "mppt_limits": {
            "max_operating_current_a": {"value": 32.0, "source": "inverter datasheet per-MPPT rating"},
            "max_short_circuit_current_a": {"value": 40.0, "source": "inverter datasheet per-MPPT rating"},
        },
        "effective_irradiance": {
            "max_irradiance_w_m2": 1100.0,
            "rear_gain_factor": 1.0,
            "source": "manufacturer maximum irradiance condition",
        },
    }
    request.update(overrides)
    return request


def issues_of(result):
    return [issue["field"] for issue in result["issues"]]


def warnings_of(result):
    return [warning["field"] for warning in result["warnings"]]


class DesignEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.module = electrical_inventory("PAN", PAN_TEXT)
        self.inverter = electrical_inventory("OND", OND_TEXT)
        self.assertEqual(self.module["status"], "readable")
        self.assertEqual(self.inverter["status"], "readable")

    def prepare(self, request=None):
        return prepare_design_envelope(self.module, self.inverter, request or base_request())

    def test_coefficient_mode_builds_covered_capacity(self):
        result = self.prepare()
        self.assertEqual(result["status"], "ready", result["issues"])
        self.assertEqual(result["envelope_mode"], "coefficients")
        capacity = result["capacity"]
        self.assertAlmostEqual(capacity["cold_voc_v"], 49.6 * (1 + 0.0025 * 35), places=9)
        self.assertAlmostEqual(capacity["cold_vmp_v"], 42.1 * (1 + 0.003 * 5), places=9)
        self.assertAlmostEqual(capacity["hot_vmp_v"], 42.1 * (1 - 0.003 * 35), places=9)
        self.assertLessEqual(capacity["hot_vmp_v"], 42.1)
        self.assertLessEqual(42.1, capacity["cold_vmp_v"])
        self.assertGreaterEqual(capacity["cold_voc_v"], 49.6)
        self.assertEqual(result["provenance"]["method"], "public linear temperature envelope")
        self.assertIn("muVocSpec", result["provenance"]["unused_serialized_fields"])
        self.assertIn("MuISC", result["provenance"]["unused_serialized_fields"])

    def test_capacity_keys_match_planner_contract(self):
        capacity = self.prepare()["capacity"]
        allowed = set(CAPACITY_NUMBERS) | {"capacity_tolerance_pct", "voltage_standard", "design_basis"}
        self.assertEqual(set(capacity), allowed)
        for name in CAPACITY_NUMBERS:
            self.assertIsInstance(capacity[name], (int, float))

    def test_reference_above_operating_band_skips_only_the_vmp_check(self):
        request = base_request()
        request["temperatures"] = dict(request["temperatures"], min_module_operating_c=30.0)
        result = self.prepare(request)
        # Cold Vmp is computed at 30 degC, below the 25 degC reference Vmp by construction;
        # only the Voc check still applies because -10 <= 25 degC.
        self.assertEqual(result["status"], "ready", result["issues"])
        self.assertLess(result["capacity"]["cold_vmp_v"], 42.1)
        self.assertIn("temperatures", warnings_of(result))
        self.assertTrue(any("Vmp coverage check" in warning["message"]
                            for warning in result["warnings"]))

    def test_explicit_envelope_overrides_coefficients(self):
        request = base_request(coefficients=None,
                               envelope={"cold_voc_v": 56.0, "cold_vmp_v": 45.0, "hot_vmp_v": 38.0})
        result = self.prepare(request)
        self.assertEqual(result["status"], "ready", result["issues"])
        self.assertEqual(result["envelope_mode"], "explicit")
        self.assertEqual(result["capacity"]["cold_voc_v"], 56.0)
        self.assertEqual(result["capacity"]["cold_vmp_v"], 45.0)
        self.assertEqual(result["capacity"]["hot_vmp_v"], 38.0)

    def test_missing_voltage_data_blocks_and_lists_both_fields(self):
        result = self.prepare(base_request(coefficients=None, envelope=None))
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["capacity"])
        fields = issues_of(result)
        self.assertIn("coefficients.beta_voc", fields)
        self.assertIn("coefficients.beta_vmp", fields)

    def test_positive_beta_voc_is_rejected(self):
        request = base_request()
        request["coefficients"]["beta_voc"] = {"value": 0.25, "unit": "pct_per_degC", "source": "typo"}
        result = self.prepare(request)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("coefficients.beta_voc", issues_of(result))

    def test_unknown_coefficient_unit_blocks(self):
        request = base_request()
        request["coefficients"]["beta_voc"] = {"value": -0.25, "unit": "percent", "source": "ambiguous"}
        result = self.prepare(request)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("coefficients.beta_voc.unit", issues_of(result))

    def test_missing_coefficient_source_blocks(self):
        request = base_request()
        request["coefficients"]["beta_vmp"] = {"value": -0.30, "unit": "pct_per_degC"}
        result = self.prepare(request)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("coefficients.beta_vmp.source", issues_of(result))

    def test_imp_without_coefficient_requires_direct_limit(self):
        request = base_request()
        del request["module_limits"]["max_operating_current_a"]
        result = self.prepare(request)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("module_limits.max_operating_current_a", issues_of(result))

    def test_isc_coefficient_derives_short_circuit_but_not_operating(self):
        request = base_request()
        del request["module_limits"]["max_short_circuit_current_a"]
        result = self.prepare(request)
        self.assertEqual(result["status"], "ready", result["issues"])
        expected = 11.05 * (1100.0 / 1000.0) * (1 + 0.00045 * 35)
        self.assertAlmostEqual(result["capacity"]["module_max_short_circuit_current_a"], expected, places=9)
        self.assertEqual(result["capacity"]["module_max_operating_current_a"], 12.0)

    def test_annual_extreme_is_not_accepted_as_design_extreme(self):
        request = base_request()
        temperatures = dict(request["temperatures"])
        del temperatures["min_ambient_c"]
        temperatures["annual_min_ambient_c"] = -5.0
        request["temperatures"] = temperatures
        result = self.prepare(request)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("temperatures.min_ambient_c", issues_of(result))

    def test_annual_extreme_equal_to_design_extreme_warns(self):
        temperatures = dict(base_request()["temperatures"])
        temperatures["annual_min_ambient_c"] = temperatures["min_ambient_c"]
        result = self.prepare(base_request(temperatures=temperatures))
        self.assertEqual(result["status"], "ready", result["issues"])
        self.assertIn("temperatures.annual_min_ambient_c", warnings_of(result))

    def test_module_operating_temperature_must_differ_from_ambient(self):
        temperatures = dict(base_request()["temperatures"])
        temperatures["min_module_operating_c"] = temperatures["min_ambient_c"]
        result = self.prepare(base_request(temperatures=temperatures))
        self.assertIn("temperatures.min_module_operating_c", warnings_of(result))

    def test_mppt_limits_cannot_come_from_a_converter_current_field(self):
        positive = electrical_inventory("OND", OND_TEXT.replace("IDCMax=0.000", "IDCMax=150.000"))
        self.assertEqual(positive["fields"]["IDCMax"]["status"], "declared")
        request = base_request(mppt_limits=None)
        result = prepare_design_envelope(self.module, positive, request)
        self.assertEqual(result["status"], "blocked")
        fields = issues_of(result)
        self.assertIn("mppt_limits.max_operating_current_a", fields)
        self.assertIn("mppt_limits.max_short_circuit_current_a", fields)
        self.assertTrue(all("IDCMax" in issue["message"] for issue in result["issues"]))

    def test_short_circuit_limits_must_cover_operating_limits(self):
        request = base_request()
        request["mppt_limits"]["max_short_circuit_current_a"] = {"value": 30.0, "source": "datasheet"}
        result = self.prepare(request)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("mppt_limits.max_short_circuit_current_a", issues_of(result))

    def test_module_operating_envelope_must_cover_reference_currents(self):
        request = base_request()
        request["module_limits"]["max_operating_current_a"] = {"value": 9.0, "source": "datasheet"}
        result = self.prepare(request)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("module_limits.max_operating_current_a", issues_of(result))

    def test_temperature_order_and_range_are_enforced(self):
        temperatures = dict(base_request()["temperatures"])
        temperatures["min_module_operating_c"] = 70.0
        result = self.prepare(base_request(temperatures=temperatures))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("temperatures", issues_of(result))

    def test_reference_temperature_outside_band_warns_only(self):
        temperatures = dict(base_request()["temperatures"])
        temperatures["min_module_operating_c"] = 20.0
        temperatures["max_module_operating_c"] = 20.0
        result = self.prepare(base_request(temperatures=temperatures))
        self.assertEqual(result["status"], "ready", result["issues"])
        self.assertIn("temperatures", warnings_of(result))

    def test_missing_declared_reference_field_blocks(self):
        module = electrical_inventory("PAN", PAN_TEXT.replace("  Voc=49.600\n", ""))
        result = prepare_design_envelope(module, self.inverter, base_request())
        self.assertEqual(result["status"], "blocked")
        self.assertIn("module.Voc", issues_of(result))

    def test_selected_voltage_standard_without_module_limit_blocks(self):
        module = electrical_inventory(
            "PAN", PAN_TEXT.replace("  VMaxIEC=1500\n", "").replace("  VMaxUL=1000\n", ""))
        result = prepare_design_envelope(module, self.inverter, base_request())
        self.assertEqual(result["status"], "blocked")
        self.assertIn("module.VMaxIEC", issues_of(result))

    def test_direct_current_limits_do_not_require_irradiance(self):
        request = base_request()
        del request["effective_irradiance"]
        result = self.prepare(request)
        self.assertEqual(result["status"], "ready", result["issues"])
        self.assertEqual(result["capacity"]["module_max_short_circuit_current_a"], 12.6)
        self.assertEqual(result["capacity"]["module_max_operating_current_a"], 12.0)

    def test_coefficient_current_route_requires_explicit_irradiance(self):
        request = base_request()
        del request["module_limits"]["max_short_circuit_current_a"]
        del request["effective_irradiance"]["max_irradiance_w_m2"]
        result = self.prepare(request)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("effective_irradiance.max_irradiance_w_m2", issues_of(result))

    def test_coefficient_current_route_requires_explicit_rear_gain(self):
        request = base_request()
        del request["module_limits"]["max_short_circuit_current_a"]
        del request["effective_irradiance"]["rear_gain_factor"]
        result = self.prepare(request)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("effective_irradiance.rear_gain_factor", issues_of(result))

    def test_temperature_maximum_follows_the_coefficient_sign(self):
        cold = base_request()
        del cold["module_limits"]["max_short_circuit_current_a"]
        cold["coefficients"]["alpha_isc"] = {"value": -0.05, "unit": "pct_per_degC", "source": "test only"}
        result = self.prepare(cold)
        self.assertEqual(result["status"], "ready", result["issues"])
        self.assertAlmostEqual(result["capacity"]["module_max_short_circuit_current_a"],
                               11.05 * 1.1 * (1 - 0.0005 * (20 - 25)), places=9)

        hot = base_request()
        del hot["module_limits"]["max_operating_current_a"]
        hot["coefficients"]["alpha_imp"] = {"value": 0.02, "unit": "pct_per_degC", "source": "test only"}
        result = self.prepare(hot)
        self.assertEqual(result["status"], "ready", result["issues"])
        self.assertAlmostEqual(result["capacity"]["module_max_operating_current_a"],
                               10.45 * 1.1 * (1 + 0.0002 * (60 - 25)), places=9)

        cold_imp = base_request()
        del cold_imp["module_limits"]["max_operating_current_a"]
        cold_imp["coefficients"]["alpha_imp"] = {"value": -0.03, "unit": "pct_per_degC", "source": "test only"}
        result = self.prepare(cold_imp)
        self.assertEqual(result["status"], "ready", result["issues"])
        self.assertAlmostEqual(result["capacity"]["module_max_operating_current_a"],
                               10.45 * 1.1 * (1 - 0.0003 * (20 - 25)), places=9)

    def test_unsourced_direct_envelope_values_warn(self):
        request = base_request(coefficients=None,
                               envelope={"cold_voc_v": 56.0, "cold_vmp_v": 45.0, "hot_vmp_v": 38.0})
        result = self.prepare(request)
        self.assertIn("envelope.cold_voc_v.source", warnings_of(result))

    def test_current_limit_unit_must_be_amps(self):
        request = base_request()
        request["mppt_limits"]["max_operating_current_a"] = {"value": 40000, "unit": "mA", "source": "datasheet"}
        result = self.prepare(request)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("mppt_limits.max_operating_current_a.unit", issues_of(result))

    def test_envelope_unit_must_be_volts(self):
        request = base_request(coefficients=None,
                               envelope={"cold_voc_v": {"value": 65, "unit": "kV", "source": "datasheet"},
                                         "cold_vmp_v": 45.0, "hot_vmp_v": 38.0})
        result = self.prepare(request)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("envelope.cold_voc_v.unit", issues_of(result))

    def test_explicit_units_and_bare_values_are_accepted(self):
        # An omitted unit follows the field meaning (A for limits, V for the
        # envelope); an explicit unit must match it.
        request = base_request(coefficients=None,
                               envelope={"cold_voc_v": {"value": 56.0, "unit": "V", "source": "datasheet"},
                                         "cold_vmp_v": {"value": 45.0, "source": "datasheet"},
                                         "hot_vmp_v": 38.0},
                               mppt_limits={"max_operating_current_a": {"value": 32.0, "unit": "A", "source": "datasheet"},
                                            "max_short_circuit_current_a": {"value": 40.0, "source": "datasheet"}})
        result = self.prepare(request)
        self.assertEqual(result["status"], "ready", result["issues"])
        self.assertEqual(result["capacity"]["cold_voc_v"], 56.0)
        self.assertEqual(result["capacity"]["cold_vmp_v"], 45.0)
        self.assertEqual(result["capacity"]["mppt_max_short_circuit_current_a"], 40.0)

    def test_declaration_unknown_keys_block_on_both_routes(self):
        limit = base_request()
        limit["module_limits"]["max_short_circuit_current_a"] = {"value": 12.6, "source": "datasheet", "converted": True}
        result = self.prepare(limit)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("module_limits.max_short_circuit_current_a.converted", issues_of(result))

        coefficient = base_request()
        coefficient["coefficients"]["beta_voc"] = {"value": -0.25, "unit": "pct_per_degC", "source": "ds", "note": "y"}
        result = self.prepare(coefficient)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("coefficients.beta_voc.note", issues_of(result))

    def test_unknown_request_keys_are_reported(self):
        request = base_request()
        request["cold_voc_v"] = 56.0
        result = self.prepare(request)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("cold_voc_v", issues_of(result))


if __name__ == "__main__":
    unittest.main()

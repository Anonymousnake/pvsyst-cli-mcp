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

    def test_coefficient_mode_builds_covered_capacity_and_schema(self):
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
        allowed = set(CAPACITY_NUMBERS) | {"capacity_tolerance_pct", "voltage_standard", "design_basis"}
        self.assertEqual(set(capacity), allowed)
        for name in CAPACITY_NUMBERS:
            self.assertIsInstance(capacity[name], (int, float))

    def test_explicit_envelope_overrides_coefficients_and_accepts_units_or_bare_values(self):
        bare = self.prepare(base_request(
            coefficients=None, envelope={"cold_voc_v": 56.0, "cold_vmp_v": 45.0, "hot_vmp_v": 38.0}))
        self.assertEqual(bare["status"], "ready", bare["issues"])
        self.assertEqual(bare["envelope_mode"], "explicit")
        self.assertEqual((bare["capacity"]["cold_voc_v"], bare["capacity"]["cold_vmp_v"],
                          bare["capacity"]["hot_vmp_v"]), (56.0, 45.0, 38.0))
        self.assertIn("envelope.cold_voc_v.source", warnings_of(bare))

        explicit = self.prepare(base_request(
            coefficients=None,
            envelope={"cold_voc_v": {"value": 56.0, "unit": "V", "source": "datasheet"},
                      "cold_vmp_v": {"value": 45.0, "source": "datasheet"},
                      "hot_vmp_v": 38.0},
            mppt_limits={"max_operating_current_a": {"value": 32.0, "unit": "A", "source": "datasheet"},
                         "max_short_circuit_current_a": {"value": 40.0, "source": "datasheet"}}))
        self.assertEqual(explicit["status"], "ready", explicit["issues"])
        self.assertEqual((explicit["capacity"]["cold_voc_v"], explicit["capacity"]["cold_vmp_v"],
                          explicit["capacity"]["mppt_max_short_circuit_current_a"]), (56.0, 45.0, 40.0))

    def test_missing_voltage_data_blocks_and_lists_both_fields(self):
        result = self.prepare(base_request(coefficients=None, envelope=None))
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["capacity"])
        fields = issues_of(result)
        self.assertIn("coefficients.beta_voc", fields)
        self.assertIn("coefficients.beta_vmp", fields)

    def test_coefficient_validation_errors_block(self):
        cases = (
            ("positive_beta_voc", "beta_voc", {"value": 0.25, "unit": "pct_per_degC", "source": "typo"},
             "coefficients.beta_voc"),
            ("unknown_unit", "beta_voc", {"value": -0.25, "unit": "percent", "source": "ambiguous"},
             "coefficients.beta_voc.unit"),
            ("missing_source", "beta_vmp", {"value": -0.30, "unit": "pct_per_degC"},
             "coefficients.beta_vmp.source"),
        )
        for label, key, value, field in cases:
            with self.subTest(case=label):
                request = base_request()
                request["coefficients"][key] = value
                result = self.prepare(request)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(field, issues_of(result))

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

    def test_direct_current_limits_do_not_require_irradiance(self):
        request = base_request()
        del request["effective_irradiance"]
        result = self.prepare(request)
        self.assertEqual(result["status"], "ready", result["issues"])
        self.assertEqual(result["capacity"]["module_max_short_circuit_current_a"], 12.6)
        self.assertEqual(result["capacity"]["module_max_operating_current_a"], 12.0)

    def test_coefficient_current_route_requires_irradiance_and_rear_gain(self):
        for key in ("max_irradiance_w_m2", "rear_gain_factor"):
            with self.subTest(missing=key):
                request = base_request()
                del request["module_limits"]["max_short_circuit_current_a"]
                del request["effective_irradiance"][key]
                result = self.prepare(request)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(f"effective_irradiance.{key}", issues_of(result))

    def test_temperature_maximum_follows_the_coefficient_sign(self):
        cases = (
            ("cold_isc", "max_short_circuit_current_a", "alpha_isc", -0.05,
             "module_max_short_circuit_current_a", 11.05 * 1.1 * (1 - 0.0005 * (20 - 25))),
            ("hot_imp", "max_operating_current_a", "alpha_imp", 0.02,
             "module_max_operating_current_a", 10.45 * 1.1 * (1 + 0.0002 * (60 - 25))),
            ("cold_imp", "max_operating_current_a", "alpha_imp", -0.03,
             "module_max_operating_current_a", 10.45 * 1.1 * (1 - 0.0003 * (20 - 25))),
        )
        for label, limit_key, coeff_key, coeff_val, cap_key, expected in cases:
            with self.subTest(case=label):
                request = base_request()
                del request["module_limits"][limit_key]
                request["coefficients"][coeff_key] = {"value": coeff_val, "unit": "pct_per_degC", "source": "test only"}
                result = self.prepare(request)
                self.assertEqual(result["status"], "ready", result["issues"])
                self.assertAlmostEqual(result["capacity"][cap_key], expected, places=9)

    def test_temperature_band_and_ambient_extreme_warnings(self):
        req_vmp = base_request()
        req_vmp["temperatures"] = dict(req_vmp["temperatures"], min_module_operating_c=30.0)
        res_vmp = self.prepare(req_vmp)
        self.assertEqual(res_vmp["status"], "ready", res_vmp["issues"])
        self.assertLess(res_vmp["capacity"]["cold_vmp_v"], 42.1)
        self.assertIn("temperatures", warnings_of(res_vmp))
        self.assertTrue(any("Vmp coverage check" in warning["message"] for warning in res_vmp["warnings"]))

        for label, updates, expected_warning in (
                ("annual_equal_design", {"annual_min_ambient_c": -10.0}, "temperatures.annual_min_ambient_c"),
                ("operating_equals_ambient", {"min_module_operating_c": -10.0}, "temperatures.min_module_operating_c"),
                ("reference_outside_band", {"min_module_operating_c": 20.0, "max_module_operating_c": 20.0},
                 "temperatures")):
            with self.subTest(case=label):
                temperatures = dict(base_request()["temperatures"], **updates)
                result = self.prepare(base_request(temperatures=temperatures))
                self.assertEqual(result["status"], "ready", result["issues"])
                self.assertIn(expected_warning, warnings_of(result))

    def test_temperature_order_and_missing_design_extreme_block(self):
        missing_design = dict(base_request()["temperatures"])
        del missing_design["min_ambient_c"]
        missing_design["annual_min_ambient_c"] = -5.0
        bad_order = dict(base_request()["temperatures"], min_module_operating_c=70.0)
        for label, temperatures, field in (("annual_not_design", missing_design, "temperatures.min_ambient_c"),
                                           ("min_above_max", bad_order, "temperatures")):
            with self.subTest(case=label):
                result = self.prepare(base_request(temperatures=temperatures))
                self.assertEqual(result["status"], "blocked")
                self.assertIn(field, issues_of(result))

    def test_mppt_limits_cannot_come_from_a_converter_current_field(self):
        positive = electrical_inventory("OND", OND_TEXT.replace("IDCMax=0.000", "IDCMax=150.000"))
        self.assertEqual(positive["fields"]["IDCMax"]["status"], "declared")
        result = prepare_design_envelope(self.module, positive, base_request(mppt_limits=None))
        self.assertEqual(result["status"], "blocked")
        fields = issues_of(result)
        self.assertIn("mppt_limits.max_operating_current_a", fields)
        self.assertIn("mppt_limits.max_short_circuit_current_a", fields)
        self.assertTrue(all("IDCMax" in issue["message"] for issue in result["issues"]))

    def test_current_limit_consistency_bounds_block(self):
        for label, section, key, value in (
                ("isc_below_operating", "mppt_limits", "max_short_circuit_current_a", 30.0),
                ("operating_below_imp", "module_limits", "max_operating_current_a", 9.0)):
            with self.subTest(case=label):
                request = base_request()
                request[section][key] = {"value": value, "source": "datasheet"}
                result = self.prepare(request)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(f"{section}.{key}", issues_of(result))

    def test_missing_declared_reference_field_and_voltage_standard_block(self):
        cases = (
            ("missing_voc", PAN_TEXT.replace("  Voc=49.600\n", ""), "module.Voc"),
            ("missing_vmax", PAN_TEXT.replace("  VMaxIEC=1500\n", "").replace("  VMaxUL=1000\n", ""),
             "module.VMaxIEC"),
        )
        for label, pan_text, field in cases:
            with self.subTest(case=label):
                module = electrical_inventory("PAN", pan_text)
                result = prepare_design_envelope(module, self.inverter, base_request())
                self.assertEqual(result["status"], "blocked")
                self.assertIn(field, issues_of(result))

    def test_limit_and_envelope_units_are_strict(self):
        bad_limit = base_request()
        bad_limit["mppt_limits"]["max_operating_current_a"] = {"value": 40000, "unit": "mA", "source": "datasheet"}
        bad_envelope = base_request(
            coefficients=None,
            envelope={"cold_voc_v": {"value": 65, "unit": "kV", "source": "datasheet"},
                      "cold_vmp_v": 45.0, "hot_vmp_v": 38.0})
        for label, request, field in (("limit_mA", bad_limit, "mppt_limits.max_operating_current_a.unit"),
                                      ("envelope_kV", bad_envelope, "envelope.cold_voc_v.unit")):
            with self.subTest(case=label):
                result = self.prepare(request)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(field, issues_of(result))

    def test_unknown_declaration_and_request_keys_block(self):
        bad_limit = base_request()
        bad_limit["module_limits"]["max_short_circuit_current_a"] = {
            "value": 12.6, "source": "datasheet", "converted": True}
        bad_coeff = base_request()
        bad_coeff["coefficients"]["beta_voc"] = {
            "value": -0.25, "unit": "pct_per_degC", "source": "ds", "note": "y"}
        bad_top = base_request(cold_voc_v=56.0)
        for label, request, field in (
                ("limit_extra", bad_limit, "module_limits.max_short_circuit_current_a.converted"),
                ("coeff_extra", bad_coeff, "coefficients.beta_voc.note"),
                ("top_level_extra", bad_top, "cold_voc_v")):
            with self.subTest(case=label):
                result = self.prepare(request)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(field, issues_of(result))


if __name__ == "__main__":
    unittest.main()

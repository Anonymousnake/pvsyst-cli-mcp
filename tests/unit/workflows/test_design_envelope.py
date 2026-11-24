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

    def test_coefficient_and_explicit_envelope_modes(self):
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

    def test_current_derivation_and_temperature_sign_routing(self):
        # 1. Imp without alpha_imp requires direct module_limits.max_operating_current_a
        req_imp = base_request()
        del req_imp["module_limits"]["max_operating_current_a"]
        res_imp = self.prepare(req_imp)
        self.assertEqual(res_imp["status"], "blocked")
        self.assertIn("module_limits.max_operating_current_a", issues_of(res_imp))

        # 2. alpha_isc derives short-circuit current while preserving direct operating limit
        req_isc = base_request()
        del req_isc["module_limits"]["max_short_circuit_current_a"]
        res_isc = self.prepare(req_isc)
        self.assertEqual(res_isc["status"], "ready", res_isc["issues"])
        expected_isc = 11.05 * (1100.0 / 1000.0) * (1 + 0.00045 * 35)
        self.assertAlmostEqual(res_isc["capacity"]["module_max_short_circuit_current_a"], expected_isc, places=9)
        self.assertEqual(res_isc["capacity"]["module_max_operating_current_a"], 12.0)

        # 3. Direct current limits do not require effective_irradiance
        req_direct = base_request()
        del req_direct["effective_irradiance"]
        res_direct = self.prepare(req_direct)
        self.assertEqual(res_direct["status"], "ready", res_direct["issues"])
        self.assertEqual(res_direct["capacity"]["module_max_short_circuit_current_a"], 12.6)
        self.assertEqual(res_direct["capacity"]["module_max_operating_current_a"], 12.0)

        # 4. Coefficient current route requires max_irradiance_w_m2 and rear_gain_factor
        for key in ("max_irradiance_w_m2", "rear_gain_factor"):
            with self.subTest(missing=key):
                req_irr = base_request()
                del req_irr["module_limits"]["max_short_circuit_current_a"]
                del req_irr["effective_irradiance"][key]
                res_irr = self.prepare(req_irr)
                self.assertEqual(res_irr["status"], "blocked")
                self.assertIn(f"effective_irradiance.{key}", issues_of(res_irr))

        # 5. Temperature maximum follows the coefficient sign
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

    def test_temperature_warnings_and_order_guards(self):
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

    def test_missing_fields_units_consistency_and_unknown_key_guards(self):
        # 1. Missing both coefficients and envelope lists both fields
        missing_v = self.prepare(base_request(coefficients=None, envelope=None))
        self.assertEqual(missing_v["status"], "blocked")
        self.assertIsNone(missing_v["capacity"])
        self.assertIn("coefficients.beta_voc", issues_of(missing_v))
        self.assertIn("coefficients.beta_vmp", issues_of(missing_v))

        # 2. Coefficient validation errors block
        for label, key, value, field in (
                ("positive_beta_voc", "beta_voc", {"value": 0.25, "unit": "pct_per_degC", "source": "typo"},
                 "coefficients.beta_voc"),
                ("unknown_unit", "beta_voc", {"value": -0.25, "unit": "percent", "source": "ambiguous"},
                 "coefficients.beta_voc.unit"),
                ("missing_source", "beta_vmp", {"value": -0.30, "unit": "pct_per_degC"},
                 "coefficients.beta_vmp.source")):
            with self.subTest(case=label):
                request = base_request()
                request["coefficients"][key] = value
                result = self.prepare(request)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(field, issues_of(result))

        # 3. MPPT limits cannot come from IDCMax converter field
        positive = electrical_inventory("OND", OND_TEXT.replace("IDCMax=0.000", "IDCMax=150.000"))
        self.assertEqual(positive["fields"]["IDCMax"]["status"], "declared")
        res_idc = prepare_design_envelope(self.module, positive, base_request(mppt_limits=None))
        self.assertEqual(res_idc["status"], "blocked")
        self.assertIn("mppt_limits.max_operating_current_a", issues_of(res_idc))
        self.assertIn("mppt_limits.max_short_circuit_current_a", issues_of(res_idc))
        self.assertTrue(all("IDCMax" in issue["message"] for issue in res_idc["issues"]))

        # 4. Current limit consistency bounds
        for label, section, key, value in (
                ("isc_below_operating", "mppt_limits", "max_short_circuit_current_a", 30.0),
                ("operating_below_imp", "module_limits", "max_operating_current_a", 9.0)):
            with self.subTest(case=label):
                request = base_request()
                request[section][key] = {"value": value, "source": "datasheet"}
                result = self.prepare(request)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(f"{section}.{key}", issues_of(result))

        # 5. Missing declared reference field / voltage standard
        for label, pan_text, field in (
                ("missing_voc", PAN_TEXT.replace("  Voc=49.600\n", ""), "module.Voc"),
                ("missing_vmax", PAN_TEXT.replace("  VMaxIEC=1500\n", "").replace("  VMaxUL=1000\n", ""),
                 "module.VMaxIEC")):
            with self.subTest(case=label):
                module = electrical_inventory("PAN", pan_text)
                result = prepare_design_envelope(module, self.inverter, base_request())
                self.assertEqual(result["status"], "blocked")
                self.assertIn(field, issues_of(result))

        # 6. Strict limit/envelope units & unknown keys block
        bad_limit = base_request()
        bad_limit["mppt_limits"]["max_operating_current_a"] = {"value": 40000, "unit": "mA", "source": "datasheet"}
        bad_envelope = base_request(
            coefficients=None,
            envelope={"cold_voc_v": {"value": 65, "unit": "kV", "source": "datasheet"},
                      "cold_vmp_v": 45.0, "hot_vmp_v": 38.0})
        bad_extra_limit = base_request()
        bad_extra_limit["module_limits"]["max_short_circuit_current_a"] = {
            "value": 12.6, "source": "datasheet", "converted": True}
        bad_extra_coeff = base_request()
        bad_extra_coeff["coefficients"]["beta_voc"] = {
            "value": -0.25, "unit": "pct_per_degC", "source": "ds", "note": "y"}
        bad_top = base_request(cold_voc_v=56.0)
        for label, request, field in (
                ("limit_mA", bad_limit, "mppt_limits.max_operating_current_a.unit"),
                ("envelope_kV", bad_envelope, "envelope.cold_voc_v.unit"),
                ("limit_extra", bad_extra_limit, "module_limits.max_short_circuit_current_a.converted"),
                ("coeff_extra", bad_extra_coeff, "coefficients.beta_voc.note"),
                ("top_level_extra", bad_top, "cold_voc_v")):
            with self.subTest(case=label):
                result = self.prepare(request)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(field, issues_of(result))


if __name__ == "__main__":
    unittest.main()

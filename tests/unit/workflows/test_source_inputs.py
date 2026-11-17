"""Source-binding tests: exact citation consistency, no fetching and no guessing.

Tables drive the public behaviour. Component text and source text are inline,
so no file, hash, SDK or network access happens here.
"""
import unittest

from pvsyst_auto.components.store import electrical_inventory
from pvsyst_auto.engineering.envelope import prepare_design_envelope
from pvsyst_auto.workflows.sourced_design import prepare_sourced_design


PAN_TEXT = """PVObject_=pvModule
  Comment=Generic\tMono 440W half-cell\tUser device
  Version=8.1.6
  Flags=$00

  PVObject_Commercial=pvCommercial
    Manufacturer=Generic
    Model=Mono 440W half-cell
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
  Comment=Solis S6-EH3P50K-H\t50 kW three-phase hybrid inverter\tUser device
  Version=8.1.6
  Flags=$0570

  PVObject_Commercial=pvCommercial
    Manufacturer=Solis
    Model=S6-EH3P50K-H
  End of PVObject pvCommercial

  Converter=TConverter
    PNomConv=50.000
    PMaxOUT=50.000
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

IDENTITY = "Generic Mono 440W half-cell / Solis S6-EH3P50K-H"

MODULE_DOC = """Generic Mono 440W half-cell module datasheet (declared extract)
Models covered: Generic Mono 440W half-cell, Generic Mono 450W half-cell
Maximum operating current: 14.5 A
Maximum short circuit current: 16.0 A
Maximum series fuse rating: 20 A
Maximum system voltage: 1500 VDC
Module operating temperature range: -40 °C
"""

COEFFICIENT_DOC = """Generic Mono 440W half-cell module datasheet (declared extract)
Temperature Coefficients of Voc: -0.25%/℃
Temperature Coefficients of Vmp: -0.30%/℃
Temperature Coefficients of Isc: 0.046%/℃
"""

ENVELOPE_DOC = """Generic Mono 440W half-cell module envelope (declared extract)
Cold Voc envelope at minimum temperature: 56.0 V
Cold Vmp envelope at minimum operating temperature: 45.0 V
Hot Vmp envelope at maximum operating temperature: 38.0 V
Cold Voc envelope in kV units: 56.0 kV
Cold Voc envelope exponent form: 1e16 A
Cold Voc envelope identifier form: x16 A
Cold Voc envelope typographic minus form: \u221216 A
"""

SITE_DOC = """Wanaluwawa site design conditions (declared extract)
Applicable module: Generic Mono 440W half-cell
Design minimum ambient temperature: -10 °C
Minimum module operating temperature: 20 °C
Maximum module operating temperature: 60 °C
Maximum effective irradiance: 1100 W/m²
Rear gain factor: 1 ratio
Rear gain factor note: 1 %
Target DC power: 21.6 kW
"""

INVERTER_DOC = """Solis S6-EH3P50K-H inverter datasheet (declared extract)
Max. input current per MPPT: 40 A
Max. short circuit current per MPPT: 60 A
"""


def sources_fixture():
    return {
        "S1": {"kind": "manufacturer", "title": "Generic module datasheet",
               "locator": "workspace://docs/generic-440w.txt", "text": MODULE_DOC,
               "model": "Generic Mono 440W half-cell"},
        "S2": {"kind": "site", "title": "Wanaluwawa site conditions",
               "locator": "workspace://sites/wanaluwawa.txt", "text": SITE_DOC,
               "model": "Generic Mono 440W half-cell"},
        "S3": {"kind": "manufacturer", "title": "Generic envelope extract",
               "locator": "workspace://docs/generic-440w-envelope.txt", "text": ENVELOPE_DOC,
               "model": "Generic Mono 440W half-cell"},
        "S4": {"kind": "manufacturer", "title": "Solis datasheet",
               "locator": "https://example.invalid/solis-s6-eh3p.txt", "text": INVERTER_DOC,
               "model": "S6-EH3P50K-H"},
        "S5": {"kind": "manufacturer", "title": "Generic coefficient extract",
               "locator": "workspace://docs/generic-440w-coefficients.txt", "text": COEFFICIENT_DOC,
               "model": "Generic Mono 440W half-cell"},
    }


def declaration(field, value, unit, source_id, excerpt, value_text, unit_text, page=None):
    record = {"field": field, "value": value, "unit": unit, "source_id": source_id,
              "excerpt": excerpt, "source_value_text": value_text, "source_unit_text": unit_text}
    if page is not None:
        record["page"] = page
    return record


def declarations_fixture():
    return [
        declaration("temperatures.min_ambient_c", -10.0, "degC", "S2",
                    "Design minimum ambient temperature: -10 °C", "-10", "°C", page=4),
        declaration("temperatures.min_module_operating_c", 20.0, "degC", "S2",
                    "Minimum module operating temperature: 20 °C", "20", "°C"),
        declaration("temperatures.max_module_operating_c", 60.0, "degC", "S2",
                    "Maximum module operating temperature: 60 °C", "60", "°C"),
        declaration("envelope.cold_voc_v", 56.0, "V", "S3",
                    "Cold Voc envelope at minimum temperature: 56.0 V", "56.0", "V", page=7),
        declaration("envelope.cold_vmp_v", 45.0, "V", "S3",
                    "Cold Vmp envelope at minimum operating temperature: 45.0 V", "45.0", "V"),
        declaration("envelope.hot_vmp_v", 38.0, "V", "S3",
                    "Hot Vmp envelope at maximum operating temperature: 38.0 V", "38.0", "V"),
        declaration("module_limits.max_operating_current_a", 14.5, "A", "S1",
                    "Maximum operating current: 14.5 A", "14.5", "A"),
        declaration("module_limits.max_short_circuit_current_a", 16.0, "A", "S1",
                    "Maximum short circuit current: 16.0 A", "16.0", "A"),
        declaration("mppt_limits.max_operating_current_a", 40.0, "A", "S4",
                    "Max. input current per MPPT: 40 A", "40", "A", page=2),
        declaration("mppt_limits.max_short_circuit_current_a", 60.0, "A", "S4",
                    "Max. short circuit current per MPPT: 60 A", "60", "A"),
        declaration("effective_irradiance.max_irradiance_w_m2", 1100.0, "W/m2", "S2",
                    "Maximum effective irradiance: 1100 W/m²", "1100", "W/m²"),
        declaration("effective_irradiance.rear_gain_factor", 1.0, "ratio", "S2",
                    "Rear gain factor: 1 ratio", "1", "ratio"),
    ]


def coefficient_declarations():
    records = [record for record in declarations_fixture() if not record["field"].startswith("envelope.")]
    records.extend([
        declaration("coefficients.beta_voc", -0.25, "pct_per_degC", "S5",
                    "Temperature Coefficients of Voc: -0.25%/℃", "-0.25", "%/℃"),
        declaration("coefficients.beta_vmp", -0.30, "pct_per_degC", "S5",
                    "Temperature Coefficients of Vmp: -0.30%/℃", "-0.30", "%/℃"),
    ])
    return records


def design_fixture():
    return {"target_dc_kw": 21.6, "max_dc_ac_ratio": 1.3, "capacity_tolerance_pct": 5,
            "voltage_standard": "IEC"}


def issues_of(result):
    return [issue["field"] for issue in result["issues"]]


class SourceInputTests(unittest.TestCase):
    def setUp(self):
        self.module = electrical_inventory("PAN", PAN_TEXT)
        self.inverter = electrical_inventory("OND", OND_TEXT)

    def call(self, sources=None, declarations=None, design=None, identity=IDENTITY):
        return prepare_sourced_design(
            self.module, self.inverter, manufacturer_model=identity,
            sources=sources_fixture() if sources is None else sources,
            declarations=declarations_fixture() if declarations is None else declarations,
            design=design_fixture() if design is None else design)

    def test_complete_direct_envelope_is_ready_and_matches_plain_envelope(self):
        result = self.call()
        self.assertEqual(result["status"], "ready", result["issues"])
        design = result["electrical_design"]
        self.assertEqual(design["temperatures"]["min_ambient_c"], -10.0)
        self.assertEqual(design["envelope"]["cold_voc_v"]["unit"], "V")
        self.assertEqual(design["mppt_limits"]["max_short_circuit_current_a"]["value"], 60.0)
        self.assertEqual(design["effective_irradiance"]["max_irradiance_w_m2"], 1100.0)
        self.assertIn("S2", design["temperatures"]["min_ambient_basis"])

        plain = prepare_design_envelope(
            self.module, self.inverter,
            {"manufacturer_model": IDENTITY, "target_dc_kw": 21.6, "max_dc_ac_ratio": 1.3,
             "capacity_tolerance_pct": 5, "voltage_standard": "IEC",
             "temperatures": {"min_ambient_c": -10.0, "min_module_operating_c": 20.0,
                              "max_module_operating_c": 60.0, "min_ambient_basis": "x" * 10},
             "envelope": {"cold_voc_v": {"value": 56.0, "unit": "V", "source": "S3"},
                          "cold_vmp_v": {"value": 45.0, "unit": "V", "source": "S3"},
                          "hot_vmp_v": {"value": 38.0, "unit": "V", "source": "S3"}},
             "module_limits": {"max_operating_current_a": {"value": 14.5, "source": "S1"},
                               "max_short_circuit_current_a": {"value": 16.0, "source": "S1"}},
             "mppt_limits": {"max_operating_current_a": {"value": 40.0, "source": "S4"},
                             "max_short_circuit_current_a": {"value": 60.0, "source": "S4"}}})
        self.assertEqual(plain["status"], "ready", plain["issues"])
        for key in ("cold_voc_v", "cold_vmp_v", "hot_vmp_v", "module_max_operating_current_a",
                    "mppt_max_short_circuit_current_a"):
            self.assertEqual(result["capacity"][key], plain["capacity"][key])

    def test_cited_coefficients_and_compact_quote_formats_are_accepted(self):
        base = self.call(declarations=coefficient_declarations())
        self.assertEqual(base["status"], "ready", base["issues"])
        self.assertEqual(base["electrical_design"]["coefficients"]["beta_voc"]["unit"], "pct_per_degC")
        self.assertAlmostEqual(base["capacity"]["cold_voc_v"], 49.6 * (1 + 0.0025 * 35), places=9)

        for label, leading_only in (("no_space_quote", False), ("quote_starts_at_number", True)):
            with self.subTest(case=label):
                records = coefficient_declarations()
                records.append(declaration("coefficients.alpha_isc", 0.046, "pct_per_degC", "S5",
                                           "Temperature Coefficients of Isc: 0.046%/℃", "0.046", "%/℃"))
                if leading_only:
                    for record in records:
                        if record["field"].startswith("coefficients."):
                            record["excerpt"] = record["excerpt"].split(": ")[-1]
                result = self.call(declarations=records)
                self.assertEqual(result["status"], "ready", result["issues"])
                coefficients = result["electrical_design"]["coefficients"]
                self.assertEqual((coefficients["beta_voc"]["value"], coefficients["beta_voc"]["unit"],
                                  coefficients["alpha_isc"]["value"]), (-0.25, "pct_per_degC", 0.046))
                self.assertEqual(result["electrical_design"]["mppt_limits"]["max_operating_current_a"]["unit"], "A")
                quoted = [item for item in result["provenance"] if item["field"].startswith("coefficients.")]
                self.assertEqual(len(quoted), 3)
                self.assertTrue(all(item["reference_match"] for item in quoted))

    def test_provenance_records_shape_and_normalized_excerpt(self):
        result = self.call()
        keys = {"field", "sourceID", "title", "locator", "model", "page", "excerpt", "value", "unit",
                "source_value_text", "source_unit_text", "source_text_head", "kind", "reference_match"}
        self.assertTrue(result["provenance"])
        for record in result["provenance"]:
            self.assertEqual(set(record), keys)
            self.assertLessEqual(len(record["source_text_head"]), 160)
            self.assertNotIn("\n", record["excerpt"])
        pages = [record["page"] for record in result["provenance"] if record["page"] is not None]
        self.assertIn(4, pages)

    def test_excerpt_must_be_a_contiguous_fragment(self):
        cases = {"absent": "Cold Voc envelope published elsewhere: 56.0 V",
                 "split": "56.0 V Cold Voc envelope at minimum temperature"}
        for label, excerpt in cases.items():
            records = declarations_fixture()
            records[3]["excerpt"] = excerpt
            result = self.call(declarations=records)
            with self.subTest(case=label):
                self.assertEqual(result["status"], "blocked")
                self.assertIn("envelope.cold_voc_v", issues_of(result))
                self.assertFalse([record for record in result["provenance"]
                                  if record["field"] == "envelope.cold_voc_v"][0]["reference_match"])

    def test_tokens_and_units_cannot_be_silently_changed(self):
        cases = (
            ("value_not_equal", "envelope.cold_voc_v", 56.0, "V",
             "Cold Voc envelope at minimum temperature: 560 V", "56.0", "V"),
            ("token_inside_decimal", "envelope.cold_voc_v", 16.0, "V",
             "Cold Voc envelope at minimum temperature: 16.0 V", "16", "V"),
            ("token_inside_larger_integer", "envelope.cold_voc_v", 16.0, "V",
             "Cold Voc envelope at minimum temperature: 160 V", "16", "V"),
            ("token_after_sign", "temperatures.min_ambient_c", 40.0, "degC",
             "Module operating temperature range: -40 °C", "40", "°C"),
            ("unit_from_milliamp", "mppt_limits.max_operating_current_a", 40.0, "A",
             "Max. input current per MPPT: 40 mA", "A", "A"),
            ("unit_from_kilovolt", "envelope.cold_voc_v", 56.0, "V",
             "Cold Voc envelope in kV units: 56.0 kV", "V", "V"),
            ("unit_inside_word", "envelope.cold_voc_v", 1500.0, "V",
             "Cold Voc envelope at minimum temperature: 1500 VDC", "1500", "V"),
            ("token_inside_exponent_literal", "envelope.cold_voc_v", 16.0, "V",
             "Cold Voc envelope exponent form: 1e16 A", "16", "A"),
            ("token_inside_identifier", "envelope.cold_voc_v", 16.0, "V",
             "Cold Voc envelope identifier form: x16 A", "16", "A"),
            ("token_after_typographic_minus", "envelope.cold_voc_v", 16.0, "V",
             "Cold Voc envelope typographic minus form: \u221216 A", "16", "A"),
            ("empty_ratio_unit_token", "effective_irradiance.rear_gain_factor", 1.0, "ratio",
             "Rear gain factor: 1 ratio", "1", ""),
        )
        for label, field, value, declared_unit, excerpt, value_token, unit_token in cases:
            records = declarations_fixture()
            target = [record for record in records if record["field"] == field][0]
            target.update(value=value, unit=declared_unit, excerpt=excerpt,
                          source_value_text=value_token, source_unit_text=unit_token)
            result = self.call(declarations=records)
            with self.subTest(case=label):
                self.assertEqual(result["status"], "blocked")
                self.assertIn(field, issues_of(result))

    def test_unit_alias_is_explicit_and_never_rescaled(self):
        cases = (
            ("percent_per_degree", "%/℃", "pct_per_degC", "coefficients.beta_voc", None),
            ("milliamp_not_amp", "mA", "A", "mppt_limits.max_operating_current_a", "Max. input current per MPPT: 40 mA"),
            ("kilovolt_not_volt", "kV", "V", "envelope.cold_voc_v", "Cold Voc envelope in kV units: 56.0 kV"),
            ("percent_not_ratio", "%", "ratio", "effective_irradiance.rear_gain_factor", "Rear gain factor note: 1 %"),
        )
        for label, unit_token, declared_unit, field, excerpt in cases:
            records = coefficient_declarations() if field == "coefficients.beta_voc" else declarations_fixture()
            target = [record for record in records if record["field"] == field][0]
            target["source_unit_text"] = unit_token
            if excerpt is not None:
                target["excerpt"] = excerpt
            result = self.call(declarations=records)
            with self.subTest(case=label):
                if label == "percent_per_degree":
                    self.assertEqual(result["status"], "ready", result["issues"])
                else:
                    self.assertEqual(result["status"], "blocked")
                    self.assertIn(field, issues_of(result))

    def test_duplicate_missing_source_and_unknown_field_are_refused(self):
        duplicate = declarations_fixture()
        duplicate.append(dict(duplicate[3]))
        missing = declarations_fixture()
        missing[3]["source_id"] = "S9"
        unknown = declarations_fixture()
        unknown[3]["field"] = "envelope.cold_voc"
        for label, records in (("duplicate", duplicate), ("missing_source", missing), ("unknown_field", unknown)):
            result = self.call(declarations=records)
            with self.subTest(case=label):
                self.assertEqual(result["status"], "blocked")
                self.assertTrue(result["issues"])

    def test_malformed_backend_inputs_block_without_raising(self):
        mixed = dict(design_fixture())
        mixed[1] = "x"
        cases = {
            "design_string": {"design": dict(design_fixture(), target_dc_kw="bad")},
            "design_null": {"design": dict(design_fixture(), target_dc_kw=None)},
            "design_huge_int": {"design": dict(design_fixture(), target_dc_kw=10 ** 400)},
            "design_mixed_keys": {"design": mixed},
            "authority_text_list": {"override_text": ["not", "text"]},
            "identity_none": {"identity": None},
            "identity_huge": {"identity": "M" * 201},
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                if "override_text" in kwargs:
                    sources = sources_fixture()
                    sources["S1"]["text"] = kwargs["override_text"]
                    result = self.call(sources=sources)
                elif "identity" in kwargs:
                    result = self.call(identity=kwargs["identity"])
                else:
                    result = self.call(design=kwargs["design"])
                self.assertEqual(result["status"], "blocked")
                self.assertTrue(result["issues"])
                if "design" in kwargs and isinstance(kwargs["design"], dict):
                    self.assertNotEqual(result["electrical_design"].get("target_dc_kw"), "bad")

    def test_schema_value_types_ids_and_control_chars_are_rejected(self):
        cases = {}
        for label, idx, key, val in (("declaration_unknown_key", 3, "extra", 1),
                                     ("non_positive_page", 3, "page", 0),
                                     ("boolean_value", 3, "value", True),
                                     ("non_finite_value", 3, "value", float("inf"))):
            records = declarations_fixture()
            records[idx][key] = val
            cases[label] = ({"declarations": records}, "")
        for label, sid, key, val, msg in (
                ("unknown_source_key", "S1", "note", "x", ""),
                ("unknown_source_kind", "S1", "kind", "vendor", ""),
                ("locator_too_long", "S2", "locator", "L" * 2001, ""),
                ("control_chars", "S1", "text", MODULE_DOC.replace("Maximum operating", "Maximum\x00operating"),
                 "control characters")):
            sources = sources_fixture()
            sources[sid][key] = val
            cases[label] = ({"sources": sources}, msg)
        for bad in ("bad id", "A" * 81, "id\nnewline"):
            sources = sources_fixture()
            sources[bad] = dict(sources["S1"])
            cases[f"bad_id_{repr(bad)[:10]}"] = ({"sources": sources}, "short id")
        cases["design_injection"] = ({"design": dict(design_fixture(), envelope={"cold_voc_v": 56.0})}, "")
        for label, (kwargs, needle) in cases.items():
            result = self.call(**kwargs)
            with self.subTest(case=label):
                self.assertEqual(result["status"], "blocked")
                self.assertTrue(result["issues"])
                if needle:
                    self.assertTrue(any(needle in issue["message"] for issue in result["issues"]))

    def test_size_and_text_budgets_are_enforced(self):
        big_text = sources_fixture()
        big_text["S1"]["text"] = MODULE_DOC + "x" * (2 * 1024 * 1024)
        many_sources = sources_fixture()
        for index in range(12):
            many_sources["X" + str(index)] = dict(many_sources["S1"])
        long_excerpt = declarations_fixture()
        long_excerpt[3]["excerpt"] = "L" * 4001
        for label, kwargs, needle in (
                ("text_budget", {"sources": big_text}, "exceeds"),
                ("too_many_sources", {"sources": many_sources}, None),
                ("long_excerpt", {"declarations": long_excerpt}, None),
                ("too_many_declarations", {"declarations": declarations_fixture() * 3}, None)):
            with self.subTest(case=label):
                result = self.call(**kwargs)
                self.assertEqual(result["status"], "blocked")
                if needle:
                    self.assertTrue(any(needle in issue["message"] for issue in result["issues"]))
                elif label == "too_many_sources":
                    self.assertIn("sources", issues_of(result))

    def test_missing_inputs_return_a_draft_instead_of_raising(self):
        records = [record for record in declarations_fixture()
                   if record["field"].startswith(("mppt_limits", "module_limits"))]
        result = self.call(declarations=records)
        self.assertEqual(result["status"], "blocked")
        self.assertIsNotNone(result["electrical_design"])
        self.assertIn("mppt_limits", result["electrical_design"])
        self.assertIn("temperatures.min_ambient_c", result["missing_fields"])
        self.assertIn("coefficients.beta_vmp", result["missing_fields"])

    def test_review_conditions_set_needs_review(self):
        assumption_sources = sources_fixture()
        assumption_sources["S2"]["kind"] = "assumption"
        unscoped_sources = sources_fixture()
        del unscoped_sources["S3"]["model"]
        mfg_ambient = declarations_fixture()
        target = [record for record in mfg_ambient if record["field"] == "temperatures.min_ambient_c"][0]
        target.update(source_id="S1", excerpt="Module operating temperature range: -40 °C",
                      source_value_text="-40", source_unit_text="°C", value=-40.0)

        r1 = self.call(sources=assumption_sources)
        self.assertEqual(r1["status"], "needs_review", r1["issues"])
        self.assertTrue(r1["needs_review"])
        self.assertEqual([r for r in r1["provenance"] if r["field"] == "temperatures.min_ambient_c"][0]["kind"],
                         "assumption")

        r2 = self.call(sources=unscoped_sources)
        self.assertEqual(r2["status"], "needs_review", r2["issues"])
        self.assertTrue(r2["needs_review"])
        self.assertIsNotNone(r2["electrical_design"])
        self.assertTrue(any("applicability" in warning["message"] for warning in r2["warnings"]))

        r3 = self.call(declarations=mfg_ambient)
        self.assertEqual(r3["status"], "needs_review", r3["issues"])
        self.assertTrue(r3["needs_review"])
        self.assertEqual(r3["electrical_design"]["temperatures"]["min_ambient_c"], -40.0)
        self.assertTrue([w for w in r3["warnings"] if w["field"] == "temperatures.min_ambient_c"])
        self.assertIn("S1 Generic module datasheet", r3["electrical_design"]["temperatures"]["min_ambient_basis"])

    def test_model_applicability_mismatch_and_token_boundaries(self):
        self.assertEqual(self.call()["status"], "ready")
        cases = (
            ("unmatched_declared_model", "S3", {"model": "Generic Mono 450W half-cell"},
             "envelope.cold_voc_v", None),
            ("module_side_mismatch", "S3", {
                "model": "Generic Mono 450W half-cell",
                "text": ENVELOPE_DOC.replace("Generic Mono 440W half-cell module envelope",
                                             "Generic Mono 450W half-cell module envelope")},
             None, "selected identity"),
            ("inverter_side_mismatch", "S4", {
                "model": "S6-EH3P60K-H",
                "text": INVERTER_DOC.replace("S6-EH3P50K-H", "S6-EH3P60K-H")},
             "mppt_limits.max_operating_current_a", "selected identity"),
            ("partial_token_50K-H", "S4", {"model": "50K-H"}, None, "exact model token"),
            ("partial_token_S6", "S4", {"model": "S6"}, None, "exact model token"),
        )
        for label, sid, updates, expected_field, expected_msg in cases:
            with self.subTest(case=label):
                sources = sources_fixture()
                sources[sid].update(updates)
                result = self.call(sources=sources)
                self.assertEqual(result["status"], "blocked")
                if expected_field:
                    self.assertIn(expected_field, issues_of(result))
                if expected_msg:
                    self.assertTrue(any(expected_msg in issue["message"] or "selected identity" in issue["message"]
                                        for issue in result["issues"]))

    def test_cited_engineering_goal_and_conflict_with_design(self):
        records = declarations_fixture()
        records.append(declaration("target_dc_kw", 21.6, "kW", "S2",
                                   "Target DC power: 21.6 kW", "21.6", "kW"))
        conflict = self.call(declarations=records)
        self.assertEqual(conflict["status"], "blocked")
        self.assertIn("target_dc_kw", issues_of(conflict))

        design = design_fixture()
        del design["target_dc_kw"]
        ready = self.call(declarations=records, design=design)
        self.assertEqual(ready["status"], "ready", ready["issues"])
        self.assertEqual(ready["electrical_design"]["target_dc_kw"], 21.6)


if __name__ == "__main__":
    unittest.main()

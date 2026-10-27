"""External contract tests for the unified single-project design flow.

Every public dependency of ``pvsyst_design_flow`` is stubbed at its boundary: no component or
variant parse, no hashing or fingerprinting, no provider request, no native CLI and no real
weather/CSV inspection. The tests assert only public arguments, the phase/status contract and
the absence of side effects - never a digest.
"""
import copy
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pvsyst_auto.workflows.design as flow
from pvsyst_auto.native.cli import PVsystError
from pvsyst_auto.engineering.sizing import NUMBERS as CAPACITY_NUMBERS


def capacity(**overrides):
    data = {"target_dc_kw": 100.0, "max_dc_ac_ratio": 1.2, "voltage_standard": "IEC",
            "design_basis": "declared datasheet envelope", "capacity_tolerance_pct": 5.0,
            "cold_voc_v": 50.0, "cold_vmp_v": 40.0, "hot_vmp_v": 30.0,
            "module_max_operating_current_a": 12.0, "module_max_short_circuit_current_a": 13.0,
            "mppt_max_operating_current_a": 20.0, "mppt_max_short_circuit_current_a": 22.0,
            "design_min_temperature_c": -10.0, "design_min_operating_temperature_c": -5.0,
            "design_max_operating_temperature_c": 70.0}
    assert set(CAPACITY_NUMBERS) <= set(data)
    data.update(overrides)
    return data


def spec(**overrides):
    base = {"scope": "fixed_unshaded_grid", "new_project": "Plant.PRJ", "year": 2024,
            "target_dc_kw": 100.0, "tilt": 20.0, "azimuth": 0.0,
            "module": {"manufacturer": "Lab", "model": "440W"},
            "inverter": {"manufacturer": "Lab", "model": "50kW"},
            "weather": {"mode": "existing", "sit_name": "Plant.SIT", "met_name": "Plant.MET"},
            "capacity": capacity()}
    base.update(overrides)
    return base


def selected():
    return {"PAN": {"name": "module.PAN", "manufacturer": "Lab", "model": "440W",
                    "electrical": {"fields": {}}, "library": "workspace"},
            "OND": {"name": "inverter.OND", "manufacturer": "Lab", "model": "50kW",
                    "electrical": {"fields": {}}, "library": "workspace"},
            "template": {"project": "Template.PRJ", "variant": "VC0", "subarray_id": 1,
                         "orientation_id": 1, "capacity_plan": None,
                         "counts": {"NModSerie": 12, "NStringCh": 6, "NInvMPPT": 1, "NInverter": 2}}}


def discovery_ready(store, **kwargs):
    return {"status": "ready", "selected": copy.deepcopy(selected()), "issues": [], "warnings": []}


def simulation(status="completed", stage="completed", **extra):
    result = {"status": status, "stage": stage, "warnings": [], "issues": [], "artifacts": {},
              "metrics": None, "recovery": []}
    result.update(extra)
    return result


def components():
    """The PAN/OND block a discovery reports even when no template is eligible."""
    full = selected()
    return {"PAN": full["PAN"], "OND": full["OND"]}


def no_template(reason="Require exactly one subarray and one fixed plane"):
    return {"status": "blocked", "selected": None, "resolved_components": components(),
            "warnings": [], "issues": [{"field": "template Seedless/VC0", "message": reason}]}


def seeded_template(project="Plant_template.PRJ"):
    full = selected()
    full["template"] = {"project": project, "variant": "VC0", "subarray_id": 1, "orientation_id": 1,
                        "capacity_plan": None,
                        "counts": {"NModSerie": 12, "NStringCh": 6, "NInvMPPT": 1, "NInverter": 2}}
    return {"status": "ready", "selected": full, "resolved_components": components(),
            "issues": [], "warnings": []}


class FakeCLI:
    def __init__(self, workspace, failure=None):
        self.workspace = workspace
        self.failure = failure
        self.calls = []

    def _require(self, command, option):
        self.calls.append((command, option))
        if self.failure:
            raise PVsystError(self.failure)

    def command_help(self, command=""):
        return ""

    def version(self):
        return "8.1.6"

    def license_info(self):
        return {"status": "LICENSED", "remaining_days": 30}

    def _redact(self, text):
        return str(text)


class FakeStore:
    def __init__(self, root):
        self.lock = threading.RLock()
        self.root = root
        (root / "Projects").mkdir(parents=True, exist_ok=True)

    def _root(self):
        return self.root / "Projects"

    @staticmethod
    def _belongs_to_project(path, project):
        # Mirrors VariantStore: the exact project stem, never a longer dotted prefix.
        return path.stem.casefold() == Path(project).stem.casefold()


class DesignFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        # The verified PVsyst 8.1 workspace layout lives under its canonical root name.
        self.workspace = self.root / flow.EXPECTED_WORKSPACE_BASENAME
        self.workspace.mkdir()
        self.store = FakeStore(self.workspace)
        self.cli = FakeCLI(self.workspace)

    def snapshot(self):
        return sorted(str(path.relative_to(self.workspace)) for path in self.workspace.rglob("*"))

    def run_flow(self, spec_value, dry_run=True, *, discovery=None, envelope=None, download=None,
                 prepare=None, simulate=None, seed=None, cli=None):
        # simulate_template is always stubbed: this file never reaches the native CLI.
        patches = {"discover_design_inputs": discovery if discovery is not None else discovery_ready,
                   "simulate_template": simulate if simulate is not None
                   else simulation(status="ready", stage="preflight")}
        for name, value in (("prepare_design_envelope", envelope), ("download_weather", download),
                            ("prepare_weather", prepare), ("prepare_standard_template", seed)):
            if value is not None:
                patches[name] = value
        mocks = {}
        for name, value in patches.items():
            entry = (patch.object(flow, name, side_effect=value) if callable(value)
                     else patch.object(flow, name, return_value=value))
            mocks[name] = entry.start()
            self.addCleanup(entry.stop)
        result = flow.simulate_design(cli or self.cli, self.store, spec=spec_value, dry_run=dry_run)
        return result, mocks

    def test_scope_is_the_only_accepted_installation_scope(self):
        for scope in ("roof", "free_standing"):
            result, _ = self.run_flow(spec(scope=scope))
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["stage"], "spec")
            message = next(item["message"] for item in result["issues"] if item["field"] == "scope")
            self.assertIn("fixed_unshaded_grid", message)
        # an unset scope (a typed server sends its default as an empty string) is the supported scope
        unset, _ = self.run_flow(spec(scope=""))
        self.assertEqual((unset["status"], unset["stage"]), ("ready", "preflight"), unset["issues"])

    def test_missing_design_choice_blocks_without_side_effects(self):
        before = self.snapshot()
        result, mocks = self.run_flow(spec(capacity=None))
        self.assertEqual((result["status"], result["stage"]), ("blocked", "spec"))
        self.assertIn("capacity", [item["field"] for item in result["issues"]])
        self.assertEqual(mocks["discover_design_inputs"].call_count, 0)
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_both_capacity_and_electrical_design_are_rejected(self):
        result, _ = self.run_flow(spec(electrical_design={"temperatures": {}}))
        self.assertEqual((result["status"], result["stage"]), ("blocked", "spec"))
        message = next(item["message"] for item in result["issues"]
                       if item["field"] == "electrical_design")
        self.assertIn("mutually exclusive", message)

    def test_strict_numbers_and_unknown_keys_are_rejected(self):
        for broken, field in ((spec(extra=1), "extra"),
                              (spec(tilt=True), "tilt"),
                              (spec(target_dc_kw=float("nan")), "target_dc_kw"),
                              (spec(azimuth=200.0), "azimuth"),
                              (spec(year=True), "year"),
                              (spec(losses={"LIDLoss": True}), "losses.LIDLoss"),
                              (spec(losses={"LIDLoss": 1.0, "Other": 2.0}), "losses.Other"),
                              (spec(module={"file_name": "sub/mod.PAN"}), "module.file_name"),
                              (spec(weather={"mode": "guess"}), "weather.mode"),
                              (spec(weather={"mode": "csv", "csv_name": "a.csv", "country_code": "CN"}),
                               "weather.time_basis"),
                              (spec(weather={"mode": "csv", "csv_name": "a.csv", "country_code": "cn",
                                             "time_basis": flow.TIME_BASIS}), "weather.country_code"),
                              (spec(weather={"mode": "open_meteo", "location_id": 1, "latitude": 1.0,
                                             "country_code": "CN"}), "weather.location_id"),
                              (spec(weather={"mode": "open_meteo", "country_code": "CN"}), "weather.location_id"),
                              (spec(weather={"mode": "open_meteo", "location_id": 1816670,
                                             "country_code": "CN", "temperature_method": "mean"}),
                               "weather.temperature_method"),
                              (spec(capacity=capacity(target_dc_kw=90.0)), "capacity.target_dc_kw")):
            result, _ = self.run_flow(broken)
            self.assertEqual((result["status"], result["stage"]), ("blocked", "spec"), field)
            self.assertIn(field, [item["field"] for item in result["issues"]])

    def test_direct_capacity_is_confirmed_against_template_hardware(self):
        plan = {"status": "ready", "inverter_count": 2, "steps": [], "capacity_error_pct": 0.0}
        calls = []

        def discovery(store, **kwargs):
            calls.append(kwargs)
            return {"status": "ready", "selected": copy.deepcopy(selected()), "issues": [],
                    "warnings": [], "capacity_plan": plan}

        result, mocks = self.run_flow(spec(weather={"mode": "csv", "csv_name": "stage.csv",
                                                    "country_code": "CN",
                                                    "time_basis": flow.TIME_BASIS}),
                                      discovery=discovery,
                                      prepare=lambda cli, **kwargs: {"status": "ready", "warnings": [],
                                                                     "issues": [], "artifacts": {},
                                                                     "created_files": {}})
        self.assertEqual((result["status"], result["stage"]), ("ready_for_weather", "planned"))
        self.assertFalse(result["execution_preflight_complete"])
        self.assertEqual([item["capacity"] for item in calls], [None, spec()["capacity"]])
        self.assertEqual(calls[1]["capacity"]["target_dc_kw"], 100.0)
        self.assertEqual(mocks["prepare_weather"].call_args.kwargs["csv_name"], "stage.csv")
        self.assertEqual(mocks["prepare_weather"].call_args.kwargs["dry_run"], True)
        self.assertEqual(mocks["simulate_template"].call_count, 0)

    def test_declared_design_is_injected_and_envelope_warnings_are_inherited(self):
        envelope_calls = []

        def envelope(module_electrical, inverter_electrical, request):
            envelope_calls.append(request)
            return {"status": "ready", "capacity": capacity(), "issues": [],
                    "warnings": [{"field": "module.GRef", "message": "not 1000 W/m2"}],
                    "provenance": {}, "envelope_mode": "coefficients"}

        plan = {"status": "ready", "inverter_count": 2, "steps": [], "capacity_error_pct": 0.0}
        calls = []

        def discovery(store, **kwargs):
            calls.append(kwargs)
            return {"status": "ready", "selected": copy.deepcopy(selected()), "issues": [],
                    "warnings": [], "capacity_plan": plan}

        simulate = simulation(status="ready", stage="preflight", artifacts={})
        result, mocks = self.run_flow(
            spec(capacity=None, electrical_design={"temperatures": {"min_ambient_c": -10.0}}),
            discovery=discovery, envelope=envelope, simulate=simulate)
        self.assertEqual((result["status"], result["stage"]), ("ready", "preflight"))
        self.assertTrue(result["execution_preflight_complete"])
        self.assertEqual(envelope_calls[0]["manufacturer_model"], "Lab 440W / Lab 50kW")
        self.assertEqual(envelope_calls[0]["target_dc_kw"], 100.0)
        self.assertEqual(calls[1]["capacity"], capacity())
        self.assertIn("module.GRef: not 1000 W/m2", result["warnings"])
        self.assertTrue(any("source binding, not a manufacturer certification" in warning
                            for warning in result["warnings"]))
        forwarded = mocks["simulate_template"].call_args.kwargs
        self.assertEqual(forwarded["azimuth"], 0.0)
        self.assertEqual(forwarded["tilt"], 20.0)
        self.assertEqual(forwarded["year"], 2024)
        self.assertEqual(forwarded["pan_name"], "module.PAN")
        self.assertEqual(forwarded["ond_name"], "inverter.OND")
        self.assertEqual(forwarded["capacity"], capacity())

    def test_declared_identity_label_must_equal_the_selected_identity(self):
        def envelope(module_electrical, inverter_electrical, request):
            raise AssertionError("envelope is not built for a contradictory identity label")

        result, mocks = self.run_flow(
            spec(capacity=None, electrical_design={"manufacturer_model": "Other 1 / Lab 50kW"}),
            envelope=envelope)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "design_envelope"))
        message = next(item["message"] for item in result["issues"]
                       if item["field"] == "electrical_design.manufacturer_model")
        self.assertIn("Lab 440W / Lab 50kW", message)
        self.assertEqual(mocks["discover_design_inputs"].call_count, 1)

    def test_ambiguous_equipment_blocks_discovery_without_downstream_calls(self):
        ambiguous = {"status": "blocked", "selected": None, "warnings": [],
                     "issues": [{"field": "PAN.name",
                                 "message": "2 usable workspace files share this identity"}]}
        result, mocks = self.run_flow(spec(), discovery=lambda store, **kwargs: ambiguous,
                                      prepare=lambda *a, **k: None,
                                      simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "discovery"))
        self.assertIsNone(result["selected"])
        self.assertIn("2 usable workspace files share this identity",
                      [item["message"] for item in result["issues"]])
        self.assertEqual(mocks["prepare_weather"].call_count, 0)
        self.assertEqual(mocks["simulate_template"].call_count, 0)

    def test_open_meteo_execution_inherits_acquisition_review_required(self):
        plan = {"status": "ready", "standard_utc_offset_hours": 8,
                "review_required": ["hourly Tamb uses the endpoint_mean approximation"],
                "artifacts": {"csv": "p.csv"}, "warnings": [], "issues": []}
        prepared = {"status": "prepared", "created_files": {"csv": {"name": "Plant_openmeteo.csv"}},
                    "review_required": ["hourly Tamb uses the endpoint_mean approximation"],
                    "artifacts": {"csv": "Plant_openmeteo.csv"}, "warnings": [], "issues": []}
        prepare = {"status": "prepared", "warnings": [], "issues": [],
                   "artifacts": {"manifest": str(self.workspace / "Results" / "Plant_weather.json")}}
        simulate = simulation(artifacts={"csv": "annual.csv"}, metrics={"PR": 80.0})

        result, mocks = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            dry_run=False, download=lambda workspace, **kwargs: (plan if kwargs["dry_run"] else prepared),
            prepare=lambda cli, **kwargs: prepare, simulate=simulate)
        self.assertEqual(result["status"], "needs_review", result)
        self.assertEqual(result["stage"], "completed")
        self.assertTrue(result["execution_preflight_complete"])
        self.assertTrue(any("endpoint_mean approximation" in warning for warning in result["warnings"]))
        forwarded = mocks["simulate_template"].call_args.kwargs
        self.assertEqual(forwarded["site_name"], "Plant.SIT")
        self.assertEqual(forwarded["met_name"], "Plant.MET")
        self.assertEqual(forwarded["weather_manifest_name"], "Plant_weather.json")
        self.assertEqual(mocks["download_weather"].call_count, 2)
        self.assertEqual([entry.kwargs["dry_run"] for entry in mocks["download_weather"].call_args_list],
                         [True, False])

    def test_offset_above_create_site_limit_blocks_before_any_write(self):
        before = self.snapshot()
        plan = {"status": "ready", "standard_utc_offset_hours": 13, "artifacts": {"csv": "p.csv"},
                "warnings": [], "issues": []}
        result, mocks = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "NZ"}),
            dry_run=False, download=lambda workspace, **kwargs: plan,
            prepare=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "acquisition"))
        message = next(item["message"] for item in result["issues"] if item["field"] == "weather.location")
        self.assertIn("create-site", message)
        self.assertEqual(mocks["download_weather"].call_count, 1)
        self.assertEqual(mocks["prepare_weather"].call_count, 0)
        self.assertEqual(self.snapshot(), before)

    def test_native_preflight_failure_stops_before_weather_and_discovery(self):
        cli = FakeCLI(self.workspace, failure="Installed PVsystCLI lacks run-simulation --time-step")
        before = self.snapshot()
        result, mocks = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            dry_run=False, cli=cli, download=lambda *a, **k: None, prepare=lambda *a, **k: None,
            simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "native_preflight"))
        self.assertEqual(mocks["discover_design_inputs"].call_count, 0)
        self.assertEqual(mocks["download_weather"].call_count, 0)
        self.assertEqual(self.snapshot(), before)

    def test_downstream_failure_keeps_produced_artifacts_and_recovery(self):
        prepare = {"status": "prepared", "warnings": [], "issues": [],
                   "artifacts": {"manifest": str(self.workspace / "Results" / "Plant_weather.json")}}
        recovery = [{"tool": "pvsyst_restore_variant", "arguments": {"project": "Plant.PRJ"}}]
        simulate = simulation(status="failed", stage="simulation", recovery=recovery,
                              artifacts={"csv": str(self.workspace / "Results" / "annual.csv")},
                              error="counts do not match the accepted capacity plan")
        result, _ = self.run_flow(
            spec(weather={"mode": "csv", "csv_name": "stage.csv", "country_code": "CN",
                          "time_basis": flow.TIME_BASIS}),
            dry_run=False, prepare=lambda cli, **kwargs: prepare, simulate=simulate)
        self.assertEqual((result["status"], result["stage"]), ("failed", "simulation"))
        self.assertEqual(result["recovery"], recovery)
        self.assertIn("Plant_weather.json", result["artifacts"]["prepare"]["manifest"])
        self.assertTrue(any("no automatic cleanup" in warning for warning in result["warnings"]))

    def test_existing_weather_without_manifest_discloses_the_skipped_comparison(self):
        simulate = simulation(status="ready", stage="preflight")
        result, mocks = self.run_flow(spec(), simulate=simulate)
        self.assertEqual((result["status"], result["stage"]), ("ready", "preflight"))
        self.assertEqual(mocks["simulate_template"].call_args.kwargs["weather_manifest_name"], "")
        self.assertTrue(any("source-weather comparison is skipped" in warning
                            for warning in result["warnings"]))

    def test_existing_output_names_are_preflighted_before_any_phase(self):
        conflict = self.workspace / "Results" / "Plant_annual_2024.csv"
        conflict.parent.mkdir(parents=True, exist_ok=True)
        conflict.write_text("", encoding="utf-8")
        result, mocks = self.run_flow(spec(), simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "outputs"))
        self.assertIn("annual_csv", [item["field"] for item in result["issues"]])
        self.assertEqual(mocks["discover_design_inputs"].call_count, 0)
        self.assertEqual(self.cli.calls, [])

    def test_raised_prepare_error_returns_failed_stage_without_simulation(self):
        def prepare(cli, **kwargs):
            raise PVsystError("convert-meteo failed with exit code 3",
                              diagnostics="Meteo conversion refused the normalized input")

        result, mocks = self.run_flow(
            spec(weather={"mode": "csv", "csv_name": "stage.csv", "country_code": "CN",
                          "time_basis": flow.TIME_BASIS}),
            dry_run=False, prepare=prepare, simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("failed", "weather_prepare"))
        self.assertTrue(any("conversion refused" in item["message"] for item in result["issues"]))
        self.assertEqual(mocks["simulate_template"].call_count, 0)
        self.assertIn("annual_csv", result["artifacts"]["planned"])

    def test_raised_acquisition_error_keeps_the_planned_provider_targets(self):
        def download(workspace, **kwargs):
            if kwargs["dry_run"]:
                return {"status": "ready", "standard_utc_offset_hours": 8,
                        "artifacts": {"csv": "Plant_openmeteo.csv", "raw_json": "raw.json",
                                      "manifest": "acq.json"}, "warnings": [], "issues": []}
            raise RuntimeError("provider response exceeds the raw storage limit")

        result, mocks = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            dry_run=False, download=download, prepare=lambda *a, **k: None,
            simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("failed", "acquisition"))
        self.assertEqual(result["artifacts"]["acquire"]["raw_json"], "raw.json")
        self.assertEqual(mocks["prepare_weather"].call_count, 0)
        self.assertTrue(any("never reported as success" in warning for warning in result["warnings"])
                        or result["recovery"] == [])

    def test_open_meteo_dry_run_plans_without_inspecting_a_missing_csv(self):
        plan = {"status": "ready", "standard_utc_offset_hours": 8,
                "location": {"country_code": "CN", "name": "Beijing"},
                "review_required": ["hourly Tamb uses the endpoint_mean approximation"],
                "artifacts": {"csv": "Plant_openmeteo.csv"}, "warnings": [], "issues": []}

        def prepare(*args, **kwargs):
            raise AssertionError("prepare_weather must not run when no CSV exists yet")

        result, mocks = self.run_flow(spec(weather={"mode": "open_meteo", "location_id": 1816670}),
                                      download=lambda workspace, **kwargs: plan, prepare=prepare,
                                      simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("ready_for_weather", "planned"))
        self.assertFalse(result["execution_preflight_complete"])
        self.assertEqual(mocks["download_weather"].call_count, 1)
        self.assertEqual(mocks["prepare_weather"].call_count, 0)
        self.assertEqual(mocks["simulate_template"].call_count, 0)
        self.assertTrue(any("planned, not performed" in warning for warning in result["warnings"]))

    def test_acquisition_plan_that_is_not_ready_stops_before_the_second_request(self):
        plan = {"status": "blocked", "warnings": [], "artifacts": {},
                "issues": ["the place has no ISO country code or IANA timezone"]}
        result, mocks = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670}), dry_run=False,
            download=lambda workspace, **kwargs: plan, prepare=lambda *a, **k: None,
            simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "acquisition"))
        self.assertEqual(mocks["download_weather"].call_count, 1)
        self.assertEqual(mocks["prepare_weather"].call_count, 0)
        self.assertTrue(any("no ISO country code" in item["message"] for item in result["issues"]))

    def test_location_id_supplies_the_country_code_for_preparation(self):
        def download(workspace, **kwargs):
            if kwargs["dry_run"]:
                return {"status": "ready", "standard_utc_offset_hours": 8,
                        "location": {"country_code": "CN"}, "warnings": [], "issues": []}
            return {"status": "prepared", "created_files": {"csv": {"name": "Plant_openmeteo.csv"}},
                    "location": {"country_code": "CN"}, "artifacts": {"csv": "Plant_openmeteo.csv"},
                    "warnings": [], "issues": []}

        prepare = {"status": "prepared", "warnings": [], "issues": [],
                   "artifacts": {"manifest": str(self.workspace / "Results" / "Plant_weather.json")}}
        result, mocks = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670}), dry_run=False,
            download=download, prepare=lambda cli, **kwargs: prepare,
            simulate=simulation(artifacts={}))
        self.assertEqual(result["status"], "completed", result["issues"])
        self.assertEqual(mocks["prepare_weather"].call_args.kwargs["country_code"], "CN")
        self.assertTrue(any("ISO code CN" in warning for warning in result["warnings"]))

    def test_typed_server_empty_strings_are_unset_while_numbers_still_reject(self):
        typed = spec(template_project="", variant="",
                     module={"file_name": "", "manufacturer": "Lab", "model": "440W"},
                     inverter={"file_name": "", "manufacturer": "Lab", "model": "50kW"},
                     weather={"mode": "existing", "sit_name": "Plant.SIT", "met_name": "Plant.MET",
                              "csv_name": "", "country_code": "", "time_basis": "",
                              "site_label": "", "weather_manifest_name": ""})
        result, _ = self.run_flow(typed)
        self.assertEqual((result["status"], result["stage"]), ("ready", "preflight"), result["issues"])
        self.assertEqual(result["issues"], [])
        for broken, field in ((spec(year=""), "year"), (spec(tilt=""), "tilt"),
                              (spec(azimuth=""), "azimuth"), (spec(target_dc_kw=""), "target_dc_kw"),
                              (spec(capacity=capacity(target_dc_kw="")), "capacity.target_dc_kw"),
                              (spec(module={"file_name": "", "manufacturer": ""}), "module.name")):
            broken_result, _ = self.run_flow(broken)
            self.assertEqual(broken_result["status"], "blocked", field)
            self.assertIn(field, [item["field"] for item in broken_result["issues"]])

    def test_component_drift_between_discovery_passes_blocks(self):
        passes = [selected(), selected()]
        passes[1]["PAN"]["electrical"] = {"fields": {"Voc": {"status": "declared", "value": 45.0}}}
        seen = []

        def discovery(store, **kwargs):
            seen.append(kwargs)
            return {"status": "ready", "selected": copy.deepcopy(passes[len(seen) - 1]),
                    "issues": [], "warnings": []}

        result, mocks = self.run_flow(spec(), discovery=discovery)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "discovery"))
        self.assertTrue(any("changed between the first discovery" in item["message"]
                            for item in result["issues"]))
        self.assertEqual(mocks["simulate_template"].call_count, 0)

    def test_alias_output_names_block_before_any_request(self):
        before = self.snapshot()
        result, mocks = self.run_flow(
            spec(weather={"mode": "open_meteo", "csv_name": "Plant_import.csv",
                          "location_id": 1816670, "country_code": "CN"}),
            download=lambda *a, **k: None, prepare=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "outputs"))
        self.assertTrue(any("resolve to the same file" in item["message"] for item in result["issues"]))
        self.assertEqual(mocks["download_weather"].call_count, 0)
        self.assertEqual(self.snapshot(), before)

    def test_failed_prepare_error_without_issues_is_surfaced(self):
        prepare = {"status": "failed", "issues": [], "warnings": [],
                   "error": "Native SIT differs from weather CSV #Longitude",
                   "artifacts": {"sit": str(self.workspace / "Sites" / "Plant.SIT")}}
        result, mocks = self.run_flow(
            spec(weather={"mode": "csv", "csv_name": "stage.csv", "country_code": "CN",
                          "time_basis": flow.TIME_BASIS}),
            dry_run=False, prepare=lambda cli, **kwargs: prepare, simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("failed", "weather_prepare"))
        self.assertIn("Native SIT differs from weather CSV #Longitude",
                      [item["message"] for item in result["issues"]])
        self.assertEqual(result["preview"]["prepare"]["error"],
                         "Native SIT differs from weather CSV #Longitude")
        self.assertEqual(mocks["simulate_template"].call_count, 0)

    def test_failed_simulation_error_is_surfaced_once(self):
        reason = "Configured counts do not match the accepted capacity plan"
        prepare = {"status": "prepared", "warnings": [], "issues": [],
                   "artifacts": {"manifest": str(self.workspace / "Results" / "Plant_weather.json")}}
        simulate = simulation(status="failed", stage="capacity", error=reason, issues=[reason],
                              artifacts={"csv": "annual.csv"})
        result, _ = self.run_flow(
            spec(weather={"mode": "csv", "csv_name": "stage.csv", "country_code": "CN",
                          "time_basis": flow.TIME_BASIS}),
            dry_run=False, prepare=lambda cli, **kwargs: prepare, simulate=simulate)
        self.assertEqual((result["status"], result["stage"]), ("failed", "capacity"))
        messages = [item["message"] for item in result["issues"]]
        self.assertEqual(messages.count(reason), 1)
        self.assertEqual(result["preview"]["simulation"]["error"], reason)
        self.assertTrue(any("no automatic cleanup" in warning for warning in result["warnings"]))

    def test_missing_template_falls_back_to_the_standard_seed(self):
        seed = {"status": "ready", "source": {"seed": "installed 8.1 residential demo"},
                "retained_defaults": ["losses", "thermal"], "changes": [], "warnings": [], "issues": []}
        result, mocks = self.run_flow(spec(), discovery=lambda store, **kw: no_template(),
                                      seed=lambda *a, **k: seed, simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("ready_for_weather", "planned"))
        self.assertFalse(result["execution_preflight_complete"])
        self.assertEqual(result["issues"], [])
        self.assertIsNone(result["selected"])
        self.assertTrue(any("Rejected template candidate" in warning for warning in result["warnings"]))
        self.assertTrue(any("falls back to the installed standard template" in warning
                            for warning in result["warnings"]))
        self.assertEqual(result["preview"]["template_seed"]["source"]["seed"],
                         "installed 8.1 residential demo")
        forwarded = mocks["prepare_standard_template"].call_args.kwargs
        self.assertEqual(forwarded["new_project"], "Plant_template.PRJ")
        self.assertTrue(forwarded["dry_run"])
        self.assertEqual(mocks["simulate_template"].call_count, 0)

    def test_seed_fallback_execution_creates_confirms_then_simulates(self):
        sequence = [no_template(), no_template(), seeded_template()]
        seen = []

        def discovery(store, **kwargs):
            seen.append(kwargs)
            return copy.deepcopy(sequence[len(seen) - 1])

        seed_calls = []

        def seed(cli, store, **kwargs):
            seed_calls.append(kwargs)
            if kwargs["dry_run"]:
                return {"status": "ready", "source": {"seed": "installed"}, "retained_defaults": [],
                        "warnings": [], "issues": []}
            return {"status": "created", "template_project": "Plant_template.PRJ", "variant": "VC0",
                    "files": {"Plant_template.PRJ": "a", "Plant_template.VC0": "b"},
                    "retained_defaults": [], "warnings": [], "issues": []}

        result, mocks = self.run_flow(spec(), dry_run=False, discovery=discovery,
                                      seed=seed, simulate=simulation(artifacts={}))
        self.assertEqual(result["status"], "completed", result["issues"])
        self.assertEqual([entry["dry_run"] for entry in seed_calls], [True, False])
        self.assertEqual(seen[2]["template_project"], "Plant_template.PRJ")
        self.assertEqual(seen[2]["variant"], "VC0")
        self.assertEqual(result["artifacts"]["seed"]["project"], "Plant_template.PRJ")
        forwarded = mocks["simulate_template"].call_args.kwargs
        self.assertEqual(forwarded["template_project"], "Plant_template.PRJ")
        self.assertEqual(forwarded["variant"], "VC0")
        self.assertEqual(forwarded["new_project"], "Plant.PRJ")

    def test_capacity_confirmed_template_cancels_the_seed_fallback(self):
        sequence = [no_template(), discovery_ready(None)]
        seen = []

        def discovery(store, **kwargs):
            seen.append(kwargs)
            return copy.deepcopy(sequence[len(seen) - 1])

        seed_calls = []

        def seed(cli, store, **kwargs):
            seed_calls.append(kwargs)
            return {"status": "created", "files": {}, "warnings": [], "issues": []}

        result, mocks = self.run_flow(spec(), dry_run=False, discovery=discovery,
                                      seed=seed, simulate=simulation(artifacts={}))
        self.assertEqual(result["status"], "completed", result["issues"])
        self.assertEqual(seed_calls, [])
        self.assertEqual(len(seen), 2)
        self.assertEqual(result["artifacts"]["seed"], {})
        self.assertTrue(any("seed fallback is not used" in warning for warning in result["warnings"]))
        forwarded = mocks["simulate_template"].call_args.kwargs
        self.assertEqual(forwarded["template_project"], "Template.PRJ")

    def test_provider_failure_does_not_write_a_seed(self):
        plan = {"status": "ready", "standard_utc_offset_hours": 8, "location": {"country_code": "CN"},
                "artifacts": {}, "warnings": [], "issues": []}

        def download(workspace, **kwargs):
            if kwargs["dry_run"]:
                return plan
            raise RuntimeError("provider request failed")

        seed_calls = []
        result, _ = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            dry_run=False, discovery=lambda store, **kw: no_template(),
            download=download, seed=lambda *a, **k: seed_calls.append(k) or {"status": "ready"},
            simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("failed", "acquisition"))
        self.assertEqual([entry["dry_run"] for entry in seed_calls], [True])
        self.assertEqual(result["artifacts"]["seed"], {})

    def test_seed_name_conflict_blocks_before_any_seed_preview(self):
        (self.workspace / "Projects" / "Plant_template.PRJ").write_bytes(b"")
        result, mocks = self.run_flow(spec(), discovery=lambda store, **kw: no_template(),
                                      seed=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "outputs"))
        self.assertEqual([item["field"] for item in result["issues"]], ["seed"])
        self.assertEqual(mocks["prepare_standard_template"].call_count, 0)

    def test_ambiguous_components_never_reach_the_seed_fallback(self):
        ambiguous = {"status": "blocked", "selected": None, "resolved_components": None, "warnings": [],
                     "issues": [{"field": "PAN.name", "message": "2 usable workspace files share this identity"}]}
        result, mocks = self.run_flow(spec(), discovery=lambda store, **kw: ambiguous,
                                      seed=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "discovery"))
        self.assertEqual(mocks["prepare_standard_template"].call_count, 0)
        self.assertEqual(mocks["simulate_template"].call_count, 0)

    def test_seed_confirmation_drift_keeps_the_written_seed_and_blocks(self):
        changed = seeded_template()
        changed["selected"]["PAN"]["name"] = "other.PAN"
        changed["selected"]["PAN"]["electrical"] = {"fields": {"Voc": {"status": "declared", "value": 45.0}}}
        sequence = [no_template(), no_template(), changed]
        seen = []

        def discovery(store, **kwargs):
            seen.append(kwargs)
            return copy.deepcopy(sequence[len(seen) - 1])

        def seed(cli, store, **kwargs):
            if kwargs["dry_run"]:
                return {"status": "ready", "retained_defaults": [], "warnings": [], "issues": []}
            return {"status": "created", "files": {"Plant_template.PRJ": "a"},
                    "retained_defaults": [], "warnings": [], "issues": []}

        result, mocks = self.run_flow(spec(), dry_run=False, discovery=discovery, seed=seed,
                                      simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "discovery"))
        self.assertEqual(result["artifacts"]["seed"]["project"], "Plant_template.PRJ")
        self.assertTrue(any("changed between" in item["message"] for item in result["issues"]))
        self.assertTrue(any("no automatic cleanup" in warning for warning in result["warnings"]))
        self.assertEqual(mocks["simulate_template"].call_count, 0)

    def test_explicit_template_without_a_match_never_falls_back(self):
        result, mocks = self.run_flow(spec(template_project="Mine.PRJ", variant="VC0"),
                                      discovery=lambda store, **kw: no_template(),
                                      seed=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "discovery"))
        self.assertEqual([item["field"] for item in result["issues"]], ["template Seedless/VC0"])
        self.assertEqual(mocks["prepare_standard_template"].call_count, 0)
        self.assertIsNone(result["preview"]["template_seed"])

    def test_absent_projects_folder_is_an_empty_workspace(self):
        (self.workspace / "Projects").rmdir()
        result, _ = self.run_flow(spec())
        self.assertEqual((result["status"], result["stage"]), ("ready", "preflight"), result["issues"])
        self.assertEqual(result["issues"], [])

    def test_seed_name_beyond_the_filename_limit_is_reported(self):
        long_name = "P" * 170 + ".PRJ"
        result, mocks = self.run_flow(spec(new_project=long_name),
                                      discovery=lambda store, **kw: no_template(),
                                      seed=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "outputs"))
        message = next(item["message"] for item in result["issues"] if item["field"] == "seed")
        self.assertIn("180-character project filename limit", message)
        self.assertIn("shorter new_project", message)
        self.assertEqual(mocks["prepare_standard_template"].call_count, 0)

    def test_seed_dry_run_never_creates_the_projects_folder(self):
        (self.workspace / "Projects").rmdir()
        result, mocks = self.run_flow(
            spec(), discovery=lambda store, **kw: no_template(),
            seed=lambda *a, **k: {"status": "ready", "retained_defaults": [], "warnings": [], "issues": []},
            simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("ready_for_weather", "planned"))
        self.assertFalse((self.workspace / "Projects").exists())
        self.assertTrue(mocks["prepare_standard_template"].call_args.kwargs["dry_run"])

    def test_seed_execution_creates_the_projects_folder_before_factory_create(self):
        (self.workspace / "Projects").rmdir()
        sequence = [no_template(), no_template(), seeded_template()]
        seen = []

        def discovery(store, **kwargs):
            seen.append(kwargs)
            return copy.deepcopy(sequence[len(seen) - 1])

        def seed(cli, store, **kwargs):
            if kwargs["dry_run"]:
                return {"status": "ready", "retained_defaults": [], "warnings": [], "issues": []}
            self.assertTrue((self.workspace / "Projects").is_dir(), "the folder exists before create")
            return {"status": "created", "files": {}, "retained_defaults": [], "warnings": [], "issues": []}

        result, mocks = self.run_flow(spec(), dry_run=False, discovery=discovery, seed=seed,
                                      simulate=simulation(artifacts={}))
        self.assertEqual(result["status"], "completed", result["issues"])
        self.assertTrue((self.workspace / "Projects").is_dir())
        self.assertEqual(mocks["simulate_template"].call_count, 1)

    def test_readonly_layout_reports_missing_folders_without_creating_them(self):
        (self.workspace / "Projects").rmdir()
        before = self.snapshot()
        result, _ = self.run_flow(spec())
        self.assertEqual((result["status"], result["stage"]), ("ready", "preflight"), result["issues"])
        layout = result["preview"]["workspace"]
        self.assertEqual(layout["basename"], flow.EXPECTED_WORKSPACE_BASENAME)
        self.assertIn("Models", layout["missing"])
        self.assertIn("ComposPV/Batteries", layout["missing"])
        self.assertEqual(layout["created_directories"], [])
        self.assertEqual(self.snapshot(), before)

    def test_layout_execution_creates_the_verified_folder_set(self):
        (self.workspace / "Projects").rmdir()
        result, _ = self.run_flow(spec(), dry_run=False, simulate=simulation(artifacts={}))
        self.assertEqual(result["status"], "completed", result["issues"])
        layout = result["preview"]["workspace"]
        self.assertEqual(len(layout["missing"]), 26)
        self.assertEqual(len(layout["created_directories"]), 26)
        labels = list(flow.LAYOUT_ROOT_FOLDERS) + [f"ComposPV/{name}"
                                                  for name in flow.LAYOUT_COMPOSPV_FOLDERS]
        for label in labels:
            self.assertTrue((self.workspace / label).is_dir(), label)

    def test_wrong_workspace_root_name_blocks_before_any_provider_request(self):
        wrong = self.root / "PVsyst8.1_Data_backup"
        wrong.mkdir()
        result, mocks = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            cli=FakeCLI(wrong), download=lambda *a, **k: None, prepare=lambda *a, **k: None,
            simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "workspace"))
        self.assertEqual([item["field"] for item in result["issues"]], ["workspace"])
        self.assertIn("PVsyst8.1_Data", result["issues"][0]["message"])
        self.assertEqual(mocks["download_weather"].call_count, 0)
        self.assertEqual(mocks["discover_design_inputs"].call_count, 0)
        self.assertTrue(wrong.is_dir())
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         ["PVsyst8.1_Data", "PVsyst8.1_Data_backup"])

    def test_existing_file_where_a_folder_belongs_blocks_before_any_provider_request(self):
        conflict = self.workspace / "Models"
        conflict.write_text("existing", encoding="utf-8")
        result, mocks = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            dry_run=False, download=lambda *a, **k: None, prepare=lambda *a, **k: None,
            simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "workspace"))
        self.assertEqual(conflict.read_text(encoding="utf-8"), "existing")
        self.assertIn("Models", result["preview"]["workspace"]["file_conflicts"])
        self.assertEqual(mocks["download_weather"].call_count, 0)
        self.assertEqual(mocks["simulate_template"].call_count, 0)

    def test_nested_escaping_link_blocks_before_any_provider_request(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "ComposPV").mkdir()
        link = self.workspace / "ComposPV" / "Batteries"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("directory symlinks are unavailable on this platform: " + str(exc))
        result, mocks = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            dry_run=False, download=lambda *a, **k: None, prepare=lambda *a, **k: None,
            simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("blocked", "workspace"))
        self.assertIn("ComposPV/Batteries", result["preview"]["workspace"]["escaped"])
        self.assertEqual(mocks["download_weather"].call_count, 0)
        self.assertEqual(result["preview"]["workspace"]["created_directories"], [])

    def test_provider_failure_creates_no_extra_workspace_folder(self):
        plan = {"status": "ready", "standard_utc_offset_hours": 8, "location": {"country_code": "CN"},
                "artifacts": {}, "warnings": [], "issues": []}

        def download(workspace, **kwargs):
            if kwargs["dry_run"]:
                return plan
            raise RuntimeError("provider request failed")

        before = self.snapshot()
        result, _ = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            dry_run=False, download=download, prepare=lambda *a, **k: None,
            simulate=lambda *a, **k: None)
        self.assertEqual((result["status"], result["stage"]), ("failed", "acquisition"))
        self.assertEqual(result["preview"]["workspace"]["created_directories"], [])
        self.assertEqual(self.snapshot(), before)

    def test_returned_contract_fields_are_always_present(self):
        result, _ = self.run_flow(spec())
        for key in ("status", "stage", "dry_run", "execution_preflight_complete", "issues",
                    "warnings", "selected", "preview", "metrics", "artifacts", "recovery"):
            self.assertIn(key, result)
        self.assertIn(result["status"], flow.STATUSES)


if __name__ == "__main__":
    unittest.main()

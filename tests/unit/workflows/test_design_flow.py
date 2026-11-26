"""Consolidated workflow tests for design flow, input discovery, envelope, source binding, and sizing."""
from __future__ import annotations
import copy
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import pvsyst_auto.workflows.design as flow
from pvsyst_auto.api.context import SERVER_INSTRUCTIONS, WORKFLOW_GUIDE_MD, _variables_guide_md
from pvsyst_auto.api.schemas import SingleDesignRequest
from pvsyst_auto.components.store import ComponentStore, electrical_inventory
from pvsyst_auto.engineering.review import SELECTION_RULE, discover_design_inputs
from pvsyst_auto.engineering.sizing import NUMBERS as CAPACITY_NUMBERS, prepare_design_envelope, suggest_sizing
from pvsyst_auto.native.cli import PVsystError
from pvsyst_auto.project.circuit import circuit_nodes, inventory as grid_inventory
from pvsyst_auto.project.template import SEED_VARIANT, prepare_standard_template, standard_seed_texts
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.workflows.design import EXPECTED_WORKSPACE_BASENAME, prepare_design_project, simulate_design
from pvsyst_auto.workflows.sourced_design import prepare_sourced_design
from tests._fixtures import FakeCliBase, init_workspace, write_utf8_crlf
def capacity(**overrides):
    data = {"target_dc_kw": 100.0, "max_dc_ac_ratio": 1.2, "voltage_standard": "IEC",
            "design_basis": "declared datasheet envelope", "capacity_tolerance_pct": 5.0, "cold_voc_v": 50.0, "cold_vmp_v": 40.0, "hot_vmp_v": 30.0,
            "module_max_operating_current_a": 12.0, "module_max_short_circuit_current_a": 13.0, "mppt_max_operating_current_a": 20.0, "mppt_max_short_circuit_current_a": 22.0,
            "design_min_temperature_c": -10.0, "design_min_operating_temperature_c": -5.0, "design_max_operating_temperature_c": 70.0}
    assert set(CAPACITY_NUMBERS) <= set(data)
    data.update(overrides)
    return data
def spec(**overrides):
    base = {"scope": "fixed_unshaded_grid", "new_project": "Plant.PRJ", "year": 2024, "target_dc_kw": 100.0, "tilt": 20.0, "azimuth": 0.0,
            "module": {"manufacturer": "Lab", "model": "440W"}, "inverter": {"manufacturer": "Lab", "model": "50kW"},
            "weather": {"mode": "existing", "sit_name": "Plant.SIT", "met_name": "Plant.MET"}, "capacity": capacity()}
    base.update(overrides)
    return base
def selected():
    return {"PAN": {"name": "module.PAN", "manufacturer": "Lab", "model": "440W", "electrical": {"fields": {}}, "library": "workspace"},
            "OND": {"name": "inverter.OND", "manufacturer": "Lab", "model": "50kW", "electrical": {"fields": {}}, "library": "workspace"},
            "template": {"project": "Template.PRJ", "variant": "VC0", "subarray_id": 1, "orientation_id": 1, "capacity_plan": None, "counts": {"NModSerie": 12, "NStringCh": 6, "NInvMPPT": 1, "NInverter": 2}}}
def discovery_ready(store, **kwargs):
    return {"status": "ready", "selected": copy.deepcopy(selected()), "issues": [], "warnings": []}
def simulation(status="completed", stage="completed", **extra):
    result = {"status": status, "stage": stage, "warnings": [], "issues": [], "artifacts": {}, "metrics": None, "recovery": []}
    result.update(extra)
    return result
def components():
    full = selected()
    return {"PAN": full["PAN"], "OND": full["OND"]}
def no_template(reason="Require exactly one subarray and one fixed plane"):
    return {"status": "blocked", "selected": None, "resolved_components": components(), "warnings": [], "issues": [{"field": "template Seedless/VC0", "message": reason}]}
def seeded_template(project="Plant_template.PRJ"):
    full = selected()
    full["template"] = {"project": project, "variant": "VC0", "subarray_id": 1, "orientation_id": 1, "capacity_plan": None, "counts": {"NModSerie": 12, "NStringCh": 6, "NInvMPPT": 1, "NInverter": 2}}
    return {"status": "ready", "selected": full, "resolved_components": components(), "issues": [], "warnings": []}
class FakeCLI(FakeCliBase):
    def __init__(self, workspace, failure=None):
        super().__init__(workspace, version="8.1.6", failure=failure)
class FakeStore:
    def __init__(self, root):
        self.lock = threading.RLock()
        self.root = root
        (root / "Projects").mkdir(parents=True, exist_ok=True)
    def _root(self):
        return self.root / "Projects"
    @staticmethod
    def _belongs_to_project(path, project):
        return path.stem.casefold() == Path(project).stem.casefold()
class DesignFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / flow.EXPECTED_WORKSPACE_BASENAME
        self.workspace.mkdir()
        self.store = FakeStore(self.workspace)
        self.cli = FakeCLI(self.workspace)
    def snapshot(self):
        return sorted(str(path.relative_to(self.workspace)) for path in self.workspace.rglob("*"))
    def run_flow(self, spec_value, dry_run=True, *, discovery=None, envelope=None, download=None, prepare=None, simulate=None, seed=None, cli=None):
        patches = {"discover_design_inputs": discovery if discovery is not None else discovery_ready, "simulate_template": simulate if simulate is not None else simulation(status="ready", stage="preflight")}
        for name, value in (("prepare_design_envelope", envelope), ("download_weather", download), ("prepare_weather", prepare), ("prepare_standard_template", seed)):
            if value is not None: patches[name] = value
        mocks = {}
        for name, value in patches.items():
            entry = (patch.object(flow, name, side_effect=value) if callable(value) else patch.object(flow, name, return_value=value))
            mocks[name] = entry.start()
            self.addCleanup(entry.stop)
        result = flow.simulate_design(cli or self.cli, self.store, spec=spec_value, dry_run=dry_run)
        return result, mocks
    def test_spec_validation_scope_design_choice_and_strict_fields(self):
        before = self.snapshot()
        for scope in ("roof", "free_standing"):
            res, _ = self.run_flow(spec(scope=scope))
            self.assertEqual((res["status"], res["stage"]), ("blocked", "spec"))
            self.assertIn("fixed_unshaded_grid", next(i["message"] for i in res["issues"] if i["field"] == "scope"))
        unset, _ = self.run_flow(spec(scope=""))
        self.assertEqual((unset["status"], unset["stage"]), ("ready", "preflight"))
        for key in ("status", "stage", "dry_run", "execution_preflight_complete", "issues", "warnings", "selected", "preview", "metrics", "artifacts", "recovery"):
            self.assertIn(key, unset)
        missing, mocks = self.run_flow(spec(capacity=None))
        self.assertEqual((missing["status"], missing["stage"], mocks["discover_design_inputs"].call_count), ("blocked", "spec", 0))
        self.assertEqual(self.snapshot(), before)
        both, _ = self.run_flow(spec(electrical_design={"temperatures": {}}))
        self.assertIn("mutually exclusive", next(i["message"] for i in both["issues"] if i["field"] == "electrical_design"))
        invalid = ((spec(extra=1), "extra"), (spec(tilt=True), "tilt"), (spec(target_dc_kw=float("nan")), "target_dc_kw"),
            (spec(azimuth=200.0), "azimuth"), (spec(year=True), "year"),
            (spec(losses={"LIDLoss": True}), "losses.LIDLoss"), (spec(losses={"LIDLoss": 1.0, "Other": 2.0}), "losses.Other"),
            (spec(module={"file_name": "sub/mod.PAN"}), "module.file_name"), (spec(weather={"mode": "guess"}), "weather.mode"),
            (spec(weather={"mode": "csv", "csv_name": "a.csv", "country_code": "CN"}), "weather.time_basis"),
            (spec(weather={"mode": "csv", "csv_name": "a.csv", "country_code": "cn", "time_basis": flow.TIME_BASIS}), "weather.country_code"),
            (spec(weather={"mode": "open_meteo", "location_id": 1, "latitude": 1.0, "country_code": "CN"}), "weather.location_id"),
            (spec(weather={"mode": "open_meteo", "country_code": "CN"}), "weather.location_id"),
            (spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN", "temperature_method": "mean"}), "weather.temperature_method"),
            (spec(capacity=capacity(target_dc_kw=90.0)), "capacity.target_dc_kw"), (spec(year=""), "year"), (spec(tilt=""), "tilt"), (spec(azimuth=""), "azimuth"),
            (spec(target_dc_kw=""), "target_dc_kw"), (spec(capacity=capacity(target_dc_kw="")), "capacity.target_dc_kw"),
            (spec(module={"file_name": "", "manufacturer": ""}), "module.name"))
        for broken, field in invalid:
            with self.subTest(field=field):
                res, _ = self.run_flow(broken)
                self.assertEqual((res["status"], res["stage"]), ("blocked", "spec"))
                self.assertIn(field, [item["field"] for item in res["issues"]])
        typed = spec(template_project="", variant="", module={"file_name": "", "manufacturer": "Lab", "model": "440W"},
                     inverter={"file_name": "", "manufacturer": "Lab", "model": "50kW"},
                     weather={"mode": "existing", "sit_name": "Plant.SIT", "met_name": "Plant.MET", "csv_name": "", "country_code": "", "time_basis": "", "site_label": "", "weather_manifest_name": ""})
        ok, _ = self.run_flow(typed)
        self.assertEqual((ok["status"], ok["stage"], ok["issues"]), ("ready", "preflight", []))
    def test_capacity_confirmation_and_declared_design_envelope_forwarding(self):
        plan = {"status": "ready", "inverter_count": 2, "steps": [], "capacity_error_pct": 0.0}
        calls = []
        def discovery(store, **kwargs):
            calls.append(kwargs)
            return {"status": "ready", "selected": copy.deepcopy(selected()), "issues": [], "warnings": [], "capacity_plan": plan}
        csv_res, mocks = self.run_flow(spec(weather={"mode": "csv", "csv_name": "stage.csv", "country_code": "CN", "time_basis": flow.TIME_BASIS}),
            discovery=discovery, prepare=lambda cli, **kwargs: {"status": "ready", "warnings": [], "issues": [], "artifacts": {}, "created_files": {}})
        self.assertEqual((csv_res["status"], csv_res["stage"]), ("ready_for_weather", "planned"))
        self.assertFalse(csv_res["execution_preflight_complete"])
        self.assertEqual([item["capacity"] for item in calls], [None, spec()["capacity"]])
        self.assertEqual(mocks["simulate_template"].call_count, 0)
        calls.clear()
        env_calls = []
        def envelope(module_electrical, inverter_electrical, request):
            env_calls.append(request)
            return {"status": "ready", "capacity": capacity(), "issues": [], "warnings": [{"field": "module.GRef", "message": "not 1000 W/m2"}], "provenance": {}, "envelope_mode": "coefficients"}
        env_res, env_mocks = self.run_flow(spec(capacity=None, electrical_design={"temperatures": {"min_ambient_c": -10.0}}),
            discovery=discovery, envelope=envelope, simulate=simulation(status="ready", stage="preflight"))
        self.assertEqual((env_res["status"], env_res["stage"], env_res["execution_preflight_complete"]), ("ready", "preflight", True))
        self.assertEqual(env_calls[0]["manufacturer_model"], "Lab 440W / Lab 50kW")
        self.assertIn("module.GRef: not 1000 W/m2", env_res["warnings"])
        self.assertEqual(env_mocks["simulate_template"].call_args.kwargs["capacity"], capacity())
        bad_label, label_mocks = self.run_flow(spec(capacity=None, electrical_design={"manufacturer_model": "Other 1 / Lab 50kW"}), envelope=lambda *a: (_ for _ in ()).throw(AssertionError("should not build envelope")))
        self.assertEqual((bad_label["status"], bad_label["stage"]), ("blocked", "design_envelope"))
        self.assertEqual(label_mocks["discover_design_inputs"].call_count, 1)
    def test_discovery_blocks_ambiguous_or_drifting_components(self):
        ambiguous = {"status": "blocked", "selected": None, "warnings": [], "issues": [{"field": "PAN.name", "message": "2 usable workspace files share this identity"}]}
        res, mocks = self.run_flow(spec(), discovery=lambda *a, **k: ambiguous, prepare=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((res["status"], res["stage"]), ("blocked", "discovery"))
        self.assertEqual((mocks["prepare_weather"].call_count, mocks["simulate_template"].call_count), (0, 0))
        passes = [selected(), selected()]
        passes[1]["PAN"]["electrical"] = {"fields": {"Voc": {"status": "declared", "value": 45.0}}}
        seen = []
        def drift(store, **kwargs):
            seen.append(kwargs)
            return {"status": "ready", "selected": copy.deepcopy(passes[len(seen) - 1]), "issues": [], "warnings": []}
        drifted, drift_mocks = self.run_flow(spec(), discovery=drift)
        self.assertEqual((drifted["status"], drifted["stage"]), ("blocked", "discovery"))
        self.assertTrue(any("changed between the first discovery" in i["message"] for i in drifted["issues"]))
        self.assertEqual(drift_mocks["simulate_template"].call_count, 0)
    def test_open_meteo_execution_dry_run_location_country_and_limits(self):
        plan = {"status": "ready", "standard_utc_offset_hours": 8, "location": {"country_code": "CN", "name": "Beijing"},
                "review_required": ["hourly Tamb uses the endpoint_mean approximation"], "artifacts": {"csv": "Plant_openmeteo.csv"}, "warnings": [], "issues": []}
        dry, dry_mocks = self.run_flow(spec(weather={"mode": "open_meteo", "location_id": 1816670}),
            download=lambda *a, **k: plan, prepare=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prepare in dry run")), simulate=lambda *a, **k: None)
        self.assertEqual((dry["status"], dry["stage"], dry["execution_preflight_complete"]), ("ready_for_weather", "planned", False))
        self.assertEqual(dry_mocks["download_weather"].call_count, 1)
        prepared = {"status": "prepared", "created_files": {"csv": {"name": "Plant_openmeteo.csv"}}, "location": {"country_code": "CN"},
                    "review_required": ["hourly Tamb uses the endpoint_mean approximation"], "artifacts": {"csv": "Plant_openmeteo.csv"}, "warnings": [], "issues": []}
        prep = {"status": "prepared", "warnings": [], "issues": [], "artifacts": {"manifest": str(self.workspace / "Results" / "Plant_weather.json")}}
        done, done_mocks = self.run_flow(spec(weather={"mode": "open_meteo", "location_id": 1816670}), dry_run=False,
            download=lambda *a, **k: plan if k["dry_run"] else prepared,
            prepare=lambda *a, **k: prep, simulate=simulation(artifacts={"csv": "annual.csv"}, metrics={"PR": 80.0}))
        self.assertEqual((done["status"], done["stage"], done["execution_preflight_complete"]), ("needs_review", "completed", True))
        self.assertEqual(done_mocks["prepare_weather"].call_args.kwargs["country_code"], "CN")
        self.assertEqual([c.kwargs["dry_run"] for c in done_mocks["download_weather"].call_args_list], [True, False])
        for bad_plan, expected_stage, field_or_msg in (
            ({"status": "ready", "standard_utc_offset_hours": 13, "artifacts": {}, "warnings": [], "issues": []}, "acquisition", "create-site"),
            ({"status": "blocked", "warnings": [], "artifacts": {}, "issues": ["no ISO country code"]}, "acquisition", "no ISO country code")):
            blocked, bm = self.run_flow(spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "NZ"}),
                dry_run=False, download=lambda *a, **k: bad_plan, prepare=lambda *a, **k: None, simulate=lambda *a, **k: None)
            self.assertEqual((blocked["status"], blocked["stage"]), ("blocked", expected_stage))
            self.assertEqual(bm["prepare_weather"].call_count, 0)
            self.assertIn(field_or_msg, " ".join(i["message"] for i in blocked["issues"]))
    def test_preflight_outputs_aliases_and_downstream_failures(self):
        cli_bad = FakeCLI(self.workspace, failure="Installed PVsystCLI lacks run-simulation --time-step")
        np_res, np_mocks = self.run_flow(spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            dry_run=False, cli=cli_bad, download=lambda *a, **k: None, prepare=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((np_res["status"], np_res["stage"], np_mocks["download_weather"].call_count), ("blocked", "native_preflight", 0))
        conflict = self.workspace / "Results" / "Plant_annual_2024.csv"
        conflict.parent.mkdir(parents=True, exist_ok=True)
        conflict.write_text("", encoding="utf-8")
        out_res, _ = self.run_flow(spec(), simulate=lambda *a, **k: None)
        self.assertEqual((out_res["status"], out_res["stage"]), ("blocked", "outputs"))
        self.assertIn("annual_csv", [i["field"] for i in out_res["issues"]])
        conflict.unlink()
        alias_res, alias_mocks = self.run_flow(spec(weather={"mode": "open_meteo", "csv_name": "Plant_import.csv", "location_id": 1816670, "country_code": "CN"}),
            download=lambda *a, **k: None, prepare=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((alias_res["status"], alias_res["stage"], alias_mocks["download_weather"].call_count), ("blocked", "outputs", 0))
        existing_ok, ex_mocks = self.run_flow(spec(), simulate=simulation(status="ready", stage="preflight"))
        self.assertEqual(ex_mocks["simulate_template"].call_args.kwargs["weather_manifest_name"], "")
        self.assertTrue(any("source-weather comparison is skipped" in w for w in existing_ok["warnings"]))
        prep_fail, pf_mocks = self.run_flow(spec(weather={"mode": "csv", "csv_name": "stage.csv", "country_code": "CN", "time_basis": flow.TIME_BASIS}),
            dry_run=False, prepare=lambda *a, **k: {"status": "failed", "issues": [], "warnings": [], "error": "Native SIT differs from weather CSV #Longitude", "artifacts": {}}, simulate=lambda *a, **k: None)
        self.assertEqual((prep_fail["status"], prep_fail["stage"], pf_mocks["simulate_template"].call_count), ("failed", "weather_prepare", 0))
        self.assertIn("Native SIT differs", [i["message"] for i in prep_fail["issues"]][0])
        def raise_prep(*a, **k):
            raise PVsystError("convert-meteo failed", diagnostics="Meteo conversion refused")
        raised_prep, _ = self.run_flow(spec(weather={"mode": "csv", "csv_name": "stage.csv", "country_code": "CN", "time_basis": flow.TIME_BASIS}), dry_run=False, prepare=raise_prep, simulate=lambda *a, **k: None)
        self.assertEqual((raised_prep["status"], raised_prep["stage"]), ("failed", "weather_prepare"))
        recovery = [{"action": "retain_isolated_project_for_review", "project": "Plant.PRJ"}]
        reason = "Configured counts do not match the accepted capacity plan"
        sim_fail, _ = self.run_flow(spec(weather={"mode": "csv", "csv_name": "stage.csv", "country_code": "CN", "time_basis": flow.TIME_BASIS}),
            dry_run=False, prepare=lambda *a, **k: {"status": "prepared", "warnings": [], "issues": [], "artifacts": {"manifest": str(self.workspace / "Results" / "Plant_weather.json")}},
            simulate=simulation(status="failed", stage="capacity", recovery=recovery, error=reason, issues=[reason]))
        self.assertEqual((sim_fail["status"], sim_fail["stage"], sim_fail["recovery"]), ("failed", "capacity", recovery))
        self.assertEqual([i["message"] for i in sim_fail["issues"]].count(reason), 1)
        def failing_dl(ws, **kw):
            if kw["dry_run"]:
                return {"status": "ready", "standard_utc_offset_hours": 8, "artifacts": {"raw_json": "raw.json"}, "warnings": [], "issues": []}
            raise RuntimeError("provider response exceeds limit")
        before = self.snapshot()
        acq_fail, acq_mocks = self.run_flow(spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            dry_run=False, download=failing_dl, prepare=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((acq_fail["status"], acq_fail["stage"], acq_mocks["prepare_weather"].call_count), ("failed", "acquisition", 0))
        self.assertEqual(acq_fail["artifacts"]["acquire"]["raw_json"], "raw.json")
        self.assertEqual(self.snapshot(), before)
    def test_seed_fallback_preview_execution_and_cancellation(self):
        seed_ready = {"status": "ready", "source": {"seed": "installed 8.1 residential demo"}, "retained_defaults": ["losses"], "changes": [], "warnings": [], "issues": []}
        (self.workspace / "Projects").rmdir()
        dry, dry_mocks = self.run_flow(spec(), discovery=lambda *a, **k: no_template(), seed=lambda *a, **k: seed_ready, simulate=lambda *a, **k: None)
        self.assertEqual((dry["status"], dry["stage"], (self.workspace / "Projects").exists()), ("ready_for_weather", "planned", False))
        self.assertTrue(dry_mocks["prepare_standard_template"].call_args.kwargs["dry_run"])
        seq = [no_template(), no_template(), seeded_template()]
        seen, seed_calls = [], []
        def discovery(store, **kwargs):
            seen.append(kwargs)
            return copy.deepcopy(seq[len(seen) - 1])
        def seed(cli, store, **kwargs):
            seed_calls.append(kwargs)
            if kwargs["dry_run"]: return seed_ready
            self.assertTrue((self.workspace / "Projects").is_dir())
            return {"status": "created", "template_project": "Plant_template.PRJ", "variant": "VC0", "files": {"Plant_template.PRJ": "a"}, "retained_defaults": [], "warnings": [], "issues": []}
        done, mocks = self.run_flow(spec(), dry_run=False, discovery=discovery, seed=seed, simulate=simulation(artifacts={}))
        self.assertEqual((done["status"], [c["dry_run"] for c in seed_calls]), ("completed", [True, False]))
        self.assertEqual(mocks["simulate_template"].call_args.kwargs["template_project"], "Plant_template.PRJ")
        seq2 = [no_template(), discovery_ready(None)]
        seen2, seed2 = [], []
        cancelled, cm = self.run_flow(spec(), dry_run=False, discovery=lambda s, **k: (seen2.append(k), copy.deepcopy(seq2[len(seen2) - 1]))[1],
            seed=lambda *a, **k: seed2.append(k) or {}, simulate=simulation(artifacts={}))
        self.assertEqual((cancelled["status"], seed2, cancelled["artifacts"]["seed"]), ("completed", [], {}))
        self.assertEqual(cm["simulate_template"].call_args.kwargs["template_project"], "Template.PRJ")
    def test_seed_fallback_conflicts_drift_limits_and_explicit_bypass(self):
        seed_conflict = self.workspace / "Projects" / "Plant_template.PRJ"
        seed_conflict.write_bytes(b"")
        conflict, cm = self.run_flow(spec(), discovery=lambda *a, **k: no_template(), seed=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((conflict["status"], conflict["stage"], cm["prepare_standard_template"].call_count), ("blocked", "outputs", 0))
        seed_conflict.unlink()
        long_res, lm = self.run_flow(spec(new_project="P" * 170 + ".PRJ"), discovery=lambda *a, **k: no_template(), seed=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((long_res["status"], long_res["stage"], lm["prepare_standard_template"].call_count), ("blocked", "outputs", 0))
        self.assertIn("180-character project filename limit", long_res["issues"][0]["message"])
        explicit, em = self.run_flow(spec(template_project="Mine.PRJ", variant="VC0"), discovery=lambda *a, **k: no_template(), seed=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((explicit["status"], explicit["stage"], em["prepare_standard_template"].call_count), ("blocked", "discovery", 0))
        changed = seeded_template()
        changed["selected"]["PAN"]["name"] = "other.PAN"
        seq = [no_template(), no_template(), changed]
        seen = []
        drifted, dm = self.run_flow(spec(), dry_run=False, discovery=lambda s, **k: (seen.append(k), copy.deepcopy(seq[len(seen) - 1]))[1],
            seed=lambda c, s, **k: ({"status": "ready", "retained_defaults": [], "warnings": [], "issues": []} if k["dry_run"] else
                                    {"status": "created", "files": {"Plant_template.PRJ": "a"}, "retained_defaults": [], "warnings": [], "issues": []}),
            simulate=lambda *a, **k: None)
        self.assertEqual((drifted["status"], drifted["stage"], dm["simulate_template"].call_count), ("blocked", "discovery", 0))
        self.assertEqual(drifted["artifacts"]["seed"]["project"], "Plant_template.PRJ")
    def test_workspace_root_layout_and_escaping_guards(self):
        (self.workspace / "Projects").rmdir()
        before = self.snapshot()
        ro, _ = self.run_flow(spec())
        self.assertEqual((ro["status"], ro["preview"]["workspace"]["created_directories"]), ("ready", []))
        self.assertEqual(self.snapshot(), before)
        done, _ = self.run_flow(spec(), dry_run=False, simulate=simulation(artifacts={}))
        self.assertEqual((done["status"], len(done["preview"]["workspace"]["created_directories"])), ("completed", 26))
        for label in list(flow.LAYOUT_ROOT_FOLDERS) + [f"ComposPV/{n}" for n in flow.LAYOUT_COMPOSPV_FOLDERS]:
            self.assertTrue((self.workspace / label).is_dir(), label)
        wrong = self.root / "PVsyst8.1_Data_backup"
        wrong.mkdir()
        wr, wm = self.run_flow(spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            cli=FakeCLI(wrong), download=lambda *a, **k: None, prepare=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((wr["status"], wr["stage"], wm["download_weather"].call_count), ("blocked", "workspace", 0))
        import shutil
        conflict = self.workspace / "Models"
        shutil.rmtree(conflict)
        conflict.write_text("existing", encoding="utf-8")
        fc, fm = self.run_flow(spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            dry_run=False, download=lambda *a, **k: None, prepare=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((fc["status"], fc["stage"], fm["download_weather"].call_count), ("blocked", "workspace", 0))
        conflict.unlink()
        outside = self.root / "outside"
        outside.mkdir()
        link = self.workspace / "ComposPV" / "Batteries"
        shutil.rmtree(link)
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError): return
        esc, em = self.run_flow(spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            dry_run=False, download=lambda *a, **k: None, prepare=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((esc["status"], esc["stage"], em["download_weather"].call_count), ("blocked", "workspace", 0))
        self.assertIn("ComposPV/Batteries", esc["preview"]["workspace"]["escaped"])
GRID = {"valid": True, "system_type": "Grid", "issues": [], "unchecked_checks": []}
def template_spec(*, subarrays=1, planes=1, grid=True, prcorr=True, geometry=None):
    parameters = {"subarrays": [{"id": 1, "values": {"NoOrientation": "1", "NModSerie": "12", "NStringCh": "6", "NInvMPPT": "1", "NInverter": "2"}}] * subarrays,
                  "orientations": [{"id": 1, "values": {"FieldTilt": "20"}}] * planes}
    structure = GRID if grid else {"valid": False, "system_type": "Grid", "issues": ["counts differ"], "unchecked_checks": []}
    return {"parameters": parameters, "structure": structure, "prcorr": [3] if prcorr else [], "geometry": geometry}
class FakeComponents:
    """Loose-file listing and component access without parsing or hashing real files."""
    def __init__(self, root, workspace, installed=None, builtin=None, unusable=()):
        self.root = root
        self.files = {"workspace": dict(workspace), "builtin": dict(installed or {})}
        self.builtin = builtin
        self.unusable = set(unusable)
        for library, entries in self.files.items():
            (root / library).mkdir(parents=True, exist_ok=True)
            for name in entries: (root / library / name).write_bytes(b"")
    def list(self, kind, library, query="", offset=0, limit=200):
        if library == "builtin" and self.builtin is None: raise ValueError("Installed component library is not configured")
        items = [{"name": name, "format": "text", "manufacturer": manufacturer, "model": model}
                 for name, (manufacturer, model) in sorted(self.files[library].items()) if query.casefold() in f"{name} {manufacturer} {model}".casefold()]
        return {"total": len(items), "offset": offset, "items": items[offset:offset + limit]}
    def _path(self, kind, name, library="workspace"):
        if library == "builtin" and self.builtin is None: raise ValueError("Installed component library is not configured")
        return self.root / library / name
    def _read(self, path):
        if path.name in self.unusable: raise ValueError("Component object type or format does not match extension")
        return f"{path.parent.name}/{path.name}".encode()
    def _inspect(self, kind, raw):
        name = raw.decode().split("/")[-1]
        for library in ("workspace", "builtin"):
            if name in self.files[library]:
                manufacturer, model = self.files[library][name]
                return {"format": "text", "manufacturer": manufacturer, "model": model, "structural_errors": [], "warnings": []}
        raise ValueError("Unknown component")
    def _require_valid(self, info, complete=False):
        if info["structural_errors"] or info["warnings"]: raise ValueError("Invalid component")
class FakeVariants:
    """Per-variant gate results, including injected failures, without reading real files."""
    def __init__(self, root, components, projects):
        self.root = root
        self.components = components
        self.projects = projects
        self.current = ("Template", "VC0")
    @staticmethod
    def _base(project):
        return project[:-4] if project.upper().endswith(".PRJ") else project
    def _root(self):
        return self.root / "Projects"
    def _spec(self, project, variant):
        return self.projects[self._base(project)][variant]
    def inspect_project(self, project):
        spec = self.projects[self._base(project)]
        if isinstance(spec, str): raise ValueError(spec)
        return {"project": project, "variants": list(spec["variants"]), "files": {}, "sources": {}, "warnings": []}
    def inspect_parameters(self, project, variant):
        return self._spec(project, variant)["parameters"]
    def validate_structure(self, project, variant):
        return self._spec(project, variant)["structure"]
    def _path(self, project, variant=None):
        base = self._base(project)
        return self.root / "Projects" / (base if variant is None else f"{base}.{variant}")
    def _read(self, path):
        self.current = path.name.rsplit(".", 1)
        return path.name.encode()
    def _text(self, data):
        self.current = data.decode().rsplit(".", 1)
        return "", ["PVObject_=pvVCalcul\n", "  Version=8.1.6\n", "PVObject_=pvOrient\n", "  PRCorrTemp=False\n"]
    def _field_indices(self, lines, bounds, key):
        return list(self.projects[self.current[0]][self.current[1]]["prcorr"]) if key == "PRCorrTemp" else []
    def _require_no_geometry(self, lines, require_shading_section=False):
        if error := self.projects[self.current[0]][self.current[1]].get("geometry"): raise ValueError(error)
    def _ordinary_fixed_plane(self, lines, bounds, allow_missing_flags=False):
        return None
    def _orientation(self, lines, number):
        return (2, 3)
class DesignInputTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "Projects").mkdir()
        self.electrical = patch("pvsyst_auto.engineering.review.electrical_inventory", side_effect=lambda kind, text: {"fields": {"source": text}})
        self.parameters = patch("pvsyst_auto.engineering.review.pan_validation_fields", return_value={"Technol": ["mtSiMono"], "GRef": ["1000"], "TRef": ["25"]})
    def components(self, workspace, installed=None, builtin=None, unusable=()):
        return FakeComponents(self.root, workspace, installed, builtin, unusable)
    def variants(self, projects, components):
        return FakeVariants(self.root, components, projects)
    def projects(self, store, names):
        for name in names: (self.root / "Projects" / f"{name}.PRJ").write_bytes(b"")
        return store
    def test_exact_identity_binds_selected_files_and_electrical(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        store = self.variants({"Template": {"variants": ["VC0"], "VC0": template_spec()}}, components)
        with self.electrical:
            result = discover_design_inputs(store, module_manufacturer="lab", module_model="440w", inverter_manufacturer="LAB", inverter_model="50KW", template_project="Template.PRJ", variant="VC0")
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["selection_rule"], SELECTION_RULE)
        self.assertEqual(result["selected"]["PAN"]["name"], "module.PAN")
        self.assertEqual(result["selected"]["PAN"]["library"], "workspace")
        self.assertEqual((result["selected"]["PAN"]["manufacturer"], result["selected"]["PAN"]["model"]), ("Lab", "440W"))
        self.assertEqual(result["selected"]["PAN"]["electrical"]["fields"]["source"], "workspace/module.PAN")
        self.assertEqual(result["selected"]["OND"]["electrical"]["fields"]["source"], "workspace/inverter.OND")
        self.assertEqual(result["selected"]["template"]["subarray_id"], 1)
        self.assertNotIn("_text", result["selected"]["PAN"])
    def test_resolved_components_survive_a_missing_template(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        store = self.variants({}, components)  # empty workspace: no PRJ member at all
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        with self.electrical:
            result = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W", inverter_manufacturer="Lab", inverter_model="50kW")
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["selected"])
        self.assertEqual(result["candidates"]["templates"], [])
        self.assertEqual(result["resolved_components"]["PAN"]["name"], "module.PAN")
        self.assertEqual(result["resolved_components"]["OND"]["name"], "inverter.OND")
        self.assertEqual(result["resolved_components"]["PAN"]["electrical"]["fields"]["source"], "workspace/module.PAN")
        message = next(item["message"] for item in result["issues"] if item["field"] == "template_project")
        self.assertIn("No readable workspace PRJ", message)
        self.assertEqual(sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")), before)
    def test_duplicate_missing_and_unusable_identity_block_specifically(self):
        entries = {"a.PAN": ("Lab", "440W"), "b.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")}
        store = self.variants({"Template": {"variants": ["VC0"], "VC0": template_spec()}}, self.components(entries))
        with self.electrical:
            ambiguous = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W", inverter_manufacturer="Lab", inverter_model="50kW", template_project="Template.PRJ", variant="VC0")
        self.assertEqual(ambiguous["status"], "blocked")
        self.assertIsNone(ambiguous["selected"])
        self.assertIsNone(ambiguous["resolved_components"])
        message = next(item["message"] for item in ambiguous["issues"] if item["field"] == "PAN.name")
        self.assertIn("2 usable workspace files", message)
        self.assertEqual([item["name"] for item in ambiguous["candidates"]["PAN"]], ["a.PAN", "b.PAN"])
        self.assertTrue(all(item["valid"] for item in ambiguous["candidates"]["PAN"]))
        missing = discover_design_inputs(store, module_manufacturer="Other", module_model="1W", inverter_manufacturer="Lab", inverter_model="50kW", template_project="Template.PRJ", variant="VC0")
        self.assertEqual(missing["status"], "blocked")
        message = next(item["message"] for item in missing["issues"] if item["field"] == "PAN.name")
        self.assertIn("No loose file matches Other 1W", message)
        self.assertIn("encrypted built-in database cannot be enumerated", message)
        self.assertEqual(missing["candidates"]["PAN"], [])
        self.assertIsNone(missing["resolved_components"])
        broken = self.variants({"Template": {"variants": ["VC0"], "VC0": template_spec()}}, self.components(entries, unusable={"a.PAN", "b.PAN"}))
        unusable = discover_design_inputs(broken, module_manufacturer="Lab", module_model="440W", inverter_manufacturer="Lab", inverter_model="50kW", template_project="Template.PRJ", variant="VC0")
        self.assertEqual(unusable["status"], "blocked")
        message = next(item["message"] for item in unusable["issues"] if item["field"] == "PAN.name")
        self.assertIn("No usable component", message)
        self.assertIn("does not match extension", message)
    def test_installed_selection_keeps_identity_under_workspace_shadow(self):
        components = self.components({"other.PAN": ("Lab", "500W"), "inverter.OND": ("Lab", "50kW")}, installed={"installed.PAN": ("Lab", "440W")}, builtin=str(self.root / "installed"))
        store = self.variants({"Template": {"variants": ["VC0"], "VC0": template_spec()}}, components)
        with self.electrical:
            selected = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W", inverter_manufacturer="Lab", inverter_model="50kW", template_project="Template.PRJ", variant="VC0")
        self.assertEqual(selected["status"], "ready", selected)
        self.assertEqual((selected["selected"]["PAN"]["name"], selected["selected"]["PAN"]["library"]), ("installed.PAN", "builtin"))
        self.assertEqual(selected["selected"]["PAN"]["electrical"]["fields"]["source"], "builtin/installed.PAN")
        components.files["workspace"]["installed.PAN"] = ("Lab", "300W")
        (self.root / "workspace" / "installed.PAN").write_bytes(b"")
        shadowed = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W", inverter_manufacturer="Lab", inverter_model="50kW", template_project="Template.PRJ", variant="VC0")
        self.assertEqual(shadowed["status"], "blocked")
        self.assertIsNone(shadowed["selected"])
        message = next(item["message"] for item in shadowed["issues"] if item["field"] == "PAN.name")
        self.assertIn("No usable component", message)
        self.assertIn("Shadowed by a workspace file", message)
        self.assertTrue(any("shadows the installed-library match" in warning for warning in shadowed["warnings"]))
        self.assertEqual([item["library"] for item in shadowed["candidates"]["PAN"]], ["builtin"])
        self.assertFalse(shadowed["candidates"]["PAN"][0]["valid"])
    def test_workspace_identity_wins_over_shadowed_identical_installed_file(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")}, installed={"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")}, builtin=str(self.root / "installed"))
        store = self.variants({"Template": {"variants": ["VC0"], "VC0": template_spec()}}, components)
        with self.electrical:
            result = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W", inverter_manufacturer="Lab", inverter_model="50kW", template_project="Template.PRJ", variant="VC0")
        # An unusable installed copy of the same name must not block the usable workspace identity.
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["selected"]["PAN"]["library"], "workspace")
        self.assertEqual(result["selected"]["OND"]["library"], "workspace")
        self.assertEqual([(item["library"], item["valid"]) for item in result["candidates"]["PAN"]], [("workspace", True), ("builtin", False)])
    def test_template_gates_report_reasons_and_select_stably(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        projects = {"Alpha": {"variants": ["VC0"], "VC0": template_spec()}, "Other": {"variants": ["VC0", "VC1", "VC2", "VC3"], "VC0": template_spec(),
                      "VC1": template_spec(subarrays=2), "VC2": template_spec(prcorr=False), "VC3": template_spec(geometry="Operation has an active shading scene")},
            "Template": {"variants": ["VC0"], "VC0": template_spec(grid=False)}}
        store = self.projects(self.variants(projects, components), projects)
        with self.electrical:
            result = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W", inverter_manufacturer="Lab", inverter_model="50kW")
        self.assertEqual(result["status"], "ready", result)
        keys = [f"{item['project']}/{item['variant']}" for item in result["candidates"]["templates"]]
        self.assertEqual(keys, ["Alpha.PRJ/VC0", "Other.PRJ/VC0", "Other.PRJ/VC1", "Other.PRJ/VC2", "Other.PRJ/VC3", "Template.PRJ/VC0"])
        rejected = {key: item["reasons"] for key, item in zip(keys, result["candidates"]["templates"]) if not item["valid"]}
        self.assertIn("exactly one subarray and one fixed plane", rejected["Other.PRJ/VC1"][0])
        self.assertIn("PRCorrTemp=False", rejected["Other.PRJ/VC2"][0])
        self.assertIn("active shading scene", rejected["Other.PRJ/VC3"][0])
        self.assertIn("completely checked Grid template", rejected["Template.PRJ/VC0"][0])
        self.assertTrue(any(warning.startswith("Other.PRJ/VC1 is not eligible") for warning in result["warnings"]))
        self.assertEqual(result["selected"]["template"]["project"], "Alpha.PRJ")
        self.assertIn("stable project/variant name", result["selection_rule"])
    def test_broken_projects_and_missing_variants_do_not_block_eligible_templates(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        projects = {"Alpha": {"variants": ["VC0", "VC9"], "VC0": template_spec(), "VC9": template_spec()}, "Broken": "Unrecognized project sidecar: Broken.VC0", "Other": {"variants": ["VC0"], "VC0": template_spec()}}
        store = self.projects(self.variants(projects, components), projects)
        with self.electrical:
            scanned = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W", inverter_manufacturer="Lab", inverter_model="50kW")
        self.assertEqual(scanned["status"], "ready", scanned)
        self.assertEqual(scanned["issues"], [])
        candidates = {f"{item['project']}/{item['variant']}": item for item in scanned["candidates"]["templates"]}
        self.assertIn("Project is not readable", candidates["Broken.PRJ/"]["reasons"][0])
        self.assertTrue(any(warning.startswith("Broken.PRJ/ is not eligible") for warning in scanned["warnings"]))
        with self.electrical:
            mismatch = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W", inverter_manufacturer="Lab", inverter_model="50kW", variant="VC9")
        # Only Alpha declares VC9; the missing variant elsewhere must not block it.
        self.assertEqual(mismatch["status"], "ready", mismatch)
        self.assertEqual(mismatch["issues"], [])
        self.assertEqual(mismatch["selected"]["template"]["project"], "Alpha.PRJ")
        rejected = {f"{item['project']}/{item['variant']}": item["reasons"] for item in mismatch["candidates"]["templates"] if not item["valid"]}
        self.assertIn("Variant VC9 is missing from this project", rejected["Other.PRJ/VC9"][0])
    def test_unusable_scan_or_explicit_broken_project_blocks_with_reason(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        store = self.projects(self.variants({"Broken": "Unrecognized project sidecar: Broken.VC0"}, components), {"Broken"})
        with self.electrical:
            scanned = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W", inverter_manufacturer="Lab", inverter_model="50kW")
        self.assertEqual(scanned["status"], "blocked")
        self.assertIsNone(scanned["selected"])
        message = next(item["message"] for item in scanned["issues"] if item["field"].startswith("template "))
        self.assertIn("Unrecognized project sidecar", message)
        explicit = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W", inverter_manufacturer="Lab", inverter_model="50kW", template_project="Broken.PRJ", variant="VC0")
        self.assertEqual(explicit["status"], "blocked")
        self.assertEqual(explicit["candidates"]["templates"], [])
        message = next(item["message"] for item in explicit["issues"] if item["field"] == "template_project")
        self.assertIn("Unrecognized project sidecar", message)
    def test_capacity_plan_gates_and_orders_candidates(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        projects = {"Far": {"variants": ["VC0"], "VC0": template_spec()}, "Near": {"variants": ["VC0"], "VC0": template_spec()}}
        store = self.projects(self.variants(projects, components), projects)
        capacity = {"target_dc_kw": 28.16, "voltage_standard": "IEC", "design_basis": "declared envelope"}
        plans = [{"status": "ready", "inverter_count": 5, "steps": [], "capacity_error_pct": 0.0, "issues": []},
                 {"status": "ready", "inverter_count": 2, "steps": [{"NStringCh": 4}], "capacity_error_pct": 0.0, "issues": []}]
        with self.electrical, self.parameters, patch("pvsyst_auto.engineering.review.plan_capacity", side_effect=plans):
            result = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W", inverter_manufacturer="Lab", inverter_model="50kW", capacity=capacity)
        # Far is listed first, but the template closest to the planned inverter count wins.
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["selected"]["template"]["project"], "Near.PRJ")
        self.assertEqual(result["selected"]["template"]["capacity_plan"]["inverter_count"], 2)
        self.assertEqual(result["candidates"]["templates"][0]["project"], "Far.PRJ")
        blocked_plan = {"status": "blocked", "issues": [{"field": "capacity.target_dc_kw", "message": "No supported integral plan"}]}
        with self.electrical, self.parameters, patch("pvsyst_auto.engineering.review.plan_capacity", return_value=blocked_plan):
            blocked = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W", inverter_manufacturer="Lab", inverter_model="50kW", capacity=capacity)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIsNone(blocked["selected"])
        self.assertEqual([item["valid"] for item in blocked["candidates"]["templates"]], [False, False])
        message = next(item["message"] for item in blocked["issues"] if item["field"].startswith("template "))
        self.assertIn("No supported integral plan", message)
    def test_discovery_writes_nothing(self):
        components = self.components({"module.PAN": ("Lab", "440W"), "inverter.OND": ("Lab", "50kW")})
        store = self.projects(self.variants({"Template": {"variants": ["VC0"], "VC0": template_spec()}}, components), {"Template"})
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        with self.electrical:
            result = discover_design_inputs(store, module_manufacturer="Lab", module_model="440W", inverter_manufacturer="Lab", inverter_model="50kW")
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")), before)
PAN_TEXT = 'PVObject_=pvModule\n  Comment=Generic\tMono 440W half-cell\tUser device\n  Version=8.1.6\n  Flags=$00\n\n  PVObject_Commercial=pvCommercial\n    Manufacturer=Generic\n    Model=Mono 440W half-cell\n    DataSource=Reference module\n  End of PVObject pvCommercial\n\n  Technol=mtSiMono\n  NCelS=72\n  NCelP=1\n  NDiode=3\n  PNom=440.0\n  GRef=1000.0\n  TRef=25.0\n  Absorb=0.900\n  VMaxIEC=1500\n  VMaxUL=1000\n  ISC=11.050\n  Voc=49.600\n  Imp=10.450\n  Vmp=42.100\n  MuISC=0.005500\n  muVocSpec=-0.148500\nEnd of PVObject pvModule\n'
OND_TEXT = 'PVObject_=pvGInverter\n  Comment=Solis S6-EH3P50K-H\t50 kW three-phase hybrid inverter\tUser device from flyer\n  Version=8.1.6\n  Flags=$0570\n\n  PVObject_Commercial=pvCommercial\n    Manufacturer=Solis\n    Model=S6-EH3P50K-H\n    DataSource=Solis flyer\n  End of PVObject pvCommercial\n\n  Transfo=Without\n\n  Converter=TConverter\n    PNomConv=50.000\n    PMaxOUT=50.000\n    VOutConv=400.0\n    VMppMin=150\n    VMPPMax=850\n    VAbsMax=1000\n    IDCMax=0.000\n    ModeOper=MPPT\n    CompPMax=Lim\n    CompVMax=Lim\n  End of TConverter\n  NbInputs=4\n  NbMPPT=4\nEnd of PVObject pvGInverter\n'
def base_request(**overrides):
    request = {"manufacturer_model": "Generic Mono 440W half-cell", "target_dc_kw": 21.6, "max_dc_ac_ratio": 1.3,
        "voltage_standard": "IEC", "temperatures": {"min_ambient_c": -10.0, "min_ambient_basis": "IEC 62548-1 lowest temperature observed on site",
            "min_module_operating_c": 20.0, "max_module_operating_c": 60.0}, "coefficients": {
            "beta_voc": {"value": -0.25, "unit": "pct_per_degC", "source": "manufacturer datasheet 2024"},
            "beta_vmp": {"value": -0.30, "unit": "pct_per_degC", "source": "manufacturer datasheet 2024"},
            "alpha_isc": {"value": 0.045, "unit": "pct_per_degC", "source": "manufacturer datasheet 2024"}},
        "module_limits": {"max_operating_current_a": {"value": 12.0, "source": "manufacturer maximum operating current"},
            "max_short_circuit_current_a": {"value": 12.6, "source": "manufacturer maximum short-circuit current"}},
        "mppt_limits": {"max_operating_current_a": {"value": 32.0, "source": "inverter datasheet per-MPPT rating"}, "max_short_circuit_current_a": {"value": 40.0, "source": "inverter datasheet per-MPPT rating"}},
        "effective_irradiance": {"max_irradiance_w_m2": 1100.0, "rear_gain_factor": 1.0, "source": "manufacturer maximum irradiance condition"}}
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
        with self.subTest(case="coefficient_mode"):
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
            for name in CAPACITY_NUMBERS: self.assertIsInstance(capacity[name], (int, float))
        with self.subTest(case="explicit_bare_envelope"):
            bare = self.prepare(base_request(coefficients=None, envelope={"cold_voc_v": 56.0, "cold_vmp_v": 45.0, "hot_vmp_v": 38.0}))
            self.assertEqual(bare["status"], "ready", bare["issues"])
            self.assertEqual(bare["envelope_mode"], "explicit")
            self.assertEqual((bare["capacity"]["cold_voc_v"], bare["capacity"]["cold_vmp_v"], bare["capacity"]["hot_vmp_v"]), (56.0, 45.0, 38.0))
            self.assertIn("envelope.cold_voc_v.source", warnings_of(bare))
        with self.subTest(case="explicit_unit_envelope"):
            explicit = self.prepare(base_request(coefficients=None,
                envelope={"cold_voc_v": {"value": 56.0, "unit": "V", "source": "datasheet"}, "cold_vmp_v": {"value": 45.0, "source": "datasheet"},
                          "hot_vmp_v": 38.0}, mppt_limits={"max_operating_current_a": {"value": 32.0, "unit": "A", "source": "datasheet"}, "max_short_circuit_current_a": {"value": 40.0, "source": "datasheet"}}))
            self.assertEqual(explicit["status"], "ready", explicit["issues"])
            self.assertEqual((explicit["capacity"]["cold_voc_v"], explicit["capacity"]["cold_vmp_v"], explicit["capacity"]["mppt_max_short_circuit_current_a"]), (56.0, 45.0, 40.0))
    def test_current_limit_derivation_and_irradiance_requirements(self):
        with self.subTest(case="imp_without_coefficient_requires_direct_limit"):
            req_imp = base_request()
            del req_imp["module_limits"]["max_operating_current_a"]
            res_imp = self.prepare(req_imp)
            self.assertEqual(res_imp["status"], "blocked")
            self.assertIn("module_limits.max_operating_current_a", issues_of(res_imp))
        with self.subTest(case="isc_coefficient_derives_short_circuit_not_operating"):
            req_isc = base_request()
            del req_isc["module_limits"]["max_short_circuit_current_a"]
            res_isc = self.prepare(req_isc)
            self.assertEqual(res_isc["status"], "ready", res_isc["issues"])
            expected_isc = 11.05 * (1100.0 / 1000.0) * (1 + 0.00045 * 35)
            self.assertAlmostEqual(res_isc["capacity"]["module_max_short_circuit_current_a"], expected_isc, places=9)
            self.assertEqual(res_isc["capacity"]["module_max_operating_current_a"], 12.0)
        with self.subTest(case="direct_limits_do_not_require_irradiance"):
            req_direct = base_request()
            del req_direct["effective_irradiance"]
            res_direct = self.prepare(req_direct)
            self.assertEqual(res_direct["status"], "ready", res_direct["issues"])
            self.assertEqual(res_direct["capacity"]["module_max_short_circuit_current_a"], 12.6)
            self.assertEqual(res_direct["capacity"]["module_max_operating_current_a"], 12.0)
        for key in ("max_irradiance_w_m2", "rear_gain_factor"):
            with self.subTest(case="coefficient_requires_irradiance", missing=key):
                req_irr = base_request()
                del req_irr["module_limits"]["max_short_circuit_current_a"]
                del req_irr["effective_irradiance"][key]
                res_irr = self.prepare(req_irr)
                self.assertEqual(res_irr["status"], "blocked")
                self.assertIn(f"effective_irradiance.{key}", issues_of(res_irr))
    def test_temperature_maximum_follows_the_coefficient_sign(self):
        cases = (("cold_isc", "max_short_circuit_current_a", "alpha_isc", -0.05, "module_max_short_circuit_current_a", 11.05 * 1.1 * (1 - 0.0005 * (20 - 25))),
            ("hot_imp", "max_operating_current_a", "alpha_imp", 0.02, "module_max_operating_current_a", 10.45 * 1.1 * (1 + 0.0002 * (60 - 25))),
            ("cold_imp", "max_operating_current_a", "alpha_imp", -0.03, "module_max_operating_current_a", 10.45 * 1.1 * (1 - 0.0003 * (20 - 25))))
        for label, limit_key, coeff_key, coeff_val, cap_key, expected in cases:
            with self.subTest(case=label):
                request = base_request()
                del request["module_limits"][limit_key]
                request["coefficients"][coeff_key] = {"value": coeff_val, "unit": "pct_per_degC", "source": "test only"}
                result = self.prepare(request)
                self.assertEqual(result["status"], "ready", result["issues"])
                self.assertAlmostEqual(result["capacity"][cap_key], expected, places=9)
    def test_temperature_warnings_and_order_guards(self):
        with self.subTest(case="vmp_coverage_warning"):
            req_vmp = base_request()
            req_vmp["temperatures"] = dict(req_vmp["temperatures"], min_module_operating_c=30.0)
            res_vmp = self.prepare(req_vmp)
            self.assertEqual(res_vmp["status"], "ready", res_vmp["issues"])
            self.assertLess(res_vmp["capacity"]["cold_vmp_v"], 42.1)
            self.assertIn("temperatures", warnings_of(res_vmp))
            self.assertTrue(any("Vmp coverage check" in warning["message"] for warning in res_vmp["warnings"]))
        for label, updates, expected_warning in (("annual_equal_design", {"annual_min_ambient_c": -10.0}, "temperatures.annual_min_ambient_c"),
                ("operating_equals_ambient", {"min_module_operating_c": -10.0}, "temperatures.min_module_operating_c"),
                ("reference_outside_band", {"min_module_operating_c": 20.0, "max_module_operating_c": 20.0}, "temperatures")):
            with self.subTest(case=label):
                temperatures = dict(base_request()["temperatures"], **updates)
                result = self.prepare(base_request(temperatures=temperatures))
                self.assertEqual(result["status"], "ready", result["issues"])
                self.assertIn(expected_warning, warnings_of(result))
        missing_design = dict(base_request()["temperatures"])
        del missing_design["min_ambient_c"]
        missing_design["annual_min_ambient_c"] = -5.0
        bad_order = dict(base_request()["temperatures"], min_module_operating_c=70.0)
        for label, temperatures, field in (("annual_not_design", missing_design, "temperatures.min_ambient_c"), ("min_above_max", bad_order, "temperatures")):
            with self.subTest(case=label):
                result = self.prepare(base_request(temperatures=temperatures))
                self.assertEqual(result["status"], "blocked")
                self.assertIn(field, issues_of(result))
    def test_missing_fields_units_consistency_and_unknown_key_guards(self):
        with self.subTest(case="missing_coefficients_and_envelope"):
            missing_v = self.prepare(base_request(coefficients=None, envelope=None))
            self.assertEqual(missing_v["status"], "blocked")
            self.assertIsNone(missing_v["capacity"])
            self.assertIn("coefficients.beta_voc", issues_of(missing_v))
            self.assertIn("coefficients.beta_vmp", issues_of(missing_v))
        for label, key, value, field in (("positive_beta_voc", "beta_voc", {"value": 0.25, "unit": "pct_per_degC", "source": "typo"}, "coefficients.beta_voc"),
                ("unknown_unit", "beta_voc", {"value": -0.25, "unit": "percent", "source": "ambiguous"}, "coefficients.beta_voc.unit"),
                ("missing_source", "beta_vmp", {"value": -0.30, "unit": "pct_per_degC"}, "coefficients.beta_vmp.source")):
            with self.subTest(case=label):
                request = base_request()
                request["coefficients"][key] = value
                result = self.prepare(request)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(field, issues_of(result))
        with self.subTest(case="mppt_limits_ignore_idcmax"):
            positive = electrical_inventory("OND", OND_TEXT.replace("IDCMax=0.000", "IDCMax=150.000"))
            self.assertEqual(positive["fields"]["IDCMax"]["status"], "declared")
            res_idc = prepare_design_envelope(self.module, positive, base_request(mppt_limits=None))
            self.assertEqual(res_idc["status"], "blocked")
            self.assertIn("mppt_limits.max_operating_current_a", issues_of(res_idc))
            self.assertIn("mppt_limits.max_short_circuit_current_a", issues_of(res_idc))
            self.assertTrue(all("IDCMax" in issue["message"] for issue in res_idc["issues"]))
        for label, section, key, value in (("isc_below_operating", "mppt_limits", "max_short_circuit_current_a", 30.0), ("operating_below_imp", "module_limits", "max_operating_current_a", 9.0)):
            with self.subTest(case=label):
                request = base_request()
                request[section][key] = {"value": value, "source": "datasheet"}
                result = self.prepare(request)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(f"{section}.{key}", issues_of(result))
        for label, pan_text, field in (("missing_voc", PAN_TEXT.replace("  Voc=49.600\n", ""), "module.Voc"), ("missing_vmax", PAN_TEXT.replace("  VMaxIEC=1500\n", "").replace("  VMaxUL=1000\n", ""), "module.VMaxIEC")):
            with self.subTest(case=label):
                module = electrical_inventory("PAN", pan_text)
                result = prepare_design_envelope(module, self.inverter, base_request())
                self.assertEqual(result["status"], "blocked")
                self.assertIn(field, issues_of(result))
        bad_limit = base_request()
        bad_limit["mppt_limits"]["max_operating_current_a"] = {"value": 40000, "unit": "mA", "source": "datasheet"}
        bad_envelope = base_request(coefficients=None, envelope={"cold_voc_v": {"value": 65, "unit": "kV", "source": "datasheet"}, "cold_vmp_v": 45.0, "hot_vmp_v": 38.0})
        bad_extra_limit = base_request()
        bad_extra_limit["module_limits"]["max_short_circuit_current_a"] = {"value": 12.6, "source": "datasheet", "converted": True}
        bad_extra_coeff = base_request()
        bad_extra_coeff["coefficients"]["beta_voc"] = {"value": -0.25, "unit": "pct_per_degC", "source": "ds", "note": "y"}
        bad_top = base_request(cold_voc_v=56.0)
        for label, request, field in (("limit_mA", bad_limit, "mppt_limits.max_operating_current_a.unit"), ("envelope_kV", bad_envelope, "envelope.cold_voc_v.unit"),
                ("limit_extra", bad_extra_limit, "module_limits.max_short_circuit_current_a.converted"),
                ("coeff_extra", bad_extra_coeff, "coefficients.beta_voc.note"), ("top_level_extra", bad_top, "cold_voc_v")):
            with self.subTest(case=label):
                result = self.prepare(request)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(field, issues_of(result))
SOURCE_PAN_TEXT = 'PVObject_=pvModule\n  Comment=Generic\tMono 440W half-cell\tUser device\n  Version=8.1.6\n  Flags=$00\n\n  PVObject_Commercial=pvCommercial\n    Manufacturer=Generic\n    Model=Mono 440W half-cell\n  End of PVObject pvCommercial\n\n  Technol=mtSiMono\n  NCelS=72\n  NCelP=1\n  NDiode=3\n  PNom=440.0\n  GRef=1000.0\n  TRef=25.0\n  Absorb=0.900\n  VMaxIEC=1500\n  VMaxUL=1000\n  ISC=11.050\n  Voc=49.600\n  Imp=10.450\n  Vmp=42.100\n  MuISC=0.005500\n  muVocSpec=-0.148500\nEnd of PVObject pvModule\n'
SOURCE_OND_TEXT = 'PVObject_=pvGInverter\n  Comment=Solis S6-EH3P50K-H\t50 kW three-phase hybrid inverter\tUser device\n  Version=8.1.6\n  Flags=$0570\n\n  PVObject_Commercial=pvCommercial\n    Manufacturer=Solis\n    Model=S6-EH3P50K-H\n  End of PVObject pvCommercial\n\n  Converter=TConverter\n    PNomConv=50.000\n    PMaxOUT=50.000\n    VMppMin=150\n    VMPPMax=850\n    VAbsMax=1000\n    IDCMax=0.000\n    ModeOper=MPPT\n    CompPMax=Lim\n    CompVMax=Lim\n  End of TConverter\n  NbInputs=4\n  NbMPPT=4\nEnd of PVObject pvGInverter\n'
IDENTITY = "Generic Mono 440W half-cell / Solis S6-EH3P50K-H"
MODULE_DOC = 'Generic Mono 440W half-cell module datasheet (declared extract)\nModels covered: Generic Mono 440W half-cell, Generic Mono 450W half-cell\nMaximum operating current: 14.5 A\nMaximum short circuit current: 16.0 A\nMaximum series fuse rating: 20 A\nMaximum system voltage: 1500 VDC\nModule operating temperature range: -40 °C\n'
COEFFICIENT_DOC = """Generic Mono 440W half-cell module datasheet (declared extract)
Temperature Coefficients of Voc: -0.25%/℃
Temperature Coefficients of Vmp: -0.30%/℃
Temperature Coefficients of Isc: 0.046%/℃
"""
ENVELOPE_DOC = 'Generic Mono 440W half-cell module envelope (declared extract)\nCold Voc envelope at minimum temperature: 56.0 V\nCold Vmp envelope at minimum operating temperature: 45.0 V\nHot Vmp envelope at maximum operating temperature: 38.0 V\nCold Voc envelope in kV units: 56.0 kV\nCold Voc envelope exponent form: 1e16 A\nCold Voc envelope identifier form: x16 A\nCold Voc envelope typographic minus form: −16 A\n'
SITE_DOC = 'Wanaluwawa site design conditions (declared extract)\nApplicable module: Generic Mono 440W half-cell\nDesign minimum ambient temperature: -10 °C\nMinimum module operating temperature: 20 °C\nMaximum module operating temperature: 60 °C\nMaximum effective irradiance: 1100 W/m²\nRear gain factor: 1 ratio\nRear gain factor note: 1 %\nTarget DC power: 21.6 kW\n'
INVERTER_DOC = """Solis S6-EH3P50K-H inverter datasheet (declared extract)
Max. input current per MPPT: 40 A
Max. short circuit current per MPPT: 60 A
"""
def sources_fixture():
    return {"S1": {"kind": "manufacturer", "title": "Generic module datasheet", "locator": "workspace://docs/generic-440w.txt", "text": MODULE_DOC, "model": "Generic Mono 440W half-cell"},
        "S2": {"kind": "site", "title": "Wanaluwawa site conditions", "locator": "workspace://sites/wanaluwawa.txt", "text": SITE_DOC,
               "model": "Generic Mono 440W half-cell"}, "S3": {"kind": "manufacturer", "title": "Generic envelope extract",
               "locator": "workspace://docs/generic-440w-envelope.txt", "text": ENVELOPE_DOC, "model": "Generic Mono 440W half-cell"},
        "S4": {"kind": "manufacturer", "title": "Solis datasheet", "locator": "https://example.invalid/solis-s6-eh3p.txt", "text": INVERTER_DOC,
               "model": "S6-EH3P50K-H"}, "S5": {"kind": "manufacturer", "title": "Generic coefficient extract",
               "locator": "workspace://docs/generic-440w-coefficients.txt", "text": COEFFICIENT_DOC, "model": "Generic Mono 440W half-cell"}}
def declaration(field, value, unit, source_id, excerpt, value_text, unit_text, page=None):
    record = {"field": field, "value": value, "unit": unit, "source_id": source_id, "excerpt": excerpt, "source_value_text": value_text, "source_unit_text": unit_text}
    if page is not None: record["page"] = page
    return record
def declarations_fixture():
    return [declaration("temperatures.min_ambient_c", -10.0, "degC", "S2", "Design minimum ambient temperature: -10 °C", "-10", "°C", page=4),
        declaration("temperatures.min_module_operating_c", 20.0, "degC", "S2", "Minimum module operating temperature: 20 °C", "20", "°C"),
        declaration("temperatures.max_module_operating_c", 60.0, "degC", "S2", "Maximum module operating temperature: 60 °C", "60", "°C"),
        declaration("envelope.cold_voc_v", 56.0, "V", "S3", "Cold Voc envelope at minimum temperature: 56.0 V", "56.0", "V", page=7),
        declaration("envelope.cold_vmp_v", 45.0, "V", "S3", "Cold Vmp envelope at minimum operating temperature: 45.0 V", "45.0", "V"),
        declaration("envelope.hot_vmp_v", 38.0, "V", "S3", "Hot Vmp envelope at maximum operating temperature: 38.0 V", "38.0", "V"),
        declaration("module_limits.max_operating_current_a", 14.5, "A", "S1", "Maximum operating current: 14.5 A", "14.5", "A"),
        declaration("module_limits.max_short_circuit_current_a", 16.0, "A", "S1", "Maximum short circuit current: 16.0 A", "16.0", "A"),
        declaration("mppt_limits.max_operating_current_a", 40.0, "A", "S4", "Max. input current per MPPT: 40 A", "40", "A", page=2),
        declaration("mppt_limits.max_short_circuit_current_a", 60.0, "A", "S4", "Max. short circuit current per MPPT: 60 A", "60", "A"),
        declaration("effective_irradiance.max_irradiance_w_m2", 1100.0, "W/m2", "S2", "Maximum effective irradiance: 1100 W/m²", "1100", "W/m²"),
        declaration("effective_irradiance.rear_gain_factor", 1.0, "ratio", "S2", "Rear gain factor: 1 ratio", "1", "ratio")]
def coefficient_declarations():
    records = [record for record in declarations_fixture() if not record["field"].startswith("envelope.")]
    records.extend([declaration("coefficients.beta_voc", -0.25, "pct_per_degC", "S5", "Temperature Coefficients of Voc: -0.25%/℃", "-0.25", "%/℃"),
        declaration("coefficients.beta_vmp", -0.30, "pct_per_degC", "S5", "Temperature Coefficients of Vmp: -0.30%/℃", "-0.30", "%/℃")])
    return records
def design_fixture():
    return {"target_dc_kw": 21.6, "max_dc_ac_ratio": 1.3, "capacity_tolerance_pct": 5, "voltage_standard": "IEC"}
class SourceInputTests(unittest.TestCase):
    def setUp(self):
        self.module = electrical_inventory("PAN", SOURCE_PAN_TEXT)
        self.inverter = electrical_inventory("OND", SOURCE_OND_TEXT)
    def call(self, sources=None, declarations=None, design=None, identity=IDENTITY):
        return prepare_sourced_design(self.module, self.inverter, manufacturer_model=identity,
            sources=sources_fixture() if sources is None else sources, declarations=declarations_fixture() if declarations is None else declarations,
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
        plain = prepare_design_envelope(self.module, self.inverter,
            {"manufacturer_model": IDENTITY, "target_dc_kw": 21.6, "max_dc_ac_ratio": 1.3, "capacity_tolerance_pct": 5, "voltage_standard": "IEC",
             "temperatures": {"min_ambient_c": -10.0, "min_module_operating_c": 20.0, "max_module_operating_c": 60.0, "min_ambient_basis": "x" * 10},
             "envelope": {"cold_voc_v": {"value": 56.0, "unit": "V", "source": "S3"}, "cold_vmp_v": {"value": 45.0, "unit": "V", "source": "S3"}, "hot_vmp_v": {"value": 38.0, "unit": "V", "source": "S3"}},
             "module_limits": {"max_operating_current_a": {"value": 14.5, "source": "S1"}, "max_short_circuit_current_a": {"value": 16.0, "source": "S1"}},
             "mppt_limits": {"max_operating_current_a": {"value": 40.0, "source": "S4"}, "max_short_circuit_current_a": {"value": 60.0, "source": "S4"}}})
        self.assertEqual(plain["status"], "ready", plain["issues"])
        for key in ("cold_voc_v", "cold_vmp_v", "hot_vmp_v", "module_max_operating_current_a", "mppt_max_short_circuit_current_a"):
            self.assertEqual(result["capacity"][key], plain["capacity"][key])
    def test_cited_coefficients_and_compact_quote_formats_are_accepted(self):
        base = self.call(declarations=coefficient_declarations())
        self.assertEqual(base["status"], "ready", base["issues"])
        self.assertEqual(base["electrical_design"]["coefficients"]["beta_voc"]["unit"], "pct_per_degC")
        self.assertAlmostEqual(base["capacity"]["cold_voc_v"], 49.6 * (1 + 0.0025 * 35), places=9)
        for label, leading_only in (("no_space_quote", False), ("quote_starts_at_number", True)):
            with self.subTest(case=label):
                records = coefficient_declarations()
                records.append(declaration("coefficients.alpha_isc", 0.046, "pct_per_degC", "S5", "Temperature Coefficients of Isc: 0.046%/℃", "0.046", "%/℃"))
                if leading_only:
                    for record in records:
                        if record["field"].startswith("coefficients."): record["excerpt"] = record["excerpt"].split(": ")[-1]
                result = self.call(declarations=records)
                self.assertEqual(result["status"], "ready", result["issues"])
                coefficients = result["electrical_design"]["coefficients"]
                self.assertEqual((coefficients["beta_voc"]["value"], coefficients["beta_voc"]["unit"], coefficients["alpha_isc"]["value"]), (-0.25, "pct_per_degC", 0.046))
                self.assertEqual(result["electrical_design"]["mppt_limits"]["max_operating_current_a"]["unit"], "A")
                quoted = [item for item in result["provenance"] if item["field"].startswith("coefficients.")]
                self.assertEqual(len(quoted), 3)
                self.assertTrue(all(item["reference_match"] for item in quoted))
    def test_provenance_records_shape_and_normalized_excerpt(self):
        result = self.call()
        keys = {"field", "sourceID", "title", "locator", "model", "page", "excerpt", "value", "unit", "source_value_text", "source_unit_text", "source_text_head", "kind", "reference_match"}
        self.assertTrue(result["provenance"])
        for record in result["provenance"]:
            self.assertEqual(set(record), keys)
            self.assertLessEqual(len(record["source_text_head"]), 160)
            self.assertNotIn("\n", record["excerpt"])
        pages = [record["page"] for record in result["provenance"] if record["page"] is not None]
        self.assertIn(4, pages)
    def test_excerpt_must_be_a_contiguous_fragment(self):
        cases = {"absent": "Cold Voc envelope published elsewhere: 56.0 V", "split": "56.0 V Cold Voc envelope at minimum temperature"}
        for label, excerpt in cases.items():
            records = declarations_fixture()
            records[3]["excerpt"] = excerpt
            result = self.call(declarations=records)
            with self.subTest(case=label):
                self.assertEqual(result["status"], "blocked")
                self.assertIn("envelope.cold_voc_v", issues_of(result))
                self.assertFalse([record for record in result["provenance"] if record["field"] == "envelope.cold_voc_v"][0]["reference_match"])
    def test_tokens_and_units_cannot_be_silently_changed(self):
        cases = (("value_not_equal", "envelope.cold_voc_v", 56.0, "V", "Cold Voc envelope at minimum temperature: 560 V", "56.0", "V"),
            ("token_inside_decimal", "envelope.cold_voc_v", 16.0, "V", "Cold Voc envelope at minimum temperature: 16.0 V", "16", "V"),
            ("token_inside_larger_integer", "envelope.cold_voc_v", 16.0, "V", "Cold Voc envelope at minimum temperature: 160 V", "16", "V"),
            ("token_after_sign", "temperatures.min_ambient_c", 40.0, "degC", "Module operating temperature range: -40 °C", "40", "°C"),
            ("unit_from_milliamp", "mppt_limits.max_operating_current_a", 40.0, "A", "Max. input current per MPPT: 40 mA", "A", "A"),
            ("unit_from_kilovolt", "envelope.cold_voc_v", 56.0, "V", "Cold Voc envelope in kV units: 56.0 kV", "V", "V"),
            ("unit_inside_word", "envelope.cold_voc_v", 1500.0, "V", "Cold Voc envelope at minimum temperature: 1500 VDC", "1500", "V"),
            ("token_inside_exponent_literal", "envelope.cold_voc_v", 16.0, "V", "Cold Voc envelope exponent form: 1e16 A", "16", "A"),
            ("token_inside_identifier", "envelope.cold_voc_v", 16.0, "V", "Cold Voc envelope identifier form: x16 A", "16", "A"),
            ("token_after_typographic_minus", "envelope.cold_voc_v", 16.0, "V", "Cold Voc envelope typographic minus form: \u221216 A", "16", "A"),
            ("empty_ratio_unit_token", "effective_irradiance.rear_gain_factor", 1.0, "ratio", "Rear gain factor: 1 ratio", "1", ""))
        for label, field, value, declared_unit, excerpt, value_token, unit_token in cases:
            records = declarations_fixture()
            target = [record for record in records if record["field"] == field][0]
            target.update(value=value, unit=declared_unit, excerpt=excerpt, source_value_text=value_token, source_unit_text=unit_token)
            result = self.call(declarations=records)
            with self.subTest(case=label):
                self.assertEqual(result["status"], "blocked")
                self.assertIn(field, issues_of(result))
    def test_unit_alias_is_explicit_and_never_rescaled(self):
        cases = (("percent_per_degree", "%/℃", "pct_per_degC", "coefficients.beta_voc", None),
            ("milliamp_not_amp", "mA", "A", "mppt_limits.max_operating_current_a", "Max. input current per MPPT: 40 mA"),
            ("kilovolt_not_volt", "kV", "V", "envelope.cold_voc_v", "Cold Voc envelope in kV units: 56.0 kV"),
            ("percent_not_ratio", "%", "ratio", "effective_irradiance.rear_gain_factor", "Rear gain factor note: 1 %"))
        for label, unit_token, declared_unit, field, excerpt in cases:
            records = coefficient_declarations() if field == "coefficients.beta_voc" else declarations_fixture()
            target = [record for record in records if record["field"] == field][0]
            target["source_unit_text"] = unit_token
            if excerpt is not None: target["excerpt"] = excerpt
            result = self.call(declarations=records)
            with self.subTest(case=label):
                if label == "percent_per_degree": self.assertEqual(result["status"], "ready", result["issues"])
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
        cases = {"design_string": {"design": dict(design_fixture(), target_dc_kw="bad")}, "design_null": {"design": dict(design_fixture(), target_dc_kw=None)},
            "design_huge_int": {"design": dict(design_fixture(), target_dc_kw=10 ** 400)}, "design_mixed_keys": {"design": mixed},
            "authority_text_list": {"override_text": ["not", "text"]}, "identity_none": {"identity": None}, "identity_huge": {"identity": "M" * 201}}
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                if "override_text" in kwargs:
                    sources = sources_fixture()
                    sources["S1"]["text"] = kwargs["override_text"]
                    result = self.call(sources=sources)
                elif "identity" in kwargs: result = self.call(identity=kwargs["identity"])
                else: result = self.call(design=kwargs["design"])
                self.assertEqual(result["status"], "blocked")
                self.assertTrue(result["issues"])
                if "design" in kwargs and isinstance(kwargs["design"], dict): self.assertNotEqual(result["electrical_design"].get("target_dc_kw"), "bad")
    def test_schema_value_types_ids_and_control_chars_are_rejected(self):
        cases = {}
        for label, idx, key, val in (("declaration_unknown_key", 3, "extra", 1), ("non_positive_page", 3, "page", 0), ("boolean_value", 3, "value", True), ("non_finite_value", 3, "value", float("inf"))):
            records = declarations_fixture()
            records[idx][key] = val
            cases[label] = ({"declarations": records}, "")
        for label, sid, key, val, msg in (("unknown_source_key", "S1", "note", "x", ""),
                ("unknown_source_kind", "S1", "kind", "vendor", ""), ("locator_too_long", "S2", "locator", "L" * 2001, ""),
                ("control_chars", "S1", "text", MODULE_DOC.replace("Maximum operating", "Maximum\x00operating"), "control characters")):
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
                if needle: self.assertTrue(any(needle in issue["message"] for issue in result["issues"]))
    def test_size_and_text_budgets_are_enforced(self):
        big_text = sources_fixture()
        big_text["S1"]["text"] = MODULE_DOC + "x" * (2 * 1024 * 1024)
        many_sources = sources_fixture()
        for index in range(12): many_sources["X" + str(index)] = dict(many_sources["S1"])
        long_excerpt = declarations_fixture()
        long_excerpt[3]["excerpt"] = "L" * 4001
        for label, kwargs, needle in (("text_budget", {"sources": big_text}, "exceeds"),
                ("too_many_sources", {"sources": many_sources}, None), ("long_excerpt", {"declarations": long_excerpt}, None),
                ("too_many_declarations", {"declarations": declarations_fixture() * 3}, None)):
            with self.subTest(case=label):
                result = self.call(**kwargs)
                self.assertEqual(result["status"], "blocked")
                if needle: self.assertTrue(any(needle in issue["message"] for issue in result["issues"]))
                elif label == "too_many_sources": self.assertIn("sources", issues_of(result))
    def test_missing_inputs_return_a_draft_instead_of_raising(self):
        records = [record for record in declarations_fixture() if record["field"].startswith(("mppt_limits", "module_limits"))]
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
        target.update(source_id="S1", excerpt="Module operating temperature range: -40 °C", source_value_text="-40", source_unit_text="°C", value=-40.0)
        r1 = self.call(sources=assumption_sources)
        self.assertEqual(r1["status"], "needs_review", r1["issues"])
        self.assertTrue(r1["needs_review"])
        self.assertEqual([r for r in r1["provenance"] if r["field"] == "temperatures.min_ambient_c"][0]["kind"], "assumption")
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
        cases = (("unmatched_declared_model", "S3", {"model": "Generic Mono 450W half-cell"}, "envelope.cold_voc_v", None),
            ("module_side_mismatch", "S3", {"model": "Generic Mono 450W half-cell", "text": ENVELOPE_DOC.replace("Generic Mono 440W half-cell module envelope", "Generic Mono 450W half-cell module envelope")},
             None, "selected identity"), ("inverter_side_mismatch", "S4", {"model": "S6-EH3P60K-H", "text": INVERTER_DOC.replace("S6-EH3P50K-H", "S6-EH3P60K-H")},
             "mppt_limits.max_operating_current_a", "selected identity"), ("partial_token_50K-H", "S4", {"model": "50K-H"}, None, "exact model token"),
            ("partial_token_S6", "S4", {"model": "S6"}, None, "exact model token"))
        for label, sid, updates, expected_field, expected_msg in cases:
            with self.subTest(case=label):
                sources = sources_fixture()
                sources[sid].update(updates)
                result = self.call(sources=sources)
                self.assertEqual(result["status"], "blocked")
                if expected_field: self.assertIn(expected_field, issues_of(result))
                if expected_msg:
                    self.assertTrue(any(expected_msg in issue["message"] or "selected identity" in issue["message"] for issue in result["issues"]))
    def test_cited_engineering_goal_and_conflict_with_design(self):
        records = declarations_fixture()
        records.append(declaration("target_dc_kw", 21.6, "kW", "S2", "Target DC power: 21.6 kW", "21.6", "kW"))
        conflict = self.call(declarations=records)
        self.assertEqual(conflict["status"], "blocked")
        self.assertIn("target_dc_kw", issues_of(conflict))
        design = design_fixture()
        del design["target_dc_kw"]
        ready = self.call(declarations=records, design=design)
        self.assertEqual(ready["status"], "ready", ready["issues"])
        self.assertEqual(ready["electrical_design"]["target_dc_kw"], 21.6)
class SuggestSizingTests(unittest.TestCase):
    def setUp(self):
        from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN
        from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = init_workspace(Path(tmp.name).resolve() / EXPECTED_WORKSPACE_BASENAME)
        write_utf8_crlf(self.root / "Sites" / "Geneva_MN90.SIT", SITE_TEXT)
        write_utf8_crlf(self.root / "Meteo" / "Geneva_MN90_SYN.MET", MET_TEXT)
        write_utf8_crlf(self.root / "ComposPV/PVmodules/Lab440.PAN", UTILITY_PAN)
        write_utf8_crlf(self.root / "ComposPV/Inverters/Lab50.OND", UTILITY_OND)
        self.components = ComponentStore(self.root)
        self.store = VariantStore(self.root, self.components)
    def test_suggest_sizing_derives_realistic_dc_ac_and_drop_in_capacity(self):
        res = suggest_sizing(self.store, pan_name="Lab440.PAN", ond_name="Lab50.OND", target_dc_kw=105.0)
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
        bad_temp = suggest_sizing(self.store, pan_name="Lab440.PAN", ond_name="Lab50.OND", target_dc_kw=100.0, min_ambient_c=30.0, min_operating_c=10.0)
        self.assertEqual(bad_temp["status"], "blocked")
    def test_simulate_design_auto_from_components_marks_needs_review_with_assumptions(self):
        from tests.unit.core.test_bifacial_utility import _FakeUtilityCLI
        cli = _FakeUtilityCLI(self.root, year=2024)
        fake_cal = {"issues": [], "warnings": [], "year": 2024, "rows": 8784}
        # Reject combining auto_from_components with explicit capacity
        bad = simulate_design(cli, self.store, spec={"scope": "ground_tracker", "sizing_mode": "auto_from_components",
            "new_project": "Conflict.PRJ", "year": 2024, "target_dc_kw": 105.0, "module": {"file_name": "Lab440.PAN"}, "inverter": {"file_name": "Lab50.OND"},
            "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"}, "capacity": {"target_dc_kw": 105.0},}, dry_run=True)
        self.assertEqual((bad["status"], bad["stage"]), ("blocked", "spec"))
        # Valid auto_from_components utility tracker + bifacial run
        req = SingleDesignRequest(scope="ground_tracker", sizing_mode="auto_from_components", new_project="AutoTracker.PRJ",
            year=2024, target_dc_kw=105.0, module={"file_name": "Lab440.PAN"}, inverter={"file_name": "Lab50.OND"},
            weather={"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            ground_layout={"model_mode": "3d", "pitch_m": 12.0, "shed_count": 6, "module_columns": 20},
            bifacial={"ground_albedo": 0.30, "bifaciality_factor": 0.80, "height_above_ground_m": 2.4})
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
        self.assertIn("declares a `dry_run` parameter", SERVER_INSTRUCTIONS)
        self.assertIn("pvsyst_build_sfi", SERVER_INSTRUCTIONS)
        self.assertIn("auto_from_components", WORKFLOW_GUIDE_MD)
        vguide = _variables_guide_md()
        self.assertIn("bifacial", vguide)
        self.assertIn("viGlobBak", vguide)
    def test_suggest_sizing_blocks_asymmetric_mppt_limits_and_caps_uniform_isc(self):
        from tests.unit.core.test_bifacial_utility import UTILITY_OND
        unbalanced_ond = UTILITY_OND.replace("  NbMPPT=2\n", "  NbMPPT=2\n  NbInputsMain=1\n  NbInputsSec=1\n  IscMaxMain=20.0\n  IscMaxSec=10.0\n")
        write_utf8_crlf(self.root / "ComposPV/Inverters/Unbalanced.OND", unbalanced_ond)
        blocked = suggest_sizing(self.store, pan_name="Lab440.PAN", ond_name="Unbalanced.OND", target_dc_kw=8.8)
        self.assertEqual(blocked["status"], "blocked", blocked)
        self.assertTrue(any(item["field"] == "mppt_limits" and "IscMaxMain" in item["message"] and "IscMaxSec" in item["message"] for item in blocked["issues"]), blocked["issues"])
        # Uniform declared IscMax=10 A on both MPPTs is not asymmetric, but still caps short-circuit current.
        low_isc_ond = UTILITY_OND.replace("  NbMPPT=2\n", "  NbMPPT=2\n  IscMaxMain=10.0\n  IscMaxSec=10.0\n")
        write_utf8_crlf(self.root / "ComposPV/Inverters/LowIsc.OND", low_isc_ond)
        capped = suggest_sizing(self.store, pan_name="Lab440.PAN", ond_name="LowIsc.OND", target_dc_kw=100.0)
        self.assertEqual(capped["status"], "blocked", capped)
        self.assertTrue(any(item["field"] == "current_window" for item in capped["issues"]), capped["issues"])
    def test_storage_prompt_matches_peak_shaving_and_load_strategy_contracts(self):
        from pvsyst_auto.api.context import register_guidance
        prompts = {}
        class _StubServer:
            def prompt(self, *, name, description):
                def decorator(fn):
                    prompts[name] = fn
                    return fn
                return decorator
            def resource(self, uri, **_):
                def decorator(fn):
                    return fn
                return decorator
        register_guidance(_StubServer())
        fn = prompts["design_pv_with_storage"]
        peak = fn(project="Example.PRJ")
        self.assertIn("pvLoad", peak)
        self.assertIn("omit `load_manifest_name`", peak)
        self.assertNotIn("Attach a load profile", peak)
        for strat in ("self_consumption", "weak_grid"):
            text = fn(project="Example.PRJ", storage_strategy=strat)
            self.assertIn("pvsyst_prepare_load_profile", text)
            self.assertIn("load_manifest_name", text)
PAN_NAME = "Generic_Mono_300W.PAN"
OND_NAME = "Generic_7.5kW.OND"
NEW_PROJECT = "Seed_template.PRJ"
SEED_PRJ = 'PVObject_=pvProject\n  Comment=Residential DEMO\n  Version=8.1.0\n  Flags=$00080010\nMeteoFileName=Geneva_MN90_SYN.MET\nPVObject_SitePrj=pvSite\n  Comment=Geneva/Cointrin;Switzerland;Europe;\n  Flags=$0006083F\n  NomF=Geneva/Cointrin\n  Latitude=46.2383\n  Longitude=6.1094\n  Altitude=418\n  TimeZone=1.0\nEnd of PVObject pvSite\nEnd of PVObject pvProject\n'
SEED_VC0 = 'PVObject_=pvVCalcul\n  Comment=First simulation\n  Version=8.1.0\n  Flags=$00100001\n  PRCorrTemp=False\n\n  PVObject_SiteSimul=pvSite\n    Flags=$0006083F\n    NomF=Geneva/Cointrin\n    Latitude=46.2383\n    Longitude=6.1094\n  End of PVObject pvSite\n\n  Orientations, list of=1 TOrientGroup\n\n    PVObject_=pvOrient\n      Comment=Fixed, Tilt 25.0 deg\n      Version=8.1.0\n      Flags=$00\n    NoOrient=1\n    FieldType=FixedPlane\n    FieldTilt=25.0\n    FieldAzim=20.0\n    UsesTiltAver=False\n    UsesAzimAver=False\n    UsesSlopeAver=False\n    End of TOrientGroup\n  End of Orientations\n\n  PVObject_PVMainArray=pvMainArray\n    Flags=$00100400\n  UFactor=20.0\n  MVNbTransfos=1\n  PVObject_SystemCircuit=pvCircuit\n    Version=8.1.0\n    Flags=$00\n    BaseNode=Start\n      InjectionPointNode Start;\n        Children=Start\n          InverterNode Start;\n            SubArrayId=1\n            SubArrayName=PV Array\n            Children=Start\n              MPPTNode Start;\n                NElements=2\n                SubArrayId=1\n                SubArrayName=PV Array\n                Children=Start\n                  StringNode Start;\n                    SubArrayId=1\n                    SubArrayName=PV Array\n                  StringNode End;\n                Children=End\n              MPPTNode End;\n            Children=End\n          InverterNode End;\n        Children=End\n      InjectionPointNode End;\n    BaseNode=End\n  End of PVObject pvCircuit\n  SubArrays=Start\n    PVObject_=pvSubArray\n      Comment=PV Array\n      Version=8.1.0\n      Flags=$001E0002\n      Flags2=$0020\n      SubArrayId=1\n      PVModule=Generic_Mono_300W.PAN\n      GInverter=Generic_7.5kW.OND\n      NModSerie=15\n      NStringCh=2\n      NStrOrient1=2\n      NoOrientation=1\n      NInverter=1\n      NInvMPPT=1\n      SEIsHeterogeneousString=False,\n      PowerSharingInverterId=-1\n      PNomAC_InMPPT=7.50\n      PQualityModU=3.00\n      PQualityModI=3.00\n      PQualityModP=3.00\n      PMismMPP=2.00\n      PMismUFixe=2.50\n      LIDLoss=0.00\n    End of PVObject pvSubArray\n  EndTags\n  End of PVObject pvMainArray\n\n  PVObject_System=pvSystem\n    Flags=$00\n    SystemType=Grid\n  End of PVObject pvSystem\n\n  PVObject_Ombrage=pvShading\n    Comment=No shading scene defined\n    Flags=$00080000\n    Precision=1\n  End of PVObject pvShading\n\n  BatchSimul=TBatchSimul\n    Version=8.1.0\n    ParamsFileName=_DEMO_RESIDENTIAL_BatchParams_0.CSV\n    SaveMode=3\n  EndTags\nEnd of PVObject pvVCalcul\n'
_STD_PAN_TEXT = 'PVObject_=pvModule\n  Version=8.1.0\n  Flags=$00\n  Manufacturer=Generic\n  Model=Mono 300W\n  Technol=mtSiMono\n  NCelS=60\n  NCelP=1\n  NDiode=3\n  PNom=300\n  GRef=1000\n  TRef=25\n  Absorb=0.6\n  ISC=9.6\n  Voc=46\n  Imp=9.0\n  Vmp=38\n  MuISC=0.05\n  muVocSpec=-0.3\n  VMaxIEC=1000\n  VMaxUL=600\n  PVObject_Commercial=pvCommercial\n    Manufacturer=Generic\n    Model=Mono 300W\n    Width=1.00\n    Height=1.70\n  End of PVObject pvCommercial\nEnd of PVObject pvModule\n'
OND_TEMPLATE = 'PVObject_=pvGInverter\n  Version=8.1.0\n  Flags=$00\n  Manufacturer=Generic\n  Model=7.5kW\n  NbInputs={mppt}\n  NbMPPT={mppt}\n  Converter=TConverter\n    ModeOper=MPPT\n    CompPMax=Lim\n    CompVMax=Lim\n    PNomConv=7.50\n    PMaxOUT=8.25\n    VMppMin=200\n    VMPPMax=800\n    VAbsMax=1000\n    IDCMax=20\n  End of TConverter\n  ProfilPIO=TCubicProfile\n  End of TCubicProfile\nEnd of PVObject pvGInverter\n'
CAPACITY = {"target_dc_kw": 8.4, "max_dc_ac_ratio": 1.2, "cold_voc_v": 50, "hot_vmp_v": 30, "cold_vmp_v": 42, "module_max_operating_current_a": 10,
            "module_max_short_circuit_current_a": 12, "mppt_max_operating_current_a": 10, "mppt_max_short_circuit_current_a": 12, "design_min_temperature_c": -10,
            "design_min_operating_temperature_c": 0, "design_max_operating_temperature_c": 60, "voltage_standard": "IEC", "design_basis": "Fixture declared design envelope"}
def ond_text(mppt=4):
    return OND_TEMPLATE.format(mppt=mppt)
def crlf(text):
    return text.replace("\n", "\r\n")
def walk(node):
    yield node
    for child in node.children: yield from walk(child)
class FixtureComponents(ComponentStore):
    def _inspect(self, kind, data):
        format_name, text = self._text(kind, data)
        fields = self._fields(text or "")
        return {"type": kind, "format": format_name, "size": len(data), "manufacturer": fields.get("Manufacturer", [None])[0],
                "model": fields.get("Model", [None])[0], "fields": fields, "structural_errors": [], "warnings": [], "known_value_errors": []}
class _StdTemplateFakeCLI(FakeCliBase):
    def __init__(self, install, workspace, version="PVsystCLI 8.1.6 (build 1234)"):
        super().__init__(workspace, cli=install, version=version)
class StandardTemplateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.projects = self.root / "Projects"
        self.projects.mkdir()
        (self.root / "ComposPV" / "PVmodules").mkdir(parents=True)
        (self.root / "ComposPV" / "Inverters").mkdir(parents=True)
        self.install = self.root / "install"
        self.install.mkdir()
        (self.install / "PVsystCLI.exe").write_bytes(b"stub")
        self.seed_dir = self.install / "DataRO" / "PVsyst8.1_Data" / "Projects"
        self.seed_dir.mkdir(parents=True)
        self.seed_prj = self.seed_dir / "_DEMO_Residential.PRJ"
        self.seed_vc0 = self.seed_dir / "_DEMO_Residential.VC0"
        self.write_seed(crlf(SEED_PRJ), crlf(SEED_VC0))
        (self.root / "ComposPV" / "PVmodules" / PAN_NAME).write_bytes(crlf(_STD_PAN_TEXT).encode("utf-8"))
        (self.root / "ComposPV" / "Inverters" / OND_NAME).write_bytes(crlf(ond_text(4)).encode("utf-8"))
        self.cli = _StdTemplateFakeCLI(self.install / "PVsystCLI.exe", self.root)
        self.store = VariantStore(self.root, FixtureComponents(self.root))
    def write_seed(self, prj, vc0, prj_bom=False, vc0_bom=True):
        self.seed_prj.write_bytes((b"\xef\xbb\xbf" if prj_bom else b"") + prj.encode("utf-8"))
        self.seed_vc0.write_bytes((b"\xef\xbb\xbf" if vc0_bom else b"") + vc0.encode("utf-8"))
        self.seed_bytes = (self.seed_prj.read_bytes(), self.seed_vc0.read_bytes())
    def ond_electrical(self, mppt=4):
        return electrical_inventory("OND", crlf(ond_text(mppt)))
    def derived(self, mppt=4, **kwargs):
        return standard_seed_texts(self.cli, self.store, ond_electrical=self.ond_electrical(mppt), pan_name=PAN_NAME, ond_name=OND_NAME, **kwargs)
    def prepare(self, mppt=4, new_project=NEW_PROJECT, **kwargs):
        return prepare_standard_template(self.cli, self.store, ond_electrical=self.ond_electrical(mppt), pan_name=PAN_NAME, ond_name=OND_NAME, new_project=new_project, **kwargs)
    def structure(self, text):
        _, lines = self.store._text(text.encode("utf-8"))
        arrays, _ = self.store._sections(lines)
        return lines, arrays, grid_inventory(lines, arrays)
    def assert_seed_untouched(self):
        self.assertEqual((self.seed_prj.read_bytes(), self.seed_vc0.read_bytes()), self.seed_bytes)
    def assert_blocked(self, result, field):
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual([item["field"] for item in result["issues"]], [field], result)
    def test_dry_run_preview_references_and_capacity_check(self):
        result = self.prepare()
        self.assertEqual((result["status"], result["stage"], result["definition"]["variant"]), ("ready", "derived", SEED_VARIANT))
        self.assertEqual((result["source"]["version"], result["source"]["project_bom_preserved"], result["source"]["variant_bom_preserved"]), ("8.1.0", False, True))
        self.assertNotIn("prj_text", result)
        self.assertEqual(sorted(self.projects.iterdir()), [])
        references = [item for item in result["changes"] if item["field"] == "PVModule/GInverter"]
        self.assertEqual(references[0]["after"], f"{PAN_NAME}/{OND_NAME}")
        self.assertTrue(any("ParamsFileName" in warning for warning in result["warnings"]))
        self.assert_seed_untouched()
        cap_ok = self.prepare(capacity=CAPACITY)
        self.assertEqual((cap_ok["status"], cap_ok["capacity_plan"]["mppt_per_inverter"], cap_ok["capacity_plan"]["series_modules"]), ("ready", 4, 7))
        cap_bad = self.prepare(capacity={**CAPACITY, "module_max_operating_current_a": 8})
        self.assertEqual((cap_bad["status"], cap_bad["stage"]), ("blocked", "capacity"))
        self.assertEqual(sorted(self.projects.iterdir()), [])
    def test_installed_cli_and_seed_library_validation(self):
        for version in ("PVsystCLI 8.0.9", "PVsystCLI"):
            res = prepare_standard_template(_StdTemplateFakeCLI(self.install / "PVsystCLI.exe", self.root, version=version),
                VariantStore(self.root, FixtureComponents(self.root)), ond_electrical=self.ond_electrical(), pan_name=PAN_NAME, ond_name=OND_NAME, new_project=NEW_PROJECT)
            self.assert_blocked(res, "seed")
            self.assertIn("8.1", res["issues"][0]["message"])
        shutil.rmtree(self.install / "DataRO")
        self.assert_blocked(self.prepare(), "seed")
        (self.install / "DataRO" / "PVsyst8.0_Data" / "Projects").mkdir(parents=True)
        (self.install / "DataRO" / "PVsyst8.0_Data" / "Projects" / "_DEMO_Residential.PRJ").write_bytes(b"PVObject_=pvProject\nEnd of PVObject pvProject\n")
        old_res = self.prepare()
        self.assert_blocked(old_res, "seed")
        self.assertIn("8.0", old_res["issues"][0]["message"])
        shutil.rmtree(self.install / "DataRO" / "PVsyst8.0_Data")
        self.seed_dir.mkdir(parents=True)
        self.write_seed(crlf(SEED_PRJ), crlf(SEED_VC0))
        (self.install / "DataRO" / "PVsyst8.1.6_Data" / "Projects").mkdir(parents=True)
        ambig = self.prepare()
        self.assert_blocked(ambig, "seed")
        self.assertIn("ambiguous", ambig["issues"][0]["message"])
    def test_seed_structural_and_count_inconsistencies_block(self):
        for vc0, fragment in ((crlf(SEED_VC0).replace("  PVObject_Ombrage=pvShading", "  PVObject_ShdTable=pvShdTable\n  PVObject_Ombrage=pvShading"), "shading"),
            (crlf(SEED_VC0).replace("NInvMPPT=1", "NInvMPPT=2"), "NInvMPPT=1"), (crlf(SEED_VC0).replace("Flags=$001E0002", "Flags=$001E0402"), "ordinary NInvMPPT=1"),
            (crlf(SEED_VC0).replace("NStringCh=2", "NStringCh=3").replace("NStrOrient1=2", "NStrOrient1=3"), "counts"),
            (crlf(SEED_VC0).replace("PVObject_=pvVCalcul", "PVObject_=pvSubArray", 1), "pvVCalcul"),
            (crlf(SEED_VC0).replace("          InverterNode Start;\r\n            SubArrayId=1", "          InverterNode Start;\r\n            NElements=3\r\n            SubArrayId=1"),
             "implicit single counts are not rewritten")):
            self.write_seed(crlf(SEED_PRJ), vc0)
            res = self.prepare()
            self.assert_blocked(res, "seed")
            self.assertIn(fragment, res["issues"][0]["message"])
    def test_mppt_1_2_4_conversion_and_explicit_count_dedup(self):
        for inputs in (1, 2, 4):
            with self.subTest(mppt_inputs=inputs):
                derived = self.derived(mppt=inputs)
                text = derived["vc0_text"]
                self.assertIn("Flags=$001E0402\r\n", text)
                self.assertIn(f"NInvMPPT={inputs}\r\n", text)
                self.assertIn(f"NStringCh={inputs}\r\n", text)
                lines, _, checked = self.structure(text)
                self.assertTrue(checked["counts_valid"], checked)
                row = checked["subarrays"][0]
                self.assertEqual((row["declared"]["NInvMPPT"], row["explicit_mppt_inputs_match_declared"], row["circuit"]["explicit_mppt_inputs"], row["circuit"]["strings"],
                                  row["declared_module_count"]), (str(inputs), True, inputs, inputs, inputs * 15))
                groups = [node for root in circuit_nodes(lines) for node in walk(root) if node.kind == "InverterNode"]
                self.assertEqual((groups[0].one("NElements"), groups[0].children[0].one("NElements"), [leaf.one("NElements") for leaf in groups[0].children[0].children]), ("1", str(inputs), ["1"]))
        seeded = crlf(SEED_VC0).replace("          InverterNode Start;\r\n            SubArrayId=1", "          InverterNode Start;\r\n            NElements=1\r\n            SubArrayId=1")
        self.write_seed(crlf(SEED_PRJ), seeded)
        derived = self.derived(mppt=4)
        self.assertEqual(sum(1 for item in derived["changes"] if item["field"] == "NElements"), 1)
        self.assertNotIn("NElements=1\r\n            NElements=1", derived["vc0_text"])
    def test_synthesized_template_writes_power_per_mppt_input(self):
        result = self.prepare(mppt=4, dry_run=False, allow_synthesize=True)
        self.assertEqual(result["status"], "created", result)
        variant = self.projects / "Seed_template.VC0"
        _, _, checked = self.structure(variant.read_text(encoding="utf-8-sig"))
        declared = checked["subarrays"][0]["declared"]
        self.assertEqual((declared["NInvMPPT"], declared["PNomAC_InMPPT"]), ("4", "1.88"))
    def test_input_validation_missing_component_and_existing_destination(self):
        for kwargs, field in (({"new_project": "Seed.VC0"}, "new_project"), ({"pan_name": ""}, "pan_name"), ({"dry_run": "yes"}, "dry_run")):
            args = dict(ond_electrical=self.ond_electrical(), pan_name=PAN_NAME, ond_name=OND_NAME, new_project=NEW_PROJECT)
            args.update(kwargs)
            res = prepare_standard_template(self.cli, self.store, **args)
            self.assertEqual(res["status"], "blocked")
            self.assertIn(field, [item["field"] for item in res["issues"]])
        for val in (None, 0, 65, True):
            electrical = self.ond_electrical()
            if val is None: electrical["fields"].pop("NbMPPT")
            else: electrical["fields"]["NbMPPT"] = {"value": val, "unit": "count", "status": "declared", "path": "root", "source_key": "NbMPPT"}
            res = prepare_standard_template(self.cli, self.store, ond_electrical=electrical, pan_name=PAN_NAME, ond_name=OND_NAME, new_project=NEW_PROJECT)
            self.assert_blocked(res, "seed")
            self.assertIn("NbMPPT", res["issues"][0]["message"])
        existing = self.projects / NEW_PROJECT
        existing.write_bytes(b"PVObject_=pvProject\nEnd of PVObject pvProject\n")
        for dry_run in (True, False):
            res = self.prepare(dry_run=dry_run)
            self.assert_blocked(res, "new_project")
            self.assertIn("no file is overwritten", res["issues"][0]["message"])
        existing.unlink()
        (self.root / "ComposPV" / "Inverters" / OND_NAME).unlink()
        self.assert_blocked(self.prepare(), "seed")
        self.assertEqual(sorted(self.projects.iterdir()), [])
    def test_creation_bom_preservation_and_creation_error(self):
        result = self.prepare(dry_run=False)
        self.assertEqual((result["status"], result["stage"], sorted(result["created"]["files"])), ("created", "created", ["Seed_template.PRJ", "Seed_template.VC0"]))
        lines, _, checked = self.structure((self.projects / "Seed_template.VC0").read_text(encoding="utf-8-sig"))
        self.assertEqual((checked["counts_valid"], checked["subarrays"][0]["declared"]["NInvMPPT"], len(circuit_nodes(lines))), (True, "4", 1))
        for p in self.projects.iterdir(): p.unlink()
        zh_prj, zh_vc0 = "\u4f4f\u5b85\u793a\u8303\u9879\u76ee", "\u9996\u6b21\u6a21\u62df"
        for prj_bom, vc0_bom, name, non_ascii in ((True, True, "BothBom.PRJ", True), (False, True, "Vc0Bom.PRJ", False), (False, False, "NoBom.PRJ", False)):
            prj = crlf(SEED_PRJ).replace("Comment=Residential DEMO", f"Comment={zh_prj} DEMO" if non_ascii else "Comment=Residential DEMO")
            vc0 = crlf(SEED_VC0).replace("Comment=First simulation", f"Comment={zh_vc0} run" if non_ascii else "Comment=First simulation")
            self.write_seed(prj, vc0, prj_bom=prj_bom, vc0_bom=vc0_bom)
            derived = self.derived()
            self.assertEqual((derived["source"]["project_bom_preserved"], derived["source"]["variant_bom_preserved"]), (prj_bom, vc0_bom))
            res = self.prepare(new_project=name, dry_run=False)
            self.assertEqual(res["status"], "created")
            stem = Path(name).stem
            self.assertEqual((self.projects / name).read_bytes().startswith(b"\xef\xbb\xbf"), prj_bom)
            self.assertEqual((self.projects / f"{stem}.VC0").read_bytes().startswith(b"\xef\xbb\xbf"), vc0_bom)
            if non_ascii:
                self.assertIn(zh_prj, (self.projects / name).read_text(encoding="utf-8-sig"))
                self.assertIn(zh_vc0, (self.projects / f"{stem}.VC0").read_text(encoding="utf-8-sig"))
        shutil.rmtree(self.projects)
        failed = self.prepare(dry_run=False)
        self.assertEqual((failed["status"], failed["stage"]), ("failed", "create"))
        self.assertTrue(failed["error"])
        self.assert_seed_untouched()
if __name__ == "__main__": unittest.main()

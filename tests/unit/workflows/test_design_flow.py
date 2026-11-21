"""Unit tests for the unified single-project design flow."""
import copy
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pvsyst_auto.workflows.design as flow
from pvsyst_auto.engineering.sizing import NUMBERS as CAPACITY_NUMBERS
from pvsyst_auto.native.cli import PVsystError
from tests._fixtures import FakeCliBase


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

    def run_flow(self, spec_value, dry_run=True, *, discovery=None, envelope=None, download=None,
                 prepare=None, simulate=None, seed=None, cli=None):
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

    def test_spec_validation_scope_design_choice_and_strict_fields(self):
        before = self.snapshot()
        for scope in ("roof", "free_standing"):
            res, _ = self.run_flow(spec(scope=scope))
            self.assertEqual((res["status"], res["stage"]), ("blocked", "spec"))
            self.assertIn("fixed_unshaded_grid", next(i["message"] for i in res["issues"] if i["field"] == "scope"))
        unset, _ = self.run_flow(spec(scope=""))
        self.assertEqual((unset["status"], unset["stage"]), ("ready", "preflight"))
        for key in ("status", "stage", "dry_run", "execution_preflight_complete", "issues",
                    "warnings", "selected", "preview", "metrics", "artifacts", "recovery"):
            self.assertIn(key, unset)

        missing, mocks = self.run_flow(spec(capacity=None))
        self.assertEqual((missing["status"], missing["stage"], mocks["discover_design_inputs"].call_count),
                         ("blocked", "spec", 0))
        self.assertEqual(self.snapshot(), before)

        both, _ = self.run_flow(spec(electrical_design={"temperatures": {}}))
        self.assertIn("mutually exclusive",
                      next(i["message"] for i in both["issues"] if i["field"] == "electrical_design"))

        invalid = (
            (spec(extra=1), "extra"), (spec(tilt=True), "tilt"), (spec(target_dc_kw=float("nan")), "target_dc_kw"),
            (spec(azimuth=200.0), "azimuth"), (spec(year=True), "year"),
            (spec(losses={"LIDLoss": True}), "losses.LIDLoss"), (spec(losses={"LIDLoss": 1.0, "Other": 2.0}), "losses.Other"),
            (spec(module={"file_name": "sub/mod.PAN"}), "module.file_name"),
            (spec(weather={"mode": "guess"}), "weather.mode"),
            (spec(weather={"mode": "csv", "csv_name": "a.csv", "country_code": "CN"}), "weather.time_basis"),
            (spec(weather={"mode": "csv", "csv_name": "a.csv", "country_code": "cn", "time_basis": flow.TIME_BASIS}),
             "weather.country_code"),
            (spec(weather={"mode": "open_meteo", "location_id": 1, "latitude": 1.0, "country_code": "CN"}),
             "weather.location_id"),
            (spec(weather={"mode": "open_meteo", "country_code": "CN"}), "weather.location_id"),
            (spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN",
                           "temperature_method": "mean"}), "weather.temperature_method"),
            (spec(capacity=capacity(target_dc_kw=90.0)), "capacity.target_dc_kw"),
            (spec(year=""), "year"), (spec(tilt=""), "tilt"), (spec(azimuth=""), "azimuth"),
            (spec(target_dc_kw=""), "target_dc_kw"), (spec(capacity=capacity(target_dc_kw="")), "capacity.target_dc_kw"),
            (spec(module={"file_name": "", "manufacturer": ""}), "module.name"),
        )
        for broken, field in invalid:
            with self.subTest(field=field):
                res, _ = self.run_flow(broken)
                self.assertEqual((res["status"], res["stage"]), ("blocked", "spec"))
                self.assertIn(field, [item["field"] for item in res["issues"]])

        typed = spec(template_project="", variant="",
                     module={"file_name": "", "manufacturer": "Lab", "model": "440W"},
                     inverter={"file_name": "", "manufacturer": "Lab", "model": "50kW"},
                     weather={"mode": "existing", "sit_name": "Plant.SIT", "met_name": "Plant.MET",
                              "csv_name": "", "country_code": "", "time_basis": "",
                              "site_label": "", "weather_manifest_name": ""})
        ok, _ = self.run_flow(typed)
        self.assertEqual((ok["status"], ok["stage"], ok["issues"]), ("ready", "preflight", []))

    def test_capacity_confirmation_and_declared_design_envelope_forwarding(self):
        plan = {"status": "ready", "inverter_count": 2, "steps": [], "capacity_error_pct": 0.0}
        calls = []

        def discovery(store, **kwargs):
            calls.append(kwargs)
            return {"status": "ready", "selected": copy.deepcopy(selected()), "issues": [],
                    "warnings": [], "capacity_plan": plan}

        csv_res, mocks = self.run_flow(
            spec(weather={"mode": "csv", "csv_name": "stage.csv", "country_code": "CN", "time_basis": flow.TIME_BASIS}),
            discovery=discovery,
            prepare=lambda cli, **kwargs: {"status": "ready", "warnings": [], "issues": [], "artifacts": {}, "created_files": {}},
        )
        self.assertEqual((csv_res["status"], csv_res["stage"]), ("ready_for_weather", "planned"))
        self.assertFalse(csv_res["execution_preflight_complete"])
        self.assertEqual([item["capacity"] for item in calls], [None, spec()["capacity"]])
        self.assertEqual(mocks["simulate_template"].call_count, 0)

        calls.clear()
        env_calls = []

        def envelope(module_electrical, inverter_electrical, request):
            env_calls.append(request)
            return {"status": "ready", "capacity": capacity(), "issues": [],
                    "warnings": [{"field": "module.GRef", "message": "not 1000 W/m2"}],
                    "provenance": {}, "envelope_mode": "coefficients"}

        env_res, env_mocks = self.run_flow(
            spec(capacity=None, electrical_design={"temperatures": {"min_ambient_c": -10.0}}),
            discovery=discovery, envelope=envelope, simulate=simulation(status="ready", stage="preflight"),
        )
        self.assertEqual((env_res["status"], env_res["stage"], env_res["execution_preflight_complete"]),
                         ("ready", "preflight", True))
        self.assertEqual(env_calls[0]["manufacturer_model"], "Lab 440W / Lab 50kW")
        self.assertIn("module.GRef: not 1000 W/m2", env_res["warnings"])
        self.assertEqual(env_mocks["simulate_template"].call_args.kwargs["capacity"], capacity())

        bad_label, label_mocks = self.run_flow(
            spec(capacity=None, electrical_design={"manufacturer_model": "Other 1 / Lab 50kW"}),
            envelope=lambda *a: (_ for _ in ()).throw(AssertionError("should not build envelope")),
        )
        self.assertEqual((bad_label["status"], bad_label["stage"]), ("blocked", "design_envelope"))
        self.assertEqual(label_mocks["discover_design_inputs"].call_count, 1)

    def test_discovery_blocks_ambiguous_or_drifting_components(self):
        ambiguous = {"status": "blocked", "selected": None, "warnings": [],
                     "issues": [{"field": "PAN.name", "message": "2 usable workspace files share this identity"}]}
        res, mocks = self.run_flow(spec(), discovery=lambda *a, **k: ambiguous,
                                   prepare=lambda *a, **k: None, simulate=lambda *a, **k: None)
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
                "review_required": ["hourly Tamb uses the endpoint_mean approximation"],
                "artifacts": {"csv": "Plant_openmeteo.csv"}, "warnings": [], "issues": []}
        dry, dry_mocks = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670}),
            download=lambda *a, **k: plan,
            prepare=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prepare in dry run")),
            simulate=lambda *a, **k: None,
        )
        self.assertEqual((dry["status"], dry["stage"], dry["execution_preflight_complete"]),
                         ("ready_for_weather", "planned", False))
        self.assertEqual(dry_mocks["download_weather"].call_count, 1)

        prepared = {"status": "prepared", "created_files": {"csv": {"name": "Plant_openmeteo.csv"}},
                    "location": {"country_code": "CN"},
                    "review_required": ["hourly Tamb uses the endpoint_mean approximation"],
                    "artifacts": {"csv": "Plant_openmeteo.csv"}, "warnings": [], "issues": []}
        prep = {"status": "prepared", "warnings": [], "issues": [],
                "artifacts": {"manifest": str(self.workspace / "Results" / "Plant_weather.json")}}
        done, done_mocks = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670}), dry_run=False,
            download=lambda *a, **k: plan if k["dry_run"] else prepared,
            prepare=lambda *a, **k: prep, simulate=simulation(artifacts={"csv": "annual.csv"}, metrics={"PR": 80.0}),
        )
        self.assertEqual((done["status"], done["stage"], done["execution_preflight_complete"]),
                         ("needs_review", "completed", True))
        self.assertEqual(done_mocks["prepare_weather"].call_args.kwargs["country_code"], "CN")
        self.assertEqual([c.kwargs["dry_run"] for c in done_mocks["download_weather"].call_args_list], [True, False])

        for bad_plan, expected_stage, field_or_msg in (
            ({"status": "ready", "standard_utc_offset_hours": 13, "artifacts": {}, "warnings": [], "issues": []},
             "acquisition", "create-site"),
            ({"status": "blocked", "warnings": [], "artifacts": {}, "issues": ["no ISO country code"]},
             "acquisition", "no ISO country code"),
        ):
            blocked, bm = self.run_flow(
                spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "NZ"}),
                dry_run=False, download=lambda *a, **k: bad_plan,
                prepare=lambda *a, **k: None, simulate=lambda *a, **k: None,
            )
            self.assertEqual((blocked["status"], blocked["stage"]), ("blocked", expected_stage))
            self.assertEqual(bm["prepare_weather"].call_count, 0)
            self.assertIn(field_or_msg, " ".join(i["message"] for i in blocked["issues"]))

    def test_preflight_outputs_aliases_and_downstream_failures(self):
        cli_bad = FakeCLI(self.workspace, failure="Installed PVsystCLI lacks run-simulation --time-step")
        np_res, np_mocks = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            dry_run=False, cli=cli_bad, download=lambda *a, **k: None, prepare=lambda *a, **k: None,
            simulate=lambda *a, **k: None,
        )
        self.assertEqual((np_res["status"], np_res["stage"], np_mocks["download_weather"].call_count),
                         ("blocked", "native_preflight", 0))

        conflict = self.workspace / "Results" / "Plant_annual_2024.csv"
        conflict.parent.mkdir(parents=True, exist_ok=True)
        conflict.write_text("", encoding="utf-8")
        out_res, _ = self.run_flow(spec(), simulate=lambda *a, **k: None)
        self.assertEqual((out_res["status"], out_res["stage"]), ("blocked", "outputs"))
        self.assertIn("annual_csv", [i["field"] for i in out_res["issues"]])
        conflict.unlink()

        alias_res, alias_mocks = self.run_flow(
            spec(weather={"mode": "open_meteo", "csv_name": "Plant_import.csv", "location_id": 1816670, "country_code": "CN"}),
            download=lambda *a, **k: None, prepare=lambda *a, **k: None, simulate=lambda *a, **k: None,
        )
        self.assertEqual((alias_res["status"], alias_res["stage"], alias_mocks["download_weather"].call_count),
                         ("blocked", "outputs", 0))

        existing_ok, ex_mocks = self.run_flow(spec(), simulate=simulation(status="ready", stage="preflight"))
        self.assertEqual(ex_mocks["simulate_template"].call_args.kwargs["weather_manifest_name"], "")
        self.assertTrue(any("source-weather comparison is skipped" in w for w in existing_ok["warnings"]))

        prep_fail, pf_mocks = self.run_flow(
            spec(weather={"mode": "csv", "csv_name": "stage.csv", "country_code": "CN", "time_basis": flow.TIME_BASIS}),
            dry_run=False,
            prepare=lambda *a, **k: {"status": "failed", "issues": [], "warnings": [],
                                      "error": "Native SIT differs from weather CSV #Longitude", "artifacts": {}},
            simulate=lambda *a, **k: None,
        )
        self.assertEqual((prep_fail["status"], prep_fail["stage"], pf_mocks["simulate_template"].call_count),
                         ("failed", "weather_prepare", 0))
        self.assertIn("Native SIT differs", [i["message"] for i in prep_fail["issues"]][0])

        def raise_prep(*a, **k):
            raise PVsystError("convert-meteo failed", diagnostics="Meteo conversion refused")

        raised_prep, _ = self.run_flow(
            spec(weather={"mode": "csv", "csv_name": "stage.csv", "country_code": "CN", "time_basis": flow.TIME_BASIS}),
            dry_run=False, prepare=raise_prep, simulate=lambda *a, **k: None,
        )
        self.assertEqual((raised_prep["status"], raised_prep["stage"]), ("failed", "weather_prepare"))

        recovery = [{"action": "restore_variant", "arguments": {"project": "Plant.PRJ"}}]
        reason = "Configured counts do not match the accepted capacity plan"
        sim_fail, _ = self.run_flow(
            spec(weather={"mode": "csv", "csv_name": "stage.csv", "country_code": "CN", "time_basis": flow.TIME_BASIS}),
            dry_run=False,
            prepare=lambda *a, **k: {"status": "prepared", "warnings": [], "issues": [],
                                      "artifacts": {"manifest": str(self.workspace / "Results" / "Plant_weather.json")}},
            simulate=simulation(status="failed", stage="capacity", recovery=recovery, error=reason, issues=[reason]),
        )
        self.assertEqual((sim_fail["status"], sim_fail["stage"], sim_fail["recovery"]),
                         ("failed", "capacity", recovery))
        self.assertEqual([i["message"] for i in sim_fail["issues"]].count(reason), 1)

        def failing_dl(ws, **kw):
            if kw["dry_run"]:
                return {"status": "ready", "standard_utc_offset_hours": 8,
                        "artifacts": {"raw_json": "raw.json"}, "warnings": [], "issues": []}
            raise RuntimeError("provider response exceeds limit")

        before = self.snapshot()
        acq_fail, acq_mocks = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            dry_run=False, download=failing_dl, prepare=lambda *a, **k: None, simulate=lambda *a, **k: None,
        )
        self.assertEqual((acq_fail["status"], acq_fail["stage"], acq_mocks["prepare_weather"].call_count),
                         ("failed", "acquisition", 0))
        self.assertEqual(acq_fail["artifacts"]["acquire"]["raw_json"], "raw.json")
        self.assertEqual(self.snapshot(), before)

    def test_seed_fallback_preview_execution_and_cancellation(self):
        seed_ready = {"status": "ready", "source": {"seed": "installed 8.1 residential demo"},
                      "retained_defaults": ["losses"], "changes": [], "warnings": [], "issues": []}
        (self.workspace / "Projects").rmdir()
        dry, dry_mocks = self.run_flow(spec(), discovery=lambda *a, **k: no_template(),
                                       seed=lambda *a, **k: seed_ready, simulate=lambda *a, **k: None)
        self.assertEqual((dry["status"], dry["stage"], (self.workspace / "Projects").exists()),
                         ("ready_for_weather", "planned", False))
        self.assertTrue(dry_mocks["prepare_standard_template"].call_args.kwargs["dry_run"])

        seq = [no_template(), no_template(), seeded_template()]
        seen, seed_calls = [], []

        def discovery(store, **kwargs):
            seen.append(kwargs)
            return copy.deepcopy(seq[len(seen) - 1])

        def seed(cli, store, **kwargs):
            seed_calls.append(kwargs)
            if kwargs["dry_run"]:
                return seed_ready
            self.assertTrue((self.workspace / "Projects").is_dir())
            return {"status": "created", "template_project": "Plant_template.PRJ", "variant": "VC0",
                    "files": {"Plant_template.PRJ": "a"}, "retained_defaults": [], "warnings": [], "issues": []}

        done, mocks = self.run_flow(spec(), dry_run=False, discovery=discovery, seed=seed,
                                    simulate=simulation(artifacts={}))
        self.assertEqual((done["status"], [c["dry_run"] for c in seed_calls]), ("completed", [True, False]))
        self.assertEqual(mocks["simulate_template"].call_args.kwargs["template_project"], "Plant_template.PRJ")

        seq2 = [no_template(), discovery_ready(None)]
        seen2, seed2 = [], []
        cancelled, cm = self.run_flow(
            spec(), dry_run=False,
            discovery=lambda s, **k: (seen2.append(k), copy.deepcopy(seq2[len(seen2) - 1]))[1],
            seed=lambda *a, **k: seed2.append(k) or {}, simulate=simulation(artifacts={}),
        )
        self.assertEqual((cancelled["status"], seed2, cancelled["artifacts"]["seed"]), ("completed", [], {}))
        self.assertEqual(cm["simulate_template"].call_args.kwargs["template_project"], "Template.PRJ")

    def test_seed_fallback_conflicts_drift_limits_and_explicit_bypass(self):
        seed_conflict = self.workspace / "Projects" / "Plant_template.PRJ"
        seed_conflict.write_bytes(b"")
        conflict, cm = self.run_flow(spec(), discovery=lambda *a, **k: no_template(),
                                     seed=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((conflict["status"], conflict["stage"], cm["prepare_standard_template"].call_count),
                         ("blocked", "outputs", 0))
        seed_conflict.unlink()

        long_res, lm = self.run_flow(spec(new_project="P" * 170 + ".PRJ"),
                                     discovery=lambda *a, **k: no_template(),
                                     seed=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((long_res["status"], long_res["stage"], lm["prepare_standard_template"].call_count),
                         ("blocked", "outputs", 0))
        self.assertIn("180-character project filename limit", long_res["issues"][0]["message"])

        explicit, em = self.run_flow(spec(template_project="Mine.PRJ", variant="VC0"),
                                     discovery=lambda *a, **k: no_template(),
                                     seed=lambda *a, **k: None, simulate=lambda *a, **k: None)
        self.assertEqual((explicit["status"], explicit["stage"], em["prepare_standard_template"].call_count),
                         ("blocked", "discovery", 0))

        changed = seeded_template()
        changed["selected"]["PAN"]["name"] = "other.PAN"
        seq = [no_template(), no_template(), changed]
        seen = []
        drifted, dm = self.run_flow(
            spec(), dry_run=False,
            discovery=lambda s, **k: (seen.append(k), copy.deepcopy(seq[len(seen) - 1]))[1],
            seed=lambda c, s, **k: ({"status": "ready", "retained_defaults": [], "warnings": [], "issues": []}
                                    if k["dry_run"] else
                                    {"status": "created", "files": {"Plant_template.PRJ": "a"},
                                     "retained_defaults": [], "warnings": [], "issues": []}),
            simulate=lambda *a, **k: None,
        )
        self.assertEqual((drifted["status"], drifted["stage"], dm["simulate_template"].call_count),
                         ("blocked", "discovery", 0))
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
        wr, wm = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            cli=FakeCLI(wrong), download=lambda *a, **k: None, prepare=lambda *a, **k: None,
            simulate=lambda *a, **k: None,
        )
        self.assertEqual((wr["status"], wr["stage"], wm["download_weather"].call_count), ("blocked", "workspace", 0))

        import shutil
        conflict = self.workspace / "Models"
        shutil.rmtree(conflict)
        conflict.write_text("existing", encoding="utf-8")
        fc, fm = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            dry_run=False, download=lambda *a, **k: None, prepare=lambda *a, **k: None,
            simulate=lambda *a, **k: None,
        )
        self.assertEqual((fc["status"], fc["stage"], fm["download_weather"].call_count), ("blocked", "workspace", 0))
        conflict.unlink()

        outside = self.root / "outside"
        outside.mkdir()
        link = self.workspace / "ComposPV" / "Batteries"
        shutil.rmtree(link)
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        esc, em = self.run_flow(
            spec(weather={"mode": "open_meteo", "location_id": 1816670, "country_code": "CN"}),
            dry_run=False, download=lambda *a, **k: None, prepare=lambda *a, **k: None,
            simulate=lambda *a, **k: None,
        )
        self.assertEqual((esc["status"], esc["stage"], em["download_weather"].call_count), ("blocked", "workspace", 0))
        self.assertIn("ComposPV/Batteries", esc["preview"]["workspace"]["escaped"])


if __name__ == "__main__":
    unittest.main()

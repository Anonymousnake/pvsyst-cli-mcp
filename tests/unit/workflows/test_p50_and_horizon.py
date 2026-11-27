"""Offline unit tests for zero-template P50/P90 probabilistic yields, bankability, and opt-in PVGIS horizon."""
from __future__ import annotations

import asyncio
import copy
import email.message
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from pvsyst_auto.api.context import TOOL_GROUPS, create_mcp_server
from pvsyst_auto.api.projects import P50P90Updates
from pvsyst_auto.api.schemas import (
    EconomicsFinancialConfig,
    FeasibilityStudyRequest,
    HorizonRequest,
    P50P90Spec,
    SingleDesignRequest,
)
from pvsyst_auto.components.store import ComponentStore
from pvsyst_auto.energy.p50_p90 import (
    _P50_ORDER,
    _p50_read,
    combined_uncertainty_pct,
    compute_p50_p90_yields,
    normal_cdf,
    normal_inv_cdf,
    normalize_p50_spec,
    p50_state,
    replace_p50,
    resolve_p50_p90_config,
)
from pvsyst_auto.engineering.economics import evaluate_economics
from pvsyst_auto.native.load import p50_state as native_p50_state, replace_horizon, replace_p50 as native_replace_p50
from pvsyst_auto.project.synthesize import synthesize_project_and_variant
from pvsyst_auto.project.template import synthesize_utility_project
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.weather.horizon import (
    PVGIS_HORIZON_URL,
    HorizonFetchError,
    _pvgis_horizon_get,
    convert_pvgis_horizon_points,
    fetch_pvgis_horizon,
    normalize_horizon_spec,
    pvgis_to_pvsyst_azimuth,
)
from pvsyst_auto.workflows.design import EXPECTED_WORKSPACE_BASENAME, prepare_design_project, simulate_design
from pvsyst_auto.workflows.optimize import run_feasibility_study
from pvsyst_auto.workflows.report import generate_report
from pvsyst_auto.workflows.simulation import simulate_template
from tests._fixtures import FakeCliBase, init_workspace, write_sfi_csv, write_utf8_crlf
from tests.unit.core.test_bifacial_utility import UTILITY_OND, UTILITY_PAN, _FakeUtilityCLI, _capacity_spec
from tests.unit.core.test_synthesize import MET_TEXT, SITE_TEXT


class _FakeHttpResponse:
    """Minimal read/read1/headers context manager emulating urllib HTTPResponse offline."""

    def __init__(self, body: bytes, *, headers: dict[str, str] | None = None) -> None:
        self._buf = io.BytesIO(body)
        msg = email.message.Message()
        for k, v in (headers or {}).items():
            msg[k] = v
        self.headers = msg

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def read1(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *args) -> bool:
        return False


def _pvgis_horizon_payload(profile: list[dict[str, float]]) -> bytes:
    return json.dumps({
        "inputs": {"location": {"latitude": 46.24, "longitude": 6.11}},
        "outputs": {"horizon_profile": profile},
    }).encode("utf-8")


def _sample_sfi_csv(path: Path, *, kw_day: float = 125.0) -> Path:
    rows = []
    for day in range(1, 11):
        for hr in range(24):
            is_day = 8 <= hr < 16
            stamp = f"{day:02d}/06/24 {hr:02d}:00"
            rows.append([stamp, 600.0 if is_day else 0.0, kw_day * 1.04 if is_day else 0.0,
                         kw_day if is_day else 0.0, 0.84 if is_day else 0.0])
    return write_sfi_csv(
        path,
        [("GlobInc", "W/m²"), ("EArray", "kW"), ("E_Grid", "kW"), ("PR", "ratio")],
        rows,
    )


class ZeroTemplateP50Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = init_workspace(Path(tmp.name).resolve() / EXPECTED_WORKSPACE_BASENAME)
        write_utf8_crlf(self.root / "Sites" / "Geneva_MN90.SIT", SITE_TEXT)
        write_utf8_crlf(self.root / "Meteo" / "Geneva_MN90_SYN.MET", MET_TEXT)
        write_utf8_crlf(self.root / "ComposPV/PVmodules/Lab440.PAN", UTILITY_PAN)
        write_utf8_crlf(self.root / "ComposPV/Inverters/Lab50.OND", UTILITY_OND)
        self.components = ComponentStore(self.root)
        self.store = VariantStore(self.root, self.components)

    def test_normalize_p50_spec_variants_and_rejections(self):
        norm_snake = normalize_p50_spec({
            "evaluation_years": 10,
            "meteo_kind": "MonthAverage",
            "p90_custom": 85,
            "pxx_custom": 92,
            "meteo_annual_variab_pct": 3.5,
            "custom_variab_pct": 1.25,
            "custom_var_label": "Grid curtailment",
        })
        self.assertEqual(norm_snake["evaluation_years"], 10)
        self.assertEqual(norm_snake["native"]["MeteoKind"], "MonthAverage")
        self.assertEqual(norm_snake["native"]["P90Custom"], 85)
        self.assertEqual(norm_snake["native"]["MeteoAnnualVariab"], 3.5)
        self.assertEqual(norm_snake["native"]["CustomVarLabel"], "Grid curtailment")
        self.assertEqual(normalize_p50_spec(norm_snake), norm_snake)
        self.assertIsNone(resolve_p50_p90_config({"enabled": False, "evaluation_years": 5}))

        for bad in (
            {"meteo_kind": "TMY", "MeteoKind": "TMY"},
            {"unknown_key": 1},
            {"evaluation_years": 0},
            {"evaluation_years": True},
            {"p90_custom": 95, "pxx_custom": 90},
            {"meteo_kind": "Hourly"},
            {"plot_kind": 2},
            {"meteo_annual_variab_pct": 4.1234},
            {"custom_var_label": "   "},
            {"custom_var_label": "非ASCII"},
        ):
            with self.assertRaises(ValueError):
                normalize_p50_spec(bad)

        with self.assertRaisesRegex(ValueError, "not both"):
            resolve_p50_p90_config({"evaluation_years": 1}, {"evaluation_years": 2})

    def test_zero_template_mount_update_remove_and_month_average_read(self):
        synth = synthesize_project_and_variant(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN", ond_name="Lab50.OND",
        )
        lines = synth["vc0_text"].lstrip("\ufeff").splitlines(keepends=True)
        self.assertEqual(native_p50_state(lines), {"mode": "none", "values": None})

        res1 = native_replace_p50(lines, {"meteo_annual_variab_pct": 4.5, "custom_var_label": "Snow uncertainty"})
        self.assertTrue(res1["mounted"])
        self.assertIsNone(res1["before"])
        self.assertEqual(res1["after"]["MeteoAnnualVariab"], 4.5)
        self.assertEqual(p50_state(lines)["mode"], "configured")

        joined = "".join(lines)
        self.assertLess(joined.index("End of PVObject pvShading"), joined.index("P50_P90=TP50_P90"))
        self.assertLess(joined.index("P50_P90=TP50_P90"), joined.rindex("End of PVObject pvVCalcul"))

        # Verify VariantStore structural check accepts zero-template VC0 with P50_P90 mounted
        self.store.create_project("ZeroP50.PRJ", synth["prj_text"], {"VC0": "".join(lines)})
        self.assertTrue(self.store.validate_structure("ZeroP50.PRJ", "VC0")["valid"])

        # Idempotent re-apply and in-place update
        res2 = replace_p50(lines, {"meteo_annual_variab_pct": 4.5, "custom_var_label": "Snow uncertainty"})
        self.assertFalse(res2["mounted"])
        self.assertEqual(res2["before"], res2["after"])

        res3 = replace_p50(lines, {"meteo_annual_variab_pct": 3.25})
        self.assertFalse(res3["mounted"])
        self.assertEqual(res3["after"]["MeteoAnnualVariab"], 3.25)

        # Removal via mode='none'
        rem = replace_p50(lines, {"mode": "none"})
        self.assertTrue(rem["removed"])
        self.assertIsNone(rem["after"])
        self.assertEqual(p50_state(lines), {"mode": "none", "values": None})
        rem2 = replace_p50(lines, {"mode": "none"})
        self.assertFalse(rem2["removed"])

        # Real PVsyst _DEMO_Commercial.VC2 12-field MonthAverage omission check
        comm_12 = [
            ln for ln in (
                "\r\n".join([
                    "  P50_P90=TP50_P90",
                    "    MeteoKind=MonthAverage",
                    "    P90Custom=90",
                    "    PxxCustom=95",
                    "    PxxReport=True",
                    "    PxxMainResults=False",
                    "    PlotKind=1",
                    "    MeteoAnnualVariab=4.000",
                    "    PVModelVariab=1.000",
                    "    InverterVariab=0.500",
                    "    SoilingVariab=1.000",
                    "    DegradVariab=1.000",
                    "    CustomVarLabel=Custom variability",
                    "  EndTags",
                ]) + "\r\n"
            ).splitlines(keepends=True)
        ]
        # Mount after End of PVObject pvShading in a clean synthesized variant
        lines_12 = synth["vc0_text"].lstrip("\ufeff").splitlines(keepends=True)
        idx_shd = next(i for i, ln in enumerate(lines_12) if ln.strip() == "End of PVObject pvShading")
        lines_12[idx_shd + 1:idx_shd + 1] = ["\r\n", *comm_12]
        _, decoded_12 = _p50_read(lines_12, require_template=False)
        self.assertEqual(decoded_12["MeteoKind"], "MonthAverage")
        self.assertEqual(decoded_12["YearDevFromAver"], 0.0)
        self.assertEqual(decoded_12["CustomVariab"], 0.0)

        upd_12 = replace_p50(lines_12, {**decoded_12, "CustomVariab": 2.0})
        self.assertEqual(upd_12["after"]["CustomVariab"], 2.0)
        fields_after, _ = _p50_read(lines_12)
        self.assertEqual(set(fields_after), set(_P50_ORDER))

    def test_variant_store_horizon_and_p50_coexistence_and_api_schema(self):
        synth = synthesize_project_and_variant(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN", ond_name="Lab50.OND",
        )
        self.store.create_project("Coexist.PRJ", synth["prj_text"], {"VC0": synth["vc0_text"]})
        info0 = self.store.inspect_parameters("Coexist.PRJ", "VC0")
        self.assertEqual(info0["horizon"]["mode"], "none")
        self.assertEqual(info0["p50_p90"]["mode"], "none")

        pts = [[-180.0, 5.0], [-60.0, 12.0], [60.0, 10.0], [180.0, 5.0]]
        dry = self.store.update_parameters(
            "Coexist.PRJ", "VC0", expected_sha256=info0["sha256"],
            horizon_updates={"mode": "profile", "profile_name": "Alpine", "points": pts},
            p50_p90_updates={"MeteoAnnualVariab": 5.0},
            dry_run=True,
        )
        self.assertTrue(dry["changed"])
        self.assertEqual(self.store.inspect_parameters("Coexist.PRJ", "VC0")["sha256"], info0["sha256"])

        live = self.store.update_parameters(
            "Coexist.PRJ", "VC0", expected_sha256=info0["sha256"],
            horizon_updates={"mode": "profile", "profile_name": "Alpine", "points": pts},
            p50_p90_updates={"MeteoAnnualVariab": 5.0},
            dry_run=False,
        )
        self.assertTrue(live["changed"])
        info1 = self.store.inspect_parameters("Coexist.PRJ", "VC0")
        self.assertEqual(info1["horizon"]["mode"], "profile")
        self.assertEqual(info1["p50_p90"]["mode"], "configured")
        self.assertEqual(info1["p50_p90"]["values"]["MeteoAnnualVariab"], 5.0)
        self.assertTrue(self.store.validate_structure("Coexist.PRJ", "VC0")["valid"])

        self.assertEqual(P50P90Updates(mode="none").mode, "none")
        with self.assertRaises(ValidationError):
            P50P90Updates(mode="none", MeteoAnnualVariab=3.0)


class ProbabilisticEconomicsTests(unittest.TestCase):
    def test_normal_cdf_rss_uncertainty_and_yields(self):
        self.assertAlmostEqual(normal_cdf(0.0), 0.5, places=9)
        self.assertAlmostEqual(normal_cdf(1.9599639845), 0.975, places=6)
        self.assertAlmostEqual(normal_inv_cdf(0.10), -1.2815515655, places=6)
        with self.assertRaises(ValueError):
            normal_inv_cdf(1.0)

        spec_1y = normalize_p50_spec({"evaluation_years": 1, "meteo_annual_variab_pct": 4.0})
        spec_10y = normalize_p50_spec({"evaluation_years": 10, "meteo_annual_variab_pct": 4.0})
        self.assertAlmostEqual(combined_uncertainty_pct(spec_1y["native"], evaluation_years=1), (16.0 + 3.25) ** 0.5, places=8)
        self.assertAlmostEqual(combined_uncertainty_pct(spec_10y["native"], evaluation_years=10), (1.6 + 3.25) ** 0.5, places=8)

        res = compute_p50_p90_yields(
            105_000.0,
            {"year_dev_from_aver_pct": 5.0, "evaluation_years": 1, "p90_custom": 85, "pxx_custom": 92},
            installed_dc_kw=100.0,
        )
        self.assertAlmostEqual(res["p50_yield_kwh"], 100_000.0, places=2)
        y = res["yields_kwh"]
        self.assertGreater(y["P50"], y["P75"])
        self.assertGreater(y["P75"], y["P85"])
        self.assertGreater(y["P85"], y["P90"])
        self.assertGreater(y["P90"], y["P92"])
        self.assertGreater(y["P92"], y["P99"])
        self.assertAlmostEqual(res["specific_yields_kwh_per_kwp"]["P50"], 1000.0, places=2)
        probs = [pt["probability_pct"] for pt in res["exceedance_curve"]]
        yields = [pt["annual_yield_kwh"] for pt in res["exceedance_curve"]]
        self.assertEqual(probs, sorted(probs))
        self.assertEqual(yields, sorted(yields, reverse=True))

    def test_evaluate_economics_p50_dscr_llcr_and_backward_compatibility(self):
        base_scenario = [{"label": "Util_1MW", "annual_yield_kwh": 1_500_000.0, "installed_dc_kw": 1000.0, "capex_total": 3_500_000.0}]
        fin = {
            "currency": "CNY", "project_life_years": 25, "discount_rate_pct": 6.0, "opex_per_kw_year": 40.0,
            "financing": {"equity_ratio_pct": 25.0, "loan_tenor_years": 12, "loan_interest_rate_pct": 4.2, "repayment_method": "equal_payment"},
        }
        tariff = {"mode": "feed_in", "feed_in_tariff": 0.42}

        plain = evaluate_economics(scenarios=base_scenario, financial=fin, tariff=tariff)
        self.assertNotIn("p50_p90", plain["scenarios"][0])

        prob = evaluate_economics(
            scenarios=base_scenario, financial=fin, tariff=tariff,
            p50_p90={"meteo_annual_variab_pct": 4.0, "pv_model_variab_pct": 1.5, "soiling_variab_pct": 1.0, "degrad_variab_pct": 1.0},
            include_cashflow_schedule=True,
        )
        p50_out = prob["scenarios"][0]["p50_p90"]
        bank = p50_out["financing_bankability"]
        self.assertLess(bank["p90_min_dscr"], bank["p50_min_dscr"])
        self.assertLess(bank["p90_llcr"], bank["p50_llcr"])
        self.assertLess(p50_out["financials"]["P90"]["npv"], p50_out["financials"]["P50"]["npv"])

        # Re-run P90 independently at its scale factor and verify closed-form LLCR calculation
        p90_factor = p50_out["factors_vs_sim"]["P90"]
        p90_direct = evaluate_economics(
            scenarios=[{**base_scenario[0], "annual_yield_kwh": 1_500_000.0 * p90_factor}],
            financial=fin, tariff=tariff, include_cashflow_schedule=True,
        )["scenarios"][0]["financials"]
        r_loan = 0.042
        debt = bank["debt_principal"]
        pv_cfads = sum(
            (row["equity_cashflow"] + row["debt_service"]) / ((1.0 + r_loan) ** row["year"])
            for row in p90_direct["cashflow_schedule"][:12]
        )
        self.assertAlmostEqual(bank["p90_llcr"], round(pv_cfads / debt, 3), places=3)

        # include_cashflow_schedule=False must still compute exact LLCR while stripping cashflow_schedule
        prob_no_sched = evaluate_economics(
            scenarios=base_scenario,
            financial={**fin, "p50_p90": {"meteo_annual_variab_pct": 4.0, "pv_model_variab_pct": 1.5, "soiling_variab_pct": 1.0, "degrad_variab_pct": 1.0}},
            tariff=tariff,
            include_cashflow_schedule=False,
        )
        self.assertNotIn("cashflow_schedule", prob_no_sched["scenarios"][0]["financials"])
        self.assertEqual(prob_no_sched["scenarios"][0]["p50_p90"]["financing_bankability"]["p90_llcr"], bank["p90_llcr"])


class ReportExceedanceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = init_workspace(Path(tmp.name).resolve())
        self.csv = _sample_sfi_csv(self.root / "Results" / "plant.csv", kw_day=125.0)

    def test_report_renders_exceedance_chart_kpis_and_backward_compatibility(self):
        scenarios = [{"label": "Plant_100kW", "csv_name": self.csv.name, "installed_dc_kw": 100.0, "capex_total": 350_000.0}]
        fin = {"currency": "CNY", "project_life_years": 20, "discount_rate_pct": 6.0, "opex_pct_capex": 1.0,
               "financing": {"equity_ratio_pct": 30.0, "loan_tenor_years": 10, "loan_interest_rate_pct": 4.0}}
        tariff = {"mode": "feed_in", "feed_in_tariff": 0.45}

        base_rep = generate_report(
            workspace=self.root, title="Base Report", output_name="base_report.html",
            scenarios=scenarios, financial=fin, tariff=tariff, language="en", dry_run=False,
        )
        self.assertFalse(base_rep["has_p50_p90"])

        p50_cfg = {"meteo_annual_variab_pct": 4.0, "pv_model_variab_pct": 1.5}
        dry_p50 = generate_report(
            workspace=self.root, title="P50 Report EN", output_name="p50_en.html",
            scenarios=scenarios, financial=fin, tariff=tariff, p50_p90=p50_cfg, language="en", dry_run=True,
        )
        self.assertTrue(dry_p50["has_p50_p90"])

        rep_en = generate_report(
            workspace=self.root, title="P50 Report EN", output_name="p50_en.html",
            scenarios=scenarios, financial=fin, tariff=tariff, p50_p90=p50_cfg, language="en", dry_run=False,
        )
        self.assertTrue(rep_en["has_p50_p90"])
        self.assertEqual(rep_en["chart_count"], base_rep["chart_count"] + 1)
        html_en = Path(rep_en["report_path"]).read_text(encoding="utf-8")
        for needle in ("Annual Yield Exceedance Probability", "P90 Yield", "P90 Min DSCR", "P90 LLCR", "plotly"):
            self.assertIn(needle, html_en)

        # Standalone report (no economics) + Chinese i18n
        rep_zh = generate_report(
            workspace=self.root, title="P50 概率报告", output_name="p50_zh.html",
            scenarios=[{"label": "Solo", "csv_name": self.csv.name, "installed_dc_kw": 100.0}],
            p50_p90=p50_cfg, language="zh", dry_run=False,
        )
        self.assertTrue(rep_zh["has_p50_p90"])
        html_zh = Path(rep_zh["report_path"]).read_text(encoding="utf-8")
        for needle in ("发电量超越概率分布", "P90 年发电量", "综合不确定度"):
            self.assertIn(needle, html_zh)


class PvgisHorizonFetcherTests(unittest.TestCase):
    def test_azimuth_conversion_and_convert_points_round_trip(self):
        # Northern hemisphere keeps PVGIS South=0 convention
        self.assertEqual(pvgis_to_pvsyst_azimuth(-90.0, 46.24), -90.0)
        self.assertEqual(pvgis_to_pvsyst_azimuth(90.0, 46.24), 90.0)
        self.assertEqual(pvgis_to_pvsyst_azimuth(0.0, 46.24), 0.0)

        # Southern hemisphere flips to North=0 via 180 - az wrapped to [-180, 180]
        self.assertEqual(pvgis_to_pvsyst_azimuth(0.0, -33.87), 180.0)
        self.assertEqual(pvgis_to_pvsyst_azimuth(180.0, -33.87), 0.0)
        self.assertEqual(pvgis_to_pvsyst_azimuth(45.0, -33.87), 135.0)
        self.assertEqual(pvgis_to_pvsyst_azimuth(-45.0, -33.87), -135.0)

        # Downsample 180 points -> <= 100 points, negative DEM clamped, +/-180 closed, peaks preserved
        raw_180 = [{"A": float(a), "H_hor": -2.0 if a == 0 else (28.5 if a == 40 else (18.0 if abs(a) < 100 else 4.0))}
                   for a in range(-180, 180, 2)]
        pts = convert_pvgis_horizon_points(raw_180, latitude=-33.87)
        self.assertLessEqual(len(pts), 100)
        self.assertGreaterEqual(len(pts), 4)
        self.assertEqual(pts[0][0], -180.0)
        self.assertEqual(pts[-1][0], 180.0)
        self.assertEqual(pts[0][1], pts[-1][1])
        self.assertTrue(all(0.0 <= h <= 90.0 for _, h in pts))
        self.assertTrue(all(pts[i][0] < pts[i + 1][0] for i in range(len(pts) - 1)))
        self.assertEqual(max(h for _, h in pts), 28.5)

        # Verify replace_horizon accepts converted points in a minimal VC0
        vc_lines = [
            "PVObject_=pvVCalcul\r\n", "  Version=8.1.7\r\n", "  Flags=$00100001\r\n",
            "  ACLossesRef=STC\r\n", "\r\n",
            "  PVObject_PVMainArray=pvMainArray\r\n", "  End of PVObject pvMainArray\r\n",
            "End of PVObject pvVCalcul\r\n",
        ]
        res = replace_horizon(vc_lines, {"mode": "profile", "profile_name": "Sydney SH", "points": pts})
        self.assertEqual(res["after"]["profile_name"], "Sydney SH")
        self.assertEqual(len(res["after"]["points"]), len(pts))

    def test_fetch_pvgis_horizon_mocked_normal_flat_and_errors(self):
        profile = [
            {"A": -180, "H_hor": 3.0},
            {"A": -90, "H_hor": 8.5},
            {"A": 0, "H_hor": 2.0},
            {"A": 90, "H_hor": 14.0},
            {"A": 180, "H_hor": 3.0},
        ]
        captured_urls: list[str] = []

        def fake_open(req, timeout=None):
            captured_urls.append((req.full_url, req.get_header("User-agent"), timeout))
            return _FakeHttpResponse(_pvgis_horizon_payload(profile))

        with patch("urllib.request.urlopen", side_effect=fake_open):
            hz = fetch_pvgis_horizon(46.24, 6.11, profile_name="Geneva Ridges", timeout=12.0)
        self.assertEqual(hz["mode"], "profile")
        self.assertFalse(hz["is_flat"])
        self.assertEqual(hz["profile_name"], "Geneva Ridges")
        self.assertEqual(hz["max_elevation_deg"], 14.0)
        self.assertEqual(hz["source"], "pvgis_v5_3")
        self.assertTrue(captured_urls[0][0].startswith(f"{PVGIS_HORIZON_URL}?lat=46.24&lon=6.11&outputformat=json"))
        self.assertIn("pvsyst-auto-horizon", captured_urls[0][1])

        # All-zero flat profile returns mode="none" cleanly with warning
        flat_payload = _pvgis_horizon_payload([{"A": a, "H_hor": -0.5} for a in (-180, -90, 0, 90, 180)])
        with patch("urllib.request.urlopen", return_value=_FakeHttpResponse(flat_payload)):
            flat = fetch_pvgis_horizon(10.0, 20.0)
        self.assertEqual(flat["mode"], "none")
        self.assertTrue(flat["is_flat"])
        self.assertTrue(flat["warnings"])

        # Bounds & transport/HTTP failure checks
        with self.assertRaisesRegex(HorizonFetchError, "Refusing to call non-PVGIS host"):
            _pvgis_horizon_get("http://example.com/printhorizon", {})

        with patch("urllib.request.urlopen", return_value=_FakeHttpResponse(b"{}", headers={"Content-Length": "9999999"})):
            with self.assertRaisesRegex(HorizonFetchError, "limit"):
                fetch_pvgis_horizon(46.24, 6.11)

        with patch("urllib.request.urlopen", return_value=_FakeHttpResponse(b"x" * 5000)):
            with self.assertRaisesRegex(HorizonFetchError, "limit"):
                fetch_pvgis_horizon(46.24, 6.11, max_bytes=2048)

        err_stream = io.BytesIO(b'{"message": "Location over the sea"}')
        http_err = urllib.error.HTTPError(PVGIS_HORIZON_URL, 400, "Bad Request", None, err_stream)
        with patch("urllib.request.urlopen", side_effect=http_err):
            with self.assertRaisesRegex(HorizonFetchError, "Location over the sea"):
                fetch_pvgis_horizon(46.24, 6.11)

        for exc in (TimeoutError("timed out"), urllib.error.URLError("DNS down")):
            with patch("urllib.request.urlopen", side_effect=exc):
                with self.assertRaises(HorizonFetchError):
                    fetch_pvgis_horizon(46.24, 6.11)

        for bad_body in (b"\xff\xfe", b"{not json", b'{"outputs": {}}'):
            with patch("urllib.request.urlopen", return_value=_FakeHttpResponse(bad_body)):
                with self.assertRaises(HorizonFetchError):
                    fetch_pvgis_horizon(46.24, 6.11)


class OptInAndWorkflowWiringTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = init_workspace(Path(tmp.name).resolve() / EXPECTED_WORKSPACE_BASENAME)
        write_utf8_crlf(self.root / "Sites" / "Geneva_MN90.SIT", SITE_TEXT)
        write_utf8_crlf(self.root / "Meteo" / "Geneva_MN90_SYN.MET", MET_TEXT)
        write_utf8_crlf(self.root / "ComposPV/PVmodules/Lab440.PAN", UTILITY_PAN)
        write_utf8_crlf(self.root / "ComposPV/Inverters/Lab50.OND", UTILITY_OND)
        self.components = ComponentStore(self.root)
        self.store = VariantStore(self.root, self.components)

    def test_synthesize_project_and_utility_emit_horizon_and_p50(self):
        pts = [[-180.0, 4.0], [-60.0, 10.0], [60.0, 9.0], [180.0, 4.0]]
        fixed = synthesize_project_and_variant(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN", ond_name="Lab50.OND",
            p50_p90={"meteo_annual_variab_pct": 3.8}, horizon={"mode": "points", "points": pts},
        )
        self.assertLess(fixed["vc0_text"].index("PVObject_Horizon=pvHorizon"), fixed["vc0_text"].index("PVObject_PVMainArray=pvMainArray"))
        self.assertLess(fixed["vc0_text"].index("End of PVObject pvShading"), fixed["vc0_text"].index("P50_P90=TP50_P90"))
        self.store.create_project("SynthOpt.PRJ", fixed["prj_text"], {"VC0": fixed["vc0_text"]})
        self.assertTrue(self.store.validate_structure("SynthOpt.PRJ", "VC0")["valid"])

        util = synthesize_utility_project(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            field_type="RegularSheds", pan_name="Lab440.PAN", ond_name="Lab50.OND",
            p50_p90={"evaluation_years": 5, "meteo_annual_variab_pct": 4.0},
            horizon={"mode": "points", "profile_name": "West Hill", "points": pts},
        )
        self.assertIn("PVObject_Horizon=pvHorizon", util["vc0_text"])
        self.assertIn("P50_P90=TP50_P90", util["vc0_text"])
        self.assertEqual(util["utility_summary"]["horizon"]["profile_name"], "West Hill")
        self.assertEqual(util["utility_summary"]["p50_p90"]["evaluation_years"], 5)

    def test_simulate_design_strict_opt_in_and_mocked_pvgis_ground_sheds(self):
        cli = _FakeUtilityCLI(self.root, year=2024)
        fake_cal = {"issues": [], "warnings": [], "year": 2024, "rows": 8784}
        base_spec = SingleDesignRequest(
            scope="ground_sheds", new_project="NoNet.PRJ", year=2024, target_dc_kw=100.0, tilt=20.0, azimuth=0.0,
            module={"file_name": "Lab440.PAN"}, inverter={"file_name": "Lab50.OND"},
            weather={"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            capacity=_capacity_spec(), ground_layout={"model_mode": "3d", "pitch_m": 11.0, "shed_count": 6, "module_rows": 2, "module_columns": 19},
        ).model_dump(exclude_none=True)

        # 1) No horizon / fetch_horizon=False -> zero network calls on both dry_run and live run
        with patch("pvsyst_auto.workflows.utility.inspect_met_calendar", return_value=fake_cal), \
             patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network call")) as mock_url:
            dry0 = simulate_design(cli, self.store, spec=base_spec, dry_run=True)
            ran0 = simulate_design(cli, self.store, spec=base_spec, dry_run=False)
        self.assertEqual(dry0["status"], "ready")
        self.assertEqual(ran0["status"], "completed")
        self.assertNotIn("p50_p90", ran0)
        self.assertNotIn("horizon", ran0)
        mock_url.assert_not_called()

        # 2) Opt-in horizon={'mode': 'pvgis'} + p50_p90: dry_run makes NO network call; live run calls PVGIS once
        pvgis_spec = {
            **base_spec,
            "new_project": "WithPvgis.PRJ",
            "horizon": {"mode": "pvgis", "profile_name": "Geneva Demo"},
            "p50_p90": {"evaluation_years": 1, "meteo_annual_variab_pct": 4.2},
        }
        profile_payload = _pvgis_horizon_payload([
            {"A": -180, "H_hor": 2.0}, {"A": -90, "H_hor": 9.0},
            {"A": 0, "H_hor": 1.0}, {"A": 90, "H_hor": 11.0}, {"A": 180, "H_hor": 2.0},
        ])
        with patch("pvsyst_auto.workflows.utility.inspect_met_calendar", return_value=fake_cal), \
             patch("urllib.request.urlopen", return_value=_FakeHttpResponse(profile_payload)) as mock_url:
            dry_hz = simulate_design(cli, self.store, spec=pvgis_spec, dry_run=True)
            mock_url.assert_not_called()
            self.assertEqual(dry_hz["status"], "ready")
            self.assertEqual(dry_hz["preview"]["horizon"]["endpoint"], PVGIS_HORIZON_URL)
            self.assertEqual(dry_hz["preview"]["horizon"]["coordinate_source"], "site_file")

            ran_hz = simulate_design(cli, self.store, spec=pvgis_spec, dry_run=False)
            self.assertEqual(mock_url.call_count, 1)
            self.assertEqual(ran_hz["status"], "completed")
            self.assertEqual(ran_hz["horizon"]["source"], "pvgis_v5_3")
            self.assertGreater(ran_hz["p50_p90"]["yields_kwh"]["P50"], ran_hz["p50_p90"]["yields_kwh"]["P90"])
            self.assertEqual(ran_hz["metrics"]["p50_p90"]["yields_kwh"], ran_hz["p50_p90"]["yields_kwh"])

        vc0_text = (self.root / "Projects" / "WithPvgis.VC0").read_text(encoding="utf-8-sig")
        self.assertIn("PVObject_Horizon=pvHorizon", vc0_text)
        self.assertIn("P50_P90=TP50_P90", vc0_text)

        # 3) weather.fetch_horizon=True alias + flat all-zero PVGIS response -> mode='none' warning, no PVObject_Horizon
        flat_payload = _pvgis_horizon_payload([{"A": a, "H_hor": 0.0} for a in (-180, -90, 0, 90, 180)])
        fetch_flag_spec = {
            **base_spec,
            "new_project": "FlatFetch.PRJ",
            "weather": {**base_spec["weather"], "fetch_horizon": True},
        }
        with patch("pvsyst_auto.workflows.utility.inspect_met_calendar", return_value=fake_cal), \
             patch("urllib.request.urlopen", return_value=_FakeHttpResponse(flat_payload)) as mock_url:
            prep = prepare_design_project(cli, self.store, spec=fetch_flag_spec, dry_run=False)
            self.assertEqual(mock_url.call_count, 1)
        self.assertEqual(prep["status"], "prepared")
        self.assertEqual(prep["horizon"]["mode"], "none")
        self.assertTrue(any("all-zero" in w for w in prep["warnings"]))
        self.assertNotIn("PVObject_Horizon=pvHorizon", (self.root / "Projects" / "FlatFetch.VC0").read_text(encoding="utf-8-sig"))

    def test_template_simulation_with_ageing_horizon_and_p50_coexistence(self):
        tpl = synthesize_project_and_variant(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN", ond_name="Lab50.OND", tilt_deg=25.0,
        )
        self.store.create_project("StdTpl.PRJ", tpl["prj_text"], {"VC0": tpl["vc0_text"]})
        cli = _FakeUtilityCLI(self.root, year=2024)
        fake_cal = {"issues": [], "warnings": [], "year": 2024, "rows": 8784}
        hz = normalize_horizon_spec({"mode": "points", "profile_name": "TplHill", "points": [[-180.0, 2.0], [0.0, 6.0], [90.0, 8.0], [180.0, 2.0]]})
        p50 = {"meteo_annual_variab_pct": 3.5, "degrad_variab_pct": 1.2}

        with patch("pvsyst_auto.workflows.simulation.inspect_met_calendar", return_value=fake_cal):
            dry = simulate_template(
                cli, self.store, template_project="StdTpl.PRJ", variant="VC0", new_project="TplRun.PRJ",
                pan_name="Lab440.PAN", ond_name="Lab50.OND", site_name="Geneva_MN90.SIT",
                met_name="Geneva_MN90_SYN.MET", tilt=25.0, year=2024, capacity=_capacity_spec(),
                losses={"ageing": {"enabled": True, "simulation_year": 10, "annual_module_degradation_percent": 0.45}},
                p50_p90=p50, horizon=hz, dry_run=True,
            )
            self.assertEqual(dry["status"], "ready", dry.get("issues"))
            self.assertEqual(dry["horizon_preview"]["horizon"]["after"]["profile_name"], "TplHill")
            self.assertTrue(dry["p50_p90_preview"]["p50_p90"]["mounted"])

            ran = simulate_template(
                cli, self.store, template_project="StdTpl.PRJ", variant="VC0", new_project="TplRun.PRJ",
                pan_name="Lab440.PAN", ond_name="Lab50.OND", site_name="Geneva_MN90.SIT",
                met_name="Geneva_MN90_SYN.MET", tilt=25.0, year=2024, capacity=_capacity_spec(),
                losses={"ageing": {"enabled": True, "simulation_year": 10, "annual_module_degradation_percent": 0.45}},
                p50_p90=p50, horizon=hz, dry_run=False,
            )
        self.assertIn(ran["status"], ("completed", "needs_review"))
        self.assertGreater(ran["p50_p90"]["yields_kwh"]["P50"], ran["p50_p90"]["yields_kwh"]["P90"])
        info = self.store.inspect_parameters("TplRun.PRJ", "VC0")
        self.assertEqual(info["horizon"]["mode"], "profile")
        self.assertEqual(info["p50_p90"]["mode"], "configured")
        self.assertEqual(info["ageing"]["mode"], "mounted")

    def test_flat_pvgis_clears_inherited_template_horizon_and_standalone_rejections(self):
        tpl = synthesize_project_and_variant(
            self.store, site_name="Geneva_MN90.SIT", met_name="Geneva_MN90_SYN.MET",
            pan_name="Lab440.PAN", ond_name="Lab50.OND", tilt_deg=25.0,
            horizon=normalize_horizon_spec({
                "mode": "points", "profile_name": "InheritedHill",
                "points": [[-180.0, 4.0], [0.0, 9.0], [90.0, 7.0], [180.0, 4.0]],
            }),
        )
        self.assertIn("PVObject_Horizon=pvHorizon", tpl["vc0_text"])
        self.store.create_project("HzTpl.PRJ", tpl["prj_text"], {"VC0": tpl["vc0_text"]})
        cli = _FakeUtilityCLI(self.root, year=2024)
        fake_cal = {"issues": [], "warnings": [], "year": 2024, "rows": 8784}
        flat_payload = {"outputs": {"horizon_profile": [{"A": a, "H_hor": 0.0} for a in (-180, -90, 0, 90, 180)]}}
        tpl_spec = SingleDesignRequest(
            template_project="HzTpl.PRJ", variant="VC0", new_project="ClearedHz.PRJ",
            year=2024, target_dc_kw=100.0, tilt=25.0, azimuth=0.0,
            module={"file_name": "Lab440.PAN"}, inverter={"file_name": "Lab50.OND"},
            weather={"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET", "fetch_horizon": True},
            capacity=_capacity_spec(),
        ).model_dump(exclude_none=True)

        class _FakeResp(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        raw_bytes = json.dumps(flat_payload).encode("utf-8")
        with patch("pvsyst_auto.workflows.simulation.inspect_met_calendar", return_value=fake_cal), \
             patch("urllib.request.urlopen", return_value=_FakeResp(raw_bytes)):
            ran = simulate_design(cli, self.store, spec=tpl_spec, dry_run=False)
        self.assertIn(ran["status"], ("completed", "needs_review"), ran)
        self.assertEqual(ran["horizon"]["mode"], "none")
        self.assertTrue(ran["horizon"]["is_flat"])
        self.assertEqual(self.store.inspect_parameters("ClearedHz.PRJ", "VC0")["horizon"]["mode"], "none")
        self.assertNotIn("PVObject_Horizon=pvHorizon", (self.root / "Projects" / "ClearedHz.VC0").read_text(encoding="utf-8-sig"))

        # Stand-alone design explicitly rejects p50_p90, horizon, and weather.fetch_horizon=True
        sa_base = {
            "scope": "stand_alone",
            "new_project": "SaReject.PRJ",
            "weather": {"mode": "existing", "sit_name": "Geneva_MN90.SIT", "met_name": "Geneva_MN90_SYN.MET"},
            "module": {"file_name": "Lab440.PAN"},
            "standalone": {"battery_file": "MissingBat.BTR"},
        }
        for bad_patch, expected_msg in (
            ({"p50_p90": {}}, "p50_p90 is not supported"),
            ({"horizon": {"mode": "points", "points": [[-180, 0], [0, 5], [90, 5], [180, 0]]}}, "horizon is not supported"),
            ({"weather": {**sa_base["weather"], "fetch_horizon": True}}, "fetch_horizon is not supported"),
        ):
            res_bad = simulate_design(cli, self.store, spec={**sa_base, **bad_patch}, dry_run=True)
            self.assertEqual(res_bad["status"], "blocked", bad_patch)
            self.assertIn(expected_msg, str(res_bad["issues"]), bad_patch)
        res_ok_flag = simulate_design(
            cli, self.store,
            spec={**sa_base, "weather": {**sa_base["weather"], "fetch_horizon": False}},
            dry_run=True,
        )
        self.assertNotIn("fetch_horizon is not supported", str(res_ok_flag.get("issues")))

    def test_feasibility_study_end_to_end_p50_bankability_and_report(self):
        csv_path = _sample_sfi_csv(self.root / "Results" / "study_p50.csv", kw_day=125.0)
        cli = FakeCliBase(self.root)
        spec = {
            "study_name": "P50 Bankability Study",
            "existing_csv": csv_path.name,
            "installed_dc_kw": 120.0,
            "p50_p90": {"evaluation_years": 1, "meteo_annual_variab_pct": 4.0, "pv_model_variab_pct": 1.5},
            "financial": {
                "currency": "CNY", "project_life_years": 20, "discount_rate_pct": 6.0, "capex_per_kw": 3500.0, "opex_pct_capex": 1.0,
                "financing": {"equity_ratio_pct": 25.0, "loan_tenor_years": 10, "loan_interest_rate_pct": 4.5},
            },
            "tariff": {"mode": "feed_in", "feed_in_tariff": 0.45},
            "report": {"title": "Bankability Report", "output_name": "bankability_p50.html", "language": "en", "plotlyjs": "cdn"},
        }
        prev = run_feasibility_study(cli, self.store, spec=spec, dry_run=True)
        self.assertEqual(prev["status"], "ready")
        self.assertTrue(prev["report"]["has_p50_p90"])
        self.assertIn("p90_llcr", prev["executive_summary"])

        done = run_feasibility_study(cli, self.store, spec=spec, dry_run=False, confirm=True)
        self.assertEqual(done["status"], "completed")
        self.assertIn("p50_p90", done)
        es = done["executive_summary"]
        self.assertGreater(es["p50_yield_kwh"], es["p90_yield_kwh"])
        self.assertIsNotNone(es["p90_min_dscr"])
        self.assertIsNotNone(es["p90_llcr"])
        self.assertTrue(done["report"]["has_p50_p90"])
        self.assertIn("Annual Yield Exceedance Probability", Path(done["report"]["report_path"]).read_text(encoding="utf-8"))

        # Explicit top-level p50_p90={"enabled": False} must override enabled financial["p50_p90"]
        econ_off = evaluate_economics(
            scenarios=[{
                "label": "plant",
                "annual_yield_kwh": 1_500_000.0,
                "installed_dc_kw": 1000.0,
                "capex_total": 3_500_000.0,
            }],
            financial={"p50_p90": {}},
            p50_p90={"enabled": False},
        )
        self.assertNotIn("p50_p90", econ_off["scenarios"][0])
        rep_off = generate_report(
            workspace=self.root, store=self.store, title="Off Report", output_name="p50_off_rep.html",
            scenarios=[{"label": "plant", "csv_name": csv_path.name, "installed_dc_kw": 120.0, "capex_total": 420_000.0}],
            financial={"p50_p90": {}},
            p50_p90={"enabled": False},
            dry_run=True,
        )
        self.assertFalse(rep_off["has_p50_p90"])
        study_off = run_feasibility_study(
            cli, self.store,
            spec={
                **spec,
                "p50_p90": {"enabled": False},
                "financial": {**spec["financial"], "p50_p90": {}},
                "report": {**spec["report"], "output_name": "bankability_p50_off.html"},
            },
            dry_run=True,
        )
        self.assertEqual(study_off["status"], "ready")
        self.assertFalse(study_off["report"]["has_p50_p90"])
        self.assertNotIn("p90_llcr", study_off["executive_summary"])


class SchemaAndBudgetTests(unittest.TestCase):
    def test_strict_request_schemas_and_mcp_tool_budget(self):
        P50P90Spec(evaluation_years=10, meteo_annual_variab_pct=4.0)
        HorizonRequest(mode="pvgis", latitude=46.24, longitude=6.11)
        SingleDesignRequest(
            scope="ground_sheds", new_project="SchemaOK.PRJ", year=2024, target_dc_kw=100.0, tilt=20.0,
            module={"file_name": "Lab440.PAN"}, inverter={"file_name": "Lab50.OND"},
            weather={"mode": "existing", "sit_name": "A.SIT", "met_name": "A.MET", "fetch_horizon": True},
            p50_p90={"meteo_annual_variab_pct": 3.5}, horizon={"mode": "pvgis"},
        )
        FeasibilityStudyRequest(study_name="OK", existing_csv="a.csv", p50_p90={"meteo_annual_variab_pct": 4.0})

        with self.assertRaises(ValidationError):
            HorizonRequest(mode="pvgis", points=[[-180, 0], [0, 5], [90, 5], [180, 0]])
        with self.assertRaises(ValidationError):
            HorizonRequest(mode="pvgis", latitude=46.24)
        with self.assertRaises(ValidationError):
            P50P90Spec(p90_custom=95, pxx_custom=90)
        with self.assertRaises(ValidationError):
            P50P90Spec(year_dev_from_aver_pct=-100)
        with self.assertRaisesRegex(ValueError, "exceed -100%"):
            normalize_p50_spec({"year_dev_from_aver_pct": -100})
        P50P90Spec(year_dev_from_aver_pct=-99.999)
        with self.assertRaises(ValidationError):
            EconomicsFinancialConfig(p50_p90={"evaluation_years": 1}, uncertainty={"evaluation_years": 1})

        # Hard MCP tool budget check
        self.assertEqual(len(TOOL_GROUPS["Admin & Diagnostics"]), 4)
        self.assertEqual(len(TOOL_GROUPS["Expert & Low-Level"]), 23)
        async def _counts():
            return {grp: len(await create_mcp_server(tool_groups=grp).list_tools()) for grp in (None, "all", "admin", "expert")}
        counts = asyncio.run(_counts())
        self.assertEqual(counts, {None: 48, "all": 75, "admin": 4, "expert": 23})


if __name__ == "__main__":
    unittest.main()

"""Opt-in end-to-end tests driving the real stdio MCP server + real Windows PVsystCLI 8.1.7.

Constructs a fresh isolated temporary workspace directly from ``DataRO\\PVsyst8.1_Data``
(never touching ``component-edit-verification``), snapshots and byte-restores both
``PVsyst.ini`` and ``PVsystCLI.ini`` under ``%LOCALAPPDATA%``, and executes real
end-to-end MCP tool sequences covering:
  1) ``pvsyst_run_house_task`` (portrait + positive spacing + obstacles + ``module_layout``)
  2) ``pvsyst_create_dc_cabling_variant`` + ``pvsyst_create_grid_storage_system``
     (builtin Li-ion ``.BTR`` + ``PowerShift`` 48-slot schedules) + ``pvsyst_update_variant_parameters``
     (``SystemUnavail`` ``periods``) + ``pvsyst_create_grid_storage_variant`` (``PeakShaving``)
     + ``pvsyst_build_sfi`` / ``pvsyst_run_simulation`` / ``pvsyst_read_results``.
  3) v0.3.0/v0.4.0 zero-template ``ground_tracker`` + ``sizing_mode="auto_from_components"`` + ``pvBiFacial``
     + ``pvsyst_diagnose_results`` (``.VC0`` attribution) + ``pvsyst_run_batch_sweep``
     + MCP guidance surface (``instructions``, 6 prompts, 5 resources, 46 default tools).
  4) v0.3.0 15-minute sub-hourly weather preparation (``pvsyst_prepare_weather(..., step_minutes=15)``)
     + ``PeakShaving`` AC storage ``-ts:subhour`` vs ``-ts:hour`` run & diagnostics.

Env (required; skipped cleanly when absent so offline CI stays isolated):
  PVSYST_E2E_REAL_NATIVE=1
  PVSYST_E2E_CLI=<path to PVsystCLI.exe>
Optional:
  PVSYST_E2E_DATARO=<path to DataRO\\PVsyst8.1_Data>
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import math
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[2]


def _decode(reply):
    text = "\n".join(item.text for item in reply.content if hasattr(item, "text"))
    self_err = bool(reply.is_error)
    return self_err, (text if self_err else json.loads(text))


def _ini_paths():
    local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return [
        local / "PVsyst" / "8.1" / "Admin" / "PVsyst.ini",
        local / "PVsystCLI" / "8.1" / "Admin" / "PVsystCLI.ini",
    ]


class RealMcpServerWorkflowsE2E(unittest.TestCase):
    def setUp(self):
        if os.name != "nt":
            self.skipTest("Real PVsystCLI MCP E2E suite is Windows-only")
        if os.environ.get("PVSYST_E2E_REAL_NATIVE") != "1":
            self.skipTest("Set PVSYST_E2E_REAL_NATIVE=1 to run the real MCP server E2E suite")
        cli_raw = os.environ.get("PVSYST_E2E_CLI") or os.environ.get("PVSYST_CONTRACT_CLI")
        if not cli_raw or not Path(cli_raw).is_file():
            self.skipTest("Set PVSYST_E2E_CLI to a real installed PVsystCLI.exe")
        self.cli = Path(cli_raw).resolve()
        dataro = Path(os.environ.get("PVSYST_E2E_DATARO") or (self.cli.parent / "DataRO" / "PVsyst8.1_Data"))
        if not dataro.is_dir():
            self.skipTest(f"DataRO workspace not found at {dataro}")
        self.dataro = dataro
        self.snapshots = {p: (p.read_bytes() if p.exists() else None) for p in _ini_paths()}
        self.addCleanup(self._restore_inis)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "PVsyst8.1_Data"
        shutil.copytree(self.dataro, self.ws)
        for sub in (
            "Other", "Results", "UserBatch", "UserData", "UserHourly",
            "UserHourlyParams", "UserImages", "UserOptimization", "UserRecycleBin",
            "ComposPV/Batteries", "ComposPV/Pumps",
        ):
            (self.ws / sub).mkdir(parents=True, exist_ok=True)

        # Prepare clean DemoHouse.PRJ + VC0 bound to Geneva_MN90.SIT / Geneva_MN90_SYN.MET with anchor FieldAzim=0.0
        for src_name, dst_name in (("_DEMO_Residential.PRJ", "DemoHouse.PRJ"), ("_DEMO_Residential.VC0", "DemoHouse.VC0")):
            txt = (self.ws / "Projects" / src_name).read_text(encoding="utf-8-sig")
            txt = re.sub(r"NomF=Geneva/Cointrin", "NomF=Geneva_MN90.SIT", txt)
            txt = re.sub(r"Latitude=46\.2383", "Latitude=46.2400", txt)
            txt = re.sub(r"Longitude=6\.1094", "Longitude=6.1100", txt)
            txt = re.sub(r"Altitude=418\b", "Altitude=425", txt)
            if dst_name.endswith(".VC0"):
                txt = re.sub(r"FieldAzim=20\.0", "FieldAzim=0.0", txt, count=1)
                txt = re.sub(r"PVModule=Generic_Mono_300W\.PAN", "PVModule=Generic_Mono_440W_Half.PAN", txt)
                txt = re.sub(r"GInverter=Generic_7\.5kW\.OND", "GInverter=Generic_12kW_2MPPT.OND", txt)
                txt = re.sub(r"PNomAC_InMPPT=7\.50", "PNomAC_InMPPT=12.00", txt)
                txt = re.sub(r"NModSerie=15", "NModSerie=12", txt)
                txt = re.sub(r"RSCablageCh=378\.105", "RSCablageCh=0.0", txt)
                txt = re.sub(r"RSCablagePC=1\.50", "RSCablagePC=0.00", txt)
                txt = re.sub(r"Flags=\$001E0002", "Flags=$001E0002", txt)
            (self.ws / "Projects" / dst_name).write_bytes(txt.encode("utf-8-sig"))

    def _restore_inis(self):
        for p, raw in self.snapshots.items():
            if raw is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(raw)

    def _mcp_params(self):
        env = dict(
            os.environ,
            PVSYST_CLI=str(self.cli),
            PVSYST_WORKSPACE=str(self.ws),
            PVSYST_BUILTIN_COMPONENTS=str(self.cli.parent / "DataRO" / "PVsyst8.1_Data" / "ComposPV"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        env.pop("PVSYST_TOOL_GROUPS", None)
        return StdioServerParameters(
            command=sys.executable,
            args=[str(REPO / "pvsyst_mcp_server.py")],
            cwd=str(REPO),
            env=env,
        )

    def test_mcp_house_task_portrait_spacing_obstacles_module_layout_e2e(self):
        async def run():
            t = math.radians(25.0)
            down = 8.0 * math.cos(t)
            drop = 8.0 * math.sin(t)
            house = {
                "kind": "mesh",
                "meshes": [
                    {
                        "name": "SouthRoof",
                        "vertices": [
                            [0.0, 0.0, 4.0],
                            [18.0, 0.0, 4.0],
                            [18.0, down, 4.0 + drop],
                            [0.0, down, 4.0 + drop],
                        ],
                        "triangles": [[0, 1, 2], [0, 2, 3]],
                    }
                ],
            }
            layout = {
                "tables": [
                    {
                        "name": "SouthPortrait",
                        "module_rows": 2,
                        "module_columns": 12,
                        "orientation": "portrait",
                        "row_spacing_m": 0.03,
                        "column_spacing_m": 0.04,
                        "origin_m": [1.0, 0.5, 4.0 + 0.5 * math.tan(t) + 0.15],
                        "roof_mesh": "SouthRoof",
                    }
                ]
            }
            string_layout = {
                "tables": [
                    {
                        "name": "SouthPortrait",
                        "subarray_id": 1,
                        "cells": [
                            {"row": r, "col": c, "global_string": r}
                            for r in (1, 2)
                            for c in range(1, 13)
                        ],
                    }
                ]
            }
            obstacles = [
                {
                    "kind": "tree",
                    "name": "FrontOak",
                    "origin_m": [5.0, -7.0, 0.0],
                    "top_height_m": 10.0,
                    "middle_height_m": 7.5,
                    "bottom_height_m": 3.0,
                    "trunk_height_m": 2.0,
                    "crown_diameter_m": 5.0,
                    "trunk_diameter_m": 0.5,
                },
                {
                    "kind": "wall",
                    "name": "FrontWall",
                    "start_m": [0.0, -3.5, 0.0],
                    "end_m": [16.0, -3.5, 0.0],
                    "height_m": 2.0,
                },
            ]
            # Generic_Mono_440W_Half dimensions from DataRO _DEMO_Residential.VC3 (1.052 x 2.115 m)
            mg = {
                "model_name": "Mono 440 Wp Twin 144 half-cells",
                "nom_power_wp": 440.0,
                "width_m": 1.052,
                "length_m": 2.115,
                "geometry_depth_m": 0.035,
            }
            spec = {
                "source_project": "DemoHouse.PRJ",
                "source_variant": "VC0",
                "new_project": "OutHouse.PRJ",
                "variant_name": "E2E Portrait ML",
                "year": 1990,
                "house": house,
                "layout": layout,
                "string_layout": string_layout,
                "obstacles": obstacles,
                "module_geometry": mg,
                "shading_mode": "module_layout",
                "report_pdf": False,
            }
            async with stdio_client(self._mcp_params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    err, prev = _decode(await session.call_tool("pvsyst_run_house_task", {"spec": spec, "dry_run": True}))
                    self.assertFalse(err, prev)
                    self.assertEqual(prev["status"], "ready", prev)

                    err, done = _decode(await session.call_tool(
                        "pvsyst_run_house_task", {"spec": spec, "dry_run": False, "confirm": True}
                    ))
                    self.assertFalse(err, done)
                    self.assertEqual(done["phase"], "completed", done.get("issues"))
                    self.assertIn(done["status"], ("completed", "needs_review"), done.get("issues"))
                    self.assertTrue(done["native_consumption_verified"])
                    self.assertGreater(done["metrics"]["array_energy_kwh"], 0.0)
                    self.assertGreater(done["metrics"]["grid_energy_kwh"], 0.0)
                    self.assertEqual(done["hourly_rows"], 8760)
                    self.assertIn("ShdElec", str(done.get("electrical_shading_output")))
                    # Check before the test's outer byte-for-byte INI cleanup.
                    from pvsyst_auto.native._workspace_ini import _is_supported, _profile_ini, _snapshot
                    if _is_supported(self.cli):
                        ini = _profile_ini()
                        restored = _snapshot(ini.read_bytes())[1]
                        original = _snapshot(self.snapshots[ini])[1]
                        self.assertEqual({k: v for k, v in restored.items() if v},
                                         {k: v for k, v in original.items() if v})
                    vc_text = Path(done["artifacts"]["native_variant"]).read_text(encoding="utf-8-sig")
                    self.assertIn("ModuleOrient=moPortrait", vc_text)
                    self.assertIn("ModSpacingX=0.040", vc_text)
                    self.assertIn("ModSpacingY=0.030", vc_text)
                    self.assertIn("ObjetElemType=oeTree", vc_text)

        asyncio.run(run())

    def test_mcp_dc_cabling_unavail_and_storage_combo_e2e(self):
        async def run():
            async with stdio_client(self._mcp_params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    # 1. DC cabling resistance edit on DemoHouse.VC0 -> VC1
                    err, dc = _decode(await session.call_tool("pvsyst_create_dc_cabling_variant", {
                        "project": "DemoHouse.PRJ",
                        "source_variant": "VC0",
                        "new_variant": "VC1",
                        "dc": {"mode": "resistance", "value": 450.0},
                        "dry_run": False,
                    }))
                    self.assertFalse(err, dc)
                    self.assertEqual(dc["status"], "created", dc)

                    # 2. SystemUnavail periods edit on VC1 -> VC2 (without touching geometry)
                    err, params = _decode(await session.call_tool("pvsyst_get_variant_parameters", {
                        "project": "DemoHouse.PRJ",
                        "variant": "VC1",
                    }))
                    self.assertFalse(err, params)
                    err, unavail = _decode(await session.call_tool("pvsyst_update_variant_parameters", {
                        "project": "DemoHouse.PRJ",
                        "variant": "VC1",
                        "expected_sha256": params["sha256"],
                        "system_unavailability_updates": {
                            "mode": "periods",
                            "periods": [{"date_beg": "03/06/90 00:00", "duration_hours": 48}],
                        },
                        "dry_run": False,
                    }))
                    self.assertFalse(err, unavail)
                    self.assertTrue(unavail["changed"], unavail)

                    # 3. Create PowerShift storage from scratch with builtin Li-ion BTR on VC1 -> VC2
                    batt = {
                        "file_name": "Generic_LI_Module_180Ah.BTR",
                        "library": "builtin",
                        "series_count": 1,
                        "parallel_count": 2,
                        "monthly_temperature_c": [20] * 12,
                        "cycle_sow": 1.0,
                        "static_sow": 1.0,
                    }
                    err, ps = _decode(await session.call_tool("pvsyst_create_grid_storage_system", {
                        "project": "DemoHouse.PRJ",
                        "source_variant": "VC1",
                        "new_variant": "VC2",
                        "battery": batt,
                        "storage": {
                            "strategy": "power_shift",
                            "charging_strategy": "pv_prod_fraction",
                            "charging_pv_fraction": 0.40,
                            "charging_schedule_48": [0.0] * 18 + [0.40] * 16 + [0.0] * 14,
                            "discharging_strategy": "specified_hours",
                            "discharging_power_kw": 3.0,
                            "discharging_schedule_48": [3.0] * 12 + [0.0] * 24 + [3.0] * 12,
                            "soc_min": 0.2,
                            "soc_max": 0.95,
                            "initial_soc": 0.6,
                            "reserve_soc": 0.5,
                            "max_charge_ac_kw": 5.0,
                            "max_discharge_ac_kw": 5.0,
                            "charger_max_efficiency_pct": 97.0,
                            "charger_euro_efficiency_pct": 95.0,
                            "battery_inverter_max_efficiency_pct": 97.0,
                            "battery_inverter_euro_efficiency_pct": 95.0,
                        },
                        "dry_run": False,
                    }))
                    self.assertFalse(err, ps)
                    self.assertEqual(ps["status"], "created", ps)

                    # 4. Switch to PeakShaving on 3D+ML _DEMO_Residential.VC3 -> VC7 -> VC8
                    err, pk = _decode(await session.call_tool("pvsyst_create_grid_storage_system", {
                        "project": "_DEMO_Residential.PRJ",
                        "source_variant": "VC3",
                        "new_variant": "VC7",
                        "battery": batt,
                        "storage": {
                            "strategy": "peak_shaving",
                            "charging_strategy": "pv_above_thresh",
                            "discharging_strategy": "hour_window",
                            "discharge_start_hour": 19,
                            "discharge_end_hour": 6,
                            "grid_limit_kw": 3.2,
                            "soc_min": 0.2,
                            "soc_max": 0.95,
                            "initial_soc": 0.6,
                            "reserve_soc": 0.5,
                            "max_charge_ac_kw": 5.0,
                            "max_discharge_ac_kw": 5.0,
                            "charger_max_efficiency_pct": 97.0,
                            "charger_euro_efficiency_pct": 95.0,
                            "battery_inverter_max_efficiency_pct": 97.0,
                            "battery_inverter_euro_efficiency_pct": 95.0,
                        },
                        "dry_run": False,
                    }))
                    self.assertFalse(err, pk)
                    self.assertEqual(pk["status"], "created", pk)

                    # 5. Build SFI + simulate both VC2 (PowerShift + Unavail + DC cabling) and VC7 (3D+ML + PeakShaving)
                    err, sfi = _decode(await session.call_tool("pvsyst_build_sfi", {
                        "filename": "e2e_storage.SFI",
                        "variables": "grid_storage",
                    }))
                    self.assertFalse(err, sfi)

                    for prj, vtag, csv_name in (
                        ("DemoHouse.PRJ", "VC2", "e2e_ps.csv"),
                        ("_DEMO_Residential.PRJ", "VC7", "e2e_pk.csv"),
                    ):
                        err, sim = _decode(await session.call_tool("pvsyst_run_simulation", {
                            "project": prj,
                            "variant": vtag,
                            "sfi_name": "e2e_storage.SFI",
                            "csv_name": csv_name,
                            "met_name": "Geneva_MN90_SYN.MET",
                            "site_name": "Geneva_MN90.SIT",
                            "time_step": "hour",
                            "start_date": "90.06.01",
                            "end_date": "90.06.07",
                            "allow_window_creation": False,
                        }))
                        self.assertFalse(err, sim)
                        err, res = _decode(await session.call_tool("pvsyst_read_results", {
                            "csv_name": csv_name,
                            "columns": "EArray,EBatCh,EBatDis,E_Grid",
                        }))
                        self.assertFalse(err, res)
                        summary = res["summary"]
                        self.assertGreater(summary["EArray"]["sum"], 0.0)
                        self.assertGreater(summary["EBatCh"]["sum"], 0.0)
                        self.assertGreater(summary["EBatDis"]["sum"], 0.0)
                        self.assertGreater(summary["E_Grid"]["sum"], 0.0)

        asyncio.run(run())

    def _stage_bifacial_pan(self) -> None:
        pan_dir = self.ws / "ComposPV" / "PVModules"
        pan_dir.mkdir(parents=True, exist_ok=True)
        pan_text = """PVObject_=pvModule
  Comment=Lab 440Wp Bifacial Mono
  Version=8.1.7
  Flags=$00800000
  PVObject_Commercial=pvCommercial
    Flags=$0041
    Manufacturer=Lab
    Model=Mono 440W Bifacial
    Width=1.052
    Height=2.131
    Depth=0.035
    Weight=24.0
  End of PVObject pvCommercial
  Technol=mtSiMono
  NCelS=72
  NCelP=2
  NDiode=3
  SubModuleLayout=slTwinHalfCells
  PNom=440.0
  PNomTolLow=0.0
  PNomTolUp=3.0
  BifacialityFactor=0.800
  LargApp=1.052
  LongApp=2.131
  GRef=1000
  TRef=25
  Absorb=0.90
  ISC=11.400
  Voc=49.50
  Imp=10.760
  Vmp=40.90
  MuISC=5.50
  muVocSpec=-148.5
  muPmpReq=-0.350
  RShunt=400
  Rp_0=1600
  Rp_Exp=5.50
  RSerie=0.220
  Gamma=1.020
  muGamma=-0.0004
  VMaxIEC=1500
  VMaxUL=1500
End of PVObject pvModule
"""
        (pan_dir / "Lab_440W_Bifi.PAN").write_text(pan_text, encoding="utf-8")

    def _stage_15min_weather_csv(self, filename: str = "Geneva_2023_15min.csv", year: int = 2023) -> None:
        meteo_dir = self.ws / "Meteo"
        meteo_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            "#Weather data sub-hourly data",
            "#Site;GenevaSub15",
            "#Country;Switzerland",
            "#Data Source;Synthetic15m",
            "#Time step;15 min",
            f"#Year;{year}",
            "#Latitude;46.24",
            "#Longitude;6.11",
            "#Altitude;425",
            "#Time Zone;1",
            "Year;Month;Day;Hour;Minute;GHI;DHI;Tamb",
            ";;;;;W/m2;W/m2;deg.C",
        ]
        # Longitude 6.11E vs TZ+1 (15E meridian): solar noon is ~12:35 local standard time.
        lon_shift_h = (15.0 - 6.11) / 15.0
        cur = datetime(year, 1, 1, 0, 0)
        end = datetime(year + 1, 1, 1, 0, 0)
        step = timedelta(minutes=15)
        lat_r = math.radians(46.24)
        while cur < end:
            doy = cur.timetuple().tm_yday
            tod = cur.hour + cur.minute / 60.0 + 0.125
            decl = math.radians(23.45 * math.sin(math.radians(360.0 * (284 + doy) / 365.0)))
            ha = math.radians((tod - 12.0 - lon_shift_h) * 15.0)
            sin_el = math.sin(lat_r) * math.sin(decl) + math.cos(lat_r) * math.cos(decl) * math.cos(ha)
            if sin_el > 0.02:
                # Intra-hour cloud pulse so 15-min differs from 60-min averaged profile
                cloud = 0.55 if (cur.minute == 15 and doy % 2 == 0) else (1.12 if cur.minute == 30 else 0.96)
                ghi = min(1050.0, max(0.0, 930.0 * sin_el * cloud))
                dhi = min(ghi, ghi * (0.26 if cloud >= 0.9 else 0.55))
            else:
                ghi = 0.0
                dhi = 0.0
            tamb = (12.0 + 10.0 * math.sin(math.radians(360.0 * (doy - 105) / 365.0))
                    + 4.0 * math.sin(math.radians((tod - 9.0) * 15.0)))
            lines.append(f"{cur.year};{cur.month};{cur.day};{cur.hour};{cur.minute};{ghi:.2f};{dhi:.2f};{tamb:.2f}")
            cur += step
        (meteo_dir / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_mcp_v030_utility_tracker_bifacial_auto_sizing_and_batch_sweep_e2e(self):
        async def run():
            self._stage_bifacial_pan()
            async with stdio_client(self._mcp_params()) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    # Verify v0.5.0 MCP guidance surface & exact 46-tool default budget
                    self.assertTrue(init.instructions and "pvsyst_run_batch_sweep" in init.instructions)
                    self.assertIn("pvsyst_evaluate_economics", init.instructions)
                    self.assertIn("pvsyst_generate_report", init.instructions)
                    self.assertIn("pvsyst_run_feasibility_study", init.instructions)
                    prompts = {item.name for item in (await session.list_prompts()).prompts}
                    self.assertEqual(len(prompts), 6)
                    resources = {str(item.uri) for item in (await session.list_resources()).resources}
                    self.assertEqual(
                        resources,
                        {
                            "pvsyst://guide/workflows",
                            "pvsyst://guide/variables",
                            "pvsyst://workspace/projects",
                            "pvsyst://workspace/components",
                            "pvsyst://workspace/results",
                        },
                    )
                    tools = {item.name for item in (await session.list_tools()).tools}
                    self.assertEqual(len(tools), 46)

                    # 1. Read-only sizing recommendation (0 CLI execs)
                    err, sizing = _decode(await session.call_tool("pvsyst_suggest_sizing", {
                        "pan_name": "Lab_440W_Bifi.PAN",
                        "ond_name": "Generic_50kW.OND",
                        "target_dc_kw": 110.0,
                    }))
                    self.assertFalse(err, sizing)
                    self.assertEqual(sizing["status"], "ready", sizing)
                    self.assertGreater(sizing["recommended"]["installed_dc_kw"], 90.0)

                    # 2. Zero-template 3D ground_tracker + auto_from_components + pvBiFacial (1 CLI exec)
                    design_spec = {
                        "scope": "ground_tracker",
                        "new_project": "E2E_Trk_Bifi.PRJ",
                        "year": 1990,
                        "target_dc_kw": 110.0,
                        "sizing_mode": "auto_from_components",
                        "module": {"file_name": "Lab_440W_Bifi.PAN"},
                        "inverter": {"file_name": "Generic_50kW.OND"},
                        "weather": {
                            "mode": "existing",
                            "sit_name": "Geneva_MN90.SIT",
                            "met_name": "Geneva_MN90_SYN.MET",
                        },
                        "ground_layout": {
                            "model_mode": "3d",
                            "pitch_m": 6.5,
                            "shed_count": 6,
                            "module_rows": 2,
                            "module_columns": 20,
                            "module_orientation": "portrait",
                            "axis_azimuth_deg": 0.0,
                            "min_phi_deg": -55.0,
                            "max_phi_deg": 55.0,
                            "backtracking": True,
                        },
                        "bifacial": {
                            "enabled": True,
                            "ground_albedo": 0.28,
                            "height_above_ground_m": 1.8,
                            "rear_shading_factor": 0.05,
                            "mismatch_loss_factor": 0.10,
                        },
                    }
                    err, prev = _decode(await session.call_tool("pvsyst_simulate_design", {
                        "spec": design_spec,
                        "dry_run": True,
                    }))
                    self.assertFalse(err, prev)
                    self.assertEqual(prev["status"], "ready", prev)

                    err, done = _decode(await session.call_tool("pvsyst_simulate_design", {
                        "spec": design_spec,
                        "dry_run": False,
                    }))
                    self.assertFalse(err, done)
                    self.assertEqual(done["status"], "needs_review", done)
                    self.assertTrue(done["native_consumption_verified"], done)
                    self.assertGreater(done["metrics"]["grid_energy_kwh"], 0.0)
                    self.assertGreater(done["bifacial_metrics"]["rear_irradiation_kwh_m2"], 50.0)
                    self.assertIn("diagnostics", done)
                    self.assertIn(done["diagnostics"]["status"], ("ok", "anomalies_detected"))

                    # 3. Deep result diagnostics with .VC0 root-cause attribution (0 CLI execs)
                    csv_name = Path(done["artifacts"]["simulate"]["csv"]).name
                    err, diag = _decode(await session.call_tool("pvsyst_diagnose_results", {
                        "csv_name": csv_name,
                        "project": "E2E_Trk_Bifi.PRJ",
                        "variant": "VC0",
                    }))
                    self.assertFalse(err, diag)
                    self.assertIn(diag["status"], ("ok", "anomalies_detected"))
                    self.assertEqual(diag["rows"], 8760)
                    self.assertTrue(diag["variant_attribution_used"])
                    self.assertTrue(diag["is_bifacial"])

                    # 4. Parametric batch sweep in ONE CLI execution (1 CLI exec)
                    sweep_spec = {
                        "project": "E2E_Trk_Bifi.PRJ",
                        "variant": "VC0",
                        "sweep_name": "E2E_TrkSweep",
                        "grid": {
                            "soiling_loss_pct": [1.0, 4.0],
                            "albedo": [20.0, 35.0],
                        },
                        "result_variables": ["GlobInc", "GlobEff", "EArray", "E_Grid", "PR"],
                        "sort_by": "E_Grid",
                        "sort_descending": True,
                    }
                    err, sprev = _decode(await session.call_tool("pvsyst_run_batch_sweep", {
                        "spec": sweep_spec,
                        "dry_run": True,
                    }))
                    self.assertFalse(err, sprev)
                    self.assertEqual(sprev["status"], "ready")
                    self.assertEqual(sprev["scenario_count"], 4)

                    err, sdone = _decode(await session.call_tool("pvsyst_run_batch_sweep", {
                        "spec": sweep_spec,
                        "dry_run": False,
                    }))
                    self.assertFalse(err, sdone)
                    self.assertIn(sdone["status"], ("completed", "needs_review"), sdone)
                    self.assertTrue(sdone["native_consumption_verified"])
                    self.assertEqual(sdone["completed_count"], 4)
                    self.assertEqual(sdone["failed_count"], 0)
                    # Best scenario should be lowest soiling (1%) + highest albedo (35%)
                    best = sdone["best_scenario"]
                    self.assertEqual(best["parameters"]["soiling_loss_pct"], 1.0)
                    self.assertEqual(best["parameters"]["albedo"], 35.0)
                    self.assertGreater(best["values"]["E_Grid"], sdone["ranked_scenarios"][-1]["values"]["E_Grid"])

        asyncio.run(run())

    def test_mcp_v030_subhourly_15min_weather_and_peak_shaving_storage_e2e(self):
        async def run():
            self._stage_15min_weather_csv("Geneva_2023_15min.csv", year=2023)
            async with stdio_client(self._mcp_params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    # 1. Prepare 15-min weather CSV -> SIT + SubHour MET (2 CLI execs: create-site + convert-meteo)
                    err, wprev = _decode(await session.call_tool("pvsyst_prepare_weather", {
                        "csv_name": "Geneva_2023_15min.csv",
                        "year": 2023,
                        "sit_name": "GenevaSub15.SIT",
                        "met_name": "GenevaSub15_2023.MET",
                        "country_code": "CH",
                        "time_basis": "local_standard_interval_start",
                        "step_minutes": 15,
                        "dry_run": True,
                    }))
                    self.assertFalse(err, wprev)
                    self.assertEqual(wprev["status"], "ready", wprev)
                    self.assertEqual(wprev["step_minutes"], 15)

                    err, wdone = _decode(await session.call_tool("pvsyst_prepare_weather", {
                        "csv_name": "Geneva_2023_15min.csv",
                        "year": 2023,
                        "sit_name": "GenevaSub15.SIT",
                        "met_name": "GenevaSub15_2023.MET",
                        "country_code": "CH",
                        "time_basis": "local_standard_interval_start",
                        "step_minutes": 15,
                        "dry_run": False,
                    }))
                    self.assertFalse(err, wdone)
                    self.assertIn(wdone["status"], ("prepared", "needs_review"), wdone)
                    self.assertEqual(wdone["step_minutes"], 15)
                    self.assertTrue(wdone["met_declarations"]["valid"], wdone["met_declarations"])

                    # 2. Build PeakShaving AC storage variant on DemoHouse.VC0 -> VC5 (0 CLI execs)
                    batt = {
                        "file_name": "Generic_LI_Module_180Ah.BTR",
                        "library": "builtin",
                        "series_count": 1,
                        "parallel_count": 2,
                        "monthly_temperature_c": [20] * 12,
                        "cycle_sow": 1.0,
                        "static_sow": 1.0,
                    }
                    err, pk = _decode(await session.call_tool("pvsyst_create_grid_storage_system", {
                        "project": "DemoHouse.PRJ",
                        "source_variant": "VC0",
                        "new_variant": "VC5",
                        "battery": batt,
                        "storage": {
                            "strategy": "peak_shaving",
                            "charging_strategy": "pv_above_thresh",
                            "discharging_strategy": "hour_window",
                            "discharge_start_hour": 19,
                            "discharge_end_hour": 6,
                            "grid_limit_kw": 2.5,
                            "soc_min": 0.2,
                            "soc_max": 0.95,
                            "initial_soc": 0.5,
                            "reserve_soc": 0.4,
                            "max_charge_ac_kw": 5.0,
                            "max_discharge_ac_kw": 5.0,
                            "charger_max_efficiency_pct": 97.0,
                            "charger_euro_efficiency_pct": 95.0,
                            "battery_inverter_max_efficiency_pct": 97.0,
                            "battery_inverter_euro_efficiency_pct": 95.0,
                        },
                        "dry_run": False,
                    }))
                    self.assertFalse(err, pk)
                    self.assertEqual(pk["status"], "created", pk)

                    # 3. Build sub-hourly & hourly SFIs (0 CLI execs)
                    err, sfi_sub = _decode(await session.call_tool("pvsyst_build_sfi", {
                        "filename": "e2e_sub15_storage.SFI",
                        "variables": "grid_storage",
                        "time_step": "subhour",
                    }))
                    self.assertFalse(err, sfi_sub)
                    self.assertEqual(sfi_sub["time_step"], "subhour")

                    err, sfi_hr = _decode(await session.call_tool("pvsyst_build_sfi", {
                        "filename": "e2e_hr60_storage.SFI",
                        "variables": "grid_storage",
                        "time_step": "hour",
                    }))
                    self.assertFalse(err, sfi_hr)

                    # 4. Run 15-min sub-hourly AND 60-min hourly PeakShaving simulations (2 CLI execs)
                    err, sim_sub = _decode(await session.call_tool("pvsyst_run_simulation", {
                        "project": "DemoHouse.PRJ",
                        "variant": "VC5",
                        "sfi_name": "e2e_sub15_storage.SFI",
                        "csv_name": "e2e_pk_15m.csv",
                        "met_name": "GenevaSub15_2023.MET",
                        "site_name": "GenevaSub15.SIT",
                        "time_step": "subhour",
                        "start_date": "23.06.01",
                        "end_date": "23.06.07",
                        "allow_window_creation": False,
                    }))
                    self.assertFalse(err, sim_sub)

                    err, sim_hr = _decode(await session.call_tool("pvsyst_run_simulation", {
                        "project": "DemoHouse.PRJ",
                        "variant": "VC5",
                        "sfi_name": "e2e_hr60_storage.SFI",
                        "csv_name": "e2e_pk_60m.csv",
                        "met_name": "GenevaSub15_2023.MET",
                        "site_name": "GenevaSub15.SIT",
                        "time_step": "hour",
                        "start_date": "23.06.01",
                        "end_date": "23.06.07",
                        "allow_window_creation": False,
                    }))
                    self.assertFalse(err, sim_hr)

                    # 5. Verify step accuracy (15 min vs 60 min), storage energy flows, and diagnostics
                    err, r15 = _decode(await session.call_tool("pvsyst_read_results", {
                        "csv_name": "e2e_pk_15m.csv",
                        "columns": "EArray,EBatCh,EBatDis,E_Grid",
                    }))
                    self.assertFalse(err, r15)
                    self.assertEqual(r15["step_minutes"], 15.0)
                    self.assertEqual(r15["rows"], 7 * 96)
                    self.assertGreater(r15["summary"]["E_Grid"]["energy_kwh"], 0.0)
                    self.assertGreater(r15["summary"]["EBatCh"]["sum"], 0.0)
                    self.assertGreater(r15["summary"]["EBatDis"]["sum"], 0.0)

                    err, r60 = _decode(await session.call_tool("pvsyst_read_results", {
                        "csv_name": "e2e_pk_60m.csv",
                        "columns": "EArray,EBatCh,EBatDis,E_Grid",
                    }))
                    self.assertFalse(err, r60)
                    self.assertEqual(r60["step_minutes"], 60.0)
                    self.assertEqual(r60["rows"], 7 * 24)
                    self.assertGreater(r60["summary"]["E_Grid"]["energy_kwh"], 0.0)

                    err, diag15 = _decode(await session.call_tool("pvsyst_diagnose_results", {
                        "csv_name": "e2e_pk_15m.csv",
                        "project": "DemoHouse.PRJ",
                        "variant": "VC5",
                    }))
                    self.assertFalse(err, diag15)
                    self.assertEqual(diag15["step_minutes"], 15.0)
                    self.assertEqual(diag15["rows"], 7 * 96)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()

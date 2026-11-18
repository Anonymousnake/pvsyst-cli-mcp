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

Env (required; skipped cleanly when absent so offline CI stays isolated):
  PVSYST_E2E_REAL_NATIVE=1
  PVSYST_E2E_CLI=<path to PVsystCLI.exe>
Optional:
  PVSYST_E2E_DATARO=<path to DataRO\\PVsyst8.1_Data>
"""
from __future__ import annotations

import asyncio
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
                "allow_window_creation": False,
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
                    self.assertEqual(done["phase"], "completed", done)
                    self.assertIn(done["status"], ("completed", "needs_review"), done)
                    self.assertTrue(done["native_consumption_verified"])
                    self.assertGreater(done["metrics"]["array_energy_kwh"], 0.0)
                    self.assertGreater(done["metrics"]["grid_energy_kwh"], 0.0)
                    self.assertEqual(done["hourly_rows"], 8760)
                    self.assertIn("ShdElec", str(done.get("electrical_shading_output")))
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


if __name__ == "__main__":
    unittest.main()

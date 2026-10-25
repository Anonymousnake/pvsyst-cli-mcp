"""Public AC-storage task/comparison/delivery contract using explicit completed native runs."""
import asyncio
import csv
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
import zipfile

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


REPO = Path(__file__).resolve().parents[2]
CASES = (
    ("原生3kW充电基准", "AC_STORAGE_BASELINE.PRJ", "AC_STORAGE_BASELINE_engineering_2024"),
    ("原生充电限制候选", "AC_STORAGE_CANDIDATE.PRJ", "AC_STORAGE_CANDIDATE_engineering_2024"),
)


class EngineeringStorageBoundary(unittest.TestCase):
    def test_bound_storage_task_comparison_and_delivery(self):
        configured = os.environ.get("PVSYST_ENGINEERING_STORAGE_FIXTURE")
        if not configured:
            self.skipTest("Explicit storage-round2 completed native runs required; no native claim")
        asyncio.run(self.check(Path(configured)))

    async def check(self, fixture):
        directory = Path(tempfile.mkdtemp(prefix="pvsyst-engineering-storage-"))
        try:
            root = directory / "PVsyst8.1_Data"
            allowed_folders = {"Projects", "Results", "Models", "Sites", "Meteo", "UserData",
                               "UserHourlyParams", "ComposPV/PVmodules", "ComposPV/Inverters",
                               "ComposPV/Batteries"}

            def copy_asset(folder, name):
                self.assertIn(folder, allowed_folders)
                self.assertEqual(Path(name).name, name)
                self.assertNotIn("\\", name)
                target = root / folder / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fixture / folder / name, target)

            def relocate_output(value):
                if isinstance(value, dict):
                    return {key: relocate_output(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [relocate_output(item) for item in value]
                if isinstance(value, str):
                    path = Path(value)
                    if path.is_absolute():
                        try:
                            return str(root / path.relative_to(fixture))
                        except ValueError:
                            pass
                return value

            scenarios, records = [], []
            for label, project, stem in CASES:
                name = stem + ".json"
                record = json.loads((fixture / "Results" / name).read_text(encoding="utf-8"))
                self.assertEqual(record["operation"], "engineering_revision")
                self.assertEqual(record["working_project"], project)
                self.assertEqual(record["working_variant"], "VCSTORAGE")
                self.assertTrue(record["native_consumption_verified"])
                self.assertTrue(record["load_binding"]["embedded_snapshot_matches"])
                for member in record["working_files"]:
                    self.assertRegex(member, r"(?i)\.(PRJ|VC[A-Z0-9]+)$")
                    copy_asset("Projects", member)
                for asset in record["input_assets"].values():
                    self.assertEqual(asset["library"], "workspace")
                    copy_asset(asset["folder"], asset["name"])
                for kind in ("csv", "pdf"):
                    self.assertEqual(Path(record["artifacts"][kind]).name, stem + "." + kind)
                    copy_asset("Results", stem + "." + kind)
                copy_asset("Models", Path(record["artifacts"]["sfi"]).name)
                # Saved native PathName and hashed load records stay byte/value faithful to their run.
                for key in ("artifacts", "run"):
                    if key in record:
                        record[key] = relocate_output(record[key])
                (root / "Results" / name).write_text(
                    json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
                records.append(record)
                scenarios.append({"label": label, "project": project, "variant": "VCSTORAGE",
                    "csv_name": stem + ".csv", "run_manifest_name": name, "year": 2024,
                    "declared_changes": [] if not scenarios else ["storage"]})

            originals = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            members = set(root.rglob("*"))
            executable = directory / "PVsystCLI.exe"
            executable.write_bytes(b"No native execution in engineering storage contract")
            environment = dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(executable))
            environment.pop("PVSYST_BUILTIN_COMPONENTS", None)
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO), env=environment)
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, arguments):
                        reply = await session.call_tool(tool, arguments)
                        self.assertFalse(reply.is_error, reply.content)
                        return json.loads("\n".join(item.text for item in reply.content if hasattr(item, "text")))

                    candidate = records[1]
                    load_name = candidate["load_binding"]["manifest_name"]
                    spec = {"objective": "检查既有AC光储在负荷输入错误时先停止，不调用原生程序",
                        "source_project": "AC_STORAGE_CANDIDATE.PRJ", "source_variant": "VCSTORAGE",
                        "new_project": "CONTRACT_STORAGE_PREVIEW.PRJ", "new_variant": "VCPREVIEW",
                        "year": 2024, "nominal_dc_power_kw": 36,
                        "power_source": "isolated native fixture declaration",
                        "changes": {"storage": {"max_charge_ac_kw": 2}}, "load_manifest_name": load_name}
                    asset = candidate["input_assets"]["load_source"]
                    raw_path = root / asset["folder"] / asset["name"]
                    raw = raw_path.read_bytes()
                    lines = raw.decode("utf-8-sig").splitlines(keepends=True)
                    row_index = next(index for index, line in enumerate(lines)
                                     if re.match(r"^\s*2024-\d{2}-\d{2} \d{2}:\d{2}[;,]", line))
                    separator = ";" if ";" in lines[row_index] else ","
                    row = next(csv.reader([lines[row_index]], delimiter=separator))
                    row[1] = format(float(row[1]) + 1, ".12g")
                    stream = io.StringIO(newline="")
                    csv.writer(stream, delimiter=separator,
                        lineterminator="\r\n" if lines[row_index].endswith("\r\n") else "\n").writerow(row)
                    lines[row_index] = stream.getvalue()
                    raw_path.write_bytes("".join(lines).encode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"))
                    stopped = await call("pvsyst_run_engineering_task", {"spec": spec, "dry_run": True})
                    self.assertEqual(stopped["status"], "blocked", stopped)
                    reason = json.dumps(stopped["issues"], ensure_ascii=False).lower()
                    self.assertTrue(any(word in reason for word in ("source", "load", "负荷")), stopped)
                    self.assertFalse(stopped["native_consumption_verified"])
                    self.assertIsNone(stopped.get("run"))
                    self.assertEqual(set(root.rglob("*")), members)
                    raw_path.write_bytes(raw)

                    load_path = root / "Results" / load_name
                    load_raw = load_path.read_bytes()
                    load_record = json.loads(load_raw)
                    self.assertEqual(load_record["utc_offset_hours"], 8)
                    load_record["utc_offset_hours"] = 7
                    load_path.write_text(json.dumps(load_record, ensure_ascii=False, allow_nan=False), encoding="utf-8")
                    stopped = await call("pvsyst_run_engineering_task", {"spec": spec, "dry_run": True})
                    self.assertEqual(stopped["status"], "blocked", stopped)
                    reason = json.dumps(stopped["issues"], ensure_ascii=False).lower()
                    self.assertTrue(any(word in reason for word in ("offset", "time", "zone", "时区", "时基")), stopped)
                    self.assertFalse(stopped["native_consumption_verified"])
                    self.assertIsNone(stopped.get("run"))
                    self.assertEqual(set(root.rglob("*")), members)
                    load_path.write_bytes(load_raw)

                    arguments = {"objective": "同一逐时负荷、组件和天气下比较既有AC光储充电限制的原生全年输出",
                        "scenarios": scenarios, "output_name": "ContractStorageComparison.json", "dry_run": True}
                    preview = await call("pvsyst_compare_engineering_scenarios", arguments)
                    self.assertEqual(preview["status"], "ready", preview)
                    self.assertTrue(preview["historical_input_binding_verified"])
                    self.assertTrue(preview["changes"][0]["load_input_equivalence_verified"])
                    self.assertIn("load", preview["changes"][0]["common_groups"])
                    self.assertEqual(preview["changes"][0]["undeclared_fields"], [])
                    metric = preview["metrics"][0]
                    self.assertEqual(metric["rows"], 8784)
                    for name, expected in {"EFrGrid": 1071.9666, "E_Grid": 1544.6303,
                                           "EBatCh": -1462.2076, "EBatDis": -1120.2297}.items():
                        self.assertEqual(metric["summary"][name]["unit"], "kW")
                        self.assertAlmostEqual(metric["summary"][name]["delta_energy_kwh"], expected, delta=0.0001)
                    for item in preview["scenarios"]:
                        period = item["result_summary"]
                        self.assertTrue(period["complete_year"])
                        self.assertEqual(period["rows"], 8784)
                        self.assertAlmostEqual(period["columns"]["E_User"]["energy_kwh"], 33285.6, delta=0.0001)
                        self.assertEqual(period["columns"]["E_Miss"]["energy_kwh"], 0)
                        self.assertEqual(period["columns"]["SOC_Beg"]["unit"], "ratio")
                        self.assertAlmostEqual(period["columns"]["SOC_Beg"]["first_value"], 0.5, delta=0.0001)
                    self.assertIn("SOC_Beg", metric["soc_changes"])
                    text = "\n".join(preview["summary"])
                    self.assertIn("电网购电", text)
                    self.assertIn("电池DC侧充电", text)
                    self.assertIn("SOC", text)
                    self.assertEqual(preview["written_artifacts"], [])
                    self.assertEqual(set(root.rglob("*")), members)
                    self.assertTrue(all((root / name).read_bytes() == value for name, value in originals.items()))

                    arguments["dry_run"] = False
                    created = await call("pvsyst_compare_engineering_scenarios", arguments)
                    self.assertEqual(created["status"], "created", created)
                    self.assertEqual(set(created["written_artifacts"]), {"json", "csv", "markdown"})
                    exported = await call("pvsyst_export_engineering_delivery", {
                        "manifest_name": "AC_STORAGE_CANDIDATE_engineering_2024.json",
                        "comparison_name": "ContractStorageComparison.json", "output_name": "storage-comparison-delivery.zip",
                        "dry_run": False})
                    self.assertEqual(exported["status"], "exported", exported)
                    with zipfile.ZipFile(root / "Results" / "storage-comparison-delivery.zip") as package:
                        expected = {"summary.md", "inputs.md", "comparison.md", "comparison.csv", "comparison.json"}
                        for _, project, stem in CASES:
                            expected.update({"Projects/" + project, "Projects/" + Path(project).stem + ".VCSTORAGE",
                                             "Results/" + stem + ".csv", "Results/" + stem + ".pdf"})
                        self.assertTrue(expected.issubset(package.namelist()))
                        self.assertEqual(package.read("comparison.json"),
                                         (root / "Results" / "ContractStorageComparison.json").read_bytes())
                    self.assertTrue(all((root / name).read_bytes() == value for name, value in originals.items()))
        except BaseException:
            print(f"Engineering storage failure artifacts retained: {directory}", file=sys.stderr)
            raise
        else:
            shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()

"""Public comparison contract using explicitly supplied native runs; no native execution."""
import asyncio
import copy
import csv
from datetime import datetime, timedelta
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
    ("固定28度基准", "ENGINEERING_REFERENCE.PRJ", "VCBASE", "ENGINEERING_REFERENCE_engineering_2024"),
    ("固定30度候选", "ENGINEERING_FIXED.PRJ", "VCRUN", "ENGINEERING_FIXED_engineering_2024"),
)


def altered_csv(source, target, change):
    """Change only the necessary units/time input in an unbound disposable native CSV copy."""
    raw = source.read_bytes()
    lines = raw.decode("utf-8-sig").splitlines(keepends=True)
    header = next(index for index, line in enumerate(lines) if re.match(r"^\s*date[;,]", line, re.I))
    separator = re.match(r"^\s*date([;,])", lines[header], re.I)[1]
    columns = next(csv.reader([lines[header]], delimiter=separator))
    index = header + 1 if change == "units" else next(
        index for index in range(header + 2, len(lines))
        if re.match(r"^\s*\d{2}/\d{2}/\d{2} \d{2}:\d{2}[;,]", lines[index]))
    row = next(csv.reader([lines[index]], delimiter=separator))
    if change == "units":
        column = [name.strip() for name in columns].index("E_Grid")
        row[column] = "W" if row[column].strip() == "kW" else "kW"
    else:
        stamp = datetime.strptime(row[0].strip(), "%d/%m/%y %H:%M")
        row[0] = (stamp + timedelta(minutes=30)).strftime("%d/%m/%y %H:%M")
    stream = io.StringIO(newline="")
    csv.writer(stream, delimiter=separator,
               lineterminator="\r\n" if lines[index].endswith("\r\n") else "\n").writerow(row)
    lines[index] = stream.getvalue()
    target.write_bytes("".join(lines).encode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"))


class EngineeringScenarioBoundary(unittest.TestCase):
    def test_bound_native_comparison_and_public_boundaries(self):
        configured = os.environ.get("PVSYST_ENGINEERING_RUN_FIXTURE")
        if not configured:
            self.skipTest("Explicit round1 native engineering-run fixture required; no native claim")
        asyncio.run(self.check(Path(configured)))

    async def check(self, fixture):
        directory = Path(tempfile.mkdtemp(prefix="pvsyst-engineering-scenarios-"))
        try:
            root = directory / "PVsyst8.1_Data"
            allowed_folders = {"Projects", "Results", "Models", "Sites", "Meteo",
                               "ComposPV/PVmodules", "ComposPV/Inverters"}

            def copy_asset(folder, name):
                self.assertIn(folder, allowed_folders)
                self.assertEqual(Path(name).name, name)
                self.assertNotIn("\\", name)
                target = root / folder / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fixture / folder / name, target)

            def relocate(value):
                if isinstance(value, dict):
                    return {key: relocate(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [relocate(item) for item in value]
                if isinstance(value, str):
                    path = Path(value)
                    if path.is_absolute():
                        try:
                            return str(root / path.relative_to(fixture))
                        except ValueError:
                            pass
                return value

            scenarios = []
            for label, project, variant, stem in CASES:
                name = stem + ".json"
                record = json.loads((fixture / "Results" / name).read_text(encoding="utf-8"))
                self.assertEqual(record["operation"], "engineering_revision")
                self.assertEqual(record["working_project"], project)
                self.assertEqual(record["working_variant"], variant)
                self.assertTrue(record["native_consumption_verified"])
                for member in record["working_files"]:
                    self.assertRegex(member, r"(?i)\.(PRJ|VC[A-Z0-9]+)$")
                    copy_asset("Projects", member)
                for asset in record["input_assets"].values():
                    self.assertEqual(asset["library"], "workspace")
                    copy_asset(asset["folder"], asset["name"])
                for kind in ("csv", "pdf"):
                    self.assertEqual(Path(record["artifacts"][kind]).name, stem + "." + kind)
                    copy_asset("Results", stem + "." + kind)
                sfi_name = Path(record["artifacts"]["sfi"]).name
                copy_asset("Models", sfi_name)
                (root / "Results" / name).write_text(
                    json.dumps(relocate(record), ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                    encoding="utf-8")
                scenarios.append({"label": label, "project": project, "variant": variant,
                                  "csv_name": stem + ".csv", "run_manifest_name": name,
                                  "year": 2024, "declared_changes": [] if not scenarios else ["orientations"]})

            originals = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            members = set(root.rglob("*"))
            executable = directory / "PVsystCLI.exe"
            executable.write_bytes(b"No native execution in engineering comparison contract")
            environment = dict(os.environ, PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(executable))
            environment.pop("PVSYST_BUILTIN_COMPONENTS", None)
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(REPO / "pvsyst_mcp_server.py")], cwd=str(REPO), env=environment)
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def compare(items, output="ContractComparison.json", dry_run=True):
                        reply = await session.call_tool("pvsyst_compare_engineering_scenarios", {
                            "objective": "比较同一天气、组件和容量下28度与30度固定平面的原生全年结果",
                            "scenarios": items, "output_name": output, "dry_run": dry_run})
                        self.assertFalse(reply.is_error, reply.content)
                        return json.loads("\n".join(item.text for item in reply.content if hasattr(item, "text")))

                    preview = await compare(scenarios)
                    self.assertEqual(preview["status"], "ready", preview)
                    self.assertTrue(preview["historical_input_binding_verified"])
                    metric = preview["metrics"][0]
                    self.assertEqual(metric["rows"], 8784)
                    self.assertAlmostEqual(metric["summary"]["E_Grid"]["delta_energy_kwh"],
                                           189.6017, delta=0.0001)
                    self.assertEqual(preview["changes"][0]["undeclared_fields"], [])
                    self.assertEqual(preview["written_artifacts"], [])
                    self.assertEqual(set(root.rglob("*")), members)
                    self.assertTrue(all((root / name).read_bytes() == raw for name, raw in originals.items()))

                    unbound = copy.deepcopy(scenarios)
                    for item in unbound:
                        item.pop("run_manifest_name")
                    review = await compare(unbound, "ContractUnbound.json")
                    self.assertEqual(review["status"], "needs_review", review)
                    self.assertFalse(review["historical_input_binding_verified"])
                    self.assertTrue(review["review_items"])
                    self.assertEqual(set(root.rglob("*")), members)

                    for change, expected_reason in (("units", "unit"), ("time", "timestamp")):
                        name = "Contract_" + change + ".csv"
                        altered_csv(root / "Results" / scenarios[1]["csv_name"], root / "Results" / name, change)
                        negative = copy.deepcopy(unbound)
                        negative[1]["csv_name"] = name
                        before = set(root.rglob("*"))
                        stopped = await compare(negative, "ContractRejected_" + change + ".json")
                        self.assertEqual(stopped["status"], "blocked", stopped)
                        self.assertIn(expected_reason, " ".join(stopped["issues"]).lower())
                        self.assertEqual(set(root.rglob("*")), before)

                    created = await compare(scenarios, dry_run=False)
                    self.assertEqual(created["status"], "created", created)
                    self.assertTrue(created["historical_input_binding_verified"])
                    outputs = {kind: root / "Results" / name for kind, name in (
                        ("json", "ContractComparison.json"), ("csv", "ContractComparison.csv"),
                        ("markdown", "ContractComparison.md"))}
                    self.assertEqual(set(created["written_artifacts"]), set(outputs))
                    written = {path: path.read_bytes() for path in outputs.values()}
                    before = set(root.rglob("*"))
                    refused = await compare(scenarios, dry_run=False)
                    self.assertEqual(refused["status"], "blocked", refused)
                    self.assertTrue(any("new filenames" in issue for issue in refused["issues"]))
                    self.assertEqual(set(root.rglob("*")), before)
                    self.assertTrue(all(path.read_bytes() == raw for path, raw in written.items()))

                    async def export(name):
                        reply = await session.call_tool("pvsyst_export_engineering_delivery", {
                            "manifest_name": "ENGINEERING_FIXED_engineering_2024.json",
                            "comparison_name": "ContractComparison.json", "output_name": name,
                            "dry_run": False})
                        self.assertFalse(reply.is_error, reply.content)
                        return json.loads("\n".join(item.text for item in reply.content if hasattr(item, "text")))

                    exported = await export("comparison-delivery.zip")
                    self.assertEqual(exported["status"], "exported", exported)
                    with zipfile.ZipFile(root / "Results" / "comparison-delivery.zip") as package:
                        expected = {"summary.md", "inputs.md", "comparison.md", "comparison.csv", "comparison.json"}
                        for _, project, variant, stem in CASES:
                            expected.update({"Projects/" + project,
                                "Projects/" + Path(project).stem + "." + variant,
                                "Results/" + stem + ".csv", "Results/" + stem + ".pdf"})
                        self.assertEqual(set(package.namelist()), expected)
                        self.assertEqual(package.read("comparison.json"), written[outputs["json"]])

                    with outputs["markdown"].open("a", encoding="utf-8") as stream:
                        stream.write("\nTemporary changed report: delivery must reject stale binding.\n")
                    before = set(root.rglob("*"))
                    drift = await export("comparison-delivery-drift.zip")
                    self.assertEqual(drift["status"], "blocked", drift)
                    self.assertTrue(drift["issues"])
                    self.assertFalse((root / "Results" / "comparison-delivery-drift.zip").exists())
                    self.assertEqual(set(root.rglob("*")), before)
                    self.assertTrue(all((root / name).read_bytes() == raw for name, raw in originals.items()))
        except BaseException:
            print(f"Engineering scenario failure artifacts retained: {directory}", file=sys.stderr)
            raise
        else:
            shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()

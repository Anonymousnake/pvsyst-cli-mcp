"""Public two-roof resume/delivery contract using one explicit completed native run."""
import asyncio
import copy
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


class TwoRoofDeliveryBoundary(unittest.TestCase):
    def test_completed_native_two_roof_resume_and_delivery(self):
        configured = os.environ.get("PVSYST_HOUSE_DELIVERY_FIXTURE") or os.environ.get("PVSYST_TWO_ROOF_DELIVERY_FIXTURE")
        if not configured:
            self.skipTest("Set the explicit completed two-roof house-task manifest; no native claim")
        manifest = Path(configured)
        self.assertTrue(manifest.is_file(), manifest)
        asyncio.run(self.check(manifest))

    async def check(self, manifest):
        fixture = manifest.parent.parent
        fixture_manifest = manifest.read_bytes()
        record = json.loads(fixture_manifest)
        self.assertEqual(record["operation"], "engineering_house")
        self.assertEqual(record["phase"], "completed")
        self.assertIn(record["status"], ("completed", "needs_review"))
        self.assertTrue(record["native_consumption_verified"])
        native_method = record["request"].get("scene_import_method") == "native"
        output_kinds = ("csv",) if record["request"].get("report_pdf") is False else ("csv", "pdf")
        if native_method:
            self.assertEqual(record["native_import"]["status"], "native_variant_created")
            self.assertTrue(record["native_import"]["stored_geometry_verified"])
        else:
            self.assertTrue(record["native_import"]["native_reload_verified"])
            self.assertTrue(record["native_import"]["native_reload"]["native_validity"]["valid"])
        self.assertEqual([item["id"] for item in record["plan"]["source"]["orientations"]], [1, 2])
        spec = copy.deepcopy(record["request"])
        self.assertEqual({table["orientation_id"] for table in spec["layout"]["tables"]}, {1, 2})
        directory = Path(tempfile.mkdtemp(prefix="pvsyst-two-roof-delivery-"))
        try:
            root = directory / "PVsyst8.1_Data"
            source_bytes = {manifest: fixture_manifest}
            allowed_folders = {"Projects", "Results", "Models", "UserData", "Sites", "Meteo",
                               "ComposPV/PVmodules", "ComposPV/Inverters"}

            def copy_asset(folder, name):
                self.assertIn(folder, allowed_folders)
                self.assertEqual(Path(name).name, name)
                self.assertNotIn("\\", name)
                self.assertNotIn("/", name)
                source = fixture / folder / name
                source_bytes[source] = source.read_bytes()
                target = root / folder / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

            for name in record["working_files"]:
                self.assertRegex(name, r"(?i)\.(PRJ|VC[A-Z0-9]+)$")
                copy_asset("Projects", name)
            copy_asset("Projects", spec["source_project"])
            copy_asset("Projects", Path(spec["source_project"]).stem + "." + spec["source_variant"].upper())
            for asset in record["input_assets"].values():
                self.assertEqual(asset["library"], "workspace")
                copy_asset(asset["folder"], asset["name"])
            for kind in output_kinds:
                copy_asset("Results", Path(record["artifacts"][kind]).name)

            def relocate(value):
                return str(root / Path(value).relative_to(fixture))

            # Relocate runtime file handles; saved input snapshots and production guards remain faithful.
            record["artifacts"] = {key: relocate(value) for key, value in record["artifacts"].items()}
            record["native_import"]["file"] = relocate(record["native_import"]["file"])
            manifest_path = root / "Results" / manifest.name
            manifest_path.write_text(json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                                     encoding="utf-8")
            comparison_csv = root / "Results" / "comparison-observed-copy.csv"
            shutil.copy2(root / "Results" / Path(record["artifacts"]["csv"]).name, comparison_csv)
            originals = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            members = set(root.rglob("*"))
            executable = directory / "PVsystCLI.exe"
            executable.write_bytes(b"No native execution in completed two-roof delivery contract")
            environment = {key: value for key, value in os.environ.items() if not key.startswith("PVSYST_")}
            environment.update(PVSYST_WORKSPACE=str(root), PVSYST_CLI=str(executable), PYTHONDONTWRITEBYTECODE="1")
            parameters = StdioServerParameters(command=sys.executable, args=[str(REPO / "pvsyst_mcp_server.py")],
                cwd=str(REPO), env=environment)
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(tool, arguments):
                        reply = await session.call_tool(tool, arguments)
                        self.assertFalse(reply.is_error, reply.content)
                        return json.loads("\n".join(item.text for item in reply.content if hasattr(item, "text")))

                    def unchanged(expected=originals, expected_members=members):
                        self.assertEqual(set(root.rglob("*")), expected_members)
                        self.assertTrue(all((root / name).read_bytes() == value for name, value in expected.items()))

                    house = "pvsyst_run_house_task"
                    resumed = await call(house, {"spec": spec, "resume": True, "dry_run": True})
                    self.assertEqual(resumed["status"], "ready", resumed)
                    self.assertFalse(resumed["native_consumption_verified"])
                    self.assertEqual(resumed["runs"], record["runs"])
                    self.assertEqual(resumed["input_handoff"], record["input_handoff"])
                    self.assertEqual(resumed["native_import"], record["native_import"])
                    unchanged()

                    for mutation in ("wrong_source", "unknown_region"):
                        bad = copy.deepcopy(spec)
                        bad["new_project"] = "ContractTwoRoofRejected.PRJ"
                        if mutation == "wrong_source":
                            bad.update(source_project=record["working_project"], source_variant=record["working_variant"])
                        else:
                            bad["layout"]["tables"][1]["orientation_id"] = 3
                        rejected = await call(house, {"spec": bad, "dry_run": False, "confirm": True})
                        self.assertEqual(rejected["status"], "blocked", (mutation, rejected))
                        self.assertFalse(rejected["native_consumption_verified"])
                        self.assertEqual(rejected["artifacts"], {})
                        unchanged()

                    saved_variant = root / "Projects" / (Path(record["working_project"]).stem + "." + record["working_variant"])
                    saved_bytes = saved_variant.read_bytes()
                    match = re.search(rb"(?s)(PVObject_[^=\r\n]*=pvShdGenObject\b.*?\bDOrigine=)([-+0-9.eE]+)(,)", saved_bytes)
                    self.assertIsNotNone(match, "Completed fixture must contain a native house origin")
                    moved = format(float(match[2]) + 0.25, ".12g").encode("ascii")
                    saved_variant.write_bytes(saved_bytes[:match.start(2)] + moved + saved_bytes[match.end(2):])
                    changed = dict(originals)
                    changed[saved_variant.relative_to(root)] = saved_variant.read_bytes()
                    rejected = await call(house, {"spec": spec, "resume": True, "dry_run": True})
                    self.assertEqual(rejected["status"], "blocked", rejected)
                    self.assertFalse(rejected["native_consumption_verified"])
                    unchanged(changed)
                    saved_variant.write_bytes(saved_bytes)

                    legacy = copy.deepcopy(record)
                    for key in ("operation", "schema_version", "working_project", "working_variant", "year", "objective",
                                "nominal_dc_power_kw", "input_handoff", "working_files", "input_assets",
                                "database_references", "changes", "cli_version", "result_files"):
                        legacy.pop(key, None)
                    manifest_path.write_text(json.dumps(legacy, ensure_ascii=False, allow_nan=False), encoding="utf-8")
                    legacy_bytes = manifest_path.read_bytes()
                    changed = dict(originals)
                    changed[manifest_path.relative_to(root)] = legacy_bytes
                    resumed = await call(house, {"spec": spec, "resume": True, "dry_run": True})
                    self.assertEqual(resumed["status"], "ready", resumed)
                    self.assertNotIn("input_handoff", resumed)
                    self.assertNotIn("operation", resumed)
                    self.assertEqual(resumed["runs"], legacy["runs"])
                    delivery = "pvsyst_export_engineering_delivery"
                    arguments = {"manifest_name": manifest.name, "output_name": "two-roof-delivery.zip", "dry_run": True}
                    rejected = await call(delivery, arguments)
                    self.assertEqual(rejected["status"], "blocked", rejected)
                    unchanged(changed)
                    manifest_path.write_bytes(originals[manifest_path.relative_to(root)])

                    current = await call("pvsyst_project_handoff", {"project": record["working_project"],
                        "variant": record["working_variant"], "declarations": record["input_handoff"].get("declarations")})
                    corruptions = ["consumption", "csv_guard", "saved_input", "inverter_state"]
                    if output_kinds == ("csv",):
                        corruptions.extend(["pdf_required", "unrequested_pdf", "nonboolean_pdf"])
                    if native_method:
                        corruptions.extend(["stored_geometry", "geometry_evidence", "import_route"])
                        if record["plan"]["shading_mode"] == "module_layout":
                            corruptions.append("stored_layout")
                    for corruption in corruptions:
                        bad = copy.deepcopy(record)
                        if corruption == "consumption":
                            bad["native_consumption_verified"] = False
                        elif corruption == "csv_guard":
                            bad["result_files"].pop("csv")
                        elif corruption == "saved_input":
                            values = bad["input_handoff"]["saved"]["subarrays"][0]["values"]
                            values["NModSerie"] = str(int(values["NModSerie"]) + 1)
                        elif corruption == "inverter_state":
                            state = copy.deepcopy(current["saved"]["subarrays"][0]["inverter_state"])
                            state["NInverter"] += 1
                            bad["input_handoff"]["saved"]["subarrays"][0]["inverter_state"] = state
                        elif corruption == "pdf_required":
                            bad["request"].pop("report_pdf", None)
                        elif corruption == "unrequested_pdf":
                            bad["artifacts"]["pdf"] = str(root / "Results" / "not-bound.pdf")
                        elif corruption == "nonboolean_pdf":
                            bad["request"]["report_pdf"] = 0
                        elif corruption == "stored_geometry":
                            bad["native_import"]["stored_geometry_verified"] = False
                        elif corruption == "geometry_evidence":
                            bad["native_import"]["verification"]["geometry"]["pv_geometry_verified"] = False
                        elif corruption == "stored_layout":
                            bad["native_import"]["stored_module_layout_verified"] = False
                        elif corruption == "import_route":
                            bad["request"]["scene_import_method"] = "gui"
                        manifest_path.write_text(json.dumps(bad, ensure_ascii=False, allow_nan=False), encoding="utf-8")
                        changed = dict(originals)
                        changed[manifest_path.relative_to(root)] = manifest_path.read_bytes()
                        rejected = await call(delivery, arguments)
                        self.assertEqual(rejected["status"], "blocked", (corruption, rejected))
                        unchanged(changed)
                    manifest_path.write_bytes(originals[manifest_path.relative_to(root)])
                    csv_path = root / "Results" / Path(record["artifacts"]["csv"]).name
                    csv_path.write_bytes(originals[csv_path.relative_to(root)] + b"\n")
                    changed = dict(originals)
                    changed[csv_path.relative_to(root)] = csv_path.read_bytes()
                    rejected = await call(delivery, arguments)
                    self.assertEqual(rejected["status"], "blocked", rejected)
                    unchanged(changed)
                    csv_path.write_bytes(originals[csv_path.relative_to(root)])

                    scenario = {"project": record["working_project"], "variant": record["working_variant"],
                                "declared_changes": [], "year": record["year"]}
                    comparison = await call("pvsyst_compare_engineering_scenarios", {
                        "objective": "Check recorded house binding against an unbound observed copy",
                        "scenarios": [dict(scenario, label="recorded", csv_name=csv_path.name, run_manifest_name=manifest.name),
                                      dict(scenario, label="observed copy", csv_name=comparison_csv.name)],
                        "output_name": "house-comparison.json", "dry_run": True})
                    self.assertEqual(comparison["status"], "needs_review", comparison)
                    self.assertTrue(comparison["scenarios"][0]["historical_input_binding_verified"])
                    self.assertFalse(comparison["scenarios"][1]["historical_input_binding_verified"])
                    if native_method:
                        bound = comparison["scenarios"][0]["bound_run"]["house"]
                        self.assertEqual(bound["import_method"], "native")
                        self.assertTrue(bound["stored_geometry_verified"])
                        self.assertEqual(bound["native_geometry_verified"], record["native_import"]["native_geometry_verified"])
                    unchanged()

                    preview = await call(delivery, arguments)
                    self.assertEqual(preview["status"], "ready", preview)
                    self.assertEqual(preview["task_status"], record["status"])
                    self.assertTrue(preview["task_native_consumption_verified"])
                    self.assertFalse(preview["native_consumption_verified"])
                    expected = {"summary.md", "inputs.md", "Results/" + manifest.name,
                                "Projects/" + record["working_project"], "Projects/" + saved_variant.name}
                    expected.update("Results/" + Path(record["artifacts"][kind]).name for kind in output_kinds)
                    expected.update(record["input_assets"][kind]["folder"] + "/" + record["input_assets"][kind]["name"]
                                    for kind in ("scene", "sfi"))
                    self.assertTrue(expected.issubset(preview["included_files"]))
                    evidence = {"regions": record["geometry"]["tables"],
                        "native_geometry_verified": record["native_import"]["native_geometry_verified"],
                        "house_mapping": record["native_import"]["verification"].get("house_mapping"),
                        "pv_mapping": record["native_import"]["verification"].get("pv_mapping"),
                        "native_reload_verified": record["native_import"].get("native_reload_verified"),
                        "native_reload": record["native_import"].get("native_reload")}
                    if native_method:
                        evidence.update(import_method="native", stored_geometry_verified=True,
                                        stored_module_layout_verified=record["native_import"].get("stored_module_layout_verified"),
                                        native_consumption_verified=True)
                    blocks = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", preview["inputs_markdown"], re.S)]
                    self.assertIn(evidence, blocks)
                    for table in spec["layout"]["tables"]:
                        self.assertIn(table["name"], preview["summary_markdown"])
                    unchanged()

                    exported = await call(delivery, dict(arguments, dry_run=False))
                    self.assertEqual(exported["status"], "exported", exported)
                    self.assertEqual(exported["task_status"], record["status"])
                    output = root / "Results" / arguments["output_name"]
                    self.assertEqual(Path(exported["artifact"]["zip"]), output)
                    with zipfile.ZipFile(output) as package:
                        self.assertTrue(expected.issubset(package.namelist()))
                        if output_kinds == ("csv",):
                            self.assertFalse(any(name.lower().endswith(".pdf") for name in package.namelist()))
                        for name in expected - {"summary.md", "inputs.md"}:
                            self.assertEqual(package.read(name), (root / name).read_bytes(), name)
                        self.assertEqual(package.read("summary.md").decode("utf-8"), exported["summary_markdown"])
                        self.assertEqual(package.read("inputs.md").decode("utf-8"), exported["inputs_markdown"])
                    unchanged(expected_members=members | {output})
            self.assertTrue(all(path.read_bytes() == value for path, value in source_bytes.items()))
        except BaseException:
            print("Two-roof delivery failure artifacts retained: " + str(directory), file=sys.stderr)
            raise
        else:
            shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()

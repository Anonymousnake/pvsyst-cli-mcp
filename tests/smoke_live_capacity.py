"""Opt-in public MCP/native quantity and orientation regression.

PVSYST_WORKSPACE remains read-only. Results live under PVSYST_EDITOR_LAB.
"""
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import shutil
import sys
import uuid

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvsyst_auto.native.cli import parse_result_csv


async def main(mode, prune_plane=2):
    independent_pruning = mode == "mppt_pruning"
    if independent_pruning:
        mode = "pruning"
    control_tilts = [35, 60] if prune_plane == 1 else [15, 35]
    source = Path(os.environ["PVSYST_WORKSPACE"]).resolve(strict=True)
    lab = Path(os.environ["PVSYST_EDITOR_LAB"]).resolve()
    if lab == source or source in lab.parents:
        raise ValueError("Lab must be outside source workspace")
    run = lab / ("mcp-capacity-" + uuid.uuid4().hex[:12])
    root = run / "PVsyst8.1_Data"
    shutil.copytree(source, root, ignore=lambda directory, names: (
        [name for name in names if name in ("Results", "UserHourly", "UserBatch", "Meteo")]
        if Path(directory) == source else []))
    for name in ("Results", "UserHourly", "UserBatch", "Meteo"):
        (root / name).mkdir(exist_ok=True)
    shutil.copy2(source / "Sites" / "Wanaluwawa.SIT", root / "Sites" / "Wanaluwawa.SIT")
    shutil.copy2(source / "Meteo" / "Wanaluwawa_Nasa_SYN.MET", root / "Meteo" / "Wanaluwawa_Nasa_SYN.MET")
    updates = {"NStringCh": 80} if mode == "strings" else {"NInverter": 12}
    evidence = {"workspace": str(root), "mode": "mppt_pruning" if independent_pruning else mode, "cases": []}
    if mode == "pruning":
        evidence["prune_plane"] = prune_plane
    params = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__).resolve().parents[1] / "pvsyst_mcp_server.py")],
        env={**os.environ, "PVSYST_WORKSPACE": str(root)})
    print(f"Capacity A/B evidence: {run}", flush=True)
    try:
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()

                async def call(tool, **args):
                    response = await session.call_tool(tool, args, read_timeout_seconds=1800)
                    if response.is_error:
                        raise RuntimeError(f"{tool}: {response.content}")
                    return json.loads(response.content[0].text)

                target = {"project": "MCP_MPPT.PRJ" if independent_pruning or mode in ("mppt_branches", "mppt_orientations", "mppt_reassociation") else "_DEMO_Commercial.PRJ", "variant": "VC0"}
                if independent_pruning or mode in ("mppt_branches", "mppt_orientations", "mppt_reassociation"):
                    demos = Path(os.environ["PVSYST_CLI"]).resolve(strict=True).parent / "DataRO" / "PVsyst8.1_Data" / "Projects"
                    evidence["project_creation"] = await call("pvsyst_create_project", project=target["project"],
                        project_content=(demos / "_DEMO_Residential.PRJ").read_bytes().decode("utf-8"),
                        variants={"VC0": (demos / "_DEMO_Residential.VC0").read_bytes().decode("utf-8")})
                if independent_pruning or mode in ("mppt_orientations", "mppt_reassociation"):
                    from pypdf import PdfReader
                    source = await call("pvsyst_get_variant_parameters", **target)
                    evidence["independent_mode_setup"] = await call("pvsyst_update_variant_parameters", **target,
                        expected_sha256=source["sha256"], independent_mppt_enabled=True)
                    evidence["tilt_setup"] = await call("pvsyst_update_variant_parameters", **target,
                        expected_sha256=evidence["independent_mode_setup"]["sha256"], orientation_updates={"FieldTilt": 15})
                await call("pvsyst_build_sfi", filename="capacity.sfi", variables="viEArray,viE_Grid,viIL_Pmax")
                report = await call("pvsyst_validate_variant_structure", **target)
                if report["valid"] is not True:
                    raise RuntimeError(report)
                variant_path = root / "Projects" / f"{target['project'][:-4]}.{target['variant']}"
                original = variant_path.read_bytes()
                if mode in ("orientations", "mppt_orientations", "reassociation", "mppt_reassociation", "pruning"):
                    copied = await call("pvsyst_clone_subarray", **target,
                        expected_sha256=report["sha256"], source_subarray_id=1, clone_orientation=True)
                    evidence["clone"] = copied
                    report = await call("pvsyst_validate_variant_structure", **target)
                    if report["valid"] is not True or report["orientation_ids"] != [1, 2]:
                        raise RuntimeError("Independent orientation was not linked correctly")
                    if mode in ("reassociation", "mppt_reassociation", "pruning"):
                        evidence["setup_edit"] = await call("pvsyst_update_variant_parameters", **target,
                            expected_sha256=report["sha256"], orientation_id=2,
                            orientation_updates={"FieldTilt": control_tilts[1] if mode == "pruning" else 60})
                        if mode == "pruning" and prune_plane == 1:
                            evidence["baseline_tilt_edit"] = await call("pvsyst_update_variant_parameters", **target,
                                expected_sha256=evidence["setup_edit"]["sha256"], orientation_id=1,
                                orientation_updates={"FieldTilt": control_tilts[0]})
                        report = await call("pvsyst_validate_variant_structure", **target)
                        if mode in ("reassociation", "mppt_reassociation"):
                            third = await call("pvsyst_clone_subarray", **target,
                                expected_sha256=report["sha256"], source_subarray_id=1)
                            evidence["third_subarray"] = third
                            report = await call("pvsyst_validate_variant_structure", **target)
                cases = ("before", "after", "control") if mode in ("orientations", "mppt_orientations", "reassociation", "mppt_reassociation") else ("before", "after")
                hourly = {}
                for name in cases:
                    hourly[name] = {}
                    if name != "before":
                        if mode in ("orientations", "mppt_orientations"):
                            changed = await call("pvsyst_update_variant_parameters", **target,
                                expected_sha256=report["sha256"], orientation_id=2 if name == "after" else 1,
                                orientation_updates={"FieldTilt": 60})
                        elif mode in ("reassociation", "mppt_reassociation"):
                            if name == "control":
                                evidence["assignment_restore"] = await call("pvsyst_restore_variant", **target,
                                    backup_name=evidence["edit"]["backup_name"], expected_sha256=report["sha256"], confirm=True)
                                report = await call("pvsyst_validate_variant_structure", **target)
                                changed = await call("pvsyst_update_variant_parameters", **target,
                                    expected_sha256=report["sha256"], orientation_id=1, orientation_updates={"FieldTilt": 60})
                            else:
                                changed = await call("pvsyst_update_variant_parameters", **target,
                                    expected_sha256=report["sha256"], subarray_id=third["subarray_id"], subarray_updates={"NoOrientation": 2})
                        elif mode == "pruning":
                            preparation = await call("pvsyst_update_variant_parameters", **target,
                                expected_sha256=report["sha256"], orientation_id=prune_plane,
                                orientation_updates={"FieldTilt": 15 if prune_plane == 1 else 60})
                            evidence["prune_preparation_edit"] = preparation
                            if prune_plane == 1:
                                preparation = await call("pvsyst_update_variant_parameters", **target,
                                    expected_sha256=preparation["sha256"], orientation_id=2, orientation_updates={"FieldTilt": 35})
                            third = await call("pvsyst_clone_subarray", **target,
                                expected_sha256=preparation["sha256"], source_subarray_id=1, clone_orientation=True)
                            evidence["third_subarray"] = third
                            tilted = await call("pvsyst_update_variant_parameters", **target,
                                expected_sha256=third["sha256"], orientation_id=3, orientation_updates={"FieldTilt": control_tilts[1]})
                            evidence["prune_preparation"] = await call("pvsyst_get_variant_parameters", **target)
                            changed = await call("pvsyst_remove_subarray", **target,
                                subarray_id=1 if prune_plane == 1 else copied["subarray_id"],
                                expected_sha256=tilted["sha256"], remove_orientation=True)
                            if changed["orientation_id_map"] != ({"2": 1, "3": 2} if prune_plane == 1 else {"3": 2}):
                                raise RuntimeError("Non-final plane deletion did not remap the remaining plane")
                        elif mode in ("subarrays", "mppt_branches"):
                            changed = await call("pvsyst_clone_subarray", **target,
                                expected_sha256=report["sha256"], source_subarray_id=1)
                        elif mode in ("project_variants", "creation"):
                            destination = "MCP_Create.PRJ" if mode == "creation" else "MCP_Import.PRJ"
                            if mode == "creation":
                                project_content = (root / "Projects" / target["project"]).read_bytes().decode("utf-8")
                                source_state = await call("pvsyst_inspect_project", project=target["project"])
                                evidence["source_archive"] = await call("pvsyst_archive_project", project=target["project"],
                                    expected_files=source_state["files"], confirm=True)
                                if variant_path.exists():
                                    raise RuntimeError("Source project was not removed before content creation")
                                changed = await call("pvsyst_create_project", project=destination,
                                    project_content=project_content, variants={"VC0": original.decode("utf-8"), "VC9": original.decode("utf-8")})
                                if (root / "Projects" / destination).read_bytes().decode("utf-8") != project_content:
                                    raise RuntimeError("Project content bytes were changed")
                                evidence["source_absent_at_creation"] = True
                            else:
                                evidence["project_copy"] = await call("pvsyst_clone_project",
                                    source_project="_Demo_PVsystCLI.PRJ", new_project=destination)
                                project_path = root / "Projects" / destination
                                if project_path.read_bytes() != (root / "Projects" / "_Demo_PVsystCLI.PRJ").read_bytes():
                                    raise RuntimeError("Project template bytes were changed")
                                changed = await call("pvsyst_clone_variant", project=destination,
                                    source_project=target["project"], source_variant=target["variant"], new_variant="VC9")
                            target = {"project": destination, "variant": "VC9"}
                            destination_variant = root / "Projects" / f"{destination[:-4]}.VC9"
                            if destination_variant.read_bytes() != original:
                                raise RuntimeError("Import changed the source design")
                            inspected = await call("pvsyst_inspect_project", project=destination)
                            differences = [warning for warning in inspected["warnings"] if "differs from" in warning]
                            if mode == "project_variants" and not any("VC9: meteo differs" in warning for warning in differences):
                                raise RuntimeError("Cross-project source difference was not reported")
                            if mode == "creation" and differences:
                                raise RuntimeError("Created source references differ from supplied project")
                            evidence["project_inspection"] = inspected
                        else:
                            changed = await call("pvsyst_update_variant_parameters", **target,
                                expected_sha256=report["sha256"], subarray_updates=updates)
                        evidence["control_edit" if name == "control" else "edit"] = changed
                        report = await call("pvsyst_validate_variant_structure", **target)
                        if report["valid"] is not True:
                            raise RuntimeError(report)
                    counts = report["grid_circuit"]["totals"]
                    expected = (3, 6) if mode == "mppt_reassociation" else (2, 4) if independent_pruning or mode == "mppt_orientations" else ((1, 2) if name == "before" else (2, 4)) if mode == "mppt_branches" else (18, 120) if mode == "reassociation" else (12, 80) if mode in ("orientations", "pruning") else (
                        (6, 40) if name == "before" or mode in ("project_variants", "creation") else
                        (6 if mode == "strings" else 12, 80))
                    if (counts["inverters"], counts["strings"]) != expected:
                        raise RuntimeError(f"Wrong capacity: {counts}")
                    if mode == "mppt_branches" and (counts["explicit_mppt_inputs"] != 2 * expected[0] or any(
                            row["declared"]["NInvMPPT"] != "1" for row in report["grid_circuit"]["subarrays"])):
                        raise RuntimeError("Explicit MPPT multiplicities or saved declaration were changed")
                    result = await call("pvsyst_run_simulation", **target, sfi_name="capacity.sfi",
                        csv_name=f"capacity_{name}.csv", pdf_name=f"capacity_{name}.pdf" if independent_pruning or mode in ("mppt_orientations", "mppt_reassociation") else "",
                        report_pages="params" if independent_pruning or mode in ("mppt_orientations", "mppt_reassociation") else "", site_name="Wanaluwawa.SIT",
                        met_name="Wanaluwawa_Nasa_SYN.MET", start_date="1990.01.01", end_date="1990.01.02")
                    columns, rows = parse_result_csv(result["csv"])
                    if len(rows) != 48:
                        raise RuntimeError("Expected 48 hourly rows")
                    metrics = {}
                    for column in ("EArray", "E_Grid", "IL_Pmax"):
                        values = [row[columns.index(column)] for row in rows]
                        if (not all(math.isfinite(value) for value in values)
                                or column != "IL_Pmax" and sum(values) <= 0):
                            raise RuntimeError(f"Invalid {column} output")
                        metrics[column] = sum(values)
                        hourly[name][column] = values
                    case = {"case": name, "inspection": report, "csv": result["csv"],
                            "rows": len(rows), "sum_hourly_kw": metrics}
                    if independent_pruning or mode in ("mppt_orientations", "mppt_reassociation"):
                        case["pdf"] = result["pdf"]
                        case["report_text"] = "\n".join(page.extract_text() for page in PdfReader(result["pdf"]).pages)
                        members = 3 if mode == "mppt_reassociation" else 2
                        assert case["report_text"].count("2 * MPPT 50%") == members
                        assert "No power sharing" in case["report_text"]
                        assert report["grid_circuit"]["totals"] == {"inverters": members, "strings": 2 * members, "explicit_mppt_inputs": 2 * members}
                        assert all(row["declared"]["NInvMPPT"] == "2" and row["declared"]["Flags"] == report["grid_circuit"]["subarrays"][0]["declared"]["Flags"]
                                   and int(row["declared"]["Flags"][1:], 16) & 0x400 and row["declared"]["PowerSharingInverterId"] == "-1"
                                   for row in report["grid_circuit"]["subarrays"])
                    if mode in ("orientations", "mppt_orientations", "reassociation", "mppt_reassociation", "pruning"):
                        parameters = await call("pvsyst_get_variant_parameters", **target)
                        expected_tilts = control_tilts if mode == "pruning" else ([60, 60] if name == "control" else [15, 60]) if mode in ("reassociation", "mppt_reassociation") else (
                            [15, 15] if name == "before" else [15, 60] if name == "after" else [60, 60])
                        if [float(o["values"]["FieldTilt"]) for o in parameters["orientations"]] != expected_tilts:
                            raise RuntimeError("Orientation edit changed the wrong plane")
                        if mode in ("reassociation", "mppt_reassociation"):
                            expected_refs = [1, 2, 2] if name == "after" else [1, 2, 1]
                            if [int(a["values"]["NoOrientation"]) for a in parameters["subarrays"]] != expected_refs:
                                raise RuntimeError("Orientation assignment changed the wrong subarray")
                        if mode == "pruning":
                            if ([a["id"] for a in parameters["subarrays"]] != ([1, 2] if name == "before" else [2, 3] if prune_plane == 1 else [1, 3]) or
                                    [a["values"]["NoOrientation"] for a in parameters["subarrays"]] != ["1", "2"]):
                                raise RuntimeError("Non-final plane deletion changed the wrong subarray reference")
                        case["parameters"] = parameters
                    evidence["cases"].append(case)
                ratios = {key: evidence["cases"][1]["sum_hourly_kw"][key] /
                               evidence["cases"][0]["sum_hourly_kw"][key] for key in ("EArray", "E_Grid")}
                if mode in ("orientations", "mppt_orientations", "reassociation", "mppt_reassociation"):
                    deltas = {key: max(abs(a - b) for a, b in zip(hourly["after"][key], hourly["before"][key]))
                              for key in ("EArray", "E_Grid")}
                    if not all(value > 1 for value in deltas.values()):
                        raise RuntimeError("Expected a measurable hourly response to the new plane's tilt")
                    evidence["max_hourly_delta_kw"] = deltas
                    residuals = {key: abs(2 * evidence["cases"][1]["sum_hourly_kw"][key] -
                        evidence["cases"][0]["sum_hourly_kw"][key] - evidence["cases"][2]["sum_hourly_kw"][key]) /
                        evidence["cases"][0]["sum_hourly_kw"][key] for key in ("EArray", "E_Grid")}
                    if not all(value < 0.02 for value in residuals.values()):
                        raise RuntimeError(f"Mixed-plane output is not consistent with both controls: {residuals}")
                    evidence["mixed_plane_relative_residuals"] = residuals
                elif mode in ("project_variants", "creation"):
                    if hourly["before"] != hourly["after"]:
                        raise RuntimeError("Imported design differs from source hourly outputs")
                    archived = await call("pvsyst_archive_variant", **target,
                        expected_sha256=report["sha256"], confirm=True)
                    if destination_variant.exists():
                        raise RuntimeError("Imported variant was not archived")
                    evidence["archive"] = archived
                    evidence["restore"] = await call("pvsyst_restore_project_archive",
                        project=target["project"], archive_name=archived["archive_name"], confirm=True)
                    if destination_variant.read_bytes() != original:
                        raise RuntimeError("Imported variant restore changed input bytes")
                    if mode == "creation":
                        archived_project = await call("pvsyst_archive_project", project=target["project"],
                            expected_files=changed["files"], confirm=True)
                        evidence["created_project_archive"] = archived_project
                        evidence["created_project_restore"] = await call("pvsyst_restore_project_archive", project=target["project"],
                            archive_name=archived_project["archive_name"], confirm=True)
                        if (await call("pvsyst_inspect_project", project=target["project"]))["files"] != changed["files"]:
                            raise RuntimeError("Created project restore changed input bytes")
                        evidence["source_restore"] = await call("pvsyst_restore_project_archive", project="_DEMO_Commercial.PRJ",
                            archive_name=evidence["source_archive"]["archive_name"], confirm=True)
                    if variant_path.read_bytes() != original:
                        raise RuntimeError("Source variant was changed")
                    evidence["hourly_outputs_identical"] = True
                    evidence["source_unchanged_and_import_restored"] = True
                elif mode == "pruning":
                    if hourly["before"] != hourly["after"]:
                        raise RuntimeError("Renumbered design differs from the matching native control")
                    evidence["hourly_outputs_identical"] = True
                elif mode != "strings":
                    if not all(1.8 < ratio < 2.2 for ratio in ratios.values()):
                        raise RuntimeError(f"Capacity did not scale output as expected: {ratios}")
                elif not all(1 < ratio < 2 for ratio in ratios.values()) or (
                        evidence["cases"][1]["sum_hourly_kw"]["IL_Pmax"] <=
                        evidence["cases"][0]["sum_hourly_kw"]["IL_Pmax"]):
                    raise RuntimeError(f"Expected increased output and clipping at fixed inverter count: {ratios}")
                evidence["ratios"] = ratios
                if mode in ("subarrays", "orientations", "mppt_orientations", "reassociation", "mppt_reassociation", "pruning", "mppt_branches"):
                    if mode == "pruning":
                        evidence["prune_restore"] = await call("pvsyst_restore_variant", **target,
                            backup_name=evidence["prune_preparation_edit"]["backup_name"],
                            expected_sha256=report["sha256"], confirm=True)
                        report = await call("pvsyst_validate_variant_structure", **target)
                        selected = copied
                    elif mode in ("orientations", "mppt_orientations", "reassociation", "mppt_reassociation"):
                        restored = await call("pvsyst_restore_variant", **target,
                            backup_name=evidence["control_edit"]["backup_name"],
                            expected_sha256=report["sha256"], confirm=True)
                        evidence["control_restore"] = restored
                        report = await call("pvsyst_validate_variant_structure", **target)
                        if mode in ("reassociation", "mppt_reassociation"):
                            evidence["third_removal"] = await call("pvsyst_remove_subarray", **target,
                                subarray_id=third["subarray_id"], expected_sha256=report["sha256"])
                            report = await call("pvsyst_validate_variant_structure", **target)
                        selected = copied
                    else:
                        selected = changed
                    if selected["inverter_group_count"] != (1 if independent_pruning or mode in ("mppt_branches", "mppt_orientations", "mppt_reassociation") else 2) or len(report["subarray_ids"]) != 2:
                        raise RuntimeError("Expected complete groups in the cloned subarray")
                    removed = await call("pvsyst_remove_subarray", **target,
                        subarray_id=selected["subarray_id"], expected_sha256=report["sha256"],
                        remove_orientation=mode in ("orientations", "mppt_orientations", "reassociation", "mppt_reassociation", "pruning"))
                    if mode == "pruning" and prune_plane == 1:
                        evidence["baseline_restore"] = await call("pvsyst_restore_variant", **target,
                            backup_name=copied["backup_name"], expected_sha256=removed["sha256"], confirm=True)
                    if variant_path.read_bytes() != original:
                        raise RuntimeError("Removal did not recover the exact baseline variant")
                    evidence["removal"] = removed
                    evidence["baseline_bytes_restored"] = True
                print(f"MCP capacity A/B ({mode}): energy ratios={ratios}: OK", flush=True)
    finally:
        (run / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("inverters", "strings", "subarrays", "orientations", "reassociation", "project_variants", "creation", "pruning", "mppt_branches", "mppt_orientations", "mppt_reassociation", "mppt_pruning"), default="inverters")
    parser.add_argument("--prune-plane", type=int, choices=(1, 2), default=2, help="Plane removed in pruning mode")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.mode, arguments.prune_plane))

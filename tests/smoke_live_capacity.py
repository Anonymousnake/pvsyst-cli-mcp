"""Opt-in MCP design checks: two CLI runs; plane controls three, MPPT mode probe four, quantity probe nine, sharing probe seven, allocation probe three, flat probe one.

PVSYST_WORKSPACE remains read-only. Evidence lives under PVSYST_EDITOR_LAB.
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
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.components.editor import objects, replace_value
from pvsyst_auto.project.circuit import circuit_nodes


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

                if mode in ("mppt_mode_probe", "mppt_flat_probe", "mppt_quantity_probe", "mppt_sharing_probe", "mppt_allocation_probe"):
                    from pypdf import PdfReader
                    demos = Path(os.environ["PVSYST_CLI"]).resolve(strict=True).parent / "DataRO" / "PVsyst8.1_Data" / "Projects"
                    seed = (demos / "_DEMO_Residential.VC0").read_bytes()
                    _, lines = VariantStore._text(seed)
                    inverter = circuit_nodes(lines)[0].children[0]
                    mppt = inverter.children[0]
                    string = mppt.children[0]
                    assert (inverter.kind, mppt.kind, string.kind) == ("InverterNode", "MPPTNode", "StringNode")
                    assert mppt.one("NElements") == "2" and string.one("NElements") is None
                    lines[mppt.field_lines["NElements"][0]] = replace_value(lines[mppt.field_lines["NElements"][0]], "1")
                    # Consolidate two strings on one serialized input; keep the PV array unchanged.
                    prefix = lines[string.field_lines["SubArrayId"][0]].split("SubArrayId", 1)[0]
                    lines.insert(string.line, prefix + "NElements=2" + ("\r\n" if lines[string.line - 1].endswith("\r\n") else "\n"))
                    bounds = VariantStore._sections(lines)[0][1]
                    array = next(block for block in objects("".join(lines[bounds[0]:bounds[1] + 1])) if not block.path)
                    assert array.one("Flags")[1] == "$001E0002" and array.one("PNomAC_InMPPT")[1] == "7.50"
                    controls = (("whole", "$001E0002", "7.50"), ("nominal_only", "$001E0002", "3.75"),
                                ("flag_only", "$001E0402", "7.50"), ("independent", "$001E0402", "3.75"))
                    if mode == "mppt_flat_probe":
                        controls = (("flat_independent", "$001E0402", "3.75"),)
                    quantities = {"partial": (1, 1, 2), "one_group_two_inputs": (1, 2, 2),
                                  "two_groups_one_input": (2, 1, 2), "three_inputs": (1, 3, 2), "one_input_four_strings": (1, 1, 4), "shared_clone": (1, 1, 2), "two_inverters": (2, 2, 2), "longer_series": (1, 1, 2)}
                    if mode == "mppt_allocation_probe":
                        quantities = {"grouped_equal": (1, 2, 2), "split_equal": (1, 2, 2), "split_unequal": (1, 2, 2)}
                    if mode in ("mppt_quantity_probe", "mppt_allocation_probe"):
                        controls = tuple((name, "$001E0402", "3.75") for name in quantities)
                    if mode == "mppt_sharing_probe":
                        controls = (("independent", "$001E0402", "5.00"), ("id_only", "$001E0402", "5.00"),
                                    ("shared", "$001E0502", "5.00"), ("swapped", "$001E0502", "2.50"),
                                    ("shared_more_strings", "$001E0502", "5.00"))
                    variants = {}
                    for number, (name, flags, nominal) in enumerate(controls):
                        candidate = lines.copy()
                        current_arrays, _ = VariantStore._sections(candidate)
                        bounds = current_arrays[1]
                        array = next(block for block in objects("".join(candidate[bounds[0]:bounds[1] + 1])) if not block.path)
                        for field, value in (("Flags", flags), ("PNomAC_InMPPT", nominal)):
                            index = bounds[0] + array.one(field)[0]
                            candidate[index] = replace_value(candidate[index], value)
                        if mode in ("mppt_quantity_probe", "mppt_allocation_probe"):
                            groups, inputs, parallel = quantities[name]
                            for field, value in (("NInverter", groups), ("NInvMPPT", groups * inputs if name == "two_inverters" else inputs),
                                                 ("NStringCh", groups * inputs * parallel), ("NStrOrient1", groups * inputs * parallel)):
                                index = bounds[0] + array.one(field)[0]
                                candidate[index] = replace_value(candidate[index], str(value))
                            if name == "longer_series":
                                index = bounds[0] + array.one("NModSerie")[0]
                                candidate[index] = replace_value(candidate[index], "18")
                            branch = circuit_nodes(candidate)[0].children[0]
                            port = branch.children[0]
                            leaf = port.children[0]
                            for node, value in ((port, inputs), (leaf, parallel)):
                                index = node.field_lines["NElements"][0]
                                candidate[index] = replace_value(candidate[index], str(value))
                            if groups != 1 or mode == "mppt_quantity_probe":
                                prefix = candidate[branch.field_lines["SubArrayId"][0]].split("SubArrayId", 1)[0]
                                candidate.insert(branch.line, prefix + f"NElements={groups}\r\n")
                        if mode == "mppt_allocation_probe" and name != "grouped_equal":
                            first = candidate[port.line - 1:port.end_line].copy()
                            first[port.field_lines["NElements"][0] - (port.line - 1)] = replace_value(
                                first[port.field_lines["NElements"][0] - (port.line - 1)], "1")
                            leaf_index = leaf.field_lines["NElements"][0] - (port.line - 1)
                            first[leaf_index] = replace_value(first[leaf_index], "3" if name == "split_unequal" else "2")
                            second = first.copy()
                            second[leaf_index] = replace_value(second[leaf_index], "1" if name == "split_unequal" else "2")
                            candidate[port.line - 1:port.end_line] = first + second
                        if mode == "mppt_sharing_probe" or mode == "mppt_quantity_probe" and name == "shared_clone":
                            grouped_clone = name == "shared_clone"
                            current_arrays, _ = VariantStore._sections(candidate)
                            bounds = current_arrays[1]
                            array = next(block for block in objects("".join(candidate[bounds[0]:bounds[1] + 1])) if not block.path)
                            group_id = "-1" if name == "independent" or grouped_clone else "0"
                            index = bounds[0] + array.one("PowerSharingInverterId")[0]
                            candidate[index] = replace_value(candidate[index], group_id)
                            if mode == "mppt_sharing_probe" and name == "independent":
                                variants["VC9"] = ("\ufeff" if seed.startswith(b"\xef\xbb\xbf") else "") + "".join(candidate)
                            if name == "shared_more_strings":
                                for field in ("NStringCh", "NStrOrient1"):
                                    index = bounds[0] + array.one(field)[0]
                                    candidate[index] = replace_value(candidate[index], "4")
                                branch = circuit_nodes(candidate)[0].children[0]
                                leaf = branch.children[0].children[0]
                                index = leaf.field_lines["NElements"][0]
                                candidate[index] = replace_value(candidate[index], "4")
                            second = candidate[bounds[0]:bounds[1] + 1].copy()
                            for field, value in (("SubArrayId", "2"), ("Comment", "Sub-array #2"),
                                                 ("NStringCh", "2" if grouped_clone else "1"), ("NStrOrient1", "2" if grouped_clone else "1"),
                                                 ("PNomAC_InMPPT", nominal if grouped_clone else f"{7.5 - float(nominal):.2f}")):
                                index = array.one(field)[0]
                                second[index] = replace_value(second[index], value)
                            candidate[bounds[1] + 1:bounds[1] + 1] = second
                            branch = circuit_nodes(candidate)[0].children[0]
                            port = branch.children[0]
                            duplicate = candidate[port.line - 1:port.end_line].copy()
                            for node in (port, port.children[0]):
                                for field, value in (("SubArrayId", "2"), ("SubArrayName", "Sub-array #2")):
                                    index = node.field_lines[field][0] - (port.line - 1)
                                    duplicate[index] = replace_value(duplicate[index], value)
                            index = port.children[0].field_lines["NElements"][0] - (port.line - 1)
                            duplicate[index] = replace_value(duplicate[index], "2" if grouped_clone else "1")
                            candidate[port.end_line:port.end_line] = duplicate
                            index = branch.field_lines["SubArrayName"][0]
                            candidate[index] = replace_value(candidate[index], "PV Array; Sub-array #2")
                            if grouped_clone or mode == "mppt_sharing_probe":
                                index = branch.field_lines["SubArrayId"][0]
                                candidate[index] = replace_value(candidate[index], "0")
                            else:
                                del candidate[branch.field_lines["SubArrayId"][0]]
                        if mode == "mppt_flat_probe":
                            node = circuit_nodes(candidate)[0].children[0].children[0]
                            leaf = node.children[0]
                            candidate[node.line - 1:node.end_line] = candidate[leaf.line - 1:leaf.end_line]
                        variant_id = ({"one_input_four_strings": "VC6", "shared_clone": "VC8", "two_inverters": "VC9", "longer_series": "VCB"}.get(name, f"VC{number}")
                                      if mode == "mppt_quantity_probe" else "VC6" if mode == "mppt_sharing_probe" and number == 4 else f"VC{number}")
                        variants[variant_id] = ("\ufeff" if seed.startswith(b"\xef\xbb\xbf") else "") + "".join(candidate)
                    target = {"project": "MCP_MPPT_PROBE.PRJ"}
                    if mode == "mppt_quantity_probe":
                        clone_control = variants.pop("VC8").encode("utf-8")
                        (run / "shared-clone-control.VC8").write_bytes(clone_control)
                    evidence["project_creation"] = await call("pvsyst_create_project", **target,
                        project_content=(demos / "_DEMO_Residential.PRJ").read_bytes().decode("utf-8"), variants=variants)
                    if mode == "mppt_quantity_probe":
                        copied = await call("pvsyst_clone_variant", **target, source_variant="VC0", new_variant="VC4")
                        original = (root / "Projects" / "MCP_MPPT_PROBE.VC4").read_bytes()
                        updates = {**target, "variant": "VC4", "expected_sha256": copied["sha256"], "subarray_updates": {"NInvMPPT": 2}}
                        preview = await call("pvsyst_update_variant_parameters", **updates, dry_run=True)
                        assert (root / "Projects" / "MCP_MPPT_PROBE.VC4").read_bytes() == original
                        edited = await call("pvsyst_update_variant_parameters", **updates)
                        assert edited["sha256"] == preview["sha256"]
                        assert (root / "Projects" / "MCP_MPPT_PROBE.VC4").read_bytes() == (root / "Projects" / "MCP_MPPT_PROBE.VC1").read_bytes()
                        evidence["mppt_quantity_edit"] = {"preview": preview, "apply": edited, "matches_authored_control_bytes": True}
                        string_copy = await call("pvsyst_clone_variant", **target, source_variant="VC0", new_variant="VC5")
                        string_original = (root / "Projects" / "MCP_MPPT_PROBE.VC5").read_bytes()
                        string_update = {**target, "variant": "VC5", "expected_sha256": string_copy["sha256"], "subarray_updates": {"NStringCh": 4}}
                        string_preview = await call("pvsyst_update_variant_parameters", **string_update, dry_run=True)
                        assert (root / "Projects" / "MCP_MPPT_PROBE.VC5").read_bytes() == string_original
                        string_edit = await call("pvsyst_update_variant_parameters", **string_update)
                        assert string_edit["sha256"] == string_preview["sha256"]
                        assert (root / "Projects" / "MCP_MPPT_PROBE.VC5").read_bytes() == (root / "Projects" / "MCP_MPPT_PROBE.VC6").read_bytes()
                        evidence["independent_string_edit"] = {"preview": string_preview, "apply": string_edit, "matches_authored_control_bytes": True}
                        inverter_copy = await call("pvsyst_clone_variant", **target, source_variant="VC1", new_variant="VC8")
                        inverter_path = root / "Projects" / "MCP_MPPT_PROBE.VC8"
                        inverter_original = inverter_path.read_bytes()
                        inverter_update = {**target, "variant": "VC8", "expected_sha256": inverter_copy["sha256"], "subarray_updates": {"NInverter": 2}}
                        inverter_preview = await call("pvsyst_update_variant_parameters", **inverter_update, dry_run=True)
                        assert inverter_path.read_bytes() == inverter_original
                        inverter_edit = await call("pvsyst_update_variant_parameters", **inverter_update)
                        assert inverter_edit["sha256"] == inverter_preview["sha256"]
                        assert inverter_path.read_bytes() == (root / "Projects" / "MCP_MPPT_PROBE.VC9").read_bytes()
                        evidence["independent_inverter_edit"] = {"preview": inverter_preview, "apply": inverter_edit, "matches_authored_control_bytes": True}
                        series_copy = await call("pvsyst_clone_variant", **target, source_variant="VC0", new_variant="VCA")
                        series_path = root / "Projects" / "MCP_MPPT_PROBE.VCA"
                        series_original = series_path.read_bytes()
                        series_update = {**target, "variant": "VCA", "expected_sha256": series_copy["sha256"], "subarray_updates": {"NModSerie": 18}}
                        series_preview = await call("pvsyst_update_variant_parameters", **series_update, dry_run=True)
                        assert series_path.read_bytes() == series_original
                        series_edit = await call("pvsyst_update_variant_parameters", **series_update)
                        assert series_edit["sha256"] == series_preview["sha256"]
                        assert series_path.read_bytes() == (root / "Projects" / "MCP_MPPT_PROBE.VCB").read_bytes()
                        evidence["series_module_edit"] = {"preview": series_preview, "apply": series_edit, "matches_authored_control_bytes": True}
                        parent_copy = await call("pvsyst_clone_variant", **target, source_variant="VC0", new_variant="VC7")
                        parent_path = root / "Projects" / "MCP_MPPT_PROBE.VC7"
                        parent_original = parent_path.read_bytes()
                        parent_edit = await call("pvsyst_clone_subarray", **target, variant="VC7", source_subarray_id=1,
                            expected_sha256=parent_copy["sha256"], share_inverter=True)
                        assert parent_edit["subarray_id"] == 2 and parent_path.read_bytes() == clone_control
                        evidence["shared_parent_clone"] = {"apply": parent_edit, "matches_authored_control_bytes": True, "control": str(run / "shared-clone-control.VC8")}
                    if mode in ("mppt_sharing_probe", "mppt_mode_probe"):
                        switch_option = "power_sharing_enabled" if mode == "mppt_sharing_probe" else "independent_mppt_enabled"
                        switch_key = "power_sharing_edits" if mode == "mppt_sharing_probe" else "mppt_mode_edits"
                        sharing_edits = []
                        for source, variant, enabled, control in (("VC0", "VC4", True, "VC2"), ("VC2", "VC5", False, "VC0")):
                            copied = await call("pvsyst_clone_variant", **target, source_variant=source, new_variant=variant)
                            path = root / "Projects" / f"MCP_MPPT_PROBE.{variant}"
                            original_bytes = path.read_bytes()
                            update = {**target, "variant": variant, "expected_sha256": copied["sha256"], switch_option: enabled}
                            preview = await call("pvsyst_update_variant_parameters", **update, dry_run=True)
                            assert path.read_bytes() == original_bytes
                            applied = await call("pvsyst_update_variant_parameters", **update)
                            assert applied["sha256"] == preview["sha256"]
                            assert path.read_bytes() == (root / "Projects" / f"MCP_MPPT_PROBE.{control}").read_bytes()
                            sharing_edits.append((variant, original_bytes, applied))
                        evidence[switch_key] = [{"variant": variant, "apply": applied, "matches_authored_control_bytes": True}
                                                           for variant, _, applied in sharing_edits]
                    if mode == "mppt_sharing_probe":
                        member_copy = await call("pvsyst_clone_variant", **target, source_variant="VC2", new_variant="VC7")
                        member_path = root / "Projects" / "MCP_MPPT_PROBE.VC7"
                        member_original = member_path.read_bytes()
                        member_update = {**target, "variant": "VC7", "expected_sha256": member_copy["sha256"], "subarray_updates": {"NStringCh": 4}}
                        member_preview = await call("pvsyst_update_variant_parameters", **member_update, dry_run=True)
                        assert member_path.read_bytes() == member_original
                        member_edit = await call("pvsyst_update_variant_parameters", **member_update)
                        assert member_edit["sha256"] == member_preview["sha256"]
                        assert member_path.read_bytes() == (root / "Projects" / "MCP_MPPT_PROBE.VC6").read_bytes()
                        evidence["shared_member_string_edit"] = {"preview": member_preview, "apply": member_edit, "matches_authored_control_bytes": True}
                    await call("pvsyst_build_sfi", filename="capacity.sfi", variables="viEArray,viE_Grid,viIL_Pmax")
                    for number, (name, flags, nominal) in enumerate(controls):
                        variant = "VC4" if mode == "mppt_quantity_probe" and number == 1 else f"VC{number}"
                        if mode == "mppt_quantity_probe" and number == 4:
                            variant = "VC5"
                        if mode == "mppt_sharing_probe" and number == 4:
                            variant = "VC7"
                        if mode == "mppt_quantity_probe" and number == 5:
                            variant = "VC7"
                        if mode == "mppt_quantity_probe" and number == 6:
                            variant = "VC8"
                        if mode == "mppt_quantity_probe" and number == 7:
                            variant = "VCA"
                        if mode in ("mppt_sharing_probe", "mppt_mode_probe") and number in (0, 2):
                            variant = "VC5" if number == 0 else "VC4"
                        report = await call("pvsyst_validate_variant_structure", **target, variant=variant)
                        groups, inputs, parallel = quantities[name] if mode in ("mppt_quantity_probe", "mppt_allocation_probe") else (1, 1, 2)
                        expected_valid = not (mode == "mppt_quantity_probe" and name == "two_groups_one_input")
                        if mode == "mppt_quantity_probe" and name == "shared_clone":
                            assert report["valid"] is True and report["grid_circuit"]["totals"] == {"inverters": 1, "strings": 4, "explicit_mppt_inputs": 2}
                        elif mode == "mppt_sharing_probe":
                            assert report["valid"] is True and report["grid_circuit"]["totals"] == {
                                "inverters": 1, "strings": 5 if name == "shared_more_strings" else 3, "explicit_mppt_inputs": 2}
                            assert len(report["grid_circuit"]["subarrays"]) == 2
                            assert all(row["shared_inverter_lines"] and row["explicit_mppt_inputs_match_declared"] is True
                                       for row in report["grid_circuit"]["subarrays"])
                        else:
                            assert report["valid"] is expected_valid and report["grid_circuit"]["totals"] == {
                                "inverters": groups, "strings": groups * inputs * parallel,
                                "explicit_mppt_inputs": 0 if mode == "mppt_flat_probe" else groups * inputs}
                        if mode == "mppt_flat_probe":
                            path = root / "Projects" / "MCP_MPPT_PROBE.VC0"
                            original = path.read_bytes()
                            stopped = await session.call_tool("pvsyst_update_variant_parameters", {**target, "variant": variant,
                                "expected_sha256": report["sha256"], "subarray_updates": {"NStringCh": 4}})
                            assert stopped.is_error and "exclusive explicit MPPT allocation" in stopped.content[0].text
                            assert path.read_bytes() == original
                            evidence["ordinary_edit_stopped_without_writes"] = True
                        case = {"case": name, "flags": flags, "nominal_ac_per_input_kw": nominal, "inspection": report}
                        response = await session.call_tool("pvsyst_run_simulation", {**target, "variant": variant,
                            "sfi_name": "capacity.sfi", "csv_name": f"probe_{name}.csv", "pdf_name": f"probe_{name}.pdf",
                            "report_pages": "params", "site_name": "Wanaluwawa.SIT", "met_name": "Wanaluwawa_Nasa_SYN.MET",
                            "start_date": "1990.01.01", "end_date": "1990.01.02"}, read_timeout_seconds=1800)
                        if response.is_error:
                            case["error"] = response.content[0].text
                        else:
                            result = json.loads(response.content[0].text)
                            columns, rows = parse_result_csv(result["csv"])
                            assert len(rows) == 48
                            metrics = {key: [row[columns.index(key)] for row in rows] for key in ("EArray", "E_Grid", "IL_Pmax")}
                            assert all(math.isfinite(value) for values in metrics.values() for value in values)
                            case.update(csv=result["csv"], pdf=result["pdf"], rows=len(rows),
                                sum_hourly_kw={key: sum(values) for key, values in metrics.items()},
                                peak_kw={key: max(values) for key, values in metrics.items()},
                                report_text="\n".join(page.extract_text() for page in PdfReader(result["pdf"]).pages))
                        evidence["cases"].append(case)
                        print(f"MPPT probe {name}: {case.get('sum_hourly_kw', case.get('error'))}", flush=True)
                    if mode == "mppt_sharing_probe":
                        assert all("error" not in case for case in evidence["cases"])
                        hourly = [parse_result_csv(case["csv"]) for case in evidence["cases"]]
                        assert hourly[0] != hourly[1] and hourly[1] == hourly[2] == hourly[3]
                        assert "No power sharing" in evidence["cases"][0]["report_text"]
                        assert all("Power sharing defined" in case["report_text"] and "MPPT 67%" in case["report_text"]
                                   and "MPPT 33%" in case["report_text"] for case in evidence["cases"][1:4])
                        assert "MPPT 80%" in evidence["cases"][4]["report_text"] and "MPPT 20%" in evidence["cases"][4]["report_text"]
                        assert hourly[4] != hourly[2]
                        evidence["shared_member_string_edit"]["restore"] = await call("pvsyst_restore_variant", **target, variant="VC7",
                            backup_name=member_edit["backup_name"], expected_sha256=member_edit["sha256"], confirm=True)
                        assert member_path.read_bytes() == member_original
                        evidence["shared_member_string_edit"]["baseline_bytes_restored"] = True
                        evidence["saved_allocation_changed_hourly_results"] = False
                        evidence["group_id_controls_shared_allocation"] = True
                        for record, (variant, original_bytes, applied) in zip(evidence["power_sharing_edits"], sharing_edits):
                            record["restore"] = await call("pvsyst_restore_variant", **target, variant=variant,
                                backup_name=applied["backup_name"], expected_sha256=applied["sha256"], confirm=True)
                            assert (root / "Projects" / f"MCP_MPPT_PROBE.{variant}").read_bytes() == original_bytes
                            record["baseline_bytes_restored"] = True
                        copied = await call("pvsyst_clone_variant", **target, source_variant="VC2", new_variant="VC8")
                        lifecycle_path = root / "Projects" / "MCP_MPPT_PROBE.VC8"
                        active_bytes = lifecycle_path.read_bytes()
                        stopped = await session.call_tool("pvsyst_remove_subarray", {**target, "variant": "VC8",
                            "expected_sha256": copied["sha256"], "subarray_id": 2, "shared_member": True})
                        assert stopped.is_error and lifecycle_path.read_bytes() == active_bytes
                        disabled = await call("pvsyst_update_variant_parameters", **target, variant="VC8",
                            expected_sha256=copied["sha256"], power_sharing_enabled=False)
                        disabled_bytes = lifecycle_path.read_bytes()
                        removed = await call("pvsyst_remove_subarray", **target, variant="VC8",
                            expected_sha256=disabled["sha256"], subarray_id=2, shared_member=True)
                        assert removed["remaining_member_id"] == 1
                        assert lifecycle_path.read_bytes() == (root / "Projects" / "MCP_MPPT_PROBE.VC9").read_bytes()
                        report = await call("pvsyst_validate_variant_structure", **target, variant="VC8")
                        assert report["valid"] is True and report["grid_circuit"]["totals"] == {"inverters": 1, "strings": 2, "explicit_mppt_inputs": 1}
                        lifecycle = {"active_delete_stopped": True, "disable": disabled, "remove": removed,
                            "matches_authored_control_bytes": True, "inspection": report, "native": []}
                        evidence["active_shared_member_lifecycle"] = lifecycle
                        for variant in ("VC8", "VC9"):
                            native = await call("pvsyst_run_simulation", **target, variant=variant,
                                sfi_name="capacity.sfi", csv_name=f"sharing_remove_{variant}.csv", pdf_name=f"sharing_remove_{variant}.pdf",
                                report_pages="params", site_name="Wanaluwawa.SIT", met_name="Wanaluwawa_Nasa_SYN.MET",
                                start_date="1990.01.01", end_date="1990.01.02")
                            columns, rows = parse_result_csv(native["csv"])
                            assert len(rows) == 48 and all(math.isfinite(value) for row in rows for value in row[1:])
                            native["sum_hourly_kw"] = {key: sum(row[columns.index(key)] for row in rows) for key in ("EArray", "E_Grid", "IL_Pmax")}
                            native["report_text"] = "\n".join(page.extract_text() for page in PdfReader(native["pdf"]).pages)
                            assert "1 * MPPT 50%" in native["report_text"]
                            lifecycle["native"].append(native)
                        assert parse_result_csv(lifecycle["native"][0]["csv"]) == parse_result_csv(lifecycle["native"][1]["csv"])
                        lifecycle["hourly_outputs_identical"] = True
                        restored = await call("pvsyst_restore_variant", **target, variant="VC8",
                            backup_name=removed["backup_name"], expected_sha256=removed["sha256"], confirm=True)
                        assert lifecycle_path.read_bytes() == disabled_bytes
                        lifecycle["restore_deleted_member"] = restored
                        lifecycle["restore_active_group"] = await call("pvsyst_restore_variant", **target, variant="VC8",
                            backup_name=disabled["backup_name"], expected_sha256=restored["sha256"], confirm=True)
                        assert lifecycle_path.read_bytes() == active_bytes
                        lifecycle["baseline_bytes_restored"] = True
                        return
                    if mode == "mppt_quantity_probe":
                        assert all("error" not in case for case in evidence["cases"][:3])
                        assert all(case["sum_hourly_kw"]["E_Grid"] > 0 for case in evidence["cases"][:3])
                        evidence["equivalent_quantity_factorizations"] = (
                            parse_result_csv(evidence["cases"][1]["csv"]) == parse_result_csv(evidence["cases"][2]["csv"]))
                        evidence["three_inputs_native_accepted"] = "error" not in evidence["cases"][3]
                        evidence["mppt_quantity_edit"]["restore"] = await call("pvsyst_restore_variant", **target, variant="VC4",
                            backup_name=edited["backup_name"], expected_sha256=edited["sha256"], confirm=True)
                        assert (root / "Projects" / "MCP_MPPT_PROBE.VC4").read_bytes() == original
                        evidence["mppt_quantity_edit"]["baseline_bytes_restored"] = True
                        evidence["independent_string_edit"]["restore"] = await call("pvsyst_restore_variant", **target, variant="VC5",
                            backup_name=string_edit["backup_name"], expected_sha256=string_edit["sha256"], confirm=True)
                        assert (root / "Projects" / "MCP_MPPT_PROBE.VC5").read_bytes() == string_original
                        evidence["independent_string_edit"]["baseline_bytes_restored"] = True
                        assert "error" not in evidence["cases"][4]
                        assert "error" not in evidence["cases"][5]
                        assert evidence["cases"][5]["report_text"].count("1 * MPPT 50%") == 2
                        assert "No power sharing" in evidence["cases"][5]["report_text"]
                        member_removed = await call("pvsyst_remove_subarray", **target, variant="VC7", subarray_id=parent_edit["subarray_id"],
                            expected_sha256=parent_edit["sha256"], shared_member=True)
                        assert parent_path.read_bytes() == parent_original
                        removal_report = await call("pvsyst_validate_variant_structure", **target, variant="VC7")
                        assert removal_report["valid"] is True and removal_report["subarray_ids"] == [1]
                        removal_run = await call("pvsyst_run_simulation", **target, variant="VC7", sfi_name="capacity.sfi",
                            csv_name="probe_shared_clone_removed.csv", pdf_name="probe_shared_clone_removed.pdf", report_pages="params",
                            site_name="Wanaluwawa.SIT", met_name="Wanaluwawa_Nasa_SYN.MET", start_date="1990.01.01", end_date="1990.01.02")
                        assert parse_result_csv(removal_run["csv"]) == parse_result_csv(evidence["cases"][0]["csv"])
                        recovered = await call("pvsyst_restore_variant", **target, variant="VC7", backup_name=member_removed["backup_name"],
                            expected_sha256=member_removed["sha256"], confirm=True)
                        assert parent_path.read_bytes() == clone_control
                        evidence["shared_member_remove"] = {"apply": member_removed, "inspection": removal_report,
                            "run": removal_run, "baseline_bytes_restored": True, "baseline_hourly_identical": True, "restore": recovered}
                        evidence["shared_parent_clone"]["restore"] = await call("pvsyst_restore_variant", **target, variant="VC7",
                            backup_name=parent_edit["backup_name"], expected_sha256=parent_edit["sha256"], confirm=True)
                        assert parent_path.read_bytes() == parent_original
                        evidence["shared_parent_clone"]["baseline_bytes_restored"] = True
                        assert "error" not in evidence["cases"][6] and "4 * MPPT 50%" in evidence["cases"][6]["report_text"]
                        assert 1.9 < evidence["cases"][6]["sum_hourly_kw"]["E_Grid"] / evidence["cases"][1]["sum_hourly_kw"]["E_Grid"] < 2.1
                        evidence["independent_inverter_edit"]["restore"] = await call("pvsyst_restore_variant", **target, variant="VC8",
                            backup_name=inverter_edit["backup_name"], expected_sha256=inverter_edit["sha256"], confirm=True)
                        assert inverter_path.read_bytes() == inverter_original
                        evidence["independent_inverter_edit"]["baseline_bytes_restored"] = True
                        assert "error" not in evidence["cases"][7]
                        series_row = evidence["cases"][7]["inspection"]["grid_circuit"]["subarrays"][0]
                        assert series_row["declared"]["NModSerie"] == "18" and series_row["circuit_module_count"] == series_row["declared_module_count"] == 36
                        assert "2 units x 18" in evidence["cases"][7]["report_text"]
                        operating = []
                        for case in (evidence["cases"][0], evidence["cases"][7]):
                            values = case["report_text"].split("Pmpp\nU mpp\nI mpp\n", 1)[1].splitlines()
                            operating.append(dict(zip(("power_kwp", "voltage_v", "current_a"), map(float, values[:3]))))
                        assert abs(operating[1]["voltage_v"] / operating[0]["voltage_v"] - 18 / 15) < 0.005
                        assert operating[1]["current_a"] == operating[0]["current_a"]
                        evidence["series_module_edit"]["operating_at_50c"] = operating
                        assert parse_result_csv(evidence["cases"][7]["csv"]) != parse_result_csv(evidence["cases"][0]["csv"])
                        evidence["series_module_edit"]["restore"] = await call("pvsyst_restore_variant", **target, variant="VCA",
                            backup_name=series_edit["backup_name"], expected_sha256=series_edit["sha256"], confirm=True)
                        assert series_path.read_bytes() == series_original
                        evidence["series_module_edit"]["baseline_bytes_restored"] = True
                        return
                    if mode == "mppt_allocation_probe":
                        assert all("error" not in case for case in evidence["cases"])
                        hourly = [parse_result_csv(case["csv"]) for case in evidence["cases"]]
                        evidence["node_expansion_changes_hourly_results"] = hourly[0] != hourly[1]
                        evidence["unequal_leaf_allocation_changes_hourly_results"] = hourly[1] != hourly[2]
                        assert hourly[0] == hourly[1] == hourly[2]
                        assert all("2 * MPPT 50%" in case["report_text"] for case in evidence["cases"])
                        return
                    if mode == "mppt_flat_probe":
                        case = evidence["cases"][0]
                        assert "error" not in case and "MPPT 50%" in case["report_text"]
                        assert case["sum_hourly_kw"]["IL_Pmax"] > 0
                        evidence["independent_mode_without_explicit_mppt_nodes"] = True
                        return
                    assert all("error" not in case for case in evidence["cases"])
                    hourly = [parse_result_csv(case["csv"]) for case in evidence["cases"]]
                    assert hourly[0] == hourly[1] and hourly[2] == hourly[3]
                    assert all("Power sharing within this inverter" in case["report_text"] for case in evidence["cases"][:2])
                    assert all("MPPT 50%" in case["report_text"] for case in evidence["cases"][2:])
                    evidence["nominal_field_controls_hourly_identical"] = True
                    whole, independent = evidence["cases"][0], evidence["cases"][3]
                    assert independent["sum_hourly_kw"]["IL_Pmax"] > whole["sum_hourly_kw"]["IL_Pmax"]
                    assert 0 < independent["sum_hourly_kw"]["E_Grid"] < whole["sum_hourly_kw"]["E_Grid"]
                    evidence["clipping_sensitive_control_passed"] = True
                    for record, (variant, original_bytes, applied) in zip(evidence["mppt_mode_edits"], sharing_edits):
                        record["restore"] = await call("pvsyst_restore_variant", **target, variant=variant,
                            backup_name=applied["backup_name"], expected_sha256=applied["sha256"], confirm=True)
                        assert (root / "Projects" / f"MCP_MPPT_PROBE.{variant}").read_bytes() == original_bytes
                        record["baseline_bytes_restored"] = True
                    return
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
    parser.add_argument("--mode", choices=("inverters", "strings", "subarrays", "orientations", "reassociation", "project_variants", "creation", "pruning", "mppt_branches", "mppt_orientations", "mppt_reassociation", "mppt_pruning", "mppt_mode_probe", "mppt_flat_probe", "mppt_quantity_probe", "mppt_sharing_probe", "mppt_allocation_probe"), default="inverters")
    parser.add_argument("--prune-plane", type=int, choices=(1, 2), default=2, help="Plane removed in pruning mode")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.mode, arguments.prune_plane))

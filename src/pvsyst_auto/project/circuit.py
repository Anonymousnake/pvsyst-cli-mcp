"""Inspect grid counts, synchronize inverter/string edits, and manage subarray topology."""
from __future__ import annotations

import re

from pvsyst_auto.components.editor import replace_value, root_block
from pvsyst_auto.project.circuit_branches import (
    _validate_clone_subarray_args,
    clone_subarray,
    clone_subarray_data,
    remove_subarray,
    require_plane_branch_mode,
    subarray_branches,
    validate_structure,
)
from pvsyst_auto.project.circuit_core import (
    CHILDREN,
    KINDS,
    MAX_COUNT,
    NODE,
    TRANSFORMER_KEYS,
    Node,
    UnsupportedCircuit,
    _encode_candidate,
    _need,
    _require_single_orient_strings,
    _set_flag_bit,
    _walk,
    circuit_nodes,
    inventory,
    positive_integer,
    require_ordinary_mppt,
    topology_state,
    transformer_state,
)
from pvsyst_auto.validation import NATIVE_TEXT_OR_SEED, SEED_LABEL


def set_independent_mppt(lines, arrays, subarray_id, enabled):
    """Switch a verified explicit exclusive layout without redrawing its circuit."""
    checked = inventory(lines, arrays)
    _need(checked["counts_valid"] is not True, "MPPT mode switching requires complete consistent grid counts")
    row = next((r for r in checked["subarrays"] if r["id"] == subarray_id), None)
    _need(row is None, f"Unknown subarray id {subarray_id}")
    _need(bool(row["shared_inverter_lines"]) or not row["circuit"]["explicit_mppt_inputs"],
          "MPPT mode switching requires exclusive explicit MPPT inputs")
    for root_node in circuit_nodes(lines):
        for node in _walk(root_node):
            if node.kind == "InverterNode" and node.one("SubArrayId") == str(subarray_id):
                _need(not node.children or any(c.kind != "MPPTNode" for c in node.children),
                      "MPPT mode switching requires only direct explicit MPPT children")
    start, end = arrays[subarray_id]
    root = root_block(lines, (start, end))
    flags = root.one("Flags")[1]
    _need(root.one("Version")[1] not in NATIVE_TEXT_OR_SEED or not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags),
          f"MPPT mode switching requires verified Version={SEED_LABEL} Flags")
    _need(root.one("PowerSharingInverterId")[1] != "-1", "MPPT mode switching requires explicit unshared PowerSharingInverterId=-1")
    _require_single_orient_strings(root, "MPPT mode switching requires all strings in one orientation")
    was_enabled = bool(int(flags[1:], 16) & 0x400)
    if not was_enabled:
        require_ordinary_mppt(root)
    quantity, before_quantity = (row["circuit"]["explicit_mppt_inputs"] if enabled else 1), root.one("NInvMPPT")[1]
    for key, rendered in (("Flags", _set_flag_bit(flags, 0x400, enabled)), ("NInvMPPT", str(quantity))):
        index = start + root.one(key)[0]
        lines[index] = replace_value(lines[index], rendered)
    _need(inventory(lines, arrays)["counts_valid"] is not True, "MPPT mode switching failed structure validation")
    return {"subarray_id": subarray_id, "enabled_before": was_enabled, "enabled_after": enabled,
            "inputs_before": before_quantity, "inputs_after": quantity}


def set_power_sharing(lines, arrays, subarray_id, enabled):
    """Toggle a verified existing shared parent without redesigning allocation."""
    checked = inventory(lines, arrays)
    _need(checked["counts_valid"] is not True, "Power sharing requires complete consistent grid counts")
    groups = [g for g in checked["inverter_groups"] if subarray_id in g.get("subarray_ids", [])]
    _need(len(groups) != 1, "Power sharing requires exactly one existing shared inverter parent")
    group, members, plane, edits = groups[0], groups[0]["subarray_ids"], None, {}
    for row in checked["subarrays"]:
        ident = row["id"]
        if ident not in members:
            _need(row["declared"].get("PowerSharingInverterId") == "0", "Power-sharing group 0 is already referenced by another subarray")
            continue
        _need(row["circuit"]["inverters"] != group["inverters"] or len(row["shared_inverter_lines"]) != 1,
              "Power-sharing members must reference only the selected shared parent")
        start, end = arrays[ident]
        root = root_block(lines, (start, end))
        orientation = root.one("NoOrientation")[1]
        _need(plane is not None and plane != orientation, "Power-sharing members require the same fixed-plane orientation")
        plane = orientation
        _require_single_orient_strings(root, "Power sharing requires all member strings in one orientation")
        flags = root.one("Flags")[1]
        for key, rendered in (("Flags", _set_flag_bit(flags, 0x100, enabled)), ("PowerSharingInverterId", "0" if enabled else "-1")):
            index = start + root.one(key)[0]
            edits[index] = replace_value(lines[index], rendered)
    for idx, val in edits.items():
        lines[idx] = val
    _need(inventory(lines, arrays)["counts_valid"] is not True, "Power-sharing settings failed structure validation")
    return {"subarray_ids": members, "inverter_line": group["line"],
            "enabled_before": group["power_sharing_id"] == 0, "enabled_after": enabled}


def resize_capacity(lines, arrays, subarray_id, target_inverters=None, target_strings=None, target_mppt=None):
    """Scale ordinary groups or explicit independent inputs with integral allocation."""
    before = inventory(lines, arrays)
    if before["counts_valid"] is not True:
        raise ValueError("Capacity editing requires consistent, fully checked grid counts")
    start, end = arrays[subarray_id]
    root = root_block(lines, (start, end))
    positive_integer(root.one("NoOrientation")[1], "NoOrientation")
    flags = root.one("Flags")[1] if "Flags" in root.fields else ""
    independent = target_mppt is not None or ((target_strings is not None or target_inverters is not None)
                                              and re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) and bool(int(flags[1:], 16) & 0x400))
    inverter_scaling, shared_line = independent and target_inverters is not None, None
    if independent:
        if (target_mppt is not None and (target_inverters is not None or target_strings is not None)) or (inverter_scaling and target_strings is not None):
            raise ValueError("Independent NInverter/NInvMPPT/NStringCh edits must be separate")
        if "Version" not in root.fields or root.one("Version")[1] not in NATIVE_TEXT_OR_SEED or not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) or not int(flags[1:], 16) & 0x400:
            field = "NInverter" if inverter_scaling else "NStringCh" if target_strings is not None else "NInvMPPT"
            raise ValueError(f"{field} editing requires verified Version={'/'.join(NATIVE_TEXT_OR_SEED)} independent $0400 mode")
        row = next((r for r in before["subarrays"] if r["id"] == subarray_id), None)
        if row is None:
            raise ValueError(f"Unknown subarray id {subarray_id}")
        if row["shared_inverter_lines"]:
            groups = [g for g in before["inverter_groups"] if subarray_id in g.get("subarray_ids", [])]
            if target_mppt is not None or inverter_scaling or len(groups) != 1 or len(row["shared_inverter_lines"]) != 1 or row["circuit"]["inverters"] != groups[0]["inverters"]:
                raise ValueError("Shared-parent capacity editing supports member strings only")
            shared_line = groups[0]["line"]
            for ident, bounds in arrays.items():
                peer = root_block(lines, bounds)
                if ident in groups[0]["subarray_ids"]:
                    if peer.one("NoOrientation")[1] != root.one("NoOrientation")[1]:
                        raise ValueError("Shared member strings require one common orientation")
                    _require_single_orient_strings(peer, "Shared member strings require one common orientation")
                elif groups[0]["power_sharing_id"] == 0 and "PowerSharingInverterId" in peer.fields and peer.one("PowerSharingInverterId")[1] == "0":
                    raise ValueError("Shared-parent group 0 is referenced outside its members")
        if "PowerSharingInverterId" not in root.fields or root.one("PowerSharingInverterId")[1] not in (("-1", "0") if shared_line is not None else ("-1",)):
            raise ValueError("NInvMPPT editing requires explicit unshared PowerSharingInverterId=-1")
        if row["explicit_mppt_inputs_match_declared"] is not True:
            raise ValueError("NInvMPPT editing requires complete exclusive explicit MPPT allocation")
    else:
        require_ordinary_mppt(root)
    if shared_line is None and "PowerSharingInverterId" in root.fields and root.one("PowerSharingInverterId")[1] != "-1":
        raise ValueError("Shared inverter power is not supported for capacity editing")
    quantity = "NInvMPPT" if independent and not inverter_scaling else "NInverter"
    old, strings = positive_integer(root.one(quantity)[1], quantity), positive_integer(root.one("NStringCh")[1], "NStringCh")
    _require_single_orient_strings(root, "Capacity editing requires all strings in one orientation", expected=str(strings))
    edits, scaled_inputs = {}, None
    target = (old if target_mppt is None else target_mppt) if independent and not inverter_scaling else (old if target_inverters is None else target_inverters)
    if inverter_scaling:
        scaled_inputs, rem = divmod(positive_integer(root.one("NInvMPPT")[1], "NInvMPPT") * target, old)
        if rem or not 1 <= scaled_inputs <= MAX_COUNT:
            raise ValueError("Target must preserve integral independent input allocation")
    scaled_strings, rem = divmod(strings * target, old) if target_strings is None else (target_strings, 0)
    if rem or not 1 <= scaled_strings <= MAX_COUNT:
        raise ValueError("Target must preserve integral string/group allocation")

    def scale_node(node, numerator, denominator):
        current = positive_integer(node.one("NElements", "1"), f"{node.kind} NElements")
        num, rem_n = divmod(current * numerator, denominator)
        if rem_n or not 1 <= num <= MAX_COUNT:
            raise ValueError("Target must preserve integral string/group allocation")
        if num != current:
            locs = node.field_lines.get("NElements", [])
            if len(locs) != 1:
                raise ValueError(f"Capacity editing requires explicit {node.kind} NElements")
            edits[locs[0]] = replace_value(lines[locs[0]], str(num))
        return {"line": node.line, "before": current, "after": num}

    selected, string_groups, mppt_groups = [], [], []
    for node in (item for root_node in circuit_nodes(lines) for item in _walk(root_node)):
        if node.kind != "InverterNode" or (node.line != shared_line if shared_line is not None else node.one("SubArrayId") != str(subarray_id)):
            continue
        if independent:
            if not node.children or any(c.kind != "MPPTNode" for c in node.children):
                raise ValueError("NInvMPPT editing requires direct explicit MPPT children")
            selected.append(scale_node(node, target, old) if inverter_scaling else scale_node(node, 1, 1))
            for port in node.children:
                if shared_line is not None and port.one("SubArrayId") != str(subarray_id):
                    continue
                mppt_groups.append(scale_node(port, 1, 1) if inverter_scaling else scale_node(port, target, old))
                if target_strings is not None:
                    string_groups.extend(scale_node(leaf, scaled_strings, strings) for leaf in port.children)
        else:
            if any(item.kind == "MPPTNode" for item in _walk(node)):
                raise ValueError("Explicit MPPT branches require separate mode-aware capacity editing")
            selected.append(scale_node(node, target, old))
            string_groups.extend(scale_node(sn, scaled_strings * old, strings * target) for sn in node.children)
    if not selected:
        raise ValueError("Subarray has no supported exclusive inverter groups")
    for key, num in [(quantity, target), ("NStringCh", scaled_strings), ("NStrOrient1", scaled_strings)] + ([("NInvMPPT", scaled_inputs)] if inverter_scaling else []):
        idx = start + root.one(key)[0]
        edits[idx] = replace_value(lines[idx], str(num))
    for idx, line in edits.items():
        lines[idx] = line
    if inventory(lines, arrays)["counts_valid"] is not True:
        raise ValueError("Scaled grid counts failed validation")
    result = {
        "subarray_id": subarray_id, "inverters_before": positive_integer(root.one("NInverter")[1], "NInverter"),
        "inverters_after": positive_integer(root.one("NInverter")[1], "NInverter") if independent and not inverter_scaling else target,
        "strings_before": strings, "strings_after": scaled_strings, "groups": selected, "string_groups": string_groups,
    }
    if independent:
        result.update(mppt_inputs_before=positive_integer(root.one("NInvMPPT")[1], "NInvMPPT"),
                      mppt_inputs_after=scaled_inputs if inverter_scaling else target, mppt_groups=mppt_groups)
    if shared_line is not None:
        result["shared_parent_line"] = shared_line
    return result

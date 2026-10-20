"""Inspect grid counts and synchronize bounded ordinary inverter/string edits."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pvsyst_component_editor import ASSIGNMENT, objects, replace_value

NODE = re.compile(r"([A-Za-z][A-Za-z0-9_]*Node) (Start|End);")
KINDS = {"InjectionPointNode", "TransformerNode", "InverterNode", "MPPTNode", "StringNode"}
CHILDREN = {
    "InjectionPointNode": {"TransformerNode", "InverterNode"},
    "TransformerNode": {"TransformerNode", "InverterNode"},
    "InverterNode": {"MPPTNode", "StringNode"},
    "MPPTNode": {"StringNode"}, "StringNode": set(),
}
MAX_COUNT = 2147483647


class UnsupportedCircuit(ValueError):
    pass


@dataclass
class Node:
    kind: str
    line: int
    fields: dict[str, list[str]] = field(default_factory=dict)
    children: list[Node] = field(default_factory=list)
    end_line: int = 0
    field_lines: dict[str, list[int]] = field(default_factory=dict)

    def one(self, key, default=None):
        values = self.fields.get(key, [])
        if not values:
            return default
        if len(values) != 1:
            raise ValueError(f"Circuit line {self.line}: duplicate {key}")
        return values[0]


def positive_integer(value, context):
    if (not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value)
            or not 1 <= int(value) <= MAX_COUNT):
        raise ValueError(f"{context} must be a positive 32-bit integer")
    return int(value)


def circuit_nodes(lines):
    """Fields in CableDefinition/other records never become node properties."""
    starts = [i for i, line in enumerate(lines)
              if line.strip() == "PVObject_SystemCircuit=pvCircuit"]
    ends = [i for i, line in enumerate(lines)
            if line.strip() == "End of PVObject pvCircuit"]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError("Expected one closed PVObject_SystemCircuit=pvCircuit")
    roots, stack = [], []
    count = 0
    for index in range(starts[0] + 1, ends[0]):
        value = lines[index].strip()
        marker = NODE.fullmatch(value)
        if marker:
            kind, action = marker.groups()
            if kind not in KINDS:
                raise UnsupportedCircuit(f"Unsupported circuit node {kind} at line {index + 1}")
            if action == "End":
                if not stack or not isinstance(stack[-1], Node) or stack[-1].kind != kind:
                    raise ValueError(f"Unmatched circuit node at line {index + 1}")
                stack[-1].end_line = index + 1
                stack.pop()
                continue
            if stack and not (isinstance(stack[-1], tuple) and stack[-1][0] in ("Children", "BaseNode")):
                raise ValueError(f"Node outside a Children/BaseNode record at line {index + 1}")
            if stack and not (
                    (stack[-1][0] == "BaseNode" and len(stack) == 1)
                    or (stack[-1][0] == "Children" and len(stack) > 1 and isinstance(stack[-2], Node))):
                raise ValueError(f"Node inside nested metadata at line {index + 1}")
            parent = next((item for item in reversed(stack) if isinstance(item, Node)), None)
            if parent and kind not in CHILDREN[parent.kind]:
                raise ValueError(f"Unexpected {kind} below {parent.kind} at line {index + 1}")
            if not parent and kind not in ("InjectionPointNode", "TransformerNode", "InverterNode"):
                raise ValueError(f"Unexpected circuit root {kind} at line {index + 1}")
            node = Node(kind, index + 1)
            (parent.children if parent else roots).append(node)
            stack.append(node)
            count += 1
            if count > 10000:
                raise UnsupportedCircuit("Circuit exceeds 10000 serialized nodes")
        else:
            match = ASSIGNMENT.fullmatch(lines[index].rstrip("\r\n"))
            if not match:
                if value:
                    raise UnsupportedCircuit(f"Unrecognized circuit record at line {index + 1}")
                continue
            key, value = match[2], match[4]
            if key in ("NElements", "SubArrayId", "SubArrayName"):
                if stack and isinstance(stack[-1], Node):
                    stack[-1].fields.setdefault(key, []).append(value)
                    stack[-1].field_lines.setdefault(key, []).append(index)
                elif not stack or stack[-1][0] in ("Children", "BaseNode"):
                    raise ValueError(f"Circuit node field outside a node at line {index + 1}")
            elif value == "Start":
                stack.append((key, index))
            elif value == "End":
                if not stack or not isinstance(stack[-1], tuple) or stack[-1][0] != key:
                    raise ValueError(f"Unmatched circuit record {key} at line {index + 1}")
                stack.pop()
            elif stack and isinstance(stack[-1], Node):
                stack[-1].fields.setdefault(key, []).append(value)
                stack[-1].field_lines.setdefault(key, []).append(index)
        if len(stack) > 64:
            raise UnsupportedCircuit("Circuit nesting exceeds 64 supported levels")
    if stack:
        raise ValueError("Unclosed circuit node or record")
    if not roots:
        raise ValueError("Grid circuit has no nodes")
    return roots


TRANSFORMER_KEYS = ("OutputVoltage", "STCRefPower", "STCIronLossFrac", "STCIronLoss",
                    "STCCopperLossFrac", "NightDisconnect", "IsHVTransformer")


def transformer_state(lines):
    """Read the serialized MV/HV transformer layers without interpreting any quantity.

    The native saves carry these seven fields on every TransformerNode, and an HV
    configuration nests an HV layer above the MV one. Values are reported exactly as
    stored: this function does not convert units, derive reference power, or decide
    whether a loss fraction is physically consistent.
    """
    result = {"layers": [], "count": 0, "nested": False, "issues": [],
              "scope": "serialized transformer fields and nesting; not electrical validation"}
    try:
        roots = circuit_nodes(lines)
    except (UnsupportedCircuit, ValueError) as exc:
        result["issues"].append(str(exc))
        return result
    found = []

    def walk(node, depth):
        if node.kind == "TransformerNode":
            missing = [key for key in TRANSFORMER_KEYS if key not in node.fields]
            repeated = [key for key in TRANSFORMER_KEYS if len(node.fields.get(key, [])) > 1]
            if missing or repeated:
                result["issues"].append(
                    "TransformerNode at line %d is missing or repeats %s"
                    % (node.line, ", ".join(sorted(missing + repeated))))
            else:
                found.append({"depth": depth,
                              "fields": {key: node.one(key) for key in TRANSFORMER_KEYS}})
        for child in node.children:
            walk(child, depth + 1 if node.kind == "TransformerNode" else depth)

    for root in roots:
        walk(root, 0)
    result["layers"] = found
    result["count"] = len(found)
    result["nested"] = any(layer["depth"] > 0 for layer in found)
    return result


def inventory(lines, arrays):
    """Compare serialized inverter/string multiplicities with subarray declarations.

    MPPT nodes are reported separately: their presence alone does not establish
    the simulation's independent-MPPT mode or the meaning of NInvMPPT.
    """
    result = {"issues": [], "unchecked_checks": [], "inverter_groups": [],
              "subarrays": [], "totals": None,
              "counts_complete": False, "counts_valid": None,
              "scope": "serialized circuit references and inverter/string counts; not electrical sizing"}
    issues, unchecked = result["issues"], result["unchecked_checks"]
    try:
        roots = circuit_nodes(lines)
    except UnsupportedCircuit as exc:
        unchecked.append(str(exc))
        return result
    except ValueError as exc:
        issues.append(str(exc))
        unchecked.append("circuit references and counts after parse failure")
        result["counts_valid"] = False
        return result
    declared = {}
    for identifier, (start, end) in arrays.items():
        try:
            blocks = objects("".join(lines[start:end + 1]))
            root = next(block for block in blocks if not block.path)
            values = {}
            for key in ("Comment", "NModSerie", "NStringCh", "NStrOrient1", "NInverter", "NInvMPPT",
                        "Version", "Flags", "PNomAC_InMPPT", "PowerSharingInverterId", "GInverter"):
                if key in root.fields:
                    values[key] = root.one(key)[1]
            declared[identifier] = values
        except ValueError as exc:
            issues.append(f"Subarray {identifier}: {exc}")
            declared[identifier] = {}

    counts = {identifier: {"inverters": 0, "strings": 0, "explicit_mppt_inputs": 0}
              for identifier in arrays}
    owned = set()
    sharing_parents = set()

    def elements(node):
        return positive_integer(node.one("NElements", "1"), f"Circuit line {node.line}: NElements")

    def bounded(number):
        if number > MAX_COUNT:
            raise ValueError("Expanded circuit count exceeds a positive 32-bit integer")
        return number

    def walk(node):
        yield node
        for child in node.children:
            yield from walk(child)

    def branch(node):
        all_nodes = list(walk(node))
        members = list(dict.fromkeys(child.one("SubArrayId") for child in node.children))
        shared = (node.one("SubArrayId") in (None, "0", "-1") and len(members) > 1
                  and all(child.kind == "MPPTNode" and child.children and child.one("SubArrayId") not in (None, "0", "-1")
                          and all(item.one("SubArrayId") == child.one("SubArrayId") for item in walk(child))
                          for child in node.children))
        if shared:
            member_ids = [positive_integer(raw, f"Circuit line {node.line}: SubArrayId") for raw in members]
            configs = [declared.get(identifier, {}) for identifier in member_ids]
            flags = [value.get("Flags", "") for value in configs]
            group_ids = {value.get("PowerSharingInverterId") for value in configs}
            models = {value.get("GInverter", "").casefold() for value in configs}
            shared = (len(set(member_ids)) > 1 and len({value.get("Version") for value in configs}) == 1
                      and all(value.get("Version") in ("8.1.0", "8.1.6") for value in configs)
                      and all(re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flag) and int(flag[1:], 16) & 0x400 for flag in flags)
                      and len(models) == 1 and "" not in models and group_ids in ({"-1"}, {"0"}))
            if shared:
                group_id = int(next(iter(group_ids)))
                if group_id >= 0 and group_id in sharing_parents:
                    raise UnsupportedCircuit("Multiple circuit parents for a power-sharing group are not checked")
                sharing_parents.add(group_id)
                if node.one("SubArrayName") != "; ".join(value.get("Comment", "") for value in configs):
                    issues.append(f"Circuit line {node.line}: shared inverter name differs from member Comments")
        ids = set()
        for item in all_nodes:
            if shared and item is node:
                continue
            raw = item.one("SubArrayId")
            if raw is None or raw in ("-1", "0"):
                unchecked.append(f"Circuit line {item.line}: shared or unspecified subarray ownership")
                continue
            identifier = positive_integer(raw, f"Circuit line {item.line}: SubArrayId")
            ids.add(identifier)
            if identifier not in arrays:
                issues.append(f"Circuit line {item.line} references unknown subarray {identifier}")
            else:
                owned.add(identifier)
                expected_name = declared[identifier].get("Comment")
                if expected_name is None or item.one("SubArrayName") != expected_name:
                    issues.append(f"Subarray {identifier} circuit name differs from Comment at line {item.line}")
            if item.kind != "StringNode" and not item.children:
                issues.append(f"Circuit line {item.line}: {item.kind} has no child nodes")
        if not shared and (len(ids) != 1 or any(item.one("SubArrayId") in (None, "0", "-1") for item in all_nodes)):
            unchecked.append(f"Circuit line {node.line}: shared inverter allocation counts")
            return
        if any(identifier not in counts for identifier in ids):
            return
        group = {"line": node.line, "subarray_id": None if shared else next(iter(ids)),
                 "inverters": elements(node), "strings": 0, "explicit_mppt_inputs": 0}
        if shared:
            group.update(subarray_ids=member_ids, power_sharing_id=group_id)
        # Members reference the shared parent; totals count that parent only once.
        allocations = {identifier: {"inverters": group["inverters"], "strings": 0, "explicit_mppt_inputs": 0} for identifier in ids}

        def visit(item, multiplier):
            expanded = bounded(multiplier * elements(item))
            identifier = positive_integer(item.one("SubArrayId"), f"Circuit line {item.line}: SubArrayId")
            key = "strings" if item.kind == "StringNode" else "explicit_mppt_inputs" if item.kind == "MPPTNode" else None
            if key:
                group[key] = bounded(group[key] + expanded)
                allocations[identifier][key] = bounded(allocations[identifier][key] + expanded)
            for child in item.children:
                visit(child, expanded)

        for child in node.children:
            visit(child, group["inverters"])
        for identifier, allocated in allocations.items():
            for key in counts[identifier]:
                counts[identifier][key] = bounded(counts[identifier][key] + allocated[key])
        result["inverter_groups"].append(group)

    def visit_root(node):
        if node.kind == "InverterNode":
            branch(node)
        else:
            if elements(node) != 1:
                raise UnsupportedCircuit(f"Circuit line {node.line}: repeated transformer/injection counts")
            for child in node.children:
                visit_root(child)

    try:
        for node in roots:
            visit_root(node)
    except UnsupportedCircuit as exc:
        unchecked.append(str(exc))
    except ValueError as exc:
        issues.append(str(exc))
        unchecked.append("circuit counts after invalid node")
    for identifier in arrays:
        if not unchecked and identifier not in owned:
            issues.append(f"Subarray {identifier} has no circuit branch reference")
    # Do not present partial sums as complete after a shared/unsupported branch.
    complete_circuit = not unchecked and not issues
    for identifier, values in declared.items():
        row = {"id": identifier, "declared": values,
               "circuit": counts[identifier] if complete_circuit else None,
               "circuit_module_count": None, "declared_module_count": None,
               "independent_mppt_inputs_declared": None,
               "explicit_mppt_inputs_match_declared": None,
               "shared_inverter_lines": [group["line"] for group in result["inverter_groups"]
                                         if identifier in group.get("subarray_ids", [])]}
        flags = values.get("Flags", "")
        if (values.get("Version") in ("8.1.0", "8.1.6") and re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags)
                and int(flags[1:], 16) & 0x400):
            try:
                inputs = positive_integer(values.get("NInvMPPT"), f"Subarray {identifier}: NInvMPPT")
                row["independent_mppt_inputs_declared"] = inputs
                if complete_circuit and counts[identifier]["explicit_mppt_inputs"]:
                    match = inputs == counts[identifier]["explicit_mppt_inputs"]
                    row["explicit_mppt_inputs_match_declared"] = match
                    if not match:
                        issues.append(f"Subarray {identifier}: independent NInvMPPT={inputs} differs from explicit circuit MPPT inputs={counts[identifier]['explicit_mppt_inputs']}")
            except ValueError as exc:
                issues.append(str(exc))
        for key, count_key in (("NInverter", "inverters"), ("NStringCh", "strings"), ("NModSerie", None)):
            if key not in values:
                unchecked.append(f"Subarray {identifier}: missing {key} for count checks")
                continue
            try:
                number = positive_integer(values[key], f"Subarray {identifier}: {key}")
                if complete_circuit and count_key and number != counts[identifier][count_key]:
                    issues.append(f"Subarray {identifier}: {key}={number} differs from circuit {count_key}={counts[identifier][count_key]}")
                if key == "NModSerie" and complete_circuit:
                    row["circuit_module_count"] = bounded(number * counts[identifier]["strings"])
            except ValueError as exc:
                issues.append(str(exc))
        if "NModSerie" in values and "NStringCh" in values:
            try:
                row["declared_module_count"] = bounded(
                    positive_integer(values["NModSerie"], "NModSerie") *
                    positive_integer(values["NStringCh"], "NStringCh"))
            except ValueError:
                pass  # Individual fields were already diagnosed above.
        result["subarrays"].append(row)
    if complete_circuit:
        result["totals"] = {key: sum(row[key] for row in counts.values()) for key in ("strings", "explicit_mppt_inputs")}
        result["totals"]["inverters"] = sum(group["inverters"] for group in result["inverter_groups"])
    result["unchecked_checks"] = list(dict.fromkeys(unchecked))
    result["counts_complete"] = not unchecked
    result["counts_valid"] = False if issues else None if unchecked else True
    return result


def require_ordinary_mppt(root):
    """A single declared input can still select independent MPPT operation."""
    if root.one("NInvMPPT")[1] != "1":
        raise ValueError("Editing requires ordinary NInvMPPT=1 mode")
    if "Flags" in root.fields:
        flags = root.one("Flags")[1]
        if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags):
            raise ValueError("Unsupported subarray Flags for ordinary editing")
        if int(flags[1:], 16) & 0x400:
            raise ValueError("Independent MPPT flag is not ordinary NInvMPPT=1 mode")


def set_independent_mppt(lines, arrays, subarray_id, enabled):
    """Switch a verified explicit exclusive layout without redrawing its circuit."""
    checked = inventory(lines, arrays)
    if checked["counts_valid"] is not True:
        raise ValueError("MPPT mode switching requires complete consistent grid counts")
    row = next(row for row in checked["subarrays"] if row["id"] == subarray_id)
    if row["shared_inverter_lines"] or not row["circuit"]["explicit_mppt_inputs"]:
        raise ValueError("MPPT mode switching requires exclusive explicit MPPT inputs")
    pending = circuit_nodes(lines)
    while pending:
        node = pending.pop()
        pending.extend(node.children)
        if node.kind == "InverterNode" and node.one("SubArrayId") == str(subarray_id):
            if not node.children or any(child.kind != "MPPTNode" for child in node.children):
                raise ValueError("MPPT mode switching requires only direct explicit MPPT children")
    start, end = arrays[subarray_id]
    root = next(block for block in objects("".join(lines[start:end + 1])) if not block.path)
    flags = root.one("Flags")[1]
    if root.one("Version")[1] not in ("8.1.0", "8.1.6") or not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags):
        raise ValueError("MPPT mode switching requires verified Version=8.1.0/8.1.6 Flags")
    if root.one("PowerSharingInverterId")[1] != "-1":
        raise ValueError("MPPT mode switching requires explicit unshared PowerSharingInverterId=-1")
    if root.one("NStrOrient1")[1] != root.one("NStringCh")[1] or any(
            key.startswith("NStrOrient") and key != "NStrOrient1" and root.one(key)[1] != "0" for key in root.fields):
        raise ValueError("MPPT mode switching requires all strings in one orientation")
    value = int(flags[1:], 16)
    was_enabled = bool(value & 0x400)
    if not was_enabled:
        require_ordinary_mppt(root)
    value = value | 0x400 if enabled else value & ~0x400
    quantity = row["circuit"]["explicit_mppt_inputs"] if enabled else 1
    before_quantity = root.one("NInvMPPT")[1]
    for key, rendered in (("Flags", f"${value:0{len(flags) - 1}X}"), ("NInvMPPT", str(quantity))):
        index = start + root.one(key)[0]
        lines[index] = replace_value(lines[index], rendered)
    if inventory(lines, arrays)["counts_valid"] is not True:
        raise ValueError("MPPT mode switching failed structure validation")
    return {"subarray_id": subarray_id, "enabled_before": was_enabled, "enabled_after": enabled,
            "inputs_before": before_quantity, "inputs_after": quantity,
            "scope": "mode declaration only; serialized layout preserved, no hardware allocation or loss refitting"}


def set_power_sharing(lines, arrays, subarray_id, enabled):
    """Toggle a verified existing shared parent without redesigning allocation."""
    checked = inventory(lines, arrays)
    if checked["counts_valid"] is not True:
        raise ValueError("Power sharing requires complete consistent grid counts")
    groups = [group for group in checked["inverter_groups"] if subarray_id in group.get("subarray_ids", [])]
    if len(groups) != 1:
        raise ValueError("Power sharing requires exactly one existing shared inverter parent")
    group = groups[0]
    members = group["subarray_ids"]
    plane = None
    edits = {}
    for row in checked["subarrays"]:
        identifier = row["id"]
        if identifier not in members:
            if row["declared"].get("PowerSharingInverterId") == "0":
                raise ValueError("Power-sharing group 0 is already referenced by another subarray")
            continue
        if row["circuit"]["inverters"] != group["inverters"] or len(row["shared_inverter_lines"]) != 1:
            raise ValueError("Power-sharing members must reference only the selected shared parent")
        start, end = arrays[identifier]
        root = next(block for block in objects("".join(lines[start:end + 1])) if not block.path)
        orientation = root.one("NoOrientation")[1]
        if plane is not None and plane != orientation:
            raise ValueError("Power-sharing members require the same fixed-plane orientation")
        plane = orientation
        if root.one("NStrOrient1")[1] != root.one("NStringCh")[1] or any(
                key.startswith("NStrOrient") and key != "NStrOrient1" and root.one(key)[1] != "0" for key in root.fields):
            raise ValueError("Power sharing requires all member strings in one orientation")
        flags = root.one("Flags")[1]
        value = int(flags[1:], 16)
        value = value | 0x100 if enabled else value & ~0x100
        for key, rendered in (("Flags", f"${value:0{len(flags) - 1}X}"), ("PowerSharingInverterId", "0" if enabled else "-1")):
            index = start + root.one(key)[0]
            edits[index] = replace_value(lines[index], rendered)
    for index, value in edits.items():
        lines[index] = value
    if inventory(lines, arrays)["counts_valid"] is not True:
        raise ValueError("Power-sharing settings failed structure validation")
    return {"subarray_ids": members, "inverter_line": group["line"],
            "enabled_before": group["power_sharing_id"] == 0, "enabled_after": enabled,
            "scope": "existing group 0 automatic sharing; no topology or nominal-power refitting"}


def resize_capacity(lines, arrays, subarray_id, target_inverters=None, target_strings=None, target_mppt=None):
    """Scale ordinary groups or explicit independent inputs with integral allocation."""
    before = inventory(lines, arrays)
    if before["counts_valid"] is not True:
        raise ValueError("Capacity editing requires consistent, fully checked grid counts")
    start, end = arrays[subarray_id]
    root = next(block for block in objects("".join(lines[start:end + 1])) if not block.path)
    positive_integer(root.one("NoOrientation")[1], "NoOrientation")
    flags = root.one("Flags")[1] if "Flags" in root.fields else ""
    independent = target_mppt is not None or ((target_strings is not None or target_inverters is not None)
        and re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) and bool(int(flags[1:], 16) & 0x400))
    inverter_scaling = independent and target_inverters is not None
    shared_line = None
    if independent:
        if (target_mppt is not None and (target_inverters is not None or target_strings is not None)
                or inverter_scaling and target_strings is not None):
            raise ValueError("Independent NInverter/NInvMPPT/NStringCh edits must be separate")
        versions = ("8.1.0", "8.1.6")
        if ("Version" not in root.fields or root.one("Version")[1] not in versions
                or not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) or not int(flags[1:], 16) & 0x400):
            field = "NInverter" if inverter_scaling else "NStringCh" if target_strings is not None else "NInvMPPT"
            raise ValueError(f"{field} editing requires verified Version={'/'.join(versions)} independent $0400 mode")
        row = next(row for row in before["subarrays"] if row["id"] == subarray_id)
        if row["shared_inverter_lines"]:
            groups = [group for group in before["inverter_groups"] if subarray_id in group.get("subarray_ids", [])]
            if (target_mppt is not None or inverter_scaling or len(groups) != 1 or len(row["shared_inverter_lines"]) != 1
                    or row["circuit"]["inverters"] != groups[0]["inverters"]):
                raise ValueError("Shared-parent capacity editing supports member strings only")
            shared_line = groups[0]["line"]
            for identifier, bounds in arrays.items():
                peer = next(block for block in objects("".join(lines[bounds[0]:bounds[1] + 1])) if not block.path)
                if identifier in groups[0]["subarray_ids"]:
                    if (peer.one("NoOrientation")[1] != root.one("NoOrientation")[1]
                            or peer.one("NStrOrient1")[1] != peer.one("NStringCh")[1]
                            or any(key.startswith("NStrOrient") and key != "NStrOrient1" and peer.one(key)[1] != "0" for key in peer.fields)):
                        raise ValueError("Shared member strings require one common orientation")
                elif groups[0]["power_sharing_id"] == 0 and "PowerSharingInverterId" in peer.fields and peer.one("PowerSharingInverterId")[1] == "0":
                    raise ValueError("Shared-parent group 0 is referenced outside its members")
        if ("PowerSharingInverterId" not in root.fields or root.one("PowerSharingInverterId")[1]
                not in (("-1", "0") if shared_line is not None else ("-1",))):
            raise ValueError("NInvMPPT editing requires explicit unshared PowerSharingInverterId=-1")
        if row["explicit_mppt_inputs_match_declared"] is not True:
            raise ValueError("NInvMPPT editing requires complete exclusive explicit MPPT allocation")
    else:
        require_ordinary_mppt(root)
    if shared_line is None and "PowerSharingInverterId" in root.fields and root.one("PowerSharingInverterId")[1] != "-1":
        raise ValueError("Shared inverter power is not supported for capacity editing")
    quantity = "NInvMPPT" if independent and not inverter_scaling else "NInverter"
    old = positive_integer(root.one(quantity)[1], quantity)
    strings = positive_integer(root.one("NStringCh")[1], "NStringCh")
    if root.one("NStrOrient1")[1] != str(strings) or any(
            key.startswith("NStrOrient") and key != "NStrOrient1" and root.one(key)[1] != "0"
            for key in root.fields):
        raise ValueError("Capacity editing requires all strings in one orientation")
    edits = {}
    target = (old if target_mppt is None else target_mppt) if independent and not inverter_scaling else old if target_inverters is None else target_inverters
    scaled_inputs = None
    if inverter_scaling:
        scaled_inputs, remainder = divmod(positive_integer(root.one("NInvMPPT")[1], "NInvMPPT") * target, old)
        if remainder or not 1 <= scaled_inputs <= MAX_COUNT:
            raise ValueError("Target must preserve integral independent input allocation")
    if target_strings is None:
        scaled_strings, remainder = divmod(strings * target, old)
    else:
        scaled_strings, remainder = target_strings, 0
    if remainder or not 1 <= scaled_strings <= MAX_COUNT:
        raise ValueError("Target must preserve integral string/group allocation")

    def walk(node):
        yield node
        for child in node.children:
            yield from walk(child)

    def scale_node(node, numerator, denominator):
        current = positive_integer(node.one("NElements", "1"), f"{node.kind} NElements")
        number, remainder = divmod(current * numerator, denominator)
        if remainder or not 1 <= number <= MAX_COUNT:
            raise ValueError("Target must preserve integral string/group allocation")
        if number != current:
            locations = node.field_lines.get("NElements", [])
            if len(locations) != 1:
                raise ValueError(f"Capacity editing requires explicit {node.kind} NElements")
            edits[locations[0]] = replace_value(lines[locations[0]], str(number))
        return {"line": node.line, "before": current, "after": number}

    selected, string_groups, mppt_groups = [], [], []
    for node in (item for root_node in circuit_nodes(lines) for item in walk(root_node)):
        if node.kind != "InverterNode" or (node.line != shared_line if shared_line is not None else node.one("SubArrayId") != str(subarray_id)):
            continue
        if independent:
            if not node.children or any(child.kind != "MPPTNode" for child in node.children):
                raise ValueError("NInvMPPT editing requires direct explicit MPPT children")
            selected.append(scale_node(node, target, old) if inverter_scaling else scale_node(node, 1, 1))
            for port in node.children:
                if shared_line is not None and port.one("SubArrayId") != str(subarray_id):
                    continue
                mppt_groups.append(scale_node(port, 1, 1) if inverter_scaling else scale_node(port, target, old))
                if target_strings is not None:
                    for leaf in port.children:
                        string_groups.append(scale_node(leaf, scaled_strings, strings))
        else:
            if any(item.kind == "MPPTNode" for item in walk(node)):
                raise ValueError("Explicit MPPT branches require separate mode-aware capacity editing")
            selected.append(scale_node(node, target, old))
            for string_node in node.children:
                string_groups.append(scale_node(string_node, scaled_strings * old, strings * target))
    if not selected:
        raise ValueError("Subarray has no supported exclusive inverter groups")
    quantities = [(quantity, target), ("NStringCh", scaled_strings), ("NStrOrient1", scaled_strings)]
    if inverter_scaling:
        quantities.append(("NInvMPPT", scaled_inputs))
    for key, number in quantities:
        index = start + root.one(key)[0]
        edits[index] = replace_value(lines[index], str(number))
    for index, line in edits.items():
        lines[index] = line
    after = inventory(lines, arrays)
    if after["counts_valid"] is not True:
        raise ValueError("Scaled grid counts failed validation")
    result = {"subarray_id": subarray_id,
              "inverters_before": positive_integer(root.one("NInverter")[1], "NInverter"),
              "inverters_after": positive_integer(root.one("NInverter")[1], "NInverter") if independent and not inverter_scaling else target,
              "strings_before": strings, "strings_after": scaled_strings,
              "groups": selected, "string_groups": string_groups,
              "scope": "proportional serialized allocation; not electrical sizing or loss refitting"}
    if independent:
        result.update(mppt_inputs_before=positive_integer(root.one("NInvMPPT")[1], "NInvMPPT"),
                      mppt_inputs_after=scaled_inputs if inverter_scaling else target, mppt_groups=mppt_groups)
    if shared_line is not None:
        result["shared_parent_line"] = shared_line
    return result

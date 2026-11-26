"""Inspect grid counts, synchronize inverter/string edits, and manage subarray topology."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from pvsyst_auto.components.editor import ASSIGNMENT, objects, replace_value, root_block
from pvsyst_auto.components.store import FIELD
from pvsyst_auto.versions import NATIVE_TEXT_OR_SEED, SEED_LABEL

NODE = re.compile(r"([A-Za-z][A-Za-z0-9_]*Node) (Start|End);")
KINDS = {"InjectionPointNode", "TransformerNode", "InverterNode", "MPPTNode", "StringNode"}
CHILDREN = {"InjectionPointNode": {"TransformerNode", "InverterNode"}, "TransformerNode": {"TransformerNode", "InverterNode"},
            "InverterNode": {"MPPTNode", "StringNode"}, "MPPTNode": {"StringNode"}, "StringNode": set()}
MAX_COUNT = 2147483647
TRANSFORMER_KEYS = ("OutputVoltage", "STCRefPower", "STCIronLossFrac", "STCIronLoss", "STCCopperLossFrac", "NightDisconnect", "IsHVTransformer")

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

def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)

def positive_integer(value, context):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value) or not 1 <= int(value) <= MAX_COUNT:
        raise ValueError(f"{context} must be a positive 32-bit integer")
    return int(value)

def _require_single_orient_strings(root, msg: str, *, expected: str | None = None) -> None:
    target = expected if expected is not None else root.one("NStringCh")[1]
    if root.one("NStrOrient1")[1] != target or any(k.startswith("NStrOrient") and k != "NStrOrient1" and root.one(k)[1] != "0" for k in root.fields):
        raise ValueError(msg)

def _encode_candidate(data: bytes, lines: list[str]) -> bytes:
    return (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")

def circuit_nodes(lines):
    """Parse serialized pvCircuit node tree; CableDefinition/metadata records never become node properties."""
    starts = [i for i, ln in enumerate(lines) if ln.strip() == "PVObject_SystemCircuit=pvCircuit"]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == "End of PVObject pvCircuit"]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError("Expected one closed PVObject_SystemCircuit=pvCircuit")
    roots, stack, count = [], [], 0
    for index in range(starts[0] + 1, ends[0]):
        value = lines[index].strip()
        if marker := NODE.fullmatch(value):
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
            if stack and not ((stack[-1][0] == "BaseNode" and len(stack) == 1)
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
            key, val = match[2], match[4]
            if key in ("NElements", "SubArrayId", "SubArrayName"):
                if stack and isinstance(stack[-1], Node):
                    stack[-1].fields.setdefault(key, []).append(val)
                    stack[-1].field_lines.setdefault(key, []).append(index)
                elif not stack or stack[-1][0] in ("Children", "BaseNode"):
                    raise ValueError(f"Circuit node field outside a node at line {index + 1}")
            elif val == "Start":
                stack.append((key, index))
            elif val == "End":
                if not stack or not isinstance(stack[-1], tuple) or stack[-1][0] != key:
                    raise ValueError(f"Unmatched circuit record {key} at line {index + 1}")
                stack.pop()
            elif stack and isinstance(stack[-1], Node):
                stack[-1].fields.setdefault(key, []).append(val)
                stack[-1].field_lines.setdefault(key, []).append(index)
        if len(stack) > 64:
            raise UnsupportedCircuit("Circuit nesting exceeds 64 supported levels")
    if stack:
        raise ValueError("Unclosed circuit node or record")
    if not roots:
        raise ValueError("Grid circuit has no nodes")
    return roots

def transformer_state(lines):
    """Read serialized MV/HV transformer layers."""
    result = {"layers": [], "count": 0, "nested": False, "issues": []}
    try:
        roots = circuit_nodes(lines)
    except (UnsupportedCircuit, ValueError) as exc:
        result["issues"].append(str(exc))
        return result
    found = []

    def walk_trf(node, depth):
        if node.kind == "TransformerNode":
            bad = [k for k in TRANSFORMER_KEYS if k not in node.fields or len(node.fields.get(k, [])) > 1]
            if bad:
                result["issues"].append(f"TransformerNode at line {node.line} is missing or repeats {', '.join(sorted(bad))}")
            else:
                found.append({"depth": depth, "fields": {k: node.one(k) for k in TRANSFORMER_KEYS}})
        for child in node.children:
            walk_trf(child, depth + 1 if node.kind == "TransformerNode" else depth)

    for root in roots:
        walk_trf(root, 0)
    result.update(layers=found, count=len(found), nested=any(layer["depth"] > 0 for layer in found))
    return result

def topology_state(lines):
    """Read the serialized native pvCircuit node tree."""
    result = {"present": False, "issues": [], "roots": []}
    if (not any(ln.strip() == "PVObject_SystemCircuit=pvCircuit" for ln in lines)
            and not any(ln.strip() == "End of PVObject pvCircuit" for ln in lines)):
        return result
    result["present"] = True
    try:
        roots = circuit_nodes(lines)
    except (UnsupportedCircuit, ValueError) as exc:
        result["issues"].append(str(exc))
        return result

    def node_state(node):
        fields = {k: list(v) for k, v in node.fields.items()}
        for key, vals in node.fields.items():
            if len(vals) > 1:
                result["issues"].append(f"Circuit line {node.line}: repeated {key}")
        sub_id, nel = node.fields.get("SubArrayId", []), node.fields.get("NElements", [])
        return {
            "kind": node.kind, "line": node.line,
            "subarray_id": {"present": bool(sub_id), "value": sub_id[0] if len(sub_id) == 1 else None},
            "nelements": {"present": bool(nel), "value": nel[0] if len(nel) == 1 else None},
            "fields": fields, "children": [node_state(c) for c in node.children],
        }

    result["roots"] = [node_state(n) for n in roots]
    return result

def inventory(lines, arrays):
    """Compare serialized inverter/string multiplicities with subarray declarations."""
    result = {"issues": [], "unchecked_checks": [], "inverter_groups": [], "subarrays": [], "totals": None, "counts_complete": False, "counts_valid": None}
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
            root = root_block(lines, (start, end))
            declared[identifier] = {k: root.one(k)[1] for k in (
                "Comment", "NModSerie", "NStringCh", "NStrOrient1", "NInverter", "NInvMPPT",
                "Version", "Flags", "PNomAC_InMPPT", "PowerSharingInverterId", "GInverter") if k in root.fields}
        except ValueError as exc:
            issues.append(f"Subarray {identifier}: {exc}")
            declared[identifier] = {}
    counts = {identifier: {"inverters": 0, "strings": 0, "explicit_mppt_inputs": 0} for identifier in arrays}
    owned, sharing_parents = set(), set()
    elements = lambda node: positive_integer(node.one("NElements", "1"), f"Circuit line {node.line}: NElements")

    def bounded(number):
        if number > MAX_COUNT:
            raise ValueError("Expanded circuit count exceeds a positive 32-bit integer")
        return number

    def branch(node):
        all_nodes = list(_walk(node))
        members = list(dict.fromkeys(child.one("SubArrayId") for child in node.children))
        shared = (node.one("SubArrayId") in (None, "0", "-1") and len(members) > 1
                  and all(c.kind == "MPPTNode" and c.children and c.one("SubArrayId") not in (None, "0", "-1")
                          and all(i.one("SubArrayId") == c.one("SubArrayId") for i in _walk(c)) for c in node.children))
        if shared:
            member_ids = [positive_integer(raw, f"Circuit line {node.line}: SubArrayId") for raw in members]
            configs = [declared.get(identifier, {}) for identifier in member_ids]
            gids, models = {c.get("PowerSharingInverterId") for c in configs}, {c.get("GInverter", "").casefold() for c in configs}
            raw_gid = next(iter(gids)) if len(gids) == 1 else None
            shared = (len(set(member_ids)) > 1 and len({c.get("Version") for c in configs}) == 1
                      and all(c.get("Version") in NATIVE_TEXT_OR_SEED for c in configs)
                      and all(re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", c.get("Flags", "")) and int(c["Flags"][1:], 16) & 0x400 for c in configs)
                      and len(models) == 1 and "" not in models and raw_gid is not None and (raw_gid == "-1" or raw_gid.isdigit()))
            if shared:
                group_id = int(raw_gid)
                if group_id >= 0 and group_id in sharing_parents:
                    raise UnsupportedCircuit("Multiple circuit parents for a power-sharing group are not checked")
                sharing_parents.add(group_id)
                if node.one("SubArrayName") != "; ".join(c.get("Comment", "") for c in configs):
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
        if any(i not in counts for i in ids):
            return
        group = {"line": node.line, "subarray_id": None if shared else next(iter(ids)), "inverters": elements(node), "strings": 0, "explicit_mppt_inputs": 0}
        if shared:
            group.update(subarray_ids=member_ids, power_sharing_id=group_id)
        allocations = {i: {"inverters": group["inverters"], "strings": 0, "explicit_mppt_inputs": 0} for i in ids}

        def visit(item, multiplier):
            expanded = bounded(multiplier * elements(item))
            ident = positive_integer(item.one("SubArrayId"), f"Circuit line {item.line}: SubArrayId")
            if key := ("strings" if item.kind == "StringNode" else "explicit_mppt_inputs" if item.kind == "MPPTNode" else None):
                group[key] = bounded(group[key] + expanded)
                allocations[ident][key] = bounded(allocations[ident][key] + expanded)
            for child in item.children:
                visit(child, expanded)

        for child in node.children:
            visit(child, group["inverters"])
        for ident, allocated in allocations.items():
            for key in counts[ident]:
                counts[ident][key] = bounded(counts[ident][key] + allocated[key])
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
    complete_circuit = not unchecked and not issues
    for identifier, values in declared.items():
        row = {
            "id": identifier, "declared": values, "circuit": counts[identifier] if complete_circuit else None,
            "circuit_module_count": None, "declared_module_count": None,
            "independent_mppt_inputs_declared": None, "explicit_mppt_inputs_match_declared": None,
            "shared_inverter_lines": [g["line"] for g in result["inverter_groups"] if identifier in g.get("subarray_ids", [])],
        }
        flags = values.get("Flags", "")
        if values.get("Version") in NATIVE_TEXT_OR_SEED and re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) and int(flags[1:], 16) & 0x400:
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
                num = positive_integer(values[key], f"Subarray {identifier}: {key}")
                if complete_circuit and count_key and num != counts[identifier][count_key]:
                    issues.append(f"Subarray {identifier}: {key}={num} differs from circuit {count_key}={counts[identifier][count_key]}")
                if key == "NModSerie" and complete_circuit:
                    row["circuit_module_count"] = bounded(num * counts[identifier]["strings"])
            except ValueError as exc:
                issues.append(str(exc))
        if "NModSerie" in values and "NStringCh" in values:
            try:
                row["declared_module_count"] = bounded(positive_integer(values["NModSerie"], "NModSerie") * positive_integer(values["NStringCh"], "NStringCh"))
            except ValueError:
                pass
        result["subarrays"].append(row)
    if complete_circuit:
        result["totals"] = {**{k: sum(r[k] for r in counts.values()) for k in ("strings", "explicit_mppt_inputs")},
                            "inverters": sum(g["inverters"] for g in result["inverter_groups"])}
    result["unchecked_checks"] = list(dict.fromkeys(unchecked))
    result["counts_complete"] = not unchecked
    result["counts_valid"] = False if issues else None if unchecked else True
    return result

def require_ordinary_mppt(root):
    if root.one("NInvMPPT")[1] != "1":
        raise ValueError("Editing requires ordinary NInvMPPT=1 mode")
    if "Flags" in root.fields:
        flags = root.one("Flags")[1]
        if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags):
            raise ValueError("Unsupported subarray Flags for ordinary editing")
        if int(flags[1:], 16) & 0x400:
            raise ValueError("Independent MPPT flag is not ordinary NInvMPPT=1 mode")

def _set_flag_bit(flags, bit, enabled):
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags):
        raise ValueError("Flags must be a hexadecimal value of at most 32 bits")
    value = (int(flags[1:], 16) | bit) if enabled else (int(flags[1:], 16) & ~bit)
    digits = format(value, f"0{len(flags) - 1}{'x' if flags[1:].islower() else 'X'}")
    old = flags[1:].rjust(len(digits), "0")
    return "$" + "".join(a if a.lower() == b.lower() else b for a, b in zip(old, digits))

def _need(cond, msg):
    if cond:
        raise ValueError(msg)

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

# --- Subarray branch topology, cloning, and removal ---
def subarray_branches(lines, arrays, identifier):
    start, end = arrays[identifier]
    root = next(b for b in objects("".join(lines[start:end + 1])) if not b.path)
    root.one("NoOrientation")
    if root.one("SubArrayId")[1] != str(identifier):
        raise ValueError("Subarray ID does not match its owning object")
    name = root.one("Comment")[1]
    for other_id, (first, last) in arrays.items():
        other = root if other_id == identifier else next(b for b in objects("".join(lines[first:last + 1])) if not b.path)
        if "PowerSharingInverterId" in other.fields and other.one("PowerSharingInverterId")[1] != "-1":
            raise ValueError("Shared inverter power is not supported for subarray editing")
    matches, owned = [], {start + root.one("SubArrayId")[0]}
    for node in (item for tree in circuit_nodes(lines) for item in _walk(tree)):
        if node.kind != "InverterNode":
            continue
        members = list(_walk(node))
        ids = [m.one("SubArrayId") for m in members]
        if str(identifier) not in ids:
            continue
        if any(v != str(identifier) for v in ids):
            raise ValueError("Subarray must own complete inverter branches without shared allocation")
        if any(m.one("SubArrayName") != name for m in members):
            raise ValueError("Circuit branch names do not match the subarray")
        owned.update(idx for m in members for idx in m.field_lines["SubArrayId"])
        matches.append((node.line - 1, node.end_line - 1, members))
    occurrences = {i for i, ln in enumerate(lines) if (f := FIELD.fullmatch(ln.rstrip("\r\n"))) and f[1] == "SubArrayId" and f[2] == str(identifier)}
    if not matches or not occurrences.issubset(owned):
        raise ValueError("Subarray ID is referenced outside its complete inverter branches")
    checked = inventory(lines, arrays)
    if any(r["id"] == identifier and r["explicit_mppt_inputs_match_declared"] is False for r in checked["subarrays"]):
        raise ValueError("Independent MPPT layout does not match its declared input count")
    if len(matches) > 1 and checked["counts_valid"] is not True:
        raise ValueError("Grouped subarray editing requires fully checked consistent grid counts")
    return matches, root

def require_plane_branch_mode(root, branches):
    flags = root.one("Flags")[1] if "Flags" in root.fields else ""
    if re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) and int(flags[1:], 16) & 0x400:
        if ("Version" not in root.fields or root.one("Version")[1] not in NATIVE_TEXT_OR_SEED
                or "PowerSharingInverterId" not in root.fields or root.one("PowerSharingInverterId")[1] != "-1"
                or any(not members[0].children or any(c.kind != "MPPTNode" for c in members[0].children) for _, _, members in branches)):
            raise ValueError("Plane editing requires verified exclusive explicit MPPT allocation")
    else:
        require_ordinary_mppt(root)
        if any(m.kind == "MPPTNode" for _, _, members in branches for m in members):
            raise ValueError("Plane editing requires ordinary branches without explicit MPPT")
    if any(set(m.fields) & {"NoOrientation", "NoOrient", "OrientationId"} for _, _, members in branches for m in members):
        raise ValueError("Circuit-level orientation references outside owned records require separate allocation handling")

def _validate_clone_subarray_args(source_subarray_id: int, clone_orientation: bool, share_inverter: bool) -> None:
    if not isinstance(source_subarray_id, int) or isinstance(source_subarray_id, bool):
        raise ValueError("source_subarray_id must be an integer")
    if not isinstance(clone_orientation, bool) or not isinstance(share_inverter, bool):
        raise ValueError("clone_orientation and share_inverter must be booleans")
    if share_inverter and clone_orientation:
        raise ValueError("Shared-parent cloning requires the existing common orientation")

def clone_subarray_data(store, data, source_subarray_id, *, clone_orientation=False, share_inverter=False, reserved_ids=()):
    """Build one cloned subarray in memory without reading or writing workspace files."""
    from pvsyst_auto.native.headless import strip_result_cache
    _validate_clone_subarray_args(source_subarray_id, clone_orientation, share_inverter)
    _, lines = store._text(data)
    arrays, system = store._sections(lines)
    if source_subarray_id not in arrays or system is None:
        raise ValueError("Source subarray and grid system are required")
    st = store._field_indices(lines, system, "SystemType")
    if len(st) != 1 or FIELD.fullmatch(lines[st[0]].rstrip("\r\n"))[2] != "Grid":
        raise ValueError("Only a grid system is supported")
    store._require_no_geometry(lines, require_shading_section=True)
    bounds = arrays[source_subarray_id]
    orient_f = store._field_indices(lines, bounds, "NoOrientation")
    if len(orient_f) != 1 or not (orientation_id := FIELD.fullmatch(lines[orient_f[0]].rstrip("\r\n"))[2]).isdigit():
        raise ValueError("Source subarray requires one orientation" if len(orient_f) != 1 else "Source orientation is invalid")
    orientation = store._orientation(lines, int(orientation_id))
    ft = store._field_indices(lines, orientation, "FieldType")
    if len(ft) != 1 or FIELD.fullmatch(lines[ft[0]].rstrip("\r\n"))[2] != "FixedPlane":
        raise ValueError("Only a fixed-plane orientation is supported")
    matches, root = subarray_branches(lines, arrays, source_subarray_id)
    if share_inverter:
        flags = root.one("Flags")[1]
        if (root.one("Version")[1] not in NATIVE_TEXT_OR_SEED or not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags)
                or not int(flags[1:], 16) & 0x400 or root.one("NInverter")[1] != "1" or root.one("NInvMPPT")[1] != "1"
                or root.one("PowerSharingInverterId")[1] != "-1" or inventory(lines, arrays)["counts_valid"] is not True or len(matches) != 1):
            raise ValueError("Shared-parent cloning requires one verified independent unshared input/inverter")
        inv = matches[0][2][0]
        if (positive_integer(inv.one("NElements", "1"), "inverter multiplicity") != 1 or len(inv.children) != 1
                or inv.children[0].kind != "MPPTNode" or positive_integer(inv.children[0].one("NElements", "1"), "MPPT multiplicity") != 1):
            raise ValueError("Shared-parent cloning requires one direct explicit MPPT input")
        _require_single_orient_strings(root, "Shared-parent cloning requires all strings in one orientation")
    new_id = max(set(arrays) | set(reserved_ids)) + 1
    if new_id > MAX_COUNT:
        raise ValueError("No available subarray ID")
    new_name, new_array = f"Sub-array #{new_id}", lines[bounds[0]:bounds[1] + 1].copy()
    for key, val in (("SubArrayId", str(new_id)), ("Comment", new_name)):
        new_array[root.one(key)[0]] = replace_value(new_array[root.one(key)[0]], val)
    new_orientation_id, insertions = int(orientation_id), [(bounds[1] + 1, new_array)]
    if clone_orientation:
        require_plane_branch_mode(root, matches)
        if inventory(lines, arrays)["counts_valid"] is not True:
            raise ValueError("Independent orientation cloning requires complete consistent grid counts")
        _require_single_orient_strings(root, "Independent orientation cloning requires all strings in one orientation")
        store._ordinary_fixed_plane(lines, orientation)
        header, groups = store._orientation_list(lines)
        if [gid for _, _, gid in groups] != list(range(1, len(groups) + 1)):
            raise ValueError("Independent orientation cloning requires sequential ordered IDs")
        new_orientation_id = len(groups) + 1
        if new_orientation_id > MAX_COUNT:
            raise ValueError("No available orientation ID")
        new_plane = lines[orientation[0]:orientation[1] + 1].copy()
        pidx = store._field_indices(lines, orientation, "NoOrient")[0] - orientation[0]
        new_plane[pidx] = replace_value(new_plane[pidx], str(new_orientation_id))
        aidx = root.one("NoOrientation")[0]
        new_array[aidx] = replace_value(new_array[aidx], str(new_orientation_id))
        lines[header] = re.sub(r"(?<==)[0-9]+", str(new_orientation_id), lines[header], count=1)
        insertions.append((groups[-1][1] + 1, new_plane))
    for start, end, members in matches:
        if share_inverter:
            inv, port = members[0], members[0].children[0]
            start, end, members = port.line - 1, port.end_line - 1, members[1:]
            sid_ln, snm_ln = inv.field_lines["SubArrayId"][0], inv.field_lines["SubArrayName"][0]
            lines[sid_ln] = replace_value(lines[sid_ln], "0")
            lines[snm_ln] = replace_value(lines[snm_ln], root.one("Comment")[1] + "; " + new_name)
        branch = lines[start:end + 1].copy()
        for member in members:
            for key, val in (("SubArrayId", str(new_id)), ("SubArrayName", new_name)):
                idx = member.field_lines[key][0] - start
                branch[idx] = replace_value(branch[idx], val)
        insertions.append((end + 1, branch))
    for offset, block in sorted(insertions, key=lambda item: item[0], reverse=True):
        lines[offset:offset] = block
    lines, _ = strip_result_cache(lines)
    candidate = _encode_candidate(data, lines)
    checked_arrays, _ = store._sections(checked := store._text(candidate)[1])
    if new_id not in checked_arrays or len(checked_arrays) != len(arrays) + 1:
        raise ValueError("Subarray structure validation failed")
    if share_inverter:
        inv_res = inventory(checked, checked_arrays)
        groups = [g for g in inv_res["inverter_groups"] if g.get("subarray_ids") == [source_subarray_id, new_id]]
        if inv_res["counts_valid"] is not True or len(groups) != 1:
            raise ValueError("Shared-parent clone failed complete allocation validation")
    elif len(subarray_branches(checked, checked_arrays, new_id)[0]) != len(matches):
        raise ValueError("Subarray inverter groups were not completely copied")
    if clone_orientation:
        store._orientation_list(checked)
        store._orientation(checked, new_orientation_id)
    return candidate, {"subarray_id": new_id, "orientation_id": new_orientation_id, "orientation_cloned": clone_orientation,
                       "inverter_group_count": len(matches), "shared_parent_created": share_inverter}

def clone_subarray(store, project: str, variant: str, source_subarray_id: int, expected_sha256: str | None = None,
                   clone_orientation: bool = False, share_inverter: bool = False) -> dict:
    """Duplicate a grid subarray with all its exclusively owned inverter groups."""
    _validate_clone_subarray_args(source_subarray_id, clone_orientation, share_inverter)
    with store.lock:
        store._check_project(project)
        path = store._path(project, variant)
        data = store._read(path)
        store._check_hash(data, expected_sha256)
        reserved_ids = set()
        for item in store._root().iterdir():
            if item.stem.casefold() == project[:-4].casefold() and re.fullmatch(r"\.VC[A-Za-z0-9]+", item.suffix, re.I):
                reserved_ids.update(store._sections(store._text(store._read(store._path(project, item.suffix[1:])))[1])[0])
        candidate, details = clone_subarray_data(store, data, source_subarray_id, clone_orientation=clone_orientation,
                                                 share_inverter=share_inverter, reserved_ids=reserved_ids)
        backup = store._replace_checked(path, data, candidate, prepare=lambda: store._backup(path, data))
        return {"project": project, "variant": variant.upper(), **details, "backup_name": backup, "sha256": hashlib.sha256(candidate).hexdigest()}

def remove_subarray(store, project: str, variant: str, subarray_id: int, expected_sha256: str | None = None,
                    remove_orientation: bool = False, shared_member: bool = False) -> dict:
    """Remove a subarray and its owned groups, retaining an orientation still in use."""
    from pvsyst_auto.native.headless import strip_result_cache
    _need(not isinstance(subarray_id, int) or isinstance(subarray_id, bool), "subarray_id must be an integer")
    _need(not isinstance(remove_orientation, bool) or not isinstance(shared_member, bool), "remove_orientation and shared_member must be booleans")
    _need(shared_member and remove_orientation, "Shared-member deletion preserves the common orientation")
    with store.lock:
        store._check_project(project)
        path = store._path(project, variant)
        data = store._read(path)
        store._check_hash(data, expected_sha256)
        _, lines = store._text(data)
        arrays, system = store._sections(lines)
        _need(subarray_id not in arrays or len(arrays) < 2 or system is None, "A target and another remaining subarray are required")
        kinds = store._field_indices(lines, system, "SystemType")
        _need(len(kinds) != 1 or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid", "Only grid subarrays are supported")
        store._require_no_geometry(lines, require_shading_section=True)
        bounds = arrays[subarray_id]
        no_orient = store._field_indices(lines, bounds, "NoOrientation")
        _need(len(no_orient) != 1, "Subarray needs one orientation")
        orientation = FIELD.fullmatch(lines[no_orient[0]].rstrip("\r\n"))[2]
        _need(not orientation.isdigit() or not 1 <= int(orientation) <= MAX_COUNT, "Orientation ID must be a positive 32-bit integer")
        shared = any(other != subarray_id and any((raw := FIELD.fullmatch(lines[i].rstrip("\r\n"))[2]).isdigit() and int(raw) == int(orientation)
                                                 for i in store._field_indices(lines, ob, "NoOrientation"))
                     for other, ob in arrays.items())
        _need(shared and remove_orientation, "Orientation is still referenced by another subarray")
        _need(not shared and not remove_orientation, "Orientation must remain in use by another subarray")
        remaining_member = None
        if shared_member:
            checked = inventory(lines, arrays)
            groups = [g for g in checked["inverter_groups"] if subarray_id in g.get("subarray_ids", [])]
            _need(checked["counts_valid"] is not True or len(groups) != 1 or len(groups[0]["subarray_ids"]) != 2
                  or groups[0]["power_sharing_id"] != -1 or groups[0]["inverters"] != 1,
                  "Shared-member deletion requires one complete disabled two-member parent")
            member_ids, roots = groups[0]["subarray_ids"], {}
            for ident, (first, last) in arrays.items():
                peer = next(b for b in objects("".join(lines[first:last + 1])) if not b.path)
                _need("PowerSharingInverterId" in peer.fields and peer.one("PowerSharingInverterId")[1] != "-1",
                      "Disable all power sharing before shared-member deletion")
                if ident in member_ids:
                    row = next(r for r in checked["subarrays"] if r["id"] == ident)
                    _need(peer.one("NInverter")[1] != "1" or peer.one("NInvMPPT")[1] != "1" or peer.one("NoOrientation")[1] != orientation
                          or row["circuit"]["inverters"] != 1 or len(row["shared_inverter_lines"]) != 1,
                          "Shared-member deletion requires single-input members in a common plane")
                    _require_single_orient_strings(peer, "Shared-member deletion requires single-input members in a common plane")
                    roots[ident] = peer
            types = store._field_indices(lines, store._orientation(lines, int(orientation)), "FieldType")
            _need(len(types) != 1 or FIELD.fullmatch(lines[types[0]].rstrip("\r\n"))[2] != "FixedPlane", "Shared-member deletion requires a fixed plane")
            parent = next(n for tree in circuit_nodes(lines) for n in _walk(tree) if n.line == groups[0]["line"])
            _need(parent.one("SubArrayId") != "0", "Shared-member deletion requires explicit parent owner 0")
            port = next((n for n in parent.children if n.one("SubArrayId") == str(subarray_id)), None)
            _need(port is None, "Shared-member deletion requires a direct port with the exact SubArrayId")
            owned = {bounds[0] + roots[subarray_id].one("SubArrayId")[0]} | {idx for n in _walk(port) for idx in n.field_lines["SubArrayId"]}
            refs = {i for i, ln in enumerate(lines) if (f := FIELD.fullmatch(ln.rstrip("\r\n"))) and f[1] == "SubArrayId" and f[2] == str(subarray_id)}
            _need(not refs.issubset(owned), "Shared member has references outside its owned subtree")
            remaining_member = next(i for i in member_ids if i != subarray_id)
            for key, val in (("SubArrayId", str(remaining_member)), ("SubArrayName", roots[remaining_member].one("Comment")[1])):
                idx = parent.field_lines[key][0]
                lines[idx] = replace_value(lines[idx], val)
            root, matches = roots[subarray_id], [(port.line - 1, port.end_line - 1, list(_walk(port)))]
        else:
            matches, root = subarray_branches(lines, arrays, subarray_id)
        deletions, orientation_id_map = [bounds] + [(s, e) for s, e, _ in matches], {}
        if remove_orientation:
            _need(inventory(lines, arrays)["counts_valid"] is not True, "Orientation removal requires complete consistent grid counts")
            header, groups = store._orientation_list(lines)
            _need(len(groups) < 2, "Another orientation must remain")
            _need([gid for _, _, gid in groups] != list(range(1, len(groups) + 1)), "Orientation removal requires sequential ordered IDs")
            plane = store._orientation(lines, int(orientation))
            fields = store._field_indices(lines, plane, "FieldType")
            _need(len(fields) != 1 or FIELD.fullmatch(lines[fields[0]].rstrip("\r\n"))[2] != "FixedPlane", "Only a fixed-plane orientation can be removed")
            _need([i for i, ln in enumerate(lines) if (f := FIELD.fullmatch(ln.rstrip("\r\n"))) and f[1] == "NoOrientation"
                   and f[2].isdigit() and int(f[2]) == int(orientation)] != [bounds[0] + root.one("NoOrientation")[0]],
                  "Orientation has references outside its owning subarray")
            owned_refs = set()
            for first, last, gid in groups:
                store._ordinary_fixed_plane(lines, (first, last))
                owned_refs.add(store._field_indices(lines, (first, last), "NoOrient")[0])
                if gid > int(orientation):
                    orientation_id_map[str(gid)] = gid - 1
            for ident, (first, last) in arrays.items():
                array_root = root if ident == subarray_id else next(b for b in objects("".join(lines[first:last + 1])) if not b.path)
                require_plane_branch_mode(array_root, subarray_branches(lines, arrays, ident)[0])
                _require_single_orient_strings(array_root, "Orientation removal requires all strings in one orientation")
                owned_refs.add(first + array_root.one("NoOrientation")[0])
            for idx, ln in enumerate(lines):
                if (f := FIELD.fullmatch(ln.rstrip("\r\n"))) and (f[1].startswith("NoOrient") or f[1] == "OrientationId") and idx not in owned_refs:
                    raise ValueError("Orientation removal has references outside owned records")
            for idx in owned_refs:
                if str(int(raw := FIELD.fullmatch(lines[idx].rstrip("\r\n"))[2])) in orientation_id_map:
                    lines[idx] = replace_value(lines[idx], str(orientation_id_map[str(int(raw))]))
            lines[header] = re.sub(r"(?<==)[0-9]+", str(len(groups) - 1), lines[header], count=1)
            deletions.append(plane)
        for first, last in sorted(deletions, reverse=True):
            del lines[first:last + 1]
        lines, _ = strip_result_cache(lines)
        candidate = _encode_candidate(data, lines)
        checked_arrays, _ = store._sections(checked := store._text(candidate)[1])
        _need(subarray_id in checked_arrays or len(checked_arrays) != len(arrays) - 1, "Subarray removal failed validation")
        if shared_member:
            subarray_branches(checked, checked_arrays, remaining_member)
        _need((len(matches) > 1 or remove_orientation or shared_member) and inventory(checked, checked_arrays)["counts_valid"] is not True,
              "Remaining grouped grid counts failed validation")
        if remove_orientation:
            store._orientation_list(checked)
        backup = store._replace_checked(path, data, candidate, prepare=lambda: store._backup(path, data))
        return {"project": project, "variant": variant.upper(), "removed_subarray_id": subarray_id,
                "removed_orientation_id": int(orientation) if remove_orientation else None,
                "orientation_id_map": orientation_id_map, "remaining_member_id": remaining_member,
                "inverter_group_count": len(matches), "backup_name": backup, "sha256": hashlib.sha256(candidate).hexdigest()}

def validate_structure(store, project: str, variant: str) -> dict:
    """Check cross-object references inside a variant."""
    with store.lock:
        store._check_project(project)
        arrays, system = store._sections(lines := store._text(data := store._read(store._path(project, variant)))[1])
        issues, warnings, orientation_ids = [], [], set()
        for index, line in enumerate(lines):
            if line.strip() not in ("PVObject_=pvOrient", "PVObject_=pvDomeOrient"):
                continue
            closing = "End of TOrientGroup" if line.strip() == "PVObject_=pvOrient" else "End of TDomeOrientGroup"
            if (end := next((i for i in range(index + 1, len(lines)) if lines[i].strip() == closing), None)) is None:
                issues.append("Unclosed orientation")
            elif len(identifiers := store._field_indices(lines, (index, end), "NoOrient")) != 1:
                issues.append("Orientation needs one NoOrient")
            elif not (raw := FIELD.fullmatch(lines[identifiers[0]].rstrip("\r\n"))[2]).isdigit() or int(raw) in orientation_ids:
                issues.append(f"Duplicate or invalid orientation ID: {raw}")
            else:
                orientation_ids.add(int(raw))
        list_headers, list_unchecked = [ln.strip() for ln in lines if ln.strip().startswith("Orientations, list of=")], []
        if list_headers:
            if any(not ln.endswith(" TOrientGroup") for ln in list_headers) or any(ln.strip() == "PVObject_=pvDomeOrient" for ln in lines):
                list_unchecked.append("orientation list counts for dome/unknown list types")
            else:
                try:
                    store._orientation_list(lines)
                except ValueError as exc:
                    issues.append(str(exc))
        for ident, bounds in arrays.items():
            if len(indices := store._field_indices(lines, bounds, "NoOrientation")) != 1:
                issues.append(f"Subarray {ident} needs one NoOrientation")
            elif not (raw := FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]).isdigit() or int(raw) not in orientation_ids:
                issues.append(f"Subarray {ident} references unknown orientation {raw}")
        system_type, unchecked, circuit_res = None, list_unchecked, None
        if system is None:
            issues.append("Missing pvSystem section")
            unchecked.append("system circuit references")
        elif len(kinds := store._field_indices(lines, system, "SystemType")) != 1:
            issues.append("System needs exactly one SystemType")
            unchecked.append("system circuit references")
        else:
            if not (system_type := FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2]):
                issues.append("SystemType must not be empty")
            if system_type == "Grid":
                circuit_res = inventory(lines, arrays)
                issues.extend(circuit_res["issues"])
                unchecked.extend(circuit_res["unchecked_checks"])
            else:
                unchecked.append("system circuit references")
                warnings.append(f"Circuit checks are not implemented for SystemType={system_type!r}")
        if any("PVObject_ShdTable" in ln or "ListeObjets, list of=" in ln for ln in lines):
            warnings.append("Shading geometry and derived factor tables require simulation verification")
        return {"project": project, "variant": variant.upper(), "sha256": hashlib.sha256(data).hexdigest(),
                "valid": False if issues else None if unchecked else True, "checks_complete": not unchecked,
                "unchecked_checks": unchecked, "system_type": system_type, "grid_circuit": circuit_res,
                "issues": issues, "warnings": warnings, "subarray_ids": sorted(arrays), "orientation_ids": sorted(orientation_ids)}

grid_inventory = inventory

def __getattr__(name: str):
    if name == "strip_result_cache":
        from pvsyst_auto.native.headless import strip_result_cache
        globals()[name] = strip_result_cache
        return strip_result_cache
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

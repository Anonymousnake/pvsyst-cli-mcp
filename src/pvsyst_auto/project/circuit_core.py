"""Core serialized pvCircuit node parser, inventory validation, and flag helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pvsyst_auto.components.editor import ASSIGNMENT, objects, root_block
from pvsyst_auto.components.store import FIELD
from pvsyst_auto.validation import NATIVE_TEXT_OR_SEED

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

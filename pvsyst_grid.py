"""Read serialized grid circuits without treating their counts as simulation truth."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pvsyst_component_editor import ASSIGNMENT, objects

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
        if len(stack) > 64:
            raise UnsupportedCircuit("Circuit nesting exceeds 64 supported levels")
    if stack:
        raise ValueError("Unclosed circuit node or record")
    if not roots:
        raise ValueError("Grid circuit has no nodes")
    return roots


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
            for key in ("Comment", "NModSerie", "NStringCh", "NStrOrient1", "NInverter", "NInvMPPT"):
                if key in root.fields:
                    values[key] = root.one(key)[1]
            declared[identifier] = values
        except ValueError as exc:
            issues.append(f"Subarray {identifier}: {exc}")
            declared[identifier] = {}

    counts = {identifier: {"inverters": 0, "strings": 0, "explicit_mppt_inputs": 0}
              for identifier in arrays}
    owned = set()

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
        ids = set()
        for item in all_nodes:
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
        if len(ids) != 1 or any(item.one("SubArrayId") in (None, "0", "-1") for item in all_nodes):
            unchecked.append(f"Circuit line {node.line}: shared inverter allocation counts")
            return
        identifier = next(iter(ids))
        if identifier not in counts:
            return
        group = {"line": node.line, "subarray_id": identifier, "inverters": elements(node),
                 "strings": 0, "explicit_mppt_inputs": 0}

        def visit(item, multiplier):
            expanded = bounded(multiplier * elements(item))
            if item.kind == "StringNode":
                group["strings"] = bounded(group["strings"] + expanded)
            if item.kind == "MPPTNode":
                group["explicit_mppt_inputs"] = bounded(group["explicit_mppt_inputs"] + expanded)
            for child in item.children:
                visit(child, expanded)

        for child in node.children:
            visit(child, group["inverters"])
        for key in counts[identifier]:
            counts[identifier][key] = bounded(counts[identifier][key] + group[key])
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
               "circuit_module_count": None, "declared_module_count": None}
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
        result["totals"] = {key: sum(row[key] for row in counts.values()) for key in ("inverters", "strings", "explicit_mppt_inputs")}
    result["unchecked_checks"] = list(dict.fromkeys(unchecked))
    result["counts_complete"] = not unchecked
    result["counts_valid"] = False if issues else None if unchecked else True
    return result

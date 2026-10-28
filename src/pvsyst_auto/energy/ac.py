"""Native per-inverter AC cable text, never a resistance or line-loss calculator."""
from __future__ import annotations

import difflib
from decimal import Decimal
import re

from pvsyst_auto.components.editor import ASSIGNMENT, objects, replace_value
from pvsyst_auto.project.circuit import circuit_nodes, positive_integer
from pvsyst_auto.project.tracking import _decimal, _require_scope

AC_FLAG = 0x200000
AC_FIELDS = ("ACWireNoSect", "ACWireLength", "ACWireMetal")
METALS = ("Copper", "Aluminium")
SECTIONS = tuple("ISO60228_" + value for value in ("10", "16", "25", "35", "50", "70", "95", "120"))


def _length(raw):
    value = _decimal(str(raw), "AC length_m")
    try:
        stepped = value.quantize(Decimal("0.1"))
    except ArithmeticError:
        raise ValueError("AC length is outside the supported decimal range") from None
    if not 0 < value <= 100000 or value != stepped:
        raise ValueError("AC length must be positive, at most 100000 m and in exact 0.1 m steps (adapter domain)")
    return value


def _header_cable(lines, node):
    """Inspect only direct inverter metadata, stopping before its Children record."""
    stack, spans = [], []
    for index in range(node.line, node.end_line - 1):
        match = ASSIGNMENT.fullmatch(lines[index].rstrip("\r\n"))
        if not match:
            if lines[index].strip():
                raise ValueError("Unexpected inverter header record")
            continue
        key, value = match[2], match[4]
        if key == "Children" and value == "Start" and not stack:
            break
        if value == "Start":
            if key == "CableDefinition" and stack:
                raise ValueError("AC cable must be directly owned by its inverter")
            stack.append((key, index))
        elif value == "End":
            if not stack or stack[-1][0] != key:
                raise ValueError("Unmatched inverter metadata")
            name, start = stack.pop()
            if name == "CableDefinition":
                spans.append((start, index))
    if stack or len(spans) > 1:
        raise ValueError("Ambiguous or incomplete inverter AC cable")
    if not spans:
        return None
    start, end = spans[0]
    tokens = [line.strip() for line in lines[start:end + 1]]
    if (len(tokens) != 8 or tokens[0] != "CableDefinition=Start" or tokens[1] != "CustomResistance=False"
            or tokens[3] != "CableType=Start" or tokens[6:] != ["CableType=End", "CableDefinition=End"]):
        raise ValueError("Only the recorded native noncustom AC cable shape is supported")
    length = re.fullmatch(r"Length=(.+) m", tokens[2])
    metal = re.fullmatch(r"Material=(\w+)", tokens[4])
    section = re.fullmatch(r"StdCrossSect=(\w+)", tokens[5])
    if not length or not metal or not section or metal[1] not in METALS or section[1] not in SECTIONS:
        raise ValueError("Unknown AC cable units, conductor or section")
    return {"length": _length(length[1]), "metal": metal[1], "section": section[1], "start": start, "end": end}


def _layout(lines):
    blocks = objects("".join(lines))
    mains = [block for block in blocks if block.object_type == "pvMainArray"]
    subs = [block for block in blocks if block.object_type == "pvSubArray"]
    circuits = [block for block in blocks if block.object_type == "pvCircuit"]
    if (len(mains) != 1 or mains[0].path != "PVObject_PVMainArray" or not mains[0].end_line
            or len(subs) != 1 or not subs[0].end_line or subs[0].path != "PVObject_PVMainArray/PVObject_"
            or len(circuits) != 1
            or circuits[0].path != "PVObject_PVMainArray/PVObject_SystemCircuit" or not circuits[0].end_line):
        raise ValueError("Requires one complete owning main array, subarray and native circuit")
    main, sub = mains[0], subs[0]
    raw_flags = main.one("Flags")[1]
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw_flags):
        raise ValueError("Main array flags must be native hex")
    active = bool(int(raw_flags[1:], 16) & AC_FLAG)
    nodes, transformers = [], []

    def walk(node):
        if node.kind == "InverterNode":
            nodes.append(node)
        if node.kind == "TransformerNode":
            transformers.append(node)
        for child in node.children:
            walk(child)

    for root in circuit_nodes(lines):
        walk(root)
    if not nodes:
        raise ValueError("Requires native inverter nodes")
    cables = [_header_cable(lines, node) for node in nodes]
    declared = [name in main.fields for name in AC_FIELDS]
    if active:
        if not all(declared) or any(cable is None for cable in cables):
            raise ValueError("Enabled AC requires all main and per-inverter cable declarations")
        length = _length(sub.one("ACWireLength")[1])
        metal = main.one("ACWireMetal")[1]
        if main.one("ACWireNoSect")[1] != "0" or main.one("ACWireLength")[1] != "0.0":
            raise ValueError("Only the recorded per-inverter AC summary form is supported")
        section = cables[0]["section"]
        if any((cable["length"], cable["metal"], cable["section"]) != (length, metal, section) for cable in cables):
            raise ValueError("Source AC inverter cables and subarray summary must agree; no mixed override")
    else:
        if any(declared) or any(cable is not None for cable in cables) or _decimal(sub.one("ACWireLength")[1], "ACWireLength") != 0:
            raise ValueError("Inactive AC must not carry partial or dormant cable declarations")
        length, metal, section = None, None, None
    state = {"mode": "active" if active else "none", "length_m": float(length) if active else None,
             "metal": metal, "section": section, "inverter_groups": len(nodes),
             "inverter_count": sum(positive_integer(node.one("NElements"), "inverter count") for node in nodes)}
    return state, main, sub, nodes, cables, transformers, circuits[0]


def ac_cabling_state(lines):
    try:
        return _layout(lines)[0]
    except (ValueError, TypeError, KeyError, StopIteration) as exc:
        return {"mode": "unsupported", "issues": [str(exc)]}


def _profile(store, lines):
    arrays, system = store._sections(lines)
    _require_scope(store, lines, arrays, system, main_flags=("$01900400", "$01B00400"), system_flags=("$00",))
    store._ordinary_fixed_plane(lines, store._orientation(lines, 1))
    layout = _layout(lines)
    if layout[5] or layout[1].one("MVNbTransfos")[1] != "0":
        raise ValueError("AC edits with MV/HV references are outside the verified profile")
    if layout[6].one("Version")[1] != "8.1.6" or layout[6].one("Flags")[1] != "$00":
        raise ValueError("Requires native 8.1.6 circuit Flags=$00")
    return layout


def _cable_text(indent, length, metal, section, newline):
    # Only native serialization; PVsyst derives the cable's electrical quantities.
    return [indent + line + newline for line in (
        "CableDefinition=Start", "    CustomResistance=False", f"    Length={format(length.normalize(), 'f')} m",
        "    CableType=Start", f"        Material={metal}", f"        StdCrossSect={section}",
        "    CableType=End", "CableDefinition=End")]


def create_ac_cabling_variant(store, *, project, source_variant, new_variant, ac,
                               dry_run=True, variant_name=None, add_utf8_bom=False):
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run, "issues": [], "warnings": [],
              "artifacts": {}, "variant_created": False, "native_consumption_verified": False}
    with store.lock:
        try:
            if type(dry_run) is not bool or type(add_utf8_bom) is not bool:
                raise ValueError("Use strict booleans")
            if not isinstance(ac, dict) or set(ac) - {"length_m", "metal", "section"} or "length_m" not in ac:
                raise ValueError("Declare AC length_m and optional metal/section only")
            if type(ac["length_m"]) is bool or not isinstance(ac["length_m"], (int, float)):
                raise ValueError("AC length_m must be a strict finite number")
            length = _length(ac["length_m"])
            store._check_project(project)
            source, target = store._path(project, source_variant), store._path(project, new_variant)
            if any(path.name.casefold() == target.name.casefold() for path in target.parent.iterdir()):
                raise FileExistsError("Choose a new variant: " + target.name)
            original = store._read(source)
            text, lines = store._text(original)
            state, main, sub, nodes, cables, _, _ = _profile(store, lines)
            metal = ac.get("metal", state["metal"] if state["mode"] == "active" else "Copper")
            section = ac.get("section", state["section"] if state["mode"] == "active" else "ISO60228_10")
            if metal not in METALS or section not in SECTIONS:
                raise ValueError("Use recorded native metal and standard section identifiers")
            newline = "\r\n" if "\r\n" in text else "\n"
            edits = []

            def replace(index, value):
                edits.append((index, index + 1, [replace_value(lines[index], value)]))

            replace(main.one("Flags")[0], "$01B00400")
            replace(sub.one("ACWireLength")[0], f"{length:.1f}")
            if state["mode"] == "active":
                replace(main.one("ACWireMetal")[0], metal)
            else:
                index = main.one("UFAverEffic")[0] + 1
                indent = re.match(r"\s*", lines[index - 1]).group(0)
                edits.append((index, index, [f"{indent}{key}={value}{newline}" for key, value in
                                            (("ACWireNoSect", "0"), ("ACWireLength", "0.0"), ("ACWireMetal", metal))]))
            for node, cable in zip(nodes, cables):
                anchor, = node.field_lines["NElements"]
                indent = re.match(r"[ \t]*", lines[anchor]).group(0)
                replacement = _cable_text(indent, length, metal, section, newline)
                start, end = (cable["start"], cable["end"] + 1) if cable else (anchor + 1, anchor + 1)
                edits.append((start, end, replacement))
            candidate_lines = list(lines)
            for start, end, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
                candidate_lines[start:end] = replacement
            bom = b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") or add_utf8_bom else b""
            if variant_name is not None:
                store._validate_display_name(variant_name, "variant_name")
                bom = store._display_name_prefix(original, variant_name, add_utf8_bom, "variant_name")
                index, = store._display_name_fields(candidate_lines, complete=True)
                result["name_change"] = {"before": candidate_lines[index].partition("=")[2].strip(), "after": variant_name}
                candidate_lines[index] = replace_value(candidate_lines[index], variant_name)
            candidate_text = "".join(candidate_lines)
            candidate = bom + candidate_text.encode("utf-8")
            _, checked = store._text(candidate)
            verified = _profile(store, checked)[0]
            expected = dict(state, mode="active", length_m=float(length), metal=metal, section=section)
            if verified != expected:
                raise ValueError("Candidate AC owners did not read back the declared values")
            diff = "".join(difflib.unified_diff(text.splitlines(keepends=True), candidate_text.splitlines(keepends=True)))
            result.update(status="ready", project=project, source_variant=source_variant, variant=target.suffix[1:].upper(),
                          ac=verified, artifacts={"variant": str(target)}, diff=diff[:32000], diff_truncated=len(diff) > 32000,
                          bom_added=bool(bom) and not original.startswith(b"\xef\xbb\xbf"))
            result["warnings"].append("Native AC cable metadata only; source, StringNode DC resistance, hardware and all unrelated records remain inherited. No resistance/loss calculation or MV/HV coupling is added.")
            if store._read(source) != original:
                raise ValueError("AC source changed before creation")
            if dry_run:
                return result
            result["stage"] = "write"
            with target.open("xb") as stream:
                stream.write(candidate)
            result.update(status="created", stage="created", variant_created=True)
        except (ValueError, OSError, KeyError, TypeError, StopIteration, ArithmeticError) as exc:
            result["issues"].append(str(exc))
            result["status"] = "failed" if result["stage"] == "write" else "blocked"
            if result["status"] == "failed":
                result["warnings"].append("A partial new artifact may remain; inspect its disclosed path. Source retained.")
    return result

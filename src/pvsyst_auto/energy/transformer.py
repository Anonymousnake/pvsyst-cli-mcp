"""Flip a recorded native MV night switch and its owning main-array dependency bit."""
from __future__ import annotations

import difflib

from pvsyst_auto.components.editor import objects, replace_value
from pvsyst_auto.project.circuit import circuit_nodes, TRANSFORMER_KEYS
from pvsyst_auto.project.tracking import _require_scope

NIGHT_DISCONNECT_FLAG = 0x20000000
MAIN_FLAGS = ("$01F00400", "$21F00400")
# Bounded native LOSS_CONTROLS.VCS/VCT profile, not engineering defaults or a formula.
MAIN_PROFILE = {"MVNbTransfos": "1", "VOutTransfo": "20000", "MVWireNoSect": "2",
                "MVWireLength": "0.0", "MVWireMetal": "Copper", "TrfIronLossFrac": "0.00100",
                "TrfResLossFrac": "0.01000"}
NODE_PROFILE = {"OutputVoltage": "20 kV", "STCRefPower": "431.47590",
                "STCIronLossFrac": "0.00083", "STCIronLoss": "0.36000",
                "STCCopperLossFrac": "0.01199", "IsHVTransformer": "False"}


def _profile(store, lines):
    arrays, system = store._sections(lines)
    _require_scope(store, lines, arrays, system, main_flags=MAIN_FLAGS, system_flags=("$00",))
    store._ordinary_fixed_plane(lines, store._orientation(lines, 1))
    blocks = objects("".join(lines))
    mains = [block for block in blocks if block.object_type == "pvMainArray"]
    circuits = [block for block in blocks if block.object_type == "pvCircuit"]
    if (len(mains) != 1 or mains[0].path != "PVObject_PVMainArray" or not mains[0].end_line
            or len(circuits) != 1 or not circuits[0].end_line
            or circuits[0].path != "PVObject_PVMainArray/PVObject_SystemCircuit"
            or circuits[0].one("Version")[1] != "8.1.6" or circuits[0].one("Flags")[1] != "$00"):
        raise ValueError("Requires one complete owning native main array and 8.1.6 circuit")
    main = mains[0]
    for name, value in MAIN_PROFILE.items():
        if main.one(name)[1] != value:
            raise ValueError(name + " must keep the recorded single-MV profile")
    transformers = []

    def walk(node):
        if node.kind == "TransformerNode":
            transformers.append(node)
        for child in node.children:
            walk(child)

    for root in circuit_nodes(lines):
        walk(root)
    if len(transformers) != 1:
        raise ValueError("Requires exactly one MV TransformerNode; no HV or multiple layers")
    node = transformers[0]
    if set(node.fields) != set(TRANSFORMER_KEYS):
        raise ValueError("TransformerNode fields must match the recorded native shape")
    for name in TRANSFORMER_KEYS:
        node.one(name)
    for name, value in NODE_PROFILE.items():
        if node.one(name) != value:
            raise ValueError(name + " must keep the recorded MV value")
    night = node.one("NightDisconnect")
    flags_index, flags = main.one("Flags")
    if night not in ("True", "False") or (night == "True") != bool(int(flags[1:], 16) & NIGHT_DISCONNECT_FLAG):
        raise ValueError("Source NightDisconnect and the owning main-array bit must agree")
    return flags_index, main, node


def create_mv_night_variant(store, *, project, source_variant, new_variant, night_disconnect,
                            dry_run=True, variant_name=None, add_utf8_bom=False):
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run, "issues": [],
              "warnings": [], "artifacts": {}, "variant_created": False,
              "native_consumption_verified": False}
    with store.lock:
        try:
            if any(type(value) is not bool for value in (night_disconnect, dry_run, add_utf8_bom)):
                raise ValueError("Use strict booleans")
            store._check_project(project)
            source, target = store._path(project, source_variant), store._path(project, new_variant)
            if any(path.name.casefold() == target.name.casefold() for path in target.parent.iterdir()):
                raise FileExistsError("Choose a new variant: " + target.name)
            original = store._read(source)
            text, lines = store._text(original)
            flags_index, main, node = _profile(store, lines)
            flags = int(main.one("Flags")[1][1:], 16)
            flags = flags | NIGHT_DISCONNECT_FLAG if night_disconnect else flags & ~NIGHT_DISCONNECT_FLAG
            candidate_lines = list(lines)
            candidate_lines[flags_index] = replace_value(candidate_lines[flags_index], f"${flags:08X}")
            index, = node.field_lines["NightDisconnect"]
            candidate_lines[index] = replace_value(candidate_lines[index], "True" if night_disconnect else "False")
            bom_prefix = b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") or add_utf8_bom else b""
            if variant_name is not None:
                store._validate_display_name(variant_name, "variant_name")
                index, = store._display_name_fields(candidate_lines, complete=True)
                before = candidate_lines[index].partition("=")[2].strip()
                candidate_lines[index] = replace_value(candidate_lines[index], variant_name)
                result["name_change"] = {"before": before, "after": variant_name}
            candidate_text = "".join(candidate_lines)
            # A BOM-less VC is read as Windows-1252, so a non-ASCII body forces the BOM too.
            if variant_name is not None:
                bom_prefix = store._display_name_prefix(original, variant_name, add_utf8_bom,
                                                        "variant_name", content=candidate_text)
            elif not candidate_text.isascii():
                bom_prefix = b"\xef\xbb\xbf"
            candidate = bom_prefix + candidate_text.encode("utf-8")
            _, checked = store._text(candidate)
            _, _, verified = _profile(store, checked)
            if verified.one("NightDisconnect") != ("True" if night_disconnect else "False"):
                raise ValueError("Candidate did not keep the requested night state")
            diff = "".join(difflib.unified_diff(text.splitlines(keepends=True), candidate_text.splitlines(keepends=True)))
            result.update(status="ready", project=project, source_variant=source_variant,
                          variant=target.suffix[1:].upper(), night_disconnect=night_disconnect,
                          artifacts={"variant": str(target)}, bom_added=bool(bom_prefix) and not original.startswith(b"\xef\xbb\xbf"),
                          diff=diff[:32000], diff_truncated=len(diff) > 32000)
            result["warnings"].append(
                "Only the existing MV NightDisconnect and main-array bit29 change, plus an optional root name. "
                "Single-MV reference power/losses/count/voltage, cables and all other fields stay inherited. "
                "No transformer calculation, HV construction or AC/DC cable editor is provided by this entry.")
            if store._read(source) != original:
                raise ValueError("MV source changed before creation")
            if dry_run:
                return result
            result["stage"] = "write"
            with target.open("xb") as stream:
                stream.write(candidate)
            result.update(status="created", stage="created", variant_created=True)
        except (ValueError, OSError, UnicodeError, KeyError, TypeError, StopIteration) as exc:
            result["issues"].append(str(exc))
            result["status"] = "failed" if result["stage"] == "write" else "blocked"
            if result["status"] == "failed":
                result["warnings"].append("New variant may contain a partial artifact; original source is preserved.")
    return result

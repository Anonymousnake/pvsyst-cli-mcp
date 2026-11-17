"""Flip a recorded native MV night switch and its owning main-array dependency bit."""
from __future__ import annotations

from pvsyst_auto.components.editor import objects, replace_value
from pvsyst_auto.project.circuit import circuit_nodes, TRANSFORMER_KEYS
from pvsyst_auto.project.tracking import _require_scope
from pvsyst_auto.project.variant_writer import run_new_variant_transaction

NIGHT_DISCONNECT_FLAG = 0x20000000
MAIN_FLAGS = ("$01F00400", "$21F00400")
# Single-MV transformer profile values matching LOSS_CONTROLS.VCS/VCT.
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
    def preflight():
        if type(night_disconnect) is not bool:
            raise ValueError("Use strict booleans")

    def build(*, lines, **_):
        flags_index, main, node = _profile(store, lines)
        flags = int(main.one("Flags")[1][1:], 16)
        flags = flags | NIGHT_DISCONNECT_FLAG if night_disconnect else flags & ~NIGHT_DISCONNECT_FLAG
        candidate_lines = list(lines)
        candidate_lines[flags_index] = replace_value(candidate_lines[flags_index], f"${flags:08X}")
        index, = node.field_lines["NightDisconnect"]
        candidate_lines[index] = replace_value(candidate_lines[index], "True" if night_disconnect else "False")

        def verify(checked, _, res):
            _, _, verified = _profile(store, checked)
            if verified.one("NightDisconnect") != ("True" if night_disconnect else "False"):
                raise ValueError("Candidate did not keep the requested night state")
            res["night_disconnect"] = night_disconnect
            res["warnings"].append(
                "Only the existing MV NightDisconnect and main-array bit29 change, plus an optional root name. "
                "Single-MV reference power/losses/count/voltage, cables and all other fields stay inherited. "
                "No transformer calculation, HV construction or AC/DC cable editor is provided by this entry.")

        return {"candidate_lines": candidate_lines, "verify": verify}

    return run_new_variant_transaction(
        store,
        project=project,
        source_variant=source_variant,
        new_variant=new_variant,
        dry_run=dry_run,
        add_utf8_bom=add_utf8_bom,
        variant_name=variant_name,
        preflight=preflight,
        build=build,
        source_changed_error="MV source changed before creation",
        write_failure_warning="New variant may contain a partial artifact; original source is preserved.",
    )

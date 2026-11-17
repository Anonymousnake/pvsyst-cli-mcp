"""Create a NEW VC that rebalances an existing verified two-face Dome string allocation."""
from __future__ import annotations

import re

from pvsyst_auto.components.editor import objects, replace_value, require_flat, root_block
from pvsyst_auto.project.circuit import inventory as grid_inventory
from pvsyst_auto.project.variant_writer import run_new_variant_transaction

ALLOCATION_MAX = 39
SUB_PROFILE = {"Version": "8.1.6", "Flags": "$019E1002", "Flags2": "$20", "SubArrayId": "1",
               "NoOrientation": "1", "NoOrientation2": "2", "NStringCh": "40", "NModSerie": "25",
               "NInverter": "6", "NInvMPPT": "1",
               "PVModule": "Generic_Mono_440W_Bifacial.PAN", "GInverter": "Generic_60kW.OND"}
DOME_KEYS = ("Comment", "Version", "Flags", "NoOrient", "FieldType", "FieldTilt", "FieldAzim",
             "BaseSlope", "UsesTiltAver", "UsesAzimAver", "UsesSlopeAver", "BTNoTrackers",
             "NoPairOrient", "IsMainOrient")
DOME_MAIN = {"Version": "8.1.6", "Flags": "$00", "NoOrient": "1", "FieldType": "Dome",
             "FieldTilt": "10.0", "FieldAzim": "90.0", "BaseSlope": "0.0",
             "UsesTiltAver": "False", "UsesAzimAver": "False", "UsesSlopeAver": "False",
             "BTNoTrackers": "-1,-1,", "NoPairOrient": "2", "IsMainOrient": "True"}
DOME_BACK = {"Version": "8.1.6", "Flags": "$00", "NoOrient": "2", "FieldType": "DomeBackfaces",
             "FieldTilt": "10.0", "FieldAzim": "-90.0", "BaseSlope": "0.0",
             "UsesTiltAver": "False", "UsesAzimAver": "False", "UsesSlopeAver": "False",
             "BTNoTrackers": "-1,-1,", "NoPairOrient": "1", "IsMainOrient": "False"}


def allocation(value, label):
    """Strict integer, bounded to the 40-string template."""
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        number = int(value)
    else:
        raise ValueError(f"{label} must be a strict integer")
    if not 1 <= number <= ALLOCATION_MAX:
        raise ValueError(f"{label} must be within 1..{ALLOCATION_MAX}")
    return number


def orientation_list(lines):
    """One counted two-record list must bracket the Dome records; objects ignores stray headers."""
    headers = [index for index, line in enumerate(lines) if re.match(r"\s*Orientations, list of=", line)]
    closers = [index for index, line in enumerate(lines) if line.strip() == "End of Orientations"]
    if len(headers) != 1 or len(closers) != 1:
        raise ValueError("Requires exactly one counted orientation list and one closing tag")
    if lines[headers[0]].strip() != "Orientations, list of=2 TOrientGroup":
        raise ValueError("The orientation list must declare exactly 2 TOrientGroup records")
    if headers[0] >= closers[0]:
        raise ValueError("Unordered orientation list")
    return headers[0], closers[0]


def dome_records(lines):
    """Return the paired main/backface records; anything else stops."""
    blocks = objects("".join(lines))
    header, closer = orientation_list(lines)
    records = [block for block in blocks if block.object_type == "pvDomeOrient"]
    if len(records) != 2:
        raise ValueError("Requires exactly two owning pvDomeOrient records")
    if any(block.object_type == "pvOrient" for block in blocks):
        raise ValueError("A plain fixed-plane orientation record is outside this batch")
    mapped = {}
    for block in records:
        if block.path != "PVObject_" or not block.end_line:
            raise ValueError("pvDomeOrient must be a complete directly owned record")
        if not header < block.line - 1 < block.end_line - 1 < closer:
            raise ValueError("A pvDomeOrient record lies outside the counted orientation list")
        if set(block.fields) != set(DOME_KEYS):
            raise ValueError("Unknown or missing fields in the pvDomeOrient record")
        for name in DOME_KEYS:
            block.one(name)  # every owning field must occur exactly once
        require_flat(block, lines)
        identified = block.one("NoOrient")[1]
        if identified not in ("1", "2") or identified in mapped:
            raise ValueError("Dome orientation IDs must be exactly 1 and 2")
        mapped[identified] = block
    main, back = mapped["1"], mapped["2"]
    for block, expected in ((main, DOME_MAIN), (back, DOME_BACK)):
        for name, value in expected.items():
            if block.one(name)[1] != value:
                raise ValueError(f"{name} must stay at its verified native value")
    return main, back


def _require_scope(store, lines, arrays, system):
    """Verified native 8.1.6 two-face Dome profile with one subarray sharing both orientations."""
    blocks = objects("".join(lines))
    root = next((block for block in blocks if not block.path), None)
    if root is None:
        raise ValueError("Project file has no root PVObject block")
    if root.one("Version")[1] != "8.1.6":
        raise ValueError("Requires a native 8.1.6 variant")
    flags = root.one("Flags")[1]
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) or int(flags[1:], 16) != 0:
        raise ValueError("Root Flags must be 0")
    if system is None or len(arrays) != 1:
        raise ValueError("Requires one pvSystem and one subarray")
    system_root = root_block(lines, system)
    if set(system_root.fields) != {"Flags", "SystemType"}:
        raise ValueError("pvSystem must declare only Flags and SystemType for this batch")
    if system_root.one("SystemType")[1] != "Grid" or system_root.one("Flags")[1] != "$06000000":
        raise ValueError("Requires SystemType=Grid with Flags=$06000000")
    require_flat(system_root, lines[system[0]:system[1] + 1])
    main_array = store._main_array(lines)
    if main_array is None:
        raise ValueError("A pvMainArray section is required")
    main_root = root_block(lines, main_array)
    if main_root.one("Flags")[1] != "$01900400" or main_root.one("Flags2")[1] != "$082A":
        raise ValueError("Main array requires the verified Flags=$01900400 and Flags2=$082A")
    bounds = next(iter(arrays.values()))
    subarrays = [block for block in blocks if block.object_type == "pvSubArray"]
    if len(subarrays) != 1:
        raise ValueError("Requires exactly one owning pvSubArray record")
    sub_root = subarrays[0]
    if sub_root.path != "PVObject_PVMainArray/PVObject_":
        raise ValueError("pvSubArray must be owned by the main array")
    if (sub_root.line - 1, sub_root.end_line - 1) != bounds:
        raise ValueError("pvSubArray bounds disagree with the parsed sections")
    for other in blocks:
        if other is sub_root:
            continue
        if "NStrOrient1" in other.fields:
            raise ValueError("NStrOrient1 must exist only in the owning pvSubArray record")
    for name, value in SUB_PROFILE.items():
        if sub_root.one(name)[1] != value:
            raise ValueError(f"{name} must stay at its verified native value")
    if any(key.startswith("NStrOrient") and key != "NStrOrient1" for key in sub_root.fields):
        raise ValueError("This profile declares only NStrOrient1; other allocation fields are outside it")
    current = allocation(sub_root.one("NStrOrient1")[1], "NStrOrient1")
    store._require_no_geometry(lines)
    if any(re.match(r"\s*(?:P50_P90|PVObject_Horizon)\s*=", line) for line in lines):
        raise ValueError("P50/P90 and saved horizon objects are outside this batch")
    if any(block.object_type in ("pvLoad", "pvGridStorage") for block in blocks):
        raise ValueError("Loads and storage are outside this batch")
    if grid_inventory(lines, arrays)["counts_valid"] is not True:
        raise ValueError("Requires complete consistent native grid counts")
    return sub_root, current


def create_dome_allocation_variant(store, *, project, source_variant, new_variant,
                                   first_orientation_strings, dry_run=True,
                                   variant_name=None, add_utf8_bom=False):
    """Preview or create a NEW VC only; every other field and the source stay untouched."""
    parsed = {}

    def preflight():
        if type(first_orientation_strings) is not int:
            raise ValueError("first_orientation_strings must be a strict integer")
        parsed["target_allocation"] = allocation(first_orientation_strings, "first_orientation_strings")

    def build(*, lines, original, **_):
        target_allocation = parsed["target_allocation"]
        arrays, system = store._sections(lines)
        sub_root, current = _require_scope(store, lines, arrays, system)
        dome_records(lines)
        index, _ = sub_root.one("NStrOrient1")
        candidate_lines = list(lines)
        candidate_lines[index] = replace_value(candidate_lines[index], str(target_allocation))

        def verify(checked, candidate, res):
            checked_arrays, checked_system = store._sections(checked)
            _, saved = _require_scope(store, checked, checked_arrays, checked_system)
            if saved != target_allocation:
                raise ValueError("Candidate did not keep the requested string allocation")
            dome_records(checked)
            res.update(
                allocation={"before": current, "after": target_allocation,
                            "total_strings": int(SUB_PROFILE["NStringCh"])},
                changed=candidate != original,
            )
            res["warnings"].append(
                "East and west string allocations must remain within 1..39 strings per face.")

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
        source_changed_error="Template changed while preparing the dome allocation",
    )

"""Create a NEW VC that switches the verified native PeakShaving discharge strategy."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from pvsyst_auto.components.editor import ASSIGNMENT, objects, replace_value, require_flat, root_block
from pvsyst_auto.components.generator import direct_fields
from pvsyst_auto.project.circuit import inventory as grid_inventory
from pvsyst_auto.energy.storage import storage_state, validate_storage
from pvsyst_auto.project.variant_writer import run_new_variant_transaction

EDITABLE = ("DischStrategy", "HourBegDisch", "HourEndDisch")
HOUR_LIMIT = 23
SYSTEM_TEXT = {"Flags": "$17000040", "SystemType": "Grid",
               "BatteryFile": "Universal_Adjustable_LI.BTR", "LimGrSeparLoss": "False",
               "TypeTempBatt": "1", "NBattSerie": "15", "NBattParall": "10"}
SYSTEM_NUMBER = {"PLimGrid": Decimal("200"), "SOHBattBeg": Decimal("1"),
                 "BattVoltage_Syst": Decimal("768"), "BattCapacity_Syst": Decimal("1000"),
                 "TempBattRef": Decimal("20")}
SYSTEM_MONTHS = "TempBattMois"
STORAGE_TEXT = {"Flags": "$00", "Strategy": "PeakShaving", "ChargingStrategy": "PVToGridMin"}
STORAGE_NUMBER = ("BattSoCInit", "SOCChDecl", "SOCDischDecl", "SOCDischSelf", "PMaxCharge", "PMaxDisch",
                  "BattRectMaxEffic", "BattRectEuroEffic", "BattInvMaxEffic", "BattInvEuroEffic")
WINDOW = "MinMaxHour"


def _decimal(text, label):
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a decimal number") from None
    if not value.is_finite():
        raise ValueError(f"{label} must be finite")
    return value


def discharge_values(discharge):
    """Check the two declared discharge modes and render the native tokens."""
    if not isinstance(discharge, dict):
        raise ValueError("discharge must be one explicit mode object")
    mode = discharge.get("mode")
    if mode == "after_sunset":
        if set(discharge) != {"mode"}:
            raise ValueError("after_sunset accepts only mode")
        return "AfterSunset", None
    if mode == "hour_window":
        if set(discharge) != {"mode", "start_hour", "end_hour"}:
            raise ValueError("hour_window accepts only mode, start_hour and end_hour")
        bounds = []
        for name in ("start_hour", "end_hour"):
            value = discharge[name]
            if type(value) is not int:
                raise ValueError(f"{name} must be a strict integer")
            if not 0 <= value <= HOUR_LIMIT:
                raise ValueError(f"{name} must be within 0..{HOUR_LIMIT}")
            bounds.append(value)
        if bounds[0] == bounds[1]:
            raise ValueError("start_hour and end_hour must differ")
        return WINDOW, (bounds[0], bounds[1])
    raise ValueError("discharge.mode must be after_sunset or hour_window")


def _hours(block, name):
    raw = block.one(name)[1]
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError(f"{name} must already be a bare integer in the source record")
    value = int(raw)
    if not 0 <= value <= HOUR_LIMIT:
        raise ValueError(f"{name} must already be within 0..{HOUR_LIMIT}")
    return value


def _require_profile(system_root):
    """Every owning pvSystem field must match the verified native storage profile exactly."""
    if set(system_root.fields) != set(SYSTEM_TEXT) | set(SYSTEM_NUMBER) | {SYSTEM_MONTHS}:
        raise ValueError("Unknown or missing fields in the pvSystem record")
    for name in SYSTEM_TEXT:
        if system_root.one(name)[1] != SYSTEM_TEXT[name]:
            raise ValueError(f"{name} must stay at its verified native value")
    for name, expected in SYSTEM_NUMBER.items():
        if _decimal(system_root.one(name)[1], name) != expected:
            raise ValueError(f"{name} must stay at its verified native value")
    months = system_root.one(SYSTEM_MONTHS)[1]
    if not months.endswith(","):
        raise ValueError(f"{SYSTEM_MONTHS} must keep its trailing comma")
    parts = months.split(",")[:-1]
    if len(parts) != 12 or any(_decimal(part, SYSTEM_MONTHS) != Decimal("20") for part in parts):
        raise ValueError(f"{SYSTEM_MONTHS} must stay twelve 20 values")


def peak_block(lines):
    """Return the one directly owned PeakShaving record, or stop on any unknown shape."""
    blocks = objects("".join(lines))
    records = [block for block in blocks if block.object_type == "pvGridStorage"]
    if len(records) != 1:
        raise ValueError("Requires exactly one pvGridStorage record")
    block = records[0]
    if block.path != "PVObject_GridStorage" or not block.end_line:
        raise ValueError("pvGridStorage must be the complete directly owned record")
    for other in blocks:
        if other is block:
            continue
        if set(other.fields) & set(EDITABLE):
            raise ValueError("Discharge controls must exist only in the owning pvGridStorage record")
    strategy = block.one("DischStrategy")[1]
    require_flat(block, lines)
    if strategy == "AfterSunset":
        expected = set(STORAGE_TEXT) | set(STORAGE_NUMBER) | {"DischStrategy"}
    elif strategy == WINDOW:
        expected = set(STORAGE_TEXT) | set(STORAGE_NUMBER) | set(EDITABLE)
        start, end = _hours(block, "HourBegDisch"), _hours(block, "HourEndDisch")
        if start == end:
            raise ValueError("The saved hour window endpoints must differ")
    else:
        raise ValueError(f"Only an existing PeakShaving discharge strategy is supported, not {strategy!r}")
    if set(block.fields) != expected:
        raise ValueError("Unknown or missing fields in the pvGridStorage record")
    for name in STORAGE_TEXT:
        if block.one(name)[1] != STORAGE_TEXT[name]:
            raise ValueError(f"{name} must stay at its verified native value")
    systems = [record for record in blocks if record.object_type == "pvSystem"]
    if len(systems) != 1 or not systems[0].end_line:
        raise ValueError("Requires one complete owning storage system")
    state, _, _ = storage_state(lines, (systems[0].line - 1, systems[0].end_line - 1))
    validate_storage(state, preserved=state)
    return block


def _require_scope(store, lines, arrays, system):
    """Verified native 8.1.6 PeakShaving profile: root bit19 without a load, storage system flags."""
    root = root_block(lines)
    if root.one("Version")[1] != "8.1.6":
        raise ValueError("Requires a native 8.1.6 variant")
    flags = root.one("Flags")[1]
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) or int(flags[1:], 16) != 0x00080000:
        raise ValueError("This PeakShaving profile requires root Flags=$00080000")
    if system is None or len(arrays) != 1:
        raise ValueError("Requires one pvSystem and one subarray")
    if any(block.object_type == "pvLoad" for block in objects("".join(lines))):
        raise ValueError("This PeakShaving profile has no load and does not add one")
    system_root = root_block(lines, system)
    _require_profile(system_root)
    require_flat(system_root, lines[system[0]:system[1] + 1])
    main = store._main_array(lines)
    if main is None:
        raise ValueError("A pvMainArray section is required")
    main_root = root_block(lines, main)
    if main_root.one("Flags")[1] != "$01900400" or main_root.one("Flags2")[1] != "$082A":
        raise ValueError("Main array requires the verified Flags=$01900400 and Flags2=$082A")
    sub_root = root_block(lines, next(iter(arrays.values())))
    if sub_root.one("Version")[1] != "8.1.6" or sub_root.one("Flags")[1] != "$019E1002":
        raise ValueError("Subarray requires the verified 8.1.6 declaration and 019E1002 flags")
    if sub_root.one("Flags2")[1] != "$20":
        raise ValueError("Subarray requires the verified Flags2=$20")
    strings = sub_root.one("NStringCh")[1]
    if (sub_root.one("NoOrientation")[1] != "1" or not strings.isdigit() or int(strings) < 1
            or sub_root.one("NStrOrient1")[1] != strings):
        raise ValueError("Requires all strings on orientation 1 with NStrOrient1 equal to NStringCh")
    if any(key.startswith("NStrOrient") and key != "NStrOrient1" and sub_root.one(key)[1] != "0"
           for key in sub_root.fields):
        raise ValueError("Every other NStrOrient* must be zero")
    store._orientation_list(lines)
    store._ordinary_fixed_plane(lines, store._orientation(lines, 1))
    store._require_no_geometry(lines)
    if any(re.match(r"\s*(?:P50_P90|PVObject_Horizon)\s*=", line) for line in lines):
        raise ValueError("P50/P90 and saved horizon objects are outside this batch")
    if grid_inventory(lines, arrays)["counts_valid"] is not True:
        raise ValueError("Requires complete consistent native grid counts")


def create_peak_shaving_variant(store, *, project, source_variant, new_variant, discharge,
                                dry_run=True, variant_name=None, add_utf8_bom=False):
    """Preview or create a NEW VC only; every other field and the source stay untouched."""
    def build(*, lines, **_):
        arrays, system = store._sections(lines)
        _require_scope(store, lines, arrays, system)
        strategy, window = discharge_values(discharge)
        block = peak_block(lines)
        index, _ = block.one("DischStrategy")
        ending = "\r\n" if lines[index].endswith("\r\n") else "\n"
        candidate_lines = list(lines)
        saved = [block.one(name)[0] for name in EDITABLE[1:] if name in block.fields]
        index -= sum(position < index for position in saved)
        for position in sorted(saved, reverse=True):
            del candidate_lines[position]
        candidate_lines[index] = replace_value(candidate_lines[index], strategy)
        if window is not None:
            indent = candidate_lines[index][:len(candidate_lines[index]) - len(candidate_lines[index].lstrip())]
            start, end = window
            candidate_lines[index + 1:index + 1] = [f"{indent}HourBegDisch={start}{ending}",
                                                    f"{indent}HourEndDisch={end}{ending}"]

        def verify(checked, _, res):
            checked_arrays, _ = store._sections(checked)
            verified = peak_block(checked)
            if len(checked_arrays) != 1 or verified.one("DischStrategy")[1] != strategy:
                raise ValueError("Candidate did not keep the requested discharge strategy")
            if window is None and any(name in verified.fields for name in EDITABLE[1:]):
                raise ValueError("after_sunset must not keep hour fields")
            if window is not None and (_hours(verified, "HourBegDisch"), _hours(verified, "HourEndDisch")) != window:
                raise ValueError("Candidate did not keep the requested hour window")
            res["discharge"] = {"mode": discharge["mode"], "strategy": strategy,
                                "start_hour": window[0] if window else None,
                                "end_hour": window[1] if window else None}

        return {"candidate_lines": candidate_lines, "verify": verify}

    return run_new_variant_transaction(
        store,
        project=project,
        source_variant=source_variant,
        new_variant=new_variant,
        dry_run=dry_run,
        add_utf8_bom=add_utf8_bom,
        variant_name=variant_name,
        build=build,
        source_changed_error="Template changed while preparing the peak-shaving variant",
    )


STORAGE_OWN = ("Flags", "Strategy", "ChargingStrategy", "DischStrategy", "HourBegDisch", "HourEndDisch",
               "BattSoCInit", "SOCChDecl", "SOCDischDecl", "SOCDischSelf", "PMaxCharge",
               "PMaxDisch", "BattRectMaxEffic", "BattRectEuroEffic", "BattInvMaxEffic",
               "BattInvEuroEffic")


def peak_shaving_state(lines):
    """Report the stored peak-shaving strategy and its native fields, or why it is absent.

    Values are returned as stored text: endpoint inclusivity, the meaning of AfterSunset
    beyond the native label, and every efficiency figure are not interpreted here.
    """
    # Same assignment grammar as objects()/direct_fields, so spacing around "=" is not "absent".
    owner = [i for i, line in enumerate(lines)
             if (match := ASSIGNMENT.fullmatch(line.rstrip("\r\n")))
             and (match[2], match[4]) == ("PVObject_GridStorage", "pvGridStorage")]
    if not owner:
        return {"mode": None, "reason": "no pvGridStorage section"}
    if len(owner) != 1:
        return {"mode": None, "issue": "Requires exactly one pvGridStorage record"}
    end = next((i for i in range(owner[0] + 1, len(lines))
                if lines[i].strip() == "End of PVObject pvGridStorage"), None)
    if end is None:
        return {"mode": None, "issue": "pvGridStorage is not closed"}
    try:
        owned = direct_fields(lines, (owner[0], end))
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}
    for key, entries in owned.items():
        if len(entries) != 1:
            return {"mode": None, "issue": f"Duplicate {key} in pvGridStorage"}
    fields = {key: entries[0][1] for key, entries in owned.items()}
    if "Strategy" not in fields:
        return {"mode": None, "issue": "pvGridStorage carries no Strategy"}
    if fields["Strategy"] != "PeakShaving":
        return {"mode": "other", "strategy": fields["Strategy"],
                "reason": "the stored strategy is not peak shaving"}
    # The two discharge modes carry different field sets: an AfterSunset save holds no
    # hour fields at all, matching the writer's own rule.
    expected = set(STORAGE_OWN)
    if fields.get("DischStrategy") == "AfterSunset":
        expected -= {"HourBegDisch", "HourEndDisch"}
    if set(fields) != expected:
        return {"mode": None,
                "issue": "pvGridStorage fields do not match the recorded native shape"}
    strategy = fields["DischStrategy"]
    if strategy == "AfterSunset":
        discharge = {"mode": "after_sunset"}
    elif strategy == WINDOW:
        discharge = {"mode": "hour_window", "start_hour": fields["HourBegDisch"],
                     "end_hour": fields["HourEndDisch"]}
    else:
        return {"mode": "other", "discharge_strategy": strategy,
                "reason": "unrecorded discharge strategy"}
    return {"mode": "configured", "discharge": discharge,
            "charging_strategy": fields["ChargingStrategy"],
            "values": {key: fields[key] for key in sorted(expected)}}

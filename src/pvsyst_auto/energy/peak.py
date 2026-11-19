"""Create a NEW VC that switches the verified native PeakShaving discharge strategy."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from pvsyst_auto.components.editor import ASSIGNMENT, objects, replace_value, require_flat, root_block
from pvsyst_auto.components.generator import direct_fields, one
from pvsyst_auto.energy.storage import PEAK_SHAVING_LOAD_ERROR, has_user_load, storage_state, validate_storage
from pvsyst_auto.native.headless import strip_result_cache
from pvsyst_auto.project.circuit import inventory as grid_inventory
from pvsyst_auto.project.variant_writer import run_new_variant_transaction
from pvsyst_auto.versions import NATIVE_TEXT_OR_SEED, SEED_LABEL

EDITABLE = ("DischStrategy", "HourBegDisch", "HourEndDisch")
HOUR_LIMIT = 23
STORAGE_CHARGING = frozenset({"PVToGridMin", "PVaboveThresh"})
STORAGE_NUMBER = (
    "BattSoCInit", "SOCChDecl", "SOCDischDecl", "SOCDischSelf",
    "PMaxCharge", "PMaxDisch",
    "BattRectMaxEffic", "BattRectEuroEffic", "BattInvMaxEffic", "BattInvEuroEffic",
)
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
    if block.one("Flags")[1] != "$00" or block.one("Strategy")[1] != "PeakShaving":
        raise ValueError("Source pvGridStorage must declare Strategy=PeakShaving and Flags=$00")
    if has_user_load(lines):
        raise ValueError(PEAK_SHAVING_LOAD_ERROR)
    charging = block.one("ChargingStrategy")[1]
    if charging not in STORAGE_CHARGING:
        raise ValueError(f"Unsupported PeakShaving ChargingStrategy {charging!r}")
    strategy = block.one("DischStrategy")[1]
    require_flat(block, lines)
    if strategy in ("AfterSunset", "Immediate"):
        expected = {"Flags", "Strategy", "ChargingStrategy", "DischStrategy"} | set(STORAGE_NUMBER)
    elif strategy == WINDOW:
        expected = {"Flags", "Strategy", "ChargingStrategy", *STORAGE_NUMBER, *EDITABLE}
        start, end = _hours(block, "HourBegDisch"), _hours(block, "HourEndDisch")
        if start == end:
            raise ValueError("The saved hour window endpoints must differ")
    else:
        raise ValueError(f"Only an existing PeakShaving discharge strategy is supported, not {strategy!r}")
    if set(block.fields) != expected:
        raise ValueError("Unknown or missing fields in the pvGridStorage record")
    systems = [record for record in blocks if record.object_type == "pvSystem"]
    if len(systems) != 1 or not systems[0].end_line:
        raise ValueError("Requires one complete owning storage system")
    state, _, _ = storage_state(lines, (systems[0].line - 1, systems[0].end_line - 1))
    validate_storage(state, preserved=state)
    return block


def _require_scope(store, lines, arrays, system):
    """Verified native PeakShaving profile: storage system + PLimGrid + valid grid counts."""
    root_block(lines)
    header_end = next((i for i, line in enumerate(lines[1:], 1) if line.strip().startswith("PVObject_")), len(lines))
    header = direct_fields(lines, (0, header_end))
    if one(header, "Version")[1] not in NATIVE_TEXT_OR_SEED:
        raise ValueError(f"Requires a {SEED_LABEL} variant")
    if system is None or not arrays:
        raise ValueError("Requires one pvSystem and at least one subarray")
    system_root = root_block(lines, system)
    sys_flags = system_root.one("Flags")[1]
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", sys_flags) or (int(sys_flags[1:], 16) & 0x10000040) != 0x10000040:
        raise ValueError("PeakShaving pvSystem requires storage enabling flags")
    plim = _decimal(system_root.one("PLimGrid")[1], "PLimGrid")
    if plim <= 0:
        raise ValueError("PLimGrid must be positive")
    main = store._main_array(lines)
    if main is None:
        raise ValueError("A pvMainArray section is required")
    store._orientation_list(lines)
    if any(re.match(r"\s*P50_P90\s*=", line) for line in lines):
        raise ValueError("P50/P90 objects are outside this batch")
    if grid_inventory(lines, arrays)["counts_complete"] is not True:
        raise ValueError("Requires complete native grid counts")


def _update_block(lines, block, strategy, hours):
    """Rewrite only DischStrategy and the optional HourBegDisch/HourEndDisch pair inside block."""
    newline = "\r\n" if any("\r\n" in line for line in lines) else "\n"
    disch_index, _ = block.one("DischStrategy")
    indent = ASSIGNMENT.fullmatch(lines[disch_index].rstrip("\r\n"))[1]
    updated = [replace_value(lines[disch_index], strategy) if index == disch_index else line
               for index, line in enumerate(lines)]
    existing = sorted((block.one(name)[0], name) for name in ("HourBegDisch", "HourEndDisch") if name in block.fields)
    if hours is None:
        for index, _ in reversed(existing):
            del updated[index]
        return updated
    values = {"HourBegDisch": str(hours[0]), "HourEndDisch": str(hours[1])}
    if len(existing) == 2:
        for index, name in existing:
            updated[index] = replace_value(updated[index], values[name])
        return updated
    if not existing:
        added = [f"{indent}{name}={values[name]}{newline}" for name in ("HourBegDisch", "HourEndDisch")]
        return updated[:disch_index + 1] + added + updated[disch_index + 1:]
    raise ValueError("PeakShaving record has an incomplete hour window")


def peak_shaving_state(lines):
    """Inspect the PeakShaving discharge state directly from pvGridStorage without throwing."""
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
    if fields.get("ChargingStrategy") not in STORAGE_CHARGING:
        return {"mode": "other", "charging_strategy": fields.get("ChargingStrategy"),
                "reason": "unsupported peak shaving charging strategy"}
    expected = {"Flags", "Strategy", "ChargingStrategy", "DischStrategy"} | set(STORAGE_NUMBER)
    strategy = fields.get("DischStrategy")
    if strategy == WINDOW:
        expected |= {"HourBegDisch", "HourEndDisch"}
    if set(fields) != expected:
        return {"mode": None,
                "issue": "pvGridStorage fields do not match the recorded native shape"}
    if strategy == "AfterSunset":
        discharge = {"mode": "after_sunset"}
    elif strategy == "Immediate":
        discharge = {"mode": "immediate"}
    elif strategy == WINDOW:
        discharge = {"mode": "hour_window", "start_hour": fields["HourBegDisch"],
                     "end_hour": fields["HourEndDisch"]}
    else:
        return {"mode": "other", "discharge_strategy": strategy,
                "reason": "unrecorded discharge strategy"}
    return {"mode": "configured", "discharge": discharge,
            "charging_strategy": fields["ChargingStrategy"],
            "values": {key: fields[key] for key in sorted(expected)}}


def create_peak_shaving_variant(store, *, project, source_variant, new_variant, discharge, dry_run=True, variant_name=None, add_utf8_bom=False):
    """Preview or write a NEW PeakShaving variant switching only DischStrategy."""
    parsed = {}

    def preflight():
        parsed["strategy"], parsed["hours"] = discharge_values(discharge)

    def build(*, text, lines, result, **_):
        arrays, system = store._sections(lines)
        _require_scope(store, lines, arrays, system)
        block = peak_block(lines)
        before = peak_shaving_state(lines)
        if before.get("mode") != "configured":
            raise ValueError("PeakShaving discharge configuration is unsupported")
        updated = _update_block(lines, block, parsed["strategy"], parsed["hours"])
        if any(line.strip().startswith("VarList,") for line in updated):
            strip_result_cache(updated)

        def verify(checked, _, res):
            checked_arrays, checked_system = store._sections(checked)
            _require_scope(store, checked, checked_arrays, checked_system)
            after = peak_shaving_state(checked)
            expected = {"mode": "after_sunset"} if parsed["strategy"] == "AfterSunset" else {
                "mode": "hour_window", "start_hour": str(parsed["hours"][0]), "end_hour": str(parsed["hours"][1])}
            if after.get("mode") != "configured" or after["discharge"] != expected:
                raise ValueError("Candidate PeakShaving state did not match request")
            res["peak_shaving"] = {"before": before["discharge"], "after": after["discharge"]}

        return {"candidate_text": "".join(updated), "verify": verify}

    return run_new_variant_transaction(
        store,
        project=project,
        source_variant=source_variant,
        new_variant=new_variant,
        dry_run=dry_run,
        variant_name=variant_name,
        add_utf8_bom=add_utf8_bom,
        bom_display_target=new_variant,
        preflight=preflight,
        build=build,
        initial_fields={"native_consumption_verified": False},
        exists_prefix="Choose a new PeakShaving variant: ",
        source_changed_error="Source variant changed before PeakShaving creation",
        write_failure_warning="Inspect the disclosed new PeakShaving variant before retrying; the source variant remains unchanged.",
        uppercase_variant=True,
    )

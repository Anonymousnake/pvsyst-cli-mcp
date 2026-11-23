"""Read-only inspection of native PeakShaving discharge state from pvGridStorage."""
from __future__ import annotations

from pvsyst_auto.components.editor import ASSIGNMENT
from pvsyst_auto.components.generator import direct_fields

STORAGE_CHARGING = frozenset({"PVToGridMin", "PVaboveThresh"})
STORAGE_NUMBER = (
    "BattSoCInit", "SOCChDecl", "SOCDischDecl", "SOCDischSelf",
    "PMaxCharge", "PMaxDisch",
    "BattRectMaxEffic", "BattRectEuroEffic", "BattInvMaxEffic", "BattInvEuroEffic",
)
WINDOW = "MinMaxHour"


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

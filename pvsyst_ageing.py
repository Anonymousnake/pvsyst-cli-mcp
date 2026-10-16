"""Native ageing model inputs only (simulation year + annual degradation).

Writes just NoYearSimul and ModuleDegradFactor inside an existing activated ageing template.
It never creates or removes the pvAgeing block, never edits the stored factors it reports, and
performs no yearly-factor or stochastic calculation. The accepted numeric range is an adapter
support envelope, not a native limit; a zero rate is a real value, not a disabled state.
"""
from __future__ import annotations

import re
from decimal import Decimal

from pvsyst_component_editor import ASSIGNMENT, objects, replace_value
from pvsyst_generator import finite, one

YEAR_FIELD = "NoYearSimul"
MEAN_FIELD = "ModuleDegradFactor"
YEAR_OBJECT = "pvAgeing"
YEAR_OBJECT_OWNER = "PVObject_Ageing"
MAIN_OBJECT = "pvMainArray"
STORED_LOSS = "DegradLossFactor"
STORED_MISMATCH = "DegradMismFactor"
AGE_KNOWN = frozenset((
    "Flags", "KeepMCValues", "PlotKind", "CurveWarranty", "ProgressMode", "NSigmaLimit",
    MEAN_FIELD, "ImpDegradSharing", "ModulesRMSYearISC", "ModulesRMSYearVoc",
    "ModuleYearWarr", "ModuleWarranty"))
AGE_FIXED = {"Flags": "$00", "KeepMCValues": "False", "ProgressMode": "1", "CurveWarranty": "1",
             "NSigmaLimit": "2.0", "ImpDegradSharing": "80.0", "ModulesRMSYearISC": "0.00",
             "ModulesRMSYearVoc": "0.00"}
MAIN_FLAGS2 = "$0010082A"
SCOPE = "Native ageing model inputs only; stored factors are reported, never written"


def _year(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("simulation_year must be a strict integer")
    if not 1 <= value <= 30:
        raise ValueError("simulation_year must stay inside the adapter domain 1..30")
    return value


def _percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("annual_module_degradation_percent must be a strict number")
    number = finite(value, "annual_module_degradation_percent", minimum=0, maximum=1)
    scaled = Decimal(str(number)) * 100
    if scaled != scaled.to_integral_value():
        raise ValueError("annual_module_degradation_percent must use 0.01 increments")
    return number


def _percent_native(raw):
    try:
        return _percent(float(raw))
    except (ValueError, OverflowError):
        raise ValueError("Require a finite native ModuleDegradFactor") from None


def _read(lines, bounds):
    """Parse the main-array slice with objects(): EndTags and nested lists are not fields here."""
    if not isinstance(bounds, tuple) or len(bounds) != 2:
        raise ValueError("Require one owning main-array bounds")
    shift = bounds[0]
    blocks = objects("".join(lines[shift:bounds[1] + 1]))
    roots = [block for block in blocks if not block.path]
    if len(roots) != 1 or roots[0].object_type != MAIN_OBJECT:
        raise ValueError("Require one owning pvMainArray root object")
    ages = [block for block in blocks if block.object_type == YEAR_OBJECT]
    if len(ages) != 1 or ages[0].path != YEAR_OBJECT_OWNER:
        raise ValueError("Require exactly one direct PVObject_Ageing block")
    root, age = roots[0], ages[0]
    header_end = next((i for i, line in enumerate(lines[1:], 1)
                       if line.strip().startswith("PVObject_")), shift)
    versions = [m[4] for m in (ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))
                               for i in range(1, header_end)) if m is not None and m[2] == "Version"]
    if len(versions) != 1 or versions[0] != "8.1.6":
        raise ValueError("Ageing model editing requires root Version=8.1.6")
    if one(root.fields, "Flags2")[1] != MAIN_FLAGS2:
        raise ValueError(f"Ageing model editing requires the verified main Flags2={MAIN_FLAGS2}")
    one(root.fields, "Flags")
    subs = [block for block in blocks if block.object_type == "pvSubArray"]
    if len(subs) != 1:
        raise ValueError("Require exactly one subarray block")
    if one(subs[0].fields, "Flags2")[1] != "$20":
        raise ValueError("Require subarray Flags2=20 for this verified template")
    if set(age.fields) != AGE_KNOWN:
        raise ValueError("Ageing template requires exactly the twelve verified fields")
    for key in AGE_KNOWN:
        value = one(age.fields, key)[1]
        if key in AGE_FIXED and value != AGE_FIXED[key]:
            raise ValueError(f"Ageing template requires {key}={AGE_FIXED[key]}")
    if not re.fullmatch(r"[0-9]+", one(age.fields, "PlotKind")[1]):
        raise ValueError("PlotKind must be a native integer")
    for key, owner in ((YEAR_FIELD, root.fields), (STORED_LOSS, root.fields),
                       (STORED_MISMATCH, root.fields), (MEAN_FIELD, age.fields)):
        total = sum(m is not None and m[2] == key for line in lines
                    for m in [ASSIGNMENT.fullmatch(line.rstrip("\r\n"))])
        if total != len(owner.get(key, [])):
            raise ValueError(f"Require {key} only in its owning object")
    year_raw = one(root.fields, YEAR_FIELD)[1]
    if not re.fullmatch(r"[0-9]+", year_raw):
        raise ValueError("Require a native integer NoYearSimul")
    stored = {}
    for key in (STORED_LOSS, STORED_MISMATCH):
        entry = one(root.fields, key)
        stored[key] = finite(float(entry[1]), key, minimum=0, maximum=100)
    if stored[STORED_MISMATCH] != 0:
        raise ValueError("This RMS0 template requires stored mismatch factor 0")
    state = {"mode": "template",
             "simulation_year": _year(int(year_raw)),
             "annual_module_degradation_percent": _percent_native(one(age.fields, MEAN_FIELD)[1]),
             "stored_loss_factor_percent": stored[STORED_LOSS],
             "stored_mismatch_factor_percent": stored[STORED_MISMATCH],
             "note": "stored factors are native metadata; native recomputes the applied loss"}
    return shift, root, age, state


def ageing_model_state(lines, bounds):
    """Model inputs plus stored metadata; an unusable template reports mode None and an issue."""
    try:
        return _read(lines, bounds)[3]
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}


def replace_ageing_model(lines, bounds, updates):
    """Validate everything on a candidate copy, then commit the copy once."""
    keys = {"simulation_year", "annual_module_degradation_percent"}
    if not isinstance(updates, dict) or set(updates) != keys:
        raise ValueError("Require exactly simulation_year and annual_module_degradation_percent")
    year = _year(updates["simulation_year"])
    percent = _percent(updates["annual_module_degradation_percent"])
    shift, root, age, before = _read(lines, bounds)
    if (before["simulation_year"], before["annual_module_degradation_percent"]) == (year, percent):
        return {"before": before, "after": before, "scope": SCOPE}
    candidate = list(lines)
    for entry, raw in ((one(root.fields, YEAR_FIELD), f"{year:d}"),
                       (one(age.fields, MEAN_FIELD), format(percent, ".2f"))):
        candidate[shift + entry[0]] = replace_value(candidate[shift + entry[0]], raw)
    after = _read(candidate, bounds)[3]
    lines[:] = candidate
    return {"before": before, "after": after, "scope": SCOPE}

"""Native ageing model inputs and the verified enable switch.

Writes NoYearSimul, ModuleDegradFactor and, through a separate entry point, only the
main-array ageing enable bit; the optional template inputs KeepMCValues and the RMS
ISC/VOC percentages stay inside the same verified field set. It never creates or removes
the pvAgeing block, never edits the stored factors it reports, and performs no
yearly-factor or stochastic calculation. The accepted numeric range is an adapter
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
KEEP_MC = "KeepMCValues"
RMS_ISC = "ModulesRMSYearISC"
RMS_VOC = "ModulesRMSYearVoc"
AGE_KNOWN = frozenset((
    "Flags", KEEP_MC, "PlotKind", "CurveWarranty", "ProgressMode", "NSigmaLimit",
    MEAN_FIELD, "ImpDegradSharing", RMS_ISC, RMS_VOC,
    "ModuleYearWarr", "ModuleWarranty"))
AGE_FIXED = {"Flags": "$00", "ProgressMode": "1", "CurveWarranty": "1",
             "NSigmaLimit": "2.0", "ImpDegradSharing": "80.0"}
MAIN_FLAGS2_ON = "$0010082A"
MAIN_FLAGS2_OFF = "$082A"
MAIN_FLAGS2_ENABLED_VALUE = 0x10082A
MAIN_FLAGS2_DISABLED_VALUE = 0x82A
MAIN_ENABLE_BIT = 0x100000
FLAGS2_RE = re.compile(r"\$[0-9A-Fa-f]{1,8}")
AGE_TEMPLATE_ORDER = ("Flags", KEEP_MC, "PlotKind", "CurveWarranty", "ProgressMode",
                      "NSigmaLimit", "ImpDegradSharing", RMS_ISC, RMS_VOC,
                      "ModuleYearWarr", "ModuleWarranty")
SCOPE = "Native ageing model inputs only; stored factors are reported, never written"
ENABLE_SCOPE = SCOPE + "; writes only the verified main-array ageing enable bit"
MODEL_KEYS = ("simulation_year", "annual_module_degradation_percent")
RMS_ISC_KEY = "modules_rms_year_isc_percent"
RMS_VOC_KEY = "modules_rms_year_voc_percent"
OPTIONAL_KEYS = ("keep_mc_values", RMS_ISC_KEY, RMS_VOC_KEY)


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


def _native_percent(value, label):
    """Adapter domain only: 0..100 with 0.01 increments; not a GUI boundary."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(label + " must be a strict number")
    number = finite(value, label, minimum=0, maximum=100)
    scaled = Decimal(str(number)) * 100
    if scaled != scaled.to_integral_value():
        raise ValueError(label + " must use 0.01 increments")
    return number


def _percent_native(raw):
    try:
        return _percent(float(raw))
    except (ValueError, OverflowError):
        raise ValueError("Require a finite native ModuleDegradFactor") from None


def _native_percent_raw(raw, label):
    if not re.fullmatch(r"[0-9]+\.[0-9]{2}", raw):
        raise ValueError("Require the recorded two-decimal native " + label)
    try:
        return _native_percent(float(raw), label)
    except (ValueError, OverflowError):
        raise ValueError("Require a finite native " + label) from None


def _flags2_state(raw):
    """Report the verified enable bit; equal value with other padding or case is accepted."""
    if not isinstance(raw, str) or not FLAGS2_RE.fullmatch(raw):
        raise ValueError("main Flags2 must be a hexadecimal $ value of 1..8 digits")
    value = int(raw[1:], 16)
    if value == MAIN_FLAGS2_ENABLED_VALUE:
        return True
    if value == MAIN_FLAGS2_DISABLED_VALUE:
        return False
    raise ValueError("Ageing editing requires the verified main Flags2=%s or %s"
                     % (MAIN_FLAGS2_ON, MAIN_FLAGS2_OFF))


def _read(lines, bounds, require_enabled=True):
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
    enabled = _flags2_state(one(root.fields, "Flags2")[1])
    if require_enabled and not enabled:
        raise ValueError("Ageing model editing requires the verified enabled template")
    one(root.fields, "Flags")
    subs = [block for block in blocks if block.object_type == "pvSubArray"]
    if len(subs) != 1:
        raise ValueError("Require exactly one subarray block")
    if one(subs[0].fields, "Flags2")[1] != "$20":
        raise ValueError("Require subarray Flags2=20 for this verified template")
    if set(age.fields) != AGE_KNOWN:
        raise ValueError("Ageing template requires exactly the twelve verified fields")
    for key in AGE_FIXED:
        value = one(age.fields, key)[1]
        if value != AGE_FIXED[key]:
            raise ValueError(f"Ageing template requires {key}={AGE_FIXED[key]}")
    if not re.fullmatch(r"[0-9]+", one(age.fields, "PlotKind")[1]):
        raise ValueError("PlotKind must be a native integer")
    keep_raw = one(age.fields, KEEP_MC)[1]
    if keep_raw not in ("True", "False"):
        raise ValueError("KeepMCValues must be a native boolean")
    for key in (RMS_ISC, RMS_VOC):
        _native_percent_raw(one(age.fields, key)[1], key)
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
        raise ValueError("The recorded template requires stored mismatch factor 0")
    state = {"mode": "template",
             "enabled": enabled,
             "simulation_year": _year(int(year_raw)),
             "annual_module_degradation_percent": _percent_native(one(age.fields, MEAN_FIELD)[1]),
             "stored_loss_factor_percent": stored[STORED_LOSS],
             "stored_mismatch_factor_percent": stored[STORED_MISMATCH],
             "template": {key: one(age.fields, key)[1] for key in AGE_TEMPLATE_ORDER},
             "note": "stored factors are native metadata; native recomputes the applied loss"}
    return shift, root, age, state


def ageing_model_state(lines, bounds):
    """Disabled templates stay readable; malformed shapes report mode None and an issue."""
    try:
        return _read(lines, bounds, require_enabled=False)[3]
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}


def _model_updates(updates):
    if not isinstance(updates, dict):
        raise ValueError("Require a mapping of ageing model inputs")
    extra = set(updates) - set(MODEL_KEYS) - set(OPTIONAL_KEYS)
    if extra or not set(MODEL_KEYS) <= set(updates):
        raise ValueError("Require exactly simulation_year and "
                         "annual_module_degradation_percent, with optional "
                         "keep_mc_values, " + RMS_ISC_KEY + " and " + RMS_VOC_KEY)
    if (RMS_ISC_KEY in updates) != (RMS_VOC_KEY in updates):
        raise ValueError(RMS_ISC_KEY + " and " + RMS_VOC_KEY
                         + " must be supplied together")
    plan = [(YEAR_FIELD, f"{_year(updates['simulation_year']):d}")]
    plan.append((MEAN_FIELD, format(_percent(updates["annual_module_degradation_percent"]), ".2f")))
    if "keep_mc_values" in updates:
        value = updates["keep_mc_values"]
        if not isinstance(value, bool):
            raise ValueError("keep_mc_values must be a strict boolean")
        plan.append((KEEP_MC, "True" if value else "False"))
    if RMS_ISC_KEY in updates:
        plan.append((RMS_ISC, format(_native_percent(updates[RMS_ISC_KEY], RMS_ISC), ".2f")))
        plan.append((RMS_VOC, format(_native_percent(updates[RMS_VOC_KEY], RMS_VOC), ".2f")))
    return plan


def replace_ageing_model(lines, bounds, updates):
    """Validate everything on a candidate copy, then commit the copy once."""
    plan = _model_updates(updates)
    shift, root, age, before = _read(lines, bounds)
    owner = {YEAR_FIELD: root.fields, MEAN_FIELD: age.fields, KEEP_MC: age.fields,
             RMS_ISC: age.fields, RMS_VOC: age.fields}
    if all(one(owner[key], key)[1] == raw for key, raw in plan):
        return {"before": before, "after": before, "scope": SCOPE}
    candidate = list(lines)
    for key, raw in plan:
        entry = one(owner[key], key)
        candidate[shift + entry[0]] = replace_value(candidate[shift + entry[0]], raw)
    after = _read(candidate, bounds)[3]
    lines[:] = candidate
    return {"before": before, "after": after, "scope": SCOPE}


def replace_ageing_enabled(lines, bounds, enabled):
    """Flip only the verified enable bit, after proving the same template reads back."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a strict boolean")
    shift, root, age, before = _read(lines, bounds, require_enabled=False)
    candidate = list(lines)
    entry = one(root.fields, "Flags2")
    value = int(entry[1][1:], 16)
    wanted = (value | MAIN_ENABLE_BIT) if enabled else (value & ~MAIN_ENABLE_BIT)
    if wanted != value:
        canonical = MAIN_FLAGS2_ON if enabled else MAIN_FLAGS2_OFF
        candidate[shift + entry[0]] = replace_value(candidate[shift + entry[0]], canonical)
    after = _read(candidate, bounds, require_enabled=False)[3]
    if after["enabled"] is not enabled:
        raise ValueError("The candidate template did not reach the requested enable state")
    lines[:] = candidate
    return {"before": before, "after": after, "scope": ENABLE_SCOPE}

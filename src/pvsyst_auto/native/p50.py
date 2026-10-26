"""Native 8.1.6 P50/P90 tag editor (P50_P90=TP50_P90); no probability calculation.

Edits only an existing root P50_P90 tag block inside a GUI-produced result template
(verified root Flags 0x01800001). It never creates or removes the block, and never
touches root Flags, caches or physical fields. The numeric domain checked here is an
adapter support envelope, not a native GUI or scientific limit.
"""
from __future__ import annotations

import re
from decimal import Decimal

from pvsyst_auto.components.editor import ASSIGNMENT, objects, replace_value
from pvsyst_auto.components.generator import finite

HEAD = "P50_P90=TP50_P90"
END = "EndTags"
PLOTS = "SimulPlots=Start"
ROOT_TYPE = "pvVCalcul"
ROOT_FLAGS = 0x01800001
# Read-only coverage: the block is identical across these root states, and the recorded
# variants carrying them complete a full native year, so refusing to report them hides
# real parameters. Writing still requires the result template below.
READ_FLAGS = (0x01800001,   # result template, as recorded by the verified writer
              0x01000000,   # enabled without the result bit
              0x00800001,   # result baseline without the enable bit
              0x00000000)   # block present but neither bit set, as in the VCI save
ORDER = ("MeteoKind", "P90Custom", "PxxCustom", "PxxReport", "PxxMainResults", "PlotKind",
         "YearDevFromAver", "MeteoAnnualVariab", "PVModelVariab", "InverterVariab",
         "SoilingVariab", "DegradVariab", "CustomVariab", "CustomVarLabel")
INTS = ("P90Custom", "PxxCustom")
BOOLS = ("PxxReport", "PxxMainResults")
VARIAB = ("MeteoAnnualVariab", "PVModelVariab", "InverterVariab", "SoilingVariab",
          "DegradVariab", "CustomVariab")
PLOT_KIND = 1
SCOPE = "Root P50_P90 tag block only; native 8.1.6 P50 fields, no probability calculation"


def _step(value, label, low, high):
    """Adapter support envelope checked with the shared finite() helper, plus 0.001 steps."""
    number = finite(value, label, minimum=low, maximum=high)
    scaled = Decimal(str(number)) * 1000
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{label} must use 0.001 increments")
    return number


def _label(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("CustomVarLabel must be a trimmed single-line string")
    if not value.isascii() or not value.isprintable() or len(value.encode("ascii")) > 80:
        raise ValueError("CustomVarLabel must be 1..80 printable ASCII bytes")
    return value


def _validate(values):
    """One rule set for both the API input and the decoded native values."""
    if not isinstance(values, dict) or set(values) != set(ORDER):
        raise ValueError("Require exactly the fourteen native P50 keys")
    out = {"MeteoKind": values["MeteoKind"]}
    if out["MeteoKind"] not in ("SpecYear", "TMY"):
        raise ValueError("MeteoKind must be SpecYear or TMY")
    for key in INTS:
        if type(values[key]) is not int:
            raise ValueError(f"{key} must be a strict integer")
        if not 51 <= values[key] <= 99:
            raise ValueError(f"{key} must stay inside 51..99")
        out[key] = values[key]
    if not 51 <= out["P90Custom"] < out["PxxCustom"] <= 99:
        raise ValueError("Require 51 <= P90Custom < PxxCustom <= 99")
    for key in BOOLS:
        if type(values[key]) is not bool:
            raise ValueError(f"{key} must be a strict boolean")
        out[key] = values[key]
    if type(values["PlotKind"]) is not int or values["PlotKind"] != PLOT_KIND:
        raise ValueError(f"PlotKind must be the integer {PLOT_KIND}")
    out["PlotKind"] = PLOT_KIND
    out["YearDevFromAver"] = _step(values["YearDevFromAver"], "YearDevFromAver", -100, 100)
    for key in VARIAB:
        out[key] = _step(values[key], key, 0, 100)
    out["CustomVarLabel"] = _label(values["CustomVarLabel"])
    return out


def _read(lines, require_template=False):
    """Locate the single root block in a result template and re-validate it in place."""
    heads = [i for i, line in enumerate(lines) if line.strip() == HEAD]
    if len(heads) > 1:
        raise ValueError("Require at most one root P50_P90 block")
    if not heads:
        return None
    head = heads[0]
    tail = next((i for i in range(head + 1, len(lines)) if lines[i].strip() == END), None)
    if tail is None:
        raise ValueError("Unclosed P50_P90 block")
    plots = [i for i, line in enumerate(lines) if line.strip() == PLOTS]
    if len(plots) != 1 or head > plots[0] or tail > plots[0]:
        raise ValueError("Require one SimulPlots marker after the whole P50_P90 block")
    eol = "\r\n" if lines[head].endswith("\r\n") else "\n"
    try:
        prefix = re.sub(r"(?m)^([ \t]*)End of TOrientGroup([ \t]*)(?=\r?$)",
                        r"\1End of PVObject pvOrient\2", "".join(lines[:head]))
        blocks = objects(prefix + f"End of PVObject {ROOT_TYPE}{eol}")
    except ValueError:
        raise ValueError("Root object prefix is unbalanced") from None
    roots = [block for block in blocks if not block.path]
    if len(roots) != 1 or roots[0].object_type != ROOT_TYPE:
        raise ValueError(f"Require a single {ROOT_TYPE} root object above P50_P90")
    version = roots[0].one("Version")[1]
    if version != "8.1.6":
        raise ValueError("P50 requires root Version=8.1.6")
    raw_flags = roots[0].one("Flags")[1]
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw_flags):
        raise ValueError("P50 root Flags must be a hexadecimal token")
    state = int(raw_flags[1:], 16)
    if require_template and state != ROOT_FLAGS:
        raise ValueError("P50 editing requires the observed result-template root Flags $01800001")
    if state not in READ_FLAGS:
        raise ValueError("P50 root Flags is not one of the recorded native states")
    fields = {}
    for i in range(head + 1, tail):
        text = lines[i].strip()
        if text.startswith(("PVObject_", "End of ", HEAD)):
            raise ValueError("P50_P90 must stay a flat tag block")
        match = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))
        if match is None or match[2] not in ORDER or match[2] in fields:
            raise ValueError("Unknown or duplicate P50 field inside the block")
        fields[match[2]] = (i, match[4])
    if set(fields) != set(ORDER):
        raise ValueError("P50_P90 requires all fourteen native fields")
    decoded = {}
    for key in ORDER:
        raw = fields[key][1]
        if key in BOOLS:
            if raw not in ("True", "False"):
                raise ValueError(f"{key} must be native True or False")
            decoded[key] = raw == "True"
        elif key in INTS or key == "PlotKind":
            if not re.fullmatch(r"-?[0-9]+", raw):
                raise ValueError(f"{key} must be a native integer")
            decoded[key] = int(raw)
        elif key in ("MeteoKind", "CustomVarLabel"):
            decoded[key] = raw
        else:
            try:
                decoded[key] = float(raw)
            except (ValueError, OverflowError):
                raise ValueError(f"{key} must be a native number") from None
    return fields, _validate(decoded)


def p50_state(lines):
    """Native P50 state; an absent block reports mode none, an unusable one reports an issue."""
    try:
        got = _read(lines)
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}
    if got is None:
        return {"mode": "none", "values": None}
    return {"mode": "configured", "values": got[1]}


def replace_p50(lines, updates):
    """Replace the fourteen existing native values in place, keeping order and formatting."""
    target = _validate(updates)
    got = _read(lines, require_template=True)
    if got is None:
        raise ValueError("Require an existing root P50_P90 block; this editor never creates one")
    fields, before = got
    if before == target:
        return {"before": before, "after": before, "scope": SCOPE}
    for key, (index, raw) in fields.items():
        value = target[key]
        rendered = ("True" if value is True else "False") if key in BOOLS else (
            format(value, ".3f") if key in VARIAB or key == "YearDevFromAver" else str(value))
        if rendered != raw:
            lines[index] = replace_value(lines[index], rendered)
    return {"before": before, "after": target, "scope": SCOPE}

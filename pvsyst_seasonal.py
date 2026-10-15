"""Native 8.1.6 seasonal tilt (TOrientGroup) tag editing; no seasonal or irradiance calculation."""
from __future__ import annotations

import math
import re
from decimal import Decimal

from pvsyst_component_editor import ASSIGNMENT, replace_value
from pvsyst_generator import direct_fields, finite, one

SEASONAL = ("SummerTilt", "WinterTilt", "WinterMonths")
BOOLS = ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver")
REQUIRED = ("summer_tilt", "winter_tilt", "winter_months")
ALLOWED = set(REQUIRED) | {"orientation_name"}
MASK = re.compile(r"\$[0-9A-Fa-f]{1,4}")


def _angle(value, label):
    number = finite(value, label, maximum=90)
    scaled = Decimal(str(number)) * 10
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{label} must use 0.1 degree steps")
    return number


def _months(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 11:
        raise ValueError("winter_months must be a list of 1 to 11 months")
    if any(not isinstance(m, int) or isinstance(m, bool) or not 1 <= m <= 12 for m in value):
        raise ValueError("winter_months must contain integers in 1..12")
    if len(set(value)) != len(value):
        raise ValueError("winter_months must not repeat a month")
    return sorted(value)


def _name(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("orientation_name must be a non-empty trimmed string")
    if len(value.encode("utf-8")) > 80 or not value.isprintable():
        raise ValueError("orientation_name must be 1..80 printable UTF-8 bytes")
    return value


def _literal(raw, label):
    """Decode an existing native literal; a missing value is never invented as zero."""
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{label} must be a finite number") from None
    if not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return value


def _mask_months(raw):
    if not MASK.fullmatch(raw):
        raise ValueError("WinterMonths must be a native hexadecimal month mask")
    mask = int(raw[1:], 16)
    if mask & ~0xFFF:
        raise ValueError("WinterMonths must use only the twelve month bits")
    return [month for month in range(1, 13) if mask >> (month - 1) & 1]


def _read(lines, bounds):
    """Validate the owning orientation; return direct fields, the FieldAzim entry,
    the decoded snake-case state and the BaseSlope entry."""
    for i in range(bounds[0] + 1, bounds[1]):
        text = lines[i].strip()
        match = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))
        if text.startswith(("End of ", "EndTags")) or (match and (
                match[2] == "PVObject_" or re.fullmatch(r"T[A-Z]\w*", match[4]))):
            raise ValueError("Nested objects are not supported in an orientation block")
    fields = direct_fields(lines, bounds)
    if any(len(entries) > 1 for entries in fields.values()):
        raise ValueError("Duplicate fields are not supported in an orientation block")
    if one(fields, "Version")[1] != "8.1.6":
        raise ValueError("Seasonal editing requires a native 8.1.6 orientation")
    flags = one(fields, "Flags")[1]
    if not re.fullmatch(r"\$[0-9A-Fa-f]+", flags) or int(flags[1:], 16) != 0:
        raise ValueError("Seasonal editing requires orientation Flags=$00")
    if one(fields, "NoOrient")[1] != "1":
        raise ValueError("Seasonal editing requires NoOrient=1")
    kind = one(fields, "FieldType")[1]
    if kind not in ("FixedPlane", "SeasonalTilt"):
        raise ValueError("Seasonal editing requires FieldType FixedPlane or SeasonalTilt")
    one(fields, "FieldTilt")
    azim = one(fields, "FieldAzim")
    for name in BOOLS:
        if one(fields, name)[1] != "False":
            raise ValueError(f"Seasonal editing requires {name}=False")
    present = [one(fields, name, False) for name in SEASONAL]
    if kind == "FixedPlane" and any(entry is not None for entry in present):
        raise ValueError("A FixedPlane orientation must not carry seasonal fields")
    if kind == "SeasonalTilt" and any(entry is None for entry in present):
        raise ValueError("A SeasonalTilt orientation must carry all three seasonal fields")
    comment = one(fields, "Comment", False)
    base = one(fields, "BaseSlope", False)
    if base is not None and kind == "SeasonalTilt":
        raise ValueError("BaseSlope is not a verified key on a SeasonalTilt orientation")
    before = {"field_type": kind, "orientation_name": comment[1] if comment else None,
              "base_slope": _literal(base[1], "BaseSlope") if base else None,
              "summer_tilt": None, "winter_tilt": None, "winter_months": None}
    if present[0] is not None:
        before["summer_tilt"] = finite(_literal(present[0][1], "SummerTilt"), "SummerTilt", maximum=90)
        before["winter_tilt"] = finite(_literal(present[1][1], "WinterTilt"), "WinterTilt", maximum=90)
        before["winter_months"] = _mask_months(present[2][1])
    return fields, azim, before, base


def replace_seasonal(lines, bounds, updates):
    """Rewrite the owning orientation as native SeasonalTilt; edits `lines` in place."""
    if (not isinstance(updates, dict) or set(updates) - ALLOWED
            or not set(REQUIRED) <= set(updates)):
        raise ValueError("Seasonal updates: summer_tilt, winter_tilt, winter_months, orientation_name")
    summer = _angle(updates["summer_tilt"], "summer_tilt")
    winter = _angle(updates["winter_tilt"], "winter_tilt")
    months = _months(updates["winter_months"])
    name = _name(updates["orientation_name"]) if "orientation_name" in updates else None
    fields, azim, before, base = _read(lines, bounds)
    if name is not None and before["orientation_name"] is None:
        raise ValueError("orientation_name requires an owning Comment")
    if base is not None:
        # The GUI drops BaseSlope when a FixedPlane orientation becomes SeasonalTilt.
        if before["base_slope"] != 0:
            raise ValueError("Non-zero BaseSlope is not a verified layout")
        del lines[base[0]]
        fields, azim, _, _ = _read(lines, (bounds[0], bounds[1] - 1))
    ending = "\r\n" if lines[bounds[0]].endswith("\r\n") else "\n"
    indent = re.match(r"[ \t]*", lines[azim[0]])[0]
    target = {"FieldType": "SeasonalTilt", "FieldTilt": "0.0",
              "SummerTilt": format(summer, ".1f"), "WinterTilt": format(winter, ".1f"),
              "WinterMonths": "$%04X" % sum(1 << (month - 1) for month in months)}
    inserted = []
    for key, value in target.items():
        entry = one(fields, key, False)
        if entry is None:
            inserted.append(f"{indent}{key}={value}{ending}")
        elif entry[1] != value:
            lines[entry[0]] = replace_value(lines[entry[0]], value)
    if name is not None and before["orientation_name"] != name:
        lines[fields["Comment"][0][0]] = replace_value(lines[fields["Comment"][0][0]], name)
    if inserted:
        lines[azim[0] + 1:azim[0] + 1] = inserted
    return {"before": before,
            "after": {"field_type": "SeasonalTilt",
                      "orientation_name": name if name is not None else before["orientation_name"],
                      "base_slope": None, "summer_tilt": summer, "winter_tilt": winter,
                      "winter_months": months},
            "scope": "Whole orientation; native 8.1.6 seasonal tilt, not a seasonal or irradiance model"}

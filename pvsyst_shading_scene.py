"""Read native scene declarations without decoding geometry or changing a scene."""
from __future__ import annotations

import re

from pvsyst_component_editor import ASSIGNMENT

ACTIVE_BIT = 0x00000100
SCENE_KEYS = ("Comment", "Flags", "Precision", "FracOmbreMod", "FracOmbreThin", "DisplayedRef")
OBJECT_COUNT = re.compile(r"^ListeObjets, list of=(\d+) TShdObject$")


def shading_state(lines):
    starts = [i for i, line in enumerate(lines) if line.strip() == "PVObject_Ombrage=pvShading"]
    if not starts:
        return {"mode": None, "reason": "no pvShading section"}
    if len(starts) != 1:
        return {"mode": None, "issue": "Requires exactly one pvShading owner"}
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() == "End of PVObject pvShading"), None)
    if end is None:
        return {"mode": None, "issue": "pvShading owner is not closed"}
    body = [line.rstrip("\r\n") for line in lines[start + 1:end]]
    values = {}
    for line in body:
        if line.strip().startswith(("PVObject_", "ListeObjets")):
            break
        match = ASSIGNMENT.fullmatch(line)
        if match is None:
            continue
        if match[2] in values:
            return {"mode": None, "issue": f"Duplicate {match[2]} in pvShading"}
        values[match[2]] = match[4]
    raw = values.get("Flags", "")
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", raw):
        return {"mode": None, "issue": "pvShading Flags is missing or not a hexadecimal token"}
    lists = [(index, int(match[1])) for index, line in enumerate(body)
             if (match := OBJECT_COUNT.fullmatch(line.strip()))]
    if len(lists) > 1:
        return {"mode": None, "issue": "Repeated native scene object list"}
    active = bool(int(raw[1:], 16) & ACTIVE_BIT)
    declared, types = 0, []
    if lists:
        begin, declared = lists[0]
        finish = next((i for i in range(begin + 1, len(body))
                       if body[i].strip() == "End of ListeObjets"), None)
        if finish is None:
            return {"mode": None, "issue": "Native scene object list is not closed"}
        # Native groups follow this list and are not additional shading objects.
        types = [line.strip().partition("=")[2] for line in body[begin + 1:finish]
                 if line.strip().startswith("PVObject_")]
    elif active:
        return {"mode": None, "issue": "Active shading owner carries no object list"}
    if declared != len(types) or (not active and declared):
        return {"mode": None, "issue": "The owner flags, declared object count and stored objects disagree"}
    scene = {key: values[key] for key in SCENE_KEYS if key in values}
    if not active:
        return {"mode": "empty", "flags": raw, "values": scene}
    return {"mode": "active", "flags": raw, "object_count": declared,
            "object_types": types, "values": scene,
            "scope": "stored scene only; object geometry is not decoded and nothing is written"}

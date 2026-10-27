"""Read native scene declarations without decoding geometry or changing a scene."""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation

from pvsyst_auto.components.editor import ASSIGNMENT, objects

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
            "pv_bindings": _pv_bindings(lines, types),
            "scope": "stored scene/PV declarations only; generic geometry, source XML and native consumption are not verified"}


def _pv_bindings(lines, types):
    """Cross-check stored declarations, not source coordinates or electrical topology."""
    result = {"status": "unsupported", "tables": [], "groups": [], "issues": [],
              "coordinate_frame": "native DOrigine in metres; not house/PVCollada coordinates",
              "source_geometry_verified": False, "native_consumption_verified": False,
              "generic_geometry_decoded": False}

    def integer(block, key):
        value = block.one(key)[1]
        if not re.fullmatch(r"[1-9][0-9]*", value):
            raise ValueError(f"{block.path}/{key} requires a positive integer")
        return int(value)

    def number(value):
        parsed = Decimal(value)
        if not parsed.is_finite():
            raise ValueError("Native PV placement values must be finite")
        return parsed

    def raw_fields(block):
        return {key: values[0][1] if len(values) == 1 else [value for _, value in values]
                for key, values in block.fields.items()}

    try:
        if (any(kind not in ("pvShdArrayTable", "pvShdGenObject") for kind in types)
                or not 1 <= types.count("pvShdArrayTable") <= 2):
            raise ValueError("Stored binding review supports one/two PV tables and generic objects only")
        blocks = objects("\n".join(line.rstrip("\r\n") for line in lines))
        tables = [block for block in blocks if block.object_type == "pvShdArrayTable"]
        if len(tables) != types.count("pvShdArrayTable"):
            raise ValueError("PV table records must belong to the declared shading list")
        planes = [block for block in blocks if block.object_type == "pvOrient"]
        arrays = [block for block in blocks if block.object_type == "pvSubArray"]
        if not 1 <= len(planes) <= 2 or not 1 <= len(arrays) <= 2:
            raise ValueError("Stored binding review supports one/two fixed source planes and subarrays")
        grouped = {}
        for plane in planes:
            identifier = integer(plane, "NoOrient")
            if identifier in grouped:
                raise ValueError("Repeated stored orientation ID")
            if (plane.one("FieldType")[1] != "FixedPlane" or plane.one("Flags")[1] != "$00"
                    or any(key in plane.fields and plane.one(key)[1] != "False"
                           for key in ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver"))):
                raise ValueError("Stored binding review requires ordinary non-averaged fixed planes")
            number(plane.one("FieldTilt")[1])
            number(plane.one("FieldAzim")[1])
            grouped[identifier] = {"orientation_id": identifier,
                "orientation_values": raw_fields(plane), "subarray_ids": [], "table_paths": [],
                "electrical_module_count": 0, "scene_module_count": 0}
        array_ids = set()
        for array in arrays:
            array_id = integer(array, "SubArrayId")
            if array_id in array_ids:
                raise ValueError("Repeated stored subarray ID")
            array_ids.add(array_id)
            identifier = integer(array, "NoOrientation")
            if identifier not in grouped:
                result["issues"].append(f"Subarray {array.path} references missing orientation {identifier}")
                continue
            group = grouped[identifier]
            group["subarray_ids"].append(array_id)
            group["electrical_module_count"] += integer(array, "NModSerie") * integer(array, "NStringCh")
        for table in tables:
            row = {"object_path": table.path, "values": raw_fields(table)}
            result["tables"].append(row)
            if table.one("Version")[1] != "8.1.6" or table.one("Source")[1] != "osPVCollada":
                raise ValueError("PV table declarations are mapped only for native8.1.6 PVCollada records")
            identifier = integer(table, "NoOrient")
            module_count = integer(table, "NModInHeight") * integer(table, "NModInWidth")
            origin = [float(number(value)) for value in table.one("DOrigine")[1].split(",")]
            if len(origin) != 3 or not all(math.isfinite(value) for value in origin):
                raise ValueError("Native DOrigine requires three finite JSON-number coordinates")
            row.update(orientation_id=identifier, module_count=module_count, origin_m=origin)
            tilt, azimuth = number(table.one("DInclin")[1]), number(table.one("DAzimut")[1])
            slope = number(table.one("BaseSlope")[1])
            if identifier not in grouped:
                result["issues"].append(f"PV table {table.path} references missing orientation {identifier}")
                continue
            group = grouped[identifier]
            group["table_paths"].append(table.path)
            group["scene_module_count"] += module_count
            if (tilt != number(group["orientation_values"]["FieldTilt"])
                    or azimuth != number(group["orientation_values"]["FieldAzim"])
                    or slope != number(group["orientation_values"]["BaseSlope"])):
                result["issues"].append(f"PV table {table.path} angles differ from orientation {identifier}")
        result["groups"] = list(grouped.values())
        for group in result["groups"]:
            if not group["subarray_ids"] or group["scene_module_count"] != group["electrical_module_count"]:
                result["issues"].append(f"Orientation {group['orientation_id']} scene/electrical module allocation differs")
        result["status"] = "needs_review" if result["issues"] else "stored_consistent"
    except (ValueError, InvalidOperation, KeyError, OverflowError) as exc:
        result["issues"].append(str(exc))
    return result

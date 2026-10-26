"""Read-only checks of native grouped scene records against their authored source."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from itertools import product

from pvsyst_auto.project.circuit import circuit_nodes, inventory
from pvsyst_auto.scene.author import MAX_VERTICES, _dot, _num, _sub

# Native 8.1.6 writes six decimals; the retained case coordinates differ by up
# to 8e-6 m. This adapter comparison tolerance is not a placement offset.
COORD_TOL = 2e-5
NORMAL_TOL = 1e-7
ANGLE_TOL = 1e-6


def _number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Native geometry values must be finite")
    return result


def _integer(value):
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError("Native identity/count must be an unsigned integer")
    return int(value)


def _indexed(blocks, kind, key):
    result = {}
    for block in blocks:
        if block.object_type != kind:
            continue
        identifier = _integer(block.one(key)[1])
        if identifier < 1 or identifier in result:
            raise ValueError("Duplicate or non-positive native " + key)
        result[identifier] = block
    return result


def verify_grouped_hardware(source_blocks, target_blocks, source_lines, target_lines, source):
    """Preserve both source identities, fixed planes, hardware and exclusive circuit branches."""
    planes = source.get("orientations", [])
    if [plane["id"] for plane in planes] != [1, 2]:
        raise ValueError("Grouped verification requires source orientations 1/2")
    originals = _indexed(source_blocks, "pvOrient", "NoOrient")
    saved = _indexed(target_blocks, "pvOrient", "NoOrient")
    if set(originals) != {1, 2} or set(saved) != {1, 2}:
        raise ValueError("Saved variant must preserve exactly the two source orientations")
    orientation_mapping = []
    for plane in planes:
        identifier = plane["id"]
        before, after = originals[identifier], saved[identifier]
        if before.one("FieldType")[1] != "FixedPlane" or after.one("FieldType")[1] != "FixedPlane":
            raise ValueError("Saved source orientation is not an ordinary fixed plane")
        for key in ("FieldTilt", "FieldAzim", "BaseSlope"):
            if _number(before.one(key)[1]) != _number(after.one(key)[1]):
                raise ValueError("Saved orientation %d changed %s" % (identifier, key))
        if (_number(after.one("FieldTilt")[1]) != plane["tilt_deg"]
                or _number(after.one("FieldAzim")[1]) != plane["field_azimuth_deg"]
                or _number(after.one("BaseSlope")[1]) != 0):
            raise ValueError("Saved orientation differs from its source profile")
        for key in ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver"):
            if before.one(key)[1] != "False" or after.one(key)[1] != "False":
                raise ValueError("Grouped verification requires unchanged ordinary plane modes")
        orientation_mapping.append({"orientation_id": identifier, "field_type": after.one("FieldType")[1],
                                    "tilt_deg": _number(after.one("FieldTilt")[1]),
                                    "field_azimuth_deg": _number(after.one("FieldAzim")[1]),
                                    "base_slope_deg": _number(after.one("BaseSlope")[1])})
    originals = _indexed(source_blocks, "pvSubArray", "SubArrayId")
    saved = _indexed(target_blocks, "pvSubArray", "SubArrayId")
    if len(originals) != 2 or set(originals) != set(saved):
        raise ValueError("Saved variant changed the source subarray identities")
    hardware_mapping = []
    for identifier, before in originals.items():
        after = saved[identifier]
        for key in ("PVModule", "GInverter"):
            if before.one(key)[1].casefold() != after.one(key)[1].casefold():
                raise ValueError("Saved subarray %d changed %s" % (identifier, key))
        for key in ("NModSerie", "NStringCh", "NInverter", "NInvMPPT", "NoOrientation"):
            if _integer(before.one(key)[1]) != _integer(after.one(key)[1]):
                raise ValueError("Saved subarray %d changed %s" % (identifier, key))
        for key in ("Flags", "Flags2"):
            values = [block.one(key)[1] if key in block.fields else "$0" for block in (before, after)]
            if (any(not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", value) for value in values)
                    or int(values[0][1:], 16) != int(values[1][1:], 16)):
                raise ValueError("Saved subarray %d changed %s" % (identifier, key))
        for key in set(before.fields) | set(after.fields):
            if re.fullmatch(r"NStrOrient[0-9]+", key):
                values = [block.one(key)[1] if key in block.fields else "0" for block in (before, after)]
                if _integer(values[0]) != _integer(values[1]):
                    raise ValueError("Saved subarray %d changed %s" % (identifier, key))
        for key, default in (("PowerSharingInverterId", "-1"), ("SEIsHeterogeneousString", "False,")):
            values = [block.one(key)[1] if key in block.fields else default for block in (before, after)]
            if values[0].rstrip(",") != values[1].rstrip(","):
                raise ValueError("Saved subarray %d changed %s" % (identifier, key))
        if ("PNomAC_InMPPT" in before.fields or "PNomAC_InMPPT" in after.fields) and (
                _number(before.one("PNomAC_InMPPT")[1]) != _number(after.one("PNomAC_InMPPT")[1])):
            raise ValueError("Saved subarray changed its inverter input power declaration")
        hardware_mapping.append({"subarray_id": identifier,
                                 "orientation_id": _integer(after.one("NoOrientation")[1]),
                                 "pan": after.one("PVModule")[1], "ond": after.one("GInverter")[1],
                                 "modules_in_series": _integer(after.one("NModSerie")[1]),
                                 "strings": _integer(after.one("NStringCh")[1]),
                                 "inverters": _integer(after.one("NInverter")[1]),
                                 "mppt_inputs": _integer(after.one("NInvMPPT")[1])})

    def circuit(node):
        return (node.kind, node.one("SubArrayId"), _integer(node.one("NElements", "1")),
                tuple(sorted(circuit(child) for child in node.children)))

    if tuple(sorted(circuit(node) for node in circuit_nodes(source_lines))) != tuple(
            sorted(circuit(node) for node in circuit_nodes(target_lines))):
        raise ValueError("Saved variant changed the source circuit ownership or multiplicities")
    for lines, arrays in ((source_lines, originals), (target_lines, saved)):
        bounds = {key: (block.line - 1, block.end_line - 1) for key, block in arrays.items()}
        checked_inventory = inventory(lines, bounds)
        if checked_inventory["counts_valid"] is not True:
            raise ValueError("Source and saved grouped circuits must both have complete consistent counts")
    tables = _indexed(target_blocks, "pvShdArrayTable", "NoOrient")
    if set(tables) != {1, 2}:
        raise ValueError("Each source orientation must own exactly one native PV table")
    for plane in planes:
        table = tables[plane["id"]]
        if (_integer(table.one("NModInHeight")[1]) * _integer(table.one("NModInWidth")[1])
                != plane["module_total"]
                or _number(table.one("DInclin")[1]) != plane["tilt_deg"]
                or _number(table.one("DAzimut")[1]) != plane["field_azimuth_deg"]
                or _number(table.one("BaseSlope")[1]) != 0):
            raise ValueError("Native PV table differs from its source orientation/count")
    return {"source_hardware_preserved": True, "source_orientations_preserved": True,
            "source_circuit_preserved": True, "source_orientation_ids": [1, 2],
            "orientation_mapping": orientation_mapping, "hardware_mapping": hardware_mapping,
            "circuit_mapping": [{"subarray_id": row["id"], **row["circuit"]}
                                for row in checked_inventory["subarrays"]]}


def _native_mesh(block, lines):
    if block.one("Source")[1] != "osPVCollada" or block.one("Version")[1] != "8.1.6":
        raise ValueError("House verification requires the recorded native PVCollada save version")
    if any(_number(block.one(key)[1]) != 0 for key in ("DInclin", "BaseSlope", "DAzimut")):
        raise ValueError("House verification requires the observed baked, zero-angle native mesh records")
    headers = [re.fullmatch(r"\s*Sommets, TPoly3D=([0-9]+) Point3D\s*", line)
               for line in lines[block.line:block.end_line - 1]]
    counts = [int(match[1]) for match in headers if match]
    if len(counts) != 1 or not 3 <= counts[0] <= MAX_VERTICES:
        raise ValueError("Native house record lacks one complete point list")
    if {key for key in block.fields if key.startswith("Point_")} != {
            "Point_%d" % number for number in range(1, counts[0] + 1)}:
        raise ValueError("Native point rows do not match their declared count")
    origin = [_number(value) for value in block.one("DOrigine")[1].split(",")]
    if len(origin) != 3:
        raise ValueError("Native house origin requires three coordinates")
    points = []
    for index in range(1, counts[0] + 1):
        local = [_number(value) for value in block.one("Point_%d" % index)[1].split(",")]
        if len(local) != 3:
            raise ValueError("Native house points require three coordinates")
        points.append([origin[axis] + local[axis] for axis in range(3)])
    raw = block.one("Faces")[1].rstrip(",").split(",")
    if any(not re.fullmatch(r"-1|[0-9]+", value) for value in raw):
        raise ValueError("Native face indices must be complete integers")
    values = [int(value) for value in raw]
    faces = []
    for value in values:
        if value == -1:
            faces.append([])
        elif not faces or not 0 <= value < len(points):
            raise ValueError("Native face indices are outside their point list")
        else:
            faces[-1].append(value)
    if (len(values) != _integer(block.one("NPElements")[1])
            or len(faces) != _integer(block.one("NElements")[1])
            or any(len(face) < 3 or len(set(face)) != len(face) for face in faces)):
        raise ValueError("Native polygon counts or vertex loops are incomplete")
    return points, faces


def _bounds(points):
    return tuple(value for axis in range(3) for value in (
        min(point[axis] for point in points), max(point[axis] for point in points)))


def _face_normal(points, face):
    normal = [0.0, 0.0, 0.0]
    origin = points[face[0]]
    for start, end in zip(face, face[1:] + face[:1]):
        a, b = _sub(points[start], origin), _sub(points[end], origin)
        normal[0] += a[1] * b[2] - a[2] * b[1]
        normal[1] += a[2] * b[0] - a[0] * b[2]
        normal[2] += a[0] * b[1] - a[1] * b[0]
    length = math.hypot(*normal)
    if not math.isfinite(length) or length == 0:
        raise ValueError("Native polygon is degenerate")
    return [value / length for value in normal]


def _surface_loops(points, faces, planes, plane_buckets):
    edges = defaultdict(Counter)
    for face in faces:
        # Signed normals keep reversed winding in a different surface group.
        normal = _face_normal(points, face)
        distance = _dot(normal, points[face[0]])
        if any(abs(_dot(normal, points[index]) - distance) > COORD_TOL for index in face):
            raise ValueError("Native polygon is not planar at the comparison precision")
        cell = tuple(math.floor(value / NORMAL_TOL) for value in normal) + (
            math.floor(distance / COORD_TOL),)
        nearby = [index for shift in product((-1, 0, 1), repeat=4)
                  for index in plane_buckets.get(tuple(cell[axis] + shift[axis] for axis in range(4)), ())]
        matches = [index for index in nearby for other, offset in [planes[index]]
                   if max(abs(normal[axis] - other[axis]) for axis in range(3)) <= NORMAL_TOL
                   and all(abs(_dot(other, points[vertex]) - offset) <= COORD_TOL for vertex in face)]
        if len(matches) > 1:
            raise ValueError("Coincident source planes cannot be identified uniquely")
        if matches:
            plane = matches[0]
        else:
            plane = len(planes)
            planes.append((normal, distance))
            plane_buckets[cell].append(plane)
        for start, end in zip(face, face[1:] + face[:1]):
            reverse = (end, start)
            if edges[plane][reverse]:
                edges[plane][reverse] -= 1
            else:
                edges[plane][(start, end)] += 1
    result = {}
    for plane, counts in edges.items():
        remaining = {edge: count for edge, count in counts.items() if count}
        if any(count != 1 for count in remaining.values()):
            raise ValueError("Coincident or repeated oriented surface boundaries")
        following = {start: end for start, end in remaining}
        if len(following) != len(remaining) or len(set(following.values())) != len(following):
            raise ValueError("Surface boundaries must form disjoint oriented loops")
        loops = []
        while following:
            start = next(iter(following))
            loop, vertex = [], start
            while vertex in following:
                loop.append(vertex)
                vertex = following.pop(vertex)
            if vertex != start or len(loop) < 3:
                raise ValueError("Surface boundary is not a closed polygon")
            while len(loop) > 3:
                simplified = []
                for index, vertex in enumerate(loop):
                    a, b = _sub(points[vertex], points[loop[index - 1]]), _sub(
                        points[loop[(index + 1) % len(loop)]], points[vertex])
                    cross = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
                             a[0] * b[1] - a[1] * b[0])
                    if _dot(a, b) >= 0 and math.hypot(*cross) <= COORD_TOL * (math.hypot(*a) + math.hypot(*b)):
                        continue
                    simplified.append(vertex)
                if len(simplified) == len(loop):
                    break
                if len(simplified) < 3:
                    raise ValueError("Surface boundary collapsed at the comparison precision")
                loop = simplified
            pivot = loop.index(min(loop))
            loops.append(tuple(loop[pivot:] + loop[:pivot]))
        result[plane] = tuple(sorted(loops))
    return result


def _same_mesh(expected, triangles, actual, polygons):
    # Map native vertices to unique source coordinates; object names/order are not identity.
    points = list(dict.fromkeys(tuple(point) for point in expected))
    source_index = {point: index for index, point in enumerate(points)}
    triangles = [[source_index[tuple(expected[index])] for index in face] for face in triangles]
    buckets = defaultdict(list)
    for index, point in enumerate(points):
        buckets[tuple(math.floor(value / COORD_TOL) for value in point)].append(index)
    mapped, residual = [], 0.0
    for point in actual:
        cell = tuple(math.floor(value / COORD_TOL) for value in point)
        candidates = [index for shift in product((-1, 0, 1), repeat=3)
                      for index in buckets.get(tuple(cell[axis] + shift[axis] for axis in range(3)), ())
                      if max(abs(point[axis] - points[index][axis]) for axis in range(3)) <= COORD_TOL]
        if len(candidates) != 1:
            return None
        mapped.append(candidates[0])
        residual = max(residual, max(abs(point[axis] - points[candidates[0]][axis]) for axis in range(3)))
    mapped_polygons = [[mapped[index] for index in face] for face in polygons]
    for face, mapped_face in zip(polygons, mapped_polygons):
        if len(set(mapped_face)) != len(mapped_face):
            return None
        actual_normal = _face_normal(actual, face)
        if _dot(actual_normal, _face_normal(points, mapped_face)) <= 0:
            return None
        if any(abs(_dot(actual_normal, _sub(actual[index], actual[face[0]]))) > COORD_TOL for index in face):
            return None
    planes, plane_buckets = [], defaultdict(list)
    source_loops = _surface_loops(points, triangles, planes, plane_buckets)
    saved_loops = _surface_loops(points, mapped_polygons, planes, plane_buckets)
    return residual if source_loops == saved_loops else None


def verify_house_geometry(blocks, lines, house, source):
    """Match every source mesh by world position and oriented polygon boundaries.

    This bounded path uses only the observed zero-azimuth source anchor: native
    coordinates negate source X/Y and preserve Z. Other anchors remain closed.
    """
    if house.get("kind") != "mesh" or source["field_azimuth_deg"] != 0:
        raise ValueError("House coordinate verification is recorded only for mesh scenes with source anchor azimuth zero")
    native = [(block, *_native_mesh(block, lines)) for block in blocks if block.object_type == "pvShdGenObject"]
    if len(native) != len(house["meshes"]):
        raise ValueError("Native and source house mesh counts differ")
    mappings, max_residual = [], 0.0
    for part in house["meshes"]:
        expected = [(-point[0], -point[1], point[2]) for point in part["vertices"]]
        bounds = _bounds(expected)
        matches = []
        for index, (block, points, faces) in enumerate(native):
            if any(abs(a - b) > COORD_TOL for a, b in zip(bounds, _bounds(points))):
                continue
            residual = _same_mesh(expected, part["triangles"], points, faces)
            if residual is not None:
                matches.append((index, residual))
        if len(matches) != 1:
            raise ValueError("Source house mesh has no unique native surface match: " + part["name"])
        index, residual = matches[0]
        block, points, faces = native.pop(index)
        max_residual = max(max_residual, residual)
        mappings.append({"source_mesh": part["name"], "native_path": block.path,
                         "native_polygons": len(faces), "max_coordinate_residual_m": residual})
    return {"house_geometry_verified": True, "house_mapping": mappings,
            "house_coordinate_tolerance_m": COORD_TOL, "house_max_coordinate_residual_m": max_residual,
            "house_geometry_scope": "source mesh positions and oriented planar boundaries; anchor azimuth zero"}


def verify_pv_geometry(blocks, tables, modules, source):
    """Match source regions to explicitly repositioned native tables, then check their links.

    No importer displacement is inferred or accepted. The native origin must
    match the emitted source origin after the recorded anchor-zero XY conversion.
    """
    planes = {plane["id"]: plane for plane in source.get("orientations", [])}
    if (source["field_azimuth_deg"] != 0 or set(planes) != {1, 2} or len(tables) != 2
            or {table["orientation_id"] for table in tables} != {1, 2}):
        raise ValueError("PV coordinate verification requires two source tables and anchor azimuth zero")
    native = []
    for block in blocks:
        if block.object_type != "pvShdArrayTable":
            continue
        if (block.one("Source")[1] != "osPVCollada" or block.one("Version")[1] != "8.1.6"
                or block.one("TypeOrigine")[1] != "1" or block.one("NElemChamps")[1] != "1"):
            raise ValueError("PV verification requires the recorded PVCollada table/origin type")
        frame = [_number(value) for value in block.one("Cadre")[1].rstrip(",").split(",")]
        if len(frame) != 4 or any(value != 0 for value in frame):
            raise ValueError("The source PV geometry has no additional native table frame")
        origin = [_number(value) for value in block.one("DOrigine")[1].split(",")]
        if len(origin) != 3:
            raise ValueError("Native PV origin requires three coordinates")
        panel = block.one("PanneauPVName")[1].rsplit(";", 2)
        if len(panel) != 3 or panel[0].casefold() != modules["name"].casefold():
            raise ValueError("Native PV module name differs from the source module geometry")
        dimensions = {key: _number(block.one(key)[1]) for key in (
            "LongShed", "LargShed", "ModSpacingX", "ModSpacingY")}
        dimensions.update(module_width_m=_number(panel[1]), module_length_m=_number(panel[2]))
        if (any(dimensions[key] <= 0 for key in ("LongShed", "LargShed", "module_width_m", "module_length_m"))
                or any(dimensions[key] < 0 for key in ("ModSpacingX", "ModSpacingY"))):
            raise ValueError("Native PV dimensions/spacing are outside the source geometry range")
        native.append({"block": block, "origin": origin, "dimensions": dimensions,
                       "angles": {key: _number(block.one(key)[1]) for key in ("DInclin", "DAzimut", "BaseSlope")},
                       "rows": _integer(block.one("NModInHeight")[1]),
                       "columns": _integer(block.one("NModInWidth")[1]),
                       "orientation": block.one("ModuleOrient")[1]})
    if len(native) != 2:
        raise ValueError("PV verification requires exactly two saved native tables")
    mappings, max_origin, max_dimension, max_angle = [], 0.0, 0.0, 0.0
    for table in tables:
        identifier = table["orientation_id"]
        plane = planes[identifier]
        if (table["module_rows"] * table["module_columns"] != plane["module_total"]
                or table["tilt_deg"] != plane["tilt_deg"]
                or table["azimuth_deg"] != plane["azimuth_deg"]
                or table["rotation_deg"] != source["azimuth_deg"] - plane["azimuth_deg"]):
            raise ValueError("Source table profile differs from its source orientation")
        origin = [float(_num(value)) for value in table["origin_m"]]
        expected_origin = [-origin[0], -origin[1], origin[2]]
        expected_dimensions = {"LongShed": table["column_span_m"], "LargShed": table["slope_span_m"],
                               "ModSpacingX": table["column_spacing_m"], "ModSpacingY": table["row_spacing_m"],
                               "module_width_m": modules["width_m"], "module_length_m": modules["length_m"]}
        expected_dimensions = {key: float(_num(value)) for key, value in expected_dimensions.items()}
        expected_angles = {"DInclin": float(_num(plane["tilt_deg"])),
                           "DAzimut": float(_num(plane["field_azimuth_deg"])), "BaseSlope": 0.0}
        orientation = {"landscape": "moLandScape", "portrait": "moPortrait"}[table["orientation"]]
        matches = []
        for index, record in enumerate(native):
            if (record["rows"] != table["module_rows"] or record["columns"] != table["module_columns"]
                    or record["orientation"] != orientation):
                continue
            coordinate_delta = [actual - expected for actual, expected in zip(record["origin"], expected_origin)]
            dimension_delta = {key: record["dimensions"][key] - value for key, value in expected_dimensions.items()}
            angle_delta = {key: record["angles"][key] - value for key, value in expected_angles.items()}
            if (max(map(abs, coordinate_delta)) <= COORD_TOL
                    and max(map(abs, dimension_delta.values())) <= COORD_TOL
                    and max(map(abs, angle_delta.values())) <= ANGLE_TOL):
                matches.append((index, coordinate_delta, dimension_delta, angle_delta))
        if len(matches) != 1:
            raise ValueError("Source PV region has no unique native geometry match: " + table["name"])
        index, coordinate_delta, dimension_delta, angle_delta = matches[0]
        record = native.pop(index)
        block = record["block"]
        if _integer(block.one("NoOrient")[1]) != identifier:
            raise ValueError("Matched native PV region is linked to the wrong source orientation")
        max_origin = max(max_origin, max(map(abs, coordinate_delta)))
        max_dimension = max(max_dimension, max(map(abs, dimension_delta.values())))
        max_angle = max(max_angle, max(map(abs, angle_delta.values())))
        mappings.append({"source_table": table["name"], "orientation_id": identifier,
                         "native_path": block.path, "expected_origin_m": expected_origin,
                         "native_origin_m": record["origin"], "origin_residual_m": coordinate_delta,
                         "dimension_residual_m": dimension_delta, "angle_residual_deg": angle_delta})
    return {"pv_geometry_verified": True, "pv_mapping": mappings,
            "pv_coordinate_tolerance_m": COORD_TOL, "pv_angle_tolerance_deg": ANGLE_TOL,
            "pv_max_coordinate_residual_m": max_origin, "pv_max_dimension_residual_m": max_dimension,
            "pv_max_angle_residual_deg": max_angle,
            "pv_geometry_scope": "source origins, planes, dimensions, module layout and NoOrient links; anchor azimuth zero"}

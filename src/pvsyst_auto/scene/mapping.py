"""Read-only checks of native grouped scene records against their authored source."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from itertools import product

from pvsyst_auto.project.circuit import circuit_nodes, inventory
from pvsyst_auto.scene.author import MAX_VERTICES, _dot, _num, _rotate_z, _sub

# Native 8.1.6 writes six decimals; the retained case coordinates differ by up
# to 8e-6 m. This adapter comparison tolerance is not a placement offset.
COORD_TOL = 2e-5
NORMAL_TOL = 1e-7
ANGLE_TOL = 1e-6

# Verified stored-state allow-lists (explicit pairs/tokens, never a version prefix).
# A native 8.1.7 re-save keeps the same osPVCollada PV-table grammar and the same
# module-layout geometry/counts/module->string assignment; only the engine version
# token and the post-computation state labels advance.  Unknown sources/versions or
# unknown states still raise, so a stored_consistent result never claims a second
# native computation or physical certification.
PV_TABLE_SOURCES = frozenset({("osPVCollada", "8.1.6"), ("osPVCollada", "8.1.7")})
MODULE_LAYOUT_GLOBAL_STATES = frozenset({"mlDefStrings", "mlDefInverters", "mlShd3DCalculated"})
MODULE_LAYOUT_TABLE_STATES = frozenset({"mlDefStrings", "mlDefInverters"})


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


def source_string_ranges(blocks):
    """Recover each source subarray's declared GLOBAL string range, in appearance order.

    A source subarray declares ``NStringCh`` strings of ``NModSerie`` modules each; native
    PVsyst numbers those module strings globally in source order, continuing across subarrays
    (SA1 owns 1..NStringCh, SA2 continues from there).  Keying by ``pvSubArray`` appearance
    order (never by numeric SubArrayId) keeps that prefix.  Capacity is bound to each GLOBAL
    string (== the owning subarray's NModSerie), never to a table.

    Returns an insertion-ordered ``{actual_subarray_id: (first, last, capacity)}``.  This is the
    single shared prefix algorithm for the native builder and the stored-layout reader; it only
    restores the declaration's ranges and never auto-allocates or renumbers them.  A duplicate
    or non-positive SubArrayId, or a non-positive NStringCh/NModSerie, raises ValueError.
    """
    arrays = _indexed(blocks, "pvSubArray", "SubArrayId")
    result, first = {}, 1
    for identifier, block in arrays.items():
        strings = _integer(block.one("NStringCh")[1])
        capacity = _integer(block.one("NModSerie")[1])
        if strings < 1 or capacity < 1:
            raise ValueError("Source subarray %d requires positive NStringCh and NModSerie" % identifier)
        last = first + strings - 1
        result[identifier] = (first, last, capacity)
        first = last + 1
    return result


def verify_grouped_hardware(source_blocks, target_blocks, source_lines, target_lines, source):
    """Preserve both source identities, fixed planes, hardware and exclusive circuit branches."""
    planes = source.get("orientations", [])
    expected_by_orient = {}
    if planes:
        # One orientation record may be shared by several source subarray owners: keep each DISTINCT
        # orientation record once, and aggregate the module total of every owner that references it.
        distinct = {}
        for plane in planes:
            identifier = plane["id"]
            if identifier < 1:
                raise ValueError("Grouped verification requires positive source orientations")
            distinct.setdefault(identifier, plane)
            expected_by_orient[identifier] = expected_by_orient.get(identifier, 0) + plane["module_total"]
        planes = list(distinct.values())
    else:
        planes = [{"id": 1, "module_total": source["module_total"],
                   "tilt_deg": source["tilt_deg"], "field_azimuth_deg": source["field_azimuth_deg"]}]
        expected_by_orient = {1: source["module_total"]}
    orientation_ids = [plane["id"] for plane in planes]
    originals = _indexed(source_blocks, "pvOrient", "NoOrient")
    saved = _indexed(target_blocks, "pvOrient", "NoOrient")
    if set(originals) != set(orientation_ids) or set(saved) != set(orientation_ids):
        raise ValueError("Saved variant must preserve exactly the source orientations")
    orientation_mapping = []
    for plane in planes:
        identifier = plane["id"]
        before, after = originals[identifier], saved[identifier]
        if before.one("FieldType")[1] != "FixedPlane" or after.one("FieldType")[1] != "FixedPlane":
            raise ValueError("Saved source orientation is not an ordinary fixed plane")
        for key in ("FieldTilt", "FieldAzim", "BaseSlope"):
            if abs(_number(before.one(key)[1]) - _number(after.one(key)[1])) > ANGLE_TOL:
                raise ValueError("Saved orientation %d changed %s" % (identifier, key))
        if (abs(_number(after.one("FieldTilt")[1]) - plane["tilt_deg"]) > ANGLE_TOL
                or abs(_number(after.one("FieldAzim")[1]) - plane["field_azimuth_deg"]) > ANGLE_TOL
                or abs(_number(after.one("BaseSlope")[1])) > ANGLE_TOL):
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
    # Identity is the SubArray owner set, not a one-subarray-per-orientation count: several owners
    # may reference one orientation, so compare the preserved owner identities directly.
    if set(originals) != set(saved):
        raise ValueError("Saved variant changed the source subarray identities")
    if {_integer(block.one("NoOrientation")[1]) for block in originals.values()} != set(orientation_ids):
        raise ValueError("Source subarrays must each own exactly one declared source orientation")
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
        declared = ["PNomAC_InMPPT" in block.fields for block in (before, after)]
        if declared[0] != declared[1] or (declared[0] and (
                _number(before.one("PNomAC_InMPPT")[1]) != _number(after.one("PNomAC_InMPPT")[1]))):
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
    by_orientation = defaultdict(list)
    for block in target_blocks:
        if block.object_type == "pvShdArrayTable":
            by_orientation[_integer(block.one("NoOrient")[1])].append(block)
    if set(by_orientation) != set(orientation_ids):
        raise ValueError("Each source orientation must own native PV tables")
    for plane in planes:
        owned = by_orientation[plane["id"]]
        if sum(_integer(table.one("NModInHeight")[1]) * _integer(table.one("NModInWidth")[1])
               for table in owned) != expected_by_orient[plane["id"]]:
            raise ValueError("Native PV tables differ from their source orientation count")
        for table in owned:
            if (abs(_number(table.one("DInclin")[1]) - plane["tilt_deg"]) > ANGLE_TOL
                    or abs(_number(table.one("DAzimut")[1]) - plane["field_azimuth_deg"]) > ANGLE_TOL
                    or abs(_number(table.one("BaseSlope")[1])) > ANGLE_TOL):
                raise ValueError("Native PV table differs from its source orientation angles")
    return {"source_hardware_preserved": True, "source_orientations_preserved": True,
            "source_circuit_preserved": True, "source_orientation_ids": orientation_ids,
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


def verify_house_geometry(blocks, lines, house, source, *, anchor_turn=None):
    """Match every source mesh by world position and oriented polygon boundaries.

    anchor_turn applies the proven native anchor frame rotation (360 - orientation-1
    COLLADA azimuth) for non-zero anchors. When it is None the recorded zero-anchor path
    is kept (turn 180, source field azimuth zero).
    """
    if house.get("kind") != "mesh":
        raise ValueError("House coordinate verification requires a mesh house")
    if anchor_turn is None:
        if source["field_azimuth_deg"] != 0:
            raise ValueError("House coordinate verification is recorded only for mesh scenes with source anchor azimuth zero")
        anchor_turn = 180.0
    native = [(block, *_native_mesh(block, lines)) for block in blocks if block.object_type == "pvShdGenObject"]
    if len(native) != len(house["meshes"]):
        raise ValueError("Native and source house mesh counts differ")
    mappings, max_residual = [], 0.0
    for part in house["meshes"]:
        expected = [_rotate_z(tuple(point), anchor_turn) for point in part["vertices"]]
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
            "house_geometry_scope": "source mesh positions and oriented planar boundaries; anchor frame rotation"}


def verify_pv_geometry(blocks, tables, modules, source, *, anchor_turn=None):
    """Match source regions to explicitly repositioned native tables, then check their links.

    anchor_turn applies the proven native anchor frame rotation for non-zero anchors; when
    None the recorded zero-anchor path is kept (turn 180, source field azimuth zero).
    """
    source_planes = source.get("orientations") or []
    if source_planes:
        planes, expected_by_orient = {}, {}
        for plane in source_planes:
            planes.setdefault(plane["id"], plane)
            expected_by_orient[plane["id"]] = expected_by_orient.get(plane["id"], 0) + plane["module_total"]
        if len(tables) < 1 or any(table.get("orientation_id") not in planes for table in tables):
            raise ValueError("PV coordinate verification requires source-oriented tables for the declared orientations")
        per_orientation = defaultdict(int)
        for table in tables:
            per_orientation[table["orientation_id"]] += table["module_rows"] * table["module_columns"]
        for identifier, plane in planes.items():
            if per_orientation.get(identifier, 0) != expected_by_orient[identifier]:
                raise ValueError("Source tables for orientation %d must sum to the source module total" % identifier)
        grouped = True
    else:
        planes = {1: {"id": 1, "module_total": source["module_total"],
                      "tilt_deg": source["tilt_deg"], "azimuth_deg": source["azimuth_deg"],
                      "field_azimuth_deg": source["field_azimuth_deg"]}}
        if not 1 <= len(tables) or any(table.get("orientation_id", 1) != 1 for table in tables):
            raise ValueError("PV coordinate verification requires single-orientation tables")
        grouped = False
    if anchor_turn is None:
        if source["field_azimuth_deg"] != 0:
            raise ValueError("PV coordinate verification requires anchor azimuth zero")
        anchor_turn = 180.0
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
        if len(panel) != 3:
            raise ValueError("Native PV module name is not a name;width;length record")
        dimensions = {key: _number(block.one(key)[1]) for key in (
            "LongShed", "LargShed", "ModSpacingX", "ModSpacingY")}
        dimensions.update(module_width_m=_number(panel[1]), module_length_m=_number(panel[2]))
        if (any(dimensions[key] <= 0 for key in ("LongShed", "LargShed", "module_width_m", "module_length_m"))
                or any(dimensions[key] < 0 for key in ("ModSpacingX", "ModSpacingY"))):
            raise ValueError("Native PV dimensions/spacing are outside the source geometry range")
        native.append({"block": block, "origin": origin, "dimensions": dimensions,
                       "panel_name": panel[0],
                       "angles": {key: _number(block.one(key)[1]) for key in ("DInclin", "DAzimut", "BaseSlope")},
                       "rows": _integer(block.one("NModInHeight")[1]),
                       "columns": _integer(block.one("NModInWidth")[1]),
                       "orientation": block.one("ModuleOrient")[1]})
    if len(native) != len(tables):
        raise ValueError("PV verification requires the saved native table count to match the declaration")
    if not grouped:
        # Real count invariant: positive cells per table, summing to the source module total
        # (no fixed table-count cap; strings/cells may be distributed over any legal tables).
        if any(table["module_rows"] < 1 or table["module_columns"] < 1 for table in tables):
            raise ValueError("Single-orientation PV tables require positive module cells")
        if sum(table["module_rows"] * table["module_columns"] for table in tables) != source["module_total"]:
            raise ValueError("Single-orientation PV tables must sum to the source module total")
    mappings, max_origin, max_dimension, max_angle = [], 0.0, 0.0, 0.0
    for table in tables:
        identifier = table.get("orientation_id", 1)
        plane = planes[identifier]
        if grouped:
            if (table["tilt_deg"] != plane["tilt_deg"]
                    or table["azimuth_deg"] != plane["azimuth_deg"]
                    or table["rotation_deg"] != source["azimuth_deg"] - plane["azimuth_deg"]):
                raise ValueError("Source table profile differs from its source orientation")
        elif (table["tilt_deg"] != plane["tilt_deg"]
                or table.get("azimuth_deg", source["azimuth_deg"]) != plane["azimuth_deg"]
                or table.get("rotation_deg", 0.0) != source["azimuth_deg"] - plane["azimuth_deg"]):
            raise ValueError("Source table profile differs from its source plane")
        origin = [float(_num(value)) for value in table["origin_m"]]
        expected_origin = list(_rotate_z(tuple(origin), anchor_turn))
        pan_modules = table.get("module_geometry", modules)
        expected_dimensions = {"LongShed": table["column_span_m"], "LargShed": table["slope_span_m"],
                               "ModSpacingX": table["column_spacing_m"], "ModSpacingY": table["row_spacing_m"],
                               "module_width_m": pan_modules["width_m"], "module_length_m": pan_modules["length_m"]}
        expected_dimensions = {key: float(_num(value)) for key, value in expected_dimensions.items()}
        expected_angles = {"DInclin": float(_num(plane["tilt_deg"])),
                           "DAzimut": float(_num(plane["field_azimuth_deg"])), "BaseSlope": 0.0}
        orientation = {"landscape": "moLandScape", "portrait": "moPortrait"}[table["orientation"]]
        matches = []
        for index, record in enumerate(native):
            if (record["rows"] != table["module_rows"] or record["columns"] != table["module_columns"]
                    or record["orientation"] != orientation
                    or record["panel_name"].casefold() != pan_modules["name"].casefold()):
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
            "pv_geometry_scope": "source origins, planes, dimensions, module layout and NoOrient links; anchor frame rotation"}


def _verify_narrow_module_layout(blocks, lines):
    """Check a saved native module layout: the two-array / one-string-per-table profile, or
    the narrow single-orientation / one-subarray two-table / two-global-string profile.
    Shading physics and native consumption are excluded."""
    owners = [block for block in blocks if block.object_type == "pvModuleLayout"]
    arrays = _indexed(blocks, "pvSubArray", "SubArrayId")
    tables = [block for block in blocks if block.object_type == "pvShdArrayTable"]
    layouts = [block for block in blocks if block.object_type == "pvMLTable"]
    single = list(arrays) == [1]
    expected_arrays = [1] if single else [1, 2]
    if (len(owners) != 1 or owners[0].path != "PVObject_ModuleLayout"
            or list(arrays) != expected_arrays or len(tables) != 2 or len(layouts) != 2
            or owners[0].one("ModLayoutGlobState")[1] not in ("mlDefStrings", "mlDefInverters")
            or owners[0].one("SubFieldsOrigin")[1] != "so3DShadings"):
        raise ValueError("Module Layout review requires two native 3D tables and defined source strings")
    planes = _indexed(blocks, "pvOrient", "NoOrient")
    if single:
        if set(planes) != {1} or {_integer(array.one("NoOrientation")[1]) for array in arrays.values()} != {1}:
            raise ValueError("Single-orientation module layout requires one source plane and one source subarray")
    elif set(planes) != {1, 2} or {_integer(array.one("NoOrientation")[1]) for array in arrays.values()} != {1, 2}:
        raise ValueError("Module Layout source arrays must own two existing distinct orientations 1/2")
    for plane in planes.values():
        if (plane.one("FieldType")[1] != "FixedPlane" or plane.one("Flags")[1] != "$00"
                or any(plane.one(key)[1] != "False" for key in ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver"))):
            raise ValueError("Module Layout review requires ordinary non-averaged fixed source planes")
    bounds = {key: (array.line - 1, array.end_line - 1) for key, array in arrays.items()}
    if inventory(lines, bounds)["counts_valid"] is not True:
        raise ValueError("Module Layout source circuit counts or ownership are incomplete")
    body = lines[owners[0].line:owners[0].end_line - 1]
    declared = [int(match[1]) for line in body
                if (match := re.fullmatch(r"\s*SubFields, list of=([0-9]+) PVObjects\s*", line))]
    if declared != [2] or sum(line.strip() == "End of SubFields" for line in body) != 1:
        raise ValueError("Module Layout declared subfield list does not match its tables")
    created = _indexed(blocks, "pvShdArrayTable", "NoObjCreated")
    scene_objects = [block for block in blocks
                     if re.fullmatch(r"PVObject_Ombrage/PVObject_[1-9][0-9]*", block.path)]
    identities = [_integer(block.one("NoObjCreated")[1]) for block in scene_objects]
    if len(created) != 2 or 0 in identities or len(identities) != len(set(identities)):
        raise ValueError("Module Layout requires unique native object creation references")
    result, used, subfields, used_strings = [], set(), set(), set()
    for layout in layouts:
        if layout.path.rsplit("/", 1)[0] != owners[0].path:
            raise ValueError("Module Layout table lies outside its owning layout")
        subfield = _integer(layout.one("NoSubField")[1])
        if subfield not in (1, 2) or subfield in subfields:
            raise ValueError("Module Layout has a missing or repeated subfield identity")
        subfields.add(subfield)
        identifier = _integer(layout.one("NoSubArrayPar")[1])
        if identifier not in arrays or (not single and identifier in used):
            raise ValueError("Module Layout has a missing or repeated source array")
        used.add(identifier)
        array = arrays[identifier]
        rows, columns = (_integer(layout.one(key)[1]) for key in ("NLigMod", "NColMod"))
        count = rows * columns
        if (not rows or not columns or not 1 <= count <= 100
                or _integer(array.one("NModSerie")[1]) != count):
            raise ValueError("Module Layout requires one string of NModSerie modules per table")
        if single:
            if (_integer(array.one("NStringCh")[1]) != 2 or _integer(array.one("NInverter")[1]) != 2
                    or _integer(array.one("NInvMPPT")[1]) != 2 or "NStrOrient1" not in array.fields
                    or _integer(array.one("NStrOrient1")[1]) != 2):
                raise ValueError("Single-orientation module layout requires two inverters, two strings and two total MPPT inputs")
        elif any(array.one(key)[1] != "1" for key in ("NStringCh", "NInverter", "NInvMPPT")):
            raise ValueError("Module Layout review requires one independent string of 1..100 modules per array")
        if "PowerSharingInverterId" in array.fields and array.one("PowerSharingInverterId")[1] != "-1":
            raise ValueError("Module Layout single-string review excludes shared inverter inputs")
        plane = planes[_integer(array.one("NoOrientation")[1])]
        reference = _integer(layout.one("NoField3DCreated")[1])
        table = created.get(reference)
        if (table is None or table.one("Version")[1] != "8.1.6"
                or table.one("Source")[1] != "osPVCollada"
                or table.path.rsplit("/", 1)[-1] != "PVObject_" + layout.one("NoField3D")[1]
                or layout.one("NoShed3D")[1] != "1" or layout.one("NIdentical")[1] != "1"
                or _integer(table.one("NoOrient")[1]) != _integer(array.one("NoOrientation")[1])
                or _integer(table.one("NModInHeight")[1]) != rows
                or _integer(table.one("NModInWidth")[1]) != columns
                or layout.one("ModLayoutState")[1] != "mlDefStrings"
                or layout.one("ModOrient")[1] != "moLandScape"
                or table.one("ModuleOrient")[1] != "moLandScape"):
            raise ValueError("Module Layout table/array/3D references or mechanical layout disagree")
        for table_key, plane_key in (("DInclin", "FieldTilt"), ("DAzimut", "FieldAzim"), ("BaseSlope", "BaseSlope")):
            if abs(_number(table.one(table_key)[1]) - _number(plane.one(plane_key)[1])) > ANGLE_TOL:
                raise ValueError("Module Layout PV table angles disagree with their owning source plane")
        for key in ("ModSpacingX", "ModSpacingY"):
            if _number(layout.one(key)[1]) != 0 or _number(table.one(key)[1]) != 0:
                raise ValueError("Module Layout review is bounded to zero-spacing native tables")
        if _number(layout.one("RowPadding")[1]) != 0:
            raise ValueError("Module Layout review requires zero native row padding")
        body = lines[layout.line:layout.end_line - 1]
        declared = [int(match[1]) for line in body
                    if (match := re.fullmatch(r"\s*Modules, list of=([0-9]+) tModule\s*", line))]
        fields = [key for key in layout.fields if key.startswith("Module_")]
        if (declared != [count] or sum(line.strip() == "End of List Modules" for line in body) != 1
                or fields != ["Module_%d" % index for index in range(1, count + 1)]):
            raise ValueError("Module Layout declared module count or records are missing or unexpected")
        cells, strings_seen = set(), set()
        for index in range(1, count + 1):
            raw = layout.one("Module_%d" % index)[1]
            if not re.fullmatch(r"[1-9][0-9]*,[1-9][0-9]*,[1-9][0-9]*,[1-9][0-9]*,", raw):
                raise ValueError("Module Layout requires complete positive native module/string records")
            sequence, row, column, string = map(int, raw.rstrip(",").split(","))
            if (sequence != index or not 1 <= row <= rows or not 1 <= column <= columns
                    or (row, column) in cells):
                raise ValueError("Module Layout contains an unassigned or repeated module cell")
            cells.add((row, column))
            strings_seen.add(string)
        if single:
            if len(strings_seen) != 1:
                raise ValueError("Each single-orientation layout table must hold exactly one global string")
            string_id = strings_seen.pop()
            if string_id not in (1, 2) or string_id in used_strings:
                raise ValueError("Single-orientation module layout must assign global strings 1 and 2 once each")
            used_strings.add(string_id)
        else:
            string_id = identifier
            if strings_seen != {identifier}:
                raise ValueError("Module Layout contains an unassigned, repeated or mismatched module/string cell")
        result.append({"object_path": layout.path, "subarray_id": identifier,
                       "orientation_id": _integer(array.one("NoOrientation")[1]),
                       "native_table_path": table.path, "native_created_id": reference,
                       "rows": rows, "columns": columns, "module_count": count,
                       "string_id": string_id, "state": layout.one("ModLayoutState")[1],
                       "module_orientation": layout.one("ModOrient")[1]})
    if single and used_strings != {1, 2}:
        raise ValueError("Single-orientation module layout must assign both global strings 1 and 2 exactly")
    return {"configured": True, "status": "stored_consistent", "tables": result,
            "global_state": owners[0].one("ModLayoutGlobState")[1],
            "module_count": sum(row["module_count"] for row in result),
            "native_consumption_verified": False,
            "scope": "Stored native8.1.6 landscape/zero-spacing layout: two-array one-string-per-table, or "
                     "single-orientation two-table two-global-string; simulation mode and consumption are separate"}


# ---------------------------------------------------------------------------
# General per-cell string-layout verification (round03)
#
# The native pvMLTable record is a per-cell projection: every module cell carries a
# global string ordinal, so a string may span several tables and one table may hold
# several strings.  Capacity is bound to the global string (== the source subarray
# NModSerie), never to the table.  The two narrow profiles above stay on their proven
# strict path; this covers the general single-subarray shape, e.g. three tables of
# twelve cells with gid1 = A12 + B6 and gid2 = B6 + C12.

_MODULE_RECORD = re.compile(r"([0-9]+),([0-9]+),([0-9]+),([0-9]+),")


def _review(issues):
    return {"configured": True, "status": "needs_review", "issues": list(issues),
            "native_consumption_verified": False,
            "scope": "Stored layout is outside the verified native profile or its references are inconsistent"}


def _matches_single_narrow(arrays, tables, layouts):
    """True when the stored record is exactly the legacy single-orientation template, so the
    original strict checks (not the general capacity invariants) keep owning it."""
    try:
        if len(tables) != 2 or len(layouts) != 2:
            return False
        sub = next(iter(arrays.values()))
        capacity = _integer(sub.one("NModSerie")[1])
        strings = []
        for layout in layouts:
            rows = _integer(layout.one("NLigMod")[1])
            columns = _integer(layout.one("NColMod")[1])
            if rows * columns != capacity:
                return False
            seen = set()
            for index in range(1, rows * columns + 1):
                match = _MODULE_RECORD.fullmatch(layout.one("Module_%d" % index)[1])
                if match is None:
                    return False
                seen.add(match[4])
            if len(seen) != 1:
                return False
            strings.append(next(iter(seen)))
        return sorted(int(value) for value in strings) == [1, 2]
    except (ValueError, KeyError, OverflowError):
        return False


def _matches_dual_narrow(arrays, tables, layouts):
    """True when a two-array stored record is the legacy one-string-per-array template with one
    table per orientation (every source array declares exactly one string), so the original
    strict verifier keeps owning it and adjudicates any corruption in place.  A two-array record
    whose source arrays declare two or more strings, or that splits an orientation over more than
    one table, is the general per-cell profile and takes the capacity invariants instead."""
    try:
        if set(arrays) != {1, 2} or len(tables) != 2 or len(layouts) != 2:
            return False
        return all(_integer(array.one("NStringCh")[1]) == 1 for array in arrays.values())
    except (ValueError, KeyError, OverflowError):
        return False


def _assess_general_module_layout(blocks, lines):
    """Classify a general per-cell stored layout as stored_consistent, draft_incomplete or
    invalid.  Supports one or more source subarrays, each on its own ordinary fixed plane, with
    every module cell bound to a source GLOBAL string ordinal (capacity = the owning subarray's
    NModSerie, never the table).  Each owner is validated independently for one exclusive
    single-orientation string branch; the PAN is per owner (homogeneous or heterogeneous) and a
    different OND per subarray is inherited electrical identity, not a shared module geometry.
    Unsupported profiles (portrait, spacing, padding, sharing, non-fixed planes) raise so the
    caller reports needs_review instead of a false stored state."""
    owners = [block for block in blocks if block.object_type == "pvModuleLayout"]
    if (len(owners) != 1 or owners[0].path != "PVObject_ModuleLayout"
            or owners[0].one("ModLayoutGlobState")[1] not in MODULE_LAYOUT_GLOBAL_STATES
            or owners[0].one("SubFieldsOrigin")[1] != "so3DShadings"):
        raise ValueError("Module Layout owner record is not a defined native module layout")
    arrays = _indexed(blocks, "pvSubArray", "SubArrayId")
    planes = _indexed(blocks, "pvOrient", "NoOrient")
    tables = [block for block in blocks if block.object_type == "pvShdArrayTable"]
    layouts = [block for block in blocks if block.object_type == "pvMLTable"]
    if len(arrays) < 1:
        raise ValueError("General module layout requires at least one source subarray")
    # Per-owner structural validation for ANY number of source subarrays: each owner must keep
    # one exclusive single-orientation string branch. The old ">2 subarrays need the same PAN"
    # shortcut assumed one global modules dict; per-source geometry is now represented, so the
    # reader validates each owner instead of keying on group count or PAN homogeneity.
    for array in arrays.values():
        strings = _integer(array.one("NStringCh")[1])
        if ("NStrOrient1" not in array.fields
                or _integer(array.one("NStrOrient1")[1]) != strings
                or any(key.startswith("NStrOrient") and key != "NStrOrient1"
                       and array.one(key)[1] != "0" for key in array.fields)):
            raise ValueError("General module layout requires an exclusive single-orientation "
                             "string branch per source subarray")
    if len(layouts) < 1 or len(tables) != len(layouts):
        raise ValueError("General module layout requires one native 3D table per module-layout table")
    # Each source subarray owns one ordinary non-averaged fixed plane; shared inputs excluded.
    plane_of = {}
    for subarray_id, array in arrays.items():
        orientation = _integer(array.one("NoOrientation")[1])
        if orientation not in planes:
            raise ValueError("General module layout source subarray %d owns a missing orientation %d"
                             % (subarray_id, orientation))
        plane = planes[orientation]
        if (plane.one("FieldType")[1] != "FixedPlane" or plane.one("Flags")[1] != "$00"
                or any(plane.one(key)[1] != "False"
                       for key in ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver"))
                or abs(_number(plane.one("BaseSlope")[1])) > ANGLE_TOL):
            raise ValueError("General module layout requires ordinary non-averaged fixed source planes")
        if "PowerSharingInverterId" in array.fields and array.one("PowerSharingInverterId")[1] != "-1":
            raise ValueError("General module layout excludes shared inverter inputs")
        plane_of[subarray_id] = (orientation, plane)
    ranges = source_string_ranges(blocks)
    expected_total = sum((last - first + 1) * capacity
                         for first, last, capacity in ranges.values())

    body = lines[owners[0].line:owners[0].end_line - 1]
    declared = [int(match[1]) for line in body
                if (match := re.fullmatch(r"\s*SubFields, list of=([0-9]+) PVObjects\s*", line))]
    issues, draft = [], False
    if declared != [len(layouts)] or sum(line.strip() == "End of SubFields" for line in body) != 1:
        issues.append("Declared subfield list does not match the module-layout tables")
    created = _indexed(tables, "pvShdArrayTable", "NoObjCreated")
    scene_objects = [block for block in blocks
                     if re.fullmatch(r"PVObject_Ombrage/PVObject_[1-9][0-9]*", block.path)]
    identities = [_integer(block.one("NoObjCreated")[1]) for block in scene_objects]
    if len(created) != len(tables) or 0 in identities or len(identities) != len(set(identities)):
        issues.append("Native object creation references are missing, repeated or zero")

    subfields, rows_out, string_totals, total_cells = set(), [], defaultdict(int), 0
    for layout in layouts:
        if layout.path.rsplit("/", 1)[0] != owners[0].path:
            issues.append("Module-layout table lies outside its owning layout: " + layout.path)
            continue
        subfield = _integer(layout.one("NoSubField")[1])
        if subfield in subfields:
            issues.append("Repeated subfield identity %d" % subfield)
        subfields.add(subfield)
        identifier = _integer(layout.one("NoSubArrayPar")[1])
        if identifier not in arrays:
            issues.append("Module-layout table references an unknown source subarray %d" % identifier)
            continue
        orientation, plane = plane_of[identifier]
        first, last, capacity = ranges[identifier]
        rows = _integer(layout.one("NLigMod")[1])
        columns = _integer(layout.one("NColMod")[1])
        if rows < 1 or columns < 1:
            issues.append("Module-layout table has a non-positive grid: " + layout.path)
            continue
        reference = _integer(layout.one("NoField3DCreated")[1])
        table = created.get(reference)
        if (table is None or (table.one("Source")[1], table.one("Version")[1]) not in PV_TABLE_SOURCES
                or table.path.rsplit("/", 1)[-1] != "PVObject_" + layout.one("NoField3D")[1]
                or layout.one("NoShed3D")[1] != "1" or layout.one("NIdentical")[1] != "1"
                or _integer(table.one("NoOrient")[1]) != orientation
                or _integer(table.one("NModInHeight")[1]) != rows
                or _integer(table.one("NModInWidth")[1]) != columns):
            issues.append("Module-layout table/array/3D reference or mechanical layout disagrees: " + layout.path)
            continue
        if (layout.one("ModLayoutState")[1] not in MODULE_LAYOUT_TABLE_STATES
                or layout.one("ModOrient")[1] != "moLandScape"
                or table.one("ModuleOrient")[1] != "moLandScape"):
            raise ValueError("General module layout supports only landscape tables with a defined-string "
                             "or computed-inverter state")
        if (_number(layout.one("ModSpacingX")[1]) != 0 or _number(layout.one("ModSpacingY")[1]) != 0
                or _number(layout.one("RowPadding")[1]) != 0
                or _number(table.one("ModSpacingX")[1]) != 0 or _number(table.one("ModSpacingY")[1]) != 0):
            raise ValueError("General module layout excludes non-zero native spacing or row padding")
        for table_key, plane_key in (("DInclin", "FieldTilt"), ("DAzimut", "FieldAzim"), ("BaseSlope", "BaseSlope")):
            if abs(_number(table.one(table_key)[1]) - _number(plane.one(plane_key)[1])) > ANGLE_TOL:
                issues.append("PV table angles disagree with the source plane for " + layout.path)
        total_cells += rows * columns

        section = lines[layout.line:layout.end_line - 1]
        declared_cells = [int(match[1]) for line in section
                          if (match := re.fullmatch(r"\s*Modules, list of=([0-9]+) tModule\s*", line))]
        present = sorted(int(key[7:]) for key in layout.fields
                         if key.startswith("Module_") and key[7:].isdigit())
        cells, seen = [], set()
        if present != list(range(1, len(present) + 1)):
            issues.append("Module records are not a contiguous 1..N run in " + layout.path)
        elif declared_cells != [len(present)]:
            issues.append("Declared module count disagrees with %d records in %s" % (len(present), layout.path))
        else:
            for index in present:
                match = _MODULE_RECORD.fullmatch(layout.one("Module_%d" % index)[1])
                if match is None:
                    issues.append("Module record Module_%d is not a complete cell tuple" % index)
                    continue
                sequence, row, column, glob = (int(value) for value in match.groups())
                if sequence != index or not 1 <= row <= rows or not 1 <= column <= columns:
                    issues.append("Module record Module_%d has an out-of-range sequence or cell" % index)
                    continue
                if (row, column) in seen:
                    issues.append("Module record Module_%d repeats cell (%d,%d)" % (index, row, column))
                    continue
                seen.add((row, column))
                cells.append((row, column, glob))
                if glob == 0:
                    draft = True
                elif glob < first or glob > last:
                    issues.append("Module record Module_%d global string %d is outside source subarray "
                                  "%d's declared range %d..%d" % (index, glob, identifier, first, last))
                else:
                    string_totals[glob] += 1
            if len(seen) < rows * columns:
                draft = True
        rows_out.append({"object_path": layout.path, "subarray_id": identifier,
                         "orientation_id": orientation,
                         "native_table_path": table.path, "native_created_id": reference,
                         "rows": rows, "columns": columns, "module_count": rows * columns,
                         "string_ids": sorted({cell[2] for cell in cells if cell[2] > 0}),
                         "state": layout.one("ModLayoutState")[1],
                         "module_orientation": layout.one("ModOrient")[1]})

    if set(subfields) != set(range(1, len(layouts) + 1)):
        issues.append("Subfield identities do not cover 1..%d exactly once" % len(layouts))
    for subarray_id, (first, last, capacity) in ranges.items():
        for glob in range(first, last + 1):
            have = string_totals.get(glob, 0)
            if have > capacity:
                issues.append("Global string %d holds %d modules, above the %d-module capacity"
                              % (glob, have, capacity))
            elif have < capacity:
                draft = True
    if total_cells > expected_total:
        issues.append("Module-layout tables declare %d cells, above the %d-module source total"
                      % (total_cells, expected_total))
    elif total_cells < expected_total:
        draft = True

    status = "invalid" if issues else ("draft_incomplete" if draft else "stored_consistent")
    result = {"configured": True, "status": status, "tables": rows_out,
              "global_state": owners[0].one("ModLayoutGlobState")[1],
              "module_count": total_cells, "native_consumption_verified": False,
              "scope": "Stored native per-cell string layout: capacity bound to the global string "
                       "(== source NModSerie); strings may span tables, a table may hold several, "
                       "and each global belongs to exactly one source subarray"}
    if issues:
        result["issues"] = issues
    if status == "draft_incomplete":
        result["unassigned_note"] = ("Some module cells are unassigned (global 0) or the tables are "
                                     "incomplete; storable draft, not importable")
    return result


def assess_module_layout(blocks, lines):
    """Read-only classifier for a stored module layout: stored_consistent, draft_incomplete,
    invalid or needs_review.  Never raises for a present layout, so a raw VC without a recipe
    can still be judged.

    The dispatch keys on the real source structure (distinct positive ``SubArrayId`` values in
    appearance order), not a ``{1, 2}`` numeric whitelist: the legacy one-string-per-array and
    single-orientation templates still own ``{1, 2}`` / ``[1]`` through the narrow matchers,
    while any other pair (e.g. sparse ``7, 2``) or more than two groups takes the general
    per-cell path whose capacities come from ``source_string_ranges`` (position/prefix order,
    never a numeric id sort).  This matches the explicit builder's source-data range and keeps
    the legacy None ``{1, 2}`` gate in ``_support_gate`` untouched."""
    owners = [block for block in blocks if block.object_type == "pvModuleLayout"]
    if not owners:
        return {"configured": False, "status": "absent", "native_consumption_verified": False}
    try:
        arrays = _indexed(blocks, "pvSubArray", "SubArrayId")
        tables = [block for block in blocks if block.object_type == "pvShdArrayTable"]
        layouts = [block for block in blocks if block.object_type == "pvMLTable"]
        ids = list(arrays)
        if len(ids) == 2 and _matches_dual_narrow(arrays, tables, layouts):
            return _verify_narrow_module_layout(blocks, lines)
        if len(ids) == 1 and ids == [1] and _matches_single_narrow(arrays, tables, layouts):
            return _verify_narrow_module_layout(blocks, lines)
        if len(ids) >= 1:
            return _assess_general_module_layout(blocks, lines)
        return _review(["Module Layout has no source subarrays"])
    except (ValueError, KeyError, OverflowError) as exc:
        return _review([str(exc)])


def verify_module_layout(blocks, lines):
    """Strict stored-layout check used by native import and the GUI verifier: return the
    classification only when it is stored_consistent, else raise with its issues."""
    result = assess_module_layout(blocks, lines)
    if result.get("status") != "stored_consistent":
        raise ValueError("; ".join(result.get("issues") or [result.get("status", "unverified module layout")]))
    return result

"""Read-only checks of native grouped scene records against their authored source."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from itertools import product

from pvsyst_auto.project.circuit import circuit_nodes, inventory
from pvsyst_auto.scene.author import MAX_VERTICES, _dot, _num, _rotate_z, _sub
from pvsyst_auto.versions import NATIVE_TEXT, NATIVE_TEXT_OR_SEED, PV_TABLE_SOURCES

COORD_TOL = 2e-5
NORMAL_TOL = 1e-7
ANGLE_TOL = 1e-6
MODULE_LAYOUT_GLOBAL_STATES = frozenset({"mlDefStrings", "mlDefInverters", "mlShd3DCalculated"})
MODULE_LAYOUT_TABLE_STATES = frozenset({"mlDefStrings", "mlDefInverters"})
_MODULE_RECORD = re.compile(r"([0-9]+),([0-9]+),([0-9]+),([0-9]+),")

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

def _is_ordinary_fixed_plane(plane, *, check_base_slope=False):
    return (plane.one("FieldType")[1] == "FixedPlane" and plane.one("Flags")[1] == "$00"
            and not any(plane.one(k)[1] != "False" for k in ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver"))
            and (not check_base_slope or abs(_number(plane.one("BaseSlope")[1])) <= ANGLE_TOL))

def _table_plane_angles_match(table, plane):
    return all(abs(_number(table.one(tk)[1]) - _number(plane.one(pk)[1])) <= ANGLE_TOL
               for tk, pk in (("DInclin", "FieldTilt"), ("DAzimut", "FieldAzim"), ("BaseSlope", "BaseSlope")))

def _check_ml_spacing_padding(layout, table, spacing_err, padding_err):
    for key in ("ModSpacingX", "ModSpacingY"):
        ml_sp, tb_sp = _number(layout.one(key)[1]), _number(table.one(key)[1])
        if ml_sp < 0 or tb_sp < 0 or abs(ml_sp - tb_sp) > COORD_TOL:
            raise ValueError(spacing_err)
    if _number(layout.one("RowPadding")[1]) != 0:
        raise ValueError(padding_err)

def source_string_ranges(blocks):
    """Recover each source subarray's declared GLOBAL string range in appearance order."""
    arrays = _indexed(blocks, "pvSubArray", "SubArrayId")
    result, first = {}, 1
    for identifier, block in arrays.items():
        strings, capacity = _integer(block.one("NStringCh")[1]), _integer(block.one("NModSerie")[1])
        if strings < 1 or capacity < 1:
            raise ValueError("Source subarray %d requires positive NStringCh and NModSerie" % identifier)
        last = first + strings - 1
        result[identifier] = (first, last, capacity)
        first = last + 1
    return result

def verify_grouped_hardware(source_blocks, target_blocks, source_lines, target_lines, source):
    """Preserve source orientations, fixed planes, hardware, and exclusive circuit branches."""
    planes = source.get("orientations", [])
    expected_by_orient = {}
    if planes:
        distinct = {}
        for plane in planes:
            ident = plane["id"]
            if ident < 1:
                raise ValueError("Grouped verification requires positive source orientations")
            distinct.setdefault(ident, plane)
            expected_by_orient[ident] = expected_by_orient.get(ident, 0) + plane["module_total"]
        planes = list(distinct.values())
    else:
        planes = [{"id": 1, "module_total": source["module_total"], "tilt_deg": source["tilt_deg"], "field_azimuth_deg": source["field_azimuth_deg"]}]
        expected_by_orient = {1: source["module_total"]}
    orientation_ids = [p["id"] for p in planes]
    originals, saved = _indexed(source_blocks, "pvOrient", "NoOrient"), _indexed(target_blocks, "pvOrient", "NoOrient")
    if set(originals) != set(orientation_ids) or set(saved) != set(orientation_ids):
        raise ValueError("Saved variant must preserve exactly the source orientations")
    orientation_mapping = []
    for plane in planes:
        ident = plane["id"]
        before, after = originals[ident], saved[ident]
        if before.one("FieldType")[1] != "FixedPlane" or after.one("FieldType")[1] != "FixedPlane":
            raise ValueError("Saved source orientation is not an ordinary fixed plane")
        for key in ("FieldTilt", "FieldAzim", "BaseSlope"):
            if abs(_number(before.one(key)[1]) - _number(after.one(key)[1])) > ANGLE_TOL:
                raise ValueError("Saved orientation %d changed %s" % (ident, key))
        if (abs(_number(after.one("FieldTilt")[1]) - plane["tilt_deg"]) > ANGLE_TOL
                or abs(_number(after.one("FieldAzim")[1]) - plane["field_azimuth_deg"]) > ANGLE_TOL
                or abs(_number(after.one("BaseSlope")[1])) > ANGLE_TOL):
            raise ValueError("Saved orientation differs from its source profile")
        for key in ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver"):
            if before.one(key)[1] != "False" or after.one(key)[1] != "False":
                raise ValueError("Grouped verification requires unchanged ordinary plane modes")
        orientation_mapping.append({"orientation_id": ident, "field_type": after.one("FieldType")[1],
                                    "tilt_deg": _number(after.one("FieldTilt")[1]),
                                    "field_azimuth_deg": _number(after.one("FieldAzim")[1]),
                                    "base_slope_deg": _number(after.one("BaseSlope")[1])})
    originals, saved = _indexed(source_blocks, "pvSubArray", "SubArrayId"), _indexed(target_blocks, "pvSubArray", "SubArrayId")
    if set(originals) != set(saved):
        raise ValueError("Saved variant changed the source subarray identities")
    if {_integer(b.one("NoOrientation")[1]) for b in originals.values()} != set(orientation_ids):
        raise ValueError("Source subarrays must each own exactly one declared source orientation")
    hardware_mapping = []
    for ident, before in originals.items():
        after = saved[ident]
        for key in ("PVModule", "GInverter"):
            if before.one(key)[1].casefold() != after.one(key)[1].casefold():
                raise ValueError("Saved subarray %d changed %s" % (ident, key))
        for key in ("NModSerie", "NStringCh", "NInverter", "NInvMPPT", "NoOrientation"):
            if _integer(before.one(key)[1]) != _integer(after.one(key)[1]):
                raise ValueError("Saved subarray %d changed %s" % (ident, key))
        for key in ("Flags", "Flags2"):
            vals = [b.one(key)[1] if key in b.fields else "$0" for b in (before, after)]
            if any(not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", v) for v in vals) or int(vals[0][1:], 16) != int(vals[1][1:], 16):
                raise ValueError("Saved subarray %d changed %s" % (ident, key))
        for key in set(before.fields) | set(after.fields):
            if re.fullmatch(r"NStrOrient[0-9]+", key):
                vals = [b.one(key)[1] if key in b.fields else "0" for b in (before, after)]
                if _integer(vals[0]) != _integer(vals[1]):
                    raise ValueError("Saved subarray %d changed %s" % (ident, key))
        for key, default in (("PowerSharingInverterId", "-1"), ("SEIsHeterogeneousString", "False,")):
            vals = [b.one(key)[1] if key in b.fields else default for b in (before, after)]
            if vals[0].rstrip(",") != vals[1].rstrip(","):
                raise ValueError("Saved subarray %d changed %s" % (ident, key))
        declared = ["PNomAC_InMPPT" in b.fields for b in (before, after)]
        if declared[0] != declared[1] or (declared[0] and _number(before.one("PNomAC_InMPPT")[1]) != _number(after.one("PNomAC_InMPPT")[1])):
            raise ValueError("Saved subarray changed its inverter input power declaration")
        hardware_mapping.append({
            "subarray_id": ident, "orientation_id": _integer(after.one("NoOrientation")[1]),
            "pan": after.one("PVModule")[1], "ond": after.one("GInverter")[1],
            "modules_in_series": _integer(after.one("NModSerie")[1]), "strings": _integer(after.one("NStringCh")[1]),
            "inverters": _integer(after.one("NInverter")[1]), "mppt_inputs": _integer(after.one("NInvMPPT")[1]),
        })

    def circuit(node):
        return (node.kind, node.one("SubArrayId", ""), node.one("SubArrayName", ""),
                _integer(node.one("NElements", "1")), tuple(sorted(circuit(c) for c in node.children)))

    if tuple(sorted(circuit(n) for n in circuit_nodes(source_lines))) != tuple(sorted(circuit(n) for n in circuit_nodes(target_lines))):
        raise ValueError("Saved variant changed the source circuit ownership or multiplicities")
    for lines, arrays in ((source_lines, originals), (target_lines, saved)):
        checked_inventory = inventory(lines, {k: (b.line - 1, b.end_line - 1) for k, b in arrays.items()})
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
        if sum(_integer(t.one("NModInHeight")[1]) * _integer(t.one("NModInWidth")[1]) for t in owned) != expected_by_orient[plane["id"]]:
            raise ValueError("Native PV tables differ from their source orientation count")
        for t in owned:
            if (abs(_number(t.one("DInclin")[1]) - plane["tilt_deg"]) > ANGLE_TOL
                    or abs(_number(t.one("DAzimut")[1]) - plane["field_azimuth_deg"]) > ANGLE_TOL
                    or abs(_number(t.one("BaseSlope")[1])) > ANGLE_TOL):
                raise ValueError("Native PV table differs from its source orientation angles")
    return {"source_hardware_preserved": True, "source_orientations_preserved": True, "source_circuit_preserved": True,
            "source_orientation_ids": orientation_ids, "orientation_mapping": orientation_mapping,
            "hardware_mapping": hardware_mapping,
            "circuit_mapping": [{"subarray_id": row["id"], **row["circuit"]} for row in checked_inventory["subarrays"]]}

def _native_mesh(block, lines):
    if block.one("Source")[1] != "osPVCollada" or block.one("Version")[1] not in NATIVE_TEXT:
        raise ValueError("House verification requires the recorded native PVCollada save version")
    if any(_number(block.one(k)[1]) != 0 for k in ("DInclin", "BaseSlope", "DAzimut")):
        raise ValueError("House verification requires the observed baked, zero-angle native mesh records")
    counts = [int(m[1]) for ln in lines[block.line:block.end_line - 1]
              if (m := re.fullmatch(r"\s*Sommets, TPoly3D=([0-9]+) Point3D\s*", ln))]
    if len(counts) != 1 or not 3 <= counts[0] <= MAX_VERTICES:
        raise ValueError("Native house record lacks one complete point list")
    if {k for k in block.fields if k.startswith("Point_")} != {f"Point_{n}" for n in range(1, counts[0] + 1)}:
        raise ValueError("Native point rows do not match their declared count")
    origin = [_number(v) for v in block.one("DOrigine")[1].split(",")]
    if len(origin) != 3:
        raise ValueError("Native house origin requires three coordinates")
    points = []
    for index in range(1, counts[0] + 1):
        local = [_number(v) for v in block.one(f"Point_{index}")[1].split(",")]
        if len(local) != 3:
            raise ValueError("Native house points require three coordinates")
        points.append([origin[ax] + local[ax] for ax in range(3)])
    raw = block.one("Faces")[1].rstrip(",").split(",")
    if any(not re.fullmatch(r"-1|[0-9]+", v) for v in raw):
        raise ValueError("Native face indices must be complete integers")
    values, faces = [int(v) for v in raw], []
    for value in values:
        if value == -1:
            faces.append([])
        elif not faces or not 0 <= value < len(points):
            raise ValueError("Native face indices are outside their point list")
        else:
            faces[-1].append(value)
    if (len(values) != _integer(block.one("NPElements")[1]) or len(faces) != _integer(block.one("NElements")[1])
            or any(len(f) < 3 or len(set(f)) != len(f) for f in faces)):
        raise ValueError("Native polygon counts or vertex loops are incomplete")
    return points, faces

def _bounds(points):
    return tuple(v for ax in range(3) for v in (min(p[ax] for p in points), max(p[ax] for p in points)))

def _face_normal(points, face):
    normal, origin = [0.0, 0.0, 0.0], points[face[0]]
    for start, end in zip(face, face[1:] + face[:1]):
        a, b = _sub(points[start], origin), _sub(points[end], origin)
        normal[0] += a[1] * b[2] - a[2] * b[1]
        normal[1] += a[2] * b[0] - a[0] * b[2]
        normal[2] += a[0] * b[1] - a[1] * b[0]
    length = math.hypot(*normal)
    if not math.isfinite(length) or length == 0:
        raise ValueError("Native polygon is degenerate")
    return [v / length for v in normal]

def _surface_loops(points, faces, planes, plane_buckets):
    edges = defaultdict(Counter)
    for face in faces:
        normal = _face_normal(points, face)
        distance = _dot(normal, points[face[0]])
        if any(abs(_dot(normal, points[i]) - distance) > COORD_TOL for i in face):
            raise ValueError("Native polygon is not planar at the comparison precision")
        cell = tuple(math.floor(v / NORMAL_TOL) for v in normal) + (math.floor(distance / COORD_TOL),)
        nearby = [i for shift in product((-1, 0, 1), repeat=4)
                  for i in plane_buckets.get(tuple(cell[ax] + shift[ax] for ax in range(4)), ())]
        matches = [i for i in nearby for other, offset in [planes[i]]
                   if max(abs(normal[ax] - other[ax]) for ax in range(3)) <= NORMAL_TOL
                   and all(abs(_dot(other, points[v]) - offset) <= COORD_TOL for v in face)]
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
        if any(c != 1 for c in remaining.values()):
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
                for idx, v in enumerate(loop):
                    a, b = _sub(points[v], points[loop[idx - 1]]), _sub(points[loop[(idx + 1) % len(loop)]], points[v])
                    cross = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
                    if not (_dot(a, b) >= 0 and math.hypot(*cross) <= COORD_TOL * (math.hypot(*a) + math.hypot(*b))):
                        simplified.append(v)
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
    points = list(dict.fromkeys(tuple(p) for p in expected))
    source_index = {p: i for i, p in enumerate(points)}
    triangles = [[source_index[tuple(expected[i])] for i in f] for f in triangles]
    buckets = defaultdict(list)
    for idx, p in enumerate(points):
        buckets[tuple(math.floor(v / COORD_TOL) for v in p)].append(idx)
    mapped, residual = [], 0.0
    for point in actual:
        cell = tuple(math.floor(v / COORD_TOL) for v in point)
        candidates = [idx for shift in product((-1, 0, 1), repeat=3)
                      for idx in buckets.get(tuple(cell[ax] + shift[ax] for ax in range(3)), ())
                      if max(abs(point[ax] - points[idx][ax]) for ax in range(3)) <= COORD_TOL]
        if len(candidates) != 1:
            return None
        mapped.append(candidates[0])
        residual = max(residual, max(abs(point[ax] - points[candidates[0]][ax]) for ax in range(3)))
    mapped_polygons = [[mapped[i] for i in f] for f in polygons]
    for face, mapped_face in zip(polygons, mapped_polygons):
        if len(set(mapped_face)) != len(mapped_face):
            return None
        actual_normal = _face_normal(actual, face)
        if _dot(actual_normal, _face_normal(points, mapped_face)) <= 0:
            return None
        if any(abs(_dot(actual_normal, _sub(actual[i], actual[face[0]]))) > COORD_TOL for i in face):
            return None
    planes, plane_buckets = [], defaultdict(list)
    return residual if _surface_loops(points, triangles, planes, plane_buckets) == _surface_loops(points, mapped_polygons, planes, plane_buckets) else None

def verify_house_geometry(blocks, lines, house, source, *, anchor_turn=None, extra_meshes=None):
    """Match source house/obstacle meshes against native pvShdGenObject surface loops."""
    if house.get("kind") != "mesh":
        raise ValueError("House coordinate verification requires a mesh house")
    if anchor_turn is None:
        if source["field_azimuth_deg"] != 0:
            raise ValueError("House coordinate verification is recorded only for mesh scenes with source anchor azimuth zero")
        anchor_turn = 180.0
    obstacle_meshes = list(extra_meshes or ())
    native = [(b, *_native_mesh(b, lines)) for b in blocks if b.object_type == "pvShdGenObject"]
    if len(native) != len(house["meshes"]) + len(obstacle_meshes):
        raise ValueError("Native and source house/obstacle mesh counts differ")
    mappings, obstacle_mappings, max_residual = [], [], 0.0
    for target_list, part in (*[(mappings, i) for i in house["meshes"]], *[(obstacle_mappings, i) for i in obstacle_meshes]):
        expected = [_rotate_z(tuple(p), anchor_turn) for p in part["vertices"]]
        bounds = _bounds(expected)
        matches = [(idx, res) for idx, (_, pts, fcs) in enumerate(native)
                   if all(abs(a - b) <= COORD_TOL for a, b in zip(bounds, _bounds(pts)))
                   and (res := _same_mesh(expected, part["triangles"], pts, fcs)) is not None]
        if len(matches) != 1:
            raise ValueError("Source mesh has no unique native surface match: " + part["name"])
        idx, res = matches[0]
        block, _, faces = native.pop(idx)
        max_residual = max(max_residual, res)
        target_list.append({"source_mesh": part["name"], "native_path": block.path,
                            "native_polygons": len(faces), "max_coordinate_residual_m": res})
    return {"house_geometry_verified": True, "house_mapping": mappings,
            "house_coordinate_tolerance_m": COORD_TOL, "house_max_coordinate_residual_m": max_residual,
            **({"obstacle_mesh_mapping": obstacle_mappings} if obstacle_meshes else {})}

def verify_tree_obstacles(blocks, tree_obstacles, source, *, anchor_turn):
    """Verify native pvShdElemObject (ObjetElemType=oeTree) records against declared trees."""
    trees = list(tree_obstacles or ())
    native = [b for b in blocks if b.object_type == "pvShdElemObject"]
    if len(native) != len(trees):
        raise ValueError("Native and declared oeTree obstacle counts differ")
    if not trees:
        return {"tree_obstacles_verified": True, "tree_count": 0}
    orientations = source.get("orientations") or []
    anchor_azim = (next(i for i in orientations if i["id"] == 1) if orientations else source)["field_azimuth_deg"]
    matched = []
    for tree in trees:
        expected_origin = _rotate_z(tuple(tree["origin_m"]), anchor_turn)
        expected_azim = anchor_azim + tree["rotation_deg"]
        expected_params = tuple(tree[k] for k in ("top_height_m", "middle_height_m", "bottom_height_m",
                                                  "trunk_height_m", "crown_diameter_m", "trunk_diameter_m"))
        hits = []
        for idx, b in enumerate(native):
            if b.one("ObjetElemType")[1] != "oeTree" or b.one("Source")[1] != "osPVsyst" or b.one("Flags")[1] != "$02200004":
                continue
            origin = [_number(v) for v in b.one("DOrigine")[1].split(",")]
            params = [_number(v) for v in b.one("Params")[1].rstrip(",").split(",") if v]
            if (len(origin) == 3 and len(params) == 6
                    and max(abs(a - c) for a, c in zip(origin, expected_origin)) <= COORD_TOL
                    and abs(_number(b.one("DInclin")[1])) <= ANGLE_TOL
                    and abs(_number(b.one("BaseSlope")[1])) <= ANGLE_TOL
                    and abs(_number(b.one("DAzimut")[1]) - expected_azim) <= ANGLE_TOL
                    and all(abs(a - c) <= 0.002 for a, c in zip(params, expected_params))):
                hits.append(idx)
        if len(hits) != 1:
            raise ValueError("Declared tree obstacle has no unique native oeTree match: " + tree["name"])
        matched.append({"name": tree["name"], "native_path": native.pop(hits[0]).path})
    return {"tree_obstacles_verified": True, "tree_count": len(matched), "tree_mapping": matched}

def verify_pv_geometry(blocks, tables, modules, source, *, anchor_turn=None):
    """Match source regions to native pvShdArrayTable records and verify coordinates/dimensions/angles."""
    source_planes = source.get("orientations") or []
    if source_planes:
        planes, expected_by_orient = {}, {}
        for p in source_planes:
            planes.setdefault(p["id"], p)
            expected_by_orient[p["id"]] = expected_by_orient.get(p["id"], 0) + p["module_total"]
        if not tables or any(t.get("orientation_id") not in planes for t in tables):
            raise ValueError("PV coordinate verification requires source-oriented tables for the declared orientations")
        per_orient = defaultdict(int)
        for t in tables:
            per_orient[t["orientation_id"]] += t["module_rows"] * t["module_columns"]
        for ident in planes:
            if per_orient.get(ident, 0) != expected_by_orient[ident]:
                raise ValueError("Source tables for orientation %d must sum to the source module total" % ident)
        grouped = True
    else:
        planes = {1: {"id": 1, "module_total": source["module_total"], "tilt_deg": source["tilt_deg"],
                      "azimuth_deg": source["azimuth_deg"], "field_azimuth_deg": source["field_azimuth_deg"]}}
        if not tables or any(t.get("orientation_id", 1) != 1 for t in tables):
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
        if (block.one("Source")[1] != "osPVCollada" or block.one("Version")[1] not in NATIVE_TEXT
                or block.one("TypeOrigine")[1] != "1" or block.one("NElemChamps")[1] != "1"):
            raise ValueError("PV verification requires the recorded PVCollada table/origin type")
        frame = [_number(v) for v in block.one("Cadre")[1].rstrip(",").split(",")]
        if len(frame) != 4 or any(v != 0 for v in frame):
            raise ValueError("The source PV geometry has no additional native table frame")
        origin = [_number(v) for v in block.one("DOrigine")[1].split(",")]
        if len(origin) != 3:
            raise ValueError("Native PV origin requires three coordinates")
        panel = block.one("PanneauPVName")[1].rsplit(";", 2)
        if len(panel) != 3:
            raise ValueError("Native PV module name is not a name;width;length record")
        dims = {k: _number(block.one(k)[1]) for k in ("LongShed", "LargShed", "ModSpacingX", "ModSpacingY")}
        dims.update(module_width_m=_number(panel[1]), module_length_m=_number(panel[2]))
        if (any(dims[k] <= 0 for k in ("LongShed", "LargShed", "module_width_m", "module_length_m"))
                or any(dims[k] < 0 for k in ("ModSpacingX", "ModSpacingY"))):
            raise ValueError("Native PV dimensions/spacing are outside the source geometry range")
        native.append({"block": block, "origin": origin, "dimensions": dims, "panel_name": panel[0],
                       "angles": {k: _number(block.one(k)[1]) for k in ("DInclin", "DAzimut", "BaseSlope")},
                       "rows": _integer(block.one("NModInHeight")[1]), "columns": _integer(block.one("NModInWidth")[1]),
                       "orientation": block.one("ModuleOrient")[1]})
    if len(native) != len(tables):
        raise ValueError("PV verification requires the saved native table count to match the declaration")
    if not grouped:
        if any(t["module_rows"] < 1 or t["module_columns"] < 1 for t in tables):
            raise ValueError("Single-orientation PV tables require positive module cells")
        if sum(t["module_rows"] * t["module_columns"] for t in tables) != source["module_total"]:
            raise ValueError("Single-orientation PV tables must sum to the source module total")
    mappings, max_origin, max_dimension, max_angle = [], 0.0, 0.0, 0.0
    for table in tables:
        ident = table.get("orientation_id", 1)
        plane = planes[ident]
        if grouped:
            if (table["tilt_deg"] != plane["tilt_deg"] or table["azimuth_deg"] != plane["azimuth_deg"]
                    or table["rotation_deg"] != source["azimuth_deg"] - plane["azimuth_deg"]):
                raise ValueError("Source table profile differs from its source orientation")
        elif (table["tilt_deg"] != plane["tilt_deg"] or table.get("azimuth_deg", source["azimuth_deg"]) != plane["azimuth_deg"]
              or table.get("rotation_deg", 0.0) != source["azimuth_deg"] - plane["azimuth_deg"]):
            raise ValueError("Source table profile differs from its source plane")
        expected_origin = list(_rotate_z(tuple(float(_num(v)) for v in table["origin_m"]), anchor_turn))
        pan_modules = table.get("module_geometry", modules)
        expected_dimensions = {k: float(_num(v)) for k, v in {
            "LongShed": table["column_span_m"], "LargShed": table["slope_span_m"],
            "ModSpacingX": table["column_spacing_m"], "ModSpacingY": table["row_spacing_m"],
            "module_width_m": pan_modules["width_m"], "module_length_m": pan_modules["length_m"],
        }.items()}
        expected_angles = {"DInclin": float(_num(plane["tilt_deg"])), "DAzimut": float(_num(plane["field_azimuth_deg"])), "BaseSlope": 0.0}
        orientation = {"landscape": "moLandScape", "portrait": "moPortrait"}[table["orientation"]]
        matches = []
        for idx, rec in enumerate(native):
            if (rec["rows"] != table["module_rows"] or rec["columns"] != table["module_columns"]
                    or rec["orientation"] != orientation or rec["panel_name"].casefold() != pan_modules["name"].casefold()):
                continue
            cdelta = [a - e for a, e in zip(rec["origin"], expected_origin)]
            ddelta = {k: rec["dimensions"][k] - v for k, v in expected_dimensions.items()}
            adelta = {k: rec["angles"][k] - v for k, v in expected_angles.items()}
            if (max(map(abs, cdelta)) <= COORD_TOL and max(map(abs, ddelta.values())) <= COORD_TOL
                    and max(map(abs, adelta.values())) <= ANGLE_TOL):
                matches.append((idx, cdelta, ddelta, adelta))
        if len(matches) != 1:
            raise ValueError("Source PV region has no unique native geometry match: " + table["name"])
        idx, cdelta, ddelta, adelta = matches[0]
        rec = native.pop(idx)
        block = rec["block"]
        if _integer(block.one("NoOrient")[1]) != ident:
            raise ValueError("Matched native PV region is linked to the wrong source orientation")
        max_origin = max(max_origin, max(map(abs, cdelta)))
        max_dimension = max(max_dimension, max(map(abs, ddelta.values())))
        max_angle = max(max_angle, max(map(abs, adelta.values())))
        mappings.append({"source_table": table["name"], "orientation_id": ident, "native_path": block.path,
                         "expected_origin_m": expected_origin, "native_origin_m": rec["origin"],
                         "origin_residual_m": cdelta, "dimension_residual_m": ddelta, "angle_residual_deg": adelta})
    return {"pv_geometry_verified": True, "pv_mapping": mappings, "pv_coordinate_tolerance_m": COORD_TOL,
            "pv_angle_tolerance_deg": ANGLE_TOL, "pv_max_coordinate_residual_m": max_origin,
            "pv_max_dimension_residual_m": max_dimension, "pv_max_angle_residual_deg": max_angle}

def _verify_narrow_module_layout(blocks, lines):
    owners = [b for b in blocks if b.object_type == "pvModuleLayout"]
    arrays, tables, layouts = _indexed(blocks, "pvSubArray", "SubArrayId"), [b for b in blocks if b.object_type == "pvShdArrayTable"], [b for b in blocks if b.object_type == "pvMLTable"]
    single = list(arrays) == [1]
    expected_arrays = [1] if single else [1, 2]
    if (len(owners) != 1 or owners[0].path != "PVObject_ModuleLayout" or list(arrays) != expected_arrays
            or len(tables) != 2 or len(layouts) != 2
            or owners[0].one("ModLayoutGlobState")[1] not in ("mlDefStrings", "mlDefInverters")
            or owners[0].one("SubFieldsOrigin")[1] != "so3DShadings"):
        raise ValueError("Module Layout review requires two native 3D tables and defined source strings")
    planes = _indexed(blocks, "pvOrient", "NoOrient")
    if single:
        if set(planes) != {1} or {_integer(a.one("NoOrientation")[1]) for a in arrays.values()} != {1}:
            raise ValueError("Single-orientation module layout requires one source plane and one source subarray")
    elif set(planes) != {1, 2} or {_integer(a.one("NoOrientation")[1]) for a in arrays.values()} != {1, 2}:
        raise ValueError("Module Layout source arrays must own two existing distinct orientations 1/2")
    if any(not _is_ordinary_fixed_plane(p) for p in planes.values()):
        raise ValueError("Module Layout review requires ordinary non-averaged fixed source planes")
    if inventory(lines, {k: (a.line - 1, a.end_line - 1) for k, a in arrays.items()})["counts_valid"] is not True:
        raise ValueError("Module Layout source circuit counts or ownership are incomplete")
    body = lines[owners[0].line:owners[0].end_line - 1]
    declared = [int(m[1]) for ln in body if (m := re.fullmatch(r"\s*SubFields, list of=([0-9]+) PVObjects\s*", ln))]
    if declared != [2] or sum(ln.strip() == "End of SubFields" for ln in body) != 1:
        raise ValueError("Module Layout declared subfield list does not match its tables")
    created = _indexed(blocks, "pvShdArrayTable", "NoObjCreated")
    identities = [_integer(b.one("NoObjCreated")[1]) for b in blocks if re.fullmatch(r"PVObject_Ombrage/PVObject_[1-9][0-9]*", b.path)]
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
        ident = _integer(layout.one("NoSubArrayPar")[1])
        if ident not in arrays or (not single and ident in used):
            raise ValueError("Module Layout has a missing or repeated source array")
        used.add(ident)
        array = arrays[ident]
        rows, columns = _integer(layout.one("NLigMod")[1]), _integer(layout.one("NColMod")[1])
        count = rows * columns
        if not rows or not columns or not 1 <= count <= 100 or _integer(array.one("NModSerie")[1]) != count:
            raise ValueError("Module Layout requires one string of NModSerie modules per table")
        if single:
            if (_integer(array.one("NStringCh")[1]) != 2 or _integer(array.one("NInverter")[1]) != 2
                    or _integer(array.one("NInvMPPT")[1]) != 2 or "NStrOrient1" not in array.fields or _integer(array.one("NStrOrient1")[1]) != 2):
                raise ValueError("Single-orientation module layout requires two inverters, two strings and two total MPPT inputs")
        elif any(array.one(k)[1] != "1" for k in ("NStringCh", "NInverter", "NInvMPPT")):
            raise ValueError("Module Layout review requires one independent string of 1..100 modules per array")
        if "PowerSharingInverterId" in array.fields and array.one("PowerSharingInverterId")[1] != "-1":
            raise ValueError("Module Layout single-string review excludes shared inverter inputs")
        plane = planes[_integer(array.one("NoOrientation")[1])]
        reference = _integer(layout.one("NoField3DCreated")[1])
        table = created.get(reference)
        if (table is None or table.one("Version")[1] not in NATIVE_TEXT or table.one("Source")[1] != "osPVCollada"
                or table.path.rsplit("/", 1)[-1] != "PVObject_" + layout.one("NoField3D")[1]
                or layout.one("NoShed3D")[1] != "1" or layout.one("NIdentical")[1] != "1"
                or _integer(table.one("NoOrient")[1]) != _integer(array.one("NoOrientation")[1])
                or _integer(table.one("NModInHeight")[1]) != rows or _integer(table.one("NModInWidth")[1]) != columns
                or layout.one("ModLayoutState")[1] != "mlDefStrings"
                or layout.one("ModOrient")[1] not in ("moLandScape", "moPortrait")
                or layout.one("ModOrient")[1] != table.one("ModuleOrient")[1]):
            raise ValueError("Module Layout table/array/3D references or mechanical layout disagree")
        if not _table_plane_angles_match(table, plane):
            raise ValueError("Module Layout PV table angles disagree with their owning source plane")
        _check_ml_spacing_padding(layout, table, "Module Layout spacing must be non-negative and match the 3D table",
                                  "Module Layout review requires zero native row padding")
        body = lines[layout.line:layout.end_line - 1]
        declared = [int(m[1]) for ln in body if (m := re.fullmatch(r"\s*Modules, list of=([0-9]+) tModule\s*", ln))]
        fields = [k for k in layout.fields if k.startswith("Module_")]
        if declared != [count] or sum(ln.strip() == "End of List Modules" for ln in body) != 1 or fields != [f"Module_{i}" for i in range(1, count + 1)]:
            raise ValueError("Module Layout declared module count or records are missing or unexpected")
        cells, strings_seen = set(), set()
        for index in range(1, count + 1):
            raw = layout.one(f"Module_{index}")[1]
            if not re.fullmatch(r"[1-9][0-9]*,[1-9][0-9]*,[1-9][0-9]*,[1-9][0-9]*,", raw):
                raise ValueError("Module Layout requires complete positive native module/string records")
            seq, row, col, string = map(int, raw.rstrip(",").split(","))
            if seq != index or not 1 <= row <= rows or not 1 <= col <= columns or (row, col) in cells:
                raise ValueError("Module Layout contains an unassigned or repeated module cell")
            cells.add((row, col))
            strings_seen.add(string)
        if single:
            if len(strings_seen) != 1:
                raise ValueError("Each single-orientation layout table must hold exactly one global string")
            string_id = strings_seen.pop()
            if string_id not in (1, 2) or string_id in used_strings:
                raise ValueError("Single-orientation module layout must assign global strings 1 and 2 once each")
            used_strings.add(string_id)
        else:
            string_id = ident
            if strings_seen != {ident}:
                raise ValueError("Module Layout contains an unassigned, repeated or mismatched module/string cell")
        result.append({"object_path": layout.path, "subarray_id": ident, "orientation_id": _integer(array.one("NoOrientation")[1]),
                       "native_table_path": table.path, "native_created_id": reference, "rows": rows, "columns": columns,
                       "module_count": count, "string_id": string_id, "state": layout.one("ModLayoutState")[1],
                       "module_orientation": layout.one("ModOrient")[1]})
    if single and used_strings != {1, 2}:
        raise ValueError("Single-orientation module layout must assign both global strings 1 and 2 exactly")
    return {"configured": True, "status": "stored_consistent", "tables": result,
            "global_state": owners[0].one("ModLayoutGlobState")[1],
            "module_count": sum(r["module_count"] for r in result), "native_consumption_verified": False}

def _review(issues):
    return {"configured": True, "status": "needs_review", "issues": list(issues), "native_consumption_verified": False}

def _matches_single_narrow(arrays, tables, layouts):
    try:
        if len(tables) != 2 or len(layouts) != 2:
            return False
        capacity = _integer(next(iter(arrays.values())).one("NModSerie")[1])
        strings = []
        for layout in layouts:
            rows, columns = _integer(layout.one("NLigMod")[1]), _integer(layout.one("NColMod")[1])
            if rows * columns != capacity:
                return False
            seen = set()
            for index in range(1, rows * columns + 1):
                if (m := _MODULE_RECORD.fullmatch(layout.one(f"Module_{index}")[1])) is None:
                    return False
                seen.add(m[4])
            if len(seen) != 1:
                return False
            strings.append(next(iter(seen)))
        return sorted(int(v) for v in strings) == [1, 2]
    except (ValueError, KeyError, OverflowError):
        return False

def _matches_dual_narrow(arrays, tables, layouts):
    try:
        if set(arrays) != {1, 2} or len(tables) != 2 or len(layouts) != 2:
            return False
        if any("PowerSharingInverterId" in a.fields and a.one("PowerSharingInverterId")[1] != "-1" for a in arrays.values()):
            return False
        return all(_integer(a.one("NStringCh")[1]) == 1 for a in arrays.values())
    except (ValueError, KeyError, OverflowError):
        return False

def _assess_general_module_layout(blocks, lines):
    """Classify a general per-cell stored layout as stored_consistent, draft_incomplete or invalid."""
    owners = [b for b in blocks if b.object_type == "pvModuleLayout"]
    if (len(owners) != 1 or owners[0].path != "PVObject_ModuleLayout"
            or owners[0].one("ModLayoutGlobState")[1] not in MODULE_LAYOUT_GLOBAL_STATES
            or owners[0].one("SubFieldsOrigin")[1] != "so3DShadings"):
        raise ValueError("Module Layout owner record is not a defined native module layout")
    arrays, planes = _indexed(blocks, "pvSubArray", "SubArrayId"), _indexed(blocks, "pvOrient", "NoOrient")
    tables, layouts = [b for b in blocks if b.object_type == "pvShdArrayTable"], [b for b in blocks if b.object_type == "pvMLTable"]
    if not arrays:
        raise ValueError("General module layout requires at least one source subarray")
    for array in arrays.values():
        strings = _integer(array.one("NStringCh")[1])
        if ("NStrOrient1" not in array.fields or _integer(array.one("NStrOrient1")[1]) != strings
                or any(k.startswith("NStrOrient") and k != "NStrOrient1" and array.one(k)[1] != "0" for k in array.fields)):
            raise ValueError("General module layout requires an exclusive single-orientation string branch per source subarray")
    if not layouts or len(tables) != len(layouts):
        raise ValueError("General module layout requires one native 3D table per module-layout table")
    plane_of = {}
    for subarray_id, array in arrays.items():
        orientation = _integer(array.one("NoOrientation")[1])
        if orientation not in planes:
            raise ValueError("General module layout source subarray %d owns a missing orientation %d" % (subarray_id, orientation))
        plane = planes[orientation]
        if not _is_ordinary_fixed_plane(plane, check_base_slope=True):
            raise ValueError("General module layout requires ordinary non-averaged fixed source planes")
        if "PowerSharingInverterId" in array.fields and array.one("PowerSharingInverterId")[1] != "-1":
            ps_id, sub_flags = array.one("PowerSharingInverterId")[1], (array.one("Flags")[1] if "Flags" in array.fields else "")
            if (not ps_id.isdigit() or "Version" not in array.fields or array.one("Version")[1] not in NATIVE_TEXT_OR_SEED
                    or not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", sub_flags) or (int(sub_flags[1:], 16) & 0x0500) != 0x0500):
                raise ValueError("General module layout shared inverter subarray requires non-negative ID and $0500 flags")
        plane_of[subarray_id] = (orientation, plane)
    checked_inv = inventory(lines, {k: (a.line - 1, a.end_line - 1) for k, a in arrays.items()})
    if checked_inv["counts_valid"] is not True:
        raise ValueError("General module layout requires complete consistent source circuit counts")
    for row in checked_inv["subarrays"]:
        if row["declared"].get("PowerSharingInverterId", "-1") != "-1" and not row["shared_inverter_lines"]:
            raise ValueError("Shared PowerSharingInverterId subarray lacks a verified shared circuit InverterNode")
    ranges = source_string_ranges(blocks)
    expected_total = sum((last - first + 1) * cap for first, last, cap in ranges.values())
    body = lines[owners[0].line:owners[0].end_line - 1]
    declared = [int(m[1]) for ln in body if (m := re.fullmatch(r"\s*SubFields, list of=([0-9]+) PVObjects\s*", ln))]
    issues, draft = [], False
    if declared != [len(layouts)] or sum(ln.strip() == "End of SubFields" for ln in body) != 1:
        issues.append("Declared subfield list does not match the module-layout tables")
    created = _indexed(tables, "pvShdArrayTable", "NoObjCreated")
    identities = [_integer(b.one("NoObjCreated")[1]) for b in blocks if re.fullmatch(r"PVObject_Ombrage/PVObject_[1-9][0-9]*", b.path)]
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
        ident = _integer(layout.one("NoSubArrayPar")[1])
        if ident not in arrays:
            issues.append("Module-layout table references an unknown source subarray %d" % ident)
            continue
        orientation, plane = plane_of[ident]
        first, last, _ = ranges[ident]
        rows, columns = _integer(layout.one("NLigMod")[1]), _integer(layout.one("NColMod")[1])
        if rows < 1 or columns < 1:
            issues.append("Module-layout table has a non-positive grid: " + layout.path)
            continue
        reference = _integer(layout.one("NoField3DCreated")[1])
        table = created.get(reference)
        if (table is None or (table.one("Source")[1], table.one("Version")[1]) not in PV_TABLE_SOURCES
                or table.path.rsplit("/", 1)[-1] != "PVObject_" + layout.one("NoField3D")[1]
                or layout.one("NoShed3D")[1] != "1" or layout.one("NIdentical")[1] != "1"
                or _integer(table.one("NoOrient")[1]) != orientation
                or _integer(table.one("NModInHeight")[1]) != rows or _integer(table.one("NModInWidth")[1]) != columns):
            issues.append("Module-layout table/array/3D reference or mechanical layout disagrees: " + layout.path)
            continue
        if (layout.one("ModLayoutState")[1] not in MODULE_LAYOUT_TABLE_STATES
                or layout.one("ModOrient")[1] not in ("moLandScape", "moPortrait")
                or layout.one("ModOrient")[1] != table.one("ModuleOrient")[1]):
            raise ValueError("General module layout supports portrait/landscape tables with consistent ModOrient and state")
        _check_ml_spacing_padding(layout, table, "General module layout spacing must be non-negative and match the 3D table",
                                  "General module layout excludes non-zero row padding")
        if not _table_plane_angles_match(table, plane):
            issues.append("PV table angles disagree with the source plane for " + layout.path)
        total_cells += rows * columns
        section = lines[layout.line:layout.end_line - 1]
        declared_cells = [int(m[1]) for ln in section if (m := re.fullmatch(r"\s*Modules, list of=([0-9]+) tModule\s*", ln))]
        present = sorted(int(k[7:]) for k in layout.fields if k.startswith("Module_") and k[7:].isdigit())
        cells, seen = [], set()
        if present != list(range(1, len(present) + 1)):
            issues.append("Module records are not a contiguous 1..N run in " + layout.path)
        elif declared_cells != [len(present)]:
            issues.append("Declared module count disagrees with %d records in %s" % (len(present), layout.path))
        else:
            for index in present:
                if (m := _MODULE_RECORD.fullmatch(layout.one(f"Module_{index}")[1])) is None:
                    issues.append("Module record Module_%d is not a complete cell tuple" % index)
                    continue
                seq, row, col, glob = (int(v) for v in m.groups())
                if seq != index or not 1 <= row <= rows or not 1 <= col <= columns:
                    issues.append("Module record Module_%d has an out-of-range sequence or cell" % index)
                    continue
                if (row, col) in seen:
                    issues.append("Module record Module_%d repeats cell (%d,%d)" % (index, row, col))
                    continue
                seen.add((row, col))
                cells.append((row, col, glob))
                if glob == 0:
                    draft = True
                elif glob < first or glob > last:
                    issues.append("Module record Module_%d global string %d is outside source subarray %d's declared range %d..%d"
                                  % (index, glob, ident, first, last))
                else:
                    string_totals[glob] += 1
            if len(seen) < rows * columns:
                draft = True
        rows_out.append({"object_path": layout.path, "subarray_id": ident, "orientation_id": orientation,
                         "native_table_path": table.path, "native_created_id": reference, "rows": rows, "columns": columns,
                         "module_count": rows * columns, "string_ids": sorted({c[2] for c in cells if c[2] > 0}),
                         "state": layout.one("ModLayoutState")[1], "module_orientation": layout.one("ModOrient")[1]})
    if set(subfields) != set(range(1, len(layouts) + 1)):
        issues.append("Subfield identities do not cover 1..%d exactly once" % len(layouts))
    for _, (first, last, cap) in ranges.items():
        for glob in range(first, last + 1):
            have = string_totals.get(glob, 0)
            if have > cap:
                issues.append("Global string %d holds %d modules, above the %d-module capacity" % (glob, have, cap))
            elif have < cap:
                draft = True
    if total_cells > expected_total:
        issues.append("Module-layout tables declare %d cells, above the %d-module source total" % (total_cells, expected_total))
    elif total_cells < expected_total:
        draft = True
    status = "invalid" if issues else ("draft_incomplete" if draft else "stored_consistent")
    result = {"configured": True, "status": status, "tables": rows_out, "global_state": owners[0].one("ModLayoutGlobState")[1],
              "module_count": total_cells, "native_consumption_verified": False}
    if issues:
        result["issues"] = issues
    if status == "draft_incomplete":
        result["unassigned_note"] = "Some module cells are unassigned (global 0) or the tables are incomplete; storable draft, not importable"
    return result

def assess_module_layout(blocks, lines):
    """Classify a stored pvModuleLayout as stored_consistent, draft_incomplete, invalid or needs_review."""
    owners = [b for b in blocks if b.object_type == "pvModuleLayout"]
    if not owners:
        return {"configured": False, "status": "absent", "native_consumption_verified": False}
    try:
        arrays = _indexed(blocks, "pvSubArray", "SubArrayId")
        tables, layouts = [b for b in blocks if b.object_type == "pvShdArrayTable"], [b for b in blocks if b.object_type == "pvMLTable"]
        ids = list(arrays)
        if len(ids) == 2 and _matches_dual_narrow(arrays, tables, layouts):
            return _verify_narrow_module_layout(blocks, lines)
        if len(ids) == 1 and ids == [1] and _matches_single_narrow(arrays, tables, layouts):
            return _verify_narrow_module_layout(blocks, lines)
        if ids:
            return _assess_general_module_layout(blocks, lines)
        return _review(["Module Layout has no source subarrays"])
    except (ValueError, KeyError, OverflowError) as exc:
        return _review([str(exc)])

def verify_module_layout(blocks, lines):
    """Strict stored-layout check returning stored_consistent or raising ValueError with issues."""
    result = assess_module_layout(blocks, lines)
    if result.get("status") != "stored_consistent":
        raise ValueError("; ".join(result.get("issues") or [result.get("status", "unverified module layout")]))
    return result

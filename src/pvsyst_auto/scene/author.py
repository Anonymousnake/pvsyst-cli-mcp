"""Serialize authored house meshes, obstacles, and fixed PV tables into PVCollada 2.0."""
from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from pvsyst_auto.components.editor import objects, pan_validation_fields, root_block
from pvsyst_auto.project.circuit import _walk, circuit_nodes, inventory as grid_inventory
from pvsyst_auto.project.paths import safe_filename, workspace_file
from pvsyst_auto.validation import finite_number as _finite
from pvsyst_auto.versions import NATIVE_TEXT_OR_SEED

NS = "http://www.collada.org/2008/03/COLLADASchema"
PV = "https://pvcollada.org/2026/XMLSchema"
TOL, SMALLEST = 1e-6, 5e-7
HOUSE_KEYS = ("length_m", "width_m", "eave_height_m", "roof")
LAYOUT_KEYS = ("module_rows", "module_columns", "orientation", "row_spacing_m",
               "column_spacing_m", "clearance_m", "edge_margin_m")
GEOMETRY_KEYS = ("model_name", "nom_power_wp", "width_m", "length_m", "geometry_depth_m")
PLACED_LAYOUT_KEYS = ("module_rows", "module_columns", "orientation", "row_spacing_m",
                      "column_spacing_m", "origin_m", "roof_mesh")
MAX_PARTS, MAX_VERTICES, MAX_TRIANGLES = 128, 50000, 100000
MAX_OBSTACLES, MAX_TABLES = 64, 64
DEFAULT_MODULE_DEPTH_M = 0.035
AUTHORING_TOOL = "pvsyst-cli-mcp/house-scene/1"

class SceneValidationError(ValueError):
    """Bounded, actionable input diagnostics without mutating authored geometry."""
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics[:100]
        self.truncated = len(diagnostics) > 100
        super().__init__("; ".join(row["message"] for row in self.diagnostics[:3]))

def _diagnostic(code, path, message, *, part=None, triangle=None, repair_hint=None):
    res = {"code": code, "path": path, "message": message,
           "repair_hint": repair_hint or "Correct the indicated input and submit a new preview."}
    if part is not None:
        res["part"] = part
    if triangle is not None:
        res["triangle"] = triangle
    return res

def _name(value):
    _vfail(not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 120,
           "Names must be 1..120 characters without surrounding whitespace")
    return value

def _vfail(cond, msg):
    if cond:
        raise ValueError(msg)

def _tag(namespace, name):
    return f"{{{namespace}}}{name}"

def _el(parent, namespace, name, attrib=None, text=None):
    el = ET.SubElement(parent, _tag(namespace, name), attrib or {})
    if text is not None:
        el.text = text
    return el

def _num(value):
    """Six-decimal text that never collapses a tiny nonzero coordinate to zero."""
    _vfail(not math.isfinite(value), "Coordinate is not finite")
    _vfail(value != 0.0 and abs(value) < SMALLEST, "Coordinate is too small to serialize at six decimals")
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text

def _paired(fields, root, alias, label):
    """Both owning declarations must agree; conflicting or duplicate keys raise."""
    found = []
    for name in (root, alias):
        if (entry := fields.get(name)) is None:
            continue
        _vfail(len(entry) != 1, f"PAN {name} must occur exactly once")
        found.append((name, _finite(float(entry[0]), f"PAN {name}", positive=True)))
    _vfail(not found, f"PAN does not declare {label}")
    _vfail(len(found) == 2 and abs(found[0][1] - found[1][1]) > TOL * max(1.0, abs(found[0][1])),
           f"PAN {label} disagrees between {root} and {alias}")
    return found[0][1]

def _pick(fields, names, label):
    for name in names:
        if (entry := fields.get(name)) is None:
            continue
        _vfail(len(entry) != 1, f"PAN {name} must occur exactly once")
        _vfail(not (text := entry[0].strip()), f"PAN {name} must not be empty")
        return text
    raise ValueError(f"PAN does not declare {label}")

def _resolve_depth(fields, explicit):
    if explicit is not None:
        return explicit["depth_m"], "caller-provided"
    entries = fields.get("Depth")
    if entries and len(entries) == 1:
        try:
            parsed = float(entries[0])
            if math.isfinite(parsed) and parsed > 0:
                return parsed, "pan-commercial-depth"
        except ValueError:
            pass
    return DEFAULT_MODULE_DEPTH_M, "default-frame-depth"

def module_profile(fields, explicit=None):
    """Read PAN width/height in metres (XML scales by 1000 downstream)."""
    short_side, long_side = _paired(fields, "Width", "LargApp", "module width"), _paired(fields, "Height", "LongApp", "module height")
    try:
        nom_power = float(_pick(fields, ("PNom", "PNomModule"), "nom_power"))
    except ValueError:
        raise ValueError("PAN nom_power must be a number") from None
    _vfail(not math.isfinite(nom_power) or nom_power <= 0, "PAN nom_power must be a finite positive number")
    name, (depth, depth_source) = _pick(fields, ("Model",), "Model"), _resolve_depth(fields, explicit)
    if explicit is not None:
        _vfail(explicit["name"] != name, "module_geometry.model_name disagrees with the loose PAN")
        for label, actual, wanted in (("width_m", short_side, explicit["width_m"]),
                                      ("length_m", long_side, explicit["length_m"]), ("nom_power_wp", nom_power, explicit["nom_power"])):
            _vfail(abs(actual - wanted) > TOL * max(1.0, abs(actual)), f"module_geometry.{label} disagrees with the loose PAN")
    return {"length_m": long_side, "width_m": short_side, "nom_power": nom_power,
            "name": name, "depth_m": depth, "depth_source": depth_source, "metadata_source": "source-pan"}

def caller_geometry(geometry):
    """Validate explicit module metadata for a native database PAN reference."""
    if geometry is None:
        return None
    _vfail(not isinstance(geometry, dict) or set(geometry) != set(GEOMETRY_KEYS), "module_geometry must declare exactly " + ", ".join(GEOMETRY_KEYS))
    _vfail(not isinstance(geometry["model_name"], str) or not geometry["model_name"].strip(), "module_geometry.model_name must be non-empty text")
    return {"name": geometry["model_name"].strip(), "nom_power": _finite(geometry["nom_power_wp"], "nom_power_wp", positive=True),
            "length_m": _finite(geometry["length_m"], "length_m", positive=True), "width_m": _finite(geometry["width_m"], "width_m", positive=True),
            "depth_m": _finite(geometry["geometry_depth_m"], "geometry_depth_m", positive=True)}

def resolve_modules(components, pan_name, geometry):
    """Resolve loose workspace/builtin PAN metadata, falling back to explicit geometry."""
    explicit, pan_path = caller_geometry(geometry), None
    for library in ("workspace", "builtin"):
        if not (library == "builtin" and components.builtin is None) and (c := components._path("PAN", pan_name, library)).is_file():
            pan_path = c
            break
    if pan_path is None:
        _vfail(explicit is None, "The referenced PAN is neither a loose workspace nor an installed-library file; "
                                 "module_geometry is required and no vendor value is guessed")
        return explicit | {"metadata_source": "caller-provided-for-native-database-reference", "depth_source": "caller-provided"}, None
    pan_bytes = components._read(pan_path)
    inspected = components._inspect("PAN", pan_bytes)
    if inspected["format"] != "text":
        _vfail(explicit is None, "The referenced PAN is legacy binary and cannot be parsed; module_geometry is required")
        return explicit | {"metadata_source": "caller-provided-for-native-database-reference", "depth_source": "caller-provided"}, (pan_path, pan_bytes)
    _vfail(bool(inspected["structural_errors"]), "PAN has structural errors: " + "; ".join(inspected["structural_errors"]))
    _vfail(bool(inspected["known_value_errors"]), "PAN has value errors: " + "; ".join(inspected["known_value_errors"]))
    return module_profile(pan_validation_fields(components._text("PAN", pan_bytes)[1]), explicit), (pan_path, pan_bytes)

def _per_table_geometry(layout):
    result = {}
    tables = layout.get("tables") if isinstance(layout, dict) else None
    if not isinstance(tables, list):
        return result
    for entry in tables:
        if not isinstance(entry, dict) or (geometry := entry.get("module_geometry")) is None:
            continue
        identifier = entry.get("source_subarray_id")
        if identifier is None:
            identifier = entry.get("orientation_id")
        else:
            _vfail(type(entry.get("orientation_id")) is not int or entry["orientation_id"] < 1, "source_subarray_id requires the table's orientation_id")
        _vfail(type(identifier) is not int or identifier < 1, "module_geometry is only supported on named tables that declare orientation_id")
        caller_geometry(geometry)
        _vfail(identifier in result and result[identifier] != geometry, f"Tables on source owner {identifier} declare conflicting module_geometry")
        result[identifier] = geometry
    return result

def resolve_source_geometry(components, source, module_geometry, layout=None):
    """Resolve each source group's module metadata once; returns (default_modules, pan_snapshots)."""
    groups = source.get("orientations") or []
    if not groups:
        modules, snapshot = resolve_modules(components, source["pan_name"], module_geometry)
        return modules, ([snapshot] if snapshot is not None else [])
    explicit = _per_table_geometry(layout)
    shared = len({group["pan_name"].casefold() for group in groups}) == 1
    snapshots = []
    for group in groups:
        identity = group.get("subarray_id", group["id"])
        requested = explicit.get(identity) or (module_geometry if shared else None)
        try:
            resolved, snapshot = resolve_modules(components, group["pan_name"], requested)
        except ValueError as exc:
            raise ValueError("Source orientation %d needs its own module geometry (%s): %s"
                             % (identity, group["pan_name"], exc)) from None
        if snapshot is not None and snapshot not in snapshots:
            snapshots.append(snapshot)
        group["module_geometry"] = resolved
    return groups[0]["module_geometry"], snapshots

def _vector(values, label):
    _vfail(not isinstance(values, list) or len(values) != 3, f"{label} must contain three numbers")
    return [_finite(value, label) for value in values]

def _vec_num(values, label):
    return [float(_num(v)) for v in _vector(values, label)]

def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]

def _dot(a, b):
    value = sum(a[i] * b[i] for i in range(3))
    _vfail(not math.isfinite(value), "Derived geometry is not finite")
    return value

def _normal(a, b, c):
    u, v = _sub(b, a), _sub(c, a)
    normal = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
    length = math.hypot(*normal)
    _vfail(not math.isfinite(length) or length == 0, "Mesh triangles must be finite and non-degenerate")
    return [value / length for value in normal]

def mesh_house_profile(house):
    if set(house) != {"kind", "meshes"} or not isinstance(house["meshes"], list):
        raise SceneValidationError([_diagnostic("mesh_fields", "house.meshes", "A mesh house must declare exactly kind and meshes")])
    if not 1 <= len(house["meshes"]) <= MAX_PARTS:
        raise SceneValidationError([_diagnostic("mesh_count", "house.meshes", f"Declare 1..{MAX_PARTS} named house meshes")])
    parts, names, vertex_count, triangle_count, errors = [], set(), 0, 0, []
    for index, part in enumerate(house["meshes"]):
        path = f"house.meshes[{index}]"
        if not isinstance(part, dict) or set(part) != {"name", "vertices", "triangles"}:
            errors.append(_diagnostic("mesh_fields", path, "Each mesh must declare exactly name, vertices and triangles"))
            continue
        try:
            name = _name(part["name"])
        except ValueError as exc:
            errors.append(_diagnostic("mesh_name", path + ".name", str(exc)))
            continue
        if name.casefold() in names:
            errors.append(_diagnostic("duplicate_mesh_name", path + ".name", "House mesh names must be unique, ignoring case", part=name))
        names.add(name.casefold())
        vertices, triangles = part["vertices"], part["triangles"]
        if not isinstance(vertices, list) or len(vertices) < 3:
            errors.append(_diagnostic("mesh_vertices", path + ".vertices", "Each mesh needs at least three vertices", part=name))
            continue
        if not isinstance(triangles, list) or not triangles:
            errors.append(_diagnostic("mesh_triangles", path + ".triangles", "Each mesh needs explicit triangle indices", part=name))
            continue
        vertex_count, triangle_count = vertex_count + len(vertices), triangle_count + len(triangles)
        if vertex_count > MAX_VERTICES or triangle_count > MAX_TRIANGLES:
            errors.append(_diagnostic("mesh_ceiling", path, "House mesh payload exceeds the adapter vertex/triangle ceiling", part=name))
            break
        serialized = []
        for vi, vertex in enumerate(vertices):
            try:
                serialized.append(_vec_num(vertex, "Mesh vertex"))
            except ValueError as exc:
                serialized.append(None)
                errors.append(_diagnostic("mesh_coordinate", f"{path}.vertices[{vi}]", str(exc), part=name))
        seen_faces = set()
        for ti, triangle in enumerate(triangles):
            face_path = f"{path}.triangles[{ti}]"
            if (not isinstance(triangle, list) or len(triangle) != 3
                    or any(type(idx) is not int or not 0 <= idx < len(vertices) for idx in triangle) or len(set(triangle)) != 3):
                errors.append(_diagnostic("triangle_indices", face_path, "Triangles require three distinct, in-range, zero-based integer indices", part=name, triangle=ti))
                continue
            if any(serialized[v] is None for v in triangle):
                continue
            points = [serialized[v] for v in triangle]
            try:
                _normal(*points)
                key = tuple(sorted(tuple(p) for p in points))
                _vfail(key in seen_faces, "A coincident triangle is repeated within this mesh")
                seen_faces.add(key)
            except ValueError as exc:
                errors.append(_diagnostic("triangle_geometry", face_path, str(exc), part=name, triangle=ti, repair_hint="Replace the indicated triangle; check vertex coordinates, six-decimal collapse and duplicated faces."))
        parts.append({"name": name, "vertices": serialized, "triangles": triangles})
        if len(errors) > 100:
            break
    if errors:
        raise SceneValidationError(errors)
    return {"kind": "mesh", "meshes": parts}

def _inside_triangle(point, a, b, c):
    normal = _normal(a, b, c)
    for start, end in ((a, b), (b, c), (c, a)):
        u, v = _sub(end, start), _sub(point, start)
        if _dot([u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]], normal) < -TOL * max(1.0, math.hypot(*u)):
            return False
    return True

def house_profile(house):
    """Validate mesh or rectangular-roof house inputs."""
    if isinstance(house, dict) and house.get("kind") == "mesh":
        return mesh_house_profile(house)
    _vfail(not isinstance(house, dict) or set(house) != set(HOUSE_KEYS), "house must declare exactly length_m, width_m, eave_height_m and roof")
    length, width, eave = (_finite(house[k], k, positive=True) for k in ("length_m", "width_m", "eave_height_m"))
    roof = house["roof"]
    _vfail(not isinstance(roof, dict) or roof.get("kind") not in ("flat", "gable"), "roof.kind must be flat or gable")
    if roof["kind"] == "flat":
        _vfail(set(roof) != {"kind"}, "flat roof accepts only kind")
        return {"kind": "flat", "length_m": length, "width_m": width, "eave_height_m": eave, "roof_rise_m": 0.0, "pitch_deg": None}
    _vfail(set(roof) != {"kind", "pitch_deg"}, "gable roof accepts only kind and pitch_deg")
    pitch = _finite(roof["pitch_deg"], "roof.pitch_deg", positive=True, maximum=90.0)
    _vfail(not 0.0 < pitch < 90.0, "roof.pitch_deg must be strictly between 0 and 90 degrees")
    ridge = (width / 2.0) * math.tan(math.radians(pitch))
    _vfail(not math.isfinite(ridge), "Derived ridge height is not finite")
    return {"kind": "gable", "length_m": length, "width_m": width, "eave_height_m": eave, "roof_rise_m": ridge, "pitch_deg": pitch}

def _box_faces():
    return [[0, 2, 1], [0, 3, 2], [0, 1, 5], [0, 5, 4], [3, 6, 2], [3, 7, 6],
            [0, 4, 7], [0, 7, 3], [1, 2, 6], [1, 6, 5], [4, 5, 6], [4, 6, 7]]

def _wall_mesh(start, end, height_m, thickness_m):
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    _vfail(length <= 1e-4, "Wall start_m and end_m must be separated by > 0.1 mm in XY")
    nx, ny, z0, z1 = -dy / length * (thickness_m / 2.0), dx / length * (thickness_m / 2.0), start[2], end[2]
    pts = [(cx, cy, cz + dh) for dh in (0.0, height_m)
           for cx, cy, cz in ((start[0] - nx, start[1] - ny, z0), (end[0] - nx, end[1] - ny, z1),
                              (end[0] + nx, end[1] + ny, z1), (start[0] + nx, start[1] + ny, z0))]
    return [[float(_num(c)) for c in p] for p in pts], _box_faces()

def _tree_proxy_mesh(origin, top_h, trunk_h, crown_d, trunk_d, rotation_deg):
    """Low-poly closed prism proxy for COLLADA preview; native export emits oeTree."""
    ox, oy, oz = origin

    def box_at(hw, z0, z1):
        return [[float(_num(ox + rx)), float(_num(oy + ry)), float(_num(oz + rz))] for rx, ry, rz in (
            _rotate_z((sx * hw, sy * hw, z), rotation_deg) for z in (z0, z1) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)))]

    return box_at(trunk_d / 2.0, 0.0, trunk_h) + box_at(crown_d / 2.0, trunk_h, trunk_h + max(top_h - trunk_h, 0.05)), _box_faces() + [[i + 8 for i in f] for f in _box_faces()]

def _obs_rot(raw, prefix):
    rot = _finite(raw.get("rotation_deg", 0.0), f"{prefix}.rotation_deg", minimum=-360.0, maximum=360.0)
    _vfail(not -360.0 < rot < 360.0, f"{prefix}.rotation_deg must be strictly between -360 and 360")
    return rot

def _obs_keys(raw, req, opt, label):
    _vfail(not req <= set(raw) <= (req | opt), f"{label} declares unexpected or missing keys")

def obstacle_profiles(obstacles, existing_names=()):
    """Validate and normalize optional scene obstacles (building, tree, wall, mesh)."""
    if obstacles is None:
        return []
    if not isinstance(obstacles, list) or not 1 <= len(obstacles) <= MAX_OBSTACLES:
        raise SceneValidationError([_diagnostic("obstacle_list", "obstacles", f"obstacles must be a non-empty list of at most {MAX_OBSTACLES} entries")])
    names, records, vertex_count, triangle_count, errors = {n.casefold() for n in existing_names}, [], 0, 0, []
    for index, raw in enumerate(obstacles):
        path = f"obstacles[{index}]"
        if not isinstance(raw, dict) or "kind" not in raw:
            errors.append(_diagnostic("obstacle_fields", path, "Each obstacle must be an object declaring kind"))
            continue
        kind = raw.get("kind")
        if kind not in ("building", "tree", "wall", "mesh"):
            errors.append(_diagnostic("obstacle_kind", path + ".kind", "Obstacle kind must be building, tree, wall, or mesh"))
            continue
        try:
            name = _name(raw.get("name"))
            _vfail(name.casefold() in names, "Obstacle names must be unique and distinct from house mesh names")
            names.add(name.casefold())
            if kind == "building":
                _obs_keys(raw, {"kind", "name", "origin_m", "length_m", "width_m", "height_m"}, {"rotation_deg", "roof"}, "building obstacle")
                origin = _vec_num(raw["origin_m"], "building.origin_m")
                length, width, height = (_finite(raw[k], f"building.{k}", positive=True) for k in ("length_m", "width_m", "height_m"))
                rot, roof = _obs_rot(raw, "building"), raw.get("roof")
                pts, tris = house_mesh(house_profile({"length_m": length, "width_m": width, "eave_height_m": height, "roof": roof if roof is not None else {"kind": "flat"}}))
                verts = [[float(_num(origin[i] + _rotate_z(p, rot)[i])) for i in range(3)] for p in pts]
                spec = {"kind": "building", "name": name, "origin_m": origin, "length_m": length, "width_m": width,
                        "height_m": height, "rotation_deg": rot, **({"roof": roof} if roof is not None else {})}
                rec = {"kind": "building", "name": name, "vertices": verts, "triangles": [list(f) for f in tris], "spec": spec}
            elif kind == "wall":
                _obs_keys(raw, {"kind", "name", "start_m", "end_m", "height_m"}, {"thickness_m"}, "wall obstacle")
                start, end = (_vec_num(raw[k], f"wall.{k}") for k in ("start_m", "end_m"))
                height, thick = _finite(raw["height_m"], "wall.height_m", positive=True), _finite(raw.get("thickness_m", 0.2), "wall.thickness_m", positive=True)
                verts, faces = _wall_mesh(start, end, height, thick)
                spec = {"kind": "wall", "name": name, "start_m": start, "end_m": end, "height_m": height, "thickness_m": thick}
                rec = {"kind": "wall", "name": name, "vertices": verts, "triangles": faces, "spec": spec}
            elif kind == "mesh":
                _vfail(set(raw) != {"kind", "name", "vertices", "triangles"}, "mesh obstacle requires exactly kind, name, vertices, and triangles")
                sub = mesh_house_profile({"kind": "mesh", "meshes": [{"name": name, "vertices": raw["vertices"], "triangles": raw["triangles"]}]})["meshes"][0]
                spec = {"kind": "mesh", "name": name, "vertices": sub["vertices"], "triangles": sub["triangles"]}
                rec = {**spec, "spec": spec}
            else:
                req = {"kind", "name", "origin_m", "top_height_m", "middle_height_m", "bottom_height_m", "trunk_height_m", "crown_diameter_m", "trunk_diameter_m"}
                _obs_keys(raw, req, {"rotation_deg"}, "tree obstacle")
                origin = _vec_num(raw["origin_m"], "tree.origin_m")
                top_h, mid_h, bot_h, trunk_h, crown_d, trunk_d = (
                    _finite(raw[k], f"tree.{k}", positive=True)
                    for k in ("top_height_m", "middle_height_m", "bottom_height_m", "trunk_height_m", "crown_diameter_m", "trunk_diameter_m"))
                rot = _obs_rot(raw, "tree")
                verts, faces = _tree_proxy_mesh(origin, top_h, trunk_h, crown_d, trunk_d, rot)
                spec = {"kind": "tree", "name": name, "origin_m": origin, "top_height_m": top_h, "middle_height_m": mid_h,
                        "bottom_height_m": bot_h, "trunk_height_m": trunk_h, "crown_diameter_m": crown_d, "trunk_diameter_m": trunk_d, "rotation_deg": rot}
                rec = {**spec, "vertices": verts, "triangles": faces, "spec": spec}
            vertex_count, triangle_count = vertex_count + len(rec["vertices"]), triangle_count + len(rec["triangles"])
            _vfail(vertex_count > MAX_VERTICES or triangle_count > MAX_TRIANGLES, "Obstacles exceed the adapter vertex/triangle ceiling")
            records.append(rec)
        except (ValueError, SceneValidationError) as exc:
            errors.append(_diagnostic("obstacle_invalid", path, str(exc)))
    if errors:
        raise SceneValidationError(errors)
    return records

def layout_profile(layout, modules, total, source_tilt, *, placed=False):
    """One fixed table; tilt and azimuth are inherited, never requested."""
    keys = PLACED_LAYOUT_KEYS if placed else LAYOUT_KEYS
    _vfail(not isinstance(layout, dict) or set(layout) != set(keys), "layout must declare exactly " + ", ".join(keys))
    rows, columns = layout["module_rows"], layout["module_columns"]
    for name, value in (("module_rows", rows), ("module_columns", columns)):
        _vfail(type(value) is not int or value < 1, f"{name} must be a strict positive integer")
    _vfail(total is not None and rows * columns != total, f"module_rows x module_columns must equal the source module count {total}")
    _vfail(layout["orientation"] not in ("portrait", "landscape"), "orientation must be portrait or landscape")
    spacing_keys = ("row_spacing_m", "column_spacing_m") if placed else ("row_spacing_m", "column_spacing_m", "clearance_m", "edge_margin_m")
    spacing = {name: _finite(layout[name], name, minimum=0.0) for name in spacing_keys}
    if placed:
        spacing["origin_m"] = _vector(layout["origin_m"], "origin_m")
        _vfail(not isinstance(layout["roof_mesh"], str) or not layout["roof_mesh"].strip(), "roof_mesh must name the planar supporting house mesh")
        spacing["roof_mesh"] = layout["roof_mesh"]
    along = modules["width_m"] if layout["orientation"] == "landscape" else modules["length_m"]
    across = modules["length_m"] if layout["orientation"] == "landscape" else modules["width_m"]
    column_span, slope_span = columns * across + (columns - 1) * spacing["column_spacing_m"], rows * along + (rows - 1) * spacing["row_spacing_m"]
    for name, value in (("column_span_m", column_span), ("slope_span_m", slope_span)):
        _vfail(not math.isfinite(value) or value <= 0, f"Derived {name} must be finite and positive")
    return {"module_rows": rows, "module_columns": columns, "orientation": layout["orientation"], **spacing,
            "geometry_depth_m": modules["depth_m"], "depth_source": modules.get("depth_source", "caller-provided"),
            "tilt_deg": source_tilt, "column_span_m": column_span, "slope_span_m": slope_span}

def table_profiles(layout, house, modules, source):
    """Validate single or grouped PV table declarations against source owners."""
    profiles = source.get("orientations", [])
    by_owner, by_orient = {}, {}
    for item in profiles:
        by_owner[item.get("subarray_id", item["id"])] = item
        by_orient.setdefault(item["id"], []).append(item)
    if not isinstance(layout, dict) or "tables" not in layout:
        _vfail(bool(profiles), "Grouped source orientations require named tables with orientation_id")
        return [layout_profile(layout, modules, source["module_total"], source["tilt_deg"], placed=house["kind"] == "mesh")]
    _vfail(house["kind"] != "mesh" or set(layout) != {"tables"} or not isinstance(layout["tables"], list) or not 1 <= len(layout["tables"]) <= MAX_TABLES,
           f"Named tables require a mesh house and 1..{MAX_TABLES} table entries")
    per_group, result, names, errors = {owner: 0 for owner in by_owner}, [], set(), []
    for index, entry in enumerate(layout["tables"]):
        path = f"layout.tables[{index}]"
        try:
            _vfail(not isinstance(entry, dict), "Each named table requires exactly its name, placement and source orientation fields")
            keys = set(PLACED_LAYOUT_KEYS) | {"name"}
            if profiles:
                keys |= {"orientation_id"} | ({"source_subarray_id"} if "source_subarray_id" in entry else set()) | ({"module_geometry"} if "module_geometry" in entry else set())
            _vfail(set(entry) != keys, "Each named table requires exactly its name, placement and source orientation fields")
            name = _name(entry["name"])
            _vfail(name.casefold() in names, "PV table names must be unique, ignoring case")
            names.add(name.casefold())
            group, owner = source, None
            if profiles:
                ident = entry["orientation_id"]
                _vfail(type(ident) is not int or ident < 1, "orientation_id must select a declared source orientation")
                if "source_subarray_id" in entry:
                    owner = entry["source_subarray_id"]
                    _vfail(type(owner) is not int or owner < 1 or owner not in by_owner, "source_subarray_id must select a declared source subarray owner")
                    group = by_owner[owner]
                    _vfail(group["id"] != ident, "source_subarray_id %d does not belong to orientation %d" % (owner, ident))
                else:
                    candidates = by_orient.get(ident, [])
                    _vfail(len(candidates) != 1, "ambiguous_source_owner: orientation %d is owned by %d source subarrays; declare source_subarray_id" % (ident, len(candidates)))
                    group = candidates[0]
                    owner = group.get("subarray_id", ident)
            table_modules = (caller_geometry(entry["module_geometry"]) if profiles and entry.get("module_geometry") is not None
                             else (group["module_geometry"] if profiles and group.get("module_geometry") is not None else modules))
            profile = layout_profile({k: entry[k] for k in PLACED_LAYOUT_KEYS}, table_modules, None, group["tilt_deg"], placed=True)
            if profiles:
                per_group[owner] += profile["module_rows"] * profile["module_columns"]
                profile.update(orientation_id=group["id"], azimuth_deg=group["azimuth_deg"], rotation_deg=source["azimuth_deg"] - group["azimuth_deg"])
                if "source_subarray_id" in entry:
                    profile["source_subarray_id"] = owner
                if table_modules != modules:
                    profile["module_geometry"] = table_modules
            result.append(profile | {"name": name})
        except (ValueError, KeyError) as exc:
            errors.append(_diagnostic("pv_table_input", path, str(exc), repair_hint="Correct this named table without changing other valid regions."))
    if errors:
        raise SceneValidationError(errors)
    if profiles:
        for owner, group in by_owner.items():
            if per_group[owner] != group["module_total"]:
                raise SceneValidationError([_diagnostic("pv_group_total", "layout.tables", "Source owner %d tables hold %d modules, not the source group total %d" % (owner, per_group[owner], group["module_total"]), repair_hint="Allocate exactly the source group's modules across its tables.")])
    total = sum(t["module_rows"] * t["module_columns"] for t in result)
    if total != source["module_total"]:
        raise SceneValidationError([_diagnostic("pv_module_total", "layout.tables", f"All PV tables together must contain {source['module_total']} modules, not {total}", repair_hint="Allocate the existing source modules across tables; do not duplicate the full array.")])
    return result

def _string_layout_profile(string_layout, tables):
    """Validate explicit per-cell string binding; returns (normalized, 'complete'|'draft_incomplete')."""
    if string_layout is None:
        return None, None
    _vfail(not isinstance(string_layout, dict) or set(string_layout) != {"tables"}, "string_layout must declare exactly tables")
    entries = string_layout["tables"]
    _vfail(not isinstance(entries, list) or not 1 <= len(entries) <= MAX_TABLES, f"string_layout.tables must hold 1..{MAX_TABLES} table bindings")
    by_name, normalized, seen_tables, errors, complete = {p["name"]: p for p in tables if "name" in p}, [], set(), [], True
    for index, entry in enumerate(entries):
        path = f"string_layout.tables[{index}]"
        try:
            _vfail(not isinstance(entry, dict) or set(entry) != {"name", "subarray_id", "cells"}, "Each table binding requires exactly name, subarray_id and cells")
            name = _name(entry["name"])
            _vfail(name in seen_tables, "Each table name may be bound only once")
            seen_tables.add(name)
            profile = by_name.get(name)
            _vfail(profile is None, "Binding names a table that is not declared in layout.tables")
            subarray_id = entry["subarray_id"]
            _vfail(type(subarray_id) is not int or subarray_id < 1, "subarray_id must be a positive strict integer")
            _vfail(not isinstance(entry["cells"], list), "cells must be a list")
            rows, columns, seen_cells, cells = profile["module_rows"], profile["module_columns"], set(), []
            for cell in entry["cells"]:
                _vfail(not isinstance(cell, dict) or set(cell) != {"row", "col", "global_string"}, "Each cell requires exactly row, col and global_string")
                row, col, glob = cell["row"], cell["col"], cell["global_string"]
                for label, value in (("row", row), ("col", col)):
                    _vfail(type(value) is not int or value < 1, f"{label} must be a positive strict integer")
                _vfail(type(glob) is not int or glob < 0, "global_string must be a non-negative strict integer (0 = unassigned draft)")
                _vfail(row > rows or col > columns, f"cell ({row},{col}) is outside the {rows}x{columns} grid of table {name}")
                _vfail((row, col) in seen_cells, f"cell ({row},{col}) is repeated in table {name}")
                seen_cells.add((row, col))
                cells.append({"row": row, "col": col, "global_string": glob})
            if len(seen_cells) != rows * columns or any(c["global_string"] == 0 for c in cells):
                complete = False
            normalized.append({"name": name, "subarray_id": subarray_id, "cells": cells})
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(_diagnostic("string_layout", path, str(exc), repair_hint="Fix the indicated table/cell binding; explicit string wiring is never auto-allocated."))
    if errors:
        raise SceneValidationError(errors)
    return {"tables": normalized}, ("complete" if (complete and len(seen_tables) == len(by_name)) else "draft_incomplete")

def _rotate_z(point, degrees):
    angle = math.radians(degrees)
    x, y, z = point
    return (x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle), z)

def _placed_points(table):
    points, _ = rack_mesh(table)
    return [_rotate_z(point, table["rotation_deg"]) for point in points] if "rotation_deg" in table else points

def check_tables(house, tables):
    errors = []
    for index, table in enumerate(tables):
        path = f"layout.tables[{index}]" if "name" in table else "layout"
        try:
            check_fit(house, table, _rack_origin(house, table), _placed_points(table))
        except ValueError as exc:
            errors.append(_diagnostic("pv_roof_fit", path, str(exc), repair_hint="Check this table's roof_mesh, origin_m and footprint against the source PV plane."))
    for index, table in enumerate(tables):
        a, down = _rack_origin(house, table), table["slope_span_m"] * math.cos(math.radians(table["tilt_deg"]))
        for other in tables[:index]:
            b = _rack_origin(house, other)
            if "orientation_id" in table:
                ca, cb = ([[pt[k] + p[k] for k in range(3)] for p in _placed_points(tbl)] for pt, tbl in ((a, table), (b, other)))
                if all(min(max(p[k] for p in ca), max(p[k] for p in cb)) - max(min(p[k] for p in ca), min(p[k] for p in cb)) > TOL for k in (0, 1)):
                    errors.append(_diagnostic("pv_group_footprints", f"layout.tables[{index}]", "Grouped authoring requires disjoint XY bounding footprints, not a physical intersection test", repair_hint="Use separate projected regions; native 3D interference remains a separate check."))
                continue
            other_down = other["slope_span_m"] * math.cos(math.radians(other["tilt_deg"]))
            offset = (a[2] - b[2]) - math.tan(math.radians(table["tilt_deg"])) * (a[1] - b[1])
            overlap_x = min(a[0] + table["column_span_m"], b[0] + other["column_span_m"]) - max(a[0], b[0])
            overlap_y = min(a[1] + down, b[1] + other_down) - max(a[1], b[1])
            if abs(offset) <= TOL and overlap_x > TOL and overlap_y > TOL:
                errors.append(_diagnostic("pv_tables_overlap", f"layout.tables[{index}]", f"PV table overlaps coplanar table {tables.index(other)}", repair_hint="Separate these PV rectangles or explicitly reallocate their modules."))
    if errors:
        raise SceneValidationError(errors)

def source_profile(store, lines, arrays, system, *, allow_groups=False):
    """Validate source system/arrays/orientations and return orientation-keyed or owner-keyed profile."""
    _vfail(system is None or (not allow_groups and len(arrays) != 1), "Requires one pvSystem and one subarray, or explicitly grouped authoring subarrays")
    _vfail(root_block(lines, system).one("SystemType")[1] != "Grid", "SystemType must be Grid")
    blocks = objects("".join(lines))
    flags = next(b for b in blocks if not b.path).one("Flags")[1]
    _vfail(not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags), "Root Flags must be hexadecimal")
    has_load = any(b.object_type == "pvLoad" for b in blocks)
    _vfail(bool(int(flags[1:], 16) & 0x00080000) and not has_load, "Root load bit is set without a pvLoad record")
    _vfail(has_load or any(b.object_type == "pvGridStorage" for b in blocks), "Loads and storage are outside this batch")
    _vfail(any(re.match(r"\s*(?:P50_P90|PVObject_Horizon)\s*=", ln) for ln in lines), "P50/P90 and saved horizon objects are outside this batch")
    store._orientation_list(lines)
    store._require_no_geometry(lines)
    inv_check = grid_inventory(lines, arrays)
    _vfail(inv_check["counts_valid"] is not True, "Requires complete consistent native grid counts")
    has_shared = any("PowerSharingInverterId" in root_block(lines, b).fields
                     and root_block(lines, b).one("PowerSharingInverterId")[1] != "-1" for b in arrays.values())
    profiles = []
    for identifier, bounds in sorted(arrays.items()):
        sub_root = root_block(lines, bounds)
        series, strings = _finite(float(sub_root.one("NModSerie")[1]), "NModSerie", positive=True), _finite(float(sub_root.one("NStringCh")[1]), "NStringCh", positive=True)
        _vfail(not series.is_integer() or not strings.is_integer(), "Source module counts must be integers")
        orientation_id = sub_root.one("NoOrientation")[1]
        _vfail(not orientation_id.isdigit(), "NoOrientation must be a positive integer")
        plane = store._orientation(lines, int(orientation_id))
        store._ordinary_fixed_plane(lines, plane)
        plane_root = root_block(lines, plane)
        tilt = _finite(float(plane_root.one("FieldTilt")[1]), "FieldTilt", minimum=0.0, maximum=90.0)
        _vfail(tilt >= 90.0, "FieldTilt must stay below 90 degrees")
        azimuth, pan_name = _finite(float(plane_root.one("FieldAzim")[1]), "FieldAzim", minimum=-180.0, maximum=180.0), sub_root.one("PVModule")[1]
        if len(arrays) > 1:
            ps_id = sub_root.one("PowerSharingInverterId")[1] if "PowerSharingInverterId" in sub_root.fields else "-1"
            inv_row = next(row for row in inv_check["subarrays"] if row["id"] == identifier)
            if ps_id == "-1" and not has_shared:
                branches, owned = store._subarray_branches(lines, arrays, identifier)
                store._require_plane_branch_mode(owned, branches)
            elif ps_id == "-1":
                _vfail(bool(inv_row["shared_inverter_lines"]), "Subarray without PowerSharingInverterId cannot occupy a shared InverterNode")
                branches = [(node.line - 1, node.end_line - 1, list(_walk(node)))
                            for tree in circuit_nodes(lines) for node in _walk(tree)
                            if node.kind == "InverterNode" and str(identifier) in [m.one("SubArrayId") for m in _walk(node)]]
                _vfail(not branches, "Subarray has no owning InverterNode branch")
                store._require_plane_branch_mode(sub_root, branches)
                owned = sub_root
            else:
                _vfail(not ps_id.isdigit(), "PowerSharingInverterId must be -1 or a non-negative integer")
                sub_flags = sub_root.one("Flags")[1] if "Flags" in sub_root.fields else ""
                _vfail(sub_root.one("Version")[1] not in NATIVE_TEXT_OR_SEED or not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", sub_flags)
                       or (int(sub_flags[1:], 16) & 0x0500) != 0x0500, "Power-sharing subarray requires verified Version and $0500 flags")
                _vfail(not inv_row["shared_inverter_lines"], "Subarray declares PowerSharingInverterId without a shared circuit parent")
                owned = sub_root
            _vfail(owned.one("Version")[1] not in NATIVE_TEXT_OR_SEED or owned.one("NStrOrient1")[1] != owned.one("NStringCh")[1]
                   or any(k.startswith("NStrOrient") and k != "NStrOrient1" and owned.one(k)[1] != "0" for k in owned.fields),
                   "Grouped authoring requires verified single-orientation exclusive branches")
        profiles.append({"id": int(orientation_id), "subarray_id": int(identifier), "module_total": int(series * strings),
                         "tilt_deg": tilt, "field_azimuth_deg": azimuth, "azimuth_deg": (180.0 + azimuth) % 360.0, "pan_name": pan_name})
    if len(arrays) > 1:
        _vfail(any(p["id"] < 1 for p in profiles), "Grouped authoring requires positive source orientations")
    if len(arrays) == 1 or len({p["id"] for p in profiles}) == len(profiles):
        for p in profiles:
            p.pop("subarray_id", None)
    profiles.sort(key=lambda item: (item["id"], item.get("subarray_id", item["id"])))
    sit = next((b for b in blocks if b.object_type == "pvSite" and b.path == "PVObject_SiteSimul"), None)
    _vfail(sit is None, "Requires an embedded pvSite record for geographic coverage")
    latitude = _finite(float(sit.one("Latitude")[1]), "Latitude", minimum=-90.0, maximum=90.0)
    longitude, altitude = _finite(float(sit.one("Longitude")[1]), "Longitude", minimum=-180.0, maximum=180.0), _finite(float(sit.one("Altitude")[1]), "Altitude")
    _vfail(latitude <= 0, "The azimuth conversion (180 + FieldAzim) % 360 is only verified for the northern hemisphere; a non-positive site latitude stops here")
    result = {**{k: profiles[0][k] for k in ("tilt_deg", "field_azimuth_deg", "azimuth_deg")},
              "module_total": sum(p["module_total"] for p in profiles), "pan_name": profiles[0]["pan_name"],
              "site": {"latitude": latitude, "longitude": longitude, "altitude": altitude}}
    if len(profiles) > 1:
        result["orientations"] = profiles
    return result

def house_mesh(profile):
    """Closed box for a flat roof; symmetric gable prism with ridge parallel to local X."""
    length, width, eave = profile["length_m"], profile["width_m"], profile["eave_height_m"]
    base = [(x, y, z) for z in (0, eave) for x, y in ((0, 0), (length, 0), (length, width), (0, width))]
    if profile["kind"] == "flat":
        return base, [tuple(f) for f in _box_faces()]
    ridge, half = eave + profile["roof_rise_m"], width / 2.0
    return base + [(0, half, ridge), (length, half, ridge)], [
        (0, 2, 1), (0, 3, 2), (0, 1, 5), (0, 5, 4), (3, 6, 2), (3, 7, 6), (0, 4, 8), (0, 8, 7),
        (0, 7, 3), (1, 2, 6), (1, 6, 9), (1, 9, 5), (4, 9, 8), (4, 5, 9), (7, 8, 9), (7, 9, 6)]

def rack_mesh(profile):
    """Positive rectangle: local origin at low south-west corner, +Y runs up the slope."""
    tilt, span, slope = math.radians(profile["tilt_deg"]), profile["column_span_m"], profile["slope_span_m"]
    down, drop = slope * math.cos(tilt), slope * math.sin(tilt)
    for name, value in (("down_slope_m", down), ("drop_m", drop)):
        _vfail(not math.isfinite(value) or value < 0 or (name == "down_slope_m" and value == 0), f"Derived {name} is outside the fixed-table geometry range")
    return [(0.0, 0.0, 0.0), (span, 0.0, 0.0), (span, down, drop), (0.0, down, drop)], [(0, 1, 2), (0, 2, 3)]

def _append_mesh(parent, geometry_id, points, faces):
    geometry = _el(parent, NS, "geometry", {"id": geometry_id})
    mesh = _el(geometry, NS, "mesh")
    array = _el(mesh, NS, "source", {"id": geometry_id + "-positions"})
    _el(array, NS, "float_array", {"id": geometry_id + "-positions-array", "count": str(len(points) * 3)}, " ".join(_num(axis) for point in points for axis in point))
    accessor = _el(_el(array, NS, "technique_common"), NS, "accessor", {"source": "#" + geometry_id + "-positions-array", "count": str(len(points)), "stride": "3"})
    for axis in ("X", "Y", "Z"):
        _el(accessor, NS, "param", {"name": axis, "type": "float"})
    _el(_el(mesh, NS, "vertices", {"id": geometry_id + "-vertices"}), NS, "input", {"semantic": "POSITION", "source": "#" + geometry_id + "-positions"})
    triangles = _el(mesh, NS, "triangles", {"count": str(len(faces))})
    _el(triangles, NS, "input", {"semantic": "VERTEX", "source": "#" + geometry_id + "-vertices", "offset": "0"})
    _el(triangles, NS, "p", text=" ".join(str(v) for face in faces for v in face))
    return geometry

def _pv_extra(parent):
    return _el(_el(parent, NS, "extra"), NS, "technique", {"profile": "PVCollada-2.0"})

def _rack_origin(house, layout):
    """Return low south-west table corner coordinates."""
    if house["kind"] == "mesh":
        return layout["origin_m"]
    margin = layout["edge_margin_m"]
    if house["kind"] == "flat":
        return [margin, margin, house["eave_height_m"] + layout["clearance_m"]]
    pitch, along = math.radians(house["pitch_deg"]), margin * math.cos(math.radians(house["pitch_deg"]))
    _vfail(along + layout["slope_span_m"] * math.cos(math.radians(layout["tilt_deg"])) > house["width_m"] / 2.0,
           "edge_margin_m leaves no room for the table before the ridge")
    return [margin, along, house["eave_height_m"] + margin * math.sin(pitch) + layout["clearance_m"]]

def check_mesh_fit(house, layout, corners, points):
    roof = next((p for p in house["meshes"] if p["name"] == layout["roof_mesh"]), None)
    _vfail(roof is None, "roof_mesh must reference an existing house mesh name")
    vertices, faces = roof["vertices"], roof["triangles"]
    a = vertices[faces[0][0]]
    normal = _normal(*(vertices[i] for i in faces[0]))
    _vfail(normal[2] <= TOL, "The supporting roof mesh must face upward")
    _vfail(_dot(normal, _normal(*points[:3])) < 1.0 - TOL, "Supporting roof normal must match the source fixed PV plane")
    _vfail(any(abs(_dot(normal, _sub(v, a))) > TOL for v in vertices), "The supporting roof mesh must be planar")
    _vfail(any(_dot(normal, _normal(*(vertices[i] for i in f))) < 1.0 - TOL for f in faces), "Supporting roof triangles must face consistently upward")
    for x, y, z in corners:
        roof_z = a[2] - (normal[0] * (x - a[0]) + normal[1] * (y - a[1])) / normal[2]
        _vfail(z < roof_z - TOL, "PV corner sinks below the selected roof plane")
        _vfail(not any(_inside_triangle((x, y, roof_z), *(vertices[i] for i in f)) for f in faces), "PV corner projects outside the supporting roof mesh")

def check_fit(house, layout, origin, points):
    """Check four projected corners against the house roof or supporting mesh."""
    corners = [(origin[0] + p[0], origin[1] + p[1], origin[2] + p[2]) for p in points]
    _vfail(any(not math.isfinite(v) for c in corners for v in c), "Table corner is not finite")
    if house["kind"] == "mesh":
        return check_mesh_fit(house, layout, corners, points)
    _vfail(max(c[0] for c in corners) > house["length_m"] + TOL, "Table projects past the house end")
    if house["kind"] == "flat":
        _vfail(max(c[1] for c in corners) > house["width_m"] + TOL, "Table projects past the house side")
        _vfail(min(c[2] for c in corners) < house["eave_height_m"] - TOL, "Table sinks below the flat roof surface")
        return
    _vfail(abs(layout["tilt_deg"] - house["pitch_deg"]) > TOL, "Gable roof requires the PV tilt to equal the roof pitch")
    half, slope = house["width_m"] / 2.0, math.tan(math.radians(house["pitch_deg"]))
    for _, y, z in corners:
        _vfail(not -TOL <= y <= half + TOL, "Table corner is not on the front roof face")
        _vfail(z < house["eave_height_m"] + y * slope - TOL, "Table corner sinks below the front roof plane")

def _els(parent, ns, items):
    for tag_name, text in items:
        _el(parent, ns, tag_name, text=text)

def build_scene(house, tables, modules, source, created, recipe=None, obstacles=None):
    """Build minimal COLLADA document with shared rotation node, house meshes, obstacles, and PV racks."""
    ET.register_namespace("", NS)
    ET.register_namespace("pv", PV)
    root = ET.Element(_tag(NS, "COLLADA"), {"version": "1.5.0"})
    asset = _el(root, NS, "asset")
    _el(_el(asset, NS, "contributor"), NS, "authoring_tool", text=AUTHORING_TOOL)
    coverage = _el(_el(asset, NS, "coverage"), NS, "geographic_location")
    _els(coverage, NS, (("longitude", _num(source["site"]["longitude"])), ("latitude", _num(source["site"]["latitude"]))))
    _el(coverage, NS, "altitude", {"mode": "absolute"}, _num(source["site"]["altitude"]))
    _els(asset, NS, (("created", created), ("modified", created)))
    if recipe is not None:
        _el(asset, NS, "subject", text=json.dumps(recipe, ensure_ascii=True, separators=(",", ":"), allow_nan=False))
    _el(asset, NS, "unit", {"meter": "1", "name": "m"})
    _el(asset, NS, "up_axis", text="Z_UP")
    _els(_el(_el(_el(_pv_extra(asset), PV, "components"), PV, "modules"), PV, "module", {"id": "ModuleModel1"}), PV, (
        ("name", modules["name"]), ("nom_power", _num(modules["nom_power"])), ("length", _num(modules["length_m"] * 1000.0)),
        ("width", _num(modules["width_m"] * 1000.0)), ("depth", _num(modules["depth_m"] * 1000.0))))
    geometries = _el(root, NS, "library_geometries")
    parts = house["meshes"] if house["kind"] == "mesh" else [{"name": "House", "vertices": house_mesh(house)[0], "triangles": house_mesh(house)[1]}]
    for prefix, items in (("House", parts), ("Obstacle", obs_list := list(obstacles or ()))):
        for i, item in enumerate(items, 1):
            _append_mesh(geometries, f"{prefix}Part{i}", item["vertices"], item["triangles"]).set("name", item["name"])
    nodes = _el(root, NS, "library_nodes")
    for i, layout in enumerate(tables, 1):
        rack = _append_mesh(geometries, f"Rack{i}", *rack_mesh(layout))
        _els(_el(_pv_extra(rack), PV, "rack"), PV, (
            ("rack_type", "fixed_tilt"), ("row_spacing", _num(layout["row_spacing_m"])), ("column_spacing", _num(layout["column_spacing_m"])),
            ("module_rows", str(layout["module_rows"])), ("module_columns", str(layout["module_columns"])),
            ("module_orientation", layout["orientation"]), ("tilt", _num(layout["tilt_deg"])),
            ("azimuth", _num(layout.get("azimuth_deg", source["azimuth_deg"]))), ("module_id", "ModuleModel1")))
        table_node = _el(nodes, NS, "node", {"id": f"TableModel{i}"})
        _el(_pv_extra(_el(table_node, NS, "instance_geometry", {"url": f"#Rack{i}"})), PV, "instance_rack", {"id": f"rackInst{i}"})
        _el(_el(_pv_extra(table_node), PV, "table"), PV, "type", text="fixed")
    shared = _el(_el(_el(root, NS, "library_visual_scenes"), NS, "visual_scene", {"id": "VisualSceneNode"}), NS, "node", {"id": "SceneRoot"})
    turn = 180.0 - source["azimuth_deg"]
    _el(shared, NS, "rotate", text="0 0 1 " + _num(turn))
    for prefix, items in (("House", parts), ("Obstacle", obs_list)):
        for i, item in enumerate(items, 1):
            _el(_el(shared, NS, "node", {"id": f"{prefix}Node{i}", "name": item["name"]}), NS, "instance_geometry", {"url": f"#{prefix}Part{i}"})
    summaries = []
    for i, layout in enumerate(tables, 1):
        origin = _rack_origin(house, layout)
        pv_node = _el(shared, NS, "node", {"id": f"PVTable{i}", "name": layout.get("name", "PV table")})
        _el(pv_node, NS, "translate", text=" ".join(_num(v) for v in origin))
        if "rotation_deg" in layout:
            _el(pv_node, NS, "rotate", text="0 0 1 " + _num(layout["rotation_deg"]))
        arr = _el(_el(_pv_extra(_el(pv_node, NS, "instance_node", {"url": f"#TableModel{i}"})),
                      PV, "instance_table", {"id": f"Table{i}"}), PV, "instance_racks_array")
        _el(arr, PV, "instance_rack_ref", {"id": f"rackRef{i}", "url": f"#rackInst{i}"})
        summaries.append({
            "name": layout.get("name", "PV table"), "origin_m": origin,
            **({"orientation_id": layout["orientation_id"]} if "orientation_id" in layout else {}),
            "module_total": layout["module_rows"] * layout["module_columns"],
            "corners_m": [[origin[k] + p[k] for k in range(3)] for p in _placed_points(layout)]})
    _el(_el(root, NS, "scene"), NS, "instance_visual_scene", {"url": "#VisualSceneNode"})
    counts = {"house_vertices": sum(len(p["vertices"]) for p in parts), "house_faces": sum(len(p["triangles"]) for p in parts),
              "house_parts": len(parts), "part_names": [p["name"] for p in parts], "rack_vertices": 4 * len(tables),
              "rack_faces": 2 * len(tables), "pv_table_count": len(tables), "tables": summaries, "rotate_deg": turn,
              **({"origin_m": summaries[0]["origin_m"], "corners_m": summaries[0]["corners_m"]} if len(tables) == 1 else {})}
    if obs_list:
        counts.update(obstacle_count=len(obs_list), obstacle_names=[o["name"] for o in obs_list],
                      tree_count=sum(1 for o in obs_list if o["kind"] == "tree"), obstacle_mesh_count=sum(1 for o in obs_list if o["kind"] != "tree"))
    return root, counts

def create_house_pv_scene(store, components, *, project, variant, output_name, house, layout,
                          dry_run=True, module_geometry=None, string_layout=None, obstacles=None, source_variant_bytes=None):
    """Preview or write one new .pvc2; the project, variant and PAN are never modified."""
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run, "issues": [], "diagnostics": [],
              "warnings": [], "artifacts": {}, "native_consumption_verified": False, "imported": False}
    with store.lock:
        try:
            _vfail(type(dry_run) is not bool, "Use a strict boolean")
            name = safe_filename(output_name, ".pvc2")
            if source_variant_bytes is not None:
                _vfail(not dry_run, "In-memory source_variant_bytes is only permitted during dry_run preview")
                source_path, original = None, bytes(source_variant_bytes)
            else:
                store._check_project(project)
                source_path = store._path(project, variant)
                original = store._read(source_path)
            arrays, system = store._sections(lines := store._text(original)[1])
            source = source_profile(store, lines, arrays, system, allow_groups=True)
            modules, pan_snapshots = resolve_source_geometry(components, source, module_geometry, layout)
            pocket = house_profile(house)
            obstacle_records = obstacle_profiles(obstacles, existing_names=[m["name"] for m in pocket["meshes"]] if pocket["kind"] == "mesh" else ["House"])
            tables = table_profiles(layout, pocket, modules, source)
            check_tables(pocket, tables)
            binding, binding_state = _string_layout_profile(string_layout, tables)
            declared = {"project": project, "variant": variant, "pan": source["pan_name"], "module_total": source["module_total"]}
            version = 1
            if "orientations" in source:
                orientation_ids = [i["id"] for i in source["orientations"]]
                if len(set(orientation_ids)) == len(orientation_ids):
                    declared["orientations"], version = [{"id": i["id"], "module_total": i["module_total"]} for i in source["orientations"]], 2
                else:
                    declared["owners"], version = [{"subarray_id": i.get("subarray_id", i["id"]), "orientation_id": i["id"], "module_total": i["module_total"]} for i in source["orientations"]], 3
            recipe = {"version": version, "source": declared, "house": {"kind": "mesh"} if pocket["kind"] == "mesh" else house,
                      "layout": layout, **({"obstacles": [o["spec"] for o in obstacle_records]} if obstacle_records else {}),
                      **({"string_layout": binding} if binding is not None else {})}
            root, counts = build_scene(pocket, tables, modules, source, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), recipe, obstacles=obstacle_records or None)
            payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            try:
                ET.fromstring(payload)
            except ET.ParseError as exc:
                raise ValueError("Module metadata must be valid XML text") from exc
            target = workspace_file(store.workspace, "UserData", name, ".pvc2")
            if target.parent.is_dir() and any(p.name.casefold() == target.name.casefold() for p in target.parent.iterdir()):
                raise FileExistsError("Choose a new scene file: " + name)
            result.update(
                status="ready", project=project, variant=variant, output=name, modeling_mode="ai-meshes" if pocket["kind"] == "mesh" else "legacy-parametric",
                artifacts={"scene_file": str(target)},
                source={"project": project, "variant": variant, "pan": source["pan_name"], "module_total": source["module_total"],
                        "field_tilt_deg": source["tilt_deg"], "field_azimuth_deg": source["field_azimuth_deg"], "collada_azimuth_deg": source["azimuth_deg"],
                        "latitude": source["site"]["latitude"], **({"orientations": source["orientations"]} if "orientations" in source else {})},
                modules=modules, house=({"kind": "mesh", "part_count": len(pocket["meshes"])} if pocket["kind"] == "mesh" else pocket),
                layout={"tables": tables} if "tables" in layout else tables[0], geometry=counts,
                **({"obstacles": [o["spec"] for o in obstacle_records]} if obstacle_records else {}), bytes=len(payload),
                **({"string_layout_state": binding_state} if binding_state is not None else {}))
            result["warnings"].extend([
                "Module metadata provenance: " + modules["metadata_source"] + ". Geometry uses metres; only pv:module dimensions use millimetres.",
                "Azimuth uses the COLLADA convention (north = 0, clockwise) and is only verified for a northern-hemisphere site. "
                + ("House and table origins use the orientation1 anchor frame; grouped PV nodes additionally inherit their own source-relative Z rotation."
                   if "orientations" in source else "House and table share one parent rotation, so the table translation is expressed in the house local frame.")])
            _vfail(source_path is not None and store._read(source_path) != original, "Source variant changed while preparing the scene file")
            for pan_path, pan_bytes in pan_snapshots:
                _vfail(components._read(pan_path) != pan_bytes, "Source PAN changed while preparing the scene file")
            if dry_run:
                return result
            result["stage"] = "write"
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(payload)
            result.update(status="created", stage="created")
        except (ValueError, OSError, UnicodeError, KeyError, TypeError) as exc:
            result["issues"].append(str(exc))
            result["diagnostics"] = exc.diagnostics if isinstance(exc, SceneValidationError) else [_diagnostic("scene_input", result["stage"], str(exc))]
            if isinstance(exc, SceneValidationError):
                result["diagnostics_truncated"] = exc.truncated
            result["status"] = "failed" if result["stage"] == "write" else "blocked"
            if result["status"] == "failed":
                result["warnings"].append("A partial scene file may exist at the disclosed path; the source project, variant and PAN are unchanged.")
    return result

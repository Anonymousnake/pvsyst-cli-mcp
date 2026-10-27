"""Serialize AI-authored house meshes and a fixed PV table into PVCollada 2.0."""
from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from pvsyst_auto.components.editor import objects
from pvsyst_auto.components.pan import validation_fields as pan_validation_fields
from pvsyst_auto.project.circuit import inventory as grid_inventory
from pvsyst_auto.project.paths import safe_filename, workspace_file

NS = "http://www.collada.org/2008/03/COLLADASchema"
PV = "https://pvcollada.org/2026/XMLSchema"
# Adapter tolerance for paired declarations and roof parallelism, not a native limit.
TOL = 1e-6
SMALLEST = 5e-7
ORIENT_CLOSE = "End of TOrientGroup"
HOUSE_KEYS = ("length_m", "width_m", "eave_height_m", "roof")
LAYOUT_KEYS = ("module_rows", "module_columns", "orientation", "row_spacing_m",
               "column_spacing_m", "clearance_m", "edge_margin_m")
GEOMETRY_KEYS = ("model_name", "nom_power_wp", "width_m", "length_m", "geometry_depth_m")

PLACED_LAYOUT_KEYS = ("module_rows", "module_columns", "orientation", "row_spacing_m",
                      "column_spacing_m", "origin_m", "roof_mesh")
# Adapter payload ceilings, not PVsyst limits or architectural constraints.
MAX_PARTS, MAX_VERTICES, MAX_TRIANGLES = 128, 50000, 100000
MAX_TABLES = 64
AUTHORING_TOOL = "pvsyst-cli-mcp/house-scene/1"


class SceneValidationError(ValueError):
    """Bounded, actionable input diagnostics; no automatic mutation of authored geometry."""
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics[:100]
        self.truncated = len(diagnostics) > 100
        super().__init__("; ".join(row["message"] for row in self.diagnostics[:3]))


def _diagnostic(code, path, message, *, part=None, triangle=None, repair_hint=None):
    result = {"code": code, "path": path, "message": message,
              "repair_hint": repair_hint or "Correct the indicated input and submit a new preview."}
    if part is not None:
        result["part"] = part
    if triangle is not None:
        result["triangle"] = triangle
    return result


def _name(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 120:
        raise ValueError("Names must be 1..120 characters without surrounding whitespace")
    return value


def _tag(namespace, name):
    return f"{{{namespace}}}{name}"


def _num(value):
    """Six-decimal text that never silently collapses a tiny nonzero coordinate to zero."""
    if not math.isfinite(value):
        raise ValueError("Coordinate is not finite")
    if value != 0.0 and abs(value) < SMALLEST:
        raise ValueError("Coordinate is too small to serialize at six decimals")
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _finite(value, label, positive=False, minimum=None, maximum=None):
    if type(value) is bool or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a strict finite number")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{label} is too large to represent") from None
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if positive and number <= 0:
        raise ValueError(f"{label} must be greater than zero")
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} must be <= {maximum}")
    return number


def _paired(fields, root, alias, label):
    """Both owning declarations must agree; a repeated or conflicting key stops instead of picking one."""
    found = []
    for name in (root, alias):
        entry = fields.get(name)
        if entry is None:
            continue
        if len(entry) != 1:
            raise ValueError(f"PAN {name} must occur exactly once")
        found.append((name, _finite(float(entry[0]), f"PAN {name}", positive=True)))
    if not found:
        raise ValueError(f"PAN does not declare {label}")
    if len(found) == 2 and abs(found[0][1] - found[1][1]) > TOL * max(1.0, abs(found[0][1])):
        raise ValueError(f"PAN {label} disagrees between {root} and {alias}")
    return found[0][1]


def _pick(fields, names, label):
    for name in names:
        entry = fields.get(name)
        if entry is None:
            continue
        if len(entry) != 1:
            raise ValueError(f"PAN {name} must occur exactly once")
        text = entry[0].strip()
        if not text:
            raise ValueError(f"PAN {name} must not be empty")
        return text
    raise ValueError(f"PAN does not declare {label}")


def module_profile(fields, explicit=None):
    """PAN width/height stay in metres here; only the XML text multiplies by 1000."""
    width = _paired(fields, "Width", "LargApp", "module width")
    height = _paired(fields, "Height", "LongApp", "module height")
    long_side, short_side = height, width
    power = _pick(fields, ("PNom", "PNomModule"), "nom_power")
    try:
        nom_power = float(power)
    except ValueError:
        raise ValueError("PAN nom_power must be a number") from None
    if not math.isfinite(nom_power) or nom_power <= 0:
        raise ValueError("PAN nom_power must be a finite positive number")
    name = _pick(fields, ("Model",), "Model")
    depth = _explicit_depth(explicit)
    if explicit is not None:
        if explicit["name"] != name:
            raise ValueError("module_geometry.model_name disagrees with the loose PAN")
        for label, actual, wanted in (("width_m", short_side, explicit["width_m"]),
                                      ("length_m", long_side, explicit["length_m"]),
                                      ("nom_power_wp", nom_power, explicit["nom_power"])):
            if abs(actual - wanted) > TOL * max(1.0, abs(actual)):
                raise ValueError(f"module_geometry.{label} disagrees with the loose PAN")
    return {"length_m": long_side, "width_m": short_side, "nom_power": nom_power,
            "name": name, "depth_m": depth, "depth_source": "caller-provided",
            "metadata_source": "source-pan"}


def _explicit_depth(explicit):
    if explicit is None:
        raise ValueError("module_geometry.geometry_depth_m is required: the table geometry needs a module "
                         "depth and no verified source field provides one")
    return explicit["depth_m"]


def caller_geometry(geometry):
    """Explicit module metadata for a native database reference; nothing is inferred from a filename."""
    if geometry is None:
        return None
    if not isinstance(geometry, dict) or set(geometry) != set(GEOMETRY_KEYS):
        raise ValueError("module_geometry must declare exactly " + ", ".join(GEOMETRY_KEYS))
    if not isinstance(geometry["model_name"], str) or not geometry["model_name"].strip():
        raise ValueError("module_geometry.model_name must be non-empty text")
    return {"name": geometry["model_name"].strip(),
            "nom_power": _finite(geometry["nom_power_wp"], "nom_power_wp", positive=True),
            "length_m": _finite(geometry["length_m"], "length_m", positive=True),
            "width_m": _finite(geometry["width_m"], "width_m", positive=True),
            "depth_m": _finite(geometry["geometry_depth_m"], "geometry_depth_m", positive=True)}


def resolve_modules(components, pan_name, geometry):
    """Workspace loose PAN first, then the installed library; a database reference needs explicit geometry."""
    explicit = caller_geometry(geometry)
    pan_path = None
    for library in ("workspace", "builtin"):
        if library == "builtin" and components.builtin is None:
            continue
        candidate = components._path("PAN", pan_name, library)
        if candidate.is_file():
            pan_path = candidate
            break
    if pan_path is None:
        if explicit is None:
            raise ValueError("The referenced PAN is neither a loose workspace nor an installed-library file; "
                             "module_geometry is required and no vendor value is guessed")
        return explicit | {"metadata_source": "caller-provided-for-native-database-reference",
                           "depth_source": "caller-provided"}, None
    pan_bytes = components._read(pan_path)
    inspected = components._inspect("PAN", pan_bytes)
    if inspected["format"] != "text":
        if explicit is None:
            raise ValueError("The referenced PAN is legacy binary and cannot be parsed; module_geometry is required")
        return explicit | {"metadata_source": "caller-provided-for-native-database-reference",
                           "depth_source": "caller-provided"}, (pan_path, pan_bytes)
    if inspected["structural_errors"]:
        raise ValueError("PAN has structural errors: " + "; ".join(inspected["structural_errors"]))
    if inspected["known_value_errors"]:
        raise ValueError("PAN has value errors: " + "; ".join(inspected["known_value_errors"]))
    _, pan_text = components._text("PAN", pan_bytes)
    return module_profile(pan_validation_fields(pan_text), explicit), (pan_path, pan_bytes)


def _vector(values, label):
    if not isinstance(values, list) or len(values) != 3:
        raise ValueError(f"{label} must contain three numbers")
    return [_finite(value, label) for value in values]


def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def _dot(a, b):
    value = sum(a[i] * b[i] for i in range(3))
    if not math.isfinite(value):
        raise ValueError("Derived geometry is not finite")
    return value


def _normal(a, b, c):
    u, v = _sub(b, a), _sub(c, a)
    normal = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2],
              u[0] * v[1] - u[1] * v[0]]
    length = math.hypot(*normal)
    if not math.isfinite(length) or length == 0:
        raise ValueError("Mesh triangles must be finite and non-degenerate")
    return [value / length for value in normal]


def mesh_house_profile(house):
    if set(house) != {"kind", "meshes"} or not isinstance(house["meshes"], list):
        raise SceneValidationError([_diagnostic("mesh_fields", "house.meshes",
                                    "A mesh house must declare exactly kind and meshes")])
    if not 1 <= len(house["meshes"]) <= MAX_PARTS:
        raise SceneValidationError([_diagnostic("mesh_count", "house.meshes",
                                    f"Declare 1..{MAX_PARTS} named house meshes")])
    parts, names, vertex_count, triangle_count, errors = [], set(), 0, 0, []
    for index, part in enumerate(house["meshes"]):
        path = f"house.meshes[{index}]"
        if not isinstance(part, dict) or set(part) != {"name", "vertices", "triangles"}:
            errors.append(_diagnostic("mesh_fields", path,
                                      "Each mesh must declare exactly name, vertices and triangles"))
            continue
        try:
            name = _name(part["name"])
        except ValueError as exc:
            errors.append(_diagnostic("mesh_name", path + ".name", str(exc)))
            continue
        if name.casefold() in names:
            errors.append(_diagnostic("duplicate_mesh_name", path + ".name",
                                      "House mesh names must be unique, ignoring case", part=name))
        names.add(name.casefold())
        vertices, triangles = part["vertices"], part["triangles"]
        if not isinstance(vertices, list) or len(vertices) < 3:
            errors.append(_diagnostic("mesh_vertices", path + ".vertices",
                                      "Each mesh needs at least three vertices", part=name))
            continue
        if not isinstance(triangles, list) or not triangles:
            errors.append(_diagnostic("mesh_triangles", path + ".triangles",
                                      "Each mesh needs explicit triangle indices", part=name))
            continue
        vertex_count += len(vertices)
        triangle_count += len(triangles)
        if vertex_count > MAX_VERTICES or triangle_count > MAX_TRIANGLES:
            errors.append(_diagnostic("mesh_ceiling", path,
                                      "House mesh payload exceeds the adapter vertex/triangle ceiling",
                                      part=name))
            break
        # Validate the coordinates that will actually be written, including collapsed edges.
        serialized = []
        for vi, vertex in enumerate(vertices):
            try:
                serialized.append([float(_num(value)) for value in _vector(vertex, "Mesh vertex")])
            except ValueError as exc:
                serialized.append(None)
                errors.append(_diagnostic("mesh_coordinate", f"{path}.vertices[{vi}]", str(exc), part=name))
        seen_faces = set()
        for ti, triangle in enumerate(triangles):
            face_path = f"{path}.triangles[{ti}]"
            if (not isinstance(triangle, list) or len(triangle) != 3
                    or any(type(index) is not int or not 0 <= index < len(vertices) for index in triangle)
                    or len(set(triangle)) != 3):
                errors.append(_diagnostic("triangle_indices", face_path,
                                          "Triangles require three distinct, in-range, zero-based integer indices",
                                          part=name, triangle=ti))
                continue
            if any(serialized[vertex] is None for vertex in triangle):
                continue
            points = [serialized[vertex] for vertex in triangle]
            try:
                _normal(*points)
                key = tuple(sorted(tuple(point) for point in points))
                if key in seen_faces:
                    raise ValueError("A coincident triangle is repeated within this mesh")
                seen_faces.add(key)
            except ValueError as exc:
                errors.append(_diagnostic("triangle_geometry", face_path, str(exc), part=name, triangle=ti,
                                          repair_hint="Replace the indicated triangle; check vertex coordinates, "
                                                      "six-decimal collapse and duplicated faces."))
        parts.append({"name": name, "vertices": serialized, "triangles": triangles})
        if len(errors) > 100:
            break
    if errors:
        raise SceneValidationError(errors)
    return {"kind": "mesh", "meshes": parts}


def _inside_triangle(point, a, b, c):
    # Corner containment only; this is not a solid/mesh-intersection engine.
    normal = _normal(a, b, c)
    for start, end in ((a, b), (b, c), (c, a)):
        u, v = _sub(end, start), _sub(point, start)
        cross = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2],
                 u[0] * v[1] - u[1] * v[0]]
        if _dot(cross, normal) < -TOL * max(1.0, math.hypot(*u)):
            return False
    return True


def house_profile(house):
    """Prefer AI-authored meshes; keep the original rectangular roof inputs compatible."""
    if isinstance(house, dict) and house.get("kind") == "mesh":
        return mesh_house_profile(house)
    if not isinstance(house, dict) or set(house) != set(HOUSE_KEYS):
        raise ValueError("house must declare exactly length_m, width_m, eave_height_m and roof")
    length = _finite(house["length_m"], "length_m", positive=True)
    width = _finite(house["width_m"], "width_m", positive=True)
    eave = _finite(house["eave_height_m"], "eave_height_m", positive=True)
    roof = house["roof"]
    if not isinstance(roof, dict) or roof.get("kind") not in ("flat", "gable"):
        raise ValueError("roof.kind must be flat or gable")
    if roof["kind"] == "flat":
        if set(roof) != {"kind"}:
            raise ValueError("flat roof accepts only kind")
        return {"kind": "flat", "length_m": length, "width_m": width,
                "eave_height_m": eave, "roof_rise_m": 0.0, "pitch_deg": None}
    if set(roof) != {"kind", "pitch_deg"}:
        raise ValueError("gable roof accepts only kind and pitch_deg")
    pitch = _finite(roof["pitch_deg"], "roof.pitch_deg", positive=True, maximum=90.0)
    if not 0.0 < pitch < 90.0:
        raise ValueError("roof.pitch_deg must be strictly between 0 and 90 degrees")
    ridge = (width / 2.0) * math.tan(math.radians(pitch))
    if not math.isfinite(ridge):
        raise ValueError("Derived ridge height is not finite")
    return {"kind": "gable", "length_m": length, "width_m": width, "eave_height_m": eave,
            "roof_rise_m": ridge, "pitch_deg": pitch}


def layout_profile(layout, modules, total, source_tilt, *, placed=False):
    """One fixed table; tilt and azimuth are inherited, never requested."""
    keys = PLACED_LAYOUT_KEYS if placed else LAYOUT_KEYS
    if not isinstance(layout, dict) or set(layout) != set(keys):
        raise ValueError("layout must declare exactly " + ", ".join(keys))
    rows, columns = layout["module_rows"], layout["module_columns"]
    for name, value in (("module_rows", rows), ("module_columns", columns)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a strict positive integer")
    if total is not None and rows * columns != total:
        raise ValueError(f"module_rows x module_columns must equal the source module count {total}")
    if layout["orientation"] not in ("portrait", "landscape"):
        raise ValueError("orientation must be portrait or landscape")
    spacing_keys = ("row_spacing_m", "column_spacing_m") if placed else (
        "row_spacing_m", "column_spacing_m", "clearance_m", "edge_margin_m")
    spacing = {name: _finite(layout[name], name, minimum=0.0) for name in spacing_keys}
    if placed:
        spacing["origin_m"] = _vector(layout["origin_m"], "origin_m")
        if not isinstance(layout["roof_mesh"], str) or not layout["roof_mesh"].strip():
            raise ValueError("roof_mesh must name the planar supporting house mesh")
        spacing["roof_mesh"] = layout["roof_mesh"]
    depth = modules["depth_m"]
    along = modules["width_m"] if layout["orientation"] == "landscape" else modules["length_m"]
    across = modules["length_m"] if layout["orientation"] == "landscape" else modules["width_m"]
    column_span = columns * across + (columns - 1) * spacing["column_spacing_m"]
    slope_span = rows * along + (rows - 1) * spacing["row_spacing_m"]
    for name, value in (("column_span_m", column_span), ("slope_span_m", slope_span)):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"Derived {name} must be finite and positive")
    return {"module_rows": rows, "module_columns": columns, "orientation": layout["orientation"],
            **spacing,
            "geometry_depth_m": depth, "depth_source": "caller-provided",
            "tilt_deg": source_tilt, "column_span_m": column_span, "slope_span_m": slope_span}


def table_profiles(layout, house, modules, source):
    """Keep legacy tables unchanged; two source groups require one explicit table each."""
    groups = {item["id"]: item for item in source.get("orientations", [])}
    if not isinstance(layout, dict) or "tables" not in layout:
        if groups:
            raise ValueError("Two source orientations require two named tables with orientation_id")
        return [layout_profile(layout, modules, source["module_total"], source["tilt_deg"],
                               placed=house["kind"] == "mesh")]
    if (house["kind"] != "mesh" or set(layout) != {"tables"}
            or not isinstance(layout["tables"], list) or not 1 <= len(layout["tables"]) <= MAX_TABLES):
        raise ValueError(f"Named tables require a mesh house and 1..{MAX_TABLES} table entries")
    if groups and len(layout["tables"]) != 2:
        raise ValueError("Grouped authoring requires exactly one table per orientation")
    result, names, errors, assigned = [], set(), [], set()
    for index, entry in enumerate(layout["tables"]):
        path = f"layout.tables[{index}]"
        try:
            keys = set(PLACED_LAYOUT_KEYS) | {"name"} | ({"orientation_id"} if groups else set())
            if not isinstance(entry, dict) or set(entry) != keys:
                raise ValueError("Each named table requires exactly its name, placement and source orientation fields")
            name = _name(entry["name"])
            if name.casefold() in names:
                raise ValueError("PV table names must be unique, ignoring case")
            names.add(name.casefold())
            group = source
            if groups:
                identifier = entry["orientation_id"]
                if type(identifier) is not int or identifier not in groups or identifier in assigned:
                    raise ValueError("orientation_id must select each of source orientations 1/2 exactly once")
                assigned.add(identifier)
                group = groups[identifier]
            profile = layout_profile({key: entry[key] for key in PLACED_LAYOUT_KEYS}, modules,
                                     group["module_total"] if groups else None, group["tilt_deg"], placed=True)
            if groups:
                profile.update(orientation_id=identifier, azimuth_deg=group["azimuth_deg"],
                               rotation_deg=source["azimuth_deg"] - group["azimuth_deg"])
            result.append(profile | {"name": name})
        except (ValueError, KeyError) as exc:
            errors.append(_diagnostic("pv_table_input", path, str(exc),
                                      repair_hint="Correct this named table without changing other valid regions."))
    if errors:
        raise SceneValidationError(errors)
    total = sum(table["module_rows"] * table["module_columns"] for table in result)
    if total != source["module_total"]:
        raise SceneValidationError([_diagnostic("pv_module_total", "layout.tables",
            f"All PV tables together must contain {source['module_total']} modules, not {total}",
            repair_hint="Allocate the existing source modules across tables; do not duplicate the full array.")])
    return result


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
            errors.append(_diagnostic("pv_roof_fit", path, str(exc),
                repair_hint="Check this table's roof_mesh, origin_m and footprint against the source PV plane."))
    for index, table in enumerate(tables):
        a = _rack_origin(house, table)
        down = table["slope_span_m"] * math.cos(math.radians(table["tilt_deg"]))
        for other_index, other in enumerate(tables[:index]):
            b = _rack_origin(house, other)
            if "orientation_id" in table:
                corners_a = [[a[k] + p[k] for k in range(3)] for p in _placed_points(table)]
                corners_b = [[b[k] + p[k] for k in range(3)] for p in _placed_points(other)]
                if all(min(max(p[k] for p in corners_a), max(p[k] for p in corners_b))
                       - max(min(p[k] for p in corners_a), min(p[k] for p in corners_b)) > TOL for k in (0, 1)):
                    errors.append(_diagnostic("pv_group_footprints", f"layout.tables[{index}]",
                        "Grouped authoring requires disjoint XY bounding footprints, not a physical intersection test",
                        repair_hint="Use separate projected regions; native 3D interference remains a separate check."))
                continue
            other_down = other["slope_span_m"] * math.cos(math.radians(other["tilt_deg"]))
            offset = (a[2] - b[2]) - math.tan(math.radians(table["tilt_deg"])) * (a[1] - b[1])
            overlap_x = min(a[0] + table["column_span_m"], b[0] + other["column_span_m"]) - max(a[0], b[0])
            overlap_y = min(a[1] + down, b[1] + other_down) - max(a[1], b[1])
            if abs(offset) <= TOL and overlap_x > TOL and overlap_y > TOL:
                errors.append(_diagnostic("pv_tables_overlap", f"layout.tables[{index}]",
                    f"PV table overlaps coplanar table {other_index}",
                    repair_hint="Separate these PV rectangles or explicitly reallocate their modules."))
    if errors:
        raise SceneValidationError(errors)


def _view(lines):
    return "".join(lines).replace(ORIENT_CLOSE, "End of PVObject pvOrient")


def _block_of(lines, bounds):
    blocks = objects(_view(lines[bounds[0]:bounds[1] + 1]))
    root = next((block for block in blocks if not block.path), None)
    if root is None:
        raise ValueError("Missing owning record")
    return root


def source_profile(store, lines, arrays, system, *, allow_groups=False):
    """The native bridge stays single-plane; only the authoring path opts into two groups."""
    if system is None or len(arrays) not in ((1, 2) if allow_groups else (1,)):
        raise ValueError("Requires one pvSystem and one subarray, or two explicitly grouped authoring subarrays")
    if _block_of(lines, system).one("SystemType")[1] != "Grid":
        raise ValueError("SystemType must be Grid")
    blocks = objects(_view(lines))
    root = next(block for block in blocks if not block.path)
    flags = root.one("Flags")[1]
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags):
        raise ValueError("Root Flags must be hexadecimal")
    has_load = any(block.object_type == "pvLoad" for block in blocks)
    if int(flags[1:], 16) & 0x00080000 and not has_load:
        raise ValueError("Root load bit is set without a pvLoad record")
    if has_load or any(block.object_type == "pvGridStorage" for block in blocks):
        raise ValueError("Loads and storage are outside this batch")
    if any(re.match(r"\s*(?:P50_P90|PVObject_Horizon)\s*=", line) for line in lines):
        raise ValueError("P50/P90 and saved horizon objects are outside this batch")
    store._orientation_list(lines)
    if sum(block.object_type == "pvOrient" for block in blocks) != len(arrays):
        raise ValueError("Requires one ordinary fixed plane per subarray")
    store._require_no_geometry(lines)
    if grid_inventory(lines, arrays)["counts_valid"] is not True:
        raise ValueError("Requires complete consistent native grid counts")
    profiles, models = [], []
    for identifier, bounds in sorted(arrays.items()):
        sub_root = _block_of(lines, bounds)
        series = _finite(float(sub_root.one("NModSerie")[1]), "NModSerie", positive=True)
        strings = _finite(float(sub_root.one("NStringCh")[1]), "NStringCh", positive=True)
        if not series.is_integer() or not strings.is_integer():
            raise ValueError("Source module counts must be integers")
        orientation_id = sub_root.one("NoOrientation")[1]
        if not orientation_id.isdigit():
            raise ValueError("NoOrientation must be a positive integer")
        plane = store._orientation(lines, int(orientation_id))
        store._ordinary_fixed_plane(lines, plane)
        plane_root = _block_of(lines, plane)
        tilt = _finite(float(plane_root.one("FieldTilt")[1]), "FieldTilt", minimum=0.0, maximum=90.0)
        if tilt >= 90.0:
            raise ValueError("FieldTilt must stay below 90 degrees")
        azimuth = _finite(float(plane_root.one("FieldAzim")[1]), "FieldAzim", minimum=-180.0, maximum=180.0)
        models.append((sub_root.one("PVModule")[1], sub_root.one("GInverter")[1] if len(arrays) == 2 else ""))
        if len(arrays) == 2:
            branches, owned = store._subarray_branches(lines, arrays, identifier)
            store._require_plane_branch_mode(owned, branches)
            if (owned.one("Version")[1] not in ("8.1.0", "8.1.6")
                    or owned.one("NStrOrient1")[1] != owned.one("NStringCh")[1]
                    or any(key.startswith("NStrOrient") and key != "NStrOrient1" and owned.one(key)[1] != "0"
                           for key in owned.fields)):
                raise ValueError("Grouped authoring requires verified single-orientation exclusive branches")
        profiles.append({"id": int(orientation_id), "module_total": int(series * strings), "tilt_deg": tilt,
                         "field_azimuth_deg": azimuth, "azimuth_deg": (180.0 + azimuth) % 360.0})
    if len(arrays) == 2 and ({p["id"] for p in profiles} != {1, 2}
                            or len({tuple(name.casefold() for name in pair) for pair in models}) != 1):
        raise ValueError("Two authoring groups require distinct orientations 1/2 and the same PAN/OND")
    profiles.sort(key=lambda item: item["id"])
    sit = next((block for block in blocks if block.object_type == "pvSite"
                and block.path == "PVObject_SiteSimul"), None)
    if sit is None:
        raise ValueError("Requires an embedded pvSite record for geographic coverage")
    latitude = _finite(float(sit.one("Latitude")[1]), "Latitude", minimum=-90.0, maximum=90.0)
    longitude = _finite(float(sit.one("Longitude")[1]), "Longitude", minimum=-180.0, maximum=180.0)
    altitude = _finite(float(sit.one("Altitude")[1]), "Altitude")
    if latitude <= 0:
        raise ValueError("The azimuth conversion (180 + FieldAzim) % 360 is only verified for the northern "
                         "hemisphere; a non-positive site latitude stops here")
    first = profiles[0]
    result = {key: first[key] for key in ("tilt_deg", "field_azimuth_deg", "azimuth_deg")}
    result.update(module_total=sum(p["module_total"] for p in profiles), pan_name=models[0][0],
                  site={"latitude": latitude, "longitude": longitude, "altitude": altitude})
    if len(profiles) == 2:
        result["orientations"] = profiles
    return result


def house_mesh(profile):
    """Closed box for a flat roof; symmetric gable prism with the ridge parallel to local X."""
    length, width, eave = profile["length_m"], profile["width_m"], profile["eave_height_m"]
    if profile["kind"] == "flat":
        points = [(0, 0, 0), (length, 0, 0), (length, width, 0), (0, width, 0),
                  (0, 0, eave), (length, 0, eave), (length, width, eave), (0, width, eave)]
        faces = [(0, 2, 1), (0, 3, 2), (0, 1, 5), (0, 5, 4), (3, 6, 2), (3, 7, 6),
                 (0, 4, 7), (0, 7, 3), (1, 2, 6), (1, 6, 5), (4, 5, 6), (4, 6, 7)]
        return points, faces
    ridge = eave + profile["roof_rise_m"]
    half = width / 2.0
    points = [(0, 0, 0), (length, 0, 0), (length, width, 0), (0, width, 0),
              (0, 0, eave), (length, 0, eave), (length, width, eave), (0, width, eave),
              (0, half, ridge), (length, half, ridge)]
    faces = [(0, 2, 1), (0, 3, 2),
             (0, 1, 5), (0, 5, 4), (3, 6, 2), (3, 7, 6),
             (0, 4, 8), (0, 8, 7), (0, 7, 3),
             (1, 2, 6), (1, 6, 9), (1, 9, 5),
             (4, 9, 8), (4, 5, 9), (7, 8, 9), (7, 9, 6)]
    return points, faces


def rack_mesh(profile):
    """Positive rectangle: local origin is the low south-west corner, +Y runs up the slope."""
    tilt = math.radians(profile["tilt_deg"])
    span = profile["column_span_m"]
    slope = profile["slope_span_m"]
    down = slope * math.cos(tilt)
    drop = slope * math.sin(tilt)
    for name, value in (("down_slope_m", down), ("drop_m", drop)):
        if not math.isfinite(value) or value < 0 or (name == "down_slope_m" and value == 0):
            raise ValueError(f"Derived {name} is outside the fixed-table geometry range")
    points = [(0.0, 0.0, 0.0), (span, 0.0, 0.0), (span, down, drop), (0.0, down, drop)]
    return points, [(0, 1, 2), (0, 2, 3)]


def _append_mesh(parent, geometry_id, points, faces):
    geometry = ET.SubElement(parent, _tag(NS, "geometry"), {"id": geometry_id})
    mesh = ET.SubElement(geometry, _tag(NS, "mesh"))
    array = ET.SubElement(mesh, _tag(NS, "source"), {"id": geometry_id + "-positions"})
    floats = ET.SubElement(array, _tag(NS, "float_array"),
                           {"id": geometry_id + "-positions-array", "count": str(len(points) * 3)})
    floats.text = " ".join(_num(axis) for point in points for axis in point)
    accessor = ET.SubElement(ET.SubElement(array, _tag(NS, "technique_common")),
                             _tag(NS, "accessor"),
                             {"source": "#" + geometry_id + "-positions-array",
                              "count": str(len(points)), "stride": "3"})
    for axis in ("X", "Y", "Z"):
        ET.SubElement(accessor, _tag(NS, "param"), {"name": axis, "type": "float"})
    vertices = ET.SubElement(mesh, _tag(NS, "vertices"), {"id": geometry_id + "-vertices"})
    ET.SubElement(vertices, _tag(NS, "input"),
                  {"semantic": "POSITION", "source": "#" + geometry_id + "-positions"})
    triangles = ET.SubElement(mesh, _tag(NS, "triangles"), {"count": str(len(faces))})
    ET.SubElement(triangles, _tag(NS, "input"),
                  {"semantic": "VERTEX", "source": "#" + geometry_id + "-vertices", "offset": "0"})
    ET.SubElement(triangles, _tag(NS, "p")).text = " ".join(
        str(vertex) for face in faces for vertex in face)
    return geometry


def _pv_extra(parent):
    return ET.SubElement(ET.SubElement(parent, _tag(NS, "extra")), _tag(NS, "technique"),
                         {"profile": "PVCollada-2.0"})


def _rack_origin(house, layout):
    """Low south-west corner of the table: margin from the eave, then the declared clearance."""
    if house["kind"] == "mesh":
        return layout["origin_m"]
    margin = layout["edge_margin_m"]
    if house["kind"] == "flat":
        return [margin, margin, house["eave_height_m"] + layout["clearance_m"]]
    pitch = math.radians(house["pitch_deg"])
    half = house["width_m"] / 2.0
    along = margin * math.cos(pitch)
    if along + layout["slope_span_m"] * math.cos(math.radians(layout["tilt_deg"])) > half:
        raise ValueError("edge_margin_m leaves no room for the table before the ridge")
    return [margin, along, house["eave_height_m"] + margin * math.sin(pitch) + layout["clearance_m"]]


def check_mesh_fit(house, layout, corners, points):
    roof = next((part for part in house["meshes"] if part["name"] == layout["roof_mesh"]), None)
    if roof is None:
        raise ValueError("roof_mesh must reference an existing house mesh name")
    vertices, faces = roof["vertices"], roof["triangles"]
    a, b, c = (vertices[index] for index in faces[0])
    normal = _normal(a, b, c)
    if normal[2] <= TOL:
        raise ValueError("The supporting roof mesh must face upward")
    pv_normal = _normal(*points[:3])
    if _dot(normal, pv_normal) < 1.0 - TOL:
        raise ValueError("Supporting roof normal must match the source fixed PV plane")
    for vertex in vertices:
        if abs(_dot(normal, _sub(vertex, a))) > TOL:
            raise ValueError("The supporting roof mesh must be planar")
    for face in faces:
        if _dot(normal, _normal(*(vertices[index] for index in face))) < 1.0 - TOL:
            raise ValueError("Supporting roof triangles must face consistently upward")
    for x, y, z in corners:
        roof_z = a[2] - (normal[0] * (x - a[0]) + normal[1] * (y - a[1])) / normal[2]
        if z < roof_z - TOL:
            raise ValueError("PV corner sinks below the selected roof plane")
        point = (x, y, roof_z)
        if not any(_inside_triangle(point, *(vertices[index] for index in face)) for face in faces):
            raise ValueError("PV corner projects outside the supporting roof mesh")


def check_fit(house, layout, origin, points):
    """Check four projected corners, not complete solid/array intersections."""
    corners = [(origin[0] + point[0], origin[1] + point[1], origin[2] + point[2]) for point in points]
    for corner in corners:
        for value in corner:
            if not math.isfinite(value):
                raise ValueError("Table corner is not finite")
    if house["kind"] == "mesh":
        check_mesh_fit(house, layout, corners, points)
        return
    length, width = house["length_m"], house["width_m"]
    if max(corner[0] for corner in corners) > length + TOL:
        raise ValueError("Table projects past the house end")
    if house["kind"] == "flat":
        if max(corner[1] for corner in corners) > width + TOL:
            raise ValueError("Table projects past the house side")
        if min(corner[2] for corner in corners) < house["eave_height_m"] - TOL:
            raise ValueError("Table sinks below the flat roof surface")
        return
    if abs(layout["tilt_deg"] - house["pitch_deg"]) > TOL:
        raise ValueError("Gable roof requires the PV tilt to equal the roof pitch")
    half = width / 2.0
    slope = math.tan(math.radians(house["pitch_deg"]))
    for x, y, z in corners:
        if not 0.0 - TOL <= y <= half + TOL:
            raise ValueError("Table corner is not on the front roof face")
        if z < house["eave_height_m"] + y * slope - TOL:
            raise ValueError("Table corner sinks below the front roof plane")


def build_scene(house, tables, modules, source, created, recipe=None):
    """Minimal COLLADA document: shared rotation node, house child, PV child with translation."""
    ET.register_namespace("", NS)
    ET.register_namespace("pv", PV)
    root = ET.Element(_tag(NS, "COLLADA"), {"version": "1.5.0"})
    asset = ET.SubElement(root, _tag(NS, "asset"))
    contributor = ET.SubElement(asset, _tag(NS, "contributor"))
    ET.SubElement(contributor, _tag(NS, "authoring_tool")).text = AUTHORING_TOOL
    coverage = ET.SubElement(ET.SubElement(asset, _tag(NS, "coverage")),
                             _tag(NS, "geographic_location"))
    ET.SubElement(coverage, _tag(NS, "longitude")).text = _num(source["site"]["longitude"])
    ET.SubElement(coverage, _tag(NS, "latitude")).text = _num(source["site"]["latitude"])
    ET.SubElement(coverage, _tag(NS, "altitude"), {"mode": "absolute"}).text = _num(source["site"]["altitude"])
    ET.SubElement(asset, _tag(NS, "created")).text = created
    ET.SubElement(asset, _tag(NS, "modified")).text = created
    if recipe is not None:
        ET.SubElement(asset, _tag(NS, "subject")).text = json.dumps(recipe, ensure_ascii=True,
                                                                   separators=(",", ":"), allow_nan=False)
    ET.SubElement(asset, _tag(NS, "unit"), {"meter": "1", "name": "m"})
    ET.SubElement(asset, _tag(NS, "up_axis")).text = "Z_UP"
    components = ET.SubElement(_pv_extra(asset), _tag(PV, "components"))
    module = ET.SubElement(ET.SubElement(components, _tag(PV, "modules")), _tag(PV, "module"),
                           {"id": "ModuleModel1"})
    for name, text in (("name", modules["name"]), ("nom_power", _num(modules["nom_power"])),
                       ("length", _num(modules["length_m"] * 1000.0)),
                       ("width", _num(modules["width_m"] * 1000.0)),
                       ("depth", _num(modules["depth_m"] * 1000.0))):
        ET.SubElement(module, _tag(PV, name)).text = text
    geometries = ET.SubElement(root, _tag(NS, "library_geometries"))
    if house["kind"] == "mesh":
        parts = house["meshes"]
    else:
        house_points, house_faces = house_mesh(house)
        parts = [{"name": "House", "vertices": house_points, "triangles": house_faces}]
    for index, part in enumerate(parts, 1):
        geometry = _append_mesh(geometries, f"HousePart{index}", part["vertices"], part["triangles"])
        geometry.set("name", part["name"])
    nodes = ET.SubElement(root, _tag(NS, "library_nodes"))
    for index, layout in enumerate(tables, 1):
        rack_points, rack_faces = rack_mesh(layout)
        rack = _append_mesh(geometries, f"Rack{index}", rack_points, rack_faces)
        rack_tag = ET.SubElement(_pv_extra(rack), _tag(PV, "rack"))
        for name, text in (("rack_type", "fixed_tilt"), ("row_spacing", _num(layout["row_spacing_m"])),
                           ("column_spacing", _num(layout["column_spacing_m"])),
                           ("module_rows", str(layout["module_rows"])),
                           ("module_columns", str(layout["module_columns"])),
                           ("module_orientation", layout["orientation"]),
                           ("tilt", _num(layout["tilt_deg"])),
                           ("azimuth", _num(layout.get("azimuth_deg", source["azimuth_deg"]))), ("module_id", "ModuleModel1")):
            ET.SubElement(rack_tag, _tag(PV, name)).text = text
        table_node = ET.SubElement(nodes, _tag(NS, "node"), {"id": f"TableModel{index}"})
        instance = ET.SubElement(table_node, _tag(NS, "instance_geometry"), {"url": f"#Rack{index}"})
        ET.SubElement(_pv_extra(instance), _tag(PV, "instance_rack"), {"id": f"rackInst{index}"})
        table_tag = ET.SubElement(_pv_extra(table_node), _tag(PV, "table"))
        ET.SubElement(table_tag, _tag(PV, "type")).text = "fixed"
    scenes = ET.SubElement(root, _tag(NS, "library_visual_scenes"))
    scene = ET.SubElement(scenes, _tag(NS, "visual_scene"), {"id": "VisualSceneNode"})
    turn = 180.0 - source["azimuth_deg"]
    shared = ET.SubElement(scene, _tag(NS, "node"), {"id": "SceneRoot"})
    ET.SubElement(shared, _tag(NS, "rotate")).text = "0 0 1 " + _num(turn)
    for index, part in enumerate(parts, 1):
        node = ET.SubElement(shared, _tag(NS, "node"), {"id": f"HouseNode{index}", "name": part["name"]})
        ET.SubElement(node, _tag(NS, "instance_geometry"), {"url": f"#HousePart{index}"})
    summaries = []
    for index, layout in enumerate(tables, 1):
        origin = _rack_origin(house, layout)
        pv_node = ET.SubElement(shared, _tag(NS, "node"), {"id": f"PVTable{index}",
                                "name": layout.get("name", "PV table")})
        ET.SubElement(pv_node, _tag(NS, "translate")).text = " ".join(_num(value) for value in origin)
        if "rotation_deg" in layout:
            ET.SubElement(pv_node, _tag(NS, "rotate")).text = "0 0 1 " + _num(layout["rotation_deg"])
        pv_instance = ET.SubElement(pv_node, _tag(NS, "instance_node"), {"url": f"#TableModel{index}"})
        table_instance = ET.SubElement(_pv_extra(pv_instance), _tag(PV, "instance_table"),
                                      {"id": f"Table{index}"})
        array = ET.SubElement(table_instance, _tag(PV, "instance_racks_array"))
        ET.SubElement(array, _tag(PV, "instance_rack_ref"),
                      {"id": f"rackRef{index}", "url": f"#rackInst{index}"})
        points = _placed_points(layout)
        summaries.append({"name": layout.get("name", "PV table"), "origin_m": origin,
                          **({"orientation_id": layout["orientation_id"]} if "orientation_id" in layout else {}),
                          "module_total": layout["module_rows"] * layout["module_columns"],
                          "corners_m": [[origin[axis] + point[axis] for axis in range(3)]
                                        for point in points]})
    ET.SubElement(ET.SubElement(root, _tag(NS, "scene")), _tag(NS, "instance_visual_scene"),
                  {"url": "#VisualSceneNode"})
    return root, {"house_vertices": sum(len(part["vertices"]) for part in parts),
                  "house_faces": sum(len(part["triangles"]) for part in parts),
                  "house_parts": len(parts), "part_names": [part["name"] for part in parts],
                  "rack_vertices": 4 * len(tables), "rack_faces": 2 * len(tables),
                  "pv_table_count": len(tables), "tables": summaries,
                  "rotate_deg": turn,
                  **({"origin_m": summaries[0]["origin_m"], "corners_m": summaries[0]["corners_m"]}
                     if len(tables) == 1 else {})}


def create_house_pv_scene(store, components, *, project, variant, output_name, house, layout,
                          dry_run=True, module_geometry=None):
    """Preview or write one new .pvc2; the project, variant and PAN are never written."""
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run, "issues": [],
              "diagnostics": [], "warnings": [], "artifacts": {},
              "native_consumption_verified": False, "imported": False}
    with store.lock:
        try:
            if type(dry_run) is not bool:
                raise ValueError("Use a strict boolean")
            name = safe_filename(output_name, ".pvc2")
            store._check_project(project)
            source_path = store._path(project, variant)
            original = store._read(source_path)
            text, lines = store._text(original)
            arrays, system = store._sections(lines)
            source = source_profile(store, lines, arrays, system, allow_groups=True)
            modules, pan_snapshot = resolve_modules(components, source["pan_name"], module_geometry)
            pocket = house_profile(house)
            tables = table_profiles(layout, pocket, modules, source)
            check_tables(pocket, tables)
            declared = {"project": project, "variant": variant, "pan": source["pan_name"],
                        "module_total": source["module_total"]}
            if "orientations" in source:
                declared["orientations"] = [{"id": item["id"], "module_total": item["module_total"]}
                                             for item in source["orientations"]]
            recipe = {"version": 2 if "orientations" in source else 1, "source": declared,
                      "house": {"kind": "mesh"} if pocket["kind"] == "mesh" else house,
                      "layout": layout}
            created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            root, counts = build_scene(pocket, tables, modules, source, created, recipe)
            payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            try:
                ET.fromstring(payload)
            except ET.ParseError as exc:
                raise ValueError("Module metadata must be valid XML text") from exc
            target = workspace_file(store.workspace, "UserData", name, ".pvc2")
            if target.parent.is_dir() and any(
                    path.name.casefold() == target.name.casefold() for path in target.parent.iterdir()):
                raise FileExistsError("Choose a new scene file: " + name)
            result.update(status="ready", project=project, variant=variant, output=name,
                          modeling_mode="ai-meshes" if pocket["kind"] == "mesh" else "legacy-parametric",
                          artifacts={"scene_file": str(target)},
                          source={"project": project, "variant": variant, "pan": source["pan_name"],
                                  "module_total": source["module_total"],
                                  "field_tilt_deg": source["tilt_deg"],
                                  "field_azimuth_deg": source["field_azimuth_deg"],
                                  "collada_azimuth_deg": source["azimuth_deg"],
                                  "latitude": source["site"]["latitude"],
                                   **({"orientations": source["orientations"]} if "orientations" in source else {})},
                          modules=modules,
                          house=({"kind": "mesh", "part_count": len(pocket["meshes"])}
                                 if pocket["kind"] == "mesh" else pocket),
                          layout={"tables": tables} if "tables" in layout else tables[0], geometry=counts,
                          bytes=len(payload))
            result["warnings"].append(
                "Module metadata provenance: " + modules["metadata_source"] + ". Geometry uses metres; "
                "only pv:module dimensions use millimetres. Depth is caller-provided geometry. "
                "Explicit metadata for a native database reference requires PVsyst's own import consistency "
                "check; writing this file does not verify a manufacturer model.")
            result["warnings"].append(
                "Azimuth uses the COLLADA convention (north = 0, clockwise) and is only verified for a "
                "northern-hemisphere site. House and table origins use the orientation1 anchor frame; "
                "grouped PV nodes additionally inherit their own source-relative Z rotation."
                if "orientations" in source else
                "Azimuth uses the COLLADA convention (north = 0, clockwise) and is only verified for a "
                "northern-hemisphere site. House and table share one parent rotation, so the table translation "
                "is expressed in the house local frame.")
            if "orientations" in source:
                result["warnings"].append("Two-orientation authoring does not certify native geometry: the bounded native path separately assigns, places, saves and reloads both regions before simulation.")
            if pocket["kind"] == "mesh":
                result["warnings"].append(
                    "House geometry is AI/caller-authored, not reconstructed from a fixed roof template. "
                    "Checks cover finite non-degenerate triangles and PV corner projections onto the named "
                    "planar roof, not solid topology, holes, full panel intersections or structural design.")
            result["warnings"].append(
                "This is an external PVCollada 2.0 file and the tested version still labels PVC2 support as beta: "
                "writing it does not prove that PVsyst imports it as a shading scene or consumes it.")
            if store._read(source_path) != original:
                raise ValueError("Source variant changed while preparing the scene file")
            if pan_snapshot is not None and components._read(pan_snapshot[0]) != pan_snapshot[1]:
                raise ValueError("Source PAN changed while preparing the scene file")
            if dry_run:
                return result
            result["stage"] = "write"
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(payload)
            result.update(status="created", stage="created")
        except (ValueError, OSError, UnicodeError, KeyError, TypeError) as exc:
            result["issues"].append(str(exc))
            if isinstance(exc, SceneValidationError):
                result["diagnostics"] = exc.diagnostics
                result["diagnostics_truncated"] = exc.truncated
            else:
                result["diagnostics"] = [_diagnostic("scene_input", result["stage"], str(exc))]
            result["status"] = "failed" if result["stage"] == "write" else "blocked"
            if result["status"] == "failed":
                result["warnings"].append("A partial scene file may exist at the disclosed path; the source "
                                          "project, variant and PAN are unchanged.")
    return result

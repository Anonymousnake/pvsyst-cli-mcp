"""Scene readback (.pvc2), native shading/layout state inspection, and 3D ground polygon builders."""
from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from typing import Any

from pvsyst_auto.components.editor import ASSIGNMENT, objects
from pvsyst_auto.project.paths import project_name, safe_filename, variant_id, workspace_file
from pvsyst_auto.scene.author import (AUTHORING_TOOL, MAX_OBSTACLES, MAX_PARTS, MAX_TABLES, MAX_TRIANGLES,
                                      MAX_VERTICES, NS, PV, SceneValidationError, _diagnostic, _finite,
                                      _string_layout_profile, build_scene, caller_geometry, check_tables,
                                      house_profile, layout_profile, obstacle_profiles, table_profiles)
from pvsyst_auto.scene.exclusions import (
    point_in_polygon as _point_in_polygon,
    point_segment_distance as _point_segment_distance,
    polygon_area,
    rect_overlaps_any_polygon,
    subtract_exclusions_from_intervals,
    validate_exclusion_zones,
)
from pvsyst_auto.scene.mapping import PV_TABLE_SOURCES, assess_module_layout
from pvsyst_auto.scene.partition import (
    SHADING_FLAGS_LINEAR,
    compute_table_partition_dims,
    normalize_electrical_shading,
    shading_header_kwargs,
)
from pvsyst_auto.validation import SEED_RECORD_VERSION, SYNTH_VERSION

XML_NS = {"n": NS, "pv": PV}
MAX_BYTES = 64 * 1024 * 1024
ACTIVE_BIT = 0x00000100
SCENE_KEYS = ("Comment", "Flags", "Precision", "FracOmbreMod", "FracOmbreThin", "DisplayedRef")
OBJECT_COUNT = re.compile(r"^ListeObjets, list of=(\d+) TShdObject$")
_EPS = 1e-9

# --------------------------------------------------------------------- .pvc2 readback
def _fail(path, message, code="unsupported_scene"):
    raise SceneValidationError([_diagnostic(
        code, path, message, repair_hint="Keep the original file; use the supported scene input or supply the missing context.")])

def _one(parent, path):
    found = parent.findall(path, XML_NS)
    if len(found) != 1:
        _fail(path, "Expected exactly one element at this path")
    return found[0]

def _text(parent, path):
    return _one(parent, path).text or ""

def _number(parent, path, **bounds):
    return _finite(float(_text(parent, path)), path, **bounds)

def _parts(root):
    geometries = list(_one(root, "n:library_geometries"))
    if len(geometries) > MAX_PARTS + MAX_TABLES + MAX_OBSTACLES:
        _fail("library_geometries", "Scene exceeds the adapter geometry ceiling")
    parts, obs_parts, racks, vertex_count, triangle_count = [], [], [], 0, 0
    for geometry in geometries:
        identifier = geometry.get("id", "")
        if re.fullmatch(r"Rack(?:[1-9][0-9]*)?", identifier):
            racks.append(geometry)
            continue
        is_house = bool(re.fullmatch(r"HousePart[1-9][0-9]*", identifier))
        is_obs = bool(re.fullmatch(r"ObstaclePart[1-9][0-9]*", identifier))
        if not (is_house or is_obs):
            _fail("library_geometries/" + identifier, "Unrecognized geometry identifier")
        positions = _text(geometry, "n:mesh/n:source/n:float_array").split()
        indices = _text(geometry, "n:mesh/n:triangles/n:p").split()
        if len(positions) % 3 or len(indices) % 3:
            _fail(identifier, "Positions and triangle indices must be complete triples")
        vertex_count += len(positions) // 3
        triangle_count += len(indices) // 3
        if vertex_count > MAX_VERTICES or triangle_count > MAX_TRIANGLES:
            _fail(identifier, "Scene exceeds the adapter mesh ceiling")
        vertices, triangles = [float(v) for v in positions], [int(v) for v in indices]
        item = {"name": geometry.get("name", identifier),
                "vertices": [vertices[i:i + 3] for i in range(0, len(vertices), 3)],
                "triangles": [triangles[i:i + 3] for i in range(0, len(triangles), 3)]}
        (parts if is_house else obs_parts).append(item)
    if not parts or not 1 <= len(racks) <= MAX_TABLES:
        _fail("library_geometries", "House meshes and bounded PV rack geometry are required")
    return parts, obs_parts, racks

def _module(asset):
    m = _one(asset, "n:extra/n:technique/pv:components/pv:modules/pv:module")
    return {"model_name": _text(m, "pv:name"), "nom_power_wp": _number(m, "pv:nom_power", positive=True),
            "width_m": _number(m, "pv:width", positive=True) / 1000,
            "length_m": _number(m, "pv:length", positive=True) / 1000,
            "geometry_depth_m": _number(m, "pv:depth", positive=True) / 1000}

def _source(asset, rack):
    coverage = _one(asset, "n:coverage/n:geographic_location")
    tilt, azimuth = _number(rack, "pv:tilt", minimum=0, maximum=90), _number(rack, "pv:azimuth", minimum=0, maximum=360)
    if tilt >= 90 or azimuth >= 360:
        _fail("pv:rack", "Only the emitted ordinary fixed-plane range is supported")
    return {"tilt_deg": tilt, "azimuth_deg": azimuth,
            "site": {"latitude": _number(coverage, "n:latitude", positive=True, maximum=90),
                     "longitude": _number(coverage, "n:longitude", minimum=-180, maximum=180),
                     "altitude": _number(coverage, "n:altitude")}}

def _legacy_layout(root, rack):
    translate = _text(root, "n:library_visual_scenes/n:visual_scene/n:node[@id='SceneRoot']/n:node[@id='PVTable1']/n:translate")
    return {"module_rows": int(_text(rack, "pv:module_rows")), "module_columns": int(_text(rack, "pv:module_columns")),
            "orientation": _text(rack, "pv:module_orientation"),
            "row_spacing_m": _number(rack, "pv:row_spacing", minimum=0),
            "column_spacing_m": _number(rack, "pv:column_spacing", minimum=0),
            "origin_m": [float(v) for v in translate.split()], "roof_mesh": "__missing__"}

def _legacy_tree(expected):
    asset = _one(expected, "n:asset")
    asset.remove(_one(asset, "n:contributor"))
    for el in expected.iter():
        for key in ("id", "source", "url"):
            if key in el.attrib:
                el.set(key, el.get(key).replace("Rack1", "Rack").replace("TableModel1", "TableModel"))
    _one(expected, "n:library_visual_scenes/n:visual_scene/n:node[@id='SceneRoot']/n:node[@id='PVTable1']").attrib.pop("name", None)
    return expected

def read_house_pv_scene(components, scene_name):
    """Read authored geometry (.pvc2) or external CAD (.pvc 1.4.1 / ASCII .dxf) preview; never writes."""
    if isinstance(scene_name, str) and scene_name.lower().endswith((".pvc", ".dxf")):
        from pvsyst_auto.scene.cad_import import read_cad_scene_preview
        with components.lock:
            return read_cad_scene_preview(components, scene_name)
    result = {"status": "unsupported", "issues": [], "diagnostics": [], "required_inputs": [],
              "source_verified": False, "imported": False, "native_consumption_verified": False}
    with components.lock:
        try:
            path = workspace_file(components.workspace, "UserData", scene_name, ".pvc2")
            if path.stat().st_size > MAX_BYTES:
                _fail("scene_name", "Scene exceeds the 64 MiB readback ceiling")
            with path.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                _fail("scene_name", "Scene exceeds the 64 MiB readback ceiling")
            text = raw.decode("utf-8-sig")
            if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
                _fail("scene_name", "DOCTYPE and ENTITY declarations are not part of this dialect")
            root = ET.fromstring(text)
            if root.tag != "{" + NS + "}COLLADA" or root.get("version") != "1.5.0":
                _fail("COLLADA", "Expected COLLADA 1.5.0")
            asset = _one(root, "n:asset")
            created = _text(asset, "n:created")
            if _text(asset, "n:modified") != created:
                _fail("asset/modified", "This dialect emits matching creation and modification timestamps")
            parts, obs_parts, racks = _parts(root)
            geometry = _module(asset)
            modules = caller_geometry(geometry)
            source = _source(asset, _one(racks[0], "n:extra/n:technique/pv:rack"))
            subject = asset.findall("n:subject", XML_NS)
            legacy = not subject
            binding = binding_state = obstacle_records = None
            if legacy:
                if obs_parts:
                    _fail("library_geometries", "Legacy scene cannot declare obstacle parts without recipe")
                if len(racks) != 1 or racks[0].get("id") != "Rack":
                    _fail("asset/subject", "Missing readback recipe for this scene")
                house = {"kind": "mesh", "meshes": parts}
                layout = _legacy_layout(root, _one(racks[0], "n:extra/n:technique/pv:rack"))
                pocket = house_profile(house)
                source["module_total"] = layout["module_rows"] * layout["module_columns"]
                tables = [layout_profile(layout, modules, source["module_total"], source["tilt_deg"], placed=True)]
                recipe, declared = None, {"provenance": "not-recorded", "project": None, "variant": None}
            else:
                if len(subject) != 1 or _text(asset, "n:contributor/n:authoring_tool") != AUTHORING_TOOL:
                    _fail("asset/subject", "Unrecognized scene authoring metadata")
                recipe = json.loads(subject[0].text or "")
                recipe_keys = {"version", "source", "house", "layout"}
                if (not isinstance(recipe, dict) or not recipe_keys <= set(recipe) <= recipe_keys | {"string_layout", "obstacles"}
                        or type(recipe["version"]) is not int or recipe["version"] not in (1, 2, 3)):
                    _fail("asset/subject", "Only complete version 1, 2 or 3 recipes are supported")
                version = recipe["version"]
                grouped, declared = version in (2, 3), recipe["source"]
                grouped_key = "orientations" if version == 2 else "owners"
                keys = {"project", "variant", "pan", "module_total"} | ({grouped_key} if grouped else set())
                if (not isinstance(declared, dict) or set(declared) != keys
                        or type(declared["module_total"]) is not int or declared["module_total"] < 1):
                    _fail("asset/subject#source", "Source declarations require a strict positive module total")
                project_name(declared["project"])
                variant_id(declared["variant"])
                safe_filename(declared["pan"], ".PAN")
                house = {"kind": "mesh", "meshes": parts} if recipe["house"] == {"kind": "mesh"} else recipe["house"]
                pocket, layout = house_profile(house), recipe["layout"]
                if grouped:
                    entries = layout["tables"] if isinstance(layout, dict) and set(layout) == {"tables"} else None
                    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_TABLES or len(racks) != len(entries):
                        _fail("asset/subject#layout", "Version %d requires one rack per named source-oriented table" % version)
                    profiles = []
                    if version == 2:
                        groups = declared["orientations"]
                        group_ids = ([i.get("id") for i in groups]
                                     if isinstance(groups, list) and all(isinstance(i, dict) for i in groups) else [])
                        if (not isinstance(groups, list) or len(groups) < 2
                                or any(not isinstance(i, dict) or set(i) != {"id", "module_total"}
                                       or type(i["id"]) is not int or i["id"] < 1
                                       or type(i["module_total"]) is not int or i["module_total"] < 1 for i in groups)
                                or len(set(group_ids)) != len(group_ids)
                                or sum(i["module_total"] for i in groups) != declared["module_total"]):
                            _fail("asset/subject#source", "Version 2 requires distinct positive source groups and their exact total")
                        first_rack = {}
                        for index, entry in enumerate(entries):
                            ident = entry.get("orientation_id") if isinstance(entry, dict) else None
                            if type(ident) is not int or ident not in set(group_ids):
                                _fail("asset/subject#layout", "Table orientation identity must be a declared strict source ID")
                            first_rack.setdefault(ident, index)
                        if set(first_rack) != set(group_ids):
                            _fail("asset/subject#layout", "Each source orientation must own at least one table")
                        for item in groups:
                            plane = _source(asset, _one(racks[first_rack[item["id"]]], "n:extra/n:technique/pv:rack"))
                            profiles.append(dict(plane, id=item["id"], module_total=item["module_total"]))
                    else:
                        owners = declared["owners"]
                        if (not isinstance(owners, list) or len(owners) < 2
                                or any(not isinstance(i, dict) or set(i) != {"subarray_id", "orientation_id", "module_total"}
                                       or any(type(i[k]) is not int or i[k] < 1 for k in ("subarray_id", "orientation_id", "module_total"))
                                       for i in owners)
                                or len({i["subarray_id"] for i in owners}) != len(owners)
                                or sum(i["module_total"] for i in owners) != declared["module_total"]):
                            _fail("asset/subject#source", "Version 3 requires distinct positive source owners and their exact total")
                        owner_by_id, owner_rack = {i["subarray_id"]: i for i in owners}, {}
                        for index, entry in enumerate(entries):
                            sid = entry.get("source_subarray_id") if isinstance(entry, dict) else None
                            oid = entry.get("orientation_id") if isinstance(entry, dict) else None
                            if type(sid) is not int or sid not in owner_by_id:
                                _fail("asset/subject#layout", "Version 3 requires each table's source_subarray_id to be a declared source owner")
                            if oid != owner_by_id[sid]["orientation_id"]:
                                _fail("asset/subject#layout", "Table orientation identity differs from its source owner orientation")
                            owner_rack.setdefault(sid, index)
                        if set(owner_rack) != set(owner_by_id):
                            _fail("asset/subject#layout", "Each source owner must own at least one table")
                        for item in owners:
                            plane = _source(asset, _one(racks[owner_rack[item["subarray_id"]]], "n:extra/n:technique/pv:rack"))
                            profiles.append(dict(plane, id=item["orientation_id"], subarray_id=item["subarray_id"], module_total=item["module_total"]))
                    source = dict(profiles[0], orientations=profiles)
                source["module_total"] = declared["module_total"]
                tables = table_profiles(layout, pocket, modules, source)
                check_tables(pocket, tables)
                if (binding := recipe.get("string_layout")) is not None:
                    _, binding_state = _string_layout_profile(binding, tables)
                if (raw_obstacles := recipe.get("obstacles")) is not None:
                    existing = [m["name"] for m in pocket["meshes"]] if pocket["kind"] == "mesh" else ["House"]
                    obstacle_records = obstacle_profiles(raw_obstacles, existing_names=existing)
                    if len(obs_parts) != len(obstacle_records) or any(
                            p["name"] != r["name"] or p["vertices"] != r["vertices"] or p["triangles"] != r["triangles"]
                            for p, r in zip(obs_parts, obstacle_records)):
                        _fail("library_geometries", "Obstacle mesh geometry differs from scene recipe")
                elif obs_parts:
                    _fail("library_geometries", "Scene contains undeclared obstacle geometry")
            expected, counts = build_scene(pocket, tables, modules, source, created, recipe, obstacles=obstacle_records)
            if legacy:
                expected = _legacy_tree(expected)
            rebuilt = ET.tostring(expected, encoding="unicode")
            if (ET.canonicalize(xml_data=text, strip_text=True, rewrite_prefixes=True)
                    != ET.canonicalize(xml_data=rebuilt, strip_text=True, rewrite_prefixes=True)):
                _fail("scene_name", "Unconsumed or modified XML is outside the lossless dialect", "scene_roundtrip_mismatch")
            required = ["output_name"]
            arguments = {"house": house, "layout": layout, "module_geometry": geometry, "dry_run": True}
            if legacy:
                layout = dict(layout)
                layout.pop("roof_mesh")
                arguments["layout"] = layout
                required += ["project", "variant", "layout.roof_mesh"]
            else:
                arguments.update(project=declared["project"], variant=declared["variant"])
                if binding is not None:
                    arguments["string_layout"] = binding
                if obstacle_records is not None:
                    arguments["obstacles"] = [rec["spec"] for rec in obstacle_records]
            recorded_source = dict(declared, provenance="not-recorded" if legacy else "file-declared",
                                   site=source["site"], tilt_deg=source["tilt_deg"], collada_azimuth_deg=source["azimuth_deg"])
            if source.get("orientations"):
                recorded_source["orientations"] = [{"id": p["id"], "module_total": p["module_total"]} for p in source["orientations"]]
                recorded_source["orientation_geometry"] = [
                    {"id": p["id"], "module_total": p["module_total"], "tilt_deg": p["tilt_deg"], "collada_azimuth_deg": p["azimuth_deg"]}
                    for p in source["orientations"]]
            result.update(status="needs_context" if legacy else "ready", required_inputs=required,
                          source=recorded_source, geometry=counts, create_arguments=arguments)
            if binding_state is not None:
                result["string_layout_state"] = binding_state
        except (ValueError, OSError, UnicodeError, ET.ParseError, TypeError, KeyError, OverflowError) as exc:
            result["issues"].append(str(exc))
            result["diagnostics"] = (exc.diagnostics if isinstance(exc, SceneValidationError)
                                     else [_diagnostic("scene_read", "scene_name", str(exc))])
    return result

# --------------------------------------------------------------------- native scene & layout state
def shading_state(lines):
    starts = [i for i, ln in enumerate(lines) if ln.strip() == "PVObject_Ombrage=pvShading"]
    if not starts:
        return {"mode": None, "reason": "no pvShading section"}
    if len(starts) != 1:
        return {"mode": None, "issue": "Requires exactly one pvShading owner"}
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == "End of PVObject pvShading"), None)
    if end is None:
        return {"mode": None, "issue": "pvShading owner is not closed"}
    body, values = [ln.rstrip("\r\n") for ln in lines[start + 1:end]], {}
    for ln in body:
        if ln.strip().startswith(("PVObject_", "ListeObjets")):
            break
        match = ASSIGNMENT.fullmatch(ln)
        if match is None:
            continue
        if match[2] in values:
            return {"mode": None, "issue": f"Duplicate {match[2]} in pvShading"}
        values[match[2]] = match[4]
    raw = values.get("Flags", "")
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", raw):
        return {"mode": None, "issue": "pvShading Flags is missing or not a hexadecimal token"}
    lists = [(idx, int(m[1])) for idx, ln in enumerate(body) if (m := OBJECT_COUNT.fullmatch(ln.strip()))]
    if len(lists) > 1:
        return {"mode": None, "issue": "Repeated native scene object list"}
    active, declared, types = bool(int(raw[1:], 16) & ACTIVE_BIT), 0, []
    if lists:
        begin, declared = lists[0]
        finish = next((i for i in range(begin + 1, len(body)) if body[i].strip() == "End of ListeObjets"), None)
        if finish is None:
            return {"mode": None, "issue": "Native scene object list is not closed"}
        types = [ln.strip().partition("=")[2] for ln in body[begin + 1:finish] if ln.strip().startswith("PVObject_")]
    elif active:
        return {"mode": None, "issue": "Active shading owner carries no object list"}
    if declared != len(types) or (not active and declared):
        return {"mode": None, "issue": "The owner flags, declared object count and stored objects disagree"}
    scene = {k: values[k] for k in SCENE_KEYS if k in values}
    if not active:
        return {"mode": "empty", "flags": raw, "values": scene}
    return {"mode": "active", "flags": raw, "object_count": declared, "object_types": types, "values": scene,
            "pv_bindings": _pv_bindings(lines, types), "module_layout": module_layout_state(lines)}

def module_layout_state(lines):
    """Classify a stored pvModuleLayout record without claiming native simulation consumption."""
    if not any(ln.strip().endswith("=pvModuleLayout") for ln in lines):
        return {"configured": False, "status": "absent", "native_consumption_verified": False}
    try:
        normalized = [ln.rstrip("\r\n") + "\n" for ln in lines]
        return assess_module_layout(objects("".join(normalized)), normalized)
    except (ValueError, KeyError, OverflowError) as exc:
        return {"configured": True, "status": "needs_review", "issues": [str(exc)], "native_consumption_verified": False}

def _pv_bindings(lines, types):
    """Cross-check stored scene table bindings against orientations and subarrays."""
    result = {"status": "unsupported", "tables": [], "groups": [], "issues": [],
              "coordinate_frame": "native DOrigine in metres; not house/PVCollada coordinates",
              "source_geometry_verified": False, "native_consumption_verified": False, "generic_geometry_decoded": False}

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
        return {k: v[0][1] if len(v) == 1 else [val for _, val in v] for k, v in block.fields.items()}

    try:
        if any(k not in ("pvShdArrayTable", "pvShdGenObject", "pvShdElemObject") for k in types):
            raise ValueError("Stored binding review supports PV tables, generic objects, and native elementary objects only")
        blocks = objects("\n".join(ln.rstrip("\r\n") for ln in lines))
        tables = [b for b in blocks if b.object_type == "pvShdArrayTable"]
        if len(tables) != types.count("pvShdArrayTable"):
            raise ValueError("PV table records must belong to the declared shading list")
        planes = [b for b in blocks if b.object_type == "pvOrient"]
        arrays = [b for b in blocks if b.object_type == "pvSubArray"]
        if len(planes) < 1 or len(arrays) < 1:
            raise ValueError("Stored binding review supports one or more fixed source planes and subarrays")
        grouped = {}
        for plane in planes:
            ident = integer(plane, "NoOrient")
            if ident in grouped:
                raise ValueError("Repeated stored orientation ID")
            if (plane.one("FieldType")[1] != "FixedPlane" or plane.one("Flags")[1] != "$00"
                    or any(k in plane.fields and plane.one(k)[1] != "False"
                           for k in ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver"))):
                raise ValueError("Stored binding review requires ordinary non-averaged fixed planes")
            number(plane.one("FieldTilt")[1])
            number(plane.one("FieldAzim")[1])
            grouped[ident] = {"orientation_id": ident, "orientation_values": raw_fields(plane),
                              "subarray_ids": [], "table_paths": [], "electrical_module_count": 0, "scene_module_count": 0}
        array_ids = set()
        for array in arrays:
            array_id = integer(array, "SubArrayId")
            if array_id in array_ids:
                raise ValueError("Repeated stored subarray ID")
            array_ids.add(array_id)
            ident = integer(array, "NoOrientation")
            if ident not in grouped:
                result["issues"].append(f"Subarray {array.path} references missing orientation {ident}")
                continue
            group = grouped[ident]
            group["subarray_ids"].append(array_id)
            group["electrical_module_count"] += integer(array, "NModSerie") * integer(array, "NStringCh")
        max_tables = sum(g["electrical_module_count"] for g in grouped.values())
        if not 1 <= types.count("pvShdArrayTable") <= max_tables:
            raise ValueError("Stored binding review requires 1..%d PV tables for %d source plane(s)" % (max_tables, len(planes)))
        for table in tables:
            row = {"object_path": table.path, "values": raw_fields(table)}
            result["tables"].append(row)
            if (table.one("Source")[1], table.one("Version")[1]) not in PV_TABLE_SOURCES:
                raise ValueError("PV table declarations are mapped only for verified osPVCollada 8.1.6/8.1.7 records")
            ident = integer(table, "NoOrient")
            mod_count = integer(table, "NModInHeight") * integer(table, "NModInWidth")
            origin = [float(number(v)) for v in table.one("DOrigine")[1].split(",")]
            if len(origin) != 3 or not all(math.isfinite(v) for v in origin):
                raise ValueError("Native DOrigine requires three finite JSON-number coordinates")
            row.update(orientation_id=ident, module_count=mod_count, origin_m=origin)
            tilt, azimuth, slope = number(table.one("DInclin")[1]), number(table.one("DAzimut")[1]), number(table.one("BaseSlope")[1])
            if ident not in grouped:
                result["issues"].append(f"PV table {table.path} references missing orientation {ident}")
                continue
            group = grouped[ident]
            group["table_paths"].append(table.path)
            group["scene_module_count"] += mod_count
            if (tilt != number(group["orientation_values"]["FieldTilt"])
                    or azimuth != number(group["orientation_values"]["FieldAzim"])
                    or slope != number(group["orientation_values"]["BaseSlope"])):
                result["issues"].append(f"PV table {table.path} angles differ from orientation {ident}")
        result["groups"] = list(grouped.values())
        for g in result["groups"]:
            if not g["subarray_ids"] or g["scene_module_count"] != g["electrical_module_count"]:
                result["issues"].append(f"Orientation {g['orientation_id']} scene/electrical module allocation differs")
        result["status"] = "needs_review" if result["issues"] else "stored_consistent"
    except (ValueError, InvalidOperation, KeyError, OverflowError) as exc:
        result["issues"].append(str(exc))
    return result

# --------------------------------------------------------------------- finite 3D ground layout & pvShading
def validate_site_polygon(vertices: Any) -> list[tuple[float, float]]:
    """Validate a 2D site polygon [[x0, y0], ...] with 3..64 vertices and positive area."""
    if not isinstance(vertices, list) or not 3 <= len(vertices) <= 64:
        raise ValueError("site_polygon must be a list of 3..64 [x, y] vertices")
    pts: list[tuple[float, float]] = []
    for idx, pt in enumerate(vertices):
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise ValueError(f"site_polygon[{idx}] must be a [x, y] pair")
        pts.append((_finite(pt[0], f"site_polygon[{idx}][0]"), _finite(pt[1], f"site_polygon[{idx}][1]")))
    if pts[0] == pts[-1] and len(pts) > 3:
        pts.pop()
    if polygon_area(pts) <= 1e-3:
        raise ValueError("site_polygon must enclose a positive non-degenerate area")
    return pts

def _min_edge_distance(x: float, y: float, poly: list[tuple[float, float]]) -> float:
    n = len(poly)
    return min(_point_segment_distance(x, y, poly[i][0], poly[i][1], poly[(i + 1) % n][0], poly[(i + 1) % n][1]) for i in range(n))

def _point_clear(x: float, y: float, poly: list[tuple[float, float]], setback: float) -> bool:
    return _point_in_polygon(x, y, poly) and not (setback > 0.0 and _min_edge_distance(x, y, poly) < setback - 1e-7)

def _rect_inside_polygon(u0: float, u1: float, v0: float, v1: float, poly: list[tuple[float, float]], setback: float) -> bool:
    """Check whether axis-aligned rectangle [u0,u1] x [v0,v1] fits inside poly with setback."""
    samples = ((u0, v0), (u1, v0), (u1, v1), (u0, v1), (0.5 * (u0 + u1), v0), (0.5 * (u0 + u1), v1),
               (u0, 0.5 * (v0 + v1)), (u1, 0.5 * (v0 + v1)), (0.5 * (u0 + u1), 0.5 * (v0 + v1)))
    if not all(_point_clear(u, v, poly, setback) for u, v in samples):
        return False
    for px, py in poly:
        if u0 - setback < px < u1 + setback and v0 - setback < py < v1 + setback:
            if u0 <= px <= u1 and v0 <= py <= v1:
                return False
            if math.hypot(px - min(max(px, u0), u1), py - min(max(py, v0), v1)) < setback - 1e-7:
                return False
    return True

def _rotate(x: float, y: float, angle_rad: float) -> tuple[float, float]:
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    return x * c - y * s, x * s + y * c

def _scanline_intervals(y: float, poly: list[tuple[float, float]]) -> list[tuple[float, float]]:
    xs, n = [], len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 <= y < y2) or (y2 <= y < y1):
            xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
    xs.sort()
    return [(xs[i], xs[i + 1]) for i in range(0, len(xs) - 1, 2)]

def plan_ground_polygon_layout(
    site_polygon: list[list[float]], *, tilt_deg: float, azimuth_deg: float = 0.0, pitch_m: float,
    module_width_m: float, module_length_m: float, module_rows: int = 2, module_orientation: str = "portrait",
    spacing_x_m: float = 0.02, spacing_y_m: float = 0.02, setback_m: float = 0.0,
    ground_clearance_m: float = 0.5, max_table_columns: int | None = None, base_slope_deg: float = 0.0,
    proj_depth_override_m: float | None = None,
    exclusion_zones: list[list[list[float]]] | None = None,
) -> dict[str, Any]:
    """Place maximal non-overlapping finite PV tables inside a 2D site polygon."""
    poly_world = validate_site_polygon(site_polygon)
    excl_world = validate_exclusion_zones(exclusion_zones)
    tilt, azim, pitch = _finite(tilt_deg, "tilt_deg"), _finite(azimuth_deg, "azimuth_deg"), _finite(pitch_m, "pitch_m")
    setback, clearance, base_slope = _finite(setback_m, "setback_m"), _finite(ground_clearance_m, "ground_clearance_m"), _finite(base_slope_deg, "base_slope_deg")
    if not 0.0 <= tilt <= 90.0:
        raise ValueError("tilt_deg must lie in 0..90")
    if not -180.0 <= azim <= 180.0:
        raise ValueError("azimuth_deg must lie in -180..180")
    if pitch <= 0:
        raise ValueError("pitch_m must be positive")
    if setback < 0:
        raise ValueError("setback_m must be non-negative")
    if clearance < 0:
        raise ValueError("ground_clearance_m must be non-negative")
    if not -45.0 <= base_slope <= 45.0:
        raise ValueError("base_slope_deg must lie in -45..45")
    if module_orientation not in ("portrait", "landscape"):
        raise ValueError("module_orientation must be 'portrait' or 'landscape'")
    if max_table_columns is not None and (type(max_table_columns) is not int or max_table_columns < 1):
        raise ValueError("max_table_columns must be a positive integer")
    cell_w = module_width_m if module_orientation == "portrait" else module_length_m
    cell_h = module_length_m if module_orientation == "portrait" else module_width_m
    larg_shed = module_rows * cell_h + max(0, module_rows - 1) * spacing_y_m
    proj_depth = (
        _finite(proj_depth_override_m, "proj_depth_override_m", positive=True)
        if proj_depth_override_m is not None
        else larg_shed * math.cos(math.radians(tilt))
    )
    if pitch <= proj_depth:
        raise ValueError(f"pitch_m ({pitch:.3f} m) must exceed horizontal table depth ({proj_depth:.3f} m)")
    col_step, rot_rad = cell_w + spacing_x_m, math.radians(azim)
    poly_rot = [_rotate(x, y, rot_rad) for x, y in poly_world]
    excl_rot = [[_rotate(x, y, rot_rad) for x, y in p] for p in excl_world]
    vs = [p[1] for p in poly_rot]
    v, v_max = min(vs) + setback, max(vs) - setback
    tables: list[dict[str, Any]] = []
    row_idx = 0
    while v + proj_depth <= v_max + 1e-6:
        scan_offset = min(1e-4, proj_depth / 2)
        front_iv = _scanline_intervals(v + scan_offset, poly_rot)
        back_iv = _scanline_intervals(v + proj_depth - scan_offset, poly_rot)
        overlaps = [(lo, hi) for a0, a1 in front_iv for b0, b1 in back_iv
                    if (hi := min(a1, b1) - setback) - (lo := max(a0, b0) + setback) >= cell_w - 1e-6]
        if excl_rot:
            overlaps = subtract_exclusions_from_intervals(overlaps, excl_rot, v, v + proj_depth, min_length=cell_w - 1e-6)
        row_tables = 0
        for seg_lo, seg_hi in overlaps:
            u = seg_lo
            while u + cell_w <= seg_hi + 1e-6:
                max_avail = int(math.floor((seg_hi - u + spacing_x_m + 1e-6) / col_step))
                if max_table_columns is not None:
                    max_avail = min(max_avail, max_table_columns)
                fitted = next((k for k in range(max_avail, 0, -1)
                               if _rect_inside_polygon(u, u + (k * cell_w + (k - 1) * spacing_x_m), v, v + proj_depth, poly_rot, setback)
                               and (not excl_rot or not rect_overlaps_any_polygon(u, u + (k * cell_w + (k - 1) * spacing_x_m), v, v + proj_depth, excl_rot))), 0)
                if fitted > 0:
                    span = fitted * cell_w + (fitted - 1) * spacing_x_m
                    wx, wy = _rotate(u + 0.5 * span, v, -rot_rad)
                    row_tables += 1
                    tables.append({
                        "name": f"GroundRow{row_idx + 1:02d}_T{row_tables:02d}", "row_index": row_idx + 1,
                        "module_rows": module_rows, "module_columns": fitted, "module_count": module_rows * fitted,
                        "long_shed_m": round(span, 6), "larg_shed_m": round(larg_shed, 6),
                        "origin_m": [round(wx, 6), round(wy, 6), round(clearance, 6)],
                        "tilt_deg": tilt, "azimuth_deg": azim, "base_slope_deg": base_slope,
                    })
                    u += span + max(spacing_x_m, 0.5)
                else:
                    u += col_step
        if row_tables > 0:
            row_idx += 1
        v += pitch
    if not tables:
        raise ValueError("No PV tables fit inside site_polygon with the requested pitch_m, tilt, and setback_m")
    site_area = polygon_area(poly_world)
    total_modules = sum(t["module_count"] for t in tables)
    collector_area = sum(t["long_shed_m"] * t["larg_shed_m"] for t in tables)
    return {
        "tables": tables, "row_count": row_idx, "table_count": len(tables), "total_modules": total_modules,
        "site_area_m2": round(site_area, 2), "collector_area_m2": round(collector_area, 2),
        "gcr": round(collector_area / site_area, 4) if site_area > 0 else 0.0,
        "proj_depth_m": round(proj_depth, 4), "base_slope_deg": base_slope,
        "exclusion_zone_count": len(excl_world),
        "exclusion_area_m2": round(sum(polygon_area(p) for p in excl_world), 2),
    }

def pv_shading_header(
    comment: str,
    total_objs: int,
    *,
    flags: str = SHADING_FLAGS_LINEAR,
    frac_ombre_mod: float = 1.0,
) -> list[str]:
    """Return the standard 3D pvShading header and ListeObjets opening lines."""
    frac = _finite(frac_ombre_mod, "frac_ombre_mod", minimum=0.0, maximum=1.0)
    return [
        "  PVObject_Ombrage=pvShading", f"    Comment={comment}", f"    Flags={flags}",
        "    Precision=1", f"    FracOmbreMod={frac:.3f}", "    FracOmbreThin=0.400", "    GCROffset=1.0",
        "    DisplayedRef=refGeographical", "", f"    ListeObjets, list of={total_objs} TShdObject", "",
    ]

def emit_shading_obstacles(parsed_obstacles: list[dict[str, Any]], *, start_index: int) -> list[str]:
    """Serialize parsed 3D obstacle profiles (trees and meshes) starting at PVObject_<start_index>."""
    # Lazy import avoids headless <-> reader module cycle.
    from pvsyst_auto.native.headless import _faces, _shd_object, _shd_tree
    out: list[str] = []
    for obj_idx, obs in enumerate(parsed_obstacles, start=start_index):
        if obs["kind"] == "tree":
            out.extend(_shd_tree(obj_idx, obj_idx, obs, 0.0, 0.0))
        else:
            out.extend(_shd_object(obj_idx, obj_idx, [tuple(v) for v in obs["vertices"]], _faces(obs)))
    return out

def build_ground_3d_shading(
    tables: list[dict[str, Any]], *, module_name: str, module_width_m: float, module_length_m: float,
    module_orient: str = "moPortrait", spacing_x_m: float = 0.02, spacing_y_m: float = 0.02,
    obstacles: list[dict[str, Any]] | None = None, terrain_mesh: Any = None, orientation_id: int = 1,
    electrical_shading: Any = None, series_modules: int | None = None, is_half_cut: bool = False,
    version: str = SYNTH_VERSION,
) -> list[str]:
    """Emit a PVObject_Ombrage=pvShading block for finite ground tables + optional terrain + 3D obstacles."""
    if not tables:
        raise ValueError("build_ground_3d_shading requires at least one table")
    cfg = normalize_electrical_shading(electrical_shading)
    parsed_obstacles = obstacle_profiles(obstacles) if obstacles else []
    total_objs = len(tables) + len(parsed_obstacles) + (1 if terrain_mesh is not None else 0)
    lines = pv_shading_header("Finite 3D ground polygon shading scene", total_objs, **shading_header_kwargs(cfg))
    obj_idx = 0
    for tbl in tables:
        obj_idx += 1
        ox, oy, oz = tbl["origin_m"]
        tbl_orient = str(tbl.get("module_orient") or module_orient)
        part_lines: list[str] = []
        if cfg["mode"] == "partition":
            dx_mod, dy_mod, _, _ = compute_table_partition_dims(
                long_shed_m=float(tbl["long_shed_m"]), larg_shed_m=float(tbl["larg_shed_m"]),
                module_rows=int(tbl["module_rows"]), module_columns=int(tbl["module_columns"]),
                module_orient=tbl_orient, is_tracker=False, electrical_shading=cfg,
                series_modules=series_modules, is_half_cut=is_half_cut,
            )
            part_lines = [f"      DXModule={dx_mod:.3f}", f"      DYModule={dy_mod:.3f}"]
        lines.extend([
            f"      PVObject_{obj_idx}=pvShdArrayTable", f"        Comment={tbl['name']}",
            f"        Version={version}", "        Flags=$02200074", f"        NoObjCreated={obj_idx}",
            "        NPElemRes=1", "        ColorTechn=$00FF0000", "        ColorSurf=$00FF9999",
            "        ColorBord=$00505050", f"        DOrigine={ox:.6f},{oy:.6f},{oz:.6f}",
            f"        DInclin={tbl['tilt_deg']:.6f}", f"        BaseSlope={tbl.get('base_slope_deg', 0.0):.6f}",
            f"        DAzimut={tbl['azimuth_deg']:.6f}", "        Source=osPVCollada", "      NElemChamps=1",
            f"      NoOrient={orientation_id}", f"      PanneauPVName={module_name};{module_width_m:.3f};{module_length_m:.3f}",
            f"      ModSpacingX={spacing_x_m:.3f}", f"      ModSpacingY={spacing_y_m:.3f}",
            f"      NModInHeight={tbl['module_rows']}", f"      NModInWidth={tbl['module_columns']}",
            f"      ModuleOrient={tbl_orient}", *part_lines, f"        LongShed={tbl['long_shed_m']:.6f}",
            f"        LargShed={tbl['larg_shed_m']:.6f}", "        Cadre=0.000,0.000,0.000,0.000,",
            "      TypeOrigine=2", "      End of PVObject pvShdArrayTable",
        ])
    if terrain_mesh is not None:
        from pvsyst_auto.scene.terrain import emit_pvshd_ground
        obj_idx += 1
        lines.extend(emit_pvshd_ground(terrain_mesh, object_index=obj_idx, version=SEED_RECORD_VERSION))
    lines.extend(emit_shading_obstacles(parsed_obstacles, start_index=obj_idx + 1))
    return [*lines, "    End of ListeObjets", "  End of PVObject pvShading"]

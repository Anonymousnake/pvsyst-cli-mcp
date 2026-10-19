"""Lossless readback of this adapter's scene dialect, not a general COLLADA converter."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

from pvsyst_paths import project_name, safe_filename, variant_id, workspace_file
from pvsyst_scene_file import (
    AUTHORING_TOOL, MAX_PARTS, MAX_TABLES, MAX_TRIANGLES, MAX_VERTICES, NS, PV,
    SceneValidationError, _diagnostic, _finite, build_scene, caller_geometry,
    check_tables, house_profile, layout_profile, table_profiles)

XML_NS = {"n": NS, "pv": PV}
MAX_BYTES = 64 * 1024 * 1024


def _fail(path, message, code="unsupported_scene"):
    raise SceneValidationError([_diagnostic(code, path, message,
        repair_hint="Keep the original file; use the supported scene input or supply the missing context.")])


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
    if len(geometries) > MAX_PARTS + MAX_TABLES:
        _fail("library_geometries", "Scene exceeds the adapter geometry ceiling")
    parts, racks, vertex_count, triangle_count = [], [], 0, 0
    for geometry in geometries:
        identifier = geometry.get("id", "")
        if re.fullmatch(r"Rack(?:[1-9][0-9]*)?", identifier):
            racks.append(geometry)
            continue
        if not re.fullmatch(r"HousePart[1-9][0-9]*", identifier):
            _fail("library_geometries/" + identifier, "Unrecognized geometry identifier")
        positions = _text(geometry, "n:mesh/n:source/n:float_array").split()
        indices = _text(geometry, "n:mesh/n:triangles/n:p").split()
        if len(positions) % 3 or len(indices) % 3:
            _fail(identifier, "Positions and triangle indices must be complete triples")
        vertex_count += len(positions) // 3
        triangle_count += len(indices) // 3
        if vertex_count > MAX_VERTICES or triangle_count > MAX_TRIANGLES:
            _fail(identifier, "Scene exceeds the adapter mesh ceiling")
        vertices = [float(value) for value in positions]
        triangles = [int(value) for value in indices]
        parts.append({"name": geometry.get("name", identifier),
                      "vertices": [vertices[i:i + 3] for i in range(0, len(vertices), 3)],
                      "triangles": [triangles[i:i + 3] for i in range(0, len(triangles), 3)]})
    if not parts or not 1 <= len(racks) <= MAX_TABLES:
        _fail("library_geometries", "House meshes and bounded PV rack geometry are required")
    return parts, racks


def _module(asset):
    module = _one(asset, "n:extra/n:technique/pv:components/pv:modules/pv:module")
    return {"model_name": _text(module, "pv:name"),
            "nom_power_wp": _number(module, "pv:nom_power", positive=True),
            "width_m": _number(module, "pv:width", positive=True) / 1000,
            "length_m": _number(module, "pv:length", positive=True) / 1000,
            "geometry_depth_m": _number(module, "pv:depth", positive=True) / 1000}


def _source(asset, rack):
    coverage = _one(asset, "n:coverage/n:geographic_location")
    tilt = _number(rack, "pv:tilt", minimum=0, maximum=90)
    azimuth = _number(rack, "pv:azimuth", minimum=0, maximum=360)
    if tilt >= 90 or azimuth >= 360:
        _fail("pv:rack", "Only the emitted ordinary fixed-plane range is supported")
    return {"tilt_deg": tilt, "azimuth_deg": azimuth,
            "site": {"latitude": _number(coverage, "n:latitude", positive=True, maximum=90),
                     "longitude": _number(coverage, "n:longitude", minimum=-180, maximum=180),
                     "altitude": _number(coverage, "n:altitude")}}


def _legacy_layout(root, rack):
    translate = _text(root, "n:library_visual_scenes/n:visual_scene/"
                            "n:node[@id='SceneRoot']/n:node[@id='PVTable1']/n:translate")
    return {"module_rows": int(_text(rack, "pv:module_rows")),
            "module_columns": int(_text(rack, "pv:module_columns")),
            "orientation": _text(rack, "pv:module_orientation"),
            "row_spacing_m": _number(rack, "pv:row_spacing", minimum=0),
            "column_spacing_m": _number(rack, "pv:column_spacing", minimum=0),
            "origin_m": [float(value) for value in translate.split()], "roof_mesh": "__missing__"}


def _legacy_tree(expected):
    asset = _one(expected, "n:asset")
    asset.remove(_one(asset, "n:contributor"))
    for element in expected.iter():
        for key in ("id", "source", "url"):
            if key in element.attrib:
                element.set(key, element.get(key).replace("Rack1", "Rack").replace("TableModel1", "TableModel"))
    _one(expected, "n:library_visual_scenes/n:visual_scene/"
                   "n:node[@id='SceneRoot']/n:node[@id='PVTable1']").attrib.pop("name", None)
    return expected


def read_house_pv_scene(components, scene_name):
    """Read authored geometry plus reusable arguments; never write or verify a native project."""
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
            parts, racks = _parts(root)
            geometry = _module(asset)
            modules = caller_geometry(geometry)
            source = _source(asset, _one(racks[0], "n:extra/n:technique/pv:rack"))
            subject = asset.findall("n:subject", XML_NS)
            legacy = not subject
            if legacy:
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
                if (not isinstance(recipe, dict) or set(recipe) != {"version", "source", "house", "layout"}
                        or type(recipe["version"]) is not int or recipe["version"] != 1):
                    _fail("asset/subject", "Only the complete version 1 recipe is supported")
                declared = recipe["source"]
                if (not isinstance(declared, dict) or set(declared) != {"project", "variant", "pan", "module_total"}
                        or type(declared["module_total"]) is not int or declared["module_total"] < 1):
                    _fail("asset/subject#source", "Source declarations require a strict positive module total")
                project_name(declared["project"])
                variant_id(declared["variant"])
                safe_filename(declared["pan"], ".PAN")
                house = {"kind": "mesh", "meshes": parts} if recipe["house"] == {"kind": "mesh"} else recipe["house"]
                pocket = house_profile(house)
                layout = recipe["layout"]
                source["module_total"] = declared["module_total"]
                tables = table_profiles(layout, pocket, modules, source)
                check_tables(pocket, tables)
            expected, counts = build_scene(pocket, tables, modules, source, created, recipe)
            if legacy:
                expected = _legacy_tree(expected)
            rebuilt = ET.tostring(expected, encoding="unicode")
            if (ET.canonicalize(xml_data=text, strip_text=True, rewrite_prefixes=True)
                    != ET.canonicalize(xml_data=rebuilt, strip_text=True, rewrite_prefixes=True)):
                _fail("scene_name", "Unconsumed or modified XML is outside the lossless dialect",
                      "scene_roundtrip_mismatch")
            required = ["output_name"]
            arguments = {"house": house, "layout": layout, "module_geometry": geometry, "dry_run": True}
            if legacy:
                layout = dict(layout)
                layout.pop("roof_mesh")
                arguments["layout"] = layout
                required += ["project", "variant", "layout.roof_mesh"]
            else:
                arguments.update(project=declared["project"], variant=declared["variant"])
            result.update(status="needs_context" if legacy else "ready", required_inputs=required,
                          source=dict(declared, provenance="not-recorded" if legacy else "file-declared"),
                          geometry=counts, create_arguments=arguments)
        except (ValueError, OSError, UnicodeError, ET.ParseError, TypeError, KeyError, OverflowError) as exc:
            result["issues"].append(str(exc))
            result["diagnostics"] = (exc.diagnostics if isinstance(exc, SceneValidationError)
                                     else [_diagnostic("scene_read", "scene_name", str(exc))])
    return result

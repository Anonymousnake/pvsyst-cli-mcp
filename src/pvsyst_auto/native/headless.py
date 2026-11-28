"""Headless native PVsyst 8.1.6 house/PV import: source prep, builder, and verification (no GUI)."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from pvsyst_auto.components.editor import objects, replace_value
from pvsyst_auto.project.circuit import inventory
from pvsyst_auto.project.paths import publish_new
from pvsyst_auto.scene.author import (MAX_TABLES, _num, _rotate_z, check_tables, house_profile,
                                      obstacle_profiles, resolve_source_geometry, source_profile, table_profiles)
from pvsyst_auto.scene.mapping import (ANGLE_TOL, COORD_TOL, source_string_ranges, verify_grouped_hardware,
                                       verify_house_geometry, verify_module_layout, verify_pv_geometry,
                                       verify_tree_obstacles)
from pvsyst_auto.scene.reader import read_house_pv_scene, shading_state
from pvsyst_auto.validation import HEADLESS_RECORD_VERSION, NATIVE_TEXT_OR_SEED

# $20100900 (fine shading + ShdElec) requires pvModuleLayout on native 8.1.6 records.
SHADING_FLAGS = {"linear": "$20000100", "module_layout": "$20100900"}
FORMAT = {
    "shading_scalars": [("Precision", "1"), ("FracOmbreMod", "1.000"), ("FracOmbreThin", "0.400"), ("DisplayedRef", "refGeographical")],
    "version": HEADLESS_RECORD_VERSION, "table_flags": "$02200024", "table_npelemres": "10",
    "table_colors": [("ColorTechn", "$00FF0000"), ("ColorSurf", "$00FF9999"), ("ColorBord", "$00505050")],
    "gen_flags": "$02200006", "gen_color": "$00C0C0C0", "group_flags": "$00", "source": "osPVCollada",
    "type_origine": "1", "nelemchamps": "1", "cadre": "0.000000,0.000000,0.000000,0.000000,",
    "modspacing": "0.000", "base_slope": "0.000000", "moduleorient": {"landscape": "moLandScape", "portrait": "moPortrait"},
    "ml_owner_flags": "$0B00", "ml_glob_state": "mlDefStrings", "ml_subfields_origin": "so3DShadings",
    "ml_table_flags": "$05", "ml_table_state": "mlDefStrings",
}
MAX_TABLES_SINGLE = 3

def _n6(value):
    return f"{0.0 if value == 0 else float(value):.6f}"

def _n3(value):
    return f"{0.0 if value == 0 else float(value):.3f}"

def _faces(obj):
    return obj["triangles"] if "triangles" in obj else obj["faces"]

def _kv(indent, *pairs):
    pad = " " * indent
    return [f"{pad}{k}={v}" for k, v in pairs]

def _anchor_turn(source):
    orientations = source.get("orientations") or []
    return 360.0 - (next(i for i in orientations if i["id"] == 1)["azimuth_deg"] if orientations else source["azimuth_deg"])

def _ordinal_tables(tables, max_tables=MAX_TABLES_SINGLE):
    """Order layout profiles (grouped: high orientation first then name; single: declaration order)."""
    grouped = all("orientation_id" in row for row in tables)
    if not 1 <= len(tables) <= max_tables:
        label = "grouped" if grouped else "single-orientation"
        raise ValueError(f"Native {label} import bridges 1..{max_tables} PV tables")
    return sorted(tables, key=lambda r: (-r["orientation_id"], r["name"])) if grouped else list(tables)

def _shd_object(idx, noc, world, faces):
    """Serialize one pvShdGenObject block."""
    origin = world[0]
    local = [[p[k] - origin[k] for k in range(3)] for p in world]
    npe = sum(1 + len(face) for face in faces)
    lines = [f"      PVObject_{idx}=pvShdGenObject", *_kv(
        8, ("Version", FORMAT["version"]), ("Flags", FORMAT["gen_flags"]), ("NoObjCreated", noc),
        ("NPElements", npe), ("NElements", len(faces)), ("ColorSurf", FORMAT["gen_color"]),
        ("DOrigine", ",".join(_n6(v) for v in origin)), ("DInclin", FORMAT["base_slope"]),
        ("BaseSlope", FORMAT["base_slope"]), ("DAzimut", FORMAT["base_slope"]), ("Source", FORMAT["source"]),
    ), f"        Sommets, TPoly3D={len(world)} Point3D"]
    lines.extend("          Point_%d=" % i + ",".join(_n6(v) for v in p) for i, p in enumerate(local, 1))
    lines.append("        End of TPoly3D")
    lines.append("        Faces=" + "".join("-1," + ",".join(str(i) for i in face) + "," for face in faces))
    return [*lines, "      End of PVObject pvShdGenObject"]

def _shd_table(idx, noc, profile, group, modules, turn):
    """Serialize one pvShdArrayTable block preserving native 6/8-space field indentation."""
    world_origin = _rotate_z(tuple(profile["origin_m"]), turn)
    panel = profile.get("module_geometry") or group.get("module_geometry") or modules
    return [
        f"      PVObject_{idx}=pvShdArrayTable",
        *_kv(8, ("Version", FORMAT["version"]), ("Flags", FORMAT["table_flags"]), ("NoObjCreated", noc),
             ("NPElemRes", FORMAT["table_npelemres"]), *FORMAT["table_colors"],
             ("DOrigine", ",".join(_n6(v) for v in world_origin)), ("DInclin", _n6(profile["tilt_deg"])),
             ("BaseSlope", _n6(group.get("base_slope_deg", 0.0))),
             ("DAzimut", _n6(group["field_azimuth_deg"])), ("Source", FORMAT["source"])),
        *_kv(6, ("NElemChamps", FORMAT["nelemchamps"]), ("NoOrient", profile["orientation_id"]),
             ("PanneauPVName", f"{panel['name']};{_num(panel['width_m'])};{_num(panel['length_m'])}"),
             ("ModSpacingX", _n3(profile["column_spacing_m"])), ("ModSpacingY", _n3(profile["row_spacing_m"])),
             ("NModInHeight", profile["module_rows"]), ("NModInWidth", profile["module_columns"]),
             ("ModuleOrient", FORMAT["moduleorient"][profile["orientation"]])),
        *_kv(8, ("LongShed", _n6(profile["column_span_m"])), ("LargShed", _n6(profile["slope_span_m"])),
             ("Cadre", FORMAT["cadre"])),
        f"      TypeOrigine={FORMAT['type_origine']}", "      End of PVObject pvShdArrayTable",
    ]

def _shd_tree(idx, noc, obstacle, anchor_azim, turn):
    """Serialize one parametric tree pvShdElemObject (ObjetElemType=oeTree)."""
    world_origin = _rotate_z(tuple(obstacle["origin_m"]), turn)
    keys = ("top_height_m", "middle_height_m", "bottom_height_m", "trunk_height_m", "crown_diameter_m", "trunk_diameter_m")
    params = ",".join(_n3(obstacle[k]) for k in keys) + ","
    return [
        f"      PVObject_{idx}=pvShdElemObject",
        *_kv(8, ("Comment", obstacle["name"]), ("Version", FORMAT["version"]), ("Flags", "$02200004"),
             ("NoObjCreated", noc), ("ColorSurf", "$8000"), ("ColorBord", "$275E"),
             ("DOrigine", ",".join(_n6(v) for v in world_origin)), ("DInclin", FORMAT["base_slope"]),
             ("BaseSlope", FORMAT["base_slope"]), ("DAzimut", _n6(anchor_azim + obstacle["rotation_deg"])),
             ("Source", "osPVsyst"), ("ObjetElemType", "oeTree"), ("Params", params)),
        "      End of PVObject pvShdElemObject",
    ]

def _resolve_table_owner(row, orientations):
    """Resolve a grouped table's owning source subarray id without conflating orientation_id and subarray_id."""
    if "source_subarray_id" in row:
        return row["source_subarray_id"]
    oid = row.get("orientation_id", 1)
    matches = [item.get("subarray_id", item["id"]) for item in orientations if item["id"] == oid]
    if len(matches) == 1:
        return matches[0]
    msg = ("Table %r names orientation %r, not a declared source orientation" % (row.get("name"), oid) if not matches else
           "Orientation %r is shared by multiple source subarrays; table %r requires explicit source_subarray_id" % (oid, row.get("name")))
    raise ValueError(msg)

def build_pvshading(house, tables, modules, source, shading_mode="linear", *, max_tables=MAX_TABLES_SINGLE,
                    obstacles=None):
    """Return (pvShading text, {table_name: (slot, created)})."""
    pocket, turn = house_profile(house), _anchor_turn(source)
    orientations = source.get("orientations") or []
    groups = {item.get("subarray_id", item["id"]): item for item in orientations} if orientations else {1: source}
    anchor_azim = (next(i for i in orientations if i["id"] == 1) if orientations else source)["field_azimuth_deg"]
    ordered = _ordinal_tables(tables, max_tables)
    gen_parts, obs_list = list(reversed(pocket["meshes"])), list(obstacles or ())
    mesh_obstacles = [obs for obs in obs_list if obs["kind"] != "tree"]
    tree_obstacles = [obs for obs in obs_list if obs["kind"] == "tree"]
    total = len(ordered) + len(gen_parts) + len(mesh_obstacles) + len(tree_obstacles)

    lines = ["  PVObject_Ombrage=pvShading", "    Comment=declaration", f"    Flags={SHADING_FLAGS[shading_mode]}",
             *_kv(4, *FORMAT["shading_scalars"]), "", f"    ListeObjets, list of={total} TShdObject", ""]
    slot_map, noc, idx = {}, 2, 1
    for profile in ordered:
        oid = profile.get("orientation_id", 1)
        owner = _resolve_table_owner(profile, orientations) if orientations else 1
        if owner not in groups or (orientations and groups[owner]["id"] != oid):
            raise ValueError("Table %r source owner %r does not match orientation %r" % (profile.get("name"), owner, oid))
        lines += _shd_table(idx, noc, {**profile, "orientation_id": oid}, groups[owner], modules, turn) + [""]
        slot_map[profile["name"] if "name" in profile else oid] = (idx, noc)
        noc, idx = noc + 1, idx + 1
    for part in gen_parts + mesh_obstacles:
        lines += _shd_object(idx, noc, [_rotate_z(tuple(v), turn) for v in part["vertices"]], _faces(part)) + [""]
        noc, idx = noc + 1, idx + 1
    for tree in tree_obstacles:
        lines += _shd_tree(idx, noc, tree, anchor_azim, turn) + [""]
        noc, idx = noc + 1, idx + 1

    refs = ",".join(str(x) for x in range(2, 2 + total)) + ","
    lines += ["    End of ListeObjets", "", "    ObjectsGroups, list of=1 TGroupeObjetsOmbrage", "      ObjectsGroup=1",
              "        PVObject_=pvShdObjectGroup", "          Comment=declaration", f"          Version={FORMAT['version']}",
              f"          Flags={FORMAT['group_flags']}", f"          Objects={refs}",
              "        End of PVObject pvShdObjectGroup", "    End of List ObjectsGroups", "  End of PVObject pvShading"]
    return "\n".join(lines) + "\n", slot_map

# --------------------------------------------------------------------- electrical layout
def _parse_electrical(text):
    return _parse_electrical_blocks(objects(text))

def _parse_electrical_blocks(blocks):
    planes, subs = {}, {}
    for block in blocks:
        if block.object_type == "pvOrient":
            planes[int(block.one("NoOrient")[1])] = {
                "field_type": block.one("FieldType")[1], "tilt_deg": float(block.one("FieldTilt")[1]),
                "azim_deg": float(block.one("FieldAzim")[1]), "base_slope_deg": float(block.one("BaseSlope")[1])}
        elif block.object_type == "pvSubArray":
            subs[int(block.one("SubArrayId")[1])] = {
                "orientation": int(block.one("NoOrientation")[1]), "comment": block.one("Comment")[1],
                "version": (block.one("Version")[1] if "Version" in block.fields else ""),
                "flags": (block.one("Flags")[1] if "Flags" in block.fields else ""),
                "nmod_serie": int(block.one("NModSerie")[1]), "nstring_ch": int(block.one("NStringCh")[1]),
                "nstr_orient1": int(block.one("NStrOrient1")[1]), "ninverter": int(block.one("NInverter")[1]),
                "ninvmppt": int(block.one("NInvMPPT")[1]),
                "power_sharing": (block.one("PowerSharingInverterId")[1] if "PowerSharingInverterId" in block.fields else "-1")}
    return planes, subs

def _validate_power_sharing(subs, source_text):
    """Allow unshared (-1) or verified consistent PowerSharingInverterId >= 0 groups."""
    shared_ids = {sid for sid, sub in subs.items() if sub["power_sharing"] != "-1"}
    if not shared_ids:
        return
    for sid in shared_ids:
        sub = subs[sid]
        if not sub["power_sharing"].isdigit():
            raise ValueError("Subarray %d PowerSharingInverterId must be -1 or a non-negative integer" % sid)
        if sub["version"] not in NATIVE_TEXT_OR_SEED or not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", sub["flags"]):
            raise ValueError("Shared subarray %d requires verified Version and Flags" % sid)
        if (int(sub["flags"][1:], 16) & 0x0500) != 0x0500:
            raise ValueError("Shared subarray %d requires independent-MPPT and power-sharing Flags ($0500)" % sid)
    arrays = {int(b.one("SubArrayId")[1]): (b.line - 1, b.end_line - 1)
              for b in objects(source_text) if b.object_type == "pvSubArray"}
    inv = inventory(source_text.splitlines(True), arrays)
    if inv["counts_valid"] is not True:
        raise ValueError("Shared inverter subarrays require complete consistent circuit counts")
    for row in inv["subarrays"]:
        if row["id"] in shared_ids and not row["shared_inverter_lines"]:
            raise ValueError("Shared subarray %d lacks a verified shared circuit InverterNode" % row["id"])

def _ml_table(ordinal, sub, rows, columns, slot, created, globalStr=None, cell_strings=None,
              orientation="landscape", row_spacing_m=0.0, column_spacing_m=0.0):
    positions = ((r, c) for r in range(1, rows + 1) for c in range(1, columns + 1))
    sval = sub if globalStr is None else globalStr
    records = [(i, r, c, sval if cell_strings is None else cell_strings[(r, c)]) for i, (r, c) in enumerate(positions, 1)]
    lines = [f"      PVObject_{ordinal}=pvMLTable", *_kv(
        8, ("Comment", f"MLTable subarray {sub}"), ("Flags", FORMAT["ml_table_flags"]),
        ("NoSubField", ordinal), ("NIdentical", 1), ("NoSubArrayPar", sub), ("NLigMod", rows),
        ("NColMod", columns), ("WPixPrint", 640), ("HPixPrint", 480), ("NoField3D", slot),
        ("NoShed3D", 1), ("NoField3DCreated", created), ("ModLayoutState", FORMAT["ml_table_state"]),
        ("ModOrient", FORMAT["moduleorient"][orientation]), ("ShowModules", "True"),
        ("ShowSubMod", "True"), ("ShowNoString", "True"), ("XFillPos", 1), ("YFillPos", 1),
        ("ModSpacingX", _n3(column_spacing_m)), ("ModSpacingY", _n3(row_spacing_m)), ("RowPadding", "0.000"),
    ), f"        Modules, list of={len(records)} tModule"]
    lines.extend("          Module_%d=%d,%d,%d,%d," % (seq, seq, row, col, sid) for seq, row, col, sid in records)
    return "\n".join([*lines, "        End of List Modules", "      End of PVObject pvMLTable"])

def _binding_dict(binding):
    """Normalize an explicit string layout to a plain dict, or None for the legacy path."""
    if binding is None:
        return None
    if hasattr(binding, "model_dump"):
        binding = binding.model_dump(exclude_none=True)
    if not isinstance(binding, dict):
        raise ValueError("string_layout must be a mapping")
    return binding

def _explicit_cells(binding, tables, subs, ranges):
    """Validate an explicit per-cell string binding and return per-table cell maps."""
    binding = _binding_dict(binding)
    _fail(set(binding) != {"tables"} or not isinstance(binding["tables"], list), "string_layout must contain exactly one 'tables' list")
    _fail(set(ranges) != set(subs), "Source string ranges do not cover exactly the declared subarrays")
    by_name = {t["name"]: t for t in tables}
    seen, cell_maps, table_sub, used = set(), {}, {}, defaultdict(int)
    for entry in binding["tables"]:
        _fail(not isinstance(entry, dict) or set(entry) != {"name", "subarray_id", "cells"}, "Each binding table requires exactly name, subarray_id and cells")
        name, sub_id = entry["name"], entry["subarray_id"]
        _fail(not isinstance(name, str) or name not in by_name or name in seen, "Binding table name must match one declared PV table exactly once: %r", name)
        seen.add(name)
        _fail(type(sub_id) is not int or sub_id not in subs, "Binding subarray_id must be a declared source subarray: %r", sub_id)
        first, last, _ = ranges[sub_id]
        profile = by_name[name]
        orient = profile.get("orientation_id", subs[sub_id]["orientation"])
        _fail(orient != subs[sub_id]["orientation"], "Binding table %r occupies orientation %d but source subarray %d owns orientation %d", name, orient, sub_id, subs[sub_id]["orientation"])
        _fail("source_subarray_id" in profile and profile["source_subarray_id"] != sub_id,
              "Binding table %r belongs to source owner %s, not subarray %d", name, profile.get("source_subarray_id"), sub_id)
        rows, columns = profile["module_rows"], profile["module_columns"]
        _fail(not isinstance(entry["cells"], list), "Binding cells must be a list")
        cell_map = {}
        for cell in entry["cells"]:
            if not isinstance(cell, dict) or set(cell) != {"row", "col", "global_string"}:
                raise ValueError("Each module cell requires exactly row, col and global_string")
            row, col, gstr = cell["row"], cell["col"], cell["global_string"]
            if type(row) is not int or type(col) is not int or type(gstr) is not int:
                raise ValueError("row, col and global_string must be strict integers")
            if not (1 <= row <= rows and 1 <= col <= columns):
                raise ValueError("Binding cell (%d, %d) lies outside table %r (%dx%d)" % (row, col, name, rows, columns))
            if (row, col) in cell_map:
                raise ValueError("Binding repeats cell (%d, %d) in table %r" % (row, col, name))
            if gstr == 0:
                raise ValueError("Draft binding: table %r has unassigned cells; complete the layout before native import" % name)
            if not first <= gstr <= last:
                raise ValueError("global_string %d is outside source subarray %d strings %d..%d" % (gstr, sub_id, first, last))
            cell_map[(row, col)] = gstr
            used[gstr] += 1
        _fail(len(cell_map) != rows * columns, "Binding must assign every cell of table %r (expected %d, got %d)", name, rows * columns, len(cell_map))
        cell_maps[name], table_sub[name] = cell_map, sub_id
    _fail(seen != set(by_name), "string_layout must bind every declared table; missing: %s", sorted(set(by_name) - seen))
    for first, last, mps in ranges.values():
        for gstr in range(first, last + 1):
            _fail(used[gstr] != mps, "Global string %d carries %d cells but its source capacity is NModSerie=%d", gstr, used[gstr], mps)
    return [(by_name[t["name"]], table_sub[t["name"]], cell_maps[t["name"]]) for t in tables]

def _valid_table_dims(profile):
    return (profile["orientation"] in ("landscape", "portrait")
            and profile["row_spacing_m"] >= 0 and profile["column_spacing_m"] >= 0)

def build_module_layout(source_text, tables, slot_map, string_layout=None):
    """Construct the stored pvModuleLayout for a proven profile or explicit cell binding."""
    source_blocks = objects(source_text)
    planes, subs = _parse_electrical_blocks(source_blocks)
    blocks, out = [], []

    def add_ml(ordinal, sub_id, profile, *, global_str=None, cell_strings=None, extra=None):
        rows, columns = profile["module_rows"], profile["module_columns"]
        slot, created = slot_map[profile["name"] if "name" in profile else profile["orientation_id"]]
        blocks.append(_ml_table(ordinal, sub_id, rows, columns, slot, created, globalStr=global_str,
                                cell_strings=cell_strings, orientation=profile["orientation"],
                                row_spacing_m=profile["row_spacing_m"], column_spacing_m=profile["column_spacing_m"]))
        out.append({"subarray_id": sub_id, "no_field_3d_slot": slot, "native_created_id": created,
                    "rows": rows, "columns": columns, "module_count": rows * columns, **(extra or {})})

    if string_layout is not None:
        ranges = source_string_ranges(source_blocks)
        for ordinal, (profile, sub_id, cell_map) in enumerate(_explicit_cells(string_layout, tables, subs, ranges), 1):
            add_ml(ordinal, sub_id, profile, cell_strings=cell_map,
                   extra={"table_name": profile["name"], "global_strings": sorted(set(cell_map.values()))})
    elif len(subs) >= 2:
        ranges = source_string_ranges(source_blocks)
        if any("source_subarray_id" in row for row in tables):
            by_owner = {row["source_subarray_id"]: row for row in tables}
            expected = {sid: subs[sid]["orientation"] for sid in subs}
            _fail(set(by_owner) != set(subs) or any(by_owner[sid]["orientation_id"] != expected[sid] for sid in subs),
                  "Module Layout requires one table per source subarray on its orientation")
        else:
            orient_to_sub = {s["orientation"]: sid for sid, s in subs.items()}
            _fail(len(orient_to_sub) != len(subs), "Shared orientations without source_subarray_id cannot disambiguate tables")
            by_orient = {row["orientation_id"]: row for row in tables}
            _fail(set(by_orient) != set(orient_to_sub) or len(tables) != len(subs), "Module Layout requires one table per source orientation")
            by_owner = {sid: by_orient[subs[sid]["orientation"]] for sid in subs}
        _validate_power_sharing(subs, source_text)
        for ordinal, sub_id in enumerate(subs, 1):
            sub, profile = subs[sub_id], by_owner[sub_id]
            plane, (rows, columns) = planes[sub["orientation"]], (profile["module_rows"], profile["module_columns"])
            first, last, capacity = ranges[sub_id]
            _fail(sub["nstring_ch"] != 1 or first != last or sub["ninverter"] != 1 or sub["ninvmppt"] != 1
                  or sub["nstr_orient1"] != sub["nstring_ch"] or capacity != rows * columns
                  or not 1 <= rows * columns <= 100 or not _valid_table_dims(profile),
                  "Module Layout requires one string of 1..100 modules matching table dimensions")
            _fail(abs(profile["tilt_deg"] - plane["tilt_deg"]) > ANGLE_TOL or abs(plane["base_slope_deg"]) > ANGLE_TOL,
                  "PV table plane differs from its source orientation on subarray %d" % sub_id)
            add_ml(ordinal, sub_id, profile, global_str=first, extra={"orientation_id": sub["orientation"]})
    elif len(subs) == 1:
        _fail(len(planes) != 1, "Single-orientation module layout requires one source plane")
        _fail(len(tables) != 2 or any("orientation_id" in row for row in tables),
              "Single-orientation module layout requires exactly two declaration-ordered tables")
        sub_id, sub = next(iter(subs.items()))
        plane = planes[sub["orientation"]]
        _fail(sub["nstring_ch"] != 2 or sub["ninverter"] != 2 or sub["ninvmppt"] != 2
              or sub["power_sharing"] != "-1" or sub["nstr_orient1"] != 2,
              "Single-orientation module layout requires two inverters, two strings and two total MPPT inputs")
        for k, profile in enumerate(tables, 1):
            rows, columns = profile["module_rows"], profile["module_columns"]
            _fail(not _valid_table_dims(profile) or rows * columns != sub["nmod_serie"] or not 1 <= rows * columns <= 100,
                  "Single-orientation layout tables must match NModSerie with non-negative spacing")
            _fail(abs(profile["tilt_deg"] - plane["tilt_deg"]) > ANGLE_TOL or abs(plane["base_slope_deg"]) > ANGLE_TOL,
                  "PV table plane differs from its source orientation")
            add_ml(k, sub_id, profile, global_str=k, extra={"orientation_id": sub["orientation"], "string_id": k})
    else:
        raise ValueError("Module layout requires one or two source subarrays")
    return "\n".join([
        "  PVObject_ModuleLayout=pvModuleLayout", "    Comment=Module layout (constructed)",
        f"    Flags={FORMAT['ml_owner_flags']}", f"    ModLayoutGlobState={FORMAT['ml_glob_state']}",
        f"    SubFieldsOrigin={FORMAT['ml_subfields_origin']}", "",
        f"    SubFields, list of={len(blocks)} PVObjects", "", "\n\n".join(blocks),
        "    End of SubFields", "    PrintOnReport=False", "    ShowNames=2", "  End of PVObject pvModuleLayout",
    ]), out

# --------------------------------------------------------------------- assembly
def _block(lines, start_pred, end_pred):
    for i, line in enumerate(lines):
        if start_pred(line.strip()):
            for j in range(i + 1, len(lines)):
                if end_pred(lines[j].strip()):
                    return i, j
            raise ValueError("unclosed block from line %d" % i)
    raise ValueError("block start not found")

_PLOT_SAMPLES = {f"Values{axis}{suffix}" for axis in "XYZU" for suffix in ("", "_contd")}

def _simul_plots(lines, root, blocks):
    """Yield only the variant's pvSimulPlot records inside its SimulPlots list."""
    if "SimulPlots" not in root.fields:
        return
    start, value = root.one("SimulPlots")
    if value != "Start":
        raise ValueError("Invalid SimulPlots start")
    by_start = {b.line - 1: b for b in blocks if b.object_type == "pvSimulPlot" and "/" not in b.path}
    index = start + 1
    while index < root.end_line - 1:
        tag = lines[index].strip()
        if tag in ("EndTags", "End of List SimulPlots"):
            return
        if not tag:
            index += 1
        elif plot := by_start.get(index):
            yield plot
            index = plot.end_line
        else:
            break
    raise ValueError("Unclosed or malformed SimulPlots list")

def _result_histograms(owners, blocks):
    """Return histograms directly owned by this root or its simulation plots."""
    return [b for b in blocks if b.object_type == "THistogramme"
            and any(b.path.rpartition("/")[0] == o.path and o.line < b.line < b.end_line < o.end_line for o in owners)]

def strip_result_cache(lines):
    """Clear saved VarList, histograms, plot sample rows/counts and root Flags bit 0 in place."""
    blocks = objects("\n".join(line.rstrip("\r\n") for line in lines), include_private=True)
    root = next((b for b in blocks if not b.path), None)
    _fail(root is None or root.object_type != "pvVCalcul", "Expected a root pvVCalcul object")
    plots = list(_simul_plots(lines, root, blocks))
    report = {"var_list_removed": False, "histograms_removed": 0,
              "plot_sample_rows_removed": 0, "plot_counts_cleared": 0, "root_simulation_bit_cleared": False}
    remove, replace = set(), {}
    for plot in plots:
        for key in _PLOT_SAMPLES & plot.fields.keys():
            remove.update(index for index, _ in plot.fields[key])
        for key in ("NValAcc", "NValRes"):
            if key in plot.fields:
                index, value = plot.one(key)
                _fail(not re.fullmatch(r"[0-9]+", value), f"Invalid plot {key} value")
                if value != "0":
                    replace[index] = replace_value(lines[index], "0")
    report["plot_sample_rows_removed"], report["plot_counts_cleared"] = len(remove), len(replace)
    histograms = _result_histograms([root, *plots], blocks)
    for h in histograms:
        remove.update(range(h.line - 1, h.end_line))
    report["histograms_removed"] = len(histograms)
    # VarList is an unwrapped root list, not a parsed PVObject.
    children = {b.line - 1: b.end_line for b in blocks if b.path and "/" not in b.path}
    start, index = None, root.line
    while index < root.end_line - 1:
        tag = lines[index].strip()
        if index in children and start is None:
            index = children[index]
            continue
        if tag.startswith("VarList, list of="):
            _fail(start is not None or report["var_list_removed"] or not re.fullmatch(r"VarList, list of=[0-9]+\s+TVarSimul", tag), "Malformed or duplicate VarList")
            start = index
        elif tag == "End of the List VarList":
            _fail(start is None, "Unmatched VarList closing tag")
            remove.update(range(start, index + 1))
            report["var_list_removed"], start = True, None
        elif start is not None and (index in children or tag in ("EndTags", "SimulPlots=Start", "Histogram=THistogramme")):
            raise ValueError("Unclosed VarList")
        index += 1
    _fail(start is not None, "Unclosed VarList")
    if "Flags" in root.fields or any(report.values()):
        index, flags = root.one("Flags")
        _fail(not re.fullmatch(r"\$[0-9A-Fa-f]+", flags), "Invalid root Flags value")
        value = int(flags[1:], 16)
        if value & 0x1:
            replace[index] = replace_value(lines[index], f"${value & ~0x1:0{len(flags) - 1}X}")
            report["root_simulation_bit_cleared"] = True
    lines[:] = [replace.get(index, line) for index, line in enumerate(lines) if index not in remove]
    return lines, report

_INPUT_SIMUL_SCOPES = {
    "EconomicEval=TEcoEvaluation": "End of TEcoEvaluation",
    "PVObject_UserNeeds=pvLoad": "End of PVObject pvLoad",
    "PVObject_GridStorage=pvGridStorage": "End of PVObject pvGridStorage",
    "PVObject_GridUnavail=pvUnavail": "End of PVObject pvUnavail",
    "SystemUnavail=TUnavailability": "End of PVObject pvUnavail",
}

def _has_derived_simul_cache(lines):
    closer = None
    for raw in lines:
        s = raw.strip()
        if s.startswith(("VarList, list of=", "PVObject_Simul=")):
            return "VarList, list of=" if s.startswith("VarList, list of=") else "PVObject_Simul="
        if closer is not None:
            if s == closer:
                closer = None
        elif s in _INPUT_SIMUL_SCOPES:
            closer = _INPUT_SIMUL_SCOPES[s]
        elif s.startswith("End of VarSimul="):
            return "End of VarSimul="
        elif re.match(r"^VarSimul_[0-9]+=", s):
            return "VarSimul_"
    return None

def _optional_block(lines, start_tag, end_tag):
    try:
        return _block(lines, lambda x: x == start_tag, lambda x: x == end_tag)
    except ValueError as exc:
        if "block start not found" not in str(exc):
            raise
        return None

def assemble_variant(base_text, pvshading, module_layout=None):
    """Splice the constructed scene into the clean electrical source and strip derived caches."""
    eol = "\r\n" if "\r\n" in base_text else "\n"
    lines = base_text.replace("\r\n", "\n").split("\n")
    block = pvshading.split("\n")[:-1] if pvshading.endswith("\n") else pvshading.split("\n")
    if found := _optional_block(lines, "PVObject_Ombrage=pvShading", "End of PVObject pvShading"):
        lines = lines[:found[0]] + block + lines[found[1] + 1:]
    else:
        idx = max(i for i, ln in enumerate(lines) if ln.strip() == "End of PVObject pvVCalcul")
        lines = lines[:idx] + block + lines[idx:]
    if found := _optional_block(lines, "PVObject_ModuleLayout=pvModuleLayout", "End of PVObject pvModuleLayout"):
        s, e = found
        stop = e + 1 if e + 1 < len(lines) and not lines[e + 1].strip() else e
        del lines[s:stop + 1]
    lines, _ = strip_result_cache(lines)
    if module_layout is not None:
        idx = max(i for i, ln in enumerate(lines) if ln.strip() == "End of PVObject pvVCalcul")
        lines = lines[:idx] + module_layout.split("\n") + [""] + lines[idx:]
    text = eol.join(lines)
    for item in ("pvShdFTable", "ShdTableLin", "Histogram=THistogramme"):
        if item in text:
            raise ValueError("derived item still present: " + item)
    if (derived_simul := _has_derived_simul_cache(lines)) is not None:
        raise ValueError("derived item still present: " + derived_simul)
    if module_layout is None and "pvModuleLayout" in text:
        raise ValueError("unexpected module layout in a linear-mode variant")
    return text

# --------------------------------------------------------------------- source prep & verification
def _source_snapshot_bytes(store, components, original, module_geometry, *, allow_groups=False, layout=None, path=None):
    """Validate an in-memory or disk variant snapshot."""
    _, lines = store._text(original)
    arrays, system = store._sections(lines)
    source = source_profile(store, lines, arrays, system, allow_groups=allow_groups)
    modules, pan_snapshots = resolve_source_geometry(components, source, module_geometry, layout)
    return {"path": path, "bytes": original, "source": source, "modules": modules,
            "pan_snapshot": (pan_snapshots[0] if len(pan_snapshots) == 1 else None), "pan_snapshots": pan_snapshots}

def _source_snapshot(store, components, project, variant, module_geometry, *, allow_groups=False, layout=None):
    """Validate the source variant read-only."""
    with store.lock:
        store._check_project(project)
        path = store._path(project, variant)
        return _source_snapshot_bytes(store, components, store._read(path), module_geometry,
                                      allow_groups=allow_groups, layout=layout, path=path)

def _prepare_source(store, components, scene, *, allow_groups=False, error_cls=ValueError):
    """Bind lossless scene geometry to the current source without driving the GUI."""
    args = scene["create_arguments"]
    snap = _source_snapshot(store, components, args["project"], args["variant"],
                            args["module_geometry"], allow_groups=allow_groups, layout=args["layout"])
    recorded, current = scene["source"], snap["source"]
    if recorded["pan"].casefold() != current["pan_name"].casefold():
        raise error_cls("The scene PAN reference differs from the current source variant")
    if recorded["module_total"] != current["module_total"]:
        raise error_cls("The scene module total differs from the current source variant")
    if (_num(recorded["tilt_deg"]) != _num(current["tilt_deg"])
            or _num(recorded["collada_azimuth_deg"]) != _num(current["azimuth_deg"])
            or any(_num(recorded["site"][k]) != _num(current["site"][k]) for k in ("latitude", "longitude", "altitude"))):
        raise error_cls("The scene PV plane or site differs from the current source variant")
    declared_groups, current_groups = recorded.get("orientations", []), current.get("orientations", [])
    if bool(declared_groups) != bool(current_groups):
        raise error_cls("The scene orientation structure differs from the current source variant")
    if declared_groups:
        actual_groups = recorded.get("orientation_geometry", [])
        if ([i["id"] for i in declared_groups] != [i["id"] for i in current_groups]
                or [i["id"] for i in actual_groups] != [i["id"] for i in current_groups]):
            raise error_cls("The scene must retain each current source orientation identity")
        for declared, actual, plane in zip(declared_groups, actual_groups, current_groups):
            if (declared["module_total"] != plane["module_total"] or actual["module_total"] != plane["module_total"]
                    or _num(actual["tilt_deg"]) != _num(plane["tilt_deg"])
                    or _num(actual["collada_azimuth_deg"]) != _num(plane["azimuth_deg"])):
                raise error_cls("Scene orientation %d differs from the current source plane" % plane["id"])
    pocket = house_profile(args["house"])
    existing_names = [m["name"] for m in pocket["meshes"]] if pocket["kind"] == "mesh" else ["House"]
    obstacles = obstacle_profiles(args.get("obstacles"), existing_names=existing_names)
    tables = table_profiles(args["layout"], pocket, snap["modules"], current)
    check_tables(pocket, tables)
    if len(tables) != int(scene["geometry"]["pv_table_count"]):
        raise error_cls("Recipe table count disagrees with the scene readback")
    return {"snapshot": snap, "house": pocket, "obstacles": obstacles, "table_profiles": tables}

def _verify_scene_geometry(blocks, lines, house, tables, modules, source, obstacles):
    obs_list, turn = list(obstacles or ()), _anchor_turn(source)
    return {
        **verify_house_geometry(blocks, lines, house, source, anchor_turn=turn,
                                extra_meshes=[o for o in obs_list if o["kind"] != "tree"]),
        **verify_tree_obstacles(blocks, [o for o in obs_list if o["kind"] == "tree"], source, anchor_turn=turn),
        **verify_pv_geometry(blocks, tables, modules, source, anchor_turn=turn),
    }

def _verify_source_table_geometry(blocks, tables, modules, source):
    """Verify table origins, spans, and angles against signed source profiles."""
    turn = _anchor_turn(source)
    orientations = source.get("orientations") or []
    planes = {plane["id"]: plane for plane in orientations} if orientations else {1: source}
    native = [block for block in blocks if block.object_type == "pvShdArrayTable"]
    if len(native) != len(tables):
        raise ValueError("Generated scene native PV table count does not match the declaration")
    for table in tables:
        oid = table.get("orientation_id", 1)
        expected = _rotate_z(tuple(float(_num(value)) for value in table["origin_m"]), turn)
        matches = [
            b for b in native
            if (int(b.one("NModInHeight")[1]) == table["module_rows"]
                and int(b.one("NModInWidth")[1]) == table["module_columns"]
                and b.one("ModuleOrient")[1] == FORMAT["moduleorient"][table["orientation"]]
                and int(b.one("NoOrient")[1]) == oid
                and max(abs(float(a) - c) for a, c in zip(b.one("DOrigine")[1].split(","), expected)) <= COORD_TOL
                and abs(float(b.one("DInclin")[1]) - table["tilt_deg"]) <= ANGLE_TOL
                and abs(float(b.one("DAzimut")[1]) - planes[oid]["field_azimuth_deg"]) <= ANGLE_TOL
                and abs(float(b.one("BaseSlope")[1])) <= ANGLE_TOL
                and abs(float(b.one("LongShed")[1]) - float(_num(table["column_span_m"]))) <= COORD_TOL
                and abs(float(b.one("LargShed")[1]) - float(_num(table["slope_span_m"]))) <= COORD_TOL)
        ]
        if len(matches) != 1:
            raise ValueError("Generated PV table for orientation %d has no unique geometry match" % oid)
        native.remove(matches[0])
    return {"table_count": len(tables), "coordinate_tolerance_m": COORD_TOL, "angle_tolerance_deg": ANGLE_TOL}

def verify_generated(text, source_text, house, tables, modules, source, shading_mode, obstacles=None):
    blocks = objects(text)
    lines = [line.rstrip("\r\n") + "\n" for line in text.splitlines()]
    state = shading_state(lines)
    if state.get("mode") != "active":
        raise ValueError("Generated shading is not active: %s" % state.get("issue", state.get("mode")))
    flags, expected_flags = (state.get("flags") or "").upper(), SHADING_FLAGS[shading_mode].upper()
    if flags != expected_flags:
        raise ValueError("Generated pvShading.Flags %s is not the %s template %s" % (flags or "<missing>", shading_mode, expected_flags))
    if state["pv_bindings"]["status"] != "stored_consistent":
        raise ValueError("Generated PV bindings are not consistent: %s" % state["pv_bindings"]["issues"])
    if shading_mode == "module_layout":
        layout = verify_module_layout(blocks, lines)
        if layout["status"] != "stored_consistent" or layout["module_count"] != source["module_total"]:
            raise ValueError("Generated module layout is inconsistent")
        module_block = layout
    else:
        if state.get("module_layout", {}).get("configured"):
            raise ValueError("Linear mode must not carry a module layout")
        module_block = {"configured": False, "status": "absent"}
    hardware = verify_grouped_hardware(objects(source_text), blocks, source_text.splitlines(True), lines, source)
    geometry = {**_verify_scene_geometry(blocks, lines, house, tables, modules, source, obstacles),
                **_verify_source_table_geometry(blocks, tables, modules, source)}
    return {"object_count": state["object_count"], "pv_bindings": state["pv_bindings"]["status"],
            "module_layout": {key: module_block.get(key) for key in ("configured", "status", "module_count")},
            "hardware": hardware, "geometry": geometry}

def _verify_new(path, source_bytes, expect_parts, expect_tables, expect_modules, *, scene_context=None,
                shading_mode="linear", error_cls=ValueError, obstacle_gen_count=0, obstacle_tree_count=0):
    target_bytes = Path(path).read_bytes()
    blocks = objects(target_bytes.decode("utf-8-sig"))
    layout = {}
    if shading_mode == "module_layout":
        try:
            layout["module_layout"] = verify_module_layout(blocks, target_bytes.decode("utf-8-sig").splitlines(True))
        except (ValueError, KeyError) as exc:
            raise error_cls(str(exc)) from exc
    tables = [b for b in blocks if b.object_type == "pvShdArrayTable"]
    generic = [b for b in blocks if b.object_type == "pvShdGenObject"]
    elems = [b for b in blocks if b.object_type == "pvShdElemObject"]
    modules = sum(int(b.one("NModInHeight")[1]) * int(b.one("NModInWidth")[1]) for b in tables)
    expected_generic = expect_parts + obstacle_gen_count
    if len(generic) != expected_generic or len(tables) != expect_tables or len(elems) != obstacle_tree_count:
        raise error_cls("New variant holds %d generic objects, %d elementary objects and %d PV tables, expected %d, %d and %d"
                        % (len(generic), len(elems), len(tables), expected_generic, obstacle_tree_count, expect_tables))
    if modules != expect_modules:
        raise error_cls("New variant module total is %d, expected %d" % (modules, expect_modules))
    if scene_context is not None and scene_context["source"].get("orientations"):
        source_text, target_text = (raw.decode("utf-8-sig") for raw in (source_bytes, target_bytes))
        target_lines = target_text.splitlines(True)
        try:
            hardware = verify_grouped_hardware(objects(source_text), blocks, source_text.splitlines(True), target_lines, scene_context["source"])
            geom = _verify_scene_geometry(blocks, target_lines, scene_context["house"], scene_context["tables"],
                                          scene_context["modules"], scene_context["source"], scene_context.get("obstacles"))
        except ValueError as exc:
            raise error_cls(str(exc)) from exc
        return {"objects": len(generic) + len(elems) + len(tables), "house": len(generic),
                "trees": len(elems), "tables": len(tables), "modules": modules, **hardware, **geom, **layout}
    if len([b for b in blocks if b.object_type == "pvOrient"]) != 1 or any(b.one("NoOrient")[1] != "1" for b in tables):
        raise error_cls("New variant does not reference a single orientation")
    if any(b.one("Source")[1] != "osPVCollada" for b in generic + tables):
        raise error_cls("New variant does not preserve the native PVCollada provenance")
    original = next(b for b in objects(source_bytes.decode("utf-8-sig")) if b.object_type == "pvSubArray")
    target = next(b for b in blocks if b.object_type == "pvSubArray")
    for field in ("PVModule", "GInverter", "NModSerie", "NStringCh", "NStrOrient1", "NInverter", "NInvMPPT"):
        if original.one(field)[1] != target.one(field)[1]:
            raise error_cls("Saved variant hardware differs from the source: " + field)
    return {"objects": len(generic) + len(elems) + len(tables), "house": len(generic),
            "trees": len(elems), "tables": len(tables), "modules": modules, "source_hardware_preserved": True}

# --------------------------------------------------------------------- preflight / execute
_VARIANT_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_VARIANT_IDS = tuple(f"V{lead}{tail}" for lead in _VARIANT_DIGITS[12:] for tail in _VARIANT_DIGITS)

def _next_variant_id(store, project, *, reserved=(), candidates=_VARIANT_IDS):
    root, stem = store._path(project).parent, Path(project).stem
    used = {p.suffix[1:].upper() for p in root.iterdir() if p.stem.casefold() == stem.casefold() and len(p.suffix) >= 2}
    used.update(r.upper() for r in reserved)
    for candidate in candidates:
        if candidate not in used:
            return candidate
    raise ValueError("All requested native 3-character variant identifiers are exhausted")

def _circuit_counts_valid(source_text, subs):
    """Return True if source circuit counts match the given subarray bounds and are valid."""
    arrays = {int(b.one("SubArrayId")[1]): (b.line - 1, b.end_line - 1)
              for b in objects(source_text) if b.object_type == "pvSubArray"}
    if set(arrays) != set(subs):
        return False
    try:
        return inventory(source_text.splitlines(True), arrays)["counts_valid"] is True
    except (ValueError, KeyError):
        return False

def _fail(cond, msg, *args):
    if cond:
        raise ValueError(msg % args if args else msg)

def _check_grouped_owners(tables, orientations, owners, orientation_ids, unknown_owner_msg):
    per_owner = defaultdict(int)
    for row in tables:
        oid = row.get("orientation_id")
        _fail(oid not in orientation_ids, "Table %r names orientation %r, not a declared source orientation", row.get("name"), oid)
        owner = _resolve_table_owner(row, orientations)
        _fail(owner not in owners, unknown_owner_msg, row.get("name"), owner)
        _fail(owners[owner]["id"] != row["orientation_id"], "Table %r source owner %r does not belong to orientation %r", row.get("name"), owner, row["orientation_id"])
        per_owner[owner] += row["module_rows"] * row["module_columns"]
    for owner, item in owners.items():
        _fail(per_owner.get(owner, 0) != item["module_total"], "Source owner %d tables hold %d modules, not the source total %d", owner, per_owner.get(owner, 0), item["module_total"])

def _check_tables_dims(tables, msg):
    if any(not _valid_table_dims(t) for t in tables):
        raise ValueError(msg)

def _explicit_gate(source, tables, source_text, mode, string_layout):
    """Validate source, geometry, and per-string capacity for explicit module layout."""
    _fail(mode != "module_layout", "An explicit string_layout requires shading_mode=module_layout")
    source_blocks = objects(source_text)
    planes, subs = _parse_electrical_blocks(source_blocks)
    ranges = source_string_ranges(source_blocks)
    _fail(not all("name" in row for row in tables), "The explicit module layout requires named PV tables")
    _fail(not 1 <= len(tables) <= MAX_TABLES, "The explicit module layout bridges 1..%d PV tables", MAX_TABLES)
    for sub_id, sub in subs.items():
        _fail(sub["nstring_ch"] < 1 or sub["nmod_serie"] < 1, "Source subarray %d requires positive NStringCh/NModSerie", sub_id)
        _fail(sub["orientation"] not in planes, "Source subarray %d references a missing source orientation %d", sub_id, sub["orientation"])
        plane = planes[sub["orientation"]]
        _fail(plane["field_type"] != "FixedPlane" or plane["base_slope_deg"] != 0.0, "Explicit module layout is bounded to ordinary fixed planes (no slope/averaging)")
        _fail(sub["nstr_orient1"] != sub["nstring_ch"], "Explicit module layout requires every source string on the single orientation")
    _validate_power_sharing(subs, source_text)
    if source.get("orientations"):
        _fail(any("orientation_id" not in row for row in tables), "A grouped source requires every explicit table to carry its source orientation_id")
        orientation_ids = {item["id"] for item in source["orientations"]}
        _fail(orientation_ids != set(planes), "Declared source orientations do not match the parsed source planes")
        owners = {item.get("subarray_id", item["id"]): item for item in source["orientations"]}
        _check_grouped_owners(tables, source["orientations"], owners, orientation_ids, "Table %r names source owner %r, not a declared source subarray")
    else:
        _fail(len(planes) != 1 or len(subs) != 1, "The single-orientation explicit module layout covers one source plane and one subarray")
        _fail(any("orientation_id" in row for row in tables), "The explicit module layout bridges tables without source orientation ids")
    _check_tables_dims(tables, "Explicit module layout requires portrait/landscape tables with non-negative spacing")
    _explicit_cells(string_layout, tables, subs, ranges)
    _fail(not _circuit_counts_valid(source_text, subs), "Explicit module layout requires complete, consistent source circuit counts")

def _support_gate(source, tables, source_text, mode="linear", string_layout=None, *, check_spacing=True):
    """Validate supported native recipes across portrait/landscape, spacing, multi-roof, and PowerSharing."""
    if check_spacing:
        for index, table in enumerate(tables, 1):
            for field in ("row_spacing_m", "column_spacing_m"):
                value = table[field]
                if float(_n3(value)) != value:
                    raise ValueError(f"Native table {table.get('name', f'PVTable{index}')!r}: {field}={value} m cannot be represented at millimetre precision; use a multiple of 0.001 m.")
    if string_layout is not None:
        return _explicit_gate(source, tables, source_text, mode, string_layout)
    orientations = source.get("orientations") or []
    planes, subs = _parse_electrical(source_text)
    for sub in subs.values():
        plane = planes.get(sub["orientation"])
        _fail(plane is not None and plane["base_slope_deg"] != 0.0,
              "Native headless shading requires BaseSlope=0.0 on source orientation %d (headless 3D PV tables emit BaseSlope=0.0)", sub["orientation"])
    if orientations:
        orientation_ids = {item["id"] for item in orientations}
        _fail(not orientation_ids.issubset(set(planes)), "Declared source orientations do not match the parsed source planes")
        owners = {item.get("subarray_id", item["id"]): item for item in orientations}
        _fail(set(owners) != set(subs), "Declared source owners do not match the parsed source subarrays")
        for sub_id, sub in subs.items():
            _fail(owners[sub_id]["id"] != sub["orientation"], "Source subarray %d orientation differs from owner profile", sub_id)
            _fail(sub["nstr_orient1"] != sub["nstring_ch"], "Grouped native method requires every source string on the subarray's orientation")
        _validate_power_sharing(subs, source_text)
        _fail(not _circuit_counts_valid(source_text, subs), "Grouped native method requires complete consistent source circuit counts")
        _fail(not 1 <= len(tables) <= MAX_TABLES, "Grouped native method bridges 1..%d PV tables", MAX_TABLES)
        _check_tables_dims(tables, "Native tables require portrait/landscape orientation and non-negative spacing")
        _check_grouped_owners(tables, orientations, owners, orientation_ids, "Table %r names unknown source owner %r")
        if mode == "linear":
            return
        _fail(mode != "module_layout", "Grouped native path supports linear or module_layout shading")
        _fail(len(tables) != len(subs), "Implicit grouped module_layout requires one table per source subarray; split tables require string_layout")
        for sub in subs.values():
            _fail(sub["nstring_ch"] != 1 or sub["ninverter"] != 1 or sub["ninvmppt"] != 1,
                  "Implicit grouped module_layout requires one string/inverter/MPPT per subarray; multi-string arrays require string_layout")
        return
    _fail(len(planes) != 1 or set(subs) != {1}, "The single-orientation native path requires one source plane and one subarray")
    _fail(any("orientation_id" in row for row in tables), "The single-orientation native path bridges tables without source orientation ids")
    _fail(not all("name" in row for row in tables), "The single-orientation native path requires named PV tables")
    _check_tables_dims(tables, "The single-orientation native path requires portrait/landscape tables with non-negative spacing")
    sub = subs[1]
    _fail(sub["orientation"] not in planes, "The source subarray must reference the sole source plane")
    _fail(sub["power_sharing"] != "-1" or sub["nstr_orient1"] != sub["nstring_ch"], "The single-orientation native path requires all strings on one orientation and no sharing")
    if mode == "module_layout":
        _fail(len(tables) != 2, "The single-orientation module layout requires exactly two PV tables")
        _fail(sub["nstring_ch"] != 2 or sub["ninverter"] != 2 or sub["ninvmppt"] != 2, "The single-orientation module layout requires two inverters, two source strings and two total MPPT inputs")
        _fail(any(t["module_rows"] * t["module_columns"] != sub["nmod_serie"] for t in tables), "Each single-orientation layout table must hold exactly NModSerie modules")
        _fail(not _circuit_counts_valid(source_text, subs), "The single-orientation module layout requires complete consistent source circuit counts")
        return
    _fail(mode != "linear", "The single-orientation native path supports linear or module_layout shading")
    _fail(not 1 <= len(tables) <= MAX_TABLES_SINGLE, "The single-orientation native path bridges 1..%d PV tables", MAX_TABLES_SINGLE)

def _preflight(store, components, scene_name, shading_mode, string_layout=None):
    scene = read_house_pv_scene(components, scene_name)
    if scene.get("status") != "ready":
        return {"status": "needs_context", "issues": [scene.get("issues") or "README scene is not ready"]}
    if string_layout is None:
        string_layout = scene.get("create_arguments", {}).get("string_layout")
    if string_layout is not None and shading_mode != "module_layout":
        raise ValueError("An explicit string_layout requires shading_mode=module_layout")
    prepared = _prepare_source(store, components, scene, allow_groups=True)
    snap, house, obstacles, tables = prepared["snapshot"], prepared["house"], prepared["obstacles"], prepared["table_profiles"]
    source, source_text = snap["source"], snap["bytes"].decode("utf-8-sig")
    _support_gate(source, tables, source_text, shading_mode, string_layout)
    max_tables = MAX_TABLES if (string_layout is not None or bool(source.get("orientations"))) else MAX_TABLES_SINGLE
    pvshading, slot_map = build_pvshading(house, tables, snap["modules"], source, shading_mode, max_tables=max_tables, obstacles=obstacles)
    layout_text, layout_tables = (build_module_layout(source_text, tables, slot_map, string_layout) if shading_mode == "module_layout" else (None, None))
    candidate_text = assemble_variant(source_text, pvshading, layout_text)
    verification = verify_generated(candidate_text, source_text, house, tables, snap["modules"], source, shading_mode, obstacles=obstacles)
    return {"status": "ready", "scene": scene, "snapshot": snap, "house": house, "obstacles": obstacles,
            "tables": tables, "source": source, "candidate_text": candidate_text, "layout_tables": layout_tables, "verification": verification}

def _resolve_target_path(store, project, next_variant):
    return store._path(project, next_variant)

def import_house_pv_scene_native(store, components, scene_name, variant_name,
                                 dry_run=True, confirm=False, shading_mode="linear", string_layout=None):
    """Build a native input variant from the scene declaration without GUI or simulation."""
    result = {"status": "blocked", "stage": "preflight", "method": "native", "dry_run": dry_run, "project": None, "source_variant": None,
              "variant": None, "file": None, "native_variant_created": False, "native_geometry_verified": False,
              "native_module_layout_verified": False, "native_consumption_verified": False, "stored_geometry_verified": False,
              "stored_module_layout_verified": False, "imported": False, "precheck_passed": False, "issues": [], "warnings": [], "artifacts": {}}
    with store.lock:
        try:
            _fail(type(dry_run) is not bool or type(confirm) is not bool, "Use strict booleans")
            _fail(type(shading_mode) is not str or shading_mode not in ("linear", "module_layout"), "shading_mode must be linear or module_layout")
            store._validate_display_name(variant_name, "variant_name")
            pre = _preflight(store, components, scene_name, shading_mode, string_layout)
            if pre["status"] != "ready":
                return {**result, "status": pre["status"], "issues": pre["issues"]}
            snap, args = pre["snapshot"], pre["scene"]["create_arguments"]
            result.update(project=args["project"], source_variant=args["variant"], verification=pre["verification"], precheck_passed=True)
            next_variant = _next_variant_id(store, args["project"])
            if dry_run:
                result.update(status="ready_to_import", stage="preflight", planned_variant=next_variant)
                result["warnings"].append("Preview builds and verifies the native records in memory only (stored_* stay false); nothing is written and no licence or engine action is used. Execution requires dry_run=false and confirm=true.")
                return result
            if not confirm:
                return {**result, "status": "confirmation_required", "issues": ["Call again with dry_run=false and confirm=true to write the new native variant."]}
            _fail(snap["path"].read_bytes() != snap["bytes"], "Source variant changed after preflight")
            for pan_path, pan_before in snap.get("pan_snapshots") or []:
                _fail(components._read(pan_path) != pan_before, "Source PAN changed after preflight")
            target = _resolve_target_path(store, args["project"], next_variant)
            if any(p.name.casefold() == target.name.casefold() for p in target.parent.iterdir()):
                raise FileExistsError("Native variant name already exists: " + target.name)
            lines = pre["candidate_text"].splitlines(keepends=True)
            index, = store._display_name_fields(lines, complete=True)
            lines[index] = replace_value(lines[index], variant_name)
            edited = "".join(lines)
            candidate = store._display_name_prefix(snap["bytes"], variant_name, not variant_name.isascii(), "variant_name", content=edited) + edited.encode("utf-8")
            written = verify_generated(candidate.decode("utf-8-sig"), snap["bytes"].decode("utf-8-sig"),
                                       pre["house"], pre["tables"], snap["modules"], pre["source"], shading_mode, obstacles=pre["obstacles"])
            _fail(snap["path"].read_bytes() != snap["bytes"], "Source variant changed before writing the native variant")
            result["stage"] = "write"
            publish_new(target, candidate)
            obs_list, geom = pre["obstacles"], written["geometry"]
            result.update(
                status="native_variant_created", stage="created", variant=target.suffix[1:].upper(), file=str(target),
                artifacts={"variant": str(target)}, native_variant_created=True,
                stored_geometry_verified=bool(geom.get("house_geometry_verified") and geom.get("tree_obstacles_verified") and geom.get("pv_geometry_verified")),
                stored_module_layout_verified=shading_mode == "module_layout" and bool(written["module_layout"].get("status") == "stored_consistent"),
            )
            if obs_list:
                result.update(obstacle_count=len(obs_list), tree_count=sum(1 for o in obs_list if o["kind"] == "tree"), obstacle_mesh_count=sum(1 for o in obs_list if o["kind"] != "tree"))
        except (ValueError, OSError, UnicodeError, KeyError, TypeError, StopIteration, FileExistsError, RuntimeError) as exc:
            result["issues"].append(str(exc))
            result["status"] = "failed" if result["stage"] == "write" else "blocked"
    return result

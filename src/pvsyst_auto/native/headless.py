"""Headless native PVsyst 8.1.6 house/PV import: declaration -> native VC, no GUI.

This is the `method="native"` branch of ``pvsyst_import_house_pv_scene``. It turns an
authored ``.pvc2`` recipe (house meshes + PV tables) plus the current source variant into
a NEW native ``pvVCalcul`` variant that carries a real ``pvShading`` block (and, opt-in,
a ``pvModuleLayout``) built field-by-field from the declaration. It never reads a golden
scene record, never touches PVsyst.ini/the clipboard/registry/licence, never starts
PVsyst.exe or drives a GUI, and never runs a simulation.

Construction reuses the product reader/author/coordinate maths and the product read-only
verifiers. The stored record is verified before it is written; native loading/consumption
remains a separate, unverified step. Only the record that this adapter can prove is
written: two or more grouped fixed source orientations (same PAN/OND; one exclusive source
subarray per orientation; each on one or more landscape zero-spacing tables via an explicit
per-cell binding, or one landscape zero-spacing table per orientation on the legacy
one-string-per-array profile), or one orientation / one subarray with 1..3 linear
landscape zero-spacing tables sharing the source PAN/OND, or (fine layout) one orientation /
one subarray with exactly two landscape zero-spacing tables of NModSerie modules each and two
total independent MPPT inputs, table k binding global string k.

An explicit per-cell ``string_layout`` is the general path: every table names its source
subarray and every cell carries a global string ordinal, so several strings may share a
table and a string may span tables. Capacity is bound to the global string (the owning
source subarray's NModSerie); the global string intervals come from the source subarray
declaration order (``scene.mapping.source_string_ranges``), so a table can never borrow a
string from another subarray. One or more source subarrays (each on its own fixed plane)
are supported; nothing is auto-allocated.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from pvsyst_auto.components.editor import objects
from pvsyst_auto.project.circuit import inventory
from pvsyst_auto.project.fileio import publish_new
from pvsyst_auto.scene.author import MAX_TABLES, _num, _rotate_z, house_profile
from pvsyst_auto.scene.mapping import (
    ANGLE_TOL, COORD_TOL, source_string_ranges, verify_grouped_hardware, verify_house_geometry,
    verify_module_layout, verify_pv_geometry)
from pvsyst_auto.scene.reader import read_house_pv_scene
from pvsyst_auto.scene.state import shading_state

# pvShading.Flags is mode-specific on native 8.1.6 saved records: linear=$20000100;
# detailed module-layout=$20100900 (0x100000 selects fine shading, 0x800 enables the
# electrical ShdElec loss). $20100900 also requires the pvModuleLayout block to exist
# (R3 M1 hold-out: same flags without the layout is refused natively).
SHADING_FLAGS = {"linear": "$20000100", "module_layout": "$20100900"}

# Serialization/state tokens proven on native 8.1.6 saved records (VC7 / SHADING_LAYOUT.VC3).
# Geometry, PV identity and dimensions are generated from the declaration; only these format
# constants are fixed. Other PVsyst versions are not covered.
FORMAT = {
    "shading_scalars": [("Precision", "1"), ("FracOmbreMod", "1.000"),
                        ("FracOmbreThin", "0.400"), ("DisplayedRef", "refGeographical")],
    "version": "8.1.6",
    "table_flags": "$02200024",
    "table_npelemres": "10",
    "table_colors": [("ColorTechn", "$00FF0000"), ("ColorSurf", "$00FF9999"),
                     ("ColorBord", "$00505050")],
    "gen_flags": "$02200006",
    "gen_color": "$00C0C0C0",
    "group_flags": "$00",
    "source": "osPVCollada",
    "type_origine": "1",
    "nelemchamps": "1",
    "cadre": "0.000000,0.000000,0.000000,0.000000,",
    "modspacing": "0.000",
    "base_slope": "0.000000",
    "moduleorient": {"landscape": "moLandScape", "portrait": "moPortrait"},
    # ModuleLayout owner/table tokens proven to keep verify_module_layout stored_consistent.
    "ml_owner_flags": "$0B00",
    "ml_glob_state": "mlDefStrings",
    "ml_subfields_origin": "so3DShadings",
    "ml_table_flags": "$05",
    "ml_table_state": "mlDefStrings",
}

MAX_TABLES_BRIDGED = 2
# The legacy None recipes stay on the proven 1..3 uniform-tilt single-orientation cap; an
# explicit per-cell string_layout instead uses the author's real 1..MAX_TABLES contract and
# the source module-capacity / per-table positive-cell invariants (no table-count cap).
MAX_TABLES_SINGLE = 3


def _n6(value):
    """Native 8.1.6 stores six decimals; keep that exact storage precision (avoid -0)."""
    value = 0.0 if value == 0 else float(value)
    return f"{value:.6f}"


def _faces(obj):
    return obj["triangles"] if "triangles" in obj else obj["faces"]


def _anchor_turn(source):
    """Native anchor frame rotation: 360 - orientation-1 COLLADA azimuth.

    Equivalent to 180 - orientation-1 FieldAzim, reused through ``author._rotate_z``.
    The zero-anchor path (turn 180) is proven against native saved records; the non-zero
    anchor is derived from the recorded source frame and has only static coverage, so it
    is not a substitute for a full native negative acceptance.
    """
    orientations = source.get("orientations") or []
    if orientations:
        group = next(item for item in orientations if item["id"] == 1)
        return 360.0 - group["azimuth_deg"]
    return 360.0 - source["azimuth_deg"]


def _ordinal_tables(tables, max_tables=MAX_TABLES_SINGLE):
    """Order layout profiles: grouped tables high orientation first (native save order, then by
    table name), single-orientation recipes in declaration order. ``max_tables`` keeps the
    legacy None cap (1..3) while an explicit per-cell binding allows the author's real
    1..MAX_TABLES range; a source orientation may be split over several tables."""
    if all("orientation_id" in row for row in tables):
        if not 1 <= len(tables) <= max_tables:
            raise ValueError("Native grouped import bridges 1..%d PV tables" % max_tables)
        return sorted(tables, key=lambda row: (-row["orientation_id"], row["name"]))
    if not 1 <= len(tables) <= max_tables:
        raise ValueError("Native single-orientation import bridges 1..%d PV tables" % max_tables)
    return list(tables)


def _shd_object(idx, noc, world, faces):
    """One pvShdGenObject: DOrigine = first rotated vertex; Points = vertex - DOrigine."""
    origin = world[0]
    local = [[p[k] - origin[k] for k in range(3)] for p in world]
    npe = sum(1 + len(face) for face in faces)
    lines = ["      PVObject_%d=pvShdGenObject" % idx,
             "        Version=%s" % FORMAT["version"],
             "        Flags=%s" % FORMAT["gen_flags"],
             "        NoObjCreated=%d" % noc,
             "        NPElements=%d" % npe,
             "        NElements=%d" % len(faces),
             "        ColorSurf=%s" % FORMAT["gen_color"],
             "        DOrigine=" + ",".join(_n6(v) for v in origin),
             "        DInclin=%s" % FORMAT["base_slope"],
             "        BaseSlope=%s" % FORMAT["base_slope"],
             "        DAzimut=%s" % FORMAT["base_slope"],
             "        Source=%s" % FORMAT["source"],
             "        Sommets, TPoly3D=%d Point3D" % len(world)]
    for i, p in enumerate(local, 1):
        lines.append("          Point_%d=" % i + ",".join(_n6(v) for v in p))
    lines.append("        End of TPoly3D")
    lines.append("        Faces=" + "".join("-1," + ",".join(str(i) for i in face) + "," for face in faces))
    lines.append("      End of PVObject pvShdGenObject")
    return lines


def _shd_table(idx, noc, profile, group, modules, turn):
    world_origin = _rotate_z(tuple(profile["origin_m"]), turn)
    orient = FORMAT["moduleorient"][profile["orientation"]]
    return ["      PVObject_%d=pvShdArrayTable" % idx,
            "        Version=%s" % FORMAT["version"],
            "        Flags=%s" % FORMAT["table_flags"],
            "        NoObjCreated=%d" % noc,
            "        NPElemRes=%s" % FORMAT["table_npelemres"],
            "        %s=%s" % FORMAT["table_colors"][0],
            "        %s=%s" % FORMAT["table_colors"][1],
            "        %s=%s" % FORMAT["table_colors"][2],
            "        DOrigine=" + ",".join(_n6(v) for v in world_origin),
            "        DInclin=%s" % _n6(profile["tilt_deg"]),
            "        BaseSlope=%s" % FORMAT["base_slope"],
            # Signed source FieldAzim convention (e.g. -45), never 315 standing in for -45.
            "        DAzimut=%s" % _n6(group["field_azimuth_deg"]),
            "        Source=%s" % FORMAT["source"],
            "      NElemChamps=%s" % FORMAT["nelemchamps"],
            "      NoOrient=%d" % profile["orientation_id"],
            "      PanneauPVName=%s;%s;%s" % (modules["name"], _num(modules["width_m"]),
                                            _num(modules["length_m"])),
            "      ModSpacingX=%s" % FORMAT["modspacing"],
            "      ModSpacingY=%s" % FORMAT["modspacing"],
            "      NModInHeight=%d" % profile["module_rows"],
            "      NModInWidth=%d" % profile["module_columns"],
            "      ModuleOrient=%s" % orient,
            "        LongShed=%s" % _n6(profile["column_span_m"]),
            "        LargShed=%s" % _n6(profile["slope_span_m"]),
            "        Cadre=%s" % FORMAT["cadre"],
            "      TypeOrigine=%s" % FORMAT["type_origine"],
            "      End of PVObject pvShdArrayTable"]


def build_pvshading(house, tables, modules, source, shading_mode="linear", *, max_tables=MAX_TABLES_SINGLE):
    """Return (pvShading text, {table_name: (slot, created)}, object/table counts).

    Pure builder: every geometry/PV field is derived from the declaration. No golden
    scene record is read. ``shading_mode`` only selects the complete pvShading.Flags
    template; the module-layout block itself is added separately by the caller.
    ``max_tables`` is the single-orientation table bound (legacy None 1..3, explicit
    per-cell binding 1..MAX_TABLES).
    """
    pocket = house_profile(house)
    turn = _anchor_turn(source)
    orientations = source.get("orientations") or []
    groups = {item["id"]: item for item in orientations} if orientations else {1: source}
    ordered = _ordinal_tables(tables, max_tables)
    gen_parts = list(reversed(pocket["meshes"]))
    total = len(ordered) + len(gen_parts)

    lines = ["  PVObject_Ombrage=pvShading",
             "    Comment=declaration",
             "    Flags=%s" % SHADING_FLAGS[shading_mode]]
    for key, value in FORMAT["shading_scalars"]:
        lines.append("    %s=%s" % (key, value))
    lines += ["", "    ListeObjets, list of=%d TShdObject" % total, ""]

    slot_map = {}
    noc, idx = 2, 1
    for profile in ordered:
        oid = profile.get("orientation_id", 1)
        native_profile = {**profile, "orientation_id": oid}
        lines += _shd_table(idx, noc, native_profile, groups[oid], modules, turn)
        lines.append("")
        # Key by the real table identity (its unique name): a source orientation may own several
        # tables now, so an orientation key is no longer unique. Every profile that reaches the
        # module layout has a name (grouped and named single-orientation recipes); the unnamed
        # legacy linear profile never builds a layout, so its fallback key is unused.
        key = profile["name"] if "name" in profile else oid
        slot_map[key] = (idx, noc)
        noc += 1
        idx += 1
    for part in gen_parts:
        world = [_rotate_z(tuple(v), turn) for v in part["vertices"]]
        lines += _shd_object(idx, noc, world, _faces(part))
        lines.append("")
        noc += 1
        idx += 1

    refs = ",".join(str(x) for x in range(2, 2 + total)) + ","
    lines += ["    End of ListeObjets",
              "",
              "    ObjectsGroups, list of=1 TGroupeObjetsOmbrage",
              "      ObjectsGroup=1",
              "        PVObject_=pvShdObjectGroup",
              "          Comment=declaration",
              "          Version=%s" % FORMAT["version"],
              "          Flags=%s" % FORMAT["group_flags"],
              "          Objects=%s" % refs,
              "        End of PVObject pvShdObjectGroup",
              "    End of List ObjectsGroups",
              "  End of PVObject pvShading"]
    return "\n".join(lines) + "\n", slot_map


# --------------------------------------------------------------------- electrical layout
def _parse_electrical(text):
    return _parse_electrical_blocks(objects(text))


def _parse_electrical_blocks(blocks):
    planes, subs = {}, {}
    for block in blocks:
        if block.object_type == "pvOrient":
            planes[int(block.one("NoOrient")[1])] = {
                "field_type": block.one("FieldType")[1],
                "tilt_deg": float(block.one("FieldTilt")[1]),
                "azim_deg": float(block.one("FieldAzim")[1]),
                "base_slope_deg": float(block.one("BaseSlope")[1])}
        elif block.object_type == "pvSubArray":
            subs[int(block.one("SubArrayId")[1])] = {
                "orientation": int(block.one("NoOrientation")[1]),
                "comment": block.one("Comment")[1],
                "nmod_serie": int(block.one("NModSerie")[1]),
                "nstring_ch": int(block.one("NStringCh")[1]),
                "nstr_orient1": int(block.one("NStrOrient1")[1]),
                "ninverter": int(block.one("NInverter")[1]),
                "ninvmppt": int(block.one("NInvMPPT")[1]),
                "power_sharing": (block.one("PowerSharingInverterId")[1]
                                  if "PowerSharingInverterId" in block.fields else "-1")}
    return planes, subs


def _ml_table(ordinal, sub, rows, columns, slot, created, globalStr=None, cell_strings=None):
    # NoSubArrayPar is the physical source subarray; the module record's last field is the
    # source GLOBAL string. They coincide on the dual two-array profile (string==array id)
    # but diverge on the single-orientation profile (two strings on one subarray).
    # cell_strings (explicit per-cell binding) maps every (row, col) to its global string;
    # globalStr keeps the legacy uniform-per-table scalar path byte-for-byte.
    positions = ((r, c) for r in range(1, rows + 1) for c in range(1, columns + 1))
    if cell_strings is None:
        string_value = sub if globalStr is None else globalStr
        records = [(i, r, c, string_value) for i, (r, c) in enumerate(positions, 1)]
    else:
        records = [(i, r, c, cell_strings[(r, c)]) for i, (r, c) in enumerate(positions, 1)]
    lines = ["      PVObject_%d=pvMLTable" % ordinal,
             "        Comment=MLTable subarray %d" % sub,
             "        Flags=%s" % FORMAT["ml_table_flags"],
             "        NoSubField=%d" % ordinal,
             "        NIdentical=1",
             "        NoSubArrayPar=%d" % sub,
             "        NLigMod=%d" % rows,
             "        NColMod=%d" % columns,
             "        WPixPrint=640",
             "        HPixPrint=480",
             "        NoField3D=%d" % slot,
             "        NoShed3D=1",
             "        NoField3DCreated=%d" % created,
             "        ModLayoutState=%s" % FORMAT["ml_table_state"],
             "        ModOrient=moLandScape",
             "        ShowModules=True",
             "        ShowSubMod=True",
             "        ShowNoString=True",
             "        XFillPos=1",
             "        YFillPos=1",
             "        ModSpacingX=0.000",
             "        ModSpacingY=0.000",
             "        RowPadding=0.000",
             "        Modules, list of=%d tModule" % len(records)]
    for sequence, row, column, string_id in records:
        lines.append("          Module_%d=%d,%d,%d,%d," % (sequence, sequence, row, column, string_id))
    lines.append("        End of List Modules")
    lines.append("      End of PVObject pvMLTable")
    return "\n".join(lines)


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
    """Validate an explicit per-cell string binding and return per-table cell maps.

    Capacity is per GLOBAL string (= the owning source subarray's NModSerie), not per table:
    a table may hold several strings and a string may span tables. ``ranges`` is the source
    ``source_string_ranges(objects(source_text))`` map ``{subarray_id: (first, last,
    modules_per_string)}`` in original subarray order; it fixes each subarray's global string
    interval, so a cell can never borrow a string from another subarray even when the global
    total capacity would still add up. A draft (global_string=0), an incomplete cover, an
    unknown/extra field, a repeated/out-of-range cell, a wrong table/subarray orientation or a
    wrong per-string capacity is refused so the caller can never publish a partial layout.
    """
    binding = _binding_dict(binding)
    if set(binding) != {"tables"} or not isinstance(binding["tables"], list):
        raise ValueError("string_layout must contain exactly one 'tables' list")
    if set(ranges) != set(subs):
        raise ValueError("Source string ranges do not cover exactly the declared subarrays")
    by_name = {table["name"]: table for table in tables}
    seen, cell_maps, table_sub = set(), {}, {}
    used = defaultdict(int)
    for entry in binding["tables"]:
        if not isinstance(entry, dict) or set(entry) != {"name", "subarray_id", "cells"}:
            raise ValueError("Each binding table requires exactly name, subarray_id and cells")
        name = entry["name"]
        if not isinstance(name, str) or name not in by_name or name in seen:
            raise ValueError("Binding table name must match one declared PV table exactly once: %r" % (name,))
        seen.add(name)
        sub_id = entry["subarray_id"]
        if type(sub_id) is not int or sub_id not in subs:
            raise ValueError("Binding subarray_id must be a declared source subarray: %r" % (sub_id,))
        first, last, modules_per_string = ranges[sub_id]
        profile = by_name[name]
        orientation = profile.get("orientation_id", subs[sub_id]["orientation"])
        if orientation != subs[sub_id]["orientation"]:
            raise ValueError("Binding table %r occupies orientation %d but source subarray %d owns "
                             "orientation %d" % (name, orientation, sub_id, subs[sub_id]["orientation"]))
        rows, columns = profile["module_rows"], profile["module_columns"]
        if not isinstance(entry["cells"], list):
            raise ValueError("Binding cells must be a list")
        cell_map = {}
        for cell in entry["cells"]:
            if not isinstance(cell, dict) or set(cell) != {"row", "col", "global_string"}:
                raise ValueError("Each module cell requires exactly row, col and global_string")
            row, column, global_string = cell["row"], cell["col"], cell["global_string"]
            if any(type(value) is not int for value in (row, column, global_string)):
                raise ValueError("row, col and global_string must be strict integers")
            if not (1 <= row <= rows and 1 <= column <= columns):
                raise ValueError("Binding cell (%d, %d) lies outside table %r (%dx%d)"
                                 % (row, column, name, rows, columns))
            if (row, column) in cell_map:
                raise ValueError("Binding repeats cell (%d, %d) in table %r" % (row, column, name))
            if global_string == 0:
                raise ValueError("Draft binding: table %r has unassigned cells; complete the "
                                 "layout before native import" % name)
            if not first <= global_string <= last:
                raise ValueError("global_string %d is outside source subarray %d strings %d..%d"
                                 % (global_string, sub_id, first, last))
            cell_map[(row, column)] = global_string
            used[global_string] += 1
        if len(cell_map) != rows * columns:
            raise ValueError("Binding must assign every cell of table %r (expected %d, got %d)"
                             % (name, rows * columns, len(cell_map)))
        cell_maps[name] = cell_map
        table_sub[name] = sub_id
    if seen != set(by_name):
        raise ValueError("string_layout must bind every declared table; missing: %s"
                         % sorted(set(by_name) - seen))
    for sub_id, (first, last, modules_per_string) in ranges.items():
        for global_string in range(first, last + 1):
            if used[global_string] != modules_per_string:
                raise ValueError("Global string %d carries %d cells but its source capacity is "
                                 "NModSerie=%d" % (global_string, used[global_string], modules_per_string))
    return [(by_name[table["name"]], table_sub[table["name"]], cell_maps[table["name"]])
            for table in tables]


def build_module_layout(source_text, tables, slot_map, string_layout=None):
    """Construct the stored pvModuleLayout for a proven profile.

    ``string_layout is None`` keeps the legacy dual / single-orientation narrow profiles
    byte-for-byte. When an explicit binding is supplied, every table names its source
    subarray and lists each module cell's global string; strings may span tables and a
    table may hold several strings (capacity is per global string = NModSerie, not per
    table). Geometry and angles still come from the source; NoField3D/NoField3DCreated are
    the scene identities build_pvshading stored in ``slot_map`` keyed by each table's unique
    name, never guessed from declaration order. Draft/incomplete/over-capacity or cross-subarray bindings raise
    here, before anything is assembled or published.
    """
    source_blocks = objects(source_text)
    planes, subs = _parse_electrical_blocks(source_blocks)
    blocks, out = [], []
    if string_layout is not None:
        ranges = source_string_ranges(source_blocks)
        for ordinal, (profile, sub_id, cell_map) in enumerate(
                _explicit_cells(string_layout, tables, subs, ranges), 1):
            rows, columns = profile["module_rows"], profile["module_columns"]
            key = profile["name"] if "name" in profile else profile["orientation_id"]
            slot, created = slot_map[key]
            blocks.append(_ml_table(ordinal, sub_id, rows, columns, slot, created,
                                    cell_strings=cell_map))
            out.append({"subarray_id": sub_id, "table_name": profile["name"],
                        "no_field_3d_slot": slot, "native_created_id": created,
                        "rows": rows, "columns": columns, "module_count": rows * columns,
                        "global_strings": sorted(set(cell_map.values()))})
    elif len(subs) == 2:
        if set(planes) != {1, 2} or set(subs) != {1, 2}:
            raise ValueError("Module Layout requires source orientations/arrays 1/2")
        by_orientation = {row["orientation_id"]: row for row in tables}
        if {row["orientation_id"] for row in tables} != {1, 2} or {s["orientation"] for s in subs.values()} != {1, 2}:
            raise ValueError("Module Layout requires two source tables, one per orientation")
        for ordinal, sub_id in enumerate(sorted(subs), 1):
            sub = subs[sub_id]
            plane = planes[sub["orientation"]]
            profile = by_orientation[sub["orientation"]]
            rows, columns = profile["module_rows"], profile["module_columns"]
            if (sub["nstring_ch"] != 1 or sub["ninverter"] != 1 or sub["ninvmppt"] != 1
                    or sub["power_sharing"] != "-1" or sub["nstr_orient1"] != sub["nstring_ch"]
                    or sub["nmod_serie"] != rows * columns
                    or not 1 <= rows * columns <= 100
                    or profile["orientation"] != "landscape"
                    or profile["row_spacing_m"] != 0 or profile["column_spacing_m"] != 0):
                raise ValueError("Module Layout requires one independent string of 1..100 modules and one "
                                 "inverter/MPPT per array on a landscape zero-spacing table")
            if (abs(profile["tilt_deg"] - plane["tilt_deg"]) > ANGLE_TOL
                    or abs(plane["base_slope_deg"]) > ANGLE_TOL):
                raise ValueError("PV table plane differs from its source orientation on subarray %d" % sub_id)
            slot, created = slot_map[by_orientation[sub["orientation"]]["name"]]
            blocks.append(_ml_table(ordinal, sub_id, rows, columns, slot, created))
            out.append({"subarray_id": sub_id, "orientation_id": sub["orientation"],
                        "no_field_3d_slot": slot, "native_created_id": created,
                        "rows": rows, "columns": columns, "module_count": rows * columns})
    elif len(subs) == 1:
        if len(planes) != 1:
            raise ValueError("Single-orientation module layout requires one source plane")
        if len(tables) != 2 or any("orientation_id" in row for row in tables):
            raise ValueError("Single-orientation module layout requires exactly two declaration-ordered tables")
        sub_id, sub = next(iter(subs.items()))
        plane = planes[sub["orientation"]]
        if (sub["nstring_ch"] != 2 or sub["ninverter"] != 2 or sub["ninvmppt"] != 2
                or sub["power_sharing"] != "-1" or sub["nstr_orient1"] != 2):
            raise ValueError("Single-orientation module layout requires two inverters, two strings and two total MPPT inputs")
        for k, profile in enumerate(tables, 1):
            rows, columns = profile["module_rows"], profile["module_columns"]
            if (profile["orientation"] != "landscape" or profile["row_spacing_m"] != 0
                    or profile["column_spacing_m"] != 0 or rows * columns != sub["nmod_serie"]
                    or not 1 <= rows * columns <= 100):
                raise ValueError("Single-orientation layout tables must be landscape zero-spacing NModSerie blocks")
            if (abs(profile["tilt_deg"] - plane["tilt_deg"]) > ANGLE_TOL
                    or abs(plane["base_slope_deg"]) > ANGLE_TOL):
                raise ValueError("PV table plane differs from its source orientation")
            slot, created = slot_map[profile["name"]]
            blocks.append(_ml_table(k, sub_id, rows, columns, slot, created, globalStr=k))
            out.append({"subarray_id": sub_id, "orientation_id": sub["orientation"],
                        "string_id": k, "no_field_3d_slot": slot, "native_created_id": created,
                        "rows": rows, "columns": columns, "module_count": rows * columns})
    else:
        raise ValueError("Module layout requires one or two source subarrays")
    body = ["  PVObject_ModuleLayout=pvModuleLayout",
            "    Comment=Module layout (constructed)",
            "    Flags=%s" % FORMAT["ml_owner_flags"],
            "    ModLayoutGlobState=%s" % FORMAT["ml_glob_state"],
            "    SubFieldsOrigin=%s" % FORMAT["ml_subfields_origin"],
            "",
            "    SubFields, list of=%d PVObjects" % len(blocks),
            "",
            "\n\n".join(blocks),
            "    End of SubFields",
            "    PrintOnReport=False",
            "    ShowNames=2",
            "  End of PVObject pvModuleLayout"]
    return "\n".join(body), out


# --------------------------------------------------------------------- assembly
def _block(lines, start_pred, end_pred):
    for i, line in enumerate(lines):
        if start_pred(line.strip()):
            for j in range(i + 1, len(lines)):
                if end_pred(lines[j].strip()):
                    return i, j
            raise ValueError("unclosed block from line %d" % i)
    raise ValueError("block start not found")


def assemble_variant(base_text, pvshading, module_layout=None):
    """Splice the constructed scene into the clean electrical source; strip derived caches."""
    eol = "\r\n" if "\r\n" in base_text else "\n"
    lines = base_text.split(eol)
    block = pvshading.split("\n")
    if block and block[-1] == "":
        block = block[:-1]
    s, e = _block(lines, lambda x: x == "PVObject_Ombrage=pvShading",
                  lambda x: x == "End of PVObject pvShading")
    lines = lines[:s] + block + lines[e + 1:]
    s, e = _block(lines, lambda x: x.startswith("VarList, list of="),
                  lambda x: x == "End of the List VarList")
    del lines[s:e + 1]
    while True:
        try:
            s, e = _block(lines, lambda x: x == "Histogram=THistogramme", lambda x: x == "End of THistogramme")
        except ValueError:
            break
        del lines[s:e + 1]
    for i, line in enumerate(lines):
        if line == "  Flags=$00100001":
            lines[i] = "  Flags=$00100000"
            break
    else:
        raise ValueError("root Flags=$00100001 not found")
    if module_layout is not None:
        closing = "End of PVObject pvVCalcul"
        index = max(i for i, line in enumerate(lines) if line == closing)
        lines = lines[:index] + module_layout.split("\n") + [""] + lines[index:]
    text = eol.join(lines)
    banned = ("pvShdFTable", "ShdTableLin", "VarSimul", "Histogram=THistogramme")
    for item in banned:
        if item in text:
            raise ValueError("derived item still present: " + item)
    if module_layout is None and "pvModuleLayout" in text:
        raise ValueError("unexpected module layout in a linear-mode variant")
    return text


# --------------------------------------------------------------------- verification
def _verify_source_table_geometry(blocks, tables, modules, source):
    """Table origins/spans/angles versus the signed source profiles using the real anchor turn."""
    turn = _anchor_turn(source)
    orientations = source.get("orientations") or []
    planes = ({plane["id"]: plane for plane in orientations} if orientations else {1: source})
    native = [block for block in blocks if block.object_type == "pvShdArrayTable"]
    if len(native) != len(tables):
        raise ValueError("Generated scene native PV table count does not match the declaration")
    for table in tables:
        oid = table.get("orientation_id", 1)
        origin = [float(_num(value)) for value in table["origin_m"]]
        expected = _rotate_z(tuple(origin), turn)
        matches = []
        for block in native:
            if (int(block.one("NModInHeight")[1]) != table["module_rows"]
                    or int(block.one("NModInWidth")[1]) != table["module_columns"]
                    or block.one("ModuleOrient")[1] != FORMAT["moduleorient"][table["orientation"]]
                    or int(block.one("NoOrient")[1]) != oid):
                continue
            values = [float(v) for v in block.one("DOrigine")[1].split(",")]
            if (max(abs(a - b) for a, b in zip(values, expected)) <= COORD_TOL
                    and abs(float(block.one("DInclin")[1]) - table["tilt_deg"]) <= ANGLE_TOL
                    and abs(float(block.one("DAzimut")[1]) - planes[oid]["field_azimuth_deg"]) <= ANGLE_TOL
                    and abs(float(block.one("BaseSlope")[1])) <= ANGLE_TOL
                    and abs(float(block.one("LongShed")[1]) - float(_num(table["column_span_m"]))) <= COORD_TOL
                    and abs(float(block.one("LargShed")[1]) - float(_num(table["slope_span_m"]))) <= COORD_TOL):
                matches.append(block)
        if len(matches) != 1:
            raise ValueError("Generated PV table for orientation %d has no unique geometry match" % oid)
        native.remove(matches[0])
    return {"table_count": len(tables), "coordinate_tolerance_m": COORD_TOL, "angle_tolerance_deg": ANGLE_TOL}


def verify_generated(text, source_text, house, tables, modules, source, shading_mode):
    blocks = objects(text)
    lines = [line.rstrip("\r\n") + "\n" for line in text.splitlines()]
    state = shading_state(lines)
    if state.get("mode") != "active":
        raise ValueError("Generated shading is not active: %s" % state.get("issue", state.get("mode")))
    flags = (state.get("flags") or "").upper()
    expected_flags = SHADING_FLAGS[shading_mode].upper()
    if flags != expected_flags:
        raise ValueError("Generated pvShading.Flags %s is not the %s template %s"
                         % (flags or "<missing>", shading_mode, expected_flags))
    if state["pv_bindings"]["status"] != "stored_consistent":
        raise ValueError("Generated PV bindings are not consistent: %s" % state["pv_bindings"]["issues"])
    layout_state = state.get("module_layout", {})
    if shading_mode == "module_layout":
        layout = verify_module_layout(blocks, lines)
        if layout["status"] != "stored_consistent" or layout["module_count"] != source["module_total"]:
            raise ValueError("Generated module layout is inconsistent")
        module_block = layout
    else:
        if layout_state.get("configured"):
            raise ValueError("Linear mode must not carry a module layout")
        module_block = {"configured": False, "status": "absent"}
    hardware = verify_grouped_hardware(
        objects(source_text), blocks, source_text.splitlines(True), lines, source)
    geometry = {**verify_house_geometry(blocks, lines, house, source, anchor_turn=_anchor_turn(source)),
                **verify_pv_geometry(blocks, tables, modules, source, anchor_turn=_anchor_turn(source)),
                **_verify_source_table_geometry(blocks, tables, modules, source)}
    return {"object_count": state["object_count"], "pv_bindings": state["pv_bindings"]["status"],
            "module_layout": {key: module_block.get(key) for key in ("configured", "status", "module_count")},
            "hardware": hardware, "geometry": geometry}


# --------------------------------------------------------------------- preflight / execute
def _next_variant_id(store, project):
    root = store._path(project).parent
    stem = Path(project).stem
    used = set()
    for path in root.iterdir():
        if path.suffix.casefold().startswith(".vc") and path.stem.casefold() == stem.casefold():
            match = re.fullmatch(r"\.VC([0-9]+)", path.suffix, re.I)
            if match:
                used.add(int(match[1]))
    number = 0
    while number in used:
        number += 1
    return "VC%d" % number


def _circuit_counts_valid(source_text, subs):
    """Complete, consistent source circuit counts for the given subarray bounds."""
    arrays = {int(block.one("SubArrayId")[1]): (block.line - 1, block.end_line - 1)
              for block in objects(source_text) if block.object_type == "pvSubArray"}
    if set(arrays) != set(subs):
        return False
    try:
        return inventory(source_text.splitlines(True), arrays)["counts_valid"] is True
    except (ValueError, KeyError):
        return False


def _explicit_gate(source, tables, source_text, mode, string_layout):
    """Source/geometry/capacity gate for an explicit per-cell string binding.

    The general path verified here is independent of the source cardinality. A single-orientation
    source accepts 1..MAX_TABLES (the author limit of 64) landscape zero-spacing tables bound to
    its one ordinary fixed plane. A grouped source accepts any number of homogeneous fixed
    orientations: every table names a declared ``orientation_id``, each source orientation owns at
    least one table, and every orientation's tables sum to that group's module total, so N=1/2/3/...
    share one loop instead of an exact-2 template. Strings are assigned per cell under a per-global
    capacity of the owning subarray's NModSerie -- a table may hold several strings and a string may
    span tables -- and the subarray/string/table counts and global string intervals follow the
    actual source declaration order (never a numeric id sort). These are author/reader profile
    bounds, not a claim about native software limits or native consumption. Only the shapes that
    are actually checked are refused (portrait, non-zero spacing/padding, shared inputs,
    non-fixed/sloped planes, a table whose orientation is not a declared source orientation, a
    group whose tables miss its module total, or incomplete cell wiring); nothing is silently
    treated as verified and no circuit is fabricated.
    """
    if mode != "module_layout":
        raise ValueError("An explicit string_layout requires shading_mode=module_layout")
    source_blocks = objects(source_text)
    planes, subs = _parse_electrical_blocks(source_blocks)
    ranges = source_string_ranges(source_blocks)
    if not all("name" in row for row in tables):
        raise ValueError("The explicit module layout requires named PV tables")
    if not 1 <= len(tables) <= MAX_TABLES:
        raise ValueError("The explicit module layout bridges 1..%d PV tables" % MAX_TABLES)
    for sub_id, sub in subs.items():
        if sub["nstring_ch"] < 1 or sub["nmod_serie"] < 1:
            raise ValueError("Source subarray %d requires positive NStringCh/NModSerie" % sub_id)
        if sub["orientation"] not in planes:
            raise ValueError("Source subarray %d references a missing source orientation %d"
                             % (sub_id, sub["orientation"]))
        plane = planes[sub["orientation"]]
        if plane["field_type"] != "FixedPlane" or abs(plane["base_slope_deg"]) > ANGLE_TOL:
            raise ValueError("Explicit module layout is bounded to ordinary fixed planes (no slope/averaging)")
        if sub["power_sharing"] != "-1":
            raise ValueError("Explicit module layout excludes shared inverter inputs; sharing is not verified")
        if sub["nstr_orient1"] != sub["nstring_ch"]:
            raise ValueError("Explicit module layout requires every source string on the single orientation")
    if source.get("orientations"):
        if any("orientation_id" not in row for row in tables):
            raise ValueError("A grouped source requires every explicit table to carry its source orientation_id")
        # Verify the declared source-orientation profile itself (any N, not a ">2 rejected"
        # template): every table names a real source orientation, every orientation owns at least
        # one table, and each group's tables hold exactly its module total. This is the gate's own
        # N-profile check; the per-cell binding/capacity is then enforced by _explicit_cells.
        orientation_ids = {item["id"] for item in source["orientations"]}
        if orientation_ids != set(planes):
            raise ValueError("Declared source orientations do not match the parsed source planes")
        per_orientation = defaultdict(int)
        for row in tables:
            if row["orientation_id"] not in orientation_ids:
                raise ValueError("Table %r names orientation %r, not a declared source orientation"
                                 % (row.get("name"), row["orientation_id"]))
            per_orientation[row["orientation_id"]] += row["module_rows"] * row["module_columns"]
        for plane in source["orientations"]:
            if per_orientation.get(plane["id"], 0) != plane["module_total"]:
                raise ValueError("Orientation %d tables hold %d modules, not the source total %d"
                                 % (plane["id"], per_orientation.get(plane["id"], 0), plane["module_total"]))
    else:
        if len(planes) != 1 or len(subs) != 1:
            raise ValueError("The single-orientation explicit module layout covers one source plane and one subarray")
        if any("orientation_id" in row for row in tables):
            raise ValueError("The explicit module layout bridges tables without source orientation ids")
    for table in tables:
        if (table["orientation"] != "landscape" or table["row_spacing_m"] != 0
                or table["column_spacing_m"] != 0):
            raise ValueError("Explicit module layout requires landscape zero-spacing tables; "
                             "portrait or non-zero spacing is not verified")
    _explicit_cells(string_layout, tables, subs, ranges)
    if not _circuit_counts_valid(source_text, subs):
        raise ValueError("Explicit module layout requires complete, consistent source circuit counts")


def _support_gate(source, tables, source_text, mode="linear", string_layout=None):
    """Reject anything outside the proven native recipes.

    Without an explicit binding the implicit path stays exactly two source orientations / two
    tables (or one orientation / one subarray with 1..3 linear tables, or the two-table fine
    layout). Three or more orientations are refused clearly here and must supply an explicit
    string_layout, which routes to the general per-group invariants instead of these count caps.
    """
    if string_layout is not None:
        _explicit_gate(source, tables, source_text, mode, string_layout)
        return
    orientations = source.get("orientations") or []
    planes, subs = _parse_electrical(source_text)
    if orientations:
        if len(orientations) != 2 or {item["id"] for item in orientations} != {1, 2}:
            raise ValueError("The implicit native method supports exactly two source orientations 1/2; "
                             "three or more require an explicit string_layout")
        if len(tables) != 2 or {row["orientation_id"] for row in tables} != {1, 2}:
            raise ValueError("The native method requires one PV table per source orientation")
        if set(subs) != {1, 2} or {row["orientation"] for row in subs.values()} != {1, 2}:
            raise ValueError("The native method requires source arrays 1/2 on distinct orientations")
        for table in tables:
            if (table["orientation"] != "landscape" or table["row_spacing_m"] != 0
                    or table["column_spacing_m"] != 0):
                raise ValueError("The native method requires landscape zero-spacing tables")
        for sub in subs.values():
            if (sub["nstring_ch"] != 1 or sub["ninverter"] != 1 or sub["ninvmppt"] != 1
                    or sub["power_sharing"] != "-1" or sub["nstr_orient1"] != sub["nstring_ch"]):
                raise ValueError("The native method requires one independent string/inverter/MPPT per array")
        return
    if len(planes) != 1 or set(subs) != {1}:
        raise ValueError("The single-orientation native path requires one source plane and one subarray")
    if any("orientation_id" in row for row in tables):
        raise ValueError("The single-orientation native path bridges tables without source orientation ids")
    if not all("name" in row for row in tables):
        raise ValueError("The single-orientation native path requires named PV tables")
    for table in tables:
        if (table["orientation"] != "landscape" or table["row_spacing_m"] != 0
                or table["column_spacing_m"] != 0):
            raise ValueError("The single-orientation native path requires landscape zero-spacing tables")
    sub = subs[1]
    if sub["orientation"] not in planes:
        raise ValueError("The source subarray must reference the sole source plane")
    if sub["power_sharing"] != "-1" or sub["nstr_orient1"] != sub["nstring_ch"]:
        raise ValueError("The single-orientation native path requires all strings on one orientation and no sharing")
    if mode == "module_layout":
        if len(tables) != 2:
            raise ValueError("The single-orientation module layout requires exactly two PV tables")
        if (sub["nstring_ch"] != 2 or sub["ninverter"] != 2 or sub["ninvmppt"] != 2):
            raise ValueError("The single-orientation module layout requires two inverters, two source strings and two total MPPT inputs")
        if any(table["module_rows"] * table["module_columns"] != sub["nmod_serie"] for table in tables):
            raise ValueError("Each single-orientation layout table must hold exactly NModSerie modules")
        if not _circuit_counts_valid(source_text, subs):
            raise ValueError("The single-orientation module layout requires complete consistent source circuit counts")
        return
    if mode != "linear":
        raise ValueError("The single-orientation native path supports linear or module_layout shading")
    if not 1 <= len(tables) <= MAX_TABLES_SINGLE:
        raise ValueError("The single-orientation native path bridges 1..%d PV tables" % MAX_TABLES_SINGLE)


def _preflight(store, components, scene_name, shading_mode, string_layout=None):
    from pvsyst_auto.native.scene import _prepare_source

    scene = read_house_pv_scene(components, scene_name)
    if scene.get("status") != "ready":
        return {"status": "needs_context", "issues": [scene.get("issues") or "README scene is not ready"]}
    if string_layout is None:
        string_layout = scene.get("create_arguments", {}).get("string_layout")
    if string_layout is not None and shading_mode != "module_layout":
        raise ValueError("An explicit string_layout requires shading_mode=module_layout")
    prepared = _prepare_source(store, components, scene, allow_groups=True)
    snap = prepared["snapshot"]
    house = prepared["house"]
    tables = prepared["table_profiles"]
    source = snap["source"]
    source_text = snap["bytes"].decode("utf-8-sig")
    _support_gate(source, tables, source_text, shading_mode, string_layout)

    pvshading, slot_map = build_pvshading(
        house, tables, snap["modules"], source, shading_mode,
        max_tables=(MAX_TABLES if string_layout is not None else MAX_TABLES_SINGLE))
    layout_text, layout_tables = (None, None)
    if shading_mode == "module_layout":
        layout_text, layout_tables = build_module_layout(source_text, tables, slot_map, string_layout)
    candidate_text = assemble_variant(source_text, pvshading, layout_text)
    verification = verify_generated(candidate_text, source_text, house, tables, snap["modules"],
                                    source, shading_mode)
    return {"status": "ready", "scene": scene, "snapshot": snap, "house": house, "tables": tables,
            "source": source, "candidate_text": candidate_text, "layout_tables": layout_tables,
            "verification": verification}


def import_house_pv_scene_native(store, components, scene_name, variant_name,
                                 dry_run=True, confirm=False, shading_mode="linear",
                                 string_layout=None):
    """Build a native input variant from the declaration without any GUI or simulation.

    ``string_layout`` (explicit per-cell wiring) is optional; when omitted it is read from
    the recipe's ``create_arguments`` so the public import path needs no extra parameter.
    """
    result = {"status": "blocked", "stage": "preflight", "method": "native", "dry_run": dry_run,
              "project": None, "source_variant": None, "variant": None, "file": None,
              "native_variant_created": False, "native_geometry_verified": False,
              "native_module_layout_verified": False, "native_consumption_verified": False,
              "stored_geometry_verified": False, "stored_module_layout_verified": False,
              "imported": False, "precheck_passed": False,
              "issues": [], "warnings": [], "artifacts": {}}
    with store.lock:
        try:
            if type(dry_run) is not bool or type(confirm) is not bool:
                raise ValueError("Use strict booleans")
            if type(shading_mode) is not str or shading_mode not in ("linear", "module_layout"):
                raise ValueError("shading_mode must be linear or module_layout")
            store._validate_display_name(variant_name, "variant_name")
            pre = _preflight(store, components, scene_name, shading_mode, string_layout)
            if pre["status"] != "ready":
                result.update(status=pre["status"], issues=pre["issues"])
                return result
            snap = pre["snapshot"]
            args = pre["scene"]["create_arguments"]
            result.update(project=args["project"], source_variant=args["variant"],
                          verification=pre["verification"], precheck_passed=True)
            next_variant = _next_variant_id(store, args["project"])
            if dry_run:
                result.update(status="ready_to_import", stage="preflight", planned_variant=next_variant)
                result["warnings"].append(
                    "Preview builds and verifies the native records in memory only (stored_* stay false); "
                    "nothing is written and no licence or engine action is used. Execution requires "
                    "dry_run=false and confirm=true.")
                return result
            if not confirm:
                result.update(status="confirmation_required",
                              issues=["Call again with dry_run=false and confirm=true to write the new native variant."])
                return result
            # Source/PAN must still be the inspected bytes before anything is built or published.
            if snap["path"].read_bytes() != snap["bytes"]:
                raise ValueError("Source variant changed after preflight")
            if snap["pan_snapshot"] is not None:
                path, before = snap["pan_snapshot"]
                if components._read(path) != before:
                    raise ValueError("Source PAN changed after preflight")
            target = store._path(args["project"], next_variant)
            if any(p.name.casefold() == target.name.casefold() for p in target.parent.iterdir()):
                raise FileExistsError("Native variant name already exists: " + target.name)
            original = snap["bytes"]
            lines = pre["candidate_text"].splitlines(keepends=True)
            index, = store._display_name_fields(lines, complete=True)
            from pvsyst_auto.components.editor import replace_value
            lines[index] = replace_value(lines[index], variant_name)
            edited = "".join(lines)
            # The emitted variant is consumed by PVsyst, whose legacy reader treats a BOM-less
            # file as Windows-1252. Mark UTF-8 when the source already had a BOM, the display
            # name is non-ASCII, or the content itself carries non-ASCII (e.g. a degree comment).
            bom = store._display_name_prefix(original, variant_name, not variant_name.isascii(),
                                             "variant_name", content=edited)
            candidate = bom + edited.encode("utf-8")
            # Re-verify the exact bytes about to be published; a display-name edit cannot change structure.
            written = verify_generated(candidate.decode("utf-8-sig"), snap["bytes"].decode("utf-8-sig"),
                                       pre["house"], pre["tables"], snap["modules"], pre["source"], shading_mode)
            # Last source/PAN check immediately before the atomic no-overwrite publish.
            if snap["path"].read_bytes() != snap["bytes"]:
                raise ValueError("Source variant changed before writing the native variant")
            result["stage"] = "write"
            # publish_new stages the bytes and renames them into place, so a failed or interrupted
            # write cannot leave a partial variant, and a concurrently created name is refused.
            publish_new(target, candidate)
            result.update(status="native_variant_created", stage="created", variant=target.suffix[1:].upper(),
                          file=str(target), artifacts={"variant": str(target)},
                          native_variant_created=True,
                          stored_geometry_verified=bool(written["geometry"].get("house_geometry_verified")
                                                        and written["geometry"].get("pv_geometry_verified")),
                          stored_module_layout_verified=shading_mode == "module_layout" and bool(
                              written["module_layout"].get("status") == "stored_consistent"))
            result["warnings"].append(
                "Wrote one NEW native input variant only. Geometry and layout are stored and verified "
                "(stored_geometry_verified/stored_module_layout_verified); PVsyst has NOT loaded or consumed "
                "them (native_geometry_verified/native_module_layout_verified/native_consumption_verified/"
                "imported stay false). Run an explicit simulation to verify native consumption. The source "
                "variant/PAN are unchanged.")
        except (ValueError, OSError, UnicodeError, KeyError, TypeError, StopIteration,
                FileExistsError, RuntimeError) as exc:
            result["issues"].append(str(exc))
            result["status"] = "failed" if result["stage"] == "write" else "blocked"
    return result

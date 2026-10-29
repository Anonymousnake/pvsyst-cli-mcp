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
written: either two fixed source orientations with one landscape zero-spacing table each
and one independent string/inverter/MPPT per array, or one orientation / one subarray with
1..3 linear landscape zero-spacing tables sharing the source PAN/OND, or (fine layout) one
orientation / one subarray with exactly two landscape zero-spacing tables of NModSerie
modules each and two total independent MPPT inputs, table k binding global string k.
"""
from __future__ import annotations

import re
from pathlib import Path

from pvsyst_auto.components.editor import objects
from pvsyst_auto.project.circuit import inventory
from pvsyst_auto.project.fileio import publish_new
from pvsyst_auto.scene.author import _num, _rotate_z, house_profile
from pvsyst_auto.scene.mapping import (
    ANGLE_TOL, COORD_TOL, verify_grouped_hardware, verify_house_geometry,
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
# Single-orientation native path: the author/readback layer already round-trips 1..64 tables
# on one orientation, but only the 1..3 uniform-tilt linear shape is covered by this adapter's
# stored-record verification today. Keep a conservative cap; the author's 1..64 stays intact.
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


def _ordinal_tables(tables):
    """Order layout profiles: two-orientation groups high orientation first (native save order),
    single-orientation recipes in declaration order."""
    if all("orientation_id" in row for row in tables):
        if len(tables) != MAX_TABLES_BRIDGED:
            raise ValueError("Native two-orientation import bridges exactly two PV tables")
        return sorted(tables, key=lambda row: -row["orientation_id"])
    if not 1 <= len(tables) <= MAX_TABLES_SINGLE:
        raise ValueError("Native single-orientation import bridges 1..%d PV tables" % MAX_TABLES_SINGLE)
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


def build_pvshading(house, tables, modules, source, shading_mode="linear"):
    """Return (pvShading text, {orientation_id: (slot, created)}, object/table counts).

    Pure builder: every geometry/PV field is derived from the declaration. No golden
    scene record is read. ``shading_mode`` only selects the complete pvShading.Flags
    template; the module-layout block itself is added separately by the caller.
    """
    pocket = house_profile(house)
    turn = _anchor_turn(source)
    orientations = source.get("orientations") or []
    groups = {item["id"]: item for item in orientations} if orientations else {1: source}
    ordered = _ordinal_tables(tables)
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
        # Key by the real table identity: a dual profile carries orientation_id (unique per table),
        # a single-orientation profile carries only its name (two tables share orientation 1, so an
        # orientation key would overwrite the first slot). build_module_layout looks up the same key.
        key = oid if "orientation_id" in profile else profile["name"]
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
    planes, subs = {}, {}
    for block in objects(text):
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


def _ml_table(ordinal, sub, rows, columns, slot, created, globalStr=None):
    # NoSubArrayPar is the physical source subarray; the module record's last field is the
    # source GLOBAL string. They coincide on the dual two-array profile (string==array id)
    # but diverge on the single-orientation profile (two strings on one subarray).
    string_value = sub if globalStr is None else globalStr
    records = [(i, r, c, string_value)
               for i, (r, c) in enumerate(
                   ((r, c) for r in range(1, rows + 1) for c in range(1, columns + 1)), 1)]
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


def build_module_layout(source_text, tables, slot_map):
    """Construct the stored pvModuleLayout for a proven narrow profile.

    Two profiles are supported: the dual two-orientation / two-array one-string-per-table
    recipe (string id == array id), and the single-orientation / one-subarray recipe with two
    landscape tables, each holding one string of NModSerie modules. In the single profile
    table k (declaration order) binds source global string k; NoSubArrayPar is 1 for both.
    """
    planes, subs = _parse_electrical(source_text)
    blocks, out = [], []
    if len(subs) == 2:
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
            slot, created = slot_map[sub["orientation"]]
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


def _support_gate(source, tables, source_text, mode="linear"):
    """Reject anything outside the proven native recipes: two orientations / two tables,
    or one orientation / one subarray with either 1..3 linear tables or exactly two
    landscape tables holding two source strings (single-orientation fine layout)."""
    orientations = source.get("orientations") or []
    planes, subs = _parse_electrical(source_text)
    if orientations:
        if len(orientations) != 2 or {item["id"] for item in orientations} != {1, 2}:
            raise ValueError("The native method supports exactly two source orientations 1/2")
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


def _preflight(store, components, scene_name, shading_mode):
    from pvsyst_auto.native.scene import _prepare_source

    scene = read_house_pv_scene(components, scene_name)
    if scene.get("status") != "ready":
        return {"status": "needs_context", "issues": [scene.get("issues") or "README scene is not ready"]}
    prepared = _prepare_source(store, components, scene, allow_groups=True)
    snap = prepared["snapshot"]
    house = prepared["house"]
    tables = prepared["table_profiles"]
    source = snap["source"]
    source_text = snap["bytes"].decode("utf-8-sig")
    _support_gate(source, tables, source_text, shading_mode)

    pvshading, slot_map = build_pvshading(house, tables, snap["modules"], source, shading_mode)
    layout_text, layout_tables = (None, None)
    if shading_mode == "module_layout":
        layout_text, layout_tables = build_module_layout(source_text, tables, slot_map)
    candidate_text = assemble_variant(source_text, pvshading, layout_text)
    verification = verify_generated(candidate_text, source_text, house, tables, snap["modules"],
                                    source, shading_mode)
    return {"status": "ready", "scene": scene, "snapshot": snap, "house": house, "tables": tables,
            "source": source, "candidate_text": candidate_text, "layout_tables": layout_tables,
            "verification": verification}


def import_house_pv_scene_native(store, components, scene_name, variant_name,
                                 dry_run=True, confirm=False, shading_mode="linear"):
    """Build a native input variant from the declaration without any GUI or simulation."""
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
            pre = _preflight(store, components, scene_name, shading_mode)
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
            bom = store._display_name_prefix(original, variant_name, not variant_name.isascii(), "variant_name")
            lines = pre["candidate_text"].splitlines(keepends=True)
            index, = store._display_name_fields(lines, complete=True)
            from pvsyst_auto.components.editor import replace_value
            lines[index] = replace_value(lines[index], variant_name)
            candidate = bom + "".join(lines).encode("utf-8")
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

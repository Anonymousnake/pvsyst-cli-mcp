"""Component synthesis, commercial form editing, and guarded BTR capacity-curve editing."""
from __future__ import annotations

import difflib
import json
import math
import re
import sys
from datetime import datetime

from pvsyst_auto.components.editor import (
    ASSIGNMENT,
    MAX_POINTS,
    NUMBER,
    ObjectBlock,
    PAN_ALIASES,
    Profile,
    checked_points,
    curve_inventory,
    inspect_profile_points,
    objects,
    point_count_errors,
    replace_key,
    replace_value,
    write_points,
)
from pvsyst_auto.components.single_diode import (
    synthesize_btr_from_datasheet,
    synthesize_ond_from_datasheet,
    synthesize_pan_from_datasheet,
)
from pvsyst_auto.project.paths import workspace_file
from pvsyst_auto.validation import NATIVE_TEXT, TEXT_LABEL

CAPACITY_TAG = "CapaCourant"
CAPACITY_ALIAS = "Capa_DischRate"
CAPACITY_PATHS = frozenset((CAPACITY_TAG, CAPACITY_ALIAS))
BTR_CURVE_ALIASES = {CAPACITY_ALIAS: CAPACITY_TAG, "SelfDisch_Temp": "IAutoShape",
                     "Capa_Temperature": "CapaTemper", "VMaxCharge_Rate": "VMaxChargeRate"}
BTR_KNOWN_TAGS = frozenset((*BTR_CURVE_ALIASES.values(), "DVGassShape", "NbCycleMax"))
RATIO_RANGE = (1.15, 1.45)

FORM = {
    "Manufacturer": {"type": "text", "required": True},
    "Model": {"type": "text", "required": True},
    "DataSource": {"type": "text", "required": False},
    "YearBeg": {"type": "integer", "minimum": 0, "maximum": 9999},
    "Width": {"type": "number", "minimum": 0, "unit": "m"},
    "Height": {"type": "number", "minimum": 0, "unit": "m"},
    "Depth": {"type": "number", "minimum": 0, "unit": "m"},
    "Weight": {"type": "number", "minimum": 0, "unit": "kg"},
    "NPieces": {"type": "integer", "minimum": 1, "maximum": 2147483647},
    "PriceDate": {"type": "date", "format": "dd/mm/yy HH:MM", "required": False},
}
COMMERCIAL_PATH = "PVObject_Commercial"
MAX_TEXT = 2048
MAX_REMARKS = 5

SCALAR_UNITS = {
    "PAN": {
        "PNom": ("W",), "ISC": ("A",), "Voc": ("V",), "Imp": ("A",), "Vmp": ("V",),
        "NCelS": ("count", "cells"), "NCelP": ("count", "strings"), "NDiode": ("count",),
        "BifacialityFactor": ("ratio", "%"), "VMaxIEC": ("V",), "VMaxUL": ("V",),
        "MuISC": ("mA/degC", "mA/C", "A_per_degC", "pct_per_degC"),
        "muVocSpec": ("mV/degC", "mV/C", "V_per_degC", "pct_per_degC"),
        "muPmpReq": ("pct_per_degC",),
    },
    "OND": {
        "PNomConv": ("kW",), "PMaxOUT": ("kW",), "VMppMin": ("V",), "VMPPMax": ("V",), "VAbsMax": ("V",),
        "EfficMax": ("%",), "EfficEuro": ("%",), "VOutConv": ("V",), "NbMPPT": ("count",),
        "NbInputs": ("count",), "IDCMax": ("A",),
        "VNomEff1": ("V",), "VNomEff2": ("V",), "VNomEff3": ("V",),
        "EfficMaxV1": ("%",), "EfficMaxV2": ("%",), "EfficMaxV3": ("%",),
        "EfficEuroV1": ("%",), "EfficEuroV2": ("%",), "EfficEuroV3": ("%",),
    },
    "BTR": {
        "CapNomC10": ("Ah",), "EnergyKWh": ("kWh",), "VNom": ("V",),
        "NCellSerie": ("count", "cells"), "NCellParall": ("count", "strings"),
        "ResInterne": ("mOhm", "Ohm"), "EfficI": ("ratio", "%"),
        "IMaxCharge": ("A",), "IMaxDischarge": ("A",), "CycleLife80DOD": ("count", "cycles"),
        "SOHEOL": ("ratio", "%"), "DureeVie20": ("years", "yr"),
    },
}
DIMENSION_UNITS = {"Width": ("m", "mm"), "Height": ("m", "mm"), "Depth": ("m", "mm"), "Weight": ("kg",)}


# ---------------------------------------------------------------------------
# Guarded BTR capacity curve editing
# ---------------------------------------------------------------------------

def _btr_control(blocks: list[ObjectBlock]) -> dict:
    result = {"source": "unknown", "path": None, "version": None,
              "battery_technology": None, "ratio_100h_range": None, "errors": []}
    try:
        roots = [b for b in blocks if b.path == ""]
        if len(roots) != 1 or roots[0].object_type != "pvBattery":
            raise ValueError("Expected exactly one root pvBattery object")
        root = roots[0]
        version, technology = root.one("Version")[1], root.one("BattTechnol")[1]
        result.update(version=version[:256], battery_technology=technology[:256])
        capacity = [b for b in blocks if b.path in CAPACITY_PATHS]
        if len(capacity) != 1 or not isinstance(capacity[0], Profile):
            raise ValueError("Expected exactly one root CapaCourant or Capa_DischRate curve, never both")
        result.update(path=capacity[0].path, source="file" if capacity[0].path == CAPACITY_TAG else "unrecognized-tag")
        if version not in NATIVE_TEXT:
            raise ValueError(f"Battery curve editing is verified only for BTR Version={TEXT_LABEL}")
        norm = technology.casefold().removeprefix("bt")
        if norm not in ("pb_sealed_agm", "pb_sealed_gel"):
            raise ValueError("Battery curve editing supports Pb_Sealed_AGM and Pb_Sealed_Gel only")
        result["ratio_100h_range"] = list(RATIO_RANGE)
    except ValueError as exc:
        result["errors"].append(str(exc))
    return result


def _btr_profile_errors(profile: Profile, points: list) -> list[str]:
    errors = [] if len(points) >= 4 else ["Battery capacity curves require at least four active points"]
    try:
        if profile.one("Mode")[1] != "1":
            errors.append("Only battery profile Mode=1 (linear) is supported for editing")
    except ValueError as exc:
        errors.append(str(exc))
    return errors


def capacity_ratio(points: list[list[float]]) -> float:
    """Validate our supported domain and interpolate the native C100/C10 check."""
    points = checked_points(points, len(points))
    if len(points) < 4:
        raise ValueError("Battery capacity curves require at least four active points")
    if any(x <= 0 or y <= 0 for x, y in points):
        raise ValueError("Discharge duration and relative capacity must be positive")
    if any(a[1] > b[1] for a, b in zip(points, points[1:])):
        raise ValueError("Supported capacity curves require nondecreasing relative capacity")
    if not points[0][0] <= 100 <= points[-1][0]:
        raise ValueError("Capacity curve must cover 100 hours; extrapolation is unsupported")
    for (xa, ya), (xb, yb) in zip(points, points[1:]):
        if xa <= 100 <= xb:
            ratio = ya + (yb - ya) * ((100 - xa) / (xb - xa))
            if not math.isfinite(ratio) or not RATIO_RANGE[0] <= ratio <= RATIO_RANGE[1]:
                raise ValueError("Capacity ratio C100/C10 at 100 hours must be within [1.15, 1.45]")
            return ratio
    raise ValueError("Capacity curve must cover 100 hours")


def battery_curve_inventory(text: str) -> dict:
    try:
        blocks = objects(text)
    except ValueError as exc:
        return {"items": [], "errors": [str(exc)], "truncated": False}
    control = _btr_control(blocks)
    found = [b for b in blocks if isinstance(b, Profile)]
    items = []
    for profile in found[:16]:
        points, errors = inspect_profile_points(profile, blocks)
        cap = profile.path in CAPACITY_PATHS
        edit_errors = (errors + control["errors"] + _btr_profile_errors(profile, points) if cap
                       else ["Only CapaCourant / Capa_DischRate capacity curves are editable"])
        native_tag = BTR_CURVE_ALIASES.get(profile.path, profile.path if profile.path in BTR_KNOWN_TAGS else None)
        item = {
            "path": profile.path, "line": profile.line, "cli_tag": native_tag,
            "cli_recognition": ("unrecognized-tag" if profile.path in BTR_CURVE_ALIASES
                                else "recognized" if native_tag else "unverified"),
            "editable": not edit_errors, "edit_errors": edit_errors,
            "structure_complete": not errors, "errors": errors, "points": points, "point_count": len(points),
            "allocated_point_count": int(profile.one("NPtsMax")[1]) if not errors else None,
            "point_count_editable": not edit_errors and not point_count_errors(profile),
            "point_count_errors": point_count_errors(profile) if not errors else errors,
            "point_count_range": [4, MAX_POINTS], "requires_expected_sha256": True,
            "requires_use_file_curve": profile.path == CAPACITY_ALIAS,
            "axes": ["discharge duration (h)", "capacity relative to C10"] if cap else None,
        }
        if cap:
            item.update(ratio_100h=None, value_errors=[])
            try:
                if control["errors"]:
                    raise ValueError("Capacity ratio check is unavailable for this battery configuration")
                if errors:
                    raise ValueError("Cannot check capacity ratio with incomplete curve structure")
                if profile.one("Mode")[1] != "1":
                    raise ValueError("Capacity ratio inspection supports Mode=1 only")
                item["ratio_100h"] = capacity_ratio(points)
            except ValueError as exc:
                item["value_errors"].append(str(exc))
        items.append(item)
    return {"control": control, "items": items, "truncated": len(found) > len(items)}


def replace_battery_curve(text: str, updates: dict[str, list[list[float]]], use_file_curve: bool = False) -> str:
    if not isinstance(updates, dict) or len(updates) != 1 or not set(updates) <= CAPACITY_PATHS:
        raise ValueError("Battery curve edits support one existing CapaCourant or Capa_DischRate curve")
    blocks = objects(text)
    control = _btr_control(blocks)
    if control["errors"]:
        raise ValueError("; ".join(control["errors"]))
    path = next(iter(updates))
    if path != control["path"]:
        raise ValueError("Use the existing capacity curve path returned by inspection")
    if path == CAPACITY_ALIAS and not use_file_curve:
        raise ValueError("Capa_DischRate is ignored by CLI; supply use_file_curve=true to rename it to CapaCourant")
    profile = next(b for b in blocks if b.path == path)
    previous = profile.points()
    if errors := profile.problems + _btr_profile_errors(profile, previous):
        raise ValueError("; ".join(errors))
    points = checked_points(updates[path])
    capacity_ratio(points)
    lines = text.splitlines(keepends=True)
    write_points(lines, profile, previous, points)
    if path == CAPACITY_ALIAS:
        lines[profile.line - 1] = replace_key(lines[profile.line - 1], CAPACITY_TAG)
    return "".join(lines)


# ---------------------------------------------------------------------------
# Commercial form editing
# ---------------------------------------------------------------------------

def _commercial_blocks(text: str):
    blocks = objects(text)
    roots = [b for b in blocks if not b.path]
    commercial = [b for b in blocks if b.path == COMMERCIAL_PATH]
    if len(roots) != 1 or len(commercial) != 1 or commercial[0].object_type != "pvCommercial":
        raise ValueError("Expected one root object and one PVObject_Commercial=pvCommercial")
    if roots[0].one("Version")[1] not in NATIVE_TEXT:
        raise ValueError(f"Commercial form editing supports text Version={TEXT_LABEL}")
    if any(b.path.startswith(COMMERCIAL_PATH + "/") for b in blocks):
        raise ValueError("Nested commercial objects require a separate schema")
    return roots[0], commercial[0]


def _remarks(lines, block):
    """Return the exact direct-child array span; reject ambiguous or damaged arrays."""
    starts, ends = [], []
    for i in range(block.line, block.end_line - 1):
        if re.fullmatch(r"\s*Remarks\s*,\s*Count\s*=\s*\d+\s*", lines[i].rstrip("\r\n")):
            starts.append(i)
        elif re.match(r"\s*Remarks\b", lines[i]):
            raise ValueError("Malformed Remarks count header")
        if re.match(r"\s*End of Remarks\s*=", lines[i]):
            ends.append(i)
    if not starts and not ends:
        if any(name.startswith("Str_") for name in block.fields):
            raise ValueError("Orphan or unsupported string-array rows in commercial object")
        return None, []
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError("Expected one complete Remarks array")
    start, end = starts[0], ends[0]
    count = int(lines[start].split("=")[-1].strip())
    if count > 256 or end - start - 1 != count:
        raise ValueError("Remarks rows do not match Count (inspection limit 256)")
    values = []
    for n, line in enumerate(lines[start + 1:end], 1):
        match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if not match or match[2] != f"Str_{n}":
            raise ValueError("Remarks rows must be numbered consecutively")
        values.append(match[4])
    footer = re.fullmatch(r"\s*End of Remarks\s*=(.*)", lines[end].rstrip("\r\n"))
    if footer[1].strip() != (values[-1].strip() if values else ""):
        raise ValueError("Remarks closing value does not match the final row")
    if any(not start < i < end for key, entries in block.fields.items() if key.startswith("Str_") for i, _ in entries):
        raise ValueError("Orphan or unsupported string-array rows outside Remarks")
    return (start, end), values


def _commercial_value(key, value):
    if not isinstance(value, str) or len(value) > MAX_TEXT or any(not c.isprintable() for c in value):
        raise ValueError(f"{key} must be single-line text of at most {MAX_TEXT} characters without controls")
    value = value.strip()
    spec = FORM[key]
    if spec["type"] == "text":
        if spec["required"] and not value:
            raise ValueError(f"{key} cannot be empty")
    elif spec["type"] == "date":
        if value:
            if not re.fullmatch(r"\d{2}/\d{2}/\d{2} \d{2}:\d{2}", value):
                raise ValueError("PriceDate must use dd/mm/yy HH:MM")
            try:
                datetime.strptime(value, "%d/%m/%y %H:%M")
            except ValueError:
                raise ValueError("PriceDate must be a valid calendar date/time") from None
    else:
        try:
            if not NUMBER.fullmatch(value):
                raise ValueError()
            number = float(value)
        except ValueError:
            raise ValueError(f"{key} must be a finite number") from None
        if not math.isfinite(number) or number < spec["minimum"] or number > spec.get("maximum", math.inf):
            raise ValueError(f"{key} is outside the supported range")
        if spec["type"] == "integer" and not re.fullmatch(r"[0-9]+", value):
            raise ValueError(f"{key} must be a decimal integer")
    return value


def commercial_inventory(kind, text):
    result = {"path": COMMERCIAL_PATH, "editable": False, "errors": [], "fields": {},
              "remarks": [], "remarks_count": 0, "remarks_truncated": False,
              "max_remarks": MAX_REMARKS, "requires_expected_sha256": True}
    try:
        root, block = _commercial_blocks(text)
        for key, spec in FORM.items():
            values = block.fields.get(key, [])
            if len(values) > 1:
                raise ValueError(f"Duplicate commercial field: {key}")
            raw = values[0][1] if values else None
            entry = {**spec, "present": bool(values), "editable": True, "can_add": True,
                     "value": raw[:MAX_TEXT] if raw is not None else None,
                     "value_truncated": raw is not None and len(raw) > MAX_TEXT,
                     "line": values[0][0] + 1 if values else None}
            if kind == "PAN" and key in ("Width", "Height"):
                paired = "LargApp" if key == "Width" else "LongApp"
                entry.update(exclusive_minimum=0, paired_root_field=paired,
                             paired_root_present=bool(root.fields.get(paired)),
                             editable=len(root.fields.get(paired, [])) <= 1)
            result["fields"][key] = entry
        _, remarks = _remarks(text.splitlines(keepends=True), block)
        result.update(
            remarks=[v[:MAX_TEXT] for v in remarks[:MAX_REMARKS]], remarks_count=len(remarks),
            remarks_truncated=(len(remarks) > MAX_REMARKS or any(len(v) > MAX_TEXT for v in remarks[:MAX_REMARKS])),
            editable=True,
        )
        if kind == "PAN":
            result["dimension_pairs"] = {"Width": "LargApp", "Height": "LongApp"}
            result["remarks_note"] = "The fifth PAN remark may encode a model association or functional option"
    except ValueError as exc:
        result["errors"].append(str(exc))
    return result


def replace_commercial(kind, text, updates, remarks=None):
    if not isinstance(updates, dict) or any(k not in FORM for k in updates):
        raise ValueError("Commercial fields: " + ", ".join(FORM))
    if remarks is not None and (not isinstance(remarks, list) or len(remarks) > MAX_REMARKS):
        raise ValueError(f"Supply at most {MAX_REMARKS} remark strings; [] clears remarks")
    values = {k: _commercial_value(k, v) for k, v in updates.items()}
    if remarks is not None:
        remarks = [_commercial_value("DataSource", v) for v in remarks]
    root, block = _commercial_blocks(text)
    lines = text.splitlines(keepends=True)
    span, previous_remarks = _remarks(lines, block)
    if any(len(block.fields.get(k, [])) > 1 for k in FORM):
        raise ValueError("Ambiguous duplicate commercial fields")
    ending = "\r\n" if lines[block.line - 1].endswith("\r\n") else "\n"
    indent = re.match(r"[ \t]*", lines[block.line - 1])[0] + "  "
    additions = []
    for key, value in values.items():
        if existing := block.fields.get(key, []):
            index, old = existing[0]
            if old != value:
                lines[index] = replace_value(lines[index], value)
        else:
            additions.append(f"{indent}{key}={value}{ending}")
        if kind == "PAN" and key in ("Width", "Height"):
            if float(value) <= 0:
                raise ValueError("PAN Width and Height must be positive")
            target = "LargApp" if key == "Width" else "LongApp"
            if target in root.fields:
                index, old = root.one(target)
                if float(old) != float(value):
                    lines[index] = replace_value(lines[index], value)
    if values.keys() & {"Manufacturer", "Model"} and "Comment" in root.fields:
        index, comment = root.one("Comment")
        parts = comment.split("\t")
        if len(parts) >= 2:
            parts[0] = values.get("Manufacturer", parts[0])
            parts[1] = values.get("Model", parts[1])
            lines[index] = replace_value(lines[index], "\t".join(parts))
    if additions:
        lines[block.end_line - 1:block.end_line - 1] = additions
    if remarks is not None and remarks != previous_remarks:
        replacement = ([f"{indent}Remarks, Count={len(remarks)}{ending}",
                        *(f"{indent}  Str_{i}={v}{ending}" for i, v in enumerate(remarks, 1)),
                        f"{indent}End of Remarks={remarks[-1]}{ending}"] if remarks else [])
        if span:
            lines[span[0]:span[1] + 1] = replacement
        else:
            lines[block.end_line - 1:block.end_line - 1] = replacement
    return "".join(lines)


# ---------------------------------------------------------------------------
# Sourced component synthesis / template derivation
# ---------------------------------------------------------------------------

def _build_zero_template_candidate(component_type, *, manufacturer, model, values, source_note, chemistry="LFP"):
    """Synthesize a complete PVsyst 8.1.7 PAN, OND, or BTR directly from cited datasheet values."""
    conversions = []
    parsed: dict[str, tuple[float, str]] = {}
    for key, entries in values.items():
        entry, = entries
        val, unit = float(entry["value"]), entry["unit"]
        if key in DIMENSION_UNITS and unit == "mm":
            val /= 1000.0
            conversions.append({"field": key, "from": "mm", "to": "m", "factor": 0.001})
            unit = "m"
        elif key in ("BifacialityFactor", "EfficI", "SOHEOL") and unit == "%":
            val /= 100.0
            conversions.append({"field": key, "from": "%", "to": "ratio", "factor": 0.01})
            unit = "ratio"
        elif key == "ResInterne" and unit == "Ohm":
            val *= 1000.0
            conversions.append({"field": key, "from": "Ohm", "to": "mOhm", "factor": 1000.0})
            unit = "mOhm"
        parsed[key] = (val, unit)

    def req(key: str) -> float:
        if key not in parsed:
            raise ValueError(f"Zero-template {component_type} synthesis requires declared field: {key}")
        return parsed[key][0]

    opt = lambda key, default=None: parsed[key][0] if key in parsed else default
    opt_int = lambda key, default=None: int(round(parsed[key][0])) if key in parsed else default

    if component_type == "BTR":
        text, meta = synthesize_btr_from_datasheet(
            manufacturer=manufacturer, model=model, chemistry=chemistry,
            capacity_ah=opt("CapNomC10"), energy_kwh=opt("EnergyKWh"), nominal_voltage_v=opt("VNom"),
            n_cells_s=opt_int("NCellSerie"), n_cells_p=opt_int("NCellParall", 1),
            internal_resistance_mohm=opt("ResInterne"), coulombic_efficiency=opt("EfficI"),
            max_charge_current_a=opt("IMaxCharge"), max_discharge_current_a=opt("IMaxDischarge"),
            cycle_life_80dod=opt_int("CycleLife80DOD"), soh_eol=opt("SOHEOL", 0.80),
            lifetime_years_20c=opt("DureeVie20"), width_m=opt("Width", 0.60), height_m=opt("Height", 0.25),
            depth_m=opt("Depth", 0.55), weight_kg=opt("Weight", 85.0), source_note=source_note,
        )
    elif component_type == "PAN":
        isc, voc = req("ISC"), req("Voc")
        def _temp_coeff(key, target_unit, scale_pct, ref_val):
            if key not in parsed:
                return None
            val, unit = parsed[key]
            if unit == "pct_per_degC":
                conversions.append({"field": key, "from": unit, "to": target_unit, "factor": ref_val * 10.0})
                return ref_val * (val / 100.0) * 1000.0
            if unit in scale_pct:
                conversions.append({"field": key, "from": unit, "to": target_unit, "factor": 1000.0})
                return val * 1000.0
            return val

        text, meta = synthesize_pan_from_datasheet(
            manufacturer=manufacturer, model=model, pnom_w=req("PNom"), isc_a=isc, voc_v=voc,
            imp_a=req("Imp"), vmp_v=req("Vmp"), width_m=req("Width"), height_m=req("Height"),
            depth_m=opt("Depth", 0.035), weight_kg=opt("Weight", 28.0),
            n_cells_s=opt_int("NCelS", 72), n_cells_p=opt_int("NCelP", 2), n_diode=opt_int("NDiode", 3),
            mu_isc_ma_per_c=_temp_coeff("MuISC", "mA/degC", ("A_per_degC",), isc),
            mu_voc_mv_per_c=_temp_coeff("muVocSpec", "mV/degC", ("V_per_degC",), voc),
            mu_pmp_pct_per_c=opt("muPmpReq", -0.32), bifaciality_factor=opt("BifacialityFactor"),
            vmax_iec_v=opt("VMaxIEC", 1500.0), vmax_ul_v=opt("VMaxUL", 1500.0), source_note=source_note,
        )
    else:
        tri = lambda prefix: (tuple(parsed[f"{prefix}{i}"][0] for i in (1, 2, 3))
                              if all(f"{prefix}{i}" in parsed for i in (1, 2, 3)) else None)
        text, meta = synthesize_ond_from_datasheet(
            manufacturer=manufacturer, model=model, pnom_ac_kw=req("PNomConv"), pmax_ac_kw=opt("PMaxOUT"),
            vmpp_min_v=req("VMppMin"), vmpp_max_v=req("VMPPMax"), vabs_max_v=req("VAbsMax"),
            effic_max_pct=opt("EfficMax", 98.5), effic_euro_pct=opt("EfficEuro"), vout_ac_v=opt("VOutConv"),
            nb_mppt=opt_int("NbMPPT", 2), nb_inputs=opt_int("NbInputs"), idc_max_a=opt("IDCMax", 0.0),
            width_m=opt("Width", 0.70), height_m=opt("Height", 0.65), depth_m=opt("Depth", 0.35),
            weight_kg=opt("Weight", 75.0), three_voltage_voltages=tri("VNomEff"),
            three_voltage_effic_max=tri("EfficMaxV"), three_voltage_effic_euro=tri("EfficEuroV"),
            source_note=source_note,
        )
    return text, meta, conversions, {k: str(v[0]) for k, v in parsed.items()}


def _validate_candidate_bytes(store, component_type: str, candidate: str, *, bom: bool) -> tuple[bytes, dict, dict]:
    data = (b"\xef\xbb\xbf" if bom else b"") + candidate.encode("utf-8")
    validation = store._inspect(component_type, data)
    store._require_valid(validation, complete=True)
    from pvsyst_auto.components.store import electrical_inventory
    electrical = electrical_inventory(component_type, candidate)
    if electrical["status"] != "readable":
        raise ValueError("Candidate electrical fields are unreadable: " + "; ".join(electrical["issues"]))
    return data, validation, electrical


def _commit_sourced_component(
    store, component_type: str, new_name: str, data: bytes, manifest, result: dict, *,
    acknowledge_inherited_model: bool, ack_message: str, dry_run: bool, issue, before_commit=None,
) -> dict:
    if dry_run:
        return result
    if not acknowledge_inherited_model:
        issue("acknowledge_inherited_model", ack_message)
        result["status"] = "blocked"
        return result
    if before_commit is not None:
        before_commit()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    record = {k: v for k, v in result.items() if k not in ("diff", "stage", "component_created", "dry_run")}
    encoded_record = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False)
    result["stage"] = "creation"
    created = False
    try:
        with manifest.open("x", encoding="utf-8") as stream:
            created = True
            stream.write(encoded_record)
        store._new(component_type, new_name, data)
    except BaseException:
        if created:
            manifest.unlink(missing_ok=True)
        raise
    result.update(stage="created", component_created=True)
    return result


def build_sourced_component(store, *, component_type, new_name, manufacturer, model, sources, declarations,
                            template_name=None, chemistry="LFP", source_library="workspace",
                            dry_run=True, acknowledge_inherited_model=False):
    """Preview or create a new component from cited values (template-derived or zero-template)."""
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run, "issues": [], "warnings": [],
              "provenance": [], "component_created": False, "artifacts": {}, "model_refitted": False}

    def issue(field, message):
        result["issues"].append({"field": field, "message": str(message)})

    if not isinstance(component_type, str) or component_type not in SCALAR_UNITS:
        issue("component_type", "Use PAN, OND, or BTR")
    for name, value in (("manufacturer", manufacturer), ("model", model)):
        if not isinstance(value, str) or not value or value != value.strip() or not value.isprintable() or len(value.encode("utf-8")) > 80:
            issue(name, "Use 1..80 UTF-8 bytes of single-line printable text without surrounding spaces")
    if type(dry_run) is not bool or type(acknowledge_inherited_model) is not bool:
        issue("options", "Use strict booleans")
    if not isinstance(declarations, list) or not declarations:
        issue("declarations", "Supply at least one cited numeric field")
    if result["issues"]:
        return result

    fields = {k: (u, ("scalar", k)) for k, u in SCALAR_UNITS[component_type].items()}
    fields.update({k: (u, ("commercial", k)) for k, u in DIMENSION_UNITS.items()})
    from pvsyst_auto.workflows.sourced_design import bind_source_values
    binding = bind_source_values(manufacturer_model=f"{manufacturer} {model}", sources=sources,
                      declarations=declarations, field_specs=fields)
    result.update(issues=binding["issues"], warnings=binding["warnings"], provenance=binding["provenance"])
    if result["issues"]:
        result["allowed_fields"] = {k: list(v[0]) for k, v in sorted(fields.items())}
        return result

    with store.lock:
        try:
            target = store._path(component_type, new_name)
            manifest = workspace_file(store.workspace, "Results", new_name + ".sources.json", ".json")
            for path in (target, manifest):
                if path.exists() or (path.parent.exists() and any(m.name.casefold() == path.name.casefold() for m in path.parent.iterdir())):
                    raise FileExistsError("Choose new component and source-record names; already exists: " + path.name)
            source_ids = sorted({item["sourceID"] for item in result["provenance"]})

            if not template_name:
                source_note = ("Zero-template datasheet synthesis; cited " + ", ".join(source_ids))[:1000]
                candidate, synth_meta, conversions, requested = _build_zero_template_candidate(
                    component_type, manufacturer=manufacturer, model=model, values=binding["values"],
                    source_note=source_note, chemistry=chemistry,
                )
                bom = not candidate.isascii()
                data, validation, _ = _validate_candidate_bytes(store, component_type, candidate, bom=bom)
                diff = "".join(difflib.unified_diff([], candidate.splitlines(keepends=True), fromfile="<zero-template>", tofile=new_name))
                curves = battery_curve_inventory(candidate) if component_type == "BTR" else curve_inventory(component_type, candidate)
                result.update(
                    status="needs_review", stage="preview", model_refitted=(component_type == "PAN"),
                    synthesized=True, synthesized_metadata=synth_meta,
                    artifacts={"component": str(target), "source_record": str(manifest)},
                    template=None, requested_fields=requested, identity={"manufacturer": manufacturer, "model": model},
                    unit_conversions=conversions, utf8_bom=bom, acknowledgement=acknowledge_inherited_model,
                    inherited_fields={}, inherited_curves=curves,
                    validation={k: validation[k] for k in ("structural_errors", "warnings", "known_value_errors")},
                    diff=diff[:32000], diff_truncated=len(diff) > 32000,
                )
                return _commit_sourced_component(
                    store, component_type, new_name, data, manifest, result,
                    acknowledge_inherited_model=acknowledge_inherited_model,
                    ack_message="Preview the synthesized model and set acknowledge_inherited_model=true before creation",
                    dry_run=dry_run, issue=issue,
                )

            original_path = store._path(component_type, template_name, source_library)
            original = store._read(original_path)
            text = store._text(component_type, original)[1]
            if text is None:
                raise ValueError("This entry requires a complete UTF-8 text template, not legacy binary PAN")
            store._require_valid(store._inspect(component_type, original), complete=True)
            blocks = objects(text)
            root, = [b for b in blocks if not b.path]
            if len({b.path for b in blocks}) != len(blocks):
                raise ValueError("Repeated owning object path in template")
            commercial = [b for b in blocks if b.path == COMMERCIAL_PATH]
            if len(commercial) > 1:
                raise ValueError("Ambiguous commercial object")
            for key in ("Manufacturer", "Model", "DataSource"):
                for block in (root, *commercial):
                    if len(block.fields.get(key, [])) > 1:
                        raise ValueError("Repeated owning identity field: " + key)
                identities = [vals[0][1] for block in (root, *commercial) if (vals := block.fields.get(key))]
                if key != "DataSource" and len(set(identities)) > 1:
                    raise ValueError("Root and commercial identity disagree: " + key)

            source_note = ("User-defined from " + template_name + "; cited " + ", ".join(source_ids)
                           + "; inherited physics/curves, not refitted")[:1000]
            identity = {"Manufacturer": manufacturer, "Model": model, "DataSource": source_note}
            scalar, form, conversions = {}, {}, []
            for key, entries in binding["values"].items():
                entry, = entries
                value = entry["value"]
                if key in ("muVocSpec", "muPmpReq"):
                    if value >= 0:
                        raise ValueError(key + " must be negative")
                elif value <= 0:
                    raise ValueError(key + " must be positive")
                if key in ("EfficMax", "EfficEuro") and value > 100:
                    raise ValueError(key + " must be a percentage in (0,100]")
                if key in DIMENSION_UNITS:
                    if entry["unit"] == "mm":
                        value /= 1000
                        conversions.append({"field": key, "from": "mm", "to": "m", "factor": 0.001})
                    form[key] = str(value)
                else:
                    scalar[key] = str(value)
            candidate = store._replace_fields(component_type, text, scalar) if scalar else text
            if commercial and root.one("Version")[1] in NATIVE_TEXT:
                candidate = replace_commercial(component_type, candidate, {**form, **identity})
                lines = candidate.splitlines(keepends=True)
                edited_root, = [b for b in objects(candidate) if not b.path]
                for key, value in identity.items():
                    if key in edited_root.fields:
                        lines[edited_root.one(key)[0]] = replace_value(lines[edited_root.one(key)[0]], value)
                candidate = "".join(lines)
            else:
                if form:
                    raise ValueError(f"Dimension edits require the verified {TEXT_LABEL} commercial form; use an eligible template")
                candidate = store._replace_fields(component_type, candidate, identity)
                lines = candidate.splitlines(keepends=True)
                if "Comment" in root.fields:
                    index, comment = root.one("Comment")
                    if len(comment.split("\t")) >= 2:
                        lines[index] = replace_value(lines[index], f"{manufacturer}\t{model}\tUser-defined template model")
                candidate = "".join(lines)

            bom = original.startswith(b"\xef\xbb\xbf") or not candidate.isascii()
            data, validation, electrical = _validate_candidate_bytes(store, component_type, candidate, bom=bom)
            declared = electrical["fields"]
            if component_type == "PAN":
                for key in ("ISC", "Voc", "Imp", "Vmp"):
                    if declared[key]["status"] != "declared":
                        raise ValueError("Candidate has invalid reference-point field: " + key)
                if declared["Vmp"]["value"] >= declared["Voc"]["value"] or declared["Imp"]["value"] >= declared["ISC"]["value"]:
                    raise ValueError("Require Vmp < Voc and Imp < Isc at the reference point")
            else:
                for key in ("PNomConv", "PMaxOUT", "VMppMin", "VMPPMax", "VAbsMax"):
                    if declared[key]["status"] != "declared":
                        raise ValueError("Candidate has invalid converter field: " + key)
                if not declared["VMppMin"]["value"] < declared["VMPPMax"]["value"] <= declared["VAbsMax"]["value"]:
                    raise ValueError("Require VMppMin < VMPPMax <= VAbsMax")
                if declared["PMaxOUT"]["value"] < declared["PNomConv"]["value"]:
                    raise ValueError("Require PMaxOUT >= PNomConv")
                converter, = [b for b in objects(candidate) if b.path == "Converter"]
                if "EfficEuro" in converter.fields and float(converter.one("EfficEuro")[1]) > float(converter.one("EfficMax")[1]):
                    raise ValueError("Require EfficEuro <= EfficMax")

            edited_fields = {"Manufacturer", "Model", "DataSource", *scalar, *form}
            if component_type == "PAN":
                edited_fields.update(alias for key in scalar for alias in PAN_ALIASES.get(key, (key,)))
                edited_fields.update({"LargApp"} if "Width" in form else set())
                edited_fields.update({"LongApp"} if "Height" in form else set())
            candidate_blocks = {b.path: b for b in objects(candidate)}
            curves = battery_curve_inventory(text) if component_type == "BTR" else curve_inventory(component_type, text)
            result.update(
                status="needs_review", stage="preview",
                artifacts={"component": str(target), "source_record": str(manifest)},
                template={"name": template_name, "library": source_library, "version": root.one("Version")[1]},
                requested_fields={**scalar, **form}, identity={"manufacturer": manufacturer, "model": model},
                unit_conversions=conversions, utf8_bom=bom, acknowledgement=acknowledge_inherited_model,
                inherited_fields={
                    b.path or "root": {k: [v[:256] for _, v in entries] for k, entries in b.fields.items()
                                       if k not in edited_fields
                                       and [v for _, v in entries] == [v for _, v in candidate_blocks[b.path].fields.get(k, [])]}
                    for b in blocks
                },
                inherited_curves=curves,
                validation={k: validation[k] for k in ("structural_errors", "warnings", "known_value_errors")},
            )
            diff = "".join(difflib.unified_diff(text.splitlines(keepends=True), candidate.splitlines(keepends=True),
                                                fromfile=template_name, tofile=new_name))
            result.update(diff=diff[:32000], diff_truncated=len(diff) > 32000)

            def _ensure_unchanged():
                if store._read(original_path) != original:
                    raise ValueError("Template changed while preparing the component")

            return _commit_sourced_component(
                store, component_type, new_name, data, manifest, result,
                acknowledge_inherited_model=acknowledge_inherited_model,
                ack_message="Preview the inherited model and explicitly acknowledge it before creation",
                dry_run=dry_run, issue=issue, before_commit=_ensure_unchanged,
            )
        except (ValueError, OSError, TypeError) as exc:
            issue("component", exc)
            result["status"] = "failed" if result["stage"] == "creation" else "blocked"
            if result["status"] == "failed":
                result["warnings"].append({"field": "recovery", "message":
                    "Creation failed; no source record from this attempt is retained and no existing file "
                    "was overwritten. Inspect the reported paths before retrying."})
    return result

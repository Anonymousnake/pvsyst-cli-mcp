"""Derive a PAN or OND file from a template using cited datasheet values."""
from __future__ import annotations

import difflib
import json
from pvsyst_auto.components.commercial import replace_commercial
from pvsyst_auto.components.editor import objects, replace_value
from pvsyst_auto.components.single_diode import (
    synthesize_btr_from_datasheet,
    synthesize_ond_from_datasheet,
    synthesize_pan_from_datasheet,
)
from pvsyst_auto.components.store import electrical_inventory, inspect_curves
from pvsyst_auto.components.pan import ALIASES as PAN_ALIASES
from pvsyst_auto.project.paths import workspace_file
from pvsyst_auto.workflows.sourced_design import bind_source_values
from pvsyst_auto.versions import NATIVE_TEXT, TEXT_LABEL

# Only fields with verified units and an existing editor or synthesizer are admitted.
SCALAR_UNITS = {
    "PAN": {
        "PNom": ("W",),
        "ISC": ("A",),
        "Voc": ("V",),
        "Imp": ("A",),
        "Vmp": ("V",),
        "NCelS": ("count", "cells"),
        "NCelP": ("count", "strings"),
        "NDiode": ("count",),
        "BifacialityFactor": ("ratio", "%"),
        "VMaxIEC": ("V",),
        "VMaxUL": ("V",),
        "MuISC": ("mA/degC", "mA/C", "A_per_degC", "pct_per_degC"),
        "muVocSpec": ("mV/degC", "mV/C", "V_per_degC", "pct_per_degC"),
        "muPmpReq": ("pct_per_degC",),
    },
    "OND": {
        "PNomConv": ("kW",),
        "PMaxOUT": ("kW",),
        "VMppMin": ("V",),
        "VMPPMax": ("V",),
        "VAbsMax": ("V",),
        "EfficMax": ("%",),
        "EfficEuro": ("%",),
        "VOutConv": ("V",),
        "NbMPPT": ("count",),
        "NbInputs": ("count",),
        "IDCMax": ("A",),
        "VNomEff1": ("V",),
        "VNomEff2": ("V",),
        "VNomEff3": ("V",),
        "EfficMaxV1": ("%",),
        "EfficMaxV2": ("%",),
        "EfficMaxV3": ("%",),
        "EfficEuroV1": ("%",),
        "EfficEuroV2": ("%",),
        "EfficEuroV3": ("%",),
    },
    "BTR": {
        "CapNomC10": ("Ah",),
        "EnergyKWh": ("kWh",),
        "VNom": ("V",),
        "NCellSerie": ("count", "cells"),
        "NCellParall": ("count", "strings"),
        "ResInterne": ("mOhm", "Ohm"),
        "EfficI": ("ratio", "%"),
        "IMaxCharge": ("A",),
        "IMaxDischarge": ("A",),
        "CycleLife80DOD": ("count", "cycles"),
        "SOHEOL": ("ratio", "%"),
        "DureeVie20": ("years", "yr"),
    },
}
DIMENSION_UNITS = {"Width": ("m", "mm"), "Height": ("m", "mm"),
                   "Depth": ("m", "mm"), "Weight": ("kg",)}


def _build_zero_template_candidate(component_type, *, manufacturer, model, values, source_note, chemistry="LFP"):
    """Synthesize a complete PVsyst 8.1.7 PAN, OND, or BTR directly from cited datasheet values."""
    conversions = []
    parsed: dict[str, tuple[float, str]] = {}
    for key, entries in values.items():
        entry, = entries
        val = float(entry["value"])
        unit = entry["unit"]
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

    def req(key):
        if key not in parsed:
            raise ValueError(f"Zero-template {component_type} synthesis requires declared field: {key}")
        return parsed[key][0]

    if component_type == "BTR":
        text, meta = synthesize_btr_from_datasheet(
            manufacturer=manufacturer,
            model=model,
            chemistry=chemistry,
            capacity_ah=parsed["CapNomC10"][0] if "CapNomC10" in parsed else None,
            energy_kwh=parsed["EnergyKWh"][0] if "EnergyKWh" in parsed else None,
            nominal_voltage_v=parsed["VNom"][0] if "VNom" in parsed else None,
            n_cells_s=int(round(parsed["NCellSerie"][0])) if "NCellSerie" in parsed else None,
            n_cells_p=int(round(parsed["NCellParall"][0])) if "NCellParall" in parsed else 1,
            internal_resistance_mohm=parsed["ResInterne"][0] if "ResInterne" in parsed else None,
            coulombic_efficiency=parsed["EfficI"][0] if "EfficI" in parsed else None,
            max_charge_current_a=parsed["IMaxCharge"][0] if "IMaxCharge" in parsed else None,
            max_discharge_current_a=parsed["IMaxDischarge"][0] if "IMaxDischarge" in parsed else None,
            cycle_life_80dod=int(round(parsed["CycleLife80DOD"][0])) if "CycleLife80DOD" in parsed else None,
            soh_eol=parsed["SOHEOL"][0] if "SOHEOL" in parsed else 0.80,
            lifetime_years_20c=parsed["DureeVie20"][0] if "DureeVie20" in parsed else None,
            width_m=parsed["Width"][0] if "Width" in parsed else 0.60,
            height_m=parsed["Height"][0] if "Height" in parsed else 0.25,
            depth_m=parsed["Depth"][0] if "Depth" in parsed else 0.55,
            weight_kg=parsed["Weight"][0] if "Weight" in parsed else 85.0,
            source_note=source_note,
        )
        return text, meta, conversions, {k: str(v[0]) for k, v in parsed.items()}

    if component_type == "PAN":
        isc = req("ISC")
        voc = req("Voc")
        mu_isc = None
        if "MuISC" in parsed:
            val, unit = parsed["MuISC"]
            if unit == "pct_per_degC":
                mu_isc = isc * (val / 100.0) * 1000.0
                conversions.append({"field": "MuISC", "from": unit, "to": "mA/degC", "factor": isc * 10.0})
            elif unit == "A_per_degC":
                mu_isc = val * 1000.0
                conversions.append({"field": "MuISC", "from": unit, "to": "mA/degC", "factor": 1000.0})
            else:
                mu_isc = val
        mu_voc = None
        if "muVocSpec" in parsed:
            val, unit = parsed["muVocSpec"]
            if unit == "pct_per_degC":
                mu_voc = voc * (val / 100.0) * 1000.0
                conversions.append({"field": "muVocSpec", "from": unit, "to": "mV/degC", "factor": voc * 10.0})
            elif unit == "V_per_degC":
                mu_voc = val * 1000.0
                conversions.append({"field": "muVocSpec", "from": unit, "to": "mV/degC", "factor": 1000.0})
            else:
                mu_voc = val
        mu_pmp = parsed["muPmpReq"][0] if "muPmpReq" in parsed else -0.32
        text, meta = synthesize_pan_from_datasheet(
            manufacturer=manufacturer,
            model=model,
            pnom_w=req("PNom"),
            isc_a=isc,
            voc_v=voc,
            imp_a=req("Imp"),
            vmp_v=req("Vmp"),
            width_m=req("Width"),
            height_m=req("Height"),
            depth_m=parsed["Depth"][0] if "Depth" in parsed else 0.035,
            weight_kg=parsed["Weight"][0] if "Weight" in parsed else 28.0,
            n_cells_s=int(round(parsed["NCelS"][0])) if "NCelS" in parsed else 72,
            n_cells_p=int(round(parsed["NCelP"][0])) if "NCelP" in parsed else 2,
            n_diode=int(round(parsed["NDiode"][0])) if "NDiode" in parsed else 3,
            mu_isc_ma_per_c=mu_isc,
            mu_voc_mv_per_c=mu_voc,
            mu_pmp_pct_per_c=mu_pmp,
            bifaciality_factor=parsed["BifacialityFactor"][0] if "BifacialityFactor" in parsed else None,
            vmax_iec_v=parsed["VMaxIEC"][0] if "VMaxIEC" in parsed else 1500.0,
            vmax_ul_v=parsed["VMaxUL"][0] if "VMaxUL" in parsed else 1500.0,
            source_note=source_note,
        )
        return text, meta, conversions, {k: str(v[0]) for k, v in parsed.items()}

    v_tri = (
        (parsed["VNomEff1"][0], parsed["VNomEff2"][0], parsed["VNomEff3"][0])
        if all(k in parsed for k in ("VNomEff1", "VNomEff2", "VNomEff3"))
        else None
    )
    emax_tri = (
        (parsed["EfficMaxV1"][0], parsed["EfficMaxV2"][0], parsed["EfficMaxV3"][0])
        if all(k in parsed for k in ("EfficMaxV1", "EfficMaxV2", "EfficMaxV3"))
        else None
    )
    eeuro_tri = (
        (parsed["EfficEuroV1"][0], parsed["EfficEuroV2"][0], parsed["EfficEuroV3"][0])
        if all(k in parsed for k in ("EfficEuroV1", "EfficEuroV2", "EfficEuroV3"))
        else None
    )

    text, meta = synthesize_ond_from_datasheet(
        manufacturer=manufacturer,
        model=model,
        pnom_ac_kw=req("PNomConv"),
        pmax_ac_kw=parsed["PMaxOUT"][0] if "PMaxOUT" in parsed else None,
        vmpp_min_v=req("VMppMin"),
        vmpp_max_v=req("VMPPMax"),
        vabs_max_v=req("VAbsMax"),
        effic_max_pct=parsed["EfficMax"][0] if "EfficMax" in parsed else 98.5,
        effic_euro_pct=parsed["EfficEuro"][0] if "EfficEuro" in parsed else None,
        vout_ac_v=parsed["VOutConv"][0] if "VOutConv" in parsed else None,
        nb_mppt=int(round(parsed["NbMPPT"][0])) if "NbMPPT" in parsed else 2,
        nb_inputs=int(round(parsed["NbInputs"][0])) if "NbInputs" in parsed else None,
        idc_max_a=parsed["IDCMax"][0] if "IDCMax" in parsed else 0.0,
        width_m=parsed["Width"][0] if "Width" in parsed else 0.70,
        height_m=parsed["Height"][0] if "Height" in parsed else 0.65,
        depth_m=parsed["Depth"][0] if "Depth" in parsed else 0.35,
        weight_kg=parsed["Weight"][0] if "Weight" in parsed else 75.0,
        three_voltage_voltages=v_tri,
        three_voltage_effic_max=emax_tri,
        three_voltage_effic_euro=eeuro_tri,
        source_note=source_note,
    )
    return text, meta, conversions, {k: str(v[0]) for k, v in parsed.items()}


def build_sourced_component(store, *, component_type, new_name,
                           manufacturer, model, sources, declarations,
                           template_name=None,
                           chemistry="LFP",
                           source_library="workspace", dry_run=True,
                           acknowledge_inherited_model=False):
    """Preview or create a new component from cited values (template-derived or zero-template)."""
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run,
              "issues": [], "warnings": [], "provenance": [],
              "component_created": False, "artifacts": {}, "model_refitted": False}
    def issue(field, message):
        result["issues"].append({"field": field, "message": str(message)})

    if not isinstance(component_type, str) or component_type not in SCALAR_UNITS:
        issue("component_type", "Use PAN, OND, or BTR")
    for name, value in (("manufacturer", manufacturer), ("model", model)):
        if (not isinstance(value, str) or not value or value != value.strip()
                or not value.isprintable() or len(value.encode("utf-8")) > 80):
            issue(name, "Use 1..80 UTF-8 bytes of single-line printable text without surrounding spaces")
    if type(dry_run) is not bool or type(acknowledge_inherited_model) is not bool:
        issue("options", "Use strict booleans")
    if not isinstance(declarations, list) or not declarations:
        issue("declarations", "Supply at least one cited numeric field")
    if result["issues"]:
        return result
    fields = {key: (units, ("scalar", key))
              for key, units in SCALAR_UNITS[component_type].items()}
    fields.update({key: (units, ("commercial", key)) for key, units in DIMENSION_UNITS.items()})
    binding = bind_source_values(manufacturer_model=manufacturer + " " + model,
                                 sources=sources, declarations=declarations, field_specs=fields)
    result.update(issues=binding["issues"], warnings=binding["warnings"],
                  provenance=binding["provenance"])
    if result["issues"]:
        result["allowed_fields"] = {key: list(spec[0]) for key, spec in sorted(fields.items())}
        return result
    with store.lock:
        try:
            target = store._path(component_type, new_name)
            manifest = workspace_file(store.workspace, "Results", new_name + ".sources.json", ".json")
            for path in (target, manifest):
                if path.exists() or (path.parent.exists() and any(
                        member.name.casefold() == path.name.casefold() for member in path.parent.iterdir())):
                    raise FileExistsError("Choose new component and source-record names; already exists: " + path.name)
            source_ids = sorted({item["sourceID"] for item in result["provenance"]})
            zero_template = template_name is None or template_name == ""
            if zero_template:
                source_note = ("Zero-template datasheet synthesis; cited " + ", ".join(source_ids))[:1000]
                candidate, synth_meta, conversions, requested = _build_zero_template_candidate(
                    component_type,
                    manufacturer=manufacturer,
                    model=model,
                    values=binding["values"],
                    source_note=source_note,
                    chemistry=chemistry,
                )
                bom = not candidate.isascii()
                data = (b"\xef\xbb\xbf" if bom else b"") + candidate.encode("utf-8")
                validation = store._inspect(component_type, data)
                store._require_valid(validation, complete=True)
                electrical = electrical_inventory(component_type, candidate)
                if electrical["status"] != "readable":
                    raise ValueError("Candidate electrical fields are unreadable: " + "; ".join(electrical["issues"]))
                diff = "".join(difflib.unified_diff([], candidate.splitlines(keepends=True),
                                                    fromfile="<zero-template>", tofile=new_name))
                result.update(
                    status="needs_review",
                    stage="preview",
                    model_refitted=(component_type == "PAN"),
                    synthesized=True,
                    synthesized_metadata=synth_meta,
                    artifacts={"component": str(target), "source_record": str(manifest)},
                    template=None,
                    requested_fields=requested,
                    identity={"manufacturer": manufacturer, "model": model},
                    unit_conversions=conversions,
                    utf8_bom=bom,
                    acknowledgement=acknowledge_inherited_model,
                    inherited_fields={},
                    inherited_curves=inspect_curves(component_type, candidate),
                    validation={key: validation[key] for key in
                                ("structural_errors", "warnings", "known_value_errors")},
                    diff=diff[:32000],
                    diff_truncated=len(diff) > 32000,
                )
                if dry_run:
                    return result
                if not acknowledge_inherited_model:
                    issue("acknowledge_inherited_model", "Preview the synthesized model and set acknowledge_inherited_model=true before creation")
                    result["status"] = "blocked"
                    return result
                manifest.parent.mkdir(parents=True, exist_ok=True)
                record = {key: value for key, value in result.items()
                          if key not in ("diff", "stage", "component_created", "dry_run")}
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

            original_path = store._path(component_type, template_name, source_library)
            original = store._read(original_path)
            text = store._text(component_type, original)[1]
            if text is None:
                raise ValueError("This entry requires a complete UTF-8 text template, not legacy binary PAN")
            store._require_valid(store._inspect(component_type, original), complete=True)
            blocks = objects(text)
            root, = [block for block in blocks if not block.path]
            if len({block.path for block in blocks}) != len(blocks):
                raise ValueError("Repeated owning object path in template")
            commercial = [block for block in blocks if block.path == "PVObject_Commercial"]
            if len(commercial) > 1:
                raise ValueError("Ambiguous commercial object")
            for key in ("Manufacturer", "Model", "DataSource"):
                for block in (root, *commercial):
                    if len(block.fields.get(key, [])) > 1:
                        raise ValueError("Repeated owning identity field: " + key)
                identities = [values[0][1] for block in (root, *commercial)
                              if (values := block.fields.get(key))]
                if key != "DataSource" and len(set(identities)) > 1:
                    raise ValueError("Root and commercial identity disagree: " + key)
            source_ids = sorted({item["sourceID"] for item in result["provenance"]})
            source_note = ("User-defined from " + template_name + "; cited " + ", ".join(source_ids)
                           + "; inherited physics/curves, not refitted")[:1000]
            identity = {"Manufacturer": manufacturer, "Model": model, "DataSource": source_note}
            scalar, form = {}, {}
            conversions = []
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
            modern_form = bool(commercial) and root.one("Version")[1] in NATIVE_TEXT
            if modern_form:
                candidate = replace_commercial(component_type, candidate, {**form, **identity})
                # A duplicated root identity is synchronized too, never left stale.
                lines = candidate.splitlines(keepends=True)
                edited_root, = [block for block in objects(candidate) if not block.path]
                for key, value in identity.items():
                    if key in edited_root.fields:
                        index, _ = edited_root.one(key)
                        lines[index] = replace_value(lines[index], value)
                candidate = "".join(lines)
            else:
                if form:
                    raise ValueError(f"Dimension edits require the verified {TEXT_LABEL} commercial form; use an eligible template")
                candidate = store._replace_fields(component_type, candidate, identity)
                lines = candidate.splitlines(keepends=True)
                if "Comment" in root.fields:
                    index, comment = root.one("Comment")
                    if len(comment.split("\t")) >= 2:
                        lines[index] = replace_value(lines[index], manufacturer + "\t" + model + "\tUser-defined template model")
                candidate = "".join(lines)
            bom = original.startswith(b"\xef\xbb\xbf") or not candidate.isascii()
            data = (b"\xef\xbb\xbf" if bom else b"") + candidate.encode("utf-8")
            validation = store._inspect(component_type, data)
            store._require_valid(validation, complete=True)
            electrical = electrical_inventory(component_type, candidate)
            if electrical["status"] != "readable":
                # A flattened-complete template can still lack its owning electrical object.
                raise ValueError("Candidate electrical fields are unreadable: " + "; ".join(electrical["issues"]))
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
                converter, = [block for block in objects(candidate) if block.path == "Converter"]
                if "EfficEuro" in converter.fields:
                    if float(converter.one("EfficEuro")[1]) > float(converter.one("EfficMax")[1]):
                        raise ValueError("Require EfficEuro <= EfficMax")
            edited_fields = {"Manufacturer", "Model", "DataSource", *scalar, *form}
            if component_type == "PAN":
                edited_fields.update(alias for key in scalar for alias in PAN_ALIASES.get(key, (key,)))
                edited_fields.update({"LargApp"} if "Width" in form else set())
                edited_fields.update({"LongApp"} if "Height" in form else set())
            candidate_blocks = {block.path: block for block in objects(candidate)}
            result.update(status="needs_review", stage="preview", artifacts={"component": str(target),
                          "source_record": str(manifest)}, template={"name": template_name, "library": source_library,
                          "version": root.one("Version")[1]}, requested_fields={**scalar, **form},
                          identity={"manufacturer": manufacturer, "model": model}, unit_conversions=conversions,
                          utf8_bom=bom, acknowledgement=acknowledge_inherited_model,
                          inherited_fields={block.path or "root": {key: [value[:256] for _, value in entries]
                              for key, entries in block.fields.items()
                              if key not in edited_fields
                              and [value for _, value in entries] == [value for _, value in
                                  candidate_blocks[block.path].fields.get(key, [])]}
                              for block in blocks}, inherited_curves=inspect_curves(component_type, text),
                          validation={key: validation[key] for key in
                              ("structural_errors", "warnings", "known_value_errors")})
            diff = "".join(difflib.unified_diff(text.splitlines(keepends=True), candidate.splitlines(keepends=True),
                                              fromfile=template_name, tofile=new_name))
            result["diff"] = diff[:32000]
            result["diff_truncated"] = len(diff) > 32000
            if dry_run:
                return result
            if not acknowledge_inherited_model:
                issue("acknowledge_inherited_model", "Preview the inherited model and explicitly acknowledge it before creation")
                result["status"] = "blocked"
                return result
            if store._read(original_path) != original:
                raise ValueError("Template changed while preparing the component")
            # Acquire the single store lock for this source record.
            manifest.parent.mkdir(parents=True, exist_ok=True)
            record = {key: value for key, value in result.items()
                      if key not in ("diff", "stage", "component_created", "dry_run")}
            encoded_record = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False)
            result["stage"] = "creation"
            created = False
            try:
                with manifest.open("x", encoding="utf-8") as stream:
                    created = True
                    stream.write(encoded_record)
                store._new(component_type, new_name, data)
            except BaseException:
                # The record describes this attempt only; a failed create must not block a retry.
                # Only remove a record this call opened, never a pre-existing one.
                if created:
                    manifest.unlink(missing_ok=True)
                raise
            result.update(stage="created", component_created=True)
        except (ValueError, OSError, TypeError) as exc:
            issue("component", exc)
            result["status"] = "failed" if result["stage"] == "creation" else "blocked"
            if result["status"] == "failed":
                result["warnings"].append({"field": "recovery", "message":
                    "Creation failed; no source record from this attempt is retained and no existing file "
                    "was overwritten. Inspect the reported paths before retrying."})
    return result

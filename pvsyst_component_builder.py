"""Cited scalar edits on complete templates, not a datasheet-to-physics fitter."""
from __future__ import annotations

import difflib
import json
from pvsyst_commercial import replace_commercial
from pvsyst_component_editor import objects, replace_value
from pvsyst_components import electrical_inventory, inspect_curves
from pvsyst_pan import ALIASES as PAN_ALIASES
from pvsyst_paths import workspace_file
from pvsyst_source_inputs import bind_source_values

# Only fields with verified units and an existing editor are admitted.
SCALAR_UNITS = {
    "PAN": {"PNom": "W", "ISC": "A", "Voc": "V", "Imp": "A", "Vmp": "V"},
    "OND": {"PNomConv": "kW", "PMaxOUT": "kW", "VMppMin": "V", "VMPPMax": "V",
            "VAbsMax": "V", "EfficMax": "%", "EfficEuro": "%"},
}
DIMENSION_UNITS = {"Width": ("m", "mm"), "Height": ("m", "mm"),
                   "Depth": ("m", "mm"), "Weight": ("kg",)}
SCOPE = ("User-defined template-derived component. Numeric citations are checked against "
         "caller-declared source text, not authenticated manufacturer documents. Unspecified "
         "model parameters, hardware declarations and curves stay inherited. No diode-model "
         "refitting, automatic efficiency profile generation, temperature coefficient conversion, "
         "hardware sizing or physical certification is performed. A referencing native simulation "
         "and independent engineering review remain necessary.")


def build_sourced_component(store, *, component_type, template_name, new_name,
                           manufacturer, model, sources, declarations,
                           source_library="workspace", dry_run=True,
                           acknowledge_inherited_model=False):
    """Preview or create one NEW component plus a source/assumption record."""
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run,
              "issues": [], "warnings": [], "provenance": [], "scope": SCOPE,
              "component_created": False, "artifacts": {}, "model_refitted": False}
    def issue(field, message):
        result["issues"].append({"field": field, "message": str(message)})

    if not isinstance(component_type, str) or component_type not in SCALAR_UNITS:
        issue("component_type", "Use PAN or OND")
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
    fields = {key: ((unit,), ("scalar", key))
              for key, unit in SCALAR_UNITS[component_type].items()}
    fields.update({key: (units, ("commercial", key)) for key, units in DIMENSION_UNITS.items()})
    binding = bind_source_values(manufacturer_model=manufacturer + " " + model,
                                 sources=sources, declarations=declarations, field_specs=fields)
    result.update(issues=binding["issues"], warnings=binding["warnings"],
                  provenance=binding["provenance"])
    if result["issues"]:
        return result
    with store.lock:
        try:
            original_path = store._path(component_type, template_name, source_library)
            target = store._path(component_type, new_name)
            manifest = workspace_file(store.workspace, "Results", new_name + ".sources.json", ".json")
            for path in (target, manifest):
                if path.exists() or (path.parent.exists() and any(
                        member.name.casefold() == path.name.casefold() for member in path.parent.iterdir())):
                    raise FileExistsError("Choose new component and source-record names; already exists: " + path.name)
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
                if value <= 0:
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
            modern_form = bool(commercial) and root.one("Version")[1] == "8.1.6"
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
                    raise ValueError("Dimension edits require the verified 8.1.6 commercial form; use an eligible template")
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
            declared = electrical_inventory(component_type, candidate)["fields"]
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
            result["warnings"].append({"field": "inherited_model", "message": SCOPE})
            if dry_run:
                return result
            if not acknowledge_inherited_model:
                issue("acknowledge_inherited_model", "Preview the inherited model and explicitly acknowledge it before creation")
                result["status"] = "blocked"
                return result
            if store._read(original_path) != original:
                raise ValueError("Template changed while preparing the component")
            # ponytail: one existing store lock and one small source record, no job framework.
            manifest.parent.mkdir(parents=True, exist_ok=True)
            record = {key: value for key, value in result.items()
                      if key not in ("diff", "stage", "component_created", "dry_run")}
            record["scope"] += " This record describes the candidate, not proof that its file was created or simulated."
            encoded_record = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False)
            result["stage"] = "creation"
            with manifest.open("x", encoding="utf-8") as stream:
                stream.write(encoded_record)
            store._new(component_type, new_name, data)
            result.update(stage="created", component_created=True)
        except (ValueError, OSError, TypeError) as exc:
            issue("component", exc)
            result["status"] = "failed" if result["stage"] == "creation" else "blocked"
            if result["status"] == "failed":
                result["warnings"].append({"field": "recovery", "message":
                    "Creation failed; retained candidate/source artifacts are disclosed, not silently removed. "
                    "Inspect the reported paths before retrying with new names."})
    return result

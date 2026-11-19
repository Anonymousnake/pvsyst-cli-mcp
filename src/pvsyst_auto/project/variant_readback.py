"""Read-only projection helpers for PVsyst variants, saved caches, economics, and legacy 8.0 DCGrid."""
from __future__ import annotations

import hashlib
import math
import re

from pvsyst_auto.components.editor import objects
from pvsyst_auto.components.generator import state as generator_state
from pvsyst_auto.components.store import FIELD
from pvsyst_auto.energy.ac import ac_cabling_state
from pvsyst_auto.energy.ageing import ageing_model_state
from pvsyst_auto.energy.auxiliaries import auxiliary_state
from pvsyst_auto.energy.iam import iam_model_state
from pvsyst_auto.energy.inverter_temperature import inverter_temperature_state
from pvsyst_auto.energy.mismatch import string_mismatch_state
from pvsyst_auto.energy.peak import peak_shaving_state
from pvsyst_auto.energy.power import power_controls_state
from pvsyst_auto.energy.power_factor import power_factor_state
from pvsyst_auto.energy.soiling import soiling_state
from pvsyst_auto.energy.storage import grid_storage_state
from pvsyst_auto.energy.unavailability import system_unavail_state
from pvsyst_auto.energy.dc import dc_cabling_state
from pvsyst_auto.native.grid_limit import grid_limit_state
from pvsyst_auto.native.headless import _PLOT_SAMPLES, _result_histograms, _simul_plots
from pvsyst_auto.native.horizon import horizon_state
from pvsyst_auto.native.p50 import p50_state
from pvsyst_auto.project.bifacial import bifacial_state
from pvsyst_auto.project.circuit import topology_state, transformer_state
from pvsyst_auto.project.sheds import TYPES as SHED_TYPES, WRITABLE as SHED_FIELDS
from pvsyst_auto.project.tracking import WRITABLE as TRACKING_FIELDS
from pvsyst_auto.scene.state import shading_state

VAR_LIST_HEADER = re.compile(r"^\s*VarList,\s*list of=([0-9]+)\s+TVarSimul\s*$")
VAR_SIMUL_OPEN = re.compile(r"^\s*VarSimul_([0-9]+)=(.*?)\s*$")
VAR_SIMUL_CLOSE = re.compile(r"^\s*End of VarSimul=(.*?)\s*$")


def _declared_numbers(text: str) -> list[float] | None:
    """Return comma-separated finite numbers declared by a field, or None."""
    values = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.append(float(item))
        except ValueError:
            return None
    return values or None


def saved_results(text: str, lines: list[str]) -> dict:
    """Read recognized saved-result records and the separate output-file configuration."""
    issues = []
    try:
        blocks = objects(text, include_private=True)
    except ValueError as exc:
        blocks = []
        issues.append("native result-cache objects unreadable: %s" % exc)
    root = next((block for block in blocks if not block.path), None)
    root_flags = root.fields["Flags"][0][1] if root and len(root.fields.get("Flags", [])) == 1 else None
    if root_flags is None:
        issues.append("root Flags missing or duplicated")
    simul_block = next((block for block in blocks if block.object_type == "pvSimulFile"), None)
    pv_simul_file = None
    if simul_block is not None:
        units_raw = simul_block.fields.get("Units", [[None, ""]])[0][1]
        idents_raw = simul_block.fields.get("VarIdentWr", [[None, ""]])[0][1]
        pv_simul_file = {
            "file_name_written": simul_block.fields.get("FileNameWr", [[None, None]])[0][1],
            "var_idents_written": [item.strip() for item in idents_raw.split(",") if item.strip()],
            "native_unit_codes": [item.strip() for item in units_raw.split(",") if item.strip()],
            "type_val_written": simul_block.fields.get("TypeValW", [[None, None]])[0][1],
        }

    var_list_start = next((i for i, line in enumerate(lines) if VAR_LIST_HEADER.match(line)), None)
    declared_count = None
    var_list_closed = False
    entries = []
    if var_list_start is not None:
        declared_count = int(VAR_LIST_HEADER.match(lines[var_list_start])[1])
        var_list_end = next((i for i in range(var_list_start + 1, len(lines))
                             if lines[i].strip() == "End of the List VarList"), None)
        if var_list_end is None:
            issues.append("VarList missing 'End of the List VarList'")
            end_scan = len(lines)
        else:
            var_list_closed = True
            end_scan = var_list_end
        i = var_list_start + 1
        while i < end_scan:
            match = VAR_SIMUL_OPEN.match(lines[i])
            if not match:
                i += 1
                continue
            index = int(match[1])
            ident = match[2]
            close_line = None
            fields = {}
            day_lines = []
            j = i + 1
            while j < end_scan:
                stripped = lines[j].strip()
                close_match = VAR_SIMUL_CLOSE.match(stripped)
                if close_match:
                    close_line = j
                    break
                if VAR_SIMUL_OPEN.match(lines[j]):
                    break
                field = FIELD.fullmatch(stripped)
                if field:
                    key, val = field[1], field[2]
                    if key.startswith("Day_"):
                        day_lines.append((key, val))
                    else:
                        fields.setdefault(key, []).append(val)
                j += 1
            if close_line is None:
                issues.append("VarSimul_%d=%s missing closing tag" % (index, ident))
                i = j
                continue
            if close_match[1] != ident:
                issues.append("VarSimul_%d=%s closed as %s" % (index, ident, close_match[1]))

            def one(key):
                vals = fields.get(key, [])
                return vals[0] if len(vals) == 1 else None

            raw_months = one("ValMonth")
            values = _declared_numbers(raw_months) if raw_months is not None else None
            stored_factor = None
            raw_factor = one("StoredWeighted")
            if raw_factor is not None:
                try:
                    stored_factor = float(raw_factor)
                except ValueError:
                    stored_factor = None
            entries.append({
                "index": index,
                "ident": ident,
                "native_ident": one("VarIdent"),
                "declared_type": one("VarType"),
                "accumulator": one("TypeAccum"),
                "mode_ss": one("ModeSS"),
                "date_beg": one("DateBeg"),
                "unit": None,
                "unit_source": "not_declared_in_varlist",
                "values": values,
                "values_scope": "month" if values is not None else None,
                "day_rows": len(day_lines),
                "stored_weighted_factor": stored_factor,
                "value_declaration": ("\n".join("%s=%s" % item for item in day_lines) if day_lines
                                      else (raw_months if values is None else None)),
            })
            i = close_line + 1

    if declared_count is not None and len(entries) != declared_count:
        issues.append("VarList declares %d TVarSimul but %d parsed" % (declared_count, len(entries)))
    parsed_indices = {entry["index"] for entry in entries}
    missing_indices = (sorted(set(range(1, declared_count + 1)) - parsed_indices)
                       if declared_count is not None else [])
    plots = []
    if root is not None:
        try:
            plots = list(_simul_plots(lines, root, blocks))
        except ValueError as exc:
            issues.append(str(exc))
    histograms = len(_result_histograms([root, *plots], blocks)) if root else 0
    plot_samples = any(_PLOT_SAMPLES & plot.fields.keys() for plot in plots)
    present = var_list_start is not None or histograms > 0 or plot_samples
    return {
        "present": present,
        "root_flags": root_flags,
        "pv_simul_file": pv_simul_file,
        "var_list": {
            "present": var_list_start is not None,
            "closed": var_list_closed,
            "declared_count": declared_count,
            "parsed_count": len(entries),
            "missing_indices": missing_indices,
            "entries": entries,
        },
        "histogram_count": histograms,
        "issues": issues,
    }


def economic_block(lines: list[str]) -> dict:
    """Read an optional root ``EconomicEval=TEcoEvaluation`` block."""
    starts = [i for i, line in enumerate(lines) if line.strip() == "EconomicEval=TEcoEvaluation"]
    if not starts:
        return {"present": False}
    if len(starts) != 1:
        raise ValueError("Multiple EconomicEval sections")
    terminator = "End of TEcoEvaluation"
    end = next((i for i in range(starts[0] + 1, len(lines)) if lines[i].strip() == terminator), None)
    if end is None:
        raise ValueError("Unclosed EconomicEval section")
    block = lines[starts[0] + 1:end]
    list_open = re.compile(r"^\s*([A-Za-z0-9_]+),\s*list of=([0-9]+)\s+([A-Za-z0-9_]+)\s*$")
    list_close = re.compile(r"^\s*End of List\b")
    opens = closes = 0
    lists = {}
    scalars = {}
    for line in block:
        stripped = line.strip()
        if list_close.match(stripped):
            closes += 1
            continue
        lst = list_open.match(stripped)
        if lst:
            opens += 1
            if opens == 1:
                lists[lst.group(1)] = {"type": lst.group(3), "declared_count": int(lst.group(2))}
            continue
        if opens == closes and (field := FIELD.fullmatch(stripped)):
            scalars.setdefault(field.group(1), []).append(field.group(2))
    if opens != closes:
        raise ValueError("EconomicEval list structure is unbalanced: %d 'list of=' but %d 'End of List'"
                         % (opens, closes))
    return {
        "present": True,
        "owner": "TEcoEvaluation",
        "terminator": terminator,
        "native_type": "TEcoEvaluation",
        "raw": "".join(block),
        "scalars": scalars,
        "lists": lists,
        "native_consumption_verified": False,
    }


def legacy_dcgrid_read_scope(store, lines: list[str], system) -> dict | None:
    """Read-only declarations for legacy 8.0 DCGrid variants."""
    if system is None:
        return None
    header_end = next((i for i in range(1, len(lines))
                       if lines[i].strip().startswith("PVObject_")), len(lines))
    root_versions = store._field_indices(lines, (0, header_end), "Version")
    kinds = store._field_indices(lines, system, "SystemType")
    if len(root_versions) != 1 or len(kinds) != 1:
        return None
    version = FIELD.fullmatch(lines[root_versions[0]].rstrip("\r\n"))[2]
    system_type = FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2]
    if not re.fullmatch(r"8\.0(?:\.[0-9]+)?", version) or system_type != "DCGrid":
        return None
    declared = {"SystemType": system_type}
    for name in ("TypeRegul", "VNomAlim", "VPVMax", "RGridPV", "RGridAlim"):
        indices = store._field_indices(lines, system, name)
        if len(indices) == 1:
            declared[name] = FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]
    return {
        "present": True,
        "readonly": True,
        "format_family": "DCGrid",
        "source_version": version,
        "system_declarations": declared,
    }


def inspect_parameters(store, project: str, variant: str,
                       include_saved_results: bool = False,
                       include_economics: bool = False,
                       include_topology: bool = False) -> dict:
    with store.lock:
        store._check_project(project)
        data = store._read(store._path(project, variant))
        text, lines = store._text(data)
        arrays, system = store._sections(lines)

        def fields(bounds, names):
            return {name: values[0] for name in names
                    if len(values := [FIELD.fullmatch(lines[i].rstrip("\r\n"))[2]
                                      for i in store._field_indices(lines, bounds, name)]) == 1}

        def single(bounds, name):
            indices = store._field_indices(lines, bounds, name)
            return FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2] if len(indices) == 1 else None

        def inverter_state(bounds):
            state = {}
            for name in ("PVModule", "GInverter"):
                if (val := single(bounds, name)) is not None:
                    state[name] = val
            for name in ("NInverter", "NInvMPPT", "PowerSharingInverterId"):
                val = single(bounds, name)
                if val is not None and re.fullmatch(r"-?[0-9]+", val):
                    state[name] = int(val)
            raw = single(bounds, "PNomAC_InMPPT")
            if raw is not None:
                try:
                    value = float(raw)
                except ValueError:
                    value = None
                if value is not None and math.isfinite(value):
                    state["PNomAC_InMPPT"] = value
            return state

        orientations = []
        for index, line in enumerate(lines):
            if line.strip() != "PVObject_=pvOrient":
                continue
            end = next((i for i in range(index + 1, len(lines))
                        if lines[i].strip() == "End of TOrientGroup"), None)
            if end is None:
                raise ValueError("Unclosed orientation")
            indices = store._field_indices(lines, (index, end), "NoOrient")
            if len(indices) != 1:
                raise ValueError("Orientation requires one ID")
            number = FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]
            if not number.isdigit():
                raise ValueError("Orientation ID must be numeric")
            store._orientation(lines, int(number))
            values = fields((index, end), ("FieldType", "FieldTilt", "FieldAzim", "SummerTilt",
                                           "WinterTilt", "WinterMonths", "UsesTiltAver", "UsesAzimAver", "UsesSlopeAver"))
            values.update(fields((index, end), TRACKING_FIELDS.get(values.get("FieldType"), ())))
            if values.get("FieldType") in SHED_TYPES:
                values.update(fields((index, end), SHED_FIELDS.values()))
            orient_entry = {"id": int(number), "values": values}
            bif = bifacial_state(lines, (index, end))
            if bif.get("enabled") or bif.get("issue"):
                orient_entry["bifacial"] = bif
            orientations.append(orient_entry)

        main_array = store._main_array(lines)
        names = store._display_name_fields(lines)
        result = {
            "project": project,
            "variant": variant.upper(),
            "variant_name": FIELD.fullmatch(lines[names[0]].rstrip("\r\n"))[2] if len(names) == 1 else None,
            "utf8_bom": data.startswith(b"\xef\xbb\xbf"),
            "main_array": {
                "values": fields(main_array, ("UFactor", "UVFactor", "Flags", "Flags2", "MVNbTransfos", "VOutTransfo")) if main_array is not None else {},
                "soiling": soiling_state(lines, main_array) if main_array is not None else None,
            },
            "sha256": hashlib.sha256(data).hexdigest(),
            "orientations": orientations,
            "system_parameters": fields(system, ("SystemType", "Flags", "TempShift", "TempFixed",
                                                 "TanPhi", "PLimGrid", "LimGrSeparLoss")) if system is not None else {},
            "generator": generator_state(lines, arrays, system),
            "grid_limit": grid_limit_state(lines, system),
            "auxiliaries": auxiliary_state(lines, system),
            "horizon": horizon_state(lines),
            "power_factor": power_factor_state(lines, system),
            "p50_p90": p50_state(lines),
            "power_controls": power_controls_state(lines, system),
            "ageing": ageing_model_state(lines, main_array) if main_array is not None else None,
            "inverter_temperature": inverter_temperature_state(lines, system),
            "transformers": transformer_state(lines),
            "ac_cabling": ac_cabling_state(lines),
            "dc_cabling": dc_cabling_state(lines),
            "system_unavailability": system_unavail_state(lines, system),
            "peak_shaving": peak_shaving_state(lines),
            "grid_storage": grid_storage_state(lines, system),
            "shading_scene": shading_state(lines),
            "source_metadata": {"stored_source_values": True},
            "subarrays": [
                {
                    "id": identifier,
                    "values": fields(bounds, (
                        "NModSerie", "NStringCh", "NStrOrient1", "NInverter", "NInvMPPT", "NoOrientation",
                        "VBkUpEncl_syst", "VBkUpDecl_syst", "LIDLoss", "PMismMPP", "PQualityModU", "PQualityModI", "PQualityModP", "Flags",
                    )),
                    "inverter_state": inverter_state(bounds),
                    "string_mismatch": string_mismatch_state(lines, bounds),
                    "iam": iam_model_state(lines, bounds),
                }
                for identifier, bounds in sorted(arrays.items())
            ],
        }
        legacy_scope = legacy_dcgrid_read_scope(store, lines, system)
        if legacy_scope is not None:
            result["legacy_read_scope"] = legacy_scope
        if include_saved_results:
            result["saved_results"] = saved_results(text, lines)
        if include_economics:
            result["economics"] = economic_block(lines)
        if include_topology:
            result["topology"] = topology_state(lines)
        return result

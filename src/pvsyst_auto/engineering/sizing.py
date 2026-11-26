"""Uniform-MPPT capacity planning, manufacturer design envelopes, and PAN/OND sizing estimates."""
from __future__ import annotations

from functools import partial
import math
from pathlib import Path
import re

from pvsyst_auto.components.editor import objects, pan_validation_fields, replace_value, root_block
from pvsyst_auto.components.store import electrical_inventory
from pvsyst_auto.project.circuit import circuit_nodes, inventory, positive_integer, resize_capacity
from pvsyst_auto.validation import (
    append_issue as _issue, append_warning as _warn, finite_number as _finite,
    is_finite_number as _is_number, is_printable_text,
)
from pvsyst_auto.validation import NATIVE_TEXT_OR_SEED

# --------------------------------------------------------------------- capacity planner
NUMBERS = (
    "target_dc_kw", "max_dc_ac_ratio", "cold_voc_v", "hot_vmp_v", "cold_vmp_v",
    "module_max_operating_current_a", "module_max_short_circuit_current_a",
    "mppt_max_operating_current_a", "mppt_max_short_circuit_current_a",
    "design_min_temperature_c", "design_min_operating_temperature_c", "design_max_operating_temperature_c",
)
CAPACITY_NUMBERS = NUMBERS


def plan_capacity(store, lines, array_id, pan, inverter, request):
    """Plan uniform MPPT series/parallel/inverter counts against declared bounds."""
    result = {"status": "blocked", "issues": []}
    def issue(field, message):
        result["issues"].append({"field": "capacity." + field, "message": message})
    allowed = {*NUMBERS, "min_dc_ac_ratio", "capacity_tolerance_pct", "voltage_standard", "design_basis"}
    if not isinstance(request, dict):
        issue("request", "Use a capacity object")
        return result
    for key in request.keys() - allowed:
        issue(key, "Unknown capacity input")
    for name in (*NUMBERS, "voltage_standard", "design_basis"):
        if request.get(name) is None or request.get(name) == "":
            issue(name, "Required design input is missing; no value is inferred")
    values = {**request, "capacity_tolerance_pct": request.get("capacity_tolerance_pct", 5)}
    for name in (*NUMBERS, "capacity_tolerance_pct"):
        value = values.get(name)
        if value is None and name == "capacity_tolerance_pct":
            issue(name, "Use a finite strict numeric tolerance, not null")
        elif value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            issue(name, "Use a finite strict numeric value")
    min_ratio = values.get("min_dc_ac_ratio")
    if min_ratio is not None and (isinstance(min_ratio, bool) or not isinstance(min_ratio, (int, float)) or not math.isfinite(min_ratio) or min_ratio <= 0):
        issue("min_dc_ac_ratio", "Use a positive finite numeric min_dc_ac_ratio")
    if result["issues"]:
        return result
    for name in NUMBERS:
        if not name.startswith("design_") and values[name] <= 0:
            issue(name, "Design magnitudes must be positive")
    if min_ratio is not None and min_ratio > values["max_dc_ac_ratio"]:
        issue("min_dc_ac_ratio", "min_dc_ac_ratio must not exceed max_dc_ac_ratio")
    if not 0 <= values["capacity_tolerance_pct"] <= 20:
        issue("capacity_tolerance_pct", "Use 0..20 percent")
    if values["voltage_standard"] not in ("IEC", "UL"):
        issue("voltage_standard", "Explicitly choose IEC or UL")
    basis = values["design_basis"]
    if not isinstance(basis, str) or not basis.strip() or len(basis) > 2000 or not basis.isprintable():
        issue("design_basis", "Provide a printable source/method for the declared design envelope, up to 2000 characters")
    if not (-100 <= values["design_min_temperature_c"] <= values["design_min_operating_temperature_c"] <= values["design_max_operating_temperature_c"] <= 150):
        issue("design_min_temperature_c", "Require -100 <= ambient minimum <= operating minimum <= operating maximum <= 150 degC")
    if not values["hot_vmp_v"] <= values["cold_vmp_v"] <= values["cold_voc_v"]:
        issue("cold_voc_v", "Require positive hot Vmp <= cold operating Vmp <= minimum-temperature Voc")
    if values["module_max_short_circuit_current_a"] < values["module_max_operating_current_a"]:
        issue("module_max_short_circuit_current_a", "Short-circuit envelope must cover operating-current envelope")
    if values["mppt_max_short_circuit_current_a"] < values["mppt_max_operating_current_a"]:
        issue("mppt_max_short_circuit_current_a", "Short-circuit rating must cover operating-current rating")
    if result["issues"]:
        return result
    try:
        arrays, system = store._sections(lines)
        if len(arrays) != 1 or array_id not in arrays:
            raise ValueError("Capacity planning requires one subarray")
        store._require_no_geometry(lines, require_shading_section=True)
        bounds = arrays[array_id]
        root = root_block(lines, bounds)
        raw_flags = root.one("Flags")[1]
        if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw_flags):
            raise ValueError("Require explicit hexadecimal subarray Flags")
        flags = int(raw_flags[1:], 16)
        types = store._field_indices(lines, system, "SystemType") if system is not None else []
        if len(types) != 1 or lines[types[0]].split("=", 1)[1].strip() != "Grid":
            raise ValueError("Capacity planning requires a Grid system")
        if (root.one("NStrOrient1")[1] != root.one("NStringCh")[1]
                or any(k.startswith("NStrOrient") and k != "NStrOrient1" and root.one(k)[1] != "0" for k in root.fields)):
            raise ValueError("Require all strings in the common fixed plane")
        if (root.one("Version")[1] not in NATIVE_TEXT_OR_SEED or not flags & 0x400
                or flags & 0x100 or root.one("PowerSharingInverterId")[1] != "-1"):
            raise ValueError("Require verified independent MPPT mode with disabled power sharing")
        plane = positive_integer(root.one("NoOrientation")[1], "NoOrientation")
        store._ordinary_fixed_plane(lines, store._orientation(lines, plane))
        if inventory(lines, arrays)["counts_valid"] is not True:
            raise ValueError("Require complete consistent grid counts")

        def walk(node):
            yield node
            for child in node.children:
                yield from walk(child)

        groups = [node for top in circuit_nodes(lines) for node in walk(top) if node.kind == "InverterNode"]
        if len(groups) != 1 or len(groups[0].children) != 1 or groups[0].children[0].kind != "MPPTNode":
            raise ValueError("Require one exclusive homogeneous inverter group and one repeatable MPPT branch")
        group, port = groups[0], groups[0].children[0]
        if len(port.children) != 1 or port.children[0].kind != "StringNode":
            raise ValueError("Require one homogeneous parallel-string leaf per MPPT")
        leaf = port.children[0]
        if "SEIsHeterogeneousString" in root.fields and root.one("SEIsHeterogeneousString")[1].rstrip(",") != "False":
            raise ValueError("Heterogeneous/optimizer string declarations require separate design handling")
        if any(inverter.get("modes", {}).get(k) != v for k, v in (("ModeOper", "MPPT"), ("CompPMax", "Lim"), ("CompVMax", "Lim"))):
            raise ValueError("Require explicitly declared MPPT operation and Lim voltage/power modes")
        ports = positive_integer(port.one("NElements", "1"), "MPPT multiplicity")
        old_inv = positive_integer(group.one("NElements", "1"), "Inverter multiplicity")
        old_leaf = positive_integer(leaf.one("NElements", "1"), "String multiplicity")
        old_series = positive_integer(root.one("NModSerie")[1], "Series modules")

        def declared(name):
            item = inverter["fields"][name]
            if item["status"] != "declared":
                raise ValueError(f"Require a positive declared inverter {name}")
            return item["value"]

        if ports != declared("NbMPPT"):
            raise ValueError("Template must populate every hardware MPPT; partial power budgets are not inferred")
        voltage_min, voltage_max, absolute_max = (declared(n) for n in ("VMppMin", "VMPPMax", "VAbsMax"))
        ac_kw = declared("PNomConv")
        if pan["Technol"] not in (["mtSiMono"], ["mtSiPoly"]) or float(pan["GRef"][0]) != 1000 or float(pan["TRef"][0]) != 25:
            raise ValueError("Require crystalline PAN with GRef=1000 W/m2 and TRef=25 degC")
        module_limit_values = pan.get("VMax" + values["voltage_standard"], [])
        if len(module_limit_values) != 1:
            raise ValueError("Require one PAN module-system voltage rating for the selected standard")
        module_limit, module_kw = float(module_limit_values[0]), float(pan["PNom"][0]) / 1000
        if not all(math.isfinite(v) and v > 0 for v in (module_limit, module_kw)):
            raise ValueError("Require finite positive module-system voltage and power")
        if (values["design_min_operating_temperature_c"] <= 25 <= values["design_max_operating_temperature_c"]
                and not values["hot_vmp_v"] <= float(pan["Vmp"][0]) <= values["cold_vmp_v"]):
            raise ValueError("Declared operating-voltage envelope must cover PAN reference Vmp when TRef is in range")
        if values["design_min_temperature_c"] <= 25 <= values["design_max_operating_temperature_c"] and values["cold_voc_v"] < float(pan["Voc"][0]):
            raise ValueError("Declared maximum Voc must cover PAN reference Voc")
        if values["module_max_operating_current_a"] < float(pan["Imp"][0]) or values["module_max_short_circuit_current_a"] < float(pan["ISC"][0]):
            raise ValueError("Declared module current envelopes must cover PAN reference-point currents")
        max_leaf = int(min(100, values["mppt_max_operating_current_a"] / values["module_max_operating_current_a"],
                           values["mppt_max_short_circuit_current_a"] / values["module_max_short_circuit_current_a"]))
        candidates = []
        for series in range(1, 101):
            if (series * values["hot_vmp_v"] < voltage_min or series * values["cold_vmp_v"] > voltage_max
                    or series * values["cold_voc_v"] > min(absolute_max, module_limit)):
                continue
            for parallel in range(1, max_leaf + 1):
                if "NElements" not in leaf.fields and parallel != old_leaf:
                    continue
                per_inverter = series * ports * parallel * module_kw
                if (not math.isfinite(per_inverter) or per_inverter <= 0 or per_inverter / ac_kw > values["max_dc_ac_ratio"]
                        or (min_ratio is not None and per_inverter / ac_kw < min_ratio)):
                    continue
                ideal = min(10000, max(1, values["target_dc_kw"] / per_inverter))
                for count in {math.floor(ideal), math.ceil(ideal)}:
                    if "NElements" not in group.fields and count != old_inv:
                        continue
                    power = per_inverter * count
                    error = abs(power - values["target_dc_kw"]) / values["target_dc_kw"] * 100
                    if error <= values["capacity_tolerance_pct"] + 1e-9:
                        candidates.append(((round(error, 9), count, abs(series - old_series), series, parallel), series, parallel, count, power))
        if not candidates:
            raise ValueError("No supported integral plan meets the declared voltage/current/ratio/tolerance bounds; explicit editable node counts may be required")
        _, series, parallel, count, power = min(candidates)
        strings, candidate, steps = count * ports * parallel, lines.copy(), []
        if count != old_inv:
            resize_capacity(candidate, arrays, array_id, target_inverters=count)
            steps.append({"NInverter": count})
        if strings != old_leaf * ports * count:
            resize_capacity(candidate, arrays, array_id, target_strings=strings)
            steps.append({"NStringCh": strings})
        if series != old_series:
            index, = store._field_indices(candidate, bounds, "NModSerie")
            candidate[index] = replace_value(candidate[index], str(series))
            steps.append({"NModSerie": series})
        final = inventory(candidate, arrays)
        if final["counts_valid"] is not True or final["subarrays"][0]["declared_module_count"] != strings * series:
            raise ValueError("Planned serialized counts do not match the intended capacity")
        result.update(status="ready", design=values, series_modules=series, parallel_strings_per_mppt=parallel,
            mppt_per_inverter=ports, inverter_count=count, string_count=strings, module_count=strings * series,
            installed_dc_kw=power, installed_ac_kw=count * ac_kw, dc_ac_ratio=power / (count * ac_kw),
            capacity_error_pct=(power - values["target_dc_kw"]) / values["target_dc_kw"] * 100, steps=steps,
            checks={"hot_string_vmp_v": series * values["hot_vmp_v"], "cold_string_vmp_v": series * values["cold_vmp_v"],
                    "cold_string_voc_v": series * values["cold_voc_v"], "inverter_absolute_max_v": absolute_max,
                    "module_system_max_v": module_limit, "mppt_operating_current_a": parallel * values["module_max_operating_current_a"],
                    "mppt_short_circuit_current_a": parallel * values["module_max_short_circuit_current_a"]},
            search_bounds={"series_modules": [1, 100], "parallel_strings_per_mppt": [1, 100], "inverter_count": [1, 10000]},
            selection="capacity error rounded to 1e-9 percent, then fewer inverters, then series closest to template")
    except (ValueError, KeyError) as exc:
        issue("template_or_components", str(exc))
    return result

# --------------------------------------------------------------------- design envelope
VOLTAGE_UNITS = ("pct_per_degC", "V_per_degC")
CURRENT_UNITS = ("pct_per_degC", "A_per_degC")
MIN_TEMPERATURE_C, MAX_TEMPERATURE_C, MIN_PCT_SLOPE, MIN_VOLT_SLOPE_PER_DEGC, MAX_TEXT = -100.0, 150.0, -2.0, -1.0, 2000
REQUEST_KEYS = frozenset((
    "manufacturer_model", "target_dc_kw", "max_dc_ac_ratio", "voltage_standard", "capacity_tolerance_pct",
    "temperatures", "envelope", "coefficients", "module_limits", "mppt_limits", "effective_irradiance", "design_basis_extra",
))
TEMPERATURE_KEYS = frozenset(("min_ambient_c", "min_ambient_basis", "min_module_operating_c", "max_module_operating_c", "annual_min_ambient_c"))
ENVELOPE_KEYS = ("cold_voc_v", "cold_vmp_v", "hot_vmp_v")
IRRADIANCE_KEYS = frozenset(("max_irradiance_w_m2", "rear_gain_factor", "source"))
MODULE_STC = ("PNom", "GRef", "TRef", "Voc", "Vmp", "Imp", "ISC")
INVERTER_REFERENCE = ("PNomConv", "VMppMin", "VMPPMax", "VAbsMax", "IDCMax", "NbMPPT")
COEFFICIENT_KEYS = ("beta_voc", "beta_vmp", "alpha_isc", "alpha_imp")
LIMIT_KEYS = ("max_operating_current_a", "max_short_circuit_current_a")
FORMULAS = {
    "cold_voc_v": "Voc_ref * (1 + beta_voc * (min_ambient_c - TRef))",
    "cold_vmp_v": "Vmp_ref * (1 + beta_vmp * (min_module_operating_c - TRef))",
    "hot_vmp_v": "Vmp_ref * (1 + beta_vmp * (max_module_operating_c - TRef))",
    "module_max_short_circuit_current_a": "ISC_ref * (max_irradiance_w_m2 / GRef) * rear_gain_factor * max(1 + alpha_isc * (T - TRef)) over the operating endpoints and TRef",
    "module_max_operating_current_a": "Imp_ref * (max_irradiance_w_m2 / GRef) * rear_gain_factor * max(1 + alpha_imp * (T - TRef)) over the operating endpoints and TRef",
}
_is_text = partial(is_printable_text, limit=MAX_TEXT)
DECLARATION_KEYS = frozenset(("value", "unit", "source"))
LIMIT_UNITS, ENVELOPE_UNITS = ("A",), ("V",)


def _declaration(declaration, field, result):
    if not isinstance(declaration, dict):
        return declaration, None, None
    if unknown := declaration.keys() - DECLARATION_KEYS:
        for key in sorted(unknown):
            _issue(result, field + "." + str(key), "Unknown declaration key")
        return None, None, None
    return declaration.get("value"), declaration.get("unit"), declaration.get("source")


def _check_number(value, field, result, positive=True):
    if not _is_number(value):
        _issue(result, field, "Use a finite strict numeric value")
        return None
    value = float(value)
    if positive and value <= 0:
        _issue(result, field, "Use a positive value")
        return None
    return value


def _check_source(source, field, result, required=False):
    if source is None:
        (_issue if required else _warn)(result, field, "Declare the manufacturer/source of this value" if required else "Value declared without a source; traceability is incomplete")
        return None
    if not _is_text(source):
        _issue(result, field, "Source must be printable text up to 2000 characters")
        return None
    return source.strip()


def _coefficient(declaration, field, result, units, reference, negative):
    if declaration is None:
        return None
    before = len(result["issues"])
    value, unit, source = _declaration(declaration, field, result)
    if len(result["issues"]) != before or (value := _check_number(value, field, result, positive=False)) is None:
        return None
    if unit not in units:
        _issue(result, field + ".unit", "Declare the manufacturer unit explicitly (" + " or ".join(units) + "); the unit of a serialized component coefficient is never assumed")
        return None
    _check_source(source, field + ".source", result, required=True)
    if negative and value > 0:
        _issue(result, field, "Reject a non-physical positive temperature coefficient for this quantity")
        return None
    floor = MIN_PCT_SLOPE if unit == "pct_per_degC" else (MIN_VOLT_SLOPE_PER_DEGC if unit == "V_per_degC" else -reference)
    if value < floor:
        _issue(result, field, "Reject a non-physical magnitude below " + str(floor) + " per degC")
        return None
    return {"value": value, "unit": unit, "source": source.strip() if _is_text(source) else None,
            "fraction": value / 100.0 if unit == "pct_per_degC" else value / reference}


def _max_temperature_factor(fraction, tref, oper_min, oper_max):
    temps = [oper_min, oper_max, *([tref] if oper_min <= tref <= oper_max else [])]
    return max(1.0 + fraction * (v - tref) for v in temps)


def _declared_limit(container, name, field, result):
    if not isinstance(container, dict) or name not in container:
        return None
    before = len(result["issues"])
    value, unit, source = _declaration(container[name], field, result)
    if len(result["issues"]) != before:
        return None
    if unit is not None and unit not in LIMIT_UNITS:
        _issue(result, field + ".unit", "Declare the current unit as A; no unit conversion is inferred")
        return None
    if (value := _check_number(value, field + ".value", result)) is None:
        return None
    _check_source(source, field + ".source", result, required=True)
    return value


def _read_declared(electrical, name, field, result, positive=True):
    item = (electrical or {}).get("fields", {}).get(name)
    if not isinstance(item, dict):
        _issue(result, field, "Required electrical field is not readable")
        return None
    if item.get("status") != "declared":
        _issue(result, field, "Field status is " + repr(item.get("status")) + "; only a declared value is used and no value is inferred")
        return None
    return _check_number(item.get("value"), field, result, positive=positive)


def _req_positive(request, name, result):
    raw = request.get(name)
    val = _check_number(raw, name, result) if raw is not None else None
    if val is None and raw is None:
        _issue(result, name, "Required design input is missing; no value is inferred")
    return val


def prepare_design_envelope(module_electrical, inverter_electrical, request):
    """Build the template-capacity input from declared manufacturer data."""
    result = {"status": "blocked", "capacity": None, "issues": [], "warnings": [], "provenance": {}, "envelope_mode": None}
    if not isinstance(request, dict):
        _issue(result, "request", "Provide the design-envelope request object")
        return result
    for key in sorted(request.keys() - REQUEST_KEYS):
        _issue(result, str(key), "Unknown design-envelope input")
    manufacturer_model = request.get("manufacturer_model")
    if not _is_text(manufacturer_model):
        _issue(result, "manufacturer_model", "Declare the module/inverter manufacturer and model this envelope describes")
    target_dc_kw, max_dc_ac_ratio = _req_positive(request, "target_dc_kw", result), _req_positive(request, "max_dc_ac_ratio", result)
    voltage_standard = request.get("voltage_standard")
    if voltage_standard not in ("IEC", "UL"):
        _issue(result, "voltage_standard", "Explicitly choose IEC or UL")
        voltage_standard = None
    tolerance = request.get("capacity_tolerance_pct", 5)
    if not _is_number(tolerance) or not 0 <= float(tolerance) <= 20:
        _issue(result, "capacity_tolerance_pct", "Use 0..20 percent")
        tolerance = None
    else:
        tolerance = float(tolerance)
    design_basis_extra = request.get("design_basis_extra")
    if design_basis_extra is not None and not _is_text(design_basis_extra):
        _issue(result, "design_basis_extra", "Use printable text up to 2000 characters")
        design_basis_extra = None

    voc_ref = _read_declared(module_electrical, "Voc", "module.Voc", result)
    vmp_ref = _read_declared(module_electrical, "Vmp", "module.Vmp", result)
    imp_ref = _read_declared(module_electrical, "Imp", "module.Imp", result)
    isc_ref = _read_declared(module_electrical, "ISC", "module.ISC", result)
    gref = _read_declared(module_electrical, "GRef", "module.GRef", result)
    tref = _read_declared(module_electrical, "TRef", "module.TRef", result, positive=False)
    _read_declared(module_electrical, "PNom", "module.PNom", result)
    if gref is not None and abs(gref - 1000.0) > 1e-9:
        _warn(result, "module.GRef", "The module reference irradiance is not 1000 W/m2; template capacity planning requires a 1000 W/m2 reference module")
    inverter_reference = {n: {k: item.get(k) for k in ("value", "unit", "status", "path")}
                          for n in INVERTER_REFERENCE if isinstance(item := (inverter_electrical or {}).get("fields", {}).get(n), dict)}
    for name in ("VMppMin", "VMPPMax", "VAbsMax"):
        if not inverter_reference.get(name) or inverter_reference[name].get("status") != "declared":
            _warn(result, "inverter." + name, "The inverter MPP/absolute voltage window is not declared; the capacity planner reads it from the OND and refuses the variant")
    if not inverter_reference.get("IDCMax") or inverter_reference["IDCMax"].get("status") != "declared":
        _warn(result, "inverter.IDCMax", "IDCMax is not a positive declared ceiling and is never used as a per-MPPT current rating")
    if voltage_standard in ("IEC", "UL"):
        limit_item = (module_electrical or {}).get("fields", {}).get("VMax" + voltage_standard)
        if not isinstance(limit_item, dict) or limit_item.get("status") != "declared":
            _issue(result, "module.VMax" + voltage_standard, "The module-system voltage rating for the selected standard is not a declared positive value; no envelope is prepared without it")

    temperatures, temps, basis = request.get("temperatures"), {"min_ambient_c": None, "min_module_operating_c": None, "max_module_operating_c": None}, None
    if not isinstance(temperatures, dict):
        _issue(result, "temperatures", "Provide the design temperature object")
    else:
        for key in sorted(temperatures.keys() - TEMPERATURE_KEYS):
            _issue(result, "temperatures." + str(key), "Unknown design temperature input")
        for name in ("min_ambient_c", "min_module_operating_c", "max_module_operating_c"):
            val = temperatures.get(name)
            if val is None:
                _issue(result, "temperatures." + name, "Required design input is missing; no value is inferred")
            else:
                temps[name] = _check_number(val, "temperatures." + name, result, positive=False)
        basis = temperatures.get("min_ambient_basis")
        if basis is None:
            _issue(result, "temperatures.min_ambient_basis", "Declare the source/method of the design minimum ambient temperature (IEC 62548-1 uses the lowest temperature ever observed)")
        elif not _is_text(basis):
            _issue(result, "temperatures.min_ambient_basis", "Use printable text up to 2000 characters")
            basis = None
        if (annual := temperatures.get("annual_min_ambient_c")) is not None:
            annual = _check_number(annual, "temperatures.annual_min_ambient_c", result, positive=False)
            if annual is not None and temps["min_ambient_c"] is not None and abs(annual - temps["min_ambient_c"]) < 1e-9:
                _warn(result, "temperatures.annual_min_ambient_c", "A weather-file annual minimum equals the declared design extreme; a single-year minimum is not a design extreme")
    tmin, tmin_op, tmax_op = temps["min_ambient_c"], temps["min_module_operating_c"], temps["max_module_operating_c"]
    if tmin is not None and tmin_op is not None and tmax_op is not None:
        if not MIN_TEMPERATURE_C <= tmin <= tmin_op <= tmax_op <= MAX_TEMPERATURE_C:
            _issue(result, "temperatures", "Require -100 <= min ambient <= min module operating <= max module operating <= 150 degC")
        if abs(tmin - tmin_op) < 1e-9:
            _warn(result, "temperatures.min_module_operating_c", "The minimum module operating temperature equals the design minimum ambient temperature; operating temperatures are cell temperatures, not ambient temperatures")

    envelope = request.get("envelope")
    direct_voltage = {}
    if envelope is not None and not isinstance(envelope, dict):
        _issue(result, "envelope", "Provide direct voltage values as an object")
    elif isinstance(envelope, dict):
        for key in sorted(envelope.keys() - set(ENVELOPE_KEYS)):
            _issue(result, "envelope." + str(key), "Unknown direct envelope input")
        for name in ENVELOPE_KEYS:
            if name not in envelope:
                continue
            before = len(result["issues"])
            value, unit, source = _declaration(envelope[name], "envelope." + name, result)
            if len(result["issues"]) != before:
                continue
            if unit is not None and unit not in ENVELOPE_UNITS:
                _issue(result, "envelope." + name + ".unit", "Declare the voltage unit as V; no unit conversion is inferred")
                continue
            if (value := _check_number(value, "envelope." + name, result)) is not None:
                _check_source(source, "envelope." + name + ".source", result, required=False)
                direct_voltage[name] = value

    coefficients = request.get("coefficients")
    if coefficients is not None and not isinstance(coefficients, dict):
        _issue(result, "coefficients", "Provide coefficients as an object")
        coefficients = {}
    else:
        coefficients = coefficients or {}
        for key in sorted(coefficients.keys() - set(COEFFICIENT_KEYS)):
            _issue(result, "coefficients." + str(key), "Unknown coefficient input")
    beta_voc = _coefficient(coefficients.get("beta_voc"), "coefficients.beta_voc", result, VOLTAGE_UNITS, voc_ref or 1.0, True)
    beta_vmp = _coefficient(coefficients.get("beta_vmp"), "coefficients.beta_vmp", result, VOLTAGE_UNITS, vmp_ref or 1.0, True)
    alpha_isc = _coefficient(coefficients.get("alpha_isc"), "coefficients.alpha_isc", result, CURRENT_UNITS, isc_ref or 1.0, False)
    alpha_imp = _coefficient(coefficients.get("alpha_imp"), "coefficients.alpha_imp", result, CURRENT_UNITS, imp_ref or 1.0, False)
    if "cold_voc_v" not in direct_voltage and beta_voc is None:
        _issue(result, "coefficients.beta_voc", "Supply beta_voc with an explicit unit, or provide envelope.cold_voc_v from the manufacturer")
    for k in ("cold_vmp_v", "hot_vmp_v"):
        if k not in direct_voltage and beta_vmp is None:
            _issue(result, "coefficients.beta_vmp", f"Supply beta_vmp with an explicit unit, or provide envelope.{k} from the manufacturer; a Pmax coefficient is not a Vmp coefficient")

    module_limits, mppt_limits = request.get("module_limits"), request.get("mppt_limits")
    for container, prefix in ((module_limits, "module_limits"), (mppt_limits, "mppt_limits")):
        if container is not None and not isinstance(container, dict):
            _issue(result, prefix, "Provide the manufacturer limit object")
        elif isinstance(container, dict):
            for key in sorted(container.keys() - set(LIMIT_KEYS)):
                _issue(result, prefix + "." + str(key), "Unknown manufacturer limit input")
    module_short = _declared_limit(module_limits, "max_short_circuit_current_a", "module_limits.max_short_circuit_current_a", result)
    module_operating = _declared_limit(module_limits, "max_operating_current_a", "module_limits.max_operating_current_a", result)
    mppt_operating = _declared_limit(mppt_limits, "max_operating_current_a", "mppt_limits.max_operating_current_a", result)
    mppt_short = _declared_limit(mppt_limits, "max_short_circuit_current_a", "mppt_limits.max_short_circuit_current_a", result)
    if (not isinstance(module_limits, dict) or "max_short_circuit_current_a" not in module_limits) and alpha_isc is None:
        _issue(result, "module_limits.max_short_circuit_current_a", "Provide the manufacturer maximum short-circuit current directly, or supply alpha_isc with an explicit unit")
    if (not isinstance(module_limits, dict) or "max_operating_current_a" not in module_limits) and alpha_imp is None:
        _issue(result, "module_limits.max_operating_current_a", "Provide the manufacturer maximum operating current directly, or supply alpha_imp with an explicit unit; the Isc coefficient is not substituted for Imp")
    for key, val in (("max_operating_current_a", mppt_operating), ("max_short_circuit_current_a", mppt_short)):
        if val is None and (not isinstance(mppt_limits, dict) or key not in mppt_limits):
            _issue(result, f"mppt_limits.{key}", f"Declare the per-MPPT {'operating' if 'operating' in key else 'short-circuit'} current rating from the inverter datasheet; a whole-converter current field such as IDCMax is not a substitute")

    irradiance = request.get("effective_irradiance")
    if irradiance is not None and not isinstance(irradiance, dict):
        _issue(result, "effective_irradiance", "Provide the effective irradiance object")
        irradiance = {}
    else:
        irradiance = irradiance or {}
        for key in sorted(irradiance.keys() - IRRADIANCE_KEYS):
            _issue(result, "effective_irradiance." + str(key), "Unknown effective irradiance input")
    e_max = rear_gain = None
    needs_irradiance = not (isinstance(module_limits, dict) and "max_short_circuit_current_a" in module_limits and "max_operating_current_a" in module_limits)
    if needs_irradiance:
        if "max_irradiance_w_m2" not in irradiance:
            _issue(result, "effective_irradiance.max_irradiance_w_m2", "Declare the maximum effective irradiance for this design; the module reference irradiance is not a maximum-irradiance envelope")
        else:
            e_max = _check_number(irradiance.get("max_irradiance_w_m2"), "effective_irradiance.max_irradiance_w_m2", result)
            if e_max is not None and gref is not None and e_max < gref:
                _warn(result, "effective_irradiance.max_irradiance_w_m2", "The declared maximum effective irradiance is below the module reference irradiance; the current envelope may be underestimated")
        if "rear_gain_factor" not in irradiance:
            _issue(result, "effective_irradiance.rear_gain_factor", "Declare the rear/bifacial gain factor explicitly (use 1.0 for a monofacial plane); a gain is never assumed")
        else:
            rear_gain = _check_number(irradiance.get("rear_gain_factor"), "effective_irradiance.rear_gain_factor", result)
        _check_source(irradiance.get("source"), "effective_irradiance.source", result, required=True)
    elif any(name in irradiance for name in ("max_irradiance_w_m2", "rear_gain_factor")):
        _warn(result, "effective_irradiance", "Irradiance inputs are declared but not used: both module current limits are direct manufacturer values")

    if result["issues"]:
        return result
    if needs_irradiance and (e_max is None or rear_gain is None):
        _issue(result, "effective_irradiance", "A coefficient-derived current envelope requires an explicit maximum irradiance and rear-gain factor")
        return result

    cold_voc, cold_voc_source = (direct_voltage["cold_voc_v"], "explicit envelope") if "cold_voc_v" in direct_voltage else (
        voc_ref * (1.0 + beta_voc["fraction"] * (tmin - tref)), "beta_voc from " + str(beta_voc["source"]))
    cold_vmp, cold_vmp_source = (direct_voltage["cold_vmp_v"], "explicit envelope") if "cold_vmp_v" in direct_voltage else (
        vmp_ref * (1.0 + beta_vmp["fraction"] * (tmin_op - tref)), "beta_vmp from " + str(beta_vmp["source"]))
    hot_vmp, hot_vmp_source = (direct_voltage["hot_vmp_v"], "explicit envelope") if "hot_vmp_v" in direct_voltage else (
        vmp_ref * (1.0 + beta_vmp["fraction"] * (tmax_op - tref)), "beta_vmp from " + str(beta_vmp["source"]))
    irradiance_factor = e_max / gref if e_max is not None else None
    module_short, module_short_source = (module_short, "direct manufacturer limit") if module_short is not None else (
        isc_ref * irradiance_factor * rear_gain * _max_temperature_factor(alpha_isc["fraction"], tref, tmin_op, tmax_op), "alpha_isc from " + str(alpha_isc["source"]))
    module_operating, module_operating_source = (module_operating, "direct manufacturer limit") if module_operating is not None else (
        imp_ref * irradiance_factor * rear_gain * _max_temperature_factor(alpha_imp["fraction"], tref, tmin_op, tmax_op), "alpha_imp from " + str(alpha_imp["source"]))

    if not hot_vmp <= cold_vmp <= cold_voc:
        _issue(result, "envelope", "Require hot Vmp <= cold operating Vmp <= minimum-temperature Voc")
    for cond, fpath, msg in (
        (module_short < module_operating, "module_limits.max_short_circuit_current_a", "The short-circuit envelope must cover the operating-current envelope"),
        (mppt_short < mppt_operating, "mppt_limits.max_short_circuit_current_a", "The short-circuit rating must cover the operating-current rating"),
        (module_operating < imp_ref, "module_limits.max_operating_current_a", "The declared module operating-current envelope must cover the PAN reference Imp"),
        (module_short < isc_ref, "module_limits.max_short_circuit_current_a", "The declared module short-circuit envelope must cover the PAN reference Isc"),
        (mppt_operating < module_operating, "mppt_limits.max_operating_current_a", "The per-MPPT operating rating must cover the module operating envelope"),
        (mppt_short < module_short, "mppt_limits.max_short_circuit_current_a", "The per-MPPT short-circuit rating must cover the module short-circuit envelope"),
    ):
        if cond:
            _issue(result, fpath, msg)
    if tmin_op <= tref <= tmax_op:
        if not hot_vmp <= vmp_ref <= cold_vmp:
            _issue(result, "envelope", "The computed operating-voltage envelope must cover the PAN reference Vmp while TRef is inside the operating band")
    else:
        _warn(result, "temperatures", "The module reference temperature is outside the declared operating band; the Vmp coverage check of the reference point is skipped")
    if tmin <= tref <= tmax_op:
        if cold_voc < voc_ref:
            _issue(result, "envelope.cold_voc_v", "The computed maximum Voc must cover the PAN reference Voc")
    else:
        _warn(result, "temperatures", "The module reference temperature is outside the declared temperature range; the Voc coverage check of the reference point is skipped")
    if result["issues"]:
        return result

    mode = "explicit" if all(n in direct_voltage for n in ENVELOPE_KEYS) else ("coefficients" if not direct_voltage else "mixed")
    sources = {"min_ambient_basis": basis, "cold_voc_v": cold_voc_source, "cold_vmp_v": cold_vmp_source, "hot_vmp_v": hot_vmp_source,
               "module_max_operating_current_a": module_operating_source, "module_max_short_circuit_current_a": module_short_source,
               "mppt_max_operating_current_a": "direct manufacturer limit", "mppt_max_short_circuit_current_a": "direct manufacturer limit",
               "max_irradiance_w_m2": irradiance.get("max_irradiance_w_m2"), "rear_gain_factor": irradiance.get("rear_gain_factor"),
               "irradiance_source": irradiance.get("source")}
    irr_text = ("not used: both module current limits are direct manufacturer values" if e_max is None
                else f"E_max={e_max:.6g} W/m2; rear_gain={rear_gain:.6g}")
    basis_text = (f"public linear envelope for {manufacturer_model}; mode={mode}; Tmin_basis={basis}; voltages="
                  + "; ".join(f"{n}: {FORMULAS[n]}" for n in ENVELOPE_KEYS)
                  + f"; {irr_text}; module_current={module_operating_source}/{module_short_source}"
                  + "; mppt_current=direct manufacturer per-MPPT limits; method is a public linear temperature relation, not the native one-diode fitting"
                  + (f"; {design_basis_extra.strip()}" if design_basis_extra else ""))
    if len(basis_text) > MAX_TEXT:
        basis_text = basis_text[:MAX_TEXT]
        _warn(result, "design_basis", "The composed design basis was truncated to 2000 characters")
    capacity = {"target_dc_kw": target_dc_kw, "max_dc_ac_ratio": max_dc_ac_ratio, "capacity_tolerance_pct": tolerance,
                "voltage_standard": voltage_standard, "cold_voc_v": cold_voc, "hot_vmp_v": hot_vmp, "cold_vmp_v": cold_vmp,
                "module_max_operating_current_a": module_operating, "module_max_short_circuit_current_a": module_short,
                "mppt_max_operating_current_a": mppt_operating, "mppt_max_short_circuit_current_a": mppt_short,
                "design_min_temperature_c": tmin, "design_min_operating_temperature_c": tmin_op,
                "design_max_operating_temperature_c": tmax_op, "design_basis": basis_text}
    if missing := [n for n in CAPACITY_NUMBERS if n not in capacity]:
        _issue(result, "capacity", "The capacity contract is incomplete: " + ", ".join(missing))
        return result
    for name in CAPACITY_NUMBERS:
        if not _is_number(capacity[name]) or (not name.startswith("design_") and capacity[name] <= 0):
            _issue(result, "capacity." + name, "Require a finite strict numeric value; design magnitudes must be positive")
    if not _is_text(capacity["design_basis"]):
        _issue(result, "capacity.design_basis", "Provide a printable basis up to 2000 characters")
    if result["issues"]:
        return result
    unused = (sorted({i.get("field") for i in (module_electrical or {}).get("unverified", []) if i.get("field")})
              + sorted({i.get("field") for i in (inverter_electrical or {}).get("unverified", []) if i.get("field")}))
    result.update(status="ready", capacity=capacity, envelope_mode=mode, provenance={
        "method": "public linear temperature envelope", "envelope_mode": mode, "selected_component": manufacturer_model,
        "module_reference": {n: module_electrical["fields"].get(n) for n in MODULE_STC}, "inverter_reference": inverter_reference,
        "formulas": {n: FORMULAS[n] for n in ENVELOPE_KEYS + tuple(n for n in FORMULAS if n.startswith("module_"))},
        "sources": sources, "unused_serialized_fields": unused})
    return result

# --------------------------------------------------------------------- sizing assistant
DEFAULT_BETA_VOC_PCT_PER_C, DEFAULT_BETA_VMP_PCT_PER_C, DEFAULT_ALPHA_ISC_PCT_PER_C = -0.30, -0.35, 0.05
_ASYMMETRIC_MPPT_PAIRS = (
    ("IscMaxMain", "IscMaxSec", "A"), ("IMaxMain", "IMaxSec", "A"), ("IMaxDCMain", "IMaxDCSec", "A"),
    ("IDCMaxMain", "IDCMaxSec", "A"), ("PNomMain", "PNomSec", "kW"), ("PMaxMain", "PMaxSec", "kW"),
    ("NbInputsMain", "NbInputsSec", "inputs"), ("NbMPPTMain", "NbMPPTSec", "MPPTs"),
)


def _inspect_ond_mppt_limits(ond_text: str) -> tuple[list[str], float | None]:
    fields: dict[str, list[float]] = {}
    for block in objects(ond_text):
        if block.path not in ("", "Converter"):
            continue
        for key, entries in block.fields.items():
            for _, raw in entries:
                try:
                    if math.isfinite(val := float(raw)):
                        fields.setdefault(key, []).append(val)
                except ValueError:
                    pass
    issues: list[str] = []
    for main_key, sec_key, unit in _ASYMMETRIC_MPPT_PAIRS:
        main_vals, sec_vals = [v for v in fields.get(main_key, ()) if v > 0], [v for v in fields.get(sec_key, ()) if v > 0]
        if main_vals and sec_vals and not math.isclose(main_vals[0], sec_vals[0], rel_tol=1e-9, abs_tol=1e-9):
            issues.append(f"OND declares asymmetric MPPT limits ({main_key}={main_vals[0]:g} {unit} != {sec_key}={sec_vals[0]:g} {unit}); uniform per-MPPT sizing cannot guarantee every input stays within rating")
        elif sec_vals and not main_vals:
            issues.append(f"OND declares secondary MPPT limit {sec_key}={sec_vals[0]:g} {unit} without a matching {main_key}; uniform per-MPPT sizing cannot assume equal inputs")
    isc_main, isc_sec = [v for v in fields.get("IscMaxMain", ()) if v > 0], [v for v in fields.get("IscMaxSec", ()) if v > 0]
    uniform_isc = isc_main[0] if (isc_main and ((isc_sec and math.isclose(isc_main[0], isc_sec[0], rel_tol=1e-9, abs_tol=1e-9)) or not isc_sec)) else None
    return issues, uniform_isc


def _resolve_loose_component(components, kind: str, filename: str) -> tuple[Path, str, str]:
    for library in ("workspace", "builtin"):
        if not (library == "builtin" and components.builtin is None) and (path := components._path(kind, filename, library)).is_file():
            raw = components._read(path)
            components._require_valid(components._inspect(kind, raw), complete=True)
            return path, library, raw.decode("utf-8-sig")
    raise FileNotFoundError(f"No readable loose {kind} file {filename!r} found in workspace or builtin library; sizing estimation requires readable text PAN and OND files")


def _infer_beta_voc_pct(pan_fields: dict, voc: float, caller_pct: float | None, assumptions: list[str]) -> float:
    if caller_pct is not None:
        val = _finite(caller_pct, "beta_voc_pct_per_c", minimum=-1.0, maximum=-0.05)
        assumptions.append(f"beta_voc={val:.3f} %/degC (caller-supplied)")
        return val
    raw_list = pan_fields.get("muVocSpec") or []
    if len(raw_list) == 1:
        try:
            raw_val = float(raw_list[0])
            if math.isfinite(raw_val) and raw_val < 0:
                v_per_c = raw_val / 1000.0 if abs(raw_val) > 1.0 else raw_val
                pct = (v_per_c / voc) * 100.0
                if -0.60 <= pct <= -0.15:
                    assumptions.append(f"beta_voc={pct:.3f} %/degC derived from PAN muVocSpec={raw_val} ({'mV/degC' if abs(raw_val) > 1.0 else 'V/degC'}) / Voc={voc:.2f} V")
                    return pct
        except ValueError:
            pass
    assumptions.append(f"beta_voc={DEFAULT_BETA_VOC_PCT_PER_C:.3f} %/degC (standard c-Si default; PAN muVocSpec absent or out of range)")
    return DEFAULT_BETA_VOC_PCT_PER_C


def suggest_sizing(
    store, *, pan_name: str, ond_name: str, target_dc_kw: float, voltage_standard: str = "IEC",
    min_ambient_c: float = -10.0, min_operating_c: float = 0.0, max_operating_c: float = 65.0,
    min_dc_ac_ratio: float = 0.85, max_dc_ac_ratio: float = 1.25, capacity_tolerance_pct: float = 8.0,
    beta_voc_pct_per_c: float | None = None, beta_vmp_pct_per_c: float | None = None, alpha_isc_pct_per_c: float | None = None,
) -> dict:
    """Estimate a safe electrical capacity envelope and recommended string/inverter layout from PAN/OND."""
    result = {"status": "blocked", "estimated": True, "requires_review": True, "issues": [], "assumptions": [], "recommended": None, "capacity": None}
    assumptions: list[str] = result["assumptions"]

    def issue(field: str, message: str) -> None:
        result["issues"].append({"field": field, "message": message})

    try:
        target = _finite(target_dc_kw, "target_dc_kw", minimum=0.0, strict_min=True)
        if voltage_standard not in ("IEC", "UL"):
            raise ValueError("voltage_standard must be 'IEC' or 'UL'")
        t_min, t_op_min, t_op_max = (_finite(min_ambient_c, "min_ambient_c", minimum=-60.0, maximum=40.0),
                                     _finite(min_operating_c, "min_operating_c", minimum=-60.0, maximum=50.0),
                                     _finite(max_operating_c, "max_operating_c", minimum=10.0, maximum=100.0))
        if not (t_min <= t_op_min <= t_op_max):
            raise ValueError("Require min_ambient_c <= min_operating_c <= max_operating_c")
        min_ratio, max_ratio = _finite(min_dc_ac_ratio, "min_dc_ac_ratio", minimum=0.5, maximum=2.0), _finite(max_dc_ac_ratio, "max_dc_ac_ratio", minimum=0.5, maximum=2.5)
        if not (min_ratio <= max_ratio):
            raise ValueError("min_dc_ac_ratio must be <= max_dc_ac_ratio")
        tol = _finite(capacity_tolerance_pct, "capacity_tolerance_pct", minimum=0.0, maximum=20.0)
    except ValueError as exc:
        issue("inputs", str(exc))
        return result

    try:
        pan_path, pan_lib, pan_text = _resolve_loose_component(store.components, "PAN", pan_name)
        ond_path, ond_lib, ond_text = _resolve_loose_component(store.components, "OND", ond_name)
        pan_elec, ond_elec, pan_fields = electrical_inventory("PAN", pan_text), electrical_inventory("OND", ond_text), pan_validation_fields(pan_text)
    except (ValueError, OSError) as exc:
        issue("components", str(exc))
        return result

    def _req(inv: dict, name: str, kind: str) -> float:
        entry = (inv.get("fields") or {}).get(name)
        if not isinstance(entry, dict) or entry.get("status") != "declared":
            raise ValueError(f"{kind} does not declare a valid positive {name}")
        return float(entry["value"])

    try:
        pnom_w, voc, vmp, imp, isc, v_max_mod = (_req(pan_elec, k, "PAN") for k in ("PNom", "Voc", "Vmp", "Imp", "ISC", "VMax" + voltage_standard))
        pnom_ac, vmpp_min, vmpp_max, vabs_max = (_req(ond_elec, k, "OND") for k in ("PNomConv", "VMppMin", "VMPPMax", "VAbsMax"))
        nb_mppt_field = (ond_elec.get("fields") or {}).get("NbMPPT") or {}
        nb_mppt = max(1, int(nb_mppt_field["value"]) if nb_mppt_field.get("status") == "declared" else 1)
    except ValueError as exc:
        issue("components", str(exc))
        return result

    try:
        beta_voc = _infer_beta_voc_pct(pan_fields, voc, beta_voc_pct_per_c, assumptions)
        beta_vmp = _finite(beta_vmp_pct_per_c, "beta_vmp_pct_per_c", minimum=-1.0, maximum=-0.05) if beta_vmp_pct_per_c is not None else DEFAULT_BETA_VMP_PCT_PER_C
        assumptions.append(f"beta_vmp={beta_vmp:.3f} %/degC ({'caller-supplied' if beta_vmp_pct_per_c is not None else 'standard c-Si default'})")
        alpha_isc = _finite(alpha_isc_pct_per_c, "alpha_isc_pct_per_c", minimum=0.0, maximum=0.3) if alpha_isc_pct_per_c is not None else DEFAULT_ALPHA_ISC_PCT_PER_C
        assumptions.append(f"alpha_isc={alpha_isc:.3f} %/degC ({'caller-supplied' if alpha_isc_pct_per_c is not None else 'standard c-Si default'})")
    except ValueError as exc:
        issue("coefficients", str(exc))
        return result

    assumptions.append(f"Design temperatures: min_ambient={t_min:g} degC, min_operating={t_op_min:g} degC, max_operating={t_op_max:g} degC")
    cold_voc, cold_vmp = round(max(voc, voc * (1.0 + (beta_voc / 100.0) * (t_min - 25.0))), 3), round(max(vmp, vmp * (1.0 + (beta_vmp / 100.0) * (t_op_min - 25.0))), 3)
    hot_vmp = round(min(vmp, vmp * (1.0 + (beta_vmp / 100.0) * (t_op_max - 25.0))), 3)
    if hot_vmp <= 0 or not (hot_vmp <= cold_vmp <= cold_voc):
        issue("voltage_envelope", "Derived temperature-adjusted voltage envelope is not monotonic/positive")
        return result
    mod_op_a = round(max(imp, imp * (1.0 + (alpha_isc / 100.0) * max(0.0, t_op_max - 25.0))), 3)
    mod_sc_a = round(max(isc, isc * (1.0 + (alpha_isc / 100.0) * max(0.0, t_op_max - 25.0))), 3)
    mppt_asymmetry_issues, declared_uniform_isc_a = _inspect_ond_mppt_limits(ond_text)
    if mppt_asymmetry_issues:
        for msg in mppt_asymmetry_issues:
            issue("mppt_limits", msg)
        return result

    idc_field = (ond_elec.get("fields") or {}).get("IDCMax") or {}
    if idc_field.get("status") == "declared" and float(idc_field["value"]) > 0:
        total_idc = float(idc_field["value"])
        mppt_op_a, mppt_sc_a = round(total_idc / nb_mppt, 3), round(round(total_idc / nb_mppt, 3) * 1.25, 3)
        assumptions.append(f"MPPT current limit derived from OND IDCMax={total_idc:g} A / NbMPPT={nb_mppt} (operating={mppt_op_a:g} A, short-circuit={mppt_sc_a:g} A)")
    else:
        v_ref_dc = max(vmpp_min, 0.5 * (vmpp_min + vmpp_max))
        mppt_op_a = round(max(mod_op_a * 2.0, (pnom_ac * 1000.0 * max_ratio) / (nb_mppt * v_ref_dc)), 3)
        mppt_sc_a = round(max(mod_sc_a * 2.0, mppt_op_a * 1.25), 3)
        assumptions.append(f"OND IDCMax not declared; estimated per-MPPT current limit from PNomConv={pnom_ac:g} kW at Vref={v_ref_dc:g} V (operating={mppt_op_a:g} A, short-circuit={mppt_sc_a:g} A)")
    if declared_uniform_isc_a is not None and declared_uniform_isc_a < mppt_sc_a:
        mppt_sc_a = round(declared_uniform_isc_a, 3)
        assumptions.append(f"Capped per-MPPT short-circuit limit to declared OND IscMax={mppt_sc_a:g} A")

    v_abs_limit = min(vabs_max, v_max_mod)
    min_series, max_series = max(1, math.ceil(vmpp_min / hot_vmp)), min(100, math.floor(min(vmpp_max / cold_vmp, v_abs_limit / cold_voc)))
    if min_series > max_series:
        issue("voltage_window", f"No feasible series count: hot Vmp requires >= {min_series} modules (VMppMin={vmpp_min} V) but cold Voc/Vmp caps at <= {max_series} modules (Vmax={v_abs_limit} V, VMPPMax={vmpp_max} V)")
        return result
    if (max_parallel := int(min(100, mppt_op_a / mod_op_a, mppt_sc_a / mod_sc_a))) < 1:
        issue("current_window", f"Module current ({mod_op_a} A / {mod_sc_a} A) exceeds MPPT limit ({mppt_op_a} A)")
        return result

    module_kw, mid_series, preferred_ratio, candidates = pnom_w / 1000.0, 0.5 * (min_series + max_series), min(max_ratio, max(min_ratio, 1.10)), []
    for series in range(min_series, max_series + 1):
        for parallel in range(1, max_parallel + 1):
            per_inv_kw = series * nb_mppt * parallel * module_kw
            ratio = per_inv_kw / pnom_ac
            if not (min_ratio <= ratio <= max_ratio + 1e-9):
                continue
            ideal = min(10000, max(1, target / per_inv_kw))
            for count in {math.floor(ideal), math.ceil(ideal)}:
                if count >= 1 and (err_pct := abs((installed_dc := per_inv_kw * count) - target) / target * 100.0) <= tol + 1e-9:
                    candidates.append(((round(err_pct, 4), round(abs(ratio - preferred_ratio), 4), count, abs(series - mid_series), series, parallel), series, parallel, count, installed_dc, ratio, err_pct))
    if not candidates:
        issue("sizing", f"No integral configuration meets target_dc_kw={target:g} kW (+/-{tol:g}%) with DC/AC in [{min_ratio:g}, {max_ratio:g}], series in [{min_series}, {max_series}], and parallel/MPPT in [1, {max_parallel}]")
        return result

    _, series, parallel, count, installed_dc, ratio, err_pct = min(candidates)
    strings, installed_ac = count * nb_mppt * parallel, count * pnom_ac
    capacity_payload = {
        "target_dc_kw": target, "min_dc_ac_ratio": min_ratio, "max_dc_ac_ratio": max_ratio, "capacity_tolerance_pct": tol,
        "voltage_standard": voltage_standard, "cold_voc_v": cold_voc, "cold_vmp_v": cold_vmp, "hot_vmp_v": hot_vmp,
        "module_max_operating_current_a": mod_op_a, "module_max_short_circuit_current_a": mod_sc_a,
        "mppt_max_operating_current_a": mppt_op_a, "mppt_max_short_circuit_current_a": mppt_sc_a,
        "design_min_temperature_c": t_min, "design_min_operating_temperature_c": t_op_min, "design_max_operating_temperature_c": t_op_max,
        "design_basis": f"Auto-estimated from {pan_path.name} ({pan_lib}) and {ond_path.name} ({ond_lib}): T=[{t_min:g}, {t_op_min:g}, {t_op_max:g}] degC, beta_voc={beta_voc:.2f}%/C, beta_vmp={beta_vmp:.2f}%/C, DC/AC in [{min_ratio:g}, {max_ratio:g}]",
    }
    result.update(status="ready", recommended={
        "series_modules": series, "parallel_strings_per_mppt": parallel, "mppt_per_inverter": nb_mppt,
        "inverter_count": count, "string_count": strings, "module_count": strings * series,
        "installed_dc_kw": round(installed_dc, 4), "installed_ac_kw": round(installed_ac, 4), "dc_ac_ratio": round(ratio, 4),
        "capacity_error_pct": round((installed_dc - target) / target * 100.0, 4),
        "feasible_series_range": [min_series, max_series], "max_parallel_strings_per_mppt": max_parallel,
        "voltage_checks": {"hot_string_vmp_v": round(series * hot_vmp, 2), "cold_string_vmp_v": round(series * cold_vmp, 2),
                           "cold_string_voc_v": round(series * cold_voc, 2), "inverter_vmpp_range_v": [vmpp_min, vmpp_max], "system_vmax_v": v_abs_limit},
    }, capacity=capacity_payload, components={"PAN": {"name": pan_path.name, "library": pan_lib}, "OND": {"name": ond_path.name, "library": ond_lib}})
    return result

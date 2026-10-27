"""Prepare the flat template-capacity input from declared manufacturer data.

Scope boundary
--------------
This module builds a *public linear design envelope*. It reads only declared
reference-point fields from an already-inspected PAN/OND electrical inventory
(`pvsyst_components.electrical_inventory`) and combines them with
caller-supplied manufacturer coefficients or with caller-supplied direct
manufacturer limits. The accepted relations are the public linear temperature
relations used for design-time voltage and current checks.

What it deliberately does not do
--------------------------------
* It never reads or converts serialized component coefficients such as
  ``muVocSpec``, ``muISC`` or ``muPmpReq``: their units are not declared inside
  the component file, and two observed files differ by three orders of
  magnitude for the same key.
* It never derives a Vmp temperature slope from a Pmax (gamma) coefficient.
* It never derives a per-MPPT current rating from a whole-converter current
  field such as ``IDCMax``, and never divides a converter rating by an MPPT
  count.
* It does not reproduce PVsyst's native one-diode temperature model, does not
  write the native project design temperatures, and does not certify a design.

Design temperatures are *module* temperatures: ``min_ambient_c`` is the site
absolute-voltage reference (IEC 62548-1 uses the lowest temperature ever
observed, not a single weather-file annual minimum), while the operating
temperatures are cell temperatures during usual winter/summer operation. A
weather file's annual extreme is never accepted as a design extreme.

Unit trust boundary: a declaration object accepts only ``value``, ``unit`` and
``source``. An omitted unit follows the field meaning (A for current limits and
A/degC or pct/degC for currents, V for the voltage envelope); an explicit unit
that does not match is refused with a specific ``.unit`` issue, and no unit is
ever converted or silently reinterpreted.

No file read, hash, SDK or native call is performed here.
"""
from __future__ import annotations

import math

from pvsyst_auto.engineering.sizing import NUMBERS as CAPACITY_NUMBERS


SCOPE = ("Public linear design envelope from declared manufacturer data; not native "
         "one-diode fitting, native design-temperature settings or design certification")

VOLTAGE_UNITS = ("pct_per_degC", "V_per_degC")
CURRENT_UNITS = ("pct_per_degC", "A_per_degC")
MIN_TEMPERATURE_C = -100.0
MAX_TEMPERATURE_C = 150.0
MIN_PCT_SLOPE = -2.0
MIN_VOLT_SLOPE_PER_DEGC = -1.0
MAX_TEXT = 2000

REQUEST_KEYS = frozenset((
    "manufacturer_model", "target_dc_kw", "max_dc_ac_ratio", "voltage_standard",
    "capacity_tolerance_pct", "temperatures", "envelope", "coefficients",
    "module_limits", "mppt_limits", "effective_irradiance", "design_basis_extra",
))
TEMPERATURE_KEYS = frozenset((
    "min_ambient_c", "min_ambient_basis", "min_module_operating_c",
    "max_module_operating_c", "annual_min_ambient_c",
))
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
    "module_max_short_circuit_current_a": ("ISC_ref * (max_irradiance_w_m2 / GRef) * rear_gain_factor "
                                          "* max(1 + alpha_isc * (T - TRef)) over the operating endpoints and TRef"),
    "module_max_operating_current_a": ("Imp_ref * (max_irradiance_w_m2 / GRef) * rear_gain_factor "
                                       "* max(1 + alpha_imp * (T - TRef)) over the operating endpoints and TRef"),
}


def _issue(result, field, message):
    result["issues"].append({"field": field, "message": message})


def _warn(result, field, message):
    result["warnings"].append({"field": field, "message": message})


def _is_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _is_text(value):
    return isinstance(value, str) and bool(value.strip()) and value.isprintable() and len(value) <= MAX_TEXT


DECLARATION_KEYS = frozenset(("value", "unit", "source"))
LIMIT_UNITS = ("A",)
ENVELOPE_UNITS = ("V",)


def _declaration(declaration, field, result):
    """Split a bare number or a {value, unit, source} object.

    Unknown keys are rejected here, in the plain backend too: no framework is
    assumed to block extra keys. A bare number carries no declared unit.
    """
    if not isinstance(declaration, dict):
        return declaration, None, None
    unknown = declaration.keys() - DECLARATION_KEYS
    if unknown:
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
        if required:
            _issue(result, field, "Declare the manufacturer/source of this value")
        else:
            _warn(result, field, "Value declared without a source; traceability is incomplete")
        return None
    if not _is_text(source):
        _issue(result, field, "Source must be printable text up to 2000 characters")
        return None
    return source.strip()


def _coefficient(declaration, field, result, units, reference, negative):
    """Return a validated coefficient with its fractional slope per degC."""
    if declaration is None:
        return None
    before = len(result["issues"])
    value, unit, source = _declaration(declaration, field, result)
    if len(result["issues"]) != before:
        return None
    value = _check_number(value, field, result, positive=False)
    if value is None:
        return None
    if unit not in units:
        _issue(result, field + ".unit", "Declare the manufacturer unit explicitly ("
               + " or ".join(units) + "); the unit of a serialized component coefficient is never assumed")
        return None
    _check_source(source, field + ".source", result, required=True)
    if negative and value > 0:
        _issue(result, field, "Reject a non-physical positive temperature coefficient for this quantity")
        return None
    floor = MIN_PCT_SLOPE if unit == "pct_per_degC" else (MIN_VOLT_SLOPE_PER_DEGC if unit == "V_per_degC" else -reference)
    if value < floor:
        _issue(result, field, "Reject a non-physical magnitude below " + str(floor) + " per degC")
        return None
    fraction = value / 100.0 if unit == "pct_per_degC" else value / reference
    return {"value": value, "unit": unit, "source": source.strip() if _is_text(source) else None,
            "fraction": fraction}


def _max_temperature_factor(fraction, tref, oper_min, oper_max):
    """Largest linear temperature factor over both operating endpoints and TRef.

    The sign of the coefficient decides which endpoint yields the maximum; no
    quantity is assumed to be monotonic in a fixed direction.
    """
    temperatures = [oper_min, oper_max]
    if oper_min <= tref <= oper_max:
        temperatures.append(tref)
    return max(1.0 + fraction * (value - tref) for value in temperatures)


def _declared_limit(container, name, field, result):
    """Read a directly declared manufacturer current limit, if present.

    The declared unit, when given, must be amperes; no conversion is inferred
    and a mismatched unit is refused instead of being silently reinterpreted.
    """
    if not isinstance(container, dict):
        return None
    if name not in container:
        return None
    before = len(result["issues"])
    value, unit, source = _declaration(container[name], field, result)
    if len(result["issues"]) != before:
        return None
    if unit is not None and unit not in LIMIT_UNITS:
        _issue(result, field + ".unit", "Declare the current unit as A; no unit conversion is inferred")
        return None
    value = _check_number(value, field + ".value", result)
    if value is None:
        return None
    _check_source(source, field + ".source", result, required=True)
    return value


def _read_declared(electrical, name, field, result, positive=True):
    item = (electrical or {}).get("fields", {}).get(name)
    if not isinstance(item, dict):
        _issue(result, field, "Required electrical field is not readable")
        return None
    if item.get("status") != "declared":
        _issue(result, field, "Field status is " + repr(item.get("status"))
               + "; only a declared value is used and no value is inferred")
        return None
    return _check_number(item.get("value"), field, result, positive=positive)


def prepare_design_envelope(module_electrical, inverter_electrical, request):
    """Build the template-capacity input from declared manufacturer data.

    ``module_electrical`` and ``inverter_electrical`` are the read-only
    inventories returned by ``pvsyst_components.electrical_inventory`` for the
    selected PAN and OND. The caller is responsible for having matched those
    components to ``request['manufacturer_model']``.

    Returns ``status`` (ready/blocked), ``capacity`` (the flat input consumed by
    the template capacity planner when ready), ``issues`` (missing or
    inadmissible design inputs), ``warnings``, ``provenance`` (method, formula
    text, reference-point values and every declared source) and ``scope``.
    """
    result = {"status": "blocked", "capacity": None, "issues": [], "warnings": [],
              "provenance": {}, "scope": SCOPE, "envelope_mode": None}
    if not isinstance(request, dict):
        _issue(result, "request", "Provide the design-envelope request object")
        return result
    for key in sorted(request.keys() - REQUEST_KEYS):
        _issue(result, str(key), "Unknown design-envelope input")

    # ---- selected component identity (matching is confirmed by the caller) ----
    manufacturer_model = request.get("manufacturer_model")
    if not _is_text(manufacturer_model):
        _issue(result, "manufacturer_model", "Declare the module/inverter manufacturer and model this envelope describes")

    # ---- flat capacity inputs that cannot be derived ----
    target_dc_kw = _check_number(request.get("target_dc_kw"), "target_dc_kw", result) \
        if request.get("target_dc_kw") is not None else None
    if target_dc_kw is None and request.get("target_dc_kw") is None:
        _issue(result, "target_dc_kw", "Required design input is missing; no value is inferred")
    max_dc_ac_ratio = _check_number(request.get("max_dc_ac_ratio"), "max_dc_ac_ratio", result) \
        if request.get("max_dc_ac_ratio") is not None else None
    if max_dc_ac_ratio is None and request.get("max_dc_ac_ratio") is None:
        _issue(result, "max_dc_ac_ratio", "Required design input is missing; no value is inferred")
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

    # ---- declared STC reference point of the selected module ----
    voc_ref = _read_declared(module_electrical, "Voc", "module.Voc", result)
    vmp_ref = _read_declared(module_electrical, "Vmp", "module.Vmp", result)
    imp_ref = _read_declared(module_electrical, "Imp", "module.Imp", result)
    isc_ref = _read_declared(module_electrical, "ISC", "module.ISC", result)
    gref = _read_declared(module_electrical, "GRef", "module.GRef", result)
    tref = _read_declared(module_electrical, "TRef", "module.TRef", result, positive=False)
    pnom_ref = _read_declared(module_electrical, "PNom", "module.PNom", result)
    if gref is not None and abs(gref - 1000.0) > 1e-9:
        _warn(result, "module.GRef", "The module reference irradiance is not 1000 W/m2; template capacity planning requires a 1000 W/m2 reference module")

    inverter_reference = {}
    for name in INVERTER_REFERENCE:
        item = (inverter_electrical or {}).get("fields", {}).get(name)
        if isinstance(item, dict):
            inverter_reference[name] = {"value": item.get("value"), "unit": item.get("unit"),
                                        "status": item.get("status"), "path": item.get("path")}
    for name in ("VMppMin", "VMPPMax", "VAbsMax"):
        item = inverter_reference.get(name)
        if not item or item.get("status") != "declared":
            _warn(result, "inverter." + name,
                  "The inverter MPP/absolute voltage window is not declared; the capacity planner reads it from the OND and refuses the variant")
    if not inverter_reference.get("IDCMax") or inverter_reference["IDCMax"].get("status") != "declared":
        _warn(result, "inverter.IDCMax",
              "IDCMax is not a positive declared ceiling and is never used as a per-MPPT current rating")
    if voltage_standard in ("IEC", "UL"):
        limit_item = (module_electrical or {}).get("fields", {}).get("VMax" + voltage_standard)
        if not isinstance(limit_item, dict) or limit_item.get("status") != "declared":
            _issue(result, "module.VMax" + voltage_standard,
                   "The module-system voltage rating for the selected standard is not a declared positive value; no envelope is prepared without it")

    # ---- design temperatures (module temperatures, never an annual extreme) ----
    temperatures = request.get("temperatures")
    tmin = tmin_op = tmax_op = None
    basis = None
    if not isinstance(temperatures, dict):
        _issue(result, "temperatures", "Provide the design temperature object")
    else:
        for key in sorted(temperatures.keys() - TEMPERATURE_KEYS):
            _issue(result, "temperatures." + str(key), "Unknown design temperature input")
        for name, field in (("min_ambient_c", "temperatures.min_ambient_c"),
                            ("min_module_operating_c", "temperatures.min_module_operating_c"),
                            ("max_module_operating_c", "temperatures.max_module_operating_c")):
            value = temperatures.get(name)
            if value is None:
                _issue(result, field, "Required design input is missing; no value is inferred")
            else:
                value = _check_number(value, field, result, positive=False)
                if value is not None:
                    if name == "min_ambient_c":
                        tmin = value
                    elif name == "min_module_operating_c":
                        tmin_op = value
                    else:
                        tmax_op = value
        basis = temperatures.get("min_ambient_basis")
        if basis is None:
            _issue(result, "temperatures.min_ambient_basis",
                   "Declare the source/method of the design minimum ambient temperature (IEC 62548-1 uses the lowest temperature ever observed)")
        elif not _is_text(basis):
            _issue(result, "temperatures.min_ambient_basis", "Use printable text up to 2000 characters")
            basis = None
        annual = temperatures.get("annual_min_ambient_c")
        if annual is not None:
            annual = _check_number(annual, "temperatures.annual_min_ambient_c", result, positive=False)
            if annual is not None and tmin is not None and abs(annual - tmin) < 1e-9:
                _warn(result, "temperatures.annual_min_ambient_c",
                      "A weather-file annual minimum equals the declared design extreme; a single-year minimum is not a design extreme")
    if tmin is not None and tmin_op is not None and tmax_op is not None:
        if not MIN_TEMPERATURE_C <= tmin <= tmin_op <= tmax_op <= MAX_TEMPERATURE_C:
            _issue(result, "temperatures",
                   "Require -100 <= min ambient <= min module operating <= max module operating <= 150 degC")
        if abs(tmin - tmin_op) < 1e-9:
            _warn(result, "temperatures.min_module_operating_c",
                  "The minimum module operating temperature equals the design minimum ambient temperature; operating temperatures are cell temperatures, not ambient temperatures")

    # ---- voltage envelope: direct manufacturer values, else declared coefficients ----
    envelope = request.get("envelope")
    direct_voltage = {}
    if envelope is None:
        envelope = {}
    if not isinstance(envelope, dict):
        _issue(result, "envelope", "Provide direct voltage values as an object")
        envelope = {}
    else:
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
                _issue(result, "envelope." + name + ".unit",
                       "Declare the voltage unit as V; no unit conversion is inferred")
                continue
            value = _check_number(value, "envelope." + name, result)
            if value is not None:
                _check_source(source, "envelope." + name + ".source", result, required=False)
                direct_voltage[name] = value

    coefficients = request.get("coefficients")
    if coefficients is None:
        coefficients = {}
    if not isinstance(coefficients, dict):
        _issue(result, "coefficients", "Provide coefficients as an object")
        coefficients = {}
    else:
        for key in sorted(coefficients.keys() - set(COEFFICIENT_KEYS)):
            _issue(result, "coefficients." + str(key), "Unknown coefficient input")
    beta_voc = _coefficient(coefficients.get("beta_voc"), "coefficients.beta_voc", result,
                            VOLTAGE_UNITS, voc_ref if voc_ref else 1.0, True)
    beta_vmp = _coefficient(coefficients.get("beta_vmp"), "coefficients.beta_vmp", result,
                            VOLTAGE_UNITS, vmp_ref if vmp_ref else 1.0, True)
    alpha_isc = _coefficient(coefficients.get("alpha_isc"), "coefficients.alpha_isc", result,
                             CURRENT_UNITS, isc_ref if isc_ref else 1.0, False)
    alpha_imp = _coefficient(coefficients.get("alpha_imp"), "coefficients.alpha_imp", result,
                             CURRENT_UNITS, imp_ref if imp_ref else 1.0, False)

    if "cold_voc_v" not in direct_voltage and beta_voc is None:
        _issue(result, "coefficients.beta_voc",
               "Supply beta_voc with an explicit unit, or provide envelope.cold_voc_v from the manufacturer")
    if "cold_vmp_v" not in direct_voltage and beta_vmp is None:
        _issue(result, "coefficients.beta_vmp",
               "Supply beta_vmp with an explicit unit, or provide envelope.cold_vmp_v from the manufacturer; a Pmax coefficient is not a Vmp coefficient")
    if "hot_vmp_v" not in direct_voltage and beta_vmp is None:
        _issue(result, "coefficients.beta_vmp",
               "Supply beta_vmp with an explicit unit, or provide envelope.hot_vmp_v from the manufacturer; a Pmax coefficient is not a Vmp coefficient")

    # ---- current envelope: direct manufacturer limits first ----
    module_limits = request.get("module_limits")
    mppt_limits = request.get("mppt_limits")
    for container, prefix in ((module_limits, "module_limits"), (mppt_limits, "mppt_limits")):
        if container is not None and not isinstance(container, dict):
            _issue(result, prefix, "Provide the manufacturer limit object")
        elif isinstance(container, dict):
            for key in sorted(container.keys() - set(LIMIT_KEYS)):
                _issue(result, prefix + "." + str(key), "Unknown manufacturer limit input")
    module_short = _declared_limit(module_limits, "max_short_circuit_current_a",
                                   "module_limits.max_short_circuit_current_a", result)
    module_operating = _declared_limit(module_limits, "max_operating_current_a",
                                       "module_limits.max_operating_current_a", result)
    mppt_operating = _declared_limit(mppt_limits, "max_operating_current_a",
                                     "mppt_limits.max_operating_current_a", result)
    mppt_short = _declared_limit(mppt_limits, "max_short_circuit_current_a",
                                 "mppt_limits.max_short_circuit_current_a", result)
    if not isinstance(module_limits, dict) or "max_short_circuit_current_a" not in module_limits:
        if alpha_isc is None:
            _issue(result, "module_limits.max_short_circuit_current_a",
                   "Provide the manufacturer maximum short-circuit current directly, or supply alpha_isc with an explicit unit")
    if not isinstance(module_limits, dict) or "max_operating_current_a" not in module_limits:
        if alpha_imp is None:
            _issue(result, "module_limits.max_operating_current_a",
                   "Provide the manufacturer maximum operating current directly, or supply alpha_imp with an explicit unit; the Isc coefficient is not substituted for Imp")
    if mppt_operating is None and (not isinstance(mppt_limits, dict) or "max_operating_current_a" not in mppt_limits):
        _issue(result, "mppt_limits.max_operating_current_a",
               "Declare the per-MPPT operating current rating from the inverter datasheet; a whole-converter current field such as IDCMax is not a substitute")
    if mppt_short is None and (not isinstance(mppt_limits, dict) or "max_short_circuit_current_a" not in mppt_limits):
        _issue(result, "mppt_limits.max_short_circuit_current_a",
               "Declare the per-MPPT short-circuit current rating from the inverter datasheet; a whole-converter current field such as IDCMax is not a substitute")

    # ---- effective irradiance: required only when a current is coefficient-derived ----
    irradiance = request.get("effective_irradiance")
    if irradiance is None:
        irradiance = {}
    if not isinstance(irradiance, dict):
        _issue(result, "effective_irradiance", "Provide the effective irradiance object")
        irradiance = {}
    else:
        for key in sorted(irradiance.keys() - IRRADIANCE_KEYS):
            _issue(result, "effective_irradiance." + str(key), "Unknown effective irradiance input")
    e_max = rear_gain = None
    declares_short = isinstance(module_limits, dict) and "max_short_circuit_current_a" in module_limits
    declares_operating = isinstance(module_limits, dict) and "max_operating_current_a" in module_limits
    needs_irradiance = not (declares_short and declares_operating)
    if needs_irradiance:
        if "max_irradiance_w_m2" not in irradiance:
            _issue(result, "effective_irradiance.max_irradiance_w_m2",
                   "Declare the maximum effective irradiance for this design; the module reference irradiance is not a maximum-irradiance envelope")
        else:
            e_max = _check_number(irradiance.get("max_irradiance_w_m2"),
                                  "effective_irradiance.max_irradiance_w_m2", result)
            if e_max is not None and gref is not None and e_max < gref:
                _warn(result, "effective_irradiance.max_irradiance_w_m2",
                      "The declared maximum effective irradiance is below the module reference irradiance; the current envelope may be underestimated")
        if "rear_gain_factor" not in irradiance:
            _issue(result, "effective_irradiance.rear_gain_factor",
                   "Declare the rear/bifacial gain factor explicitly (use 1.0 for a monofacial plane); a gain is never assumed")
        else:
            rear_gain = _check_number(irradiance.get("rear_gain_factor"),
                                      "effective_irradiance.rear_gain_factor", result)
        _check_source(irradiance.get("source"), "effective_irradiance.source", result, required=True)
    elif any(name in irradiance for name in ("max_irradiance_w_m2", "rear_gain_factor")):
        _warn(result, "effective_irradiance",
              "Irradiance inputs are declared but not used: both module current limits are direct manufacturer values")

    if result["issues"]:
        return result
    if needs_irradiance and (e_max is None or rear_gain is None):
        _issue(result, "effective_irradiance",
               "A coefficient-derived current envelope requires an explicit maximum irradiance and rear-gain factor")
        return result

    # ---- computation ----
    if "cold_voc_v" in direct_voltage:
        cold_voc, cold_voc_source = direct_voltage["cold_voc_v"], "explicit envelope"
    else:
        cold_voc = voc_ref * (1.0 + beta_voc["fraction"] * (tmin - tref))
        cold_voc_source = "beta_voc from " + str(beta_voc["source"])
    if "cold_vmp_v" in direct_voltage:
        cold_vmp, cold_vmp_source = direct_voltage["cold_vmp_v"], "explicit envelope"
    else:
        cold_vmp = vmp_ref * (1.0 + beta_vmp["fraction"] * (tmin_op - tref))
        cold_vmp_source = "beta_vmp from " + str(beta_vmp["source"])
    if "hot_vmp_v" in direct_voltage:
        hot_vmp, hot_vmp_source = direct_voltage["hot_vmp_v"], "explicit envelope"
    else:
        hot_vmp = vmp_ref * (1.0 + beta_vmp["fraction"] * (tmax_op - tref))
        hot_vmp_source = "beta_vmp from " + str(beta_vmp["source"])

    irradiance_factor = e_max / gref if e_max is not None else None
    if module_short is None:
        module_short = (isc_ref * irradiance_factor * rear_gain
                        * _max_temperature_factor(alpha_isc["fraction"], tref, tmin_op, tmax_op))
        module_short_source = "alpha_isc from " + str(alpha_isc["source"])
    else:
        module_short_source = "direct manufacturer limit"
    if module_operating is None:
        # The coefficient sign selects the maximum-temperature endpoint; the
        # short-circuit coefficient is never substituted for Imp.
        module_operating = (imp_ref * irradiance_factor * rear_gain
                           * _max_temperature_factor(alpha_imp["fraction"], tref, tmin_op, tmax_op))
        module_operating_source = "alpha_imp from " + str(alpha_imp["source"])
    else:
        module_operating_source = "direct manufacturer limit"

    if not hot_vmp <= cold_vmp <= cold_voc:
        _issue(result, "envelope", "Require hot Vmp <= cold operating Vmp <= minimum-temperature Voc")
    if module_short < module_operating:
        _issue(result, "module_limits.max_short_circuit_current_a", "The short-circuit envelope must cover the operating-current envelope")
    if mppt_short < mppt_operating:
        _issue(result, "mppt_limits.max_short_circuit_current_a", "The short-circuit rating must cover the operating-current rating")
    if module_operating < imp_ref:
        _issue(result, "module_limits.max_operating_current_a", "The declared module operating-current envelope must cover the PAN reference Imp")
    if module_short < isc_ref:
        _issue(result, "module_limits.max_short_circuit_current_a", "The declared module short-circuit envelope must cover the PAN reference Isc")
    if mppt_operating < module_operating:
        _issue(result, "mppt_limits.max_operating_current_a", "The per-MPPT operating rating must cover the module operating envelope")
    if mppt_short < module_short:
        _issue(result, "mppt_limits.max_short_circuit_current_a", "The per-MPPT short-circuit rating must cover the module short-circuit envelope")

    # Each reference-point check uses the band its envelope was computed over: cold Vmp from the
    # minimum operating temperature, cold Voc from the minimum ambient temperature.
    if tmin_op <= tref <= tmax_op:
        if not hot_vmp <= vmp_ref <= cold_vmp:
            _issue(result, "envelope", "The computed operating-voltage envelope must cover the PAN reference Vmp while TRef is inside the operating band")
    else:
        _warn(result, "temperatures",
              "The module reference temperature is outside the declared operating band; the Vmp coverage check of the reference point is skipped")
    if tmin <= tref <= tmax_op:
        if cold_voc < voc_ref:
            _issue(result, "envelope.cold_voc_v", "The computed maximum Voc must cover the PAN reference Voc")
    else:
        _warn(result, "temperatures",
              "The module reference temperature is outside the declared temperature range; the Voc coverage check of the reference point is skipped")
    if result["issues"]:
        return result

    mode = "explicit" if all(name in direct_voltage for name in ENVELOPE_KEYS) else (
        "coefficients" if not direct_voltage else "mixed")

    sources = {"min_ambient_basis": basis, "cold_voc_v": cold_voc_source, "cold_vmp_v": cold_vmp_source,
               "hot_vmp_v": hot_vmp_source, "module_max_operating_current_a": module_operating_source,
               "module_max_short_circuit_current_a": module_short_source,
               "mppt_max_operating_current_a": "direct manufacturer limit",
               "mppt_max_short_circuit_current_a": "direct manufacturer limit",
               "max_irradiance_w_m2": irradiance.get("max_irradiance_w_m2"),
               "rear_gain_factor": irradiance.get("rear_gain_factor"),
               "irradiance_source": irradiance.get("source")}
    irradiance_text = ("not used: both module current limits are direct manufacturer values" if e_max is None
                       else "E_max=" + format(e_max, ".6g") + " W/m2; rear_gain=" + format(rear_gain, ".6g"))
    basis_text = ("public linear envelope for " + str(manufacturer_model) + "; mode=" + mode
                  + "; Tmin_basis=" + str(basis)
                  + "; voltages=" + "; ".join(name + ": " + FORMULAS[name] for name in ENVELOPE_KEYS)
                  + "; " + irradiance_text
                  + "; module_current=" + module_operating_source + "/" + module_short_source
                  + "; mppt_current=direct manufacturer per-MPPT limits"
                  + "; method is a public linear temperature relation, not the native one-diode fitting")
    if design_basis_extra:
        basis_text = basis_text + "; " + design_basis_extra.strip()
    if len(basis_text) > MAX_TEXT:
        basis_text = basis_text[:MAX_TEXT]
        _warn(result, "design_basis", "The composed design basis was truncated to 2000 characters")

    capacity = {
        "target_dc_kw": target_dc_kw,
        "max_dc_ac_ratio": max_dc_ac_ratio,
        "capacity_tolerance_pct": tolerance,
        "voltage_standard": voltage_standard,
        "cold_voc_v": cold_voc,
        "hot_vmp_v": hot_vmp,
        "cold_vmp_v": cold_vmp,
        "module_max_operating_current_a": module_operating,
        "module_max_short_circuit_current_a": module_short,
        "mppt_max_operating_current_a": mppt_operating,
        "mppt_max_short_circuit_current_a": mppt_short,
        "design_min_temperature_c": tmin,
        "design_min_operating_temperature_c": tmin_op,
        "design_max_operating_temperature_c": tmax_op,
        "design_basis": basis_text,
    }
    missing = [name for name in CAPACITY_NUMBERS if name not in capacity]
    if missing:
        _issue(result, "capacity", "The capacity contract is incomplete: " + ", ".join(missing))
        return result
    for name in CAPACITY_NUMBERS:
        value = capacity[name]
        if not _is_number(value) or (not name.startswith("design_") and value <= 0):
            _issue(result, "capacity." + name, "Require a finite strict numeric value; design magnitudes must be positive")
    if not _is_text(capacity["design_basis"]):
        _issue(result, "capacity.design_basis", "Provide a printable basis up to 2000 characters")
    if result["issues"]:
        return result

    unused = sorted({item.get("field") for item in (module_electrical or {}).get("unverified", [])
                     if item.get("field")}) \
        + sorted({item.get("field") for item in (inverter_electrical or {}).get("unverified", [])
                  if item.get("field")})
    result.update(
        status="ready", capacity=capacity, envelope_mode=mode,
        provenance={
            "method": "public linear temperature envelope",
            "scope": SCOPE,
            "envelope_mode": mode,
            "selected_component": manufacturer_model,
            "module_reference": {name: module_electrical["fields"].get(name) for name in MODULE_STC},
            "inverter_reference": inverter_reference,
            "formulas": {name: FORMULAS[name] for name in ENVELOPE_KEYS + tuple(
                name for name in FORMULAS if name.startswith("module_"))},
            "sources": sources,
            "unused_serialized_fields": unused,
        })
    return result

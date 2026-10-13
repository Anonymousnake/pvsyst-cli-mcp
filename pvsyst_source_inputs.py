"""Bind AI-extracted numeric design values to caller-declared source text.

This is a source-consistency checker, not a guaranteed PDF/table reader. Each
declaration carries the excerpt it was extracted from plus the exact numeric
token and unit token found there. The module verifies that the tokens occur in
the excerpt as standalone tokens, that the excerpt occurs in the declared text
as a whitespace-normalized contiguous fragment, that the unit token maps to the
declared unit through an explicit literal whitelist, and that the numeric token
equals the declared value without rescaling. Bound values are assembled into the
existing design request and handed to ``prepare_design_envelope``.

Missing inputs return a draft for the caller/AI to complete; nothing is inferred
and no exception is raised. The returned text never includes a full source text.

No fetching, PDF parsing, hash, file IO or SDK call is performed here.
"""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation

from pvsyst_design_envelope import prepare_design_envelope


SCOPE = ("Source-consistency checker over caller-declared text only: excerpt, numeric token and unit "
         "token agree with the declared value. Document authenticity, manufacturer model fitting, page "
         "layout, regional climate extremes and component identity are not certified, and a declared "
         "locator is never fetched.")

SOURCE_KEYS = ("kind", "title", "locator", "text", "model")
SOURCE_KINDS = ("manufacturer", "site", "assumption")
DECLARATION_KEYS = ("field", "value", "unit", "source_id", "excerpt", "source_value_text",
                    "source_unit_text", "page")
DESIGN_KEYS = ("target_dc_kw", "max_dc_ac_ratio", "capacity_tolerance_pct", "voltage_standard",
               "design_basis_extra")
DESIGN_TARGETS = ("target_dc_kw", "max_dc_ac_ratio", "capacity_tolerance_pct")
BARE_VALUE_BUCKETS = ("temperatures", "effective_irradiance")
SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}")

MAX_SOURCES = 16
MAX_SOURCE_TEXT_CHARS = 2 * 1024 * 1024
MAX_DECLARATIONS = 32
MAX_EXCERPT_CHARS = 4000
MAX_META_CHARS = 2000
MAX_IDENTITY_CHARS = 200
LABEL_LIMIT = 2000

DECIMAL_LITERAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
# A number may be written tightly against its unit (-0.25%/℃, 30A), so the
# right boundary only forbids a real continuation of the same literal. The left
# boundary also rejects a letter or a typographic minus, so '1e16', 'x16' and
# '−16' never yield 16.
NUMBER_EDGE_LEFT = re.compile(r"[0-9A-Za-z_.+-]")
UNICODE_MINUS = "\u2212\u2010\u2011\u2012\u2013\u2014\u2015\ufe63\uff0d"
UNIT_EDGE_LEFT = re.compile(r"[A-Za-z]")
UNIT_EDGE_RIGHT = re.compile(r"[0-9A-Za-z]")
# Model matching is stricter: a hyphen also keeps S6 from matching S6-EH...
MODEL_EDGE = re.compile(r"[0-9A-Za-z_./%²°\-]")

# Explicit literal aliases only: nothing is rescaled and no unit is inferred.
# mA is not A, kV is not V, % is not ratio.
UNIT_FORMS = {
    "degc": "degC", "°c": "degC",
    "v": "V",
    "a": "A",
    "w": "W",
    "w/m2": "W/m2",
    "kw": "kW",
    "ratio": "ratio",
    "%": "%", "pct": "%", "percent": "%",
    "pct_per_degc": "pct_per_degC", "%/°c": "pct_per_degC", "%/degc": "pct_per_degC", "%/c": "pct_per_degC",
    "v_per_degc": "V_per_degC", "v/°c": "V_per_degC", "v/degc": "V_per_degC",
    "a_per_degc": "A_per_degC", "a/°c": "A_per_degC", "a/degc": "A_per_degC",
}

FIELD_SPECS = {
    "temperatures.min_ambient_c": (("degC",), ("temperatures", "min_ambient_c")),
    "temperatures.min_module_operating_c": (("degC",), ("temperatures", "min_module_operating_c")),
    "temperatures.max_module_operating_c": (("degC",), ("temperatures", "max_module_operating_c")),
    "temperatures.annual_min_ambient_c": (("degC",), ("temperatures", "annual_min_ambient_c")),
    "envelope.cold_voc_v": (("V",), ("envelope", "cold_voc_v")),
    "envelope.cold_vmp_v": (("V",), ("envelope", "cold_vmp_v")),
    "envelope.hot_vmp_v": (("V",), ("envelope", "hot_vmp_v")),
    "coefficients.beta_voc": (("pct_per_degC", "V_per_degC"), ("coefficients", "beta_voc")),
    "coefficients.beta_vmp": (("pct_per_degC", "V_per_degC"), ("coefficients", "beta_vmp")),
    "coefficients.alpha_isc": (("pct_per_degC", "A_per_degC"), ("coefficients", "alpha_isc")),
    "coefficients.alpha_imp": (("pct_per_degC", "A_per_degC"), ("coefficients", "alpha_imp")),
    "module_limits.max_operating_current_a": (("A",), ("module_limits", "max_operating_current_a")),
    "module_limits.max_short_circuit_current_a": (("A",), ("module_limits", "max_short_circuit_current_a")),
    "mppt_limits.max_operating_current_a": (("A",), ("mppt_limits", "max_operating_current_a")),
    "mppt_limits.max_short_circuit_current_a": (("A",), ("mppt_limits", "max_short_circuit_current_a")),
    "effective_irradiance.max_irradiance_w_m2": (("W/m2",), ("effective_irradiance", "max_irradiance_w_m2")),
    "effective_irradiance.rear_gain_factor": (("ratio",), ("effective_irradiance", "rear_gain_factor")),
    "target_dc_kw": (("kW",), ("", "target_dc_kw")),
    "max_dc_ac_ratio": (("ratio",), ("", "max_dc_ac_ratio")),
    "capacity_tolerance_pct": (("%",), ("", "capacity_tolerance_pct")),
}


def _issue(result, field, message):
    result["issues"].append({"field": field, "message": message})


def _warn(result, field, message):
    result["warnings"].append({"field": field, "message": message})


def _report_unknown_keys(container, allowed, prefix, result, label="Unknown key"):
    """Report unknown keys without assuming the keys are sortable strings."""
    unknown = container.keys() - allowed
    text_keys = sorted(key for key in unknown if isinstance(key, str))
    for key in text_keys:
        _issue(result, prefix + "." + key, label)
    if len(text_keys) != len(unknown):
        _issue(result, prefix, "Every object key must be a printable string")


def _is_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _is_text(value, limit=MAX_META_CHARS):
    return isinstance(value, str) and bool(value.strip()) and value.isprintable() and len(value) <= limit


def _norm_ws(value):
    return " ".join(str(value).split())


def _normalize_unit(token):
    if token is None:
        return None
    plain = str(token).strip().replace("²", "2").replace("℃", "°C")
    return UNIT_FORMS.get(plain.casefold(), plain)


def _unit_matches(expected, token):
    """A unit token matches only by explicit literal alias; nothing is converted.

    An empty unit token carries no evidence and is never accepted as
    dimensionless: a ratio must be written out in the cited text.
    """
    normalized = _normalize_unit(token)
    if not normalized:
        return False
    return normalized == expected


def _contains_token(text, token, edge):
    """True when token occurs inside text without touching an edge character."""
    if not token:
        return False
    start = 0
    while True:
        index = text.find(token, start)
        if index < 0:
            return False
        left = text[index - 1] if index else ""
        right = text[index + len(token)] if index + len(token) < len(text) else ""
        if not edge.match(left) and not edge.match(right):
            return True
        start = index + 1


def _continues_number_right(text, index):
    """True only when the character really continues the same numeric literal."""
    if index >= len(text):
        return False
    character = text[index]
    if "0" <= character <= "9" or character == ".":
        return True
    if character in "eE":
        tail = text[index + 1:index + 2]
        if tail in ("+", "-"):
            tail = text[index + 2:index + 3]
        return "0" <= tail <= "9"
    return False


def _number_token_positions(text, token):
    """End offsets of every occurrence that is not part of a longer literal."""
    ends = []
    start = 0
    while True:
        index = text.find(token, start)
        if index < 0:
            return ends
        left = text[index - 1] if index else ""
        if (not NUMBER_EDGE_LEFT.match(left) and (not left or left not in UNICODE_MINUS)
                and not _continues_number_right(text, index + len(token))):
            ends.append(index + len(token))
        start = index + 1


def _contains_number_token(text, token):
    """A numeric token never continues an adjacent literal: 16 is not taken from
    16.0, 160 or -16, while a tightly written unit may follow it."""
    return bool(_number_token_positions(text, token))


def _unit_token_follows_number(text, value_token, unit_token):
    """The unit must be its own word and written right after the cited number.

    '-0.25%/℃' and '30 A' both pass; 'mA' never yields 'A' and 'kV' never
    yields 'V', because those units start with a letter.
    """
    if not unit_token:
        return False
    starts = []
    start = 0
    while True:
        index = text.find(unit_token, start)
        if index < 0:
            break
        left = text[index - 1] if index else ""
        right = text[index + len(unit_token)] if index + len(unit_token) < len(text) else ""
        if not UNIT_EDGE_LEFT.match(left) and not UNIT_EDGE_RIGHT.match(right):
            starts.append(index)
        start = index + 1
    if not starts:
        return False
    ends = _number_token_positions(text, value_token)
    return any(unit_start >= end and not text[end:unit_start].strip()
               for unit_start in starts for end in ends)


def _contains_model(text, model):
    """Model matching uses a stricter boundary, so S6 never matches S6-EH..."""
    return _contains_token(text.casefold(), str(model).strip().casefold(), MODEL_EDGE)


def _decimal_of_value(value):
    """Exact decimal of the declared number without passing through float()."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(str(value))


def _has_control_characters(text):
    return any(ord(character) < 32 and character not in "\n\r\t" or ord(character) == 127
               for character in text)


def _record(field, source_id, source, page, excerpt, value, unit, value_token, unit_token):
    """A citation record that keeps everything needed to replay the binding."""
    text = _norm_ws(source.get("text"))
    return {"field": field, "sourceID": source_id, "title": source.get("title"),
            "locator": source.get("locator"), "model": source.get("model"), "page": page,
            "excerpt": excerpt, "value": value, "unit": unit,
            "source_value_text": value_token, "source_unit_text": unit_token,
            "source_text_head": text[:160], "kind": source.get("kind"), "reference_match": False}


def _source_label(source_id, source, page, excerpt):
    """'sourceID title locator page excerpt' as a bounded, single-line citation."""
    parts = [str(source_id)]
    for name in ("title", "locator"):
        if source.get(name):
            parts.append(_norm_ws(source[name]))
    if page is not None:
        parts.append("page " + str(page))
    parts.append(excerpt)
    return " ".join(parts)[:LABEL_LIMIT]


def prepare_sourced_design(module_electrical, inverter_electrical, *, manufacturer_model,
                           sources, declarations, design=None):
    """Bind declared excerpts to source text, then build and validate the request.

    Returns ``status`` (ready/needs_review/blocked), ``scope``, the assembled
    ``electrical_design`` draft, the reused ``envelope`` result and its
    ``capacity``, ``issues``, ``missing_fields``, ``warnings`` and a
    ``provenance`` list of citation records.
    """
    result = {"status": "blocked", "scope": SCOPE, "electrical_design": None, "capacity": None,
              "envelope": None, "issues": [], "missing_fields": [], "warnings": [],
              "needs_review": False, "provenance": []}
    records = []
    reasons = []
    uncertain_fields = set()
    review_fields = set()

    if not _is_text(manufacturer_model, MAX_IDENTITY_CHARS):
        _issue(result, "manufacturer_model",
               "Provide the caller-resolved component identity as printable text up to "
               + str(MAX_IDENTITY_CHARS) + " characters")
        return result

    # ---- 1. declared sources: structure and size bounds ----
    clean_sources = {}
    total_text = 0
    if not isinstance(sources, dict):
        _issue(result, "sources", "Provide sources as an object of short ids")
    else:
        if len(sources) > MAX_SOURCES:
            _issue(result, "sources", "Declare at most " + str(MAX_SOURCES) + " sources")
        for source_id, entry in sources.items():
            prefix = "sources." + str(source_id)
            usable = True
            if not isinstance(source_id, str) or SOURCE_ID.fullmatch(source_id) is None:
                _issue(result, prefix, "Use a short id of at most 80 characters from letters, digits, _ . : -")
                continue
            if not isinstance(entry, dict):
                _issue(result, prefix, "Provide a source object")
                continue
            _report_unknown_keys(entry, set(SOURCE_KEYS), prefix, result, label="Unknown source key")
            if entry.keys() - set(SOURCE_KEYS):
                usable = False
            if entry.get("kind") not in SOURCE_KINDS:
                _issue(result, prefix + ".kind", "Declare the kind as manufacturer, site or assumption")
                usable = False
            for name in ("title", "locator"):
                if not _is_text(entry.get(name)):
                    _issue(result, prefix + "." + name, "Provide printable text up to " + str(MAX_META_CHARS) + " characters")
                    usable = False
            if not isinstance(entry.get("text"), str) or not entry.get("text").strip():
                _issue(result, prefix + ".text", "Provide the declared extracted text")
                usable = False
            elif _has_control_characters(entry["text"]):
                _issue(result, prefix + ".text", "Remove NUL and control characters other than newline, "
                       "carriage return and tab")
                usable = False
            else:
                total_text += len(entry["text"])
                if total_text > MAX_SOURCE_TEXT_CHARS:
                    _issue(result, "sources", "Declared source text exceeds "
                           + str(MAX_SOURCE_TEXT_CHARS) + " characters")
                    return result
            if entry.get("model") is not None and not _is_text(entry.get("model")):
                _issue(result, prefix + ".model", "Use printable text for the applicable model")
                usable = False
            if usable:
                clean_sources[source_id] = entry

    # ---- 2. explicit engineering goals (never a hidden envelope or coefficient) ----
    design = {} if design is None else design
    if not isinstance(design, dict):
        _issue(result, "design", "Provide design as an object")
        design = {}
    else:
        _report_unknown_keys(design, set(DESIGN_KEYS), "design", result,
                             label="A design input may not inject an envelope, coefficient or limit "
                                   "without a citation")
    for name in DESIGN_TARGETS:
        if name in design and not _is_number(design[name]):
            _issue(result, "design." + name, "Use a finite strict numeric value")
    if design.get("voltage_standard") is not None and design.get("voltage_standard") not in ("IEC", "UL"):
        _issue(result, "design.voltage_standard", "Explicitly choose IEC or UL")
    if design.get("design_basis_extra") is not None and not _is_text(design.get("design_basis_extra")):
        _issue(result, "design.design_basis_extra", "Use printable text")

    # ---- 3. declarations: structure, then citation binding ----
    if not isinstance(declarations, list):
        _issue(result, "declarations", "Provide declarations as a list")
        declarations = []
    elif len(declarations) > MAX_DECLARATIONS:
        _issue(result, "declarations", "Declare at most " + str(MAX_DECLARATIONS) + " records")

    accepted = {}
    for index, declaration in enumerate(declarations):
        prefix = "declarations[" + str(index) + "]"
        if not isinstance(declaration, dict):
            _issue(result, prefix, "Provide a declaration object")
            continue
        _report_unknown_keys(declaration, set(DECLARATION_KEYS), prefix, result,
                             label="Unknown declaration key")
        field = declaration.get("field")
        if not isinstance(field, str) or field not in FIELD_SPECS:
            _issue(result, prefix + ".field", "Use one of the supported dotted numeric design fields")
            continue
        value = declaration.get("value")
        if not _is_number(value):
            _issue(result, field, "Use a finite strict numeric value")
            continue
        allowed_units, target = FIELD_SPECS[field]
        unit = _normalize_unit(declaration.get("unit"))
        if unit not in allowed_units:
            _issue(result, field, "Declare the unit explicitly as " + " or ".join(allowed_units))
            continue
        source_id = declaration.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip() or source_id not in clean_sources:
            _issue(result, field, "Declare a source_id that exists in sources")
            continue
        source = clean_sources[source_id]
        excerpt = declaration.get("excerpt")
        value_token = declaration.get("source_value_text")
        unit_token = declaration.get("source_unit_text")
        if not all(isinstance(raw, str) for raw in (excerpt, value_token, unit_token)):
            _issue(result, field, "Declare excerpt, source_value_text and source_unit_text as text")
            continue
        page = declaration.get("page")
        if page is not None and (isinstance(page, bool) or not isinstance(page, int) or page <= 0):
            _issue(result, field, "Use a positive integer page number")
            continue
        record = _record(field, source_id, source, page, _norm_ws(excerpt)[:MAX_EXCERPT_CHARS],
                         value, unit, value_token, unit_token)
        records.append(record)
        if len(excerpt) > MAX_EXCERPT_CHARS:
            _issue(result, field, "The declared excerpt exceeds " + str(MAX_EXCERPT_CHARS) + " characters")
            continue
        if not unit_token.strip():
            _issue(result, field, "The excerpt must carry an explicit unit token for this field")
            continue
        excerpt_norm = _norm_ws(excerpt)
        text_norm = _norm_ws(source.get("text"))
        if not excerpt_norm:
            _issue(result, field, "The declared excerpt is empty")
            continue
        if excerpt_norm not in text_norm:
            _issue(result, field, "The excerpt is not a whitespace-normalized contiguous fragment of the declared source text")
            continue
        if not _contains_number_token(excerpt_norm, value_token):
            _issue(result, field, "source_value_text does not occur in the excerpt as a standalone numeric token")
            continue
        if not _unit_token_follows_number(excerpt_norm, value_token, unit_token):
            _issue(result, field, "source_unit_text is not a standalone unit written after the cited number token")
            continue
        if not _unit_matches(unit, unit_token):
            _issue(result, field, "source_unit_text " + repr(unit_token) + " does not map to the declared unit "
                   + unit + "; no unit conversion is inferred")
            continue
        if not DECIMAL_LITERAL.fullmatch(value_token):
            _issue(result, field, "source_value_text must be a plain ASCII decimal literal with an ASCII sign and "
                   "decimal dot; separators, percent signs, unit suffixes and typographic minus signs are not "
                   "reinterpreted, so transcribe them while the source excerpt keeps the original text")
            continue
        try:
            token_value = Decimal(value_token)
        except InvalidOperation:
            _issue(result, field, "source_value_text is not a readable decimal literal")
            continue
        if token_value != _decimal_of_value(value):
            _issue(result, field, "source_value_text does not equal the declared value; no rescaling is applied")
            continue

        model = source.get("model")
        if model:
            identity = manufacturer_model
            if not _contains_model(text_norm, model):
                _issue(result, field, "The declared source text does not contain the exact model token " + repr(model)
                       + "; a family or group label is not a substitute")
                continue
            if not _contains_model(identity, model):
                _issue(result, field, "The source model " + repr(model) + " is not a standalone token of the "
                       "selected identity " + repr(identity) + "; the document may not apply to this design")
                continue
        else:
            _warn(result, field, "The source declares no applicable model; applicability is not established")
            uncertain_fields.add(field)

        if field == "temperatures.min_ambient_c" and source.get("kind") == "manufacturer":
            _warn(result, field, "A manufacturer document is cited for the design minimum ambient temperature; "
                  "the quote does not establish a site extreme")
            review_fields.add(field)

        record["reference_match"] = True
        accepted.setdefault(field, []).append({
            "value": value, "unit": unit, "target": target, "source_id": source_id,
            "source_kind": source.get("kind"), "page": page,
            "label": _source_label(source_id, source, page, excerpt_norm)})

    # ---- 4. one value per field: a duplicate is never resolved by last-wins ----
    for field, entries in list(accepted.items()):
        if len(entries) > 1:
            _issue(result, field, "Duplicate declarations for this field ("
                   + ", ".join(entry["label"][:80] for entry in entries) + "); resolve the conflict explicitly")
            del accepted[field]
    for field in DESIGN_TARGETS:
        if field in accepted and field in design:
            _issue(result, field, "The value is declared both as an engineering goal and as a cited declaration; keep one")

    result["provenance"] = records

    # ---- 5. assemble the draft request (always returned, even when incomplete) ----
    draft = {"manufacturer_model": manufacturer_model}
    if design.get("voltage_standard") is not None:
        draft["voltage_standard"] = design["voltage_standard"]
    if design.get("design_basis_extra") is not None:
        draft["design_basis_extra"] = design["design_basis_extra"]
    for field, entries in accepted.items():
        entry = entries[0]
        bucket, key = entry["target"]
        if not bucket:
            draft[key] = entry["value"]
        elif bucket in BARE_VALUE_BUCKETS:
            # Temperatures and effective irradiance carry plain numbers; their
            # citation text is attached to the dedicated basis/source keys.
            draft.setdefault(bucket, {})[key] = entry["value"]
        else:
            draft.setdefault(bucket, {})[key] = {"value": entry["value"], "unit": entry["unit"],
                                                 "source": entry["label"]}
    for field in DESIGN_TARGETS:
        if field not in draft and _is_number(design.get(field)):
            draft[field] = design[field]
    # The temperature bucket always exists so that missing inputs are reported
    # field by field; the basis text is generated only from a cited record.
    draft.setdefault("temperatures", {})
    ambient = accepted.get("temperatures.min_ambient_c")
    if ambient is not None:
        draft["temperatures"]["min_ambient_basis"] = (
            "caller-declared design minimum ambient temperature cited in " + ambient[0]["label"]
            + "; caller-declared design value; site-extreme validity is not established by the quote")[:MAX_META_CHARS]
    irradiance = draft.get("effective_irradiance")
    if irradiance:
        labels = [accepted[field][0]["label"] for field in
                  ("effective_irradiance.max_irradiance_w_m2", "effective_irradiance.rear_gain_factor")
                  if field in accepted]
        irradiance["source"] = ("; ".join(labels) if labels else "no cited irradiance source")[:MAX_META_CHARS]
    citations = "; ".join(sorted(field + "=" + entries[0]["source_id"]
                                 + ("" if entries[0]["page"] is None else " p." + str(entries[0]["page"]))
                                 for field, entries in accepted.items()))
    if citations:
        extra = draft.get("design_basis_extra") or ""
        draft["design_basis_extra"] = (extra + ("; " if extra else "") + "cited declarations: " + citations)[:1900]
    result["electrical_design"] = draft

    # ---- 6. reuse the public envelope preparation ----
    envelope = prepare_design_envelope(module_electrical, inverter_electrical, draft)
    for issue in envelope["issues"]:
        _issue(result, "envelope." + str(issue["field"]), issue["message"])
    for warning in envelope["warnings"]:
        _warn(result, "envelope." + str(warning["field"]), warning["message"])
    result["envelope"] = envelope
    result["capacity"] = envelope.get("capacity") if envelope.get("status") == "ready" else None
    result["missing_fields"] = sorted({str(issue["field"]) for issue in envelope["issues"]})

    assumptions = sorted(field for field, entries in accepted.items()
                         if entries[0]["source_kind"] == "assumption")
    if assumptions:
        reasons.append("Values rely on declared assumptions, not on manufacturer sources: " + ", ".join(assumptions))
    if uncertain_fields:
        reasons.append("Source applicability is not established (no applicable model declared) for: "
                       + ", ".join(sorted(uncertain_fields)))
    if review_fields:
        reasons.append("Site-extreme validity is not established by the quote for: "
                       + ", ".join(sorted(review_fields)))

    if result["issues"] or envelope.get("status") != "ready":
        result["status"] = "blocked"
    elif reasons:
        result["status"] = "needs_review"
    else:
        result["status"] = "ready"
    result["needs_review"] = bool(reasons)
    if reasons:
        result["warnings"].append({"field": "review", "message": " | ".join(reasons)})
    return result

"""Create a NEW VC editing the native shed and sun-shield geometry in place.

``Sheds`` and ``SunShields`` share one owning field set; only ``FieldType`` and the four
geometry values differ between recorded saves. This module writes declared fields on an
existing record and never converts one type into the other.

Scope and the orientation view reuse ``pvsyst_tracking`` rather than a weaker parallel
gate. Standard library only.
"""
from __future__ import annotations

import difflib
from decimal import Decimal, InvalidOperation

from pvsyst_auto.components.editor import objects, replace_value
from pvsyst_auto.project.tracking import _decimal, _require_scope

BOM = b"\xef\xbb\xbf"
TYPES = ("Sheds", "SunShields")
# Recorded owning field sequence; shape errors stop rather than being patched around.
OWN = ("Comment", "Version", "Flags", "NoOrient", "FieldType", "FieldTilt", "FieldAzim",
       "Pitch", "CollectorWidth", "InactiveBandWidth1", "InactiveBandWidth2", "NSheds",
       "UsesElectricalEffect", "FracShadingFactor", "NModInWidth", "CellWidth",
       "UsesTiltAver", "UsesAzimAver", "UsesSlopeAver", "BTNoTrackers")
# Keys the caller may state, mapped to their native names.
WRITABLE = {"field_tilt_deg": "FieldTilt", "field_azimuth_deg": "FieldAzim",
            "pitch_m": "Pitch", "collector_width_m": "CollectorWidth",
            "inactive_band_width_1_m": "InactiveBandWidth1",
            "inactive_band_width_2_m": "InactiveBandWidth2",
            "shed_count": "NSheds", "module_columns": "NModInWidth",
            "cell_width_m": "CellWidth", "shading_factor": "FracShadingFactor",
            "electrical_effect": "UsesElectricalEffect"}
ANGLES = ("FieldTilt", "FieldAzim")
COUNTS = ("NSheds", "NModInWidth")
FLAGS = ("UsesElectricalEffect",)
AVER = ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver")
BT_NO_TRACKERS = "-1,-1,"
MAX_COUNT = 2147483647
STEP_ANGLE = Decimal("0.1")
STEP_LENGTH = Decimal("0.001")
# Adapter sanity domains, not vendor limits and not measured GUI bounds.
ANGLE_LIMITS = {"FieldTilt": (Decimal("0"), Decimal("90")),
                "FieldAzim": (Decimal("-180"), Decimal("180"))}
POSITIVE_LENGTHS = ("Pitch", "CollectorWidth", "CellWidth")
NONNEGATIVE_LENGTHS = ("InactiveBandWidth1", "InactiveBandWidth2")
FACTOR_LIMITS = (Decimal("0"), Decimal("1"))


def orient_block(lines):
    """Return the one directly owned shed/sun-shield record after checking its shape."""
    blocks = objects("".join(lines))
    records = [block for block in blocks if block.object_type == "pvOrient"]
    if len(records) != 1:
        raise ValueError("Requires exactly one pvOrient record")
    block = records[0]
    if block.path != "PVObject_" or not block.end_line:
        raise ValueError("pvOrient must be the complete directly owned record")
    fieldtype = block.one("FieldType")[1]
    if fieldtype not in TYPES:
        raise ValueError(f"Only a Sheds or SunShields record is supported, not {fieldtype}")
    if set(block.fields) != set(OWN):
        raise ValueError(f"{fieldtype} record fields do not match the recorded native shape")
    for name in OWN:
        block.one(name)  # duplicates stop here
    if block.one("Version")[1] != "8.1.6" or block.one("Flags")[1] != "$00":
        raise ValueError("pvOrient requires Version=8.1.6 and Flags=$00")
    if block.one("NoOrient")[1] != "1":
        raise ValueError("Only orientation 1 is supported")
    targets = set(WRITABLE.values())
    for other in blocks:
        if other is block:
            continue
        if set(other.fields) & targets:
            raise ValueError("Shed geometry fields must exist only in the owning pvOrient record")
    for name in AVER:
        if block.one(name)[1] != "False":
            raise ValueError(f"{name} must stay False")
    if block.one("BTNoTrackers")[1] != BT_NO_TRACKERS:
        raise ValueError(f"BTNoTrackers must stay {BT_NO_TRACKERS}")
    _check_domain(block)
    return block


def _label(name):
    """Public key for a native field name, so error text names what the caller passed."""
    return next((key for key, value in WRITABLE.items() if value == name), name)


def _quantized(number, step, label):
    try:
        return number.quantize(step)
    except (InvalidOperation, ArithmeticError):
        raise ValueError(f"{label} is outside the decimal range this adapter can step") from None


def _domain(name, number):
    """Shared source/request domain: recorded precision first, then the adapter envelope."""
    label = _label(name)
    step = STEP_ANGLE if name in ANGLES else STEP_LENGTH
    if _quantized(number, step, label) != number:
        places = 1 if name in ANGLES else 3
        raise ValueError(f"{label} uses at most {places} decimal places")
    if name in ANGLE_LIMITS:
        low, high = ANGLE_LIMITS[name]
        if not low <= number <= high:
            raise ValueError(f"{label} must stay within the adapter range {low}..{high}")
    elif name == "FracShadingFactor":
        low, high = FACTOR_LIMITS
        if not low <= number <= high:
            raise ValueError(f"{label} must stay within the adapter range {low}..{high}")
    elif name in POSITIVE_LENGTHS:
        if number <= 0:
            raise ValueError(f"{label} must be positive")
    elif name in NONNEGATIVE_LENGTHS:
        if number < 0:
            raise ValueError(f"{label} must be nonnegative")
    return number


def _render(name, value):
    if name in FLAGS:
        if type(value) is not bool:
            raise ValueError(f"{_label(name)} must be a strict boolean")
        return "True" if value else "False"
    if name in COUNTS:
        if type(value) is bool or not isinstance(value, int):
            raise ValueError(f"{_label(name)} must be a strict integer")
        if not 1 <= value <= MAX_COUNT:
            raise ValueError(f"{_label(name)} must stay within 1..{MAX_COUNT}")
        return str(value)
    if type(value) is bool or not isinstance(value, (int, float)):
        raise ValueError(f"{_label(name)} must be a strict finite number")
    number = _domain(name, _decimal(str(value), _label(name)))
    places = 1 if name in ANGLES else 3
    return f"{number:.{places}f}"


def _check_domain(block):
    """Every writable field of the record must already sit in the same domain."""
    for name in WRITABLE.values():
        raw = block.one(name)[1]
        if name in FLAGS:
            if raw not in ("True", "False"):
                raise ValueError(f"{_label(name)} must already be True or False in the record")
            continue
        if name in COUNTS:
            if not raw.isdigit() or not 1 <= int(raw) <= MAX_COUNT:
                raise ValueError(f"{_label(name)} must already be a positive native integer")
            continue
        _domain(name, _decimal(raw, _label(name)))
    bands = (_decimal(block.one("InactiveBandWidth1")[1], "inactive_band_width_1_m")
             + _decimal(block.one("InactiveBandWidth2")[1], "inactive_band_width_2_m"))
    width = _decimal(block.one("CollectorWidth")[1], "collector_width_m")
    if not bands < width:
        raise ValueError("inactive band widths must stay below the collector width")


def shed_values(geometry):
    """Validate the declared fields and render the exact native tokens."""
    if not isinstance(geometry, dict) or not geometry:
        raise ValueError("geometry must declare at least one field")
    rendered = {}
    for key, value in geometry.items():
        name = WRITABLE.get(key)
        if name is None:
            raise ValueError(f"Unknown shed geometry field: {key}")
        rendered[name] = _render(name, value)
    return rendered


def create_shed_variant(store, *, project, source_variant, new_variant, geometry,
                        dry_run=True, variant_name=None, add_utf8_bom=False):
    """Preview or create a NEW VC editing the shed or sun-shield geometry."""
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run, "issues": [],
              "warnings": [], "artifacts": {}, "variant_created": False,
              "native_consumption_verified": False}
    with store.lock:
        try:
            if type(dry_run) is not bool or type(add_utf8_bom) is not bool:
                raise ValueError("Use strict booleans")
            store._check_project(project)
            source, target = store._path(project, source_variant), store._path(project, new_variant)
            if any(path.name.casefold() == target.name.casefold() for path in target.parent.iterdir()):
                raise FileExistsError("Choose a new variant: " + target.name)
            original = store._read(source)
            text, lines = store._text(original)
            arrays, system = store._sections(lines)
            _require_scope(store, lines, arrays, system)
            block = orient_block(lines)
            fieldtype = block.one("FieldType")[1]
            values = shed_values(geometry)
            candidate_lines = list(lines)
            for name, rendered in values.items():
                index, _ = block.one(name)
                candidate_lines[index] = replace_value(candidate_lines[index], rendered)
            bom_prefix = BOM if original.startswith(BOM) or add_utf8_bom else b""
            name_change = None
            if variant_name is not None:
                store._validate_display_name(variant_name, "variant_name")
                index, = store._display_name_fields(candidate_lines, complete=True)
                before = candidate_lines[index].partition("=")[2].strip()
                candidate_lines[index] = replace_value(candidate_lines[index], variant_name)
                name_change = {"before": before, "after": variant_name}
            candidate_text = "".join(candidate_lines)
            # A BOM-less VC is read as Windows-1252, so a non-ASCII body forces the BOM too.
            if variant_name is not None:
                bom_prefix = store._display_name_prefix(original, variant_name,
                                                        add_utf8_bom, "variant_name",
                                                        content=candidate_text)
            elif not candidate_text.isascii():
                bom_prefix = BOM
            candidate = bom_prefix + candidate_text.encode("utf-8")
            _, checked = store._text(candidate)
            checked_arrays, checked_system = store._sections(checked)
            _require_scope(store, checked, checked_arrays, checked_system)
            verified = orient_block(checked)
            if verified.one("FieldType")[1] != fieldtype or any(
                    verified.one(name)[1] != rendered for name, rendered in values.items()):
                raise ValueError("Candidate did not keep the requested shed geometry")
            result.update(status="ready", project=project, source_variant=source_variant,
                          variant=target.suffix[1:].upper(), field_type=fieldtype,
                          geometry=values, artifacts={"variant": str(target)},
                          bom_added=bool(bom_prefix) and not original.startswith(BOM))
            if name_change is not None:
                result["name_change"] = name_change
            result["warnings"].append(
                "Only one new VC is written: the declared geometry fields on the existing %s record "
                "and an optional root Comment change. The field type is never converted, and "
                "orientation list, module model, inverter, strings, weather and topology stay "
                "inherited. Saving is not native consumption." % fieldtype)
            result["warnings"].append(
                "Values are stored text with the recorded precision: angles keep one decimal, "
                "lengths and the shading factor keep three, counts stay positive integers. Ranges "
                "are adapter sanity limits, not vendor or measured GUI bounds. What NSheds and "
                "NModInWidth mean physically is not established, so they are written as declared "
                "values rather than interpreted. No self-shading, electrical grouping or 2D "
                "shading is computed here; verify native hourly results for the matching year.")
            diff = "".join(difflib.unified_diff(text.splitlines(keepends=True),
                                                candidate_text.splitlines(keepends=True)))
            result.update(diff=diff[:32000], diff_truncated=len(diff) > 32000)
            if store._read(source) != original:
                raise ValueError("Template changed while preparing the shed variant")
            if dry_run:
                return result
            result["stage"] = "write"
            with target.open("xb") as stream:
                stream.write(candidate)
            result.update(status="created", stage="created", variant_created=True)
        except (ValueError, OSError, UnicodeError, KeyError, TypeError, StopIteration) as exc:
            result["issues"].append(str(exc))
            result["status"] = "failed" if result["stage"] == "write" else "blocked"
            if result["status"] == "failed":
                result["warnings"].append(
                    "The new variant may contain a partial artifact; inspect the disclosed path "
                    "before retrying. Original template is retained.")
    return result

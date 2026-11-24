"""Create a NEW VC editing the native shed and sun-shield geometry in place.

``Sheds`` and ``SunShields`` share one owning field set; only ``FieldType`` and the four
geometry values differ between recorded saves. This module writes declared fields on an
existing record and never converts one type into the other.

Scope and the orientation view reuse ``pvsyst_tracking`` rather than a weaker parallel
gate. Standard library only.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

from pvsyst_auto.components.editor import objects, replace_value, root_block
from pvsyst_auto.project.tracking import _check_single_orient_subarray_strings, _decimal
from pvsyst_auto.project.variant_writer import run_new_variant_transaction
from pvsyst_auto.versions import NATIVE_TEXT_OR_SEED, SEED_LABEL

TYPES = ("Sheds", "SunShields")
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
ANGLE_LIMITS = {"FieldTilt": (Decimal("0"), Decimal("90")),
                "FieldAzim": (Decimal("-180"), Decimal("180"))}
POSITIVE_LENGTHS = ("Pitch", "CollectorWidth", "CellWidth")
NONNEGATIVE_LENGTHS = ("InactiveBandWidth1", "InactiveBandWidth2")
FACTOR_LIMITS = (Decimal("0"), Decimal("1"))


_HEX_FLAG = re.compile(r"\$[0-9A-Fa-f]{1,8}")


def _require_shed_scope(store, lines, arrays, system):
    """Allow native or synthesized single-orientation Grid Sheds/SunShields variants (including non-zero root/trf Flags)."""
    root = root_block(lines)
    if root.one("Version")[1] not in NATIVE_TEXT_OR_SEED:
        raise ValueError(f"Requires a native {SEED_LABEL} variant")
    if not _HEX_FLAG.fullmatch(root.one("Flags")[1]):
        raise ValueError("Root Flags must be hexadecimal")
    if system is None or len(arrays) != 1:
        raise ValueError("Requires one pvSystem and one subarray")
    system_root = root_block(lines, system)
    if system_root.one("SystemType")[1] != "Grid":
        raise ValueError("Requires SystemType=Grid")
    if "Flags" in system_root.fields and not _HEX_FLAG.fullmatch(system_root.one("Flags")[1]):
        raise ValueError("pvSystem Flags must be hexadecimal")
    main = store._main_array(lines)
    if main is None:
        raise ValueError("A pvMainArray section is required")
    main_root = root_block(lines, main)
    if not _HEX_FLAG.fullmatch(main_root.one("Flags")[1]) or main_root.one("Flags2")[1] != "$082A":
        raise ValueError("Main array requires hexadecimal Flags and Flags2=$082A")
    sub_root = root_block(lines, next(iter(arrays.values())))
    if sub_root.one("Version")[1] not in NATIVE_TEXT_OR_SEED or not _HEX_FLAG.fullmatch(sub_root.one("Flags")[1]):
        raise ValueError(f"Subarray requires native {SEED_LABEL} and hexadecimal Flags")
    _check_single_orient_subarray_strings(store, lines, sub_root)


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
    required = set(WRITABLE.values()) | {"NoOrient", "FieldType"}
    if not required <= set(block.fields):
        missing = sorted(required - set(block.fields))
        raise ValueError(f"{fieldtype} record is missing required shed fields: {', '.join(missing)}")
    for name in required:
        block.one(name)  # duplicates stop here
    if "Version" in block.fields and block.one("Version")[1] not in NATIVE_TEXT_OR_SEED:
        raise ValueError(f"pvOrient requires Version={SEED_LABEL}")
    if "Flags" in block.fields and block.one("Flags")[1] != "$00":
        raise ValueError("pvOrient requires Flags=$00")
    if block.one("NoOrient")[1] != "1":
        raise ValueError("Only orientation 1 is supported")
    targets = set(WRITABLE.values())
    for other in blocks:
        if other is block or other.object_type in ("pvBiFacial", "pvBiFacialSheds", "pvBiFacialTrackers"):
            continue
        if set(other.fields) & targets:
            raise ValueError("Shed geometry fields must exist only in the owning pvOrient record")
    for name in AVER:
        if name in block.fields and block.one(name)[1] != "False":
            raise ValueError(f"{name} must stay False")
    if "BTNoTrackers" in block.fields and block.one("BTNoTrackers")[1] != BT_NO_TRACKERS:
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
            raise ValueError(f"{label} must stay within {low}..{high}")
    elif name == "FracShadingFactor":
        low, high = FACTOR_LIMITS
        if not low <= number <= high:
            raise ValueError(f"{label} must stay within {low}..{high}")
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
    def build(*, lines, **_):
        arrays, system = store._sections(lines)
        _require_shed_scope(store, lines, arrays, system)
        block = orient_block(lines)
        fieldtype = block.one("FieldType")[1]
        values = shed_values(geometry)
        candidate_lines = list(lines)
        for name, rendered in values.items():
            index, _ = block.one(name)
            candidate_lines[index] = replace_value(candidate_lines[index], rendered)

        def verify(checked, _, res):
            checked_arrays, checked_system = store._sections(checked)
            _require_shed_scope(store, checked, checked_arrays, checked_system)
            verified = orient_block(checked)
            if verified.one("FieldType")[1] != fieldtype or any(
                    verified.one(name)[1] != rendered for name, rendered in values.items()):
                raise ValueError("Candidate did not keep the requested shed geometry")
            res.update(field_type=fieldtype, geometry=values)
            res["warnings"].extend([
                "Only one new VC is written: the declared geometry fields on the existing %s record "
                "and an optional root Comment change. The field type is never converted, and "
                "orientation list, module model, inverter, strings, weather and topology stay "
                "inherited. Saving is not native consumption." % fieldtype,
                "Values are stored text with the recorded precision: angles keep one decimal, "
                "lengths and the shading factor keep three, counts stay positive integers. Ranges "
                "are adapter sanity limits, not vendor or measured GUI bounds. What NSheds and "
                "NModInWidth mean physically is not established, so they are written as declared "
                "values rather than interpreted. No self-shading, electrical grouping or 2D "
                "shading is computed here; verify native hourly results for the matching year.",
            ])

        return {"candidate_lines": candidate_lines, "verify": verify}

    return run_new_variant_transaction(
        store,
        project=project,
        source_variant=source_variant,
        new_variant=new_variant,
        dry_run=dry_run,
        add_utf8_bom=add_utf8_bom,
        variant_name=variant_name,
        build=build,
        source_changed_error="Template changed while preparing the shed variant",
    )

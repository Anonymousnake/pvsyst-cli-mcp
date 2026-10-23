"""Create a NEW VC with verified native tracking controls; no tracking model."""
from __future__ import annotations

import difflib
import re
from decimal import Decimal, InvalidOperation

from pvsyst_component_editor import objects, replace_value
from pvsyst_grid import inventory as grid_inventory

BOM = b"\xef\xbb\xbf"
STEP = Decimal("0.1")
# Parser view only: the saved bytes keep the original orientation closing tag.
ORIENT_CLOSE = re.compile(r"(?m)^([ \t]*)End of TOrientGroup([ \t]*)(?=\r?$)")

# Adapter ranges only; the native GUI limits are not measured.
RANGES = {"AxisAzim": (Decimal("-20"), Decimal("20")),
          "MinPhi": (Decimal("-60"), Decimal("-45")),
          "MaxPhi": (Decimal("45"), Decimal("60"))}

# Record shapes recorded from native 8.1.6 saves in orientation-type-matrix-r31.
# Each entry is the exact owning field sequence of that FieldType's TOrientGroup.
_SHARED_HEAD = ("Comment", "Version", "Flags", "NoOrient", "FieldType")
_SHARED_TAIL = ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver", "BTNoTrackers")
_SHELF = ("Pitch", "CollectorWidth", "InactiveBandWidth1", "InactiveBandWidth2")
_RECORD_FIELDS = {
    "UnlimTracking": _SHARED_HEAD + ("AxisAzim", "MinPhi", "MaxPhi", "UsesBacktracking",
                                     "UsesIrradOptimiz", "Pitch", "CollectorWidth",
                                     "InactiveBandWidth1", "InactiveBandWidth2", "NSheds",
                                     "UsesElectricalEffect", "FracShadingFactor", "NModInWidth",
                                     "CellWidth") + _SHARED_TAIL,
    "TrackHorizNSAxis": _SHARED_HEAD + ("AxisAzim", "MinPhi", "MaxPhi", "CentralGap",
                                        "PitchSlope", "UsesBacktracking", "PartialSystemInclObj",
                                        "UsesIrradOptimiz", "UsesWindStow", "WindStowSpeed",
                                        "WindStowPosition") + _SHELF + _SHARED_TAIL,
    "TrackTiltedAxis": _SHARED_HEAD + ("AxisTilt", "AxisAzim", "MinPhi", "MaxPhi", "CentralGap",
                                       "PitchSlope", "UsesBacktracking", "PartialSystemInclObj",
                                       "UsesIrradOptimiz", "UsesWindStow", "WindStowSpeed",
                                       "WindStowPosition") + _SHELF + _SHARED_TAIL,
    "TrackVertAxis": _SHARED_HEAD + ("FieldTilt", "AxisAzim", "MinAzim", "MaxAzim",
                                     "PartialSystemInclObj") + _SHARED_TAIL,
    "TrackSunShield": _SHARED_HEAD + ("AxisAzim", "MinTilt", "MaxTilt", "UsesBacktracking")
                      + _SHELF + _SHARED_TAIL,
    "TrackHorizAxis": _SHARED_HEAD + ("AxisAzim", "MinTilt", "MaxTilt", "PitchSlope",
                                      "UsesBacktracking", "PartialSystemInclObj")
                      + _SHELF + _SHARED_TAIL,
    "TrackTwoAxis": _SHARED_HEAD + ("AxisAzim", "MinAzim", "MaxAzim", "MinTilt", "MaxTilt",
                                    "PartialSystemInclObj") + _SHARED_TAIL,
    "TrackFrameNS": _SHARED_HEAD + ("AxisTilt", "AxisAzim", "MinTilt", "MaxTilt",
                                    "FrameMinPhi", "FrameMaxPhi", "UsesBacktracking",
                                    "PartialSystemInclObj") + _SHELF + _SHARED_TAIL,
    "TrackFrameEW": _SHARED_HEAD + ("AxisAzim", "MinPhi", "MaxPhi", "FrameMinTilt",
                                    "FrameMaxTilt", "UsesBacktracking", "PartialSystemInclObj")
                    + _SHELF + _SHARED_TAIL,
}

# Which editable controls each FieldType owns. A control outside this map is refused,
# so one record shape is never silently mutated into another. PartialSystemInclObj is
# present in several recorded shapes but is deliberately not writable here.
WRITABLE = {
    "UnlimTracking": ("UsesElectricalEffect", "AxisAzim", "MinPhi", "MaxPhi",
                      "UsesBacktracking", "UsesIrradOptimiz"),
    "TrackHorizNSAxis": ("AxisAzim", "MinPhi", "MaxPhi", "UsesBacktracking",
                         "UsesIrradOptimiz", "UsesWindStow", "WindStowSpeed", "WindStowPosition",
                         "CentralGap", "PitchSlope"),
    "TrackTiltedAxis": ("AxisAzim", "AxisTilt", "MinPhi", "MaxPhi", "UsesBacktracking",
                        "UsesIrradOptimiz", "UsesWindStow", "WindStowSpeed", "WindStowPosition",
                        "CentralGap", "PitchSlope"),
    "TrackVertAxis": ("AxisAzim", "FieldTilt", "MinAzim", "MaxAzim"),
    "TrackSunShield": ("AxisAzim", "MinTilt", "MaxTilt", "UsesBacktracking"),
    "TrackHorizAxis": ("AxisAzim", "MinTilt", "MaxTilt", "PitchSlope", "UsesBacktracking"),
    "TrackTwoAxis": ("AxisAzim", "MinAzim", "MaxAzim", "MinTilt", "MaxTilt"),
    "TrackFrameNS": ("AxisAzim", "AxisTilt", "MinTilt", "MaxTilt", "FrameMinPhi", "FrameMaxPhi",
                     "UsesBacktracking"),
    "TrackFrameEW": ("AxisAzim", "MinPhi", "MaxPhi", "FrameMinTilt", "FrameMaxTilt",
                     "UsesBacktracking"),
}

# Public JSON keys usable for any type; the legacy five keep their UnlimTracking names.
GENERIC_KEYS = {
    "axis_azimuth_deg": "AxisAzim",
    "axis_tilt_deg": "AxisTilt",
    "field_tilt_deg": "FieldTilt",
    "min_rotation_deg": "MinPhi",
    "max_rotation_deg": "MaxPhi",
    "min_azimuth_deg": "MinAzim",
    "max_azimuth_deg": "MaxAzim",
    "min_tilt_deg": "MinTilt",
    "max_tilt_deg": "MaxTilt",
    "frame_min_rotation_deg": "FrameMinPhi",
    "frame_max_rotation_deg": "FrameMaxPhi",
    "frame_min_tilt_deg": "FrameMinTilt",
    "frame_max_tilt_deg": "FrameMaxTilt",
    "central_gap_m": "CentralGap",
    "pitch_slope_deg": "PitchSlope",
    "wind_stow_position_deg": "WindStowPosition",
    "wind_stow_speed_mps": "WindStowSpeed",
    "backtracking": "UsesBacktracking",
    "irradiance_optimization": "UsesIrradOptimiz",
    "wind_stow": "UsesWindStow",
    "electrical_effect": "UsesElectricalEffect",
}

# Every min/max pair that can appear in a recorded shape, checked for order in the source
# and again on the candidate, not only for the pair the request happens to touch.
PAIRS = (("MinPhi", "MaxPhi"), ("MinAzim", "MaxAzim"), ("MinTilt", "MaxTilt"),
         ("FrameMinPhi", "FrameMaxPhi"), ("FrameMinTilt", "FrameMaxTilt"))
BOOLEAN_FIELDS = ("UsesBacktracking", "UsesIrradOptimiz", "UsesWindStow",
                  "UsesElectricalEffect")
FALSE_FLAGS = ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver")
BT_NO_TRACKERS = "-1,-1,"

# Baseline values recorded from the native type batch; a source record must match them
# before any control is written, so a mismatched template stops instead of being edited.
UNLIM_GEOMETRY = {"Pitch": Decimal("6.600"), "CollectorWidth": Decimal("3.000"),
                  "InactiveBandWidth1": Decimal("0.020"), "InactiveBandWidth2": Decimal("0.020"),
                  "CellWidth": Decimal("0.156"), "FracShadingFactor": Decimal("1.000")}
UNLIM_INTEGERS = {"NSheds": "10", "NModInWidth": "3"}


def _view(lines):
    return ORIENT_CLOSE.sub(r"\1End of PVObject pvOrient\2", "".join(lines))


def _root_block(lines):
    return next(block for block in objects(_view(lines)) if not block.path)


def _block_of(lines, bounds):
    blocks = objects(_view(lines[bounds[0]:bounds[1] + 1]))
    return next(block for block in blocks if not block.path)


def _decimal(text, label):
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a decimal number") from None
    if not value.is_finite():
        raise ValueError(f"{label} must be finite")
    return value


def _stepped(number, field):
    """Accept only values already on the 0.1 grid; quantize overflow must not escape."""
    try:
        stepped = number.quantize(STEP)
    except (InvalidOperation, ArithmeticError):
        raise ValueError(f"{field} is outside the decimal range this adapter can step") from None
    if number != stepped:
        raise ValueError(f"{field} must use at most 0.1 steps (adapter range, not a native limit)")
    return number


def _angle(number, field):
    """Declared adapter domains, not measured native GUI bounds."""
    low, high = RANGES[field]
    if not low <= number <= high:
        raise ValueError(f"{field} must stay within the adapter range {low}..{high}")
    return _stepped(number, field)


def _check_pairs(fieldtype, values, source):
    """Order every pair this record owns, using the request first and the saved token next."""
    allowed = set(WRITABLE[fieldtype])
    for low, high in PAIRS:
        if low not in allowed and high not in allowed:
            continue
        lo, hi = values.get(low), values.get(high)
        if lo is None and source is not None and low in _RECORD_FIELDS[fieldtype]:
            lo = source.one(low)[1]
        if hi is None and source is not None and high in _RECORD_FIELDS[fieldtype]:
            hi = source.one(high)[1]
        if lo is None or hi is None:
            raise ValueError(f"{low} and {high} must both be readable to check their order")
        if not Decimal(lo) < Decimal(hi):
            raise ValueError(f"{low} must stay below {high}")


def render_control(field, value):
    """Render one requested control to its native token, or stop."""
    if field in BOOLEAN_FIELDS:
        if type(value) is not bool:
            raise ValueError(f"{field} must be a strict boolean")
        return "True" if value else "False"
    if type(value) is bool or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a strict finite number")
    number = _decimal(str(value), field)
    return f"{_angle(number, field) if field in RANGES else _stepped(number, field):.1f}"


def _check_source_numbers(block, fieldtype):
    """The saved record must already satisfy the same domain; a bad value stops, never overwritten."""
    for name in WRITABLE[fieldtype]:
        if name in BOOLEAN_FIELDS:
            if block.one(name)[1] not in ("True", "False"):
                raise ValueError(f"{name} must already be True or False in the source record")
            continue
        number = _decimal(block.one(name)[1], name)
        if name in RANGES:
            _angle(number, name)
        else:
            _stepped(number, name)


def tracking_values(tracking, fieldtype, source=None):
    """Check the declared controls for this record type and render exact native tokens."""
    if not isinstance(tracking, dict) or not tracking:
        raise ValueError("tracking must declare at least one control")
    allowed = WRITABLE[fieldtype]
    values = {}
    for key, value in tracking.items():
        field = GENERIC_KEYS.get(key)
        if field is None:
            raise ValueError(f"Unknown tracking control: {key}")
        if field not in allowed:
            raise ValueError(f"{field} is not a control of a {fieldtype} record")
        values[field] = render_control(field, value)
    _check_pairs(fieldtype, values, source)
    return values


def orient_block(lines):
    """Return the one directly owned tracking record after checking every owned field."""
    blocks = objects(_view(lines))
    records = [block for block in blocks if block.object_type == "pvOrient"]
    if len(records) != 1:
        raise ValueError("Requires exactly one pvOrient record")
    block = records[0]
    if block.path != "PVObject_" or not block.end_line:
        raise ValueError("pvOrient must be the complete directly owned record")
    if block.one("Version")[1] != "8.1.6" or block.one("Flags")[1] != "$00":
        raise ValueError("pvOrient requires Version=8.1.6 and Flags=$00")
    if block.one("NoOrient")[1] != "1":
        raise ValueError("Only orientation 1 is supported")
    fieldtype = block.one("FieldType")[1]
    if fieldtype not in _RECORD_FIELDS:
        raise ValueError(f"Unsupported FieldType: {fieldtype}")
    own = _RECORD_FIELDS[fieldtype]
    if set(block.fields) != set(own):
        raise ValueError(f"{fieldtype} record fields do not match the recorded native shape")
    for name in own:
        block.one(name)  # every owning field must occur exactly once; duplicates stop here
    writable = set(WRITABLE[fieldtype])
    for other in blocks:
        if other is block:
            continue
        if set(other.fields) & writable:
            raise ValueError("Tracking controls must exist only in the owning pvOrient record")
    if fieldtype == "UnlimTracking":
        for name, expected in UNLIM_GEOMETRY.items():
            if _decimal(block.one(name)[1], name) != expected:
                raise ValueError(f"{name} must stay at its verified native value")
        for name, expected in UNLIM_INTEGERS.items():
            if block.one(name)[1] != expected:
                raise ValueError(f"{name} must stay at {expected}")
    # UsesElectricalEffect is deliberately absent from FALSE_FLAGS: a native pair shows the
    # switch is live (the tracked case drops 28.325 to 18.702 on the same hour), so it is a
    # writable control for UnlimTracking rather than an invariant.
    for name in FALSE_FLAGS:
        if name in own and block.one(name)[1] != "False":
            raise ValueError(f"{name} must stay False")
    if block.one("BTNoTrackers")[1] != BT_NO_TRACKERS:
        raise ValueError(f"BTNoTrackers must stay {BT_NO_TRACKERS}")
    _check_source_numbers(block, fieldtype)
    _check_pairs(fieldtype, {}, block)
    return block


def _require_scope(store, lines, arrays, system, *, main_flags=("$01900400",), system_flags=("$06000000",),
                   sub_flags=("$019E1002",)):
    """Verified native 8.1.6 PV-only single-subarray Grid scope; callers own flag-family validation."""
    root = _root_block(lines)
    if root.one("Version")[1] != "8.1.6":
        raise ValueError("Requires a native 8.1.6 variant")
    flags = root.one("Flags")[1]
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) or int(flags[1:], 16) != 0:
        raise ValueError("Root Flags must be 0")
    if system is None or len(arrays) != 1:
        raise ValueError("Requires one pvSystem and one subarray")
    system_root = _block_of(lines, system)
    if set(system_root.fields) != {"Flags", "SystemType"}:
        raise ValueError("pvSystem must declare only Flags and SystemType for this batch")
    if system_root.one("SystemType")[1] != "Grid" or system_root.one("Flags")[1] not in system_flags:
        raise ValueError("Requires SystemType=Grid with the caller's verified system flag family")
    main = store._main_array(lines)
    if main is None:
        raise ValueError("A pvMainArray section is required")
    main_root = _block_of(lines, main)
    if main_root.one("Flags")[1] not in main_flags or main_root.one("Flags2")[1] != "$082A":
        raise ValueError("Main array requires the caller's verified flag family and Flags2=$082A")
    sub_root = _block_of(lines, next(iter(arrays.values())))
    if sub_root.one("Version")[1] != "8.1.6" or sub_root.one("Flags")[1] not in sub_flags:
        raise ValueError("Subarray requires native 8.1.6 and the caller's verified flag family")
    if sub_root.one("Flags2")[1] != "$20":
        raise ValueError("Subarray requires the verified Flags2=$20")
    strings = sub_root.one("NStringCh")[1]
    if (sub_root.one("NoOrientation")[1] != "1" or not strings.isdigit() or int(strings) < 1
            or sub_root.one("NStrOrient1")[1] != strings):
        raise ValueError("Requires all strings on orientation 1 with NStrOrient1 equal to NStringCh")
    if any(key.startswith("NStrOrient") and key != "NStrOrient1" and sub_root.one(key)[1] != "0"
           for key in sub_root.fields):
        raise ValueError("Every other NStrOrient* must be zero")
    store._orientation_list(lines)
    store._require_no_geometry(lines)
    if any(block.object_type in ("pvLoad", "pvGridStorage") for block in objects(_view(lines))):
        raise ValueError("Load and storage objects are outside this batch")
    if any(re.match(r"\s*(?:P50_P90|PVObject_Horizon)\s*=", line) for line in lines):
        raise ValueError("P50/P90 and saved horizon objects are outside this batch")
    if grid_inventory(lines, arrays)["counts_valid"] is not True:
        raise ValueError("Requires complete consistent native grid counts")


def create_tracking_variant(store, *, project, source_variant, new_variant, tracking,
                            dry_run=True, variant_name=None, add_utf8_bom=False):
    """Preview or create a NEW VC only; every other field and the source stay untouched."""
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
            values = tracking_values(tracking, fieldtype, block)
            candidate_lines = list(lines)
            for name, rendered in values.items():
                index, _ = block.one(name)
                candidate_lines[index] = replace_value(candidate_lines[index], rendered)
            bom_prefix = BOM if original.startswith(BOM) or add_utf8_bom else b""
            name_change = None
            if variant_name is not None:
                store._validate_display_name(variant_name, "variant_name")
                bom_prefix = store._display_name_prefix(original, variant_name, add_utf8_bom,
                                                        "variant_name")
                index, = store._display_name_fields(candidate_lines, complete=True)
                before = candidate_lines[index].partition("=")[2].strip()
                candidate_lines[index] = replace_value(candidate_lines[index], variant_name)
                name_change = {"before": before, "after": variant_name}
            candidate_text = "".join(candidate_lines)
            candidate = bom_prefix + candidate_text.encode("utf-8")
            _, checked = store._text(candidate)
            checked_arrays, _ = store._sections(checked)
            verified = orient_block(checked)
            if verified.one("FieldType")[1] != fieldtype:
                raise ValueError("Candidate changed the tracked FieldType")
            if len(checked_arrays) != 1 or any(verified.one(name)[1] != rendered
                                               for name, rendered in values.items()):
                raise ValueError("Candidate did not keep the requested tracking controls")
            result.update(status="ready", project=project, source_variant=source_variant,
                          variant=target.suffix[1:].upper(), field_type=fieldtype,
                          tracking=values, artifacts={"variant": str(target)},
                          bom_added=bool(bom_prefix) and not original.startswith(BOM))
            if name_change is not None:
                result["name_change"] = name_change
            result["warnings"].extend([
                "Only the declared owning %s controls and optional root Comment change; "
                "unrequested geometry, controls, topology and source files stay inherited." % fieldtype,
                "Electrical effect is editable only where the native type owns that field. "
                "No tracking/backtracking/irradiance/shading model is computed; saving is not native consumption.",
            ])
            diff = "".join(difflib.unified_diff(text.splitlines(keepends=True),
                                                candidate_text.splitlines(keepends=True)))
            result.update(diff=diff[:32000], diff_truncated=len(diff) > 32000)
            if store._read(source) != original:
                raise ValueError("Template changed while preparing the tracking variant")
            if dry_run:
                return result
            result["stage"] = "write"
            with target.open("xb") as stream:
                stream.write(candidate)
            result.update(status="created", stage="created", variant_created=True)
        except (ValueError, OSError, UnicodeError, KeyError, TypeError) as exc:
            result["issues"].append(str(exc))
            result["status"] = "failed" if result["stage"] == "write" else "blocked"
            if result["status"] == "failed":
                result["warnings"].append(
                    "The new variant may contain a partial artifact; inspect the disclosed path "
                    "before retrying. Original template is retained.")
    return result

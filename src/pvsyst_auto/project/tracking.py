"""Native tracking controls and in-place cross-orientation conversion (Fixed <-> Seasonal <-> Sheds <-> Tracker)."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from pvsyst_auto.components.editor import objects, replace_value, root_block
from pvsyst_auto.native.headless import strip_result_cache
from pvsyst_auto.project.circuit import inventory as grid_inventory
from pvsyst_auto.project.variant_updates import run_new_variant_transaction
from pvsyst_auto.validation import bounded_number as _finite
from pvsyst_auto.versions import NATIVE_TEXT, SYNTH_VERSION, TEXT_LABEL

STEP = Decimal("0.1")
RANGES = {"AxisAzim": (Decimal("-20"), Decimal("20")), "MinPhi": (Decimal("-60"), Decimal("-45")), "MaxPhi": (Decimal("45"), Decimal("60"))}
_SHARED_HEAD = ("Comment", "Version", "Flags", "NoOrient", "FieldType")
_SHARED_TAIL = ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver", "BTNoTrackers")
_SHELF = ("Pitch", "CollectorWidth", "InactiveBandWidth1", "InactiveBandWidth2")
_RECORD_FIELDS = {
    "UnlimTracking": _SHARED_HEAD + ("AxisAzim", "MinPhi", "MaxPhi", "UsesBacktracking", "UsesIrradOptimiz", *_SHELF, "NSheds", "UsesElectricalEffect", "FracShadingFactor", "NModInWidth", "CellWidth") + _SHARED_TAIL,
    "TrackHorizNSAxis": _SHARED_HEAD + ("AxisAzim", "MinPhi", "MaxPhi", "CentralGap", "PitchSlope", "UsesBacktracking", "PartialSystemInclObj", "UsesIrradOptimiz", "UsesWindStow", "WindStowSpeed", "WindStowPosition") + _SHELF + _SHARED_TAIL,
    "TrackTiltedAxis": _SHARED_HEAD + ("AxisTilt", "AxisAzim", "MinPhi", "MaxPhi", "CentralGap", "PitchSlope", "UsesBacktracking", "PartialSystemInclObj", "UsesIrradOptimiz", "UsesWindStow", "WindStowSpeed", "WindStowPosition") + _SHELF + _SHARED_TAIL,
    "TrackVertAxis": _SHARED_HEAD + ("FieldTilt", "AxisAzim", "MinAzim", "MaxAzim", "PartialSystemInclObj") + _SHARED_TAIL,
    "TrackSunShield": _SHARED_HEAD + ("AxisAzim", "MinTilt", "MaxTilt", "UsesBacktracking") + _SHELF + _SHARED_TAIL,
    "TrackHorizAxis": _SHARED_HEAD + ("AxisAzim", "MinTilt", "MaxTilt", "PitchSlope", "UsesBacktracking", "PartialSystemInclObj") + _SHELF + _SHARED_TAIL,
    "TrackTwoAxis": _SHARED_HEAD + ("AxisAzim", "MinAzim", "MaxAzim", "MinTilt", "MaxTilt", "PartialSystemInclObj") + _SHARED_TAIL,
    "TrackFrameNS": _SHARED_HEAD + ("AxisTilt", "AxisAzim", "MinTilt", "MaxTilt", "FrameMinPhi", "FrameMaxPhi", "UsesBacktracking", "PartialSystemInclObj") + _SHELF + _SHARED_TAIL,
    "TrackFrameEW": _SHARED_HEAD + ("AxisAzim", "MinPhi", "MaxPhi", "FrameMinTilt", "FrameMaxTilt", "UsesBacktracking", "PartialSystemInclObj") + _SHELF + _SHARED_TAIL,
}
WRITABLE = {
    "UnlimTracking": ("UsesElectricalEffect", "AxisAzim", "MinPhi", "MaxPhi", "UsesBacktracking", "UsesIrradOptimiz"),
    "TrackHorizNSAxis": ("AxisAzim", "MinPhi", "MaxPhi", "UsesBacktracking", "UsesIrradOptimiz", "UsesWindStow", "WindStowSpeed", "WindStowPosition", "CentralGap", "PitchSlope"),
    "TrackTiltedAxis": ("AxisAzim", "AxisTilt", "MinPhi", "MaxPhi", "UsesBacktracking", "UsesIrradOptimiz", "UsesWindStow", "WindStowSpeed", "WindStowPosition", "CentralGap", "PitchSlope"),
    "TrackVertAxis": ("AxisAzim", "FieldTilt", "MinAzim", "MaxAzim"),
    "TrackSunShield": ("AxisAzim", "MinTilt", "MaxTilt", "UsesBacktracking"),
    "TrackHorizAxis": ("AxisAzim", "MinTilt", "MaxTilt", "PitchSlope", "UsesBacktracking"),
    "TrackTwoAxis": ("AxisAzim", "MinAzim", "MaxAzim", "MinTilt", "MaxTilt"),
    "TrackFrameNS": ("AxisAzim", "AxisTilt", "MinTilt", "MaxTilt", "FrameMinPhi", "FrameMaxPhi", "UsesBacktracking"),
    "TrackFrameEW": ("AxisAzim", "MinPhi", "MaxPhi", "FrameMinTilt", "FrameMaxTilt", "UsesBacktracking"),
}
GENERIC_KEYS = {
    "axis_azimuth_deg": "AxisAzim", "axis_tilt_deg": "AxisTilt", "field_tilt_deg": "FieldTilt",
    "min_rotation_deg": "MinPhi", "max_rotation_deg": "MaxPhi", "min_azimuth_deg": "MinAzim", "max_azimuth_deg": "MaxAzim",
    "min_tilt_deg": "MinTilt", "max_tilt_deg": "MaxTilt", "frame_min_rotation_deg": "FrameMinPhi",
    "frame_max_rotation_deg": "FrameMaxPhi", "frame_min_tilt_deg": "FrameMinTilt", "frame_max_tilt_deg": "FrameMaxTilt",
    "central_gap_m": "CentralGap", "pitch_slope_deg": "PitchSlope", "wind_stow_position_deg": "WindStowPosition",
    "wind_stow_speed_mps": "WindStowSpeed", "backtracking": "UsesBacktracking", "irradiance_optimization": "UsesIrradOptimiz",
    "wind_stow": "UsesWindStow", "electrical_effect": "UsesElectricalEffect",
}
PAIRS = (("MinPhi", "MaxPhi"), ("MinAzim", "MaxAzim"), ("MinTilt", "MaxTilt"), ("FrameMinPhi", "FrameMaxPhi"), ("FrameMinTilt", "FrameMaxTilt"))
BOOLEAN_FIELDS = ("UsesBacktracking", "UsesIrradOptimiz", "UsesWindStow", "UsesElectricalEffect")
FALSE_FLAGS = ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver")
BT_NO_TRACKERS = "-1,-1,"
UNLIM_GEOMETRY = {"Pitch": Decimal("6.600"), "CollectorWidth": Decimal("3.000"), "InactiveBandWidth1": Decimal("0.020"), "InactiveBandWidth2": Decimal("0.020"), "CellWidth": Decimal("0.156"), "FracShadingFactor": Decimal("1.000")}
UNLIM_INTEGERS = {"NSheds": "10", "NModInWidth": "3"}
TARGET_TYPES = {"fixed": "FixedPlane", "seasonal": "SeasonalTilt", "sheds": "Sheds", "tracker_ns": "UnlimTracking", "tracker_2axis": "TrackTwoAxis"}


def _decimal(text, label):
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a decimal number") from None
    if not value.is_finite():
        raise ValueError(f"{label} must be finite")
    return value


def _stepped(number, field):
    try:
        stepped = number.quantize(STEP)
    except (InvalidOperation, ArithmeticError):
        raise ValueError(f"{field} is outside the decimal range this adapter can step") from None
    if number != stepped:
        raise ValueError(f"{field} must use at most 0.1 steps")
    return number


def _angle(number, field):
    low, high = RANGES[field]
    if not low <= number <= high:
        raise ValueError(f"{field} must stay within {low}..{high}")
    return _stepped(number, field)


def _check_pairs(fieldtype, values, source):
    allowed = set(WRITABLE[fieldtype])
    for low, high in PAIRS:
        if low not in allowed and high not in allowed:
            continue
        lo = values.get(low) or (source.one(low)[1] if source is not None and low in _RECORD_FIELDS[fieldtype] else None)
        hi = values.get(high) or (source.one(high)[1] if source is not None and high in _RECORD_FIELDS[fieldtype] else None)
        if lo is None or hi is None:
            raise ValueError(f"{low} and {high} must both be readable to check their order")
        if not _decimal(lo, low) < _decimal(hi, high):
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
    for name in WRITABLE[fieldtype]:
        if name in BOOLEAN_FIELDS:
            if block.one(name)[1] not in ("True", "False"):
                raise ValueError(f"{name} must already be True or False in the source record")
        else:
            number = _decimal(block.one(name)[1], name)
            _angle(number, name) if name in RANGES else _stepped(number, name)


def tracking_values(tracking, fieldtype, source=None):
    """Check the declared controls for this record type and render exact native tokens."""
    if not isinstance(tracking, dict) or not tracking:
        raise ValueError("tracking must declare at least one control")
    allowed, values = WRITABLE[fieldtype], {}
    for key, value in tracking.items():
        if (field := GENERIC_KEYS.get(key)) is None:
            raise ValueError(f"Unknown tracking control: {key}")
        if field not in allowed:
            raise ValueError(f"{field} is not a control of a {fieldtype} record")
        values[field] = render_control(field, value)
    _check_pairs(fieldtype, values, source)
    return values


def _single_orient_record(lines):
    blocks = objects("".join(lines))
    records = [block for block in blocks if block.object_type == "pvOrient"]
    if len(records) != 1:
        raise ValueError("Requires exactly one pvOrient record")
    block = records[0]
    if block.path != "PVObject_" or not block.end_line:
        raise ValueError("pvOrient must be the complete directly owned record")
    return blocks, block


def orient_block(lines):
    """Return the one directly owned tracking record after checking every owned field."""
    blocks, block = _single_orient_record(lines)
    if block.one("Version")[1] not in NATIVE_TEXT or block.one("Flags")[1] != "$00":
        raise ValueError(f"pvOrient requires Version={TEXT_LABEL} and Flags=$00")
    if block.one("NoOrient")[1] != "1":
        raise ValueError("Only orientation 1 is supported")
    fieldtype = block.one("FieldType")[1]
    if fieldtype not in _RECORD_FIELDS:
        raise ValueError(f"Unsupported FieldType: {fieldtype}")
    own = _RECORD_FIELDS[fieldtype]
    if set(block.fields) != set(own):
        raise ValueError(f"{fieldtype} record fields do not match the recorded native shape")
    for name in own:
        block.one(name)
    writable = set(WRITABLE[fieldtype])
    if any(other is not block and (set(other.fields) & writable) for other in blocks):
        raise ValueError("Tracking controls must exist only in the owning pvOrient record")
    if fieldtype == "UnlimTracking":
        for name, expected in UNLIM_GEOMETRY.items():
            if _decimal(block.one(name)[1], name) != expected:
                raise ValueError(f"{name} must stay at its verified native value")
        for name, expected in UNLIM_INTEGERS.items():
            if block.one(name)[1] != expected:
                raise ValueError(f"{name} must stay at {expected}")
    for name in FALSE_FLAGS:
        if name in own and block.one(name)[1] != "False":
            raise ValueError(f"{name} must stay False")
    if block.one("BTNoTrackers")[1] != BT_NO_TRACKERS:
        raise ValueError(f"BTNoTrackers must stay {BT_NO_TRACKERS}")
    _check_source_numbers(block, fieldtype)
    _check_pairs(fieldtype, {}, block)
    return block


def _check_single_orient_subarray_strings(store, lines, sub_root):
    strings = sub_root.one("NStringCh")[1]
    if sub_root.one("NoOrientation")[1] != "1" or not strings.isdigit() or int(strings) < 1 or sub_root.one("NStrOrient1")[1] != strings:
        raise ValueError("Requires all strings on orientation 1 with NStrOrient1 equal to NStringCh")
    if any(k.startswith("NStrOrient") and k != "NStrOrient1" and sub_root.one(k)[1] != "0" for k in sub_root.fields):
        raise ValueError("Every other NStrOrient* must be zero")
    store._orientation_list(lines)
    store._require_no_geometry(lines)
    if any(block.object_type in ("pvLoad", "pvGridStorage") for block in objects("".join(lines))):
        raise ValueError("Load and storage objects are outside this batch")


def _require_scope(store, lines, arrays, system, *, main_flags=("$01900400",), system_flags=("$06000000",), sub_flags=("$019E1002",)):
    root = root_block(lines)
    if root.one("Version")[1] not in NATIVE_TEXT:
        raise ValueError(f"Requires a native {TEXT_LABEL} variant")
    flags = root.one("Flags")[1]
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags) or int(flags[1:], 16) != 0:
        raise ValueError("Root Flags must be 0")
    if system is None or len(arrays) != 1:
        raise ValueError("Requires one pvSystem and one subarray")
    sys_root = root_block(lines, system)
    if set(sys_root.fields) != {"Flags", "SystemType"}:
        raise ValueError("pvSystem must declare only Flags and SystemType for this batch")
    if sys_root.one("SystemType")[1] != "Grid" or sys_root.one("Flags")[1] not in system_flags:
        raise ValueError("Requires SystemType=Grid with the caller's verified system flag family")
    if (main := store._main_array(lines)) is None:
        raise ValueError("A pvMainArray section is required")
    main_root = root_block(lines, main)
    if main_root.one("Flags")[1] not in main_flags or main_root.one("Flags2")[1] != "$082A":
        raise ValueError("Main array requires the caller's verified flag family and Flags2=$082A")
    sub_root = root_block(lines, next(iter(arrays.values())))
    if sub_root.one("Version")[1] not in NATIVE_TEXT or sub_root.one("Flags")[1] not in sub_flags:
        raise ValueError(f"Subarray requires native {TEXT_LABEL} and the caller's verified flag family")
    if sub_root.one("Flags2")[1] != "$20":
        raise ValueError("Subarray requires the verified Flags2=$20")
    _check_single_orient_subarray_strings(store, lines, sub_root)
    if any(re.match(r"\s*(?:P50_P90|PVObject_Horizon)\s*=", line) for line in lines):
        raise ValueError("P50/P90 and saved horizon objects are outside this batch")
    if grid_inventory(lines, arrays)["counts_valid"] is not True:
        raise ValueError("Requires complete consistent native grid counts")


def create_tracking_variant(store, *, project, source_variant, new_variant, tracking,
                            dry_run=True, variant_name=None, add_utf8_bom=False):
    """Preview or create a NEW VC only; every other field and the source stay untouched."""
    def build(*, lines, **_):
        arrays, system = store._sections(lines)
        _require_scope(store, lines, arrays, system)
        block = orient_block(lines)
        fieldtype = block.one("FieldType")[1]
        values = tracking_values(tracking, fieldtype, block)
        candidate_lines = list(lines)
        for name, rendered in values.items():
            idx = block.one(name)[0]
            candidate_lines[idx] = replace_value(candidate_lines[idx], rendered)

        def verify(checked, _, res):
            checked_arrays, _ = store._sections(checked)
            verified = orient_block(checked)
            if verified.one("FieldType")[1] != fieldtype:
                raise ValueError("Candidate changed the tracked FieldType")
            if len(checked_arrays) != 1 or any(verified.one(n)[1] != r for n, r in values.items()):
                raise ValueError("Candidate did not keep the requested tracking controls")
            res.update(field_type=fieldtype, tracking=values)
            res["warnings"].extend([
                f"Only the declared owning {fieldtype} controls and optional root Comment change; unrequested geometry, controls, topology and source files stay inherited.",
                "Electrical effect is editable only where the native type owns that field. No tracking/backtracking/irradiance/shading model is computed; saving is not native consumption.",
            ])

        return {"candidate_lines": candidate_lines, "verify": verify}

    return run_new_variant_transaction(
        store, project=project, source_variant=source_variant, new_variant=new_variant,
        dry_run=dry_run, add_utf8_bom=add_utf8_bom, variant_name=variant_name, build=build,
        source_changed_error="Template changed while preparing the tracking variant",
    )


# --- Cross-orientation conversion ---

def _strip_block(lines: list[str], start_marker: str, end_marker: str) -> tuple[list[str], bool]:
    out, inside, removed = [], False, False
    for ln in lines:
        s = ln.strip()
        if not inside and s == start_marker:
            inside = removed = True
        elif inside:
            if s == end_marker:
                inside = False
        else:
            out.append(ln)
    return out, removed


def _build_orientation_block(target_type: str, params: dict[str, Any], *, eol: str, version: str = SYNTH_VERSION) -> tuple[list[str], dict[str, Any]]:
    ft = TARGET_TYPES[target_type]
    if target_type == "fixed":
        tilt, azim, slope = _finite(params.get("tilt_deg", 25.0), "tilt_deg", 0.0, 90.0), _finite(params.get("azimuth_deg", 0.0), "azimuth_deg", -180.0, 180.0), _finite(params.get("base_slope_deg", 0.0), "base_slope_deg", -45.0, 45.0)
        comment = f"Fixed, Tilt {tilt:.1f}°, Azim. {azim:.1f}°"
        body = [f"    FieldType={ft}", f"    FieldTilt={tilt:.1f}", f"    FieldAzim={azim:.1f}", f"    BaseSlope={slope:.1f}"]
        meta = {"field_type": ft, "tilt_deg": tilt, "azimuth_deg": azim, "base_slope_deg": slope}
    elif target_type == "seasonal":
        s_tilt, w_tilt, azim = _finite(params.get("summer_tilt_deg", 15.0), "summer_tilt_deg", 0.0, 90.0), _finite(params.get("winter_tilt_deg", 45.0), "winter_tilt_deg", 0.0, 90.0), _finite(params.get("azimuth_deg", 0.0), "azimuth_deg", -180.0, 180.0)
        w_months = params.get("winter_months", [1, 2, 3, 10, 11, 12])
        if not isinstance(w_months, list) or not w_months or any(type(m) is not int or not 1 <= m <= 12 for m in w_months):
            raise ValueError("winter_months must be a non-empty list of month integers in 1..12")
        mask = sum(1 << (m - 1) for m in set(w_months))
        comment = f"Seasonal, Summer {s_tilt:.1f}° / Winter {w_tilt:.1f}°, Azim. {azim:.1f}°"
        body = [f"    FieldType={ft}", "    FieldTilt=0.0", f"    FieldAzim={azim:.1f}", f"    SummerTilt={s_tilt:.1f}", f"    WinterTilt={w_tilt:.1f}", f"    WinterMonths=${mask:04X}"]
        meta = {"field_type": ft, "summer_tilt_deg": s_tilt, "winter_tilt_deg": w_tilt, "azimuth_deg": azim, "winter_months": sorted(set(w_months))}
    elif target_type == "sheds":
        tilt, azim = _finite(params.get("tilt_deg", 20.0), "tilt_deg", 0.0, 90.0), _finite(params.get("azimuth_deg", 0.0), "azimuth_deg", -180.0, 180.0)
        pitch = _finite(params.get("pitch_m", 10.0), "pitch_m", 0.5, 200.0)
        width = _finite(params.get("collector_width_m", 4.5), "collector_width_m", 0.2, pitch - 0.05)
        n_sheds = params.get("shed_count", 10)
        if type(n_sheds) is not int or not 1 <= n_sheds <= 1000:
            raise ValueError("shed_count must be an integer in 1..1000")
        comment = f"Unlimited Sheds, Tilt {tilt:.1f}°, Pitch {pitch:.1f}m"
        body = [f"    FieldType={ft}", f"    FieldTilt={tilt:.1f}", f"    FieldAzim={azim:.1f}", f"    Pitch={pitch:.3f}", f"    CollectorWidth={width:.3f}",
                "    InactiveBandWidth1=0.020", "    InactiveBandWidth2=0.020", f"    NSheds={n_sheds}", "    UsesElectricalEffect=False", "    FracShadingFactor=1.000", "    NModInWidth=4", "    CellWidth=0.156"]
        meta = {"field_type": ft, "tilt_deg": tilt, "azimuth_deg": azim, "pitch_m": pitch, "collector_width_m": width, "shed_count": n_sheds}
    elif target_type == "tracker_ns":
        axis_az, min_phi, max_phi = _finite(params.get("axis_azimuth_deg", 0.0), "axis_azimuth_deg", -180.0, 180.0), _finite(params.get("min_phi_deg", -60.0), "min_phi_deg", -90.0, 0.0), _finite(params.get("max_phi_deg", 60.0), "max_phi_deg", 0.0, 90.0)
        if min_phi >= max_phi:
            raise ValueError("min_phi_deg must be < max_phi_deg")
        pitch = _finite(params.get("pitch_m", 6.0), "pitch_m", 0.5, 200.0)
        width = _finite(params.get("collector_width_m", 2.3), "collector_width_m", 0.2, pitch - 0.05)
        bt = params.get("backtracking", True)
        if type(bt) is not bool:
            raise ValueError("backtracking must be a strict boolean")
        comment = f"Horizontal N-S Axis Tracker, Phi {min_phi:.0f}°..{max_phi:.0f}°"
        body = [f"    FieldType={ft}", f"    AxisAzim={axis_az:.1f}", f"    MinPhi={min_phi:.1f}", f"    MaxPhi={max_phi:.1f}", f"    UsesBacktracking={'True' if bt else 'False'}", "    UsesIrradOptimiz=False",
                f"    Pitch={pitch:.3f}", f"    CollectorWidth={width:.3f}", "    InactiveBandWidth1=0.020", "    InactiveBandWidth2=0.020", "    NSheds=10", "    UsesElectricalEffect=False", "    FracShadingFactor=1.000", "    NModInWidth=2", "    CellWidth=0.156"]
        meta = {"field_type": ft, "axis_azimuth_deg": axis_az, "min_phi_deg": min_phi, "max_phi_deg": max_phi, "pitch_m": pitch, "collector_width_m": width, "backtracking": bt}
    else:
        axis_az, min_phi, max_phi = _finite(params.get("axis_azimuth_deg", 0.0), "axis_azimuth_deg", -180.0, 180.0), _finite(params.get("min_phi_deg", -120.0), "min_phi_deg", -180.0, 0.0), _finite(params.get("max_phi_deg", 120.0), "max_phi_deg", 0.0, 180.0)
        min_tilt = _finite(params.get("min_tilt_deg", 10.0), "min_tilt_deg", 0.0, 85.0)
        max_tilt = _finite(params.get("max_tilt_deg", 80.0), "max_tilt_deg", min_tilt + 1.0, 90.0)
        comment = f"Two-Axis Tracker, Tilt {min_tilt:.0f}°..{max_tilt:.0f}°, Azim {min_phi:.0f}°..{max_phi:.0f}°"
        body = [f"    FieldType={ft}", f"    AxisAzim={axis_az:.1f}", f"    MinAzim={min_phi:.1f}", f"    MaxAzim={max_phi:.1f}", f"    MinTilt={min_tilt:.1f}", f"    MaxTilt={max_tilt:.1f}"]
        meta = {"field_type": ft, "axis_azimuth_deg": axis_az, "min_tilt_deg": min_tilt, "max_tilt_deg": max_tilt, "min_phi_deg": min_phi, "max_phi_deg": max_phi}
    body.extend(["    UsesTiltAver=False", "    UsesAzimAver=False", "    UsesSlopeAver=False", "    BTNoTrackers=-1,-1,"])
    return [f"  Orientations, list of=1 TOrientGroup{eol}", eol, f"    PVObject_=pvOrient{eol}", f"      Comment={comment}{eol}", f"      Version={version}{eol}", f"      Flags=$00{eol}", f"    NoOrient=1{eol}", *[ln + eol for ln in body], f"    End of TOrientGroup{eol}", f"  End of Orientations{eol}"], meta


def convert_orientation_lines(lines: list[str], *, target_type: str, params: dict[str, Any] | None = None) -> tuple[list[str], dict[str, Any]]:
    """Replace the Orientations list of a variant with a new orientation type, stripping incompatible 3D caches."""
    if target_type == "dome":
        raise ValueError("Conversions to or from paired dome orientations are not supported; dome faces need native pvDomeOrient pairing and string allocation")
    if target_type not in TARGET_TYPES:
        raise ValueError(f"target_type must be one of {sorted(TARGET_TYPES)}")
    parsed_blocks = objects("".join(lines))
    subarrays = [b for b in parsed_blocks if b.object_type == "pvSubArray"]
    if (any(b.object_type == "pvDomeOrient" for b in parsed_blocks)
            or any("NoOrientation2" in b.fields or "NStrOrient2" in b.fields for b in subarrays)
            or any(b.object_type == "pvOrient" and ("NoPairOrient" in b.fields or any(v in ("Dome", "DomeBackfaces") for _, v in b.fields.get("FieldType", ()))) for b in parsed_blocks)):
        raise ValueError("Conversions to or from paired dome orientations are not supported; dome faces need native pvDomeOrient pairing and string allocation")
    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    start = next((i for i, ln in enumerate(lines) if ln.strip().startswith("Orientations, list of=")), None)
    end = next((i for i, ln in enumerate(lines) if ln.strip() in ("End of Orientations", "End of List Orientations")), None)
    if start is None or end is None or start >= end:
        raise ValueError("Variant does not contain a complete Orientations list")
    prior_types = [ln.strip().split("=", 1)[1] for ln in lines[start:end + 1] if ln.strip().startswith("FieldType=")]
    new_block, meta = _build_orientation_block(target_type, params or {}, eol=eol)
    cand = list(lines[:start]) + new_block + list(lines[end + 1:])
    cand, removed_3d = _strip_block(cand, "PVObject_Ombrage=pvShading", "End of PVObject pvShading")
    cand, removed_ml = _strip_block(cand, "PVObject_ModuleLayout=pvModuleLayout", "End of PVObject pvModuleLayout")
    cand, _ = strip_result_cache(cand)
    if removed_3d or removed_ml:
        for idx in range(min(15, len(cand))):
            if m := re.match(r"^(\s*Flags=)\$([0-9A-Fa-f]{1,8})(\r?\n?)$", cand[idx]):
                cand[idx] = f"{m.group(1)}${int(m.group(2), 16) & ~0x00000C41:08X}{m.group(3)}"
                break
    for idx, ln in enumerate(cand):
        if ln.strip().startswith("NoOrientation="):
            cand[idx] = replace_value(ln, "1")
    return cand, {"previous_field_types": prior_types, "target_type": target_type, "converted": meta, "stripped_3d_shading": removed_3d, "stripped_module_layout": removed_ml}


def convert_orientation(store, *, project: str, source_variant: str, new_variant: str, target_type: str,
                        params: dict[str, Any] | None = None, dry_run: bool = True, variant_name: str | None = None,
                        add_utf8_bom: bool = False) -> dict[str, Any]:
    """Create a new variant converting an existing variant's orientation type in-place."""
    def preflight():
        if target_type not in TARGET_TYPES:
            raise ValueError(f"target_type must be one of {sorted(TARGET_TYPES)}")

    def build(*, lines, **_):
        candidate_lines, summary = convert_orientation_lines(lines, target_type=target_type, params=params or {})

        def verify(checked, _, res):
            if not [b for b in objects("".join(checked)) if b.object_type == "pvOrient"]:
                raise ValueError("Converted variant lost its pvOrient block")
            res["orientation_conversion"] = summary

        return {"candidate_lines": candidate_lines, "verify": verify}

    return run_new_variant_transaction(
        store, project=project, source_variant=source_variant, new_variant=new_variant,
        dry_run=dry_run, add_utf8_bom=add_utf8_bom, variant_name=variant_name, preflight=preflight, build=build,
        source_changed_error="Source variant changed before orientation conversion",
        write_failure_warning="New variant may contain a partial artifact; original source is preserved.",
    )

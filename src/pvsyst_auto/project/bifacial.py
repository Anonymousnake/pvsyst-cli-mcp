"""Native PVsyst 8.1 pvBiFacial, seasonal tilt, and shed/sun-shield geometry editors."""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pvsyst_auto.components.editor import ASSIGNMENT, direct_fields, finite, objects, one, pan_blocks, replace_value, root_block
from pvsyst_auto.project.tracking import _check_single_orient_subarray_strings, _decimal, _single_orient_record
from pvsyst_auto.project.variant_updates import run_new_variant_transaction
from pvsyst_auto.validation import finite_number as _finite
from pvsyst_auto.versions import NATIVE_TEXT, NATIVE_TEXT_OR_SEED, SEED_LABEL, TEXT_LABEL

# --- Bifacial ---

BIFACIAL_MODELS = ("Sheds2D", "Trackers2D")
PARAM_ORIGINS = ("UnlimSheds", "RegularSheds")
TRACKING_FIELD_TYPES = frozenset({"UnlimTracking", "TrackHorizNSAxis"})
SHED_FIELD_TYPES = frozenset({"Sheds", "FixedPlane", "RegularSheds"})
SUPPORTED_FIELD_TYPES = TRACKING_FIELD_TYPES | SHED_FIELD_TYPES
ALLOWED_KEYS = frozenset({
    "enabled", "model", "param_origin", "ground_albedo", "bifaciality_factor", "rear_shading_factor",
    "mismatch_loss_factor", "height_above_ground_m", "pitch_user_m", "n_sheds_user", "shed_pitch_from_user",
})
_BIFACIAL_TARGETS = {
    "model": ("root", "BifacialModel", str), "param_origin": ("root", "ParamOrigin", str),
    "ground_albedo": ("root", "GroundAlbedo", lambda v: f"{v:.3f}"),
    "bifaciality_factor": ("root", "BiFacialityFactor", lambda v: f"{v:.3f}"),
    "rear_shading_factor": ("root", "RearShdFactor", lambda v: f"{v:.3f}"),
    "mismatch_loss_factor": ("root", "MismatchLossFactor", lambda v: f"{v:.3f}"),
    "height_above_ground_m": ("sub", "HeightGr", lambda v: f"{v:.3f}"),
    "pitch_user_m": ("sub", "PitchUser", lambda v: f"{v:.3f}"),
    "n_sheds_user": ("sub", "NShedsUser", str),
    "shed_pitch_from_user": ("sub", "ShedPitchComesFromUser", lambda v: "True" if v else "False"),
}


def _strict_int(value, label: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be a strict integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def _strict_bool(value, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a strict boolean")
    return value


def pan_is_bifacial(components, pan_name: str, *, path: str | Path | None = None, library: str | None = None) -> bool | None:
    """Return True/False for a readable text .PAN based on TPVModule.isBiFacial (Flags bit 23 / $00800000)."""
    if components is None:
        return None
    candidates: list[Path] = [Path(path)] if path and Path(path).is_file() else []
    if not candidates:
        name = Path(pan_name).name
        for lib in ((library,) if library in ("workspace", "builtin") else ("workspace", "builtin")):
            if lib == "builtin" and getattr(components, "builtin", None) is None:
                continue
            try:
                p = components._path("PAN", name, lib)
            except (ValueError, OSError):
                continue
            if p.is_file():
                candidates.append(p)
                break
    for pan_path in candidates:
        try:
            root, _ = pan_blocks(components._read(pan_path).decode("utf-8-sig"))
            flags_raw = root.one("Flags")[1].strip()
            return bool(int(flags_raw[1:], 16) & 0x00800000) if flags_raw.startswith("$") else None
        except (ValueError, OSError, KeyError):
            return None
    return None


def _default_model_and_origin(field_type: str | None) -> tuple[str, str]:
    if field_type == "TrackHorizNSAxis":
        return "Trackers2D", "RegularSheds"
    if field_type == "UnlimTracking":
        return "Trackers2D", "UnlimSheds"
    if field_type in ("FixedPlane", "RegularSheds"):
        return "Sheds2D", "RegularSheds"
    return "Sheds2D", "UnlimSheds"


def _validate_bifacial_field(key: str, raw_val, *, field_type: str | None):
    if key == "model":
        if raw_val not in BIFACIAL_MODELS:
            raise ValueError(f"bifacial.model must be one of {BIFACIAL_MODELS}")
        if field_type in TRACKING_FIELD_TYPES and raw_val != "Trackers2D":
            raise ValueError(f"Orientation FieldType={field_type} requires bifacial.model='Trackers2D'")
        if field_type in SHED_FIELD_TYPES and raw_val != "Sheds2D":
            raise ValueError(f"Orientation FieldType={field_type} requires bifacial.model='Sheds2D'")
        return raw_val
    if key == "param_origin":
        if raw_val not in PARAM_ORIGINS:
            raise ValueError(f"bifacial.param_origin must be one of {PARAM_ORIGINS}")
        return raw_val
    if key in ("ground_albedo", "bifaciality_factor"):
        return _finite(raw_val, f"bifacial.{key}", minimum=0.0, maximum=1.0, strict_min=True)
    if key in ("rear_shading_factor", "mismatch_loss_factor"):
        return _finite(raw_val, f"bifacial.{key}", minimum=0.0, maximum=1.0)
    if key == "height_above_ground_m":
        return _finite(raw_val, "bifacial.height_above_ground_m", minimum=0.0, maximum=50.0, strict_min=True)
    if key == "pitch_user_m":
        val = _finite(raw_val, "bifacial.pitch_user_m")
        if val != -1.0 and val <= 0.0:
            raise ValueError("bifacial.pitch_user_m must be -1.0 (from geometry) or > 0")
        return val
    if key == "n_sheds_user":
        val = _strict_int(raw_val, "bifacial.n_sheds_user", minimum=-1)
        if val == 0:
            raise ValueError("bifacial.n_sheds_user must be -1 (unlimited/from geometry) or >= 1")
        return val
    if key == "shed_pitch_from_user":
        return _strict_bool(raw_val, "bifacial.shed_pitch_from_user")
    raise ValueError(f"Unknown bifacial field: {key}")


def normalize_bifacial_spec(spec: dict, *, field_type: str | None = None, existing: dict | None = None) -> dict:
    """Validate and normalize a bifacial configuration dict."""
    if not isinstance(spec, dict):
        raise ValueError("bifacial configuration must be an object")
    if unknown := sorted(set(spec) - ALLOWED_KEYS):
        raise ValueError("Unknown bifacial fields: " + ", ".join(unknown))
    if not _strict_bool(spec.get("enabled", True), "bifacial.enabled"):
        return {"enabled": False}
    default_model, default_origin = _default_model_and_origin(field_type)
    ex = existing or {}
    _ex = lambda k, d: d if ex.get(k) is None else ex[k]
    base = {
        "model": _ex("model", default_model), "param_origin": _ex("param_origin", default_origin),
        "ground_albedo": _ex("ground_albedo", 0.30), "bifaciality_factor": _ex("bifaciality_factor", 0.80),
        "rear_shading_factor": _ex("rear_shading_factor", 0.05), "mismatch_loss_factor": _ex("mismatch_loss_factor", 0.10),
        "height_above_ground_m": _ex("height_above_ground_m", 2.50 if default_model == "Trackers2D" else 1.00),
        "pitch_user_m": _ex("pitch_user_m", -1.0), "n_sheds_user": _ex("n_sheds_user", -1),
        "shed_pitch_from_user": _ex("shed_pitch_from_user", False),
    }
    return {"enabled": True, **{k: _validate_bifacial_field(k, spec.get(k, base[k]), field_type=field_type) for k in _BIFACIAL_TARGETS}}


def render_bifacial_block(spec: dict, *, field_type: str | None = None, indent: str = "    ") -> list[str]:
    """Render a complete PVsyst 8.1 PVObject_Bifacial=pvBiFacial block (no trailing newline)."""
    cfg = normalize_bifacial_spec(spec, field_type=field_type)
    if not cfg["enabled"]:
        return []
    sub_key, sub_type = (("PVObject_BiFacialTrackerModel", "pvBiFacialTrackers") if cfg["model"] == "Trackers2D"
                         else ("PVObject_BiFacialShedModel", "pvBiFacialSheds"))
    return [
        f"{indent}PVObject_Bifacial=pvBiFacial", f"{indent}  Flags=$00", f"{indent}  BifacialModel={cfg['model']}",
        f"{indent}  ParamOrigin={cfg['param_origin']}", f"{indent}  GroundAlbedo={cfg['ground_albedo']:.3f}",
        f"{indent}  BiFacialityFactor={cfg['bifaciality_factor']:.3f}", f"{indent}  RearShdFactor={cfg['rear_shading_factor']:.3f}",
        f"{indent}  MismatchLossFactor={cfg['mismatch_loss_factor']:.3f}", f"{indent}  {sub_key}={sub_type}",
        f"{indent}    Flags=$00", f"{indent}    HeightGr={cfg['height_above_ground_m']:.3f}",
        f"{indent}    PitchUser={cfg['pitch_user_m']:.3f}", f"{indent}    NShedsUser={cfg['n_sheds_user']}",
        f"{indent}    ShedPitchComesFromUser={'True' if cfg['shed_pitch_from_user'] else 'False'}",
        f"{indent}  End of PVObject {sub_type}", f"{indent}End of PVObject pvBiFacial",
    ]


def _find_bifacial_span(lines: list[str], bounds: tuple[int, int]) -> tuple[int, int] | None:
    start, end = bounds
    starts = [i for i in range(start + 1, end) if lines[i].strip() == "PVObject_Bifacial=pvBiFacial"]
    if not starts:
        return None
    if len(starts) > 1:
        raise ValueError("Orientation declares multiple PVObject_Bifacial blocks")
    b_end = next((i for i in range(starts[0] + 1, end) if lines[i].strip() == "End of PVObject pvBiFacial"), None)
    if b_end is None:
        raise ValueError("Unclosed PVObject_Bifacial=pvBiFacial block")
    return starts[0], b_end


def bifacial_state(lines: list[str], orient_bounds: tuple[int, int] | None = None) -> dict:
    """Read-only projection of pvBiFacial inside an orientation (or the whole variant)."""
    if orient_bounds is None:
        orient_bounds = (0, len(lines))
    try:
        span = _find_bifacial_span(lines, orient_bounds)
    except ValueError as exc:
        return {"enabled": None, "issue": str(exc)}
    if span is None:
        return {"enabled": False}
    try:
        parsed = objects("".join(lines[span[0]:span[1] + 1]))
    except ValueError as exc:
        return {"enabled": None, "issue": str(exc)}
    root = next((b for b in parsed if b.object_type == "pvBiFacial"), None)
    sub = next((b for b in parsed if b.object_type in ("pvBiFacialSheds", "pvBiFacialTrackers")), None)
    if root is None:
        return {"enabled": None, "issue": "Malformed pvBiFacial block"}

    def _parse(block, key, cast):
        if block is None or key not in block.fields:
            return None
        try:
            return cast(block.one(key)[1])
        except ValueError:
            return None

    return {
        "enabled": True, "model": root.one("BifacialModel")[1] if "BifacialModel" in root.fields else None,
        "param_origin": root.one("ParamOrigin")[1] if "ParamOrigin" in root.fields else None,
        "ground_albedo": _parse(root, "GroundAlbedo", float), "bifaciality_factor": _parse(root, "BiFacialityFactor", float),
        "rear_shading_factor": _parse(root, "RearShdFactor", float), "mismatch_loss_factor": _parse(root, "MismatchLossFactor", float),
        "sub_model_type": sub.object_type if sub is not None else None,
        "height_above_ground_m": _parse(sub, "HeightGr", float), "pitch_user_m": _parse(sub, "PitchUser", float),
        "n_sheds_user": _parse(sub, "NShedsUser", int),
        "shed_pitch_from_user": sub.one("ShedPitchComesFromUser")[1] == "True" if sub is not None and "ShedPitchComesFromUser" in sub.fields else None,
    }


def _edit_block_fields_in_place(block_lines: list[str], edits: dict[str, str], *, start_idx: int, end_idx: int, stop_at_child: bool, ending: str) -> int:
    if not edits:
        return 0
    existing: dict[str, list[int]] = {}
    insert_at, field_indent = end_idx, None
    for i in range(start_idx + 1, end_idx):
        if stop_at_child and block_lines[i].strip().startswith("PVObject_"):
            insert_at = i
            break
        if match := ASSIGNMENT.fullmatch(block_lines[i].rstrip("\r\n")):
            field_indent = field_indent or match[1]
            existing.setdefault(match[2], []).append(i)
    if field_indent is None:
        field_indent = block_lines[start_idx][:len(block_lines[start_idx]) - len(block_lines[start_idx].lstrip())] + "  "
    to_insert = []
    for key, rendered in edits.items():
        if indices := existing.get(key):
            if len(indices) != 1:
                raise ValueError(f"pvBiFacial field {key} occurs multiple times")
            block_lines[indices[0]] = replace_value(block_lines[indices[0]], rendered)
        else:
            to_insert.append(f"{field_indent}{key}={rendered}{ending}")
    if to_insert:
        block_lines[insert_at:insert_at] = to_insert
    return len(to_insert)


def replace_bifacial(lines: list[str], orient_bounds: tuple[int, int], updates: dict) -> dict:
    """Mount, update, or remove PVObject_Bifacial=pvBiFacial inside a TOrientGroup in place."""
    start, end = orient_bounds
    ending = "\r\n" if lines[start].endswith("\r\n") else "\n"
    indent = lines[start][:len(lines[start]) - len(lines[start].lstrip())]
    version = field_type = None
    for i in range(start + 1, end):
        if lines[i].strip().startswith("PVObject_"):
            break
        if match := ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n")):
            if match[2] == "Version":
                version = match[4]
            elif match[2] == "FieldType":
                field_type = match[4]
    if version not in NATIVE_TEXT_OR_SEED:
        raise ValueError(f"Bifacial editing requires pvOrient Version={SEED_LABEL}")
    if field_type not in SUPPORTED_FIELD_TYPES:
        raise ValueError(f"Bifacial model is not supported on FieldType={field_type!r}; supported types: {sorted(SUPPORTED_FIELD_TYPES)}")
    if not isinstance(updates, dict):
        raise ValueError("bifacial configuration must be an object")
    if unknown := sorted(set(updates) - ALLOWED_KEYS):
        raise ValueError("Unknown bifacial fields: " + ", ".join(unknown))
    enabled = _strict_bool(updates.get("enabled", True), "bifacial.enabled")
    before = bifacial_state(lines, orient_bounds)
    if before.get("issue"):
        raise ValueError(before["issue"])
    span = _find_bifacial_span(lines, orient_bounds)
    if not enabled:
        if span is not None:
            del lines[span[0]:span[1] + 1]
        return {"before": before, "after": {"enabled": False}, "action": "removed" if span is not None else "unchanged"}
    if span is not None:
        root_edits, sub_edits = {}, {}
        for key, (target, native_key, fmt) in _BIFACIAL_TARGETS.items():
            if key in updates:
                rendered = fmt(_validate_bifacial_field(key, updates[key], field_type=field_type))
                (root_edits if target == "root" else sub_edits)[native_key] = rendered
        block_lines = list(lines[span[0]:span[1] + 1])
        added_root = _edit_block_fields_in_place(block_lines, root_edits, start_idx=0, end_idx=len(block_lines) - 1, stop_at_child=True, ending=ending)
        added_sub = 0
        if sub_edits:
            sub_start = next((i for i in range(1, len(block_lines) - 1) if block_lines[i].strip() in ("PVObject_BiFacialShedModel=pvBiFacialSheds", "PVObject_BiFacialTrackerModel=pvBiFacialTrackers")), None)
            if sub_start is None:
                raise ValueError("Existing pvBiFacial block lacks a supported shed/tracker sub-model")
            sub_end = next((i for i in range(sub_start + 1, len(block_lines) - 1) if block_lines[i].strip() in ("End of PVObject pvBiFacialSheds", "End of PVObject pvBiFacialTrackers")), None)
            if sub_end is None:
                raise ValueError("Unclosed pvBiFacial sub-model block")
            added_sub = _edit_block_fields_in_place(block_lines, sub_edits, start_idx=sub_start, end_idx=sub_end, stop_at_child=False, ending=ending)
        lines[span[0]:span[1] + 1] = block_lines
        return {"before": before, "after": bifacial_state(lines, (start, end + added_root + added_sub)), "action": "updated"}
    cfg = normalize_bifacial_spec(updates, field_type=field_type, existing=None)
    rendered = [line + ending for line in render_bifacial_block(cfg, field_type=field_type, indent=indent)]
    insert_at = next((i for i in range(start + 1, end) if lines[i].strip().startswith("PVObject_ShdTable")), end)
    lines[insert_at:insert_at] = rendered
    return {"before": before, "after": bifacial_state(lines, (start, end + len(rendered))), "action": "mounted"}


# --- Seasonal tilt ---

SEASONAL_FIELDS = ("SummerTilt", "WinterTilt", "WinterMonths")
SEASONAL_BOOLS = ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver")


def _seasonal_angle(value, label):
    number = finite(value, label, maximum=90)
    scaled = Decimal(str(number)) * 10
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{label} must use 0.1 degree steps")
    return number


def _seasonal_literal(raw, label):
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{label} must be a finite number") from None
    if not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return value


def _read_seasonal(lines, bounds):
    for i in range(bounds[0] + 1, bounds[1]):
        text, match = lines[i].strip(), ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))
        if text.startswith(("End of ", "EndTags")) or (match and (match[2] == "PVObject_" or (match[2] != "Comment" and re.fullmatch(r"T[A-Z]\w*", match[4])))):
            raise ValueError("Nested objects are not supported in an orientation block")
    fields = direct_fields(lines, bounds)
    if any(len(entries) > 1 for entries in fields.values()):
        raise ValueError("Duplicate fields are not supported in an orientation block")
    if one(fields, "Version")[1] not in NATIVE_TEXT:
        raise ValueError(f"Seasonal editing requires a native {TEXT_LABEL} orientation")
    flags = one(fields, "Flags")[1]
    if not re.fullmatch(r"\$[0-9A-Fa-f]+", flags) or int(flags[1:], 16) != 0:
        raise ValueError("Seasonal editing requires orientation Flags=$00")
    if one(fields, "NoOrient")[1] != "1":
        raise ValueError("Seasonal editing requires NoOrient=1")
    kind = one(fields, "FieldType")[1]
    if kind not in ("FixedPlane", "SeasonalTilt"):
        raise ValueError("Seasonal editing requires FieldType FixedPlane or SeasonalTilt")
    one(fields, "FieldTilt")
    azim = one(fields, "FieldAzim")
    for name in SEASONAL_BOOLS:
        if one(fields, name)[1] != "False":
            raise ValueError(f"Seasonal editing requires {name}=False")
    present = [one(fields, name, False) for name in SEASONAL_FIELDS]
    if kind == "FixedPlane" and any(e is not None for e in present):
        raise ValueError("A FixedPlane orientation must not carry seasonal fields")
    if kind == "SeasonalTilt" and any(e is None for e in present):
        raise ValueError("A SeasonalTilt orientation must carry all three seasonal fields")
    comment, base = one(fields, "Comment", False), one(fields, "BaseSlope", False)
    if base is not None and kind == "SeasonalTilt":
        raise ValueError("BaseSlope is not a verified key on a SeasonalTilt orientation")
    before = {"field_type": kind, "orientation_name": comment[1] if comment else None,
              "base_slope": _seasonal_literal(base[1], "BaseSlope") if base else None,
              "summer_tilt": None, "winter_tilt": None, "winter_months": None}
    if present[0] is not None:
        raw_mask = present[2][1]
        if not re.fullmatch(r"\$[0-9A-Fa-f]{1,4}", raw_mask):
            raise ValueError("WinterMonths must be a native hexadecimal month mask")
        mask = int(raw_mask[1:], 16)
        if mask & ~0xFFF:
            raise ValueError("WinterMonths must use only the twelve month bits")
        before.update(
            summer_tilt=finite(_seasonal_literal(present[0][1], "SummerTilt"), "SummerTilt", maximum=90),
            winter_tilt=finite(_seasonal_literal(present[1][1], "WinterTilt"), "WinterTilt", maximum=90),
            winter_months=[m for m in range(1, 13) if (mask >> (m - 1)) & 1],
        )
    return fields, azim, before, base


def replace_seasonal(lines, bounds, updates):
    """Rewrite the owning orientation as native SeasonalTilt; commits to `lines` only on success."""
    req = ("summer_tilt", "winter_tilt", "winter_months")
    if not isinstance(updates, dict) or (set(updates) - (set(req) | {"orientation_name"})) or not set(req) <= set(updates):
        raise ValueError("Seasonal updates: summer_tilt, winter_tilt, winter_months, orientation_name")
    summer, winter = _seasonal_angle(updates["summer_tilt"], "summer_tilt"), _seasonal_angle(updates["winter_tilt"], "winter_tilt")
    months_raw = updates["winter_months"]
    if not isinstance(months_raw, list) or not 1 <= len(months_raw) <= 11:
        raise ValueError("winter_months must be a list of 1 to 11 months")
    if any(not isinstance(m, int) or isinstance(m, bool) or not 1 <= m <= 12 for m in months_raw):
        raise ValueError("winter_months must contain integers in 1..12")
    if len(set(months_raw)) != len(months_raw):
        raise ValueError("winter_months must not repeat a month")
    months = sorted(months_raw)
    name = None
    if "orientation_name" in updates:
        name = updates["orientation_name"]
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValueError("orientation_name must be a non-empty trimmed string")
        if len(name.encode("utf-8")) > 80 or not name.isprintable():
            raise ValueError("orientation_name must be 1..80 printable UTF-8 bytes")
    work = list(lines)
    fields, azim, before, base = _read_seasonal(work, bounds)
    if name is not None and before["orientation_name"] is None:
        raise ValueError("orientation_name requires an owning Comment")
    if base is not None:
        if before["base_slope"] != 0:
            raise ValueError("Non-zero BaseSlope is not a verified layout")
        del work[base[0]]
        fields, azim, _, _ = _read_seasonal(work, (bounds[0], bounds[1] - 1))
    ending = "\r\n" if work[bounds[0]].endswith("\r\n") else "\n"
    indent = re.match(r"[ \t]*", work[azim[0]])[0]
    target = {"FieldType": "SeasonalTilt", "FieldTilt": "0.0", "SummerTilt": format(summer, ".1f"),
              "WinterTilt": format(winter, ".1f"), "WinterMonths": "$%04X" % sum(1 << (m - 1) for m in months)}
    inserted = []
    for key, value in target.items():
        entry = one(fields, key, False)
        if entry is None:
            inserted.append(f"{indent}{key}={value}{ending}")
        elif entry[1] != value:
            work[entry[0]] = replace_value(work[entry[0]], value)
    if name is not None and before["orientation_name"] != name:
        work[fields["Comment"][0][0]] = replace_value(work[fields["Comment"][0][0]], name)
    if inserted:
        work[azim[0] + 1:azim[0] + 1] = inserted
    lines[:] = work
    return {"before": before, "after": {"field_type": "SeasonalTilt", "orientation_name": name if name is not None else before["orientation_name"], "base_slope": None, "summer_tilt": summer, "winter_tilt": winter, "winter_months": months}}


# --- Shed / sun-shield geometry ---

SHED_TYPES = ("Sheds", "SunShields")
SHED_WRITABLE = {
    "field_tilt_deg": "FieldTilt", "field_azimuth_deg": "FieldAzim", "pitch_m": "Pitch",
    "collector_width_m": "CollectorWidth", "inactive_band_width_1_m": "InactiveBandWidth1",
    "inactive_band_width_2_m": "InactiveBandWidth2", "shed_count": "NSheds",
    "module_columns": "NModInWidth", "cell_width_m": "CellWidth",
    "shading_factor": "FracShadingFactor", "electrical_effect": "UsesElectricalEffect",
}
_SHED_ANGLES = ("FieldTilt", "FieldAzim")
_SHED_COUNTS = ("NSheds", "NModInWidth")
_SHED_ANGLE_LIMITS = {"FieldTilt": (Decimal("0"), Decimal("90")), "FieldAzim": (Decimal("-180"), Decimal("180"))}
_SHED_HEX_FLAG = re.compile(r"\$[0-9A-Fa-f]{1,8}")
_SHED_MAX_COUNT = 2147483647


def _require_shed_scope(store, lines, arrays, system):
    root = root_block(lines)
    if root.one("Version")[1] not in NATIVE_TEXT_OR_SEED:
        raise ValueError(f"Requires a native {SEED_LABEL} variant")
    if not _SHED_HEX_FLAG.fullmatch(root.one("Flags")[1]):
        raise ValueError("Root Flags must be hexadecimal")
    if system is None or len(arrays) != 1:
        raise ValueError("Requires one pvSystem and one subarray")
    sys_root = root_block(lines, system)
    if sys_root.one("SystemType")[1] != "Grid":
        raise ValueError("Requires SystemType=Grid")
    if "Flags" in sys_root.fields and not _SHED_HEX_FLAG.fullmatch(sys_root.one("Flags")[1]):
        raise ValueError("pvSystem Flags must be hexadecimal")
    if (main := store._main_array(lines)) is None:
        raise ValueError("A pvMainArray section is required")
    main_root = root_block(lines, main)
    if not _SHED_HEX_FLAG.fullmatch(main_root.one("Flags")[1]) or main_root.one("Flags2")[1] != "$082A":
        raise ValueError("Main array requires hexadecimal Flags and Flags2=$082A")
    sub_root = root_block(lines, next(iter(arrays.values())))
    if sub_root.one("Version")[1] not in NATIVE_TEXT_OR_SEED or not _SHED_HEX_FLAG.fullmatch(sub_root.one("Flags")[1]):
        raise ValueError(f"Subarray requires native {SEED_LABEL} and hexadecimal Flags")
    _check_single_orient_subarray_strings(store, lines, sub_root)


def _shed_label(name):
    return next((k for k, v in SHED_WRITABLE.items() if v == name), name)


def _shed_domain(name, number):
    label = _shed_label(name)
    step = Decimal("0.1") if name in _SHED_ANGLES else Decimal("0.001")
    try:
        q = number.quantize(step)
    except (InvalidOperation, ArithmeticError):
        raise ValueError(f"{label} is outside the decimal range this adapter can step") from None
    if q != number:
        raise ValueError(f"{label} uses at most {1 if name in _SHED_ANGLES else 3} decimal places")
    if name in _SHED_ANGLE_LIMITS:
        low, high = _SHED_ANGLE_LIMITS[name]
        if not low <= number <= high:
            raise ValueError(f"{label} must stay within {low}..{high}")
    elif name == "FracShadingFactor" and not Decimal("0") <= number <= Decimal("1"):
        raise ValueError(f"{label} must stay within 0..1")
    elif name in ("Pitch", "CollectorWidth", "CellWidth") and number <= 0:
        raise ValueError(f"{label} must be positive")
    elif name in ("InactiveBandWidth1", "InactiveBandWidth2") and number < 0:
        raise ValueError(f"{label} must be nonnegative")
    return number


def _check_shed_domain(block):
    for name in SHED_WRITABLE.values():
        raw = block.one(name)[1]
        if name == "UsesElectricalEffect":
            if raw not in ("True", "False"):
                raise ValueError(f"{_shed_label(name)} must already be True or False in the record")
        elif name in _SHED_COUNTS:
            if not raw.isdigit() or not 1 <= int(raw) <= _SHED_MAX_COUNT:
                raise ValueError(f"{_shed_label(name)} must already be a positive native integer")
        else:
            _shed_domain(name, _decimal(raw, _shed_label(name)))
    bands = _decimal(block.one("InactiveBandWidth1")[1], "inactive_band_width_1_m") + _decimal(block.one("InactiveBandWidth2")[1], "inactive_band_width_2_m")
    if not bands < _decimal(block.one("CollectorWidth")[1], "collector_width_m"):
        raise ValueError("inactive band widths must stay below the collector width")


def orient_block(lines):
    """Return the one directly owned shed/sun-shield record after checking its shape."""
    blocks, block = _single_orient_record(lines)
    fieldtype = block.one("FieldType")[1]
    if fieldtype not in SHED_TYPES:
        raise ValueError(f"Only a Sheds or SunShields record is supported, not {fieldtype}")
    required = set(SHED_WRITABLE.values()) | {"NoOrient", "FieldType"}
    if not required <= set(block.fields):
        raise ValueError(f"{fieldtype} record is missing required shed fields: {', '.join(sorted(required - set(block.fields)))}")
    for name in required:
        block.one(name)
    if "Version" in block.fields and block.one("Version")[1] not in NATIVE_TEXT_OR_SEED:
        raise ValueError(f"pvOrient requires Version={SEED_LABEL}")
    if "Flags" in block.fields and block.one("Flags")[1] != "$00":
        raise ValueError("pvOrient requires Flags=$00")
    if block.one("NoOrient")[1] != "1":
        raise ValueError("Only orientation 1 is supported")
    targets = set(SHED_WRITABLE.values())
    for other in blocks:
        if other is not block and other.object_type not in ("pvBiFacial", "pvBiFacialSheds", "pvBiFacialTrackers") and (set(other.fields) & targets):
            raise ValueError("Shed geometry fields must exist only in the owning pvOrient record")
    for name in ("UsesTiltAver", "UsesAzimAver", "UsesSlopeAver"):
        if name in block.fields and block.one(name)[1] != "False":
            raise ValueError(f"{name} must stay False")
    if "BTNoTrackers" in block.fields and block.one("BTNoTrackers")[1] != "-1,-1,":
        raise ValueError("BTNoTrackers must stay -1,-1,")
    _check_shed_domain(block)
    return block


def shed_values(geometry):
    """Validate the declared fields and render the exact native tokens."""
    if not isinstance(geometry, dict) or not geometry:
        raise ValueError("geometry must declare at least one field")
    rendered = {}
    for key, value in geometry.items():
        if (name := SHED_WRITABLE.get(key)) is None:
            raise ValueError(f"Unknown shed geometry field: {key}")
        if name == "UsesElectricalEffect":
            if type(value) is not bool:
                raise ValueError(f"{_shed_label(name)} must be a strict boolean")
            rendered[name] = "True" if value else "False"
        elif name in _SHED_COUNTS:
            if type(value) is bool or not isinstance(value, int) or not 1 <= value <= _SHED_MAX_COUNT:
                raise ValueError(f"{_shed_label(name)} must be a strict integer" if type(value) is bool or not isinstance(value, int) else f"{_shed_label(name)} must stay within 1..{_SHED_MAX_COUNT}")
            rendered[name] = str(value)
        else:
            if type(value) is bool or not isinstance(value, (int, float)):
                raise ValueError(f"{_shed_label(name)} must be a strict finite number")
            rendered[name] = f"{_shed_domain(name, _decimal(str(value), _shed_label(name))):.{(1 if name in _SHED_ANGLES else 3)}f}"
    return rendered


def create_shed_variant(store, *, project, source_variant, new_variant, geometry, dry_run=True, variant_name=None, add_utf8_bom=False):
    """Preview or create a NEW VC editing the shed or sun-shield geometry."""
    def build(*, lines, **_):
        arrays, system = store._sections(lines)
        _require_shed_scope(store, lines, arrays, system)
        block = orient_block(lines)
        fieldtype, values = block.one("FieldType")[1], shed_values(geometry)
        candidate_lines = list(lines)
        for name, rendered in values.items():
            idx = block.one(name)[0]
            candidate_lines[idx] = replace_value(candidate_lines[idx], rendered)

        def verify(checked, _, res):
            _require_shed_scope(store, checked, *store._sections(checked))
            verified = orient_block(checked)
            if verified.one("FieldType")[1] != fieldtype or any(verified.one(n)[1] != r for n, r in values.items()):
                raise ValueError("Candidate did not keep the requested shed geometry")
            res.update(field_type=fieldtype, geometry=values)
            res["warnings"].extend([
                f"Only one new VC is written: the declared geometry fields on the existing {fieldtype} record and an optional root Comment change. The field type is never converted, and orientation list, module model, inverter, strings, weather and topology stay inherited. Saving is not native consumption.",
                "Values are stored text with the recorded precision: angles keep one decimal, lengths and the shading factor keep three, counts stay positive integers. Ranges are adapter sanity limits, not vendor or measured GUI bounds. What NSheds and NModInWidth mean physically is not established, so they are written as declared values rather than interpreted. No self-shading, electrical grouping or 2D shading is computed here; verify native hourly results for the matching year.",
            ])

        return {"candidate_lines": candidate_lines, "verify": verify}

    return run_new_variant_transaction(
        store, project=project, source_variant=source_variant, new_variant=new_variant,
        dry_run=dry_run, add_utf8_bom=add_utf8_bom, variant_name=variant_name, build=build,
        source_changed_error="Template changed while preparing the shed variant",
    )

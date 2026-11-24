"""Native PVsyst 8.1 pvBiFacial readback, rendering, and transactional editing.

Supports mounting pvBiFacial from scratch, updating fields in place, or removing it
on Sheds / UnlimSheds, FixedPlane + RegularSheds, UnlimTracking, and TrackHorizNSAxis
orientations. Verified empirically against PVsystCLI 8.1.7.
"""
from __future__ import annotations

from pathlib import Path

from pvsyst_auto.components.editor import ASSIGNMENT, objects, replace_value
from pvsyst_auto.components.pan import pan_blocks
from pvsyst_auto.versions import NATIVE_TEXT_OR_SEED, SEED_LABEL

BIFACIAL_MODELS = ("Sheds2D", "Trackers2D")
PARAM_ORIGINS = ("UnlimSheds", "RegularSheds")
TRACKING_FIELD_TYPES = frozenset({"UnlimTracking", "TrackHorizNSAxis"})
SHED_FIELD_TYPES = frozenset({"Sheds", "FixedPlane", "RegularSheds"})
SUPPORTED_FIELD_TYPES = TRACKING_FIELD_TYPES | SHED_FIELD_TYPES

ALLOWED_KEYS = frozenset({
    "enabled",
    "model",
    "param_origin",
    "ground_albedo",
    "bifaciality_factor",
    "rear_shading_factor",
    "mismatch_loss_factor",
    "height_above_ground_m",
    "pitch_user_m",
    "n_sheds_user",
    "shed_pitch_from_user",
})


from pvsyst_auto.validation import finite_number as _finite


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


def pan_is_bifacial(components, pan_name: str, *, path: str | Path | None = None,
                    library: str | None = None) -> bool | None:
    """Return True/False for a readable text .PAN based on TPVModule.isBiFacial (Flags bit 23 / $00800000)."""
    if components is None:
        return None
    candidates: list[Path] = []
    if path:
        p = Path(path)
        if p.is_file():
            candidates.append(p)
    if not candidates:
        name = Path(pan_name).name
        libs = (library,) if library in ("workspace", "builtin") else ("workspace", "builtin")
        for lib in libs:
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
            raw = components._read(pan_path)
            root, _ = pan_blocks(raw.decode("utf-8-sig"))
            flags_raw = root.one("Flags")[1].strip()
            if not flags_raw.startswith("$"):
                return None
            return bool(int(flags_raw[1:], 16) & 0x00800000)
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


_BIFACIAL_TARGETS = {
    "model": ("root", "BifacialModel", str),
    "param_origin": ("root", "ParamOrigin", str),
    "ground_albedo": ("root", "GroundAlbedo", lambda v: f"{v:.3f}"),
    "bifaciality_factor": ("root", "BiFacialityFactor", lambda v: f"{v:.3f}"),
    "rear_shading_factor": ("root", "RearShdFactor", lambda v: f"{v:.3f}"),
    "mismatch_loss_factor": ("root", "MismatchLossFactor", lambda v: f"{v:.3f}"),
    "height_above_ground_m": ("sub", "HeightGr", lambda v: f"{v:.3f}"),
    "pitch_user_m": ("sub", "PitchUser", lambda v: f"{v:.3f}"),
    "n_sheds_user": ("sub", "NShedsUser", str),
    "shed_pitch_from_user": ("sub", "ShedPitchComesFromUser", lambda v: "True" if v else "False"),
}


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


def normalize_bifacial_spec(spec: dict, *, field_type: str | None = None,
                            existing: dict | None = None) -> dict:
    """Validate and normalize a bifacial configuration dict."""
    if not isinstance(spec, dict):
        raise ValueError("bifacial configuration must be an object")
    unknown = sorted(set(spec) - ALLOWED_KEYS)
    if unknown:
        raise ValueError("Unknown bifacial fields: " + ", ".join(unknown))

    enabled = spec.get("enabled", True)
    _strict_bool(enabled, "bifacial.enabled")
    if not enabled:
        return {"enabled": False}

    default_model, default_origin = _default_model_and_origin(field_type)
    ex = existing or {}

    def _ex(key: str, default):
        val = ex.get(key)
        return default if val is None else val

    base = {
        "model": _ex("model", default_model),
        "param_origin": _ex("param_origin", default_origin),
        "ground_albedo": _ex("ground_albedo", 0.30),
        "bifaciality_factor": _ex("bifaciality_factor", 0.80),
        "rear_shading_factor": _ex("rear_shading_factor", 0.05),
        "mismatch_loss_factor": _ex("mismatch_loss_factor", 0.10),
        "height_above_ground_m": _ex("height_above_ground_m",
                                     2.50 if default_model == "Trackers2D" else 1.00),
        "pitch_user_m": _ex("pitch_user_m", -1.0),
        "n_sheds_user": _ex("n_sheds_user", -1),
        "shed_pitch_from_user": _ex("shed_pitch_from_user", False),
    }

    normalized = {
        key: _validate_bifacial_field(key, spec.get(key, base[key]), field_type=field_type)
        for key in _BIFACIAL_TARGETS
    }
    return {"enabled": True, **normalized}


def render_bifacial_block(spec: dict, *, field_type: str | None = None,
                          indent: str = "    ") -> list[str]:
    """Render a complete PVsyst 8.1 PVObject_Bifacial=pvBiFacial block (no trailing newline)."""
    cfg = normalize_bifacial_spec(spec, field_type=field_type)
    if not cfg["enabled"]:
        return []
    sub_key, sub_type = (
        ("PVObject_BiFacialTrackerModel", "pvBiFacialTrackers")
        if cfg["model"] == "Trackers2D"
        else ("PVObject_BiFacialShedModel", "pvBiFacialSheds")
    )
    return [
        f"{indent}PVObject_Bifacial=pvBiFacial",
        f"{indent}  Flags=$00",
        f"{indent}  BifacialModel={cfg['model']}",
        f"{indent}  ParamOrigin={cfg['param_origin']}",
        f"{indent}  GroundAlbedo={cfg['ground_albedo']:.3f}",
        f"{indent}  BiFacialityFactor={cfg['bifaciality_factor']:.3f}",
        f"{indent}  RearShdFactor={cfg['rear_shading_factor']:.3f}",
        f"{indent}  MismatchLossFactor={cfg['mismatch_loss_factor']:.3f}",
        f"{indent}  {sub_key}={sub_type}",
        f"{indent}    Flags=$00",
        f"{indent}    HeightGr={cfg['height_above_ground_m']:.3f}",
        f"{indent}    PitchUser={cfg['pitch_user_m']:.3f}",
        f"{indent}    NShedsUser={cfg['n_sheds_user']}",
        f"{indent}    ShedPitchComesFromUser={'True' if cfg['shed_pitch_from_user'] else 'False'}",
        f"{indent}  End of PVObject {sub_type}",
        f"{indent}End of PVObject pvBiFacial",
    ]


def _find_bifacial_span(lines: list[str], bounds: tuple[int, int]) -> tuple[int, int] | None:
    start, end = bounds
    starts = [i for i in range(start + 1, end) if lines[i].strip() == "PVObject_Bifacial=pvBiFacial"]
    if not starts:
        return None
    if len(starts) > 1:
        raise ValueError("Orientation declares multiple PVObject_Bifacial blocks")
    b_start = starts[0]
    b_end = next((i for i in range(b_start + 1, end) if lines[i].strip() == "End of PVObject pvBiFacial"), None)
    if b_end is None:
        raise ValueError("Unclosed PVObject_Bifacial=pvBiFacial block")
    return b_start, b_end


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
    slice_text = "".join(lines[span[0]:span[1] + 1])
    try:
        parsed = objects(slice_text)
    except ValueError as exc:
        return {"enabled": None, "issue": str(exc)}
    root = next((b for b in parsed if b.object_type == "pvBiFacial"), None)
    sub = next((b for b in parsed if b.object_type in ("pvBiFacialSheds", "pvBiFacialTrackers")), None)
    if root is None:
        return {"enabled": None, "issue": "Malformed pvBiFacial block"}

    def _num(block, key):
        if block is None or key not in block.fields:
            return None
        try:
            return float(block.one(key)[1])
        except ValueError:
            return None

    def _int(block, key):
        if block is None or key not in block.fields:
            return None
        try:
            return int(block.one(key)[1])
        except ValueError:
            return None

    state = {
        "enabled": True,
        "model": root.one("BifacialModel")[1] if "BifacialModel" in root.fields else None,
        "param_origin": root.one("ParamOrigin")[1] if "ParamOrigin" in root.fields else None,
        "ground_albedo": _num(root, "GroundAlbedo"),
        "bifaciality_factor": _num(root, "BiFacialityFactor"),
        "rear_shading_factor": _num(root, "RearShdFactor"),
        "mismatch_loss_factor": _num(root, "MismatchLossFactor"),
        "sub_model_type": sub.object_type if sub is not None else None,
        "height_above_ground_m": _num(sub, "HeightGr"),
        "pitch_user_m": _num(sub, "PitchUser"),
        "n_sheds_user": _int(sub, "NShedsUser"),
        "shed_pitch_from_user": (
            sub.one("ShedPitchComesFromUser")[1] == "True"
            if sub is not None and "ShedPitchComesFromUser" in sub.fields else None
        ),
    }
    return state


def _validate_partial_bifacial_updates(updates: dict, *, field_type: str | None) -> tuple[dict[str, str], dict[str, str]]:
    """Validate only requested keys for an in-place pvBiFacial edit, split into (root_fields, sub_fields)."""
    if not isinstance(updates, dict):
        raise ValueError("bifacial configuration must be an object")
    unknown = sorted(set(updates) - ALLOWED_KEYS)
    if unknown:
        raise ValueError("Unknown bifacial fields: " + ", ".join(unknown))

    root_edits: dict[str, str] = {}
    sub_edits: dict[str, str] = {}
    for key, (target, native_key, fmt) in _BIFACIAL_TARGETS.items():
        if key in updates:
            val = _validate_bifacial_field(key, updates[key], field_type=field_type)
            rendered = fmt(val)
            if target == "root":
                root_edits[native_key] = rendered
            else:
                sub_edits[native_key] = rendered
    return root_edits, sub_edits


def _edit_block_fields_in_place(block_lines: list[str], edits: dict[str, str], *,
                                start_idx: int, end_idx: int, stop_at_child: bool,
                                ending: str) -> int:
    """Update existing keys or insert newly requested keys inside a section, preserving untouched lines."""
    if not edits:
        return 0
    existing: dict[str, list[int]] = {}
    insert_at = end_idx
    field_indent: str | None = None
    for i in range(start_idx + 1, end_idx):
        stripped = block_lines[i].strip()
        if stop_at_child and stripped.startswith("PVObject_"):
            insert_at = i
            break
        match = ASSIGNMENT.fullmatch(block_lines[i].rstrip("\r\n"))
        if not match:
            continue
        if field_indent is None:
            field_indent = match[1]
        existing.setdefault(match[2], []).append(i)
    if field_indent is None:
        header_indent = block_lines[start_idx][:len(block_lines[start_idx]) - len(block_lines[start_idx].lstrip())]
        field_indent = header_indent + "  "

    to_insert: list[str] = []
    for key, rendered in edits.items():
        indices = existing.get(key)
        if indices:
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

    version = None
    field_type = None
    for i in range(start + 1, end):
        stripped = lines[i].strip()
        if stripped.startswith("PVObject_"):
            break
        match = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))
        if not match:
            continue
        if match[2] == "Version":
            version = match[4]
        elif match[2] == "FieldType":
            field_type = match[4]

    if version not in NATIVE_TEXT_OR_SEED:
        raise ValueError(f"Bifacial editing requires pvOrient Version={SEED_LABEL}")
    if field_type not in SUPPORTED_FIELD_TYPES:
        raise ValueError(f"Bifacial model is not supported on FieldType={field_type!r}; "
                         f"supported types: {sorted(SUPPORTED_FIELD_TYPES)}")

    if not isinstance(updates, dict):
        raise ValueError("bifacial configuration must be an object")
    unknown = sorted(set(updates) - ALLOWED_KEYS)
    if unknown:
        raise ValueError("Unknown bifacial fields: " + ", ".join(unknown))
    enabled = updates.get("enabled", True)
    _strict_bool(enabled, "bifacial.enabled")

    before = bifacial_state(lines, orient_bounds)
    if before.get("issue"):
        raise ValueError(before["issue"])
    span = _find_bifacial_span(lines, orient_bounds)

    if not enabled:
        if span is not None:
            del lines[span[0]:span[1] + 1]
        return {"before": before, "after": {"enabled": False}, "action": "removed" if span is not None else "unchanged"}

    if span is not None:
        root_edits, sub_edits = _validate_partial_bifacial_updates(updates, field_type=field_type)
        block_lines = list(lines[span[0]:span[1] + 1])
        added_root = _edit_block_fields_in_place(
            block_lines, root_edits, start_idx=0, end_idx=len(block_lines) - 1,
            stop_at_child=True, ending=ending,
        )
        added_sub = 0
        if sub_edits:
            sub_start = next(
                (i for i in range(1, len(block_lines) - 1)
                 if block_lines[i].strip() in (
                     "PVObject_BiFacialShedModel=pvBiFacialSheds",
                     "PVObject_BiFacialTrackerModel=pvBiFacialTrackers",
                 )),
                None,
            )
            if sub_start is None:
                raise ValueError("Existing pvBiFacial block lacks a supported shed/tracker sub-model")
            sub_end = next(
                (i for i in range(sub_start + 1, len(block_lines) - 1)
                 if block_lines[i].strip() in (
                     "End of PVObject pvBiFacialSheds",
                     "End of PVObject pvBiFacialTrackers",
                 )),
                None,
            )
            if sub_end is None:
                raise ValueError("Unclosed pvBiFacial sub-model block")
            added_sub = _edit_block_fields_in_place(
                block_lines, sub_edits, start_idx=sub_start, end_idx=sub_end,
                stop_at_child=False, ending=ending,
            )
        lines[span[0]:span[1] + 1] = block_lines
        new_end = end + added_root + added_sub
        after = bifacial_state(lines, (start, new_end))
        return {"before": before, "after": after, "action": "updated"}

    cfg = normalize_bifacial_spec(updates, field_type=field_type, existing=None)
    rendered = [line + ending for line in render_bifacial_block(cfg, field_type=field_type, indent=indent)]
    # Insert right before End of TOrientGroup (or before PVObject_ShdTableLin if present).
    insert_at = end
    for i in range(start + 1, end):
        if lines[i].strip().startswith("PVObject_ShdTable"):
            insert_at = i
            break
    lines[insert_at:insert_at] = rendered
    new_end = end + len(rendered)
    after = bifacial_state(lines, (start, new_end))
    return {"before": before, "after": after, "action": "mounted"}

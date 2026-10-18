"""Create a NEW VC with five verified native UnlimTracking controls; no tracking model."""
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
MODEL_KEYS = ("axis_azimuth_deg", "min_rotation_deg", "max_rotation_deg",
              "backtracking", "irradiance_optimization")
TARGETS = {"axis_azimuth_deg": "AxisAzim", "min_rotation_deg": "MinPhi",
           "max_rotation_deg": "MaxPhi", "backtracking": "UsesBacktracking",
           "irradiance_optimization": "UsesIrradOptimiz"}
# Adapter ranges only; the native GUI limits are not measured.
RANGES = {"AxisAzim": (Decimal("-20"), Decimal("20")),
          "MinPhi": (Decimal("-60"), Decimal("-45")),
          "MaxPhi": (Decimal("45"), Decimal("60"))}
GEOMETRY = {"Pitch": Decimal("6.600"), "CollectorWidth": Decimal("3.000"),
            "InactiveBandWidth1": Decimal("0.020"), "InactiveBandWidth2": Decimal("0.020"),
            "CellWidth": Decimal("0.156"), "FracShadingFactor": Decimal("1.000")}
INTEGERS = {"NSheds": "10", "NModInWidth": "3"}
FALSE_FLAGS = ("UsesElectricalEffect", "UsesTiltAver", "UsesAzimAver", "UsesSlopeAver")
BT_NO_TRACKERS = "-1,-1,"
OWN = ("Comment", "Version", "Flags", "NoOrient", "FieldType", "AxisAzim", "MinPhi", "MaxPhi",
       "UsesBacktracking", "UsesIrradOptimiz", "Pitch", "CollectorWidth", "InactiveBandWidth1",
       "InactiveBandWidth2", "NSheds", "UsesElectricalEffect", "FracShadingFactor",
       "NModInWidth", "CellWidth", "UsesTiltAver", "UsesAzimAver", "UsesSlopeAver", "BTNoTrackers")


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


def _angle(number, key):
    """Shared native-token domain: adapter range first, then the 0.1 step, for source and request."""
    low, high = RANGES[TARGETS[key]]
    if not low <= number <= high:
        raise ValueError(f"{key} must stay within the adapter range {low}..{high}")
    if number != number.quantize(STEP):
        raise ValueError(f"{key} must use at most 0.1 steps (adapter range, not a native limit)")
    return number


def _source_controls(block):
    """The saved record must already sit inside the same domain; a bad value is blocked, not overwritten."""
    for key in ("axis_azimuth_deg", "min_rotation_deg", "max_rotation_deg"):
        _angle(_decimal(block.one(TARGETS[key])[1], key), key)
    for name in ("UsesBacktracking", "UsesIrradOptimiz"):
        if block.one(name)[1] not in ("True", "False"):
            raise ValueError(f"{name} must already be True or False in the source record")


def tracking_values(tracking):
    """Check the five declared controls and render the exact native tokens."""
    if not isinstance(tracking, dict) or set(tracking) != set(MODEL_KEYS):
        raise ValueError("tracking must contain exactly the five declared controls")
    values = {}
    for key in ("axis_azimuth_deg", "min_rotation_deg", "max_rotation_deg"):
        value = tracking[key]
        if type(value) is bool or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a strict finite number")
        number = _angle(_decimal(str(value), key), key)
        values[TARGETS[key]] = f"{number:.1f}"
    for key, name in (("backtracking", "UsesBacktracking"),
                      ("irradiance_optimization", "UsesIrradOptimiz")):
        if type(tracking[key]) is not bool:
            raise ValueError(f"{key} must be a strict boolean")
        values[name] = "True" if tracking[key] else "False"
    if not Decimal(values["MinPhi"]) < Decimal(values["MaxPhi"]):
        raise ValueError("min_rotation_deg must stay below max_rotation_deg")
    return values


def orient_block(lines):
    """Return the one directly owned UnlimTracking record after checking every fixed field."""
    blocks = objects(_view(lines))
    records = [block for block in blocks if block.object_type == "pvOrient"]
    if len(records) != 1:
        raise ValueError("Requires exactly one pvOrient record")
    block = records[0]
    if block.path != "PVObject_" or not block.end_line:
        raise ValueError("pvOrient must be the complete directly owned record")
    if set(block.fields) != set(OWN):
        raise ValueError("Unknown or missing fields in the pvOrient record")
    for name in OWN:
        block.one(name)  # every owning field must occur exactly once; duplicates stop here
    for other in blocks:
        if other is block:
            continue
        if set(other.fields) & set(TARGETS.values()):
            raise ValueError("Tracking controls must exist only in the owning pvOrient record")
    if block.one("Version")[1] != "8.1.6" or block.one("Flags")[1] != "$00":
        raise ValueError("pvOrient requires Version=8.1.6 and Flags=$00")
    if block.one("NoOrient")[1] != "1":
        raise ValueError("Only orientation 1 is supported")
    if block.one("FieldType")[1] != "UnlimTracking":
        raise ValueError("Only an existing UnlimTracking record is supported")
    for name, expected in GEOMETRY.items():
        if _decimal(block.one(name)[1], name) != expected:
            raise ValueError(f"{name} must stay at its verified native value")
    for name, expected in INTEGERS.items():
        if block.one(name)[1] != expected:
            raise ValueError(f"{name} must stay at {expected}")
    for name in FALSE_FLAGS:
        if block.one(name)[1] != "False":
            raise ValueError(f"{name} must stay False")
    if block.one("BTNoTrackers")[1] != BT_NO_TRACKERS:
        raise ValueError(f"BTNoTrackers must stay {BT_NO_TRACKERS}")
    _source_controls(block)
    return block


def _require_scope(store, lines, arrays, system):
    """Verified native 8.1.6 PV-only single-subarray Grid variant around one UnlimTracking record."""
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
    if system_root.one("SystemType")[1] != "Grid" or system_root.one("Flags")[1] != "$06000000":
        raise ValueError("Requires SystemType=Grid with Flags=$06000000")
    main = store._main_array(lines)
    if main is None:
        raise ValueError("A pvMainArray section is required")
    main_root = _block_of(lines, main)
    if main_root.one("Flags")[1] != "$01900400" or main_root.one("Flags2")[1] != "$082A":
        raise ValueError("Main array requires the verified Flags=$01900400 and Flags2=$082A")
    sub_root = _block_of(lines, next(iter(arrays.values())))
    if sub_root.one("Version")[1] != "8.1.6" or sub_root.one("Flags")[1] != "$019E1002":
        raise ValueError("Subarray requires the verified 8.1.6 declaration and 019E1002 flags")
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
            values = tracking_values(tracking)
            block = orient_block(lines)
            candidate_lines = list(lines)
            for name, rendered in values.items():
                index, _ = block.one(name)
                candidate_lines[index] = replace_value(candidate_lines[index], rendered)
            bom_prefix = BOM if original.startswith(BOM) or add_utf8_bom else b""
            name_change = None
            if variant_name is not None:
                store._validate_display_name(variant_name, "variant_name")
                bom_prefix = store._display_name_prefix(original, variant_name, add_utf8_bom, "variant_name")
                index, = store._display_name_fields(candidate_lines, complete=True)
                before = candidate_lines[index].partition("=")[2].strip()
                candidate_lines[index] = replace_value(candidate_lines[index], variant_name)
                name_change = {"before": before, "after": variant_name}
            candidate_text = "".join(candidate_lines)
            candidate = bom_prefix + candidate_text.encode("utf-8")
            _, checked = store._text(candidate)
            checked_arrays, _ = store._sections(checked)
            verified = orient_block(checked)
            if len(checked_arrays) != 1 or any(verified.one(name)[1] != rendered
                                               for name, rendered in values.items()):
                raise ValueError("Candidate did not keep the requested tracking controls")
            result.update(status="ready", project=project, source_variant=source_variant,
                          variant=target.suffix[1:].upper(), tracking=values,
                          artifacts={"variant": str(target)},
                          bom_added=bool(bom_prefix) and not original.startswith(BOM))
            if name_change is not None:
                result["name_change"] = name_change
            result["warnings"].append(
                "Only one new VC is written: the five owning pvOrient controls and an optional root Comment "
                "change; geometry, BTNoTrackers, electrical-effect settings, PRJ, weather, topology, loads and "
                "storage stay inherited. Saving is not native consumption.")
            result["warnings"].append(
                "Supported subset is an existing native 8.1.6 UnlimTracking record with one orientation list and "
                "one subarray. Sources with an explicit 3D scene, module layout, load, storage, P50/P90 or saved "
                "horizon are blocked rather than silently accepted. Geometry and electrical-effect settings are "
                "not edited and no 2D shading is computed here; native near-shading losses may still change with "
                "these controls.")
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
                    "The new variant may contain a partial artifact; inspect the disclosed path before retrying. "
                    "Original template is retained.")
    return result

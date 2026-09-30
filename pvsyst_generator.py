"""Standalone backup generator configuration from GUI-exported variant fields."""
from __future__ import annotations

import math
import re

from pvsyst_component_editor import ASSIGNMENT, replace_value

THRESHOLDS = ("VBkUpEncl_syst", "VBkUpDecl_syst")
GENERATOR_MASK = 0x20
SUPPORTED_VERSIONS = ("8.1.0", "8.1.6")


def direct_fields(lines, bounds):
    """Read only fields owned by a system/subarray, skipping nested objects."""
    fields, stack = {}, []
    for i in range(bounds[0] + 1, bounds[1]):
        line = lines[i].strip()
        match = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))
        if match and (match[2].startswith("PVObject_") and re.fullmatch(r"pv\w+", match[4])
                      or re.fullmatch(r"T[A-Z]\w*", match[4])):
            stack.append(match[4])
        elif line.startswith("End of PVObject ") or line.startswith("End of T"):
            kind = line.removeprefix("End of PVObject ").removeprefix("End of ")
            if not stack or stack.pop() != kind:
                raise ValueError("Unmatched nested object in generator configuration scope")
        elif line == "EndTags":
            if not stack:
                raise ValueError("Unmatched EndTags in generator configuration scope")
            stack.pop()
        elif match and not stack:
            fields.setdefault(match[2], []).append((i, match[4]))
    if stack:
        raise ValueError("Unclosed nested object in generator configuration scope")
    return fields


def one(fields, name, required=True):
    entries = fields.get(name, [])
    if len(entries) != 1:
        if not entries and not required:
            return None
        raise ValueError(f"{name} must occur exactly once in its owning object")
    return entries[0]


def finite(value, label, minimum=0, maximum=math.inf, positive=False):
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{label} must be a finite number") from None
    if not math.isfinite(number) or not minimum <= number <= maximum or positive and number <= 0:
        raise ValueError(f"{label} is outside the supported range")
    return number


def _read_number(fields, name):
    raw = one(fields, name, False)
    if raw is None:
        return None
    try:
        value = float(raw[1])
    except ValueError:
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def state(lines, arrays, system):
    result = {"supported": False, "errors": [], "enabled": None, "filename": None,
              "operating_power_kw": None, "flags": None, "subarrays": [],
              "threshold_units": "native normalized voltage thresholds (0..1); not SOC",
              "scope": "8.1.0/8.1.6 standalone Battery system; runtime dispatch requires simulation"}
    try:
        if system is None:
            raise ValueError("No pvSystem object")
        first_child = next((i for i, line in enumerate(lines[1:], 1)
                            if re.match(r"\s*PVObject_.*=pv\w+", line)), len(lines) - 1)
        root = direct_fields(lines, (0, first_child))
        if one(root, "Version")[1] not in SUPPORTED_VERSIONS:
            raise ValueError("Generator configuration supports variant Version=8.1.0 or 8.1.6")
        fields = direct_fields(lines, system)
        if one(fields, "SystemType")[1] != "Battery":
            raise ValueError("Generator configuration requires SystemType=Battery")
        flags = one(fields, "Flags")[1]
        if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", flags):
            raise ValueError("System Flags must be hexadecimal, at most 32 bits")
        filename = one(fields, "GensetFile", False)
        result.update(flags=flags, enabled=bool(int(flags[1:], 16) & GENERATOR_MASK),
                      filename=filename[1] if filename else None,
                      operating_power_kw=_read_number(fields, "PEffBackUp"))
        for identifier, bounds in sorted(arrays.items()):
            f = direct_fields(lines, bounds)
            result["subarrays"].append({"id": identifier,
                **{name: _read_number(f, name) for name in THRESHOLDS}})
        result["supported"] = True
    except ValueError as exc:
        result["errors"].append(str(exc))
    return result


def replace_generator(lines, arrays, system, updates, components):
    allowed = {"enabled", "filename", "operating_power_kw", "thresholds"}
    if not isinstance(updates, dict) or not updates or set(updates) - allowed:
        raise ValueError("Generator updates: enabled, filename, operating_power_kw, thresholds")
    before = state(lines, arrays, system)
    if not before["supported"]:
        raise ValueError("; ".join(before["errors"]))
    if "enabled" in updates and not isinstance(updates["enabled"], bool):
        raise ValueError("enabled must be a boolean")
    enabled = updates.get("enabled", before["enabled"])
    filename = updates.get("filename", before["filename"])
    power = updates.get("operating_power_kw", before["operating_power_kw"])
    if "operating_power_kw" in updates or enabled:
        power = finite(power, "operating_power_kw", positive=True)
    warnings, dependency = [], None
    if "filename" in updates or enabled:
        # The normal component resolver enforces workspace filenames and format.
        path = components._path("GEN", filename)
        library = "workspace"
        if not path.is_file() and components.builtin:
            path = components._path("GEN", filename, "builtin")
            library = "builtin"
        info = components._inspect("GEN", components._read(path))
        components._require_valid(info, complete=True)
        nominal = float(info["fields"]["PNomGen"][0])
        dependency = {"filename": filename, "library": library, "sha256": info["sha256"],
                      "nominal_power_kw": nominal}
        if power is not None and power > nominal:
            warnings.append("Operating power exceeds GEN nominal power; review system sizing")
    thresholds = updates.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise ValueError("thresholds must map subarray IDs to threshold dictionaries")
    selected = {}
    for key, values in thresholds.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[1-9][0-9]*", key)
                or int(key) not in arrays):
            raise ValueError("Each thresholds key must be an existing positive subarray ID")
        if not isinstance(values, dict) or not values or set(values) - set(THRESHOLDS):
            raise ValueError("Threshold fields are VBkUpEncl_syst and VBkUpDecl_syst")
        selected[int(key)] = {name: finite(value, name, maximum=1) for name, value in values.items()}
    for array in before["subarrays"]:
        merged = array | selected.get(array["id"], {})
        if enabled:
            for name in THRESHOLDS:
                finite(merged[name], f"subarray {array['id']} {name}", maximum=1)
    # Collect insertions separately and apply backwards, preserving old offsets.
    insertions = []
    def assign(bounds, values):
        fields = direct_fields(lines, bounds)
        ending = "\r\n" if lines[bounds[0]].endswith("\r\n") else "\n"
        indent = re.match(r"[ \t]*", lines[bounds[0]])[0] + "  "
        added = []
        for name, value in values.items():
            old = one(fields, name, False)
            rendered = value if isinstance(value, str) else format(value, ".17g")
            if old:
                if (isinstance(value, str) and old[1] == value
                        or not isinstance(value, str) and float(old[1]) == value):
                    continue
                lines[old[0]] = replace_value(lines[old[0]], rendered)
            else:
                added.append(f"{indent}{name}={rendered}{ending}")
        if added:
            insertions.append((bounds[1], added))
    system_values = {}
    if "enabled" in updates:
        raw = before["flags"]
        digits = raw[1:].rjust(2, "0") if enabled else raw[1:]
        if len(digits) >= 2:
            old = digits[-2]
            digit = (int(old, 16) | 2) if enabled else (int(old, 16) & ~2)
            digits = digits[:-2] + format(digit, "x" if old.islower() else "X") + digits[-1]
        system_values["Flags"] = "$" + digits
    if "filename" in updates:
        system_values["GensetFile"] = filename
    if "operating_power_kw" in updates:
        system_values["PEffBackUp"] = power
    assign(system, system_values)
    for identifier, values in selected.items():
        assign(arrays[identifier], values)
    for position, added in sorted(insertions, reverse=True):
        lines[position:position] = added
    return {"before": before, "warnings": warnings, "component": dependency}

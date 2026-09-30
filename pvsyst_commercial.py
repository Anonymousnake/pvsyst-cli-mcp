"""Commercial form editing based on exported text, without model reconstruction."""
from __future__ import annotations

from datetime import datetime
import math
import re

from pvsyst_component_editor import ASSIGNMENT, objects, replace_value


FORM = {
    "Manufacturer": {"type": "text", "required": True},
    "Model": {"type": "text", "required": True},
    "DataSource": {"type": "text", "required": False},
    "YearBeg": {"type": "integer", "minimum": 0, "maximum": 9999},
    "Width": {"type": "number", "minimum": 0, "unit": "m"},
    "Height": {"type": "number", "minimum": 0, "unit": "m"},
    "Depth": {"type": "number", "minimum": 0, "unit": "m"},
    "Weight": {"type": "number", "minimum": 0, "unit": "kg"},
    "NPieces": {"type": "integer", "minimum": 1, "maximum": 2147483647},
    "PriceDate": {"type": "date", "format": "dd/mm/yy HH:MM", "required": False},
}
PATH = "PVObject_Commercial"
MAX_TEXT = 2048
MAX_REMARKS = 5


def _blocks(text):
    blocks = objects(text)
    roots = [b for b in blocks if not b.path]
    commercial = [b for b in blocks if b.path == PATH]
    if len(roots) != 1 or len(commercial) != 1 or commercial[0].object_type != "pvCommercial":
        raise ValueError("Expected one root object and one PVObject_Commercial=pvCommercial")
    # New form insertions/array serialization have been exercised on this export version.
    if roots[0].one("Version")[1] != "8.1.6":
        raise ValueError("Commercial form editing supports text Version=8.1.6")
    if any(b.path.startswith(PATH + "/") for b in blocks):
        raise ValueError("Nested commercial objects require a separate schema")
    return roots[0], commercial[0]


def _remarks(lines, block):
    """Return the exact direct-child array span; reject ambiguous or damaged arrays."""
    starts, ends = [], []
    for i in range(block.line, block.end_line - 1):
        if re.fullmatch(r"\s*Remarks\s*,\s*Count\s*=\s*\d+\s*", lines[i].rstrip("\r\n")):
            starts.append(i)
        elif re.match(r"\s*Remarks\b", lines[i]):
            raise ValueError("Malformed Remarks count header")
        if re.match(r"\s*End of Remarks\s*=", lines[i]):
            ends.append(i)
    if not starts and not ends:
        if any(name.startswith("Str_") for name in block.fields):
            raise ValueError("Orphan or unsupported string-array rows in commercial object")
        return None, []
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError("Expected one complete Remarks array")
    start, end = starts[0], ends[0]
    count = int(lines[start].split("=")[-1].strip())
    if count > 256 or end - start - 1 != count:
        raise ValueError("Remarks rows do not match Count (inspection limit 256)")
    values = []
    for n, line in enumerate(lines[start + 1:end], 1):
        match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if not match or match[2] != f"Str_{n}":
            raise ValueError("Remarks rows must be numbered consecutively")
        values.append(match[4])
    footer = re.fullmatch(r"\s*End of Remarks\s*=(.*)", lines[end].rstrip("\r\n"))
    if footer[1].strip() != (values[-1].strip() if values else ""):
        raise ValueError("Remarks closing value does not match the final row")
    if any(not start < i < end for key, entries in block.fields.items()
           if key.startswith("Str_") for i, _ in entries):
        raise ValueError("Orphan or unsupported string-array rows outside Remarks")
    return (start, end), values


def _value(key, value):
    if not isinstance(value, str) or len(value) > MAX_TEXT or any(not c.isprintable() for c in value):
        raise ValueError(f"{key} must be single-line text of at most {MAX_TEXT} characters without controls")
    value = value.strip()
    spec = FORM[key]
    if spec["type"] == "text":
        if spec["required"] and not value:
            raise ValueError(f"{key} cannot be empty")
    elif spec["type"] == "date":
        if value:
            if not re.fullmatch(r"\d{2}/\d{2}/\d{2} \d{2}:\d{2}", value):
                raise ValueError("PriceDate must use dd/mm/yy HH:MM")
            try:
                datetime.strptime(value, "%d/%m/%y %H:%M")
            except ValueError:
                raise ValueError("PriceDate must be a valid calendar date/time") from None
    else:
        try:
            number = float(value)
        except ValueError:
            raise ValueError(f"{key} must be a finite number") from None
        if not math.isfinite(number) or number < spec["minimum"] or number > spec.get("maximum", math.inf):
            raise ValueError(f"{key} is outside the supported range")
        if spec["type"] == "integer" and not re.fullmatch(r"\d+", value):
            raise ValueError(f"{key} must be a decimal integer")
    return value


def commercial_inventory(kind, text):
    result = {"path": PATH, "editable": False, "errors": [], "fields": {},
              "remarks": [], "remarks_count": 0, "remarks_truncated": False,
              "max_remarks": MAX_REMARKS, "requires_expected_sha256": True,
              "scope": "Commercial form; prices by seller and model-specific options are separate"}
    try:
        root, block = _blocks(text)
        for key, spec in FORM.items():
            values = block.fields.get(key, [])
            if len(values) > 1:
                raise ValueError(f"Duplicate commercial field: {key}")
            raw = values[0][1] if values else None
            result["fields"][key] = {**spec, "present": bool(values),
                "editable": True, "can_add": True,
                "value": raw[:MAX_TEXT] if raw is not None else None,
                "value_truncated": raw is not None and len(raw) > MAX_TEXT,
                "line": values[0][0] + 1 if values else None}
            if kind == "PAN" and key in ("Width", "Height"):
                result["fields"][key]["exclusive_minimum"] = 0
                paired = "LargApp" if key == "Width" else "LongApp"
                result["fields"][key].update(paired_root_field=paired,
                    paired_root_present=bool(root.fields.get(paired)),
                    editable=len(root.fields.get(paired, [])) <= 1)
        _, remarks = _remarks(text.splitlines(keepends=True), block)
        result.update(remarks=[v[:MAX_TEXT] for v in remarks[:MAX_REMARKS]],
                      remarks_count=len(remarks), remarks_truncated=(len(remarks) > MAX_REMARKS
                          or any(len(v) > MAX_TEXT for v in remarks[:MAX_REMARKS])), editable=True)
        if kind == "PAN":
            result["dimension_pairs"] = {"Width": "LargApp", "Height": "LongApp"}
            result["remarks_note"] = "The fifth PAN remark may encode a model association or functional option"
    except ValueError as exc:
        result["errors"].append(str(exc))
    return result


def replace_commercial(kind, text, updates, remarks=None):
    if not isinstance(updates, dict) or any(k not in FORM for k in updates):
        raise ValueError("Commercial fields: " + ", ".join(FORM))
    if remarks is not None and (not isinstance(remarks, list) or len(remarks) > MAX_REMARKS):
        raise ValueError(f"Supply at most {MAX_REMARKS} remark strings; [] clears remarks")
    values = {key: _value(key, value) for key, value in updates.items()}
    if remarks is not None:
        remarks = [_value("DataSource", value) for value in remarks]
    root, block = _blocks(text)
    lines = text.splitlines(keepends=True)
    span, previous_remarks = _remarks(lines, block)
    if any(len(block.fields.get(k, [])) > 1 for k in FORM):
        raise ValueError("Ambiguous duplicate commercial fields")
    # Inserting immediately before the block close preserves every existing line.
    ending = "\r\n" if lines[block.line - 1].endswith("\r\n") else "\n"
    indent = re.match(r"[ \t]*", lines[block.line - 1])[0] + "  "
    additions = []
    for key, value in values.items():
        existing = block.fields.get(key, [])
        if existing:
            index, old = existing[0]
            if old != value:
                lines[index] = replace_value(lines[index], value)
        else:
            additions.append(f"{indent}{key}={value}{ending}")
        if kind == "PAN" and key in ("Width", "Height"):
            target = "LargApp" if key == "Width" else "LongApp"
            if float(value) <= 0:
                raise ValueError("PAN Width and Height must be positive")
            # GUI 8.1.6 writes commercial dimensions without these legacy root
            # fields. Preserve that representation; synchronize only if present.
            if target in root.fields:
                index, old = root.one(target)
                if float(old) != float(value):
                    lines[index] = replace_value(lines[index], value)
    # Keep the display synopsis consistent without replacing its source/status text.
    if values.keys() & {"Manufacturer", "Model"} and "Comment" in root.fields:
        index, comment = root.one("Comment")
        parts = comment.split("\t")
        if len(parts) >= 2:
            parts[0] = values.get("Manufacturer", parts[0])
            parts[1] = values.get("Model", parts[1])
            lines[index] = replace_value(lines[index], "\t".join(parts))
    # Apply edits from the end so original line indices remain valid.
    if additions:
        lines[block.end_line - 1:block.end_line - 1] = additions
    if remarks is not None and remarks != previous_remarks:
        replacement = []
        if remarks:
            replacement = [f"{indent}Remarks, Count={len(remarks)}{ending}",
                *(f"{indent}  Str_{i}={v}{ending}" for i, v in enumerate(remarks, 1)),
                f"{indent}End of Remarks={remarks[-1]}{ending}"]
        if span:
            lines[span[0]:span[1] + 1] = replacement
        else:
            lines[block.end_line - 1:block.end_line - 1] = replacement
    return "".join(lines)

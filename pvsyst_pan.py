"""Scoped PAN field spellings observed in text templates and GUI exports."""
from __future__ import annotations

from pvsyst_component_editor import objects


ALIASES = {"ISC": ("ISC", "Isc"), "MuISC": ("MuISC", "muISC")}
CANONICAL = {alias: name for name, aliases in ALIASES.items() for alias in aliases}
DIMENSIONS = {"Width": "LargApp", "Height": "LongApp"}


def pan_blocks(text):
    blocks = objects(text)
    roots = [block for block in blocks if not block.path]
    commercial = [block for block in blocks if block.path == "PVObject_Commercial"]
    if len(roots) != 1 or roots[0].object_type != "pvModule":
        raise ValueError("Expected one root pvModule object")
    if len(commercial) != 1 or commercial[0].object_type != "pvCommercial":
        raise ValueError("Expected one direct PVObject_Commercial=pvCommercial")
    return roots[0], commercial[0]


def validation_fields(text):
    """Validate physical fields in the root and identity/dimensions in their form.

    Merge equivalent spellings into one list so duplicates are still rejected;
    never change the inspection's raw fields or the file's serialized keys.
    """
    root, commercial = pan_blocks(text)
    fields = {}
    for key, values in root.fields.items():
        fields.setdefault(CANONICAL.get(key, key), []).extend(value for _, value in values)
    for key in ("Manufacturer", "Model", "Width", "Height"):
        # Root Width/Height or identity fields do not replace the commercial form.
        fields.pop(key, None)
        if key in commercial.fields:
            fields[key] = [value for _, value in commercial.fields[key]]
    return fields

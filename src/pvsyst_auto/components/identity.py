"""Read declared component identity without model fitting."""
from __future__ import annotations

from pvsyst_auto.components.editor import objects
from pvsyst_auto.components.store import ComponentStore, electrical_inventory


def sourced_component(store: ComponentStore, kind: str, filename: str) -> tuple[str, str, dict | None]:
    """Read one loose UTF-8 component by exact filename and bind its declared identity.

    Reuses the store's own path/format checks and the object parser: an explicit filename is
    never replaced by a manufacturer search, nothing is hashed and no model is invented. The
    root object is required and a PVObject_Commercial block is optional; when declared, the
    Commercial identity wins over the root, while a disagreement between the two blocks the
    flow. Returns (field, message, payload); payload is None when the component blocks.
    """
    path = None
    for library in ("workspace", "builtin"):
        if library == "builtin" and store.builtin is None:
            continue  # No installed library to search: report the missing file below.
        try:
            candidate = store._path(kind, filename, library)
        except ValueError as exc:
            return f"{kind}.name", str(exc), None
        if candidate.is_file():
            path = candidate
            break
    if path is None:
        return (f"{kind}.name",
                f"{filename} is not a loose {kind} file in the workspace or the installed library", None)
    try:
        text = store._text(kind, store._read(path))[1]
    except (ValueError, OSError) as exc:
        return f"{kind}.name", str(exc), None
    if text is None:
        return (f"{kind}.name",
                "Legacy binary PAN carries no readable declared identity; use a UTF-8 text component", None)
    try:
        blocks = objects(text)
    except ValueError as exc:
        return f"{kind}.name", str(exc), None
    roots = [block for block in blocks if not block.path]
    commercial = [block for block in blocks if block.path == "PVObject_Commercial"]
    if len(roots) != 1 or len(commercial) > 1:
        return (f"{kind}.name",
                "Expected exactly one root object and at most one PVObject_Commercial block", None)
    owners = [("Commercial", commercial[0].fields)] if commercial else []
    owners.append(("root", roots[0].fields))
    identity = {}
    for name, key in (("Manufacturer", "manufacturer"), ("Model", "model")):
        declared = {}
        for label, fields in owners:
            values = [value.strip() for _, value in fields.get(name, []) if value.strip()]
            # Occurrence count, not a de-duplicated set: a repeated identical field is ambiguous.
            if len(values) > 1:
                return (f"{kind}.{key}", f"{name} occurs {len(values)} times in its {label} object; "
                                         "exactly one occurrence is bound", None)
            if values:
                declared[label] = values[0]
        if len({*declared.values()}) > 1:
            return (f"{kind}.{key}",
                    f"{name} is declared differently in the root object and the Commercial block", None)
        if not declared:
            return f"{kind}.{key}", f"{name} is missing in its owning object", None
        identity[key] = next(iter(declared.values()))
    return "", "", {"manufacturer": identity["manufacturer"], "model": identity["model"],
                    "electrical": electrical_inventory(kind, text)}

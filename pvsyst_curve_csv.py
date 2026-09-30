"""Explicit-unit CSV transport for the existing guarded curve editor."""
from __future__ import annotations

import csv
import io
import re

from pvsyst_component_editor import MAX_POINTS, checked_points


HEADERS = {
    ("OND", "Converter/ProfilPIO"): ("input_watts", "output_watts"),
    ("BTR", "CapaCourant"): ("discharge_hours", "relative_capacity"),
    ("BTR", "Capa_DischRate"): ("discharge_hours", "relative_capacity"),
}
MAX_CSV_BYTES = 65536
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def import_curve_csv(kind: str, supplied: dict[str, str]) -> dict[str, list[list[float]]]:
    """Parse one full table; never infer units, separators, order or missing data."""
    if not isinstance(supplied, dict) or len(supplied) != 1:
        raise ValueError("curve_csv must contain exactly one curve path and CSV text")
    path, text = next(iter(supplied.items()))
    header = HEADERS.get((kind, path))
    if header is None:
        raise ValueError("CSV supports OND Converter/ProfilPIO and BTR CapaCourant/Capa_DischRate only")
    if not isinstance(text, str) or len(text) > MAX_CSV_BYTES or len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise ValueError(f"Curve CSV must be text of at most {MAX_CSV_BYTES} UTF-8 bytes")
    rows = csv.reader(io.StringIO(text.removeprefix("\ufeff"), newline=""), strict=True)
    points = []
    try:
        if next(rows, None) != list(header):
            raise ValueError("Curve CSV requires the header: " + ",".join(header))
        for row in rows:
            if len(points) >= MAX_POINTS:
                raise ValueError(f"Curve CSV supports at most {MAX_POINTS} point rows")
            if len(row) != 2 or any(not NUMBER.fullmatch(value.strip(" \t")) for value in row):
                raise ValueError(f"Curve CSV row {rows.line_num} requires two decimal numbers; blanks and formulas are unsupported")
            points.append([float(value) for value in row])
    except csv.Error as exc:
        raise ValueError(f"Invalid curve CSV: {exc}") from None
    return {path: checked_points(points)}


def export_curve_csv(kind: str, inventory: dict) -> dict:
    """Export only complete profiles with known axis semantics, including read-only ones."""
    exported, errors = {}, {}
    if inventory.get("errors"):
        errors["<component>"] = inventory["errors"]
    for item in inventory["items"]:
        path = item["path"]
        header = HEADERS.get((kind, path))
        if header is None:
            errors[path] = ["CSV axis semantics are unsupported for this curve"]
        elif not item["structure_complete"]:
            errors[path] = item["errors"]
        else:
            stream = io.StringIO(newline="")
            writer = csv.writer(stream, lineterminator="\r\n")
            writer.writerow(header)
            writer.writerows([[format(value, ".17g") for value in row] for row in item["points"]])
            exported[path] = stream.getvalue()
    return {"curve_csv": exported, "curve_csv_errors": errors}

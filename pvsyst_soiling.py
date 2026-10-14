"""Native 8.1.6 Soiling tag-block editing; no irradiance or yield calculation."""
from __future__ import annotations

import math
from decimal import Decimal

from pvsyst_component_editor import ASSIGNMENT


def _profile(lines, bounds):
    start, end = bounds
    end = next((i for i in range(start + 1, end) if lines[i].strip().startswith("PVObject_")), end)
    header = range(start + 1, end)
    assignments = {i: ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n")) for i in header}
    if any(m is not None and m[2] in ("SoilingLoss", "SoilingLossM") for m in assignments.values()):
        raise ValueError("Legacy soiling representation; resave with the native 8.1.6 GUI first")
    begins = [i for i, m in assignments.items() if m is not None and m[2] == "Soiling"]
    total = sum(m is not None and m[2] == "Soiling" for line in lines
                for m in [ASSIGNMENT.fullmatch(line.rstrip("\r\n"))])
    if total != len(begins):
        raise ValueError("Non-owning Soiling blocks are not supported")
    if len(begins) > 1:
        raise ValueError("Require at most one owning Soiling block")
    if not begins:
        if any(lines[i].strip() == "EndTags" for i in header):
            raise ValueError("Unverified main-array tag block")
        return {"mode": "none"}, (end, end), "0.000"
    begin, = begins
    indent = lines[start][:len(lines[start]) - len(lines[start].lstrip())]
    if lines[begin].rstrip("\r\n") != indent + "Soiling=Soiling":
        raise ValueError("Require an owning native Soiling=Soiling block")
    finish = next((i for i in range(begin + 1, end) if lines[i].strip() == "EndTags"), None)
    if finish is None or any(lines[i].strip() == "EndTags" for i in header if not begin <= i <= finish):
        raise ValueError("Unclosed or unverified main-array tag block")
    values = {}
    for line in lines[begin + 1:finish]:
        match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if (match is None or match[2] not in ("IsYearly", "IsMonthly", "IsFromFile", "YearlyValue", "MonthlyValue")
                or match[2] in values):
            raise ValueError("Unknown or duplicate native Soiling field")
        values[match[2]] = match[4]
    if any(values.get(key) not in ("True", "False") for key in ("IsYearly", "IsMonthly", "IsFromFile")):
        raise ValueError("Require explicit native Soiling mode booleans")
    if values["IsFromFile"] != "False":
        raise ValueError("File-based soiling is not verified by this editor")
    if values["IsYearly"] == values["IsMonthly"] == "True":
        raise ValueError("Native Soiling modes are mutually exclusive")
    try:
        yearly = float(values["YearlyValue"])
        raw = values.get("MonthlyValue", "")
        monthly = [float(x) for x in raw.removesuffix(",").split(",")] if raw else None
    except (ValueError, KeyError):
        raise ValueError("Invalid native Soiling values") from None
    if (not math.isfinite(yearly) or not 0 <= yearly <= 1 or monthly is not None and
            (len(monthly) != 12 or any(not math.isfinite(x) or not 0 <= x <= 1 for x in monthly))):
        raise ValueError("Native Soiling values must be finite fractions in 0..1")
    mode = "constant" if values["IsYearly"] == "True" else "monthly" if values["IsMonthly"] == "True" else "none"
    if mode == "monthly" and monthly is None:
        raise ValueError("Monthly soiling requires twelve native values")
    state = {"mode": mode}
    if mode == "constant":
        state["yearly_loss_pct"] = round(yearly * 100, 10)
    if mode == "monthly":
        state["monthly_loss_pct"] = [round(x * 100, 10) for x in monthly]
    return state, (begin, finish + 1), values["YearlyValue"]


def soiling_state(lines, bounds):
    try:
        return _profile(lines, bounds)[0]
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}


def replace_soiling(lines, bounds, updates):
    if not isinstance(updates, dict) or updates.get("mode") not in ("none", "constant", "monthly"):
        raise ValueError("Require soiling mode none, constant or monthly")
    mode = updates["mode"]
    keys = {"mode"} | ({"yearly_loss_pct"} if mode == "constant" else {"monthly_loss_pct"} if mode == "monthly" else set())
    if set(updates) != keys:
        raise ValueError("Provide only the values required by the chosen soiling mode")
    supplied = [updates["yearly_loss_pct"]] if mode == "constant" else updates.get("monthly_loss_pct", [])
    if not isinstance(supplied, list) or mode == "monthly" and len(supplied) != 12:
        raise ValueError("Monthly soiling requires exactly twelve percentage values")
    for value in supplied:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100 or not math.isfinite(value):
            raise ValueError("Soiling percentages must be finite numbers in 0..100")
        scaled = Decimal(str(value)) * 10
        if scaled != scaled.to_integral_value():
            raise ValueError("Use 0.1 percentage-point increments, matching native GUI save precision")
    before, (begin, end), yearly_raw = _profile(lines, bounds)
    indent = lines[bounds[0]][:len(lines[bounds[0]]) - len(lines[bounds[0]].lstrip())]
    newline = "\r\n" if lines[bounds[0]].endswith("\r\n") else "\n"
    block = []
    if mode != "none":
        fraction = lambda value: f"{Decimal(str(value)) / 100:.3f}"
        body = ["Soiling=Soiling", f"  IsYearly={mode == 'constant'}", f"  IsMonthly={mode == 'monthly'}",
                "  IsFromFile=False", "  YearlyValue=" + (fraction(supplied[0]) if mode == "constant" else yearly_raw)]
        if mode == "monthly":
            body.append("  MonthlyValue=" + ",".join(map(fraction, supplied)) + ",")
        block = [indent + text + newline for text in (*body, "EndTags")]
    lines[begin:end] = block
    after_bounds = (bounds[0], bounds[1] + len(block) - (end - begin))
    return {"before": before, "after": _profile(lines, after_bounds)[0],
            "scope": "Whole PV main array; native 8.1.6 constant/monthly soiling, not a loss model"}

"""Bounded native PF and grid-limit serialization, reusing the standalone editors.

Reuses the two existing editors through restricted parse views over the real file, then
re-serialises the single owning system once. It performs no cosine, kVA or power
calculation, and never edits root or any other object.
"""
from __future__ import annotations

import re

from pvsyst_component_editor import ASSIGNMENT, replace_value
from pvsyst_grid_limit_native import grid_limit_state, replace_grid_limit
from pvsyst_power_factor import power_factor_state, replace_power_factor

COMMON = ("Comment", "Flags", "SystemType")
PF_OWN = ("TanPhi", "TanPhiM")
GRID_OWN = ("PLimGrid", "LimGrSeparLoss")
ORDER = (*PF_OWN, *GRID_OWN)
PF_BITS = 0x00100000 | 0x00200000 | 0x00400000 | 0x00800000
ENABLED, APPARENT, INJECTION = 0x01000000, 0x02000000, 0x04000000
PF_MODE_BITS = {"none": 0x00000000, "constant": 0x00100000, "monthly": 0x00300000}
PNOM_BITS = {"native": 0x00000000, "active": 0x00800000, "apparent": 0x00400000}
LEGAL = frozenset((
    0x00000000, 0x00100000, 0x00300000, 0x00500000, 0x00900000, 0x04000000, 0x05000000,
    0x06000000, 0x06100000, 0x05100000, 0x07100000, 0x05300000, 0x07300000, 0x01100000,
    0x03100000, 0x05900000, 0x07900000, 0x05500000, 0x07500000,
    # Recorded monthly PNom families: no grid limit, or active injection with separate loss.
    0x00B00000, 0x00700000, 0x05B00000, 0x05700000))
SCOPE = ("Owning native pvSystem only; combined PF and injection/grid-limit tags, "
         "no cosine, kVA or power calculation")


def _owning(lines, system):
    """Strict first pass on the real file; only Flags/SystemType are mandatory."""
    if not isinstance(system, tuple) or len(system) != 2:
        raise ValueError("Require one owning native pvSystem")
    if any(line.strip().startswith("PVObject_") or line.strip() == "EndTags"
           for line in lines[system[0] + 1:system[1]]):
        raise ValueError("Require a flat native grid system")
    if any("pvLoad" in line or "pvGridStorage" in line for line in lines):
        raise ValueError("Load or storage coupling is outside this scope")
    if any(line.strip() == "P50_P90=TP50_P90" for line in lines):
        raise ValueError("P50 parameter coupling is outside this scope")
    fields = {}
    for i in range(system[0] + 1, system[1]):
        match = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))
        if match is None or match[2] in fields:
            raise ValueError("Unknown or duplicate line inside the owning system")
        fields[match[2]] = (i, match[4])
    if set(fields) - set(COMMON) - set(ORDER):
        raise ValueError("Unknown owning system fields require separate review")
    for key in ("Flags", "SystemType"):
        if key not in fields:
            raise ValueError(f"Require one owning {key}")
    if fields["SystemType"][1] != "Grid":
        raise ValueError("Require SystemType=Grid")
    for key in ORDER:
        total = sum(m is not None and m[2] == key for line in lines
                    for m in [ASSIGNMENT.fullmatch(line.rstrip("\r\n"))])
        if total != (1 if key in fields else 0):
            raise ValueError(f"Require {key} only in the owning system")
    raw = fields["Flags"][1]
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw):
        raise ValueError("Require hexadecimal owning Flags")
    flags = int(raw[1:], 16)
    if flags not in LEGAL:
        raise ValueError("Unverified native flag family for a combined control")
    if not flags & ENABLED and any(key in fields for key in GRID_OWN):
        raise ValueError("A disabled grid limit must not carry PLimGrid or LimGrSeparLoss")
    return fields, flags


def _view(lines, system, keep, own):
    """Whole-file copy; inside the owning body drop counterpart fields and mask Flags."""
    view, delta = list(lines), 0
    for i in range(system[0] + 1, system[1]):
        key = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))[2]
        if key == "Flags":
            raw = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))[4]
            masked = int(raw[1:], 16) & keep
            view[i] = replace_value(lines[i], "$%08X" % masked if masked else "$00")
        elif key not in COMMON and key not in own:
            view[i] = None
            delta -= 1
    return [line for line in view if line is not None], (system[0], system[1] + delta)


def _own_lines(view):
    """Native rows produced by the reused editors, never re-implemented here."""
    own = {}
    for line in view:
        match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if match and match[2] in ORDER:
            own[match[2]] = match[4]
    return own


def _grid_shape(state, flags):
    """Normalise the legacy grid state into this module's shape (apparent from bit25)."""
    if state.get("mode") is None:
        return state
    return {"mode": state["mode"], "limit": state["limit_kw"],
            "quantity": "apparent" if flags & APPARENT else "active",
            "point": state["point"], "separate_loss": state["separate_loss"]}


def _parts(lines, system):
    """Both views from the untouched file, so `before` is never a post-edit read."""
    fields, flags = _owning(lines, system)
    pf_view, pf_bounds = _view(lines, system, PF_BITS, PF_OWN)
    grid_view, grid_bounds = _view(lines, system, ENABLED | INJECTION, GRID_OWN)
    power_factor = power_factor_state(pf_view, pf_bounds)
    grid_limit = _grid_shape(grid_limit_state(grid_view, grid_bounds), flags)
    if power_factor.get("mode") is None or grid_limit.get("mode") is None:
        raise ValueError(power_factor.get("issue") or grid_limit.get("issue"))
    if grid_limit["mode"] == "constant" and type(grid_limit["separate_loss"]) is not bool:
        raise ValueError("An enabled limit requires explicit native LimGrSeparLoss")
    _coupling(power_factor, grid_limit)
    return power_factor, grid_limit, pf_view, pf_bounds, grid_view, grid_bounds


def power_controls_state(lines, system):
    """Semantic state of both controls; unusable input reports mode None plus an issue."""
    if not isinstance(system, tuple) or len(system) != 2:
        return {"mode": None, "issue": "Require one owning native pvSystem"}
    try:
        power_factor, grid_limit, *_ = _parts(lines, system)
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}
    if power_factor.get("mode") is None or grid_limit.get("mode") is None:
        return {"mode": None, "issue": power_factor.get("issue") or grid_limit.get("issue")}
    return {"mode": "configured", "power_factor": power_factor, "grid_limit": grid_limit}


def _grid_updates(value):
    if not isinstance(value, dict) or value.get("mode") not in ("none", "constant"):
        raise ValueError("Require grid limit mode none or constant")
    keys = ({"mode", "limit", "quantity", "point", "separate_loss"} if value["mode"] == "constant"
            else {"mode", "quantity", "point"})
    if set(value) != keys:
        raise ValueError("Constant needs limit/quantity/point/separate_loss; none needs quantity/point")
    if value["quantity"] not in ("active", "apparent") or value["point"] not in ("injection", "inverter"):
        raise ValueError("Require quantity active or apparent and point injection or inverter")
    if value["mode"] == "constant" and type(value["separate_loss"]) is not bool:
        raise ValueError("separate_loss must be a strict boolean")
    return value


def _coupling(power_factor, grid):
    """Only the couplings already covered by the verified native flag families."""
    mode, pnom = power_factor["mode"], power_factor["pnom_limit"]
    if grid["mode"] != "constant":
        return
    strict = mode == "constant" and pnom == "native"
    separate, point = grid["separate_loss"], grid["point"]
    if not separate and not (strict and point == "injection"):
        raise ValueError("A limit without separate loss is verified only for injection with native constant PF")
    if point == "inverter" and not (strict and separate):
        raise ValueError("An inverter-point limit requires constant PF, native PNom and separate loss")
    if mode == "monthly" and not (point == "injection" and separate):
        raise ValueError("Monthly PF is verified only with an injection-point limit and separate loss")
    if pnom != "native" and not (point == "injection" and separate):
        raise ValueError("A Legacy PNom limit is verified only with injection and separate loss")
    if mode == "none" and not (grid["quantity"] == "active" and point == "injection" and separate):
        raise ValueError("A limit without PF is verified only for active injection with separate loss")


def replace_power_controls(lines, system, updates):
    """Validate everything first, then rewrite the single owning system once, in place."""
    if not isinstance(updates, dict) or set(updates) != {"power_factor", "grid_limit"}:
        raise ValueError("Require exactly power_factor and grid_limit")
    if not isinstance(system, tuple) or len(system) != 2:
        raise ValueError("Require one owning native pvSystem")
    grid = _grid_updates(updates["grid_limit"])
    eol = "\r\n" if lines[system[0]].endswith("\r\n") else "\n"
    power_factor, grid_before, pf_view, pf_bounds, grid_view, grid_bounds = _parts(lines, system)
    if power_factor.get("mode") is None:
        raise ValueError(power_factor.get("issue", "Unsupported power factor source"))
    pf_after = replace_power_factor(pf_view, pf_bounds, updates["power_factor"])["after"]
    replace_grid_limit(grid_view, grid_bounds,
                       {"mode": "constant", "limit_kw": grid["limit"]} if grid["mode"] == "constant"
                       else {"mode": "none"})
    grid_after = {"mode": grid["mode"],
                  "limit": grid["limit"] if grid["mode"] == "constant" else None,
                  "quantity": grid["quantity"], "point": grid["point"],
                  "separate_loss": grid["separate_loss"] if grid["mode"] == "constant" else None}
    _coupling(pf_after, grid_after)
    flags = (PF_MODE_BITS[pf_after["mode"]] | PNOM_BITS[pf_after["pnom_limit"]]
             | (ENABLED if grid["mode"] == "constant" else 0)
             | (APPARENT if grid["quantity"] == "apparent" else 0)
             | (INJECTION if grid["point"] == "injection" else 0))
    if flags not in LEGAL:
        raise ValueError("Combined control does not match a verified native flag family")
    before = {"power_factor": power_factor, "grid_limit": grid_before}
    after = {"power_factor": pf_after, "grid_limit": grid_after}
    if before == after:
        return {"before": before, "after": after, "scope": SCOPE}
    rendered = {key: value for key, value in _own_lines(pf_view).items()
                if (key == "TanPhi" and pf_after["mode"] == "constant")
                or (key == "TanPhiM" and pf_after["mode"] == "monthly")}
    if grid["mode"] == "constant":
        rendered["PLimGrid"] = _own_lines(grid_view)["PLimGrid"]
        rendered["LimGrSeparLoss"] = "True" if grid["separate_loss"] else "False"
    else:
        rendered.pop("PLimGrid", None)
        rendered.pop("LimGrSeparLoss", None)
    body, indent = [], lines[system[0]][:len(lines[system[0]]) - len(lines[system[0]].lstrip())] + "  "
    for i in range(system[0] + 1, system[1]):
        key = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))[2]
        if key in ORDER:
            continue
        body.append(replace_value(lines[i], "$%08X" % flags if flags else "$00") if key == "Flags" else lines[i])
    for key in ORDER:
        if key in rendered:
            body.append(f"{indent}{key}={rendered[key]}{eol}")
    candidate = list(lines)
    candidate[system[0] + 1:system[1]] = body
    if power_controls_state(candidate, (system[0], system[0] + len(body) + 1)) != dict(mode="configured", **after):
        raise ValueError("Candidate combined control state differs from the request")
    lines[:] = candidate
    return {"before": before, "after": after, "scope": SCOPE}

"""Zero-template PVsyst ageing model (pvAgeing) mounting, inspection, and economics curves."""
from __future__ import annotations

from decimal import Decimal
import math
from pathlib import Path
import re
from typing import Any

from pvsyst_auto.components.editor import ASSIGNMENT, Profile, objects, one, replace_value
from pvsyst_auto.validation import NATIVE_TEXT_OR_SEED, SEED_LABEL, finite_number

AGEING_FLAG2_BIT = 0x00100000
YEAR_FIELD, MEAN_FIELD = "NoYearSimul", "ModuleDegradFactor"
KEEP_MC, RMS_ISC, RMS_VOC = "KeepMCValues", "ModulesRMSYearISC", "ModulesRMSYearVoc"
STORED_LOSS, STORED_MISMATCH, IMP_SHARING = "DegradLossFactor", "DegradMismFactor", "ImpDegradSharing"
RMS_ISC_KEY, RMS_VOC_KEY = "modules_rms_year_isc_percent", "modules_rms_year_voc_percent"
IMP_SHARING_KEY = "imp_degrad_sharing_percent"

_HEX_FLAG = re.compile(r"\$[0-9A-Fa-f]{1,8}")
_AGE_PURE_YEARS = (0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0)
_AGE_SPEC_KEYS = frozenset((
    "enabled", "simulation_year", "annual_module_degradation_percent",
    IMP_SHARING_KEY, "keep_mc_values", RMS_ISC_KEY, RMS_VOC_KEY,
))
_AGE_UPDATE_REQ = ("simulation_year", "annual_module_degradation_percent")
_AGE_UPDATE_OPT = ("keep_mc_values", IMP_SHARING_KEY, RMS_ISC_KEY, RMS_VOC_KEY)
_MAIN_ANCHORS = (
    "PVObject_SystemCircuit=", "ACWireNoSect=", "MVNbTransfos=", "UseSpectralCorr=", "SubArrays=Start",
)
_BATCH_YEAR_KEYS = frozenset(("simulation for year", "simulation year", "noyearsimul"))


def format_flags2(value: int) -> str:
    """Render main-array hexadecimal Flags2 keeping native 4-digit ($082A) or 8-digit ($0010082A) width."""
    return f"${value:08X}" if value > 0xFFFF else f"${value:04X}"


def _count_field(lines: list[str], key: str) -> int:
    return sum(
        m is not None and m[2] == key
        for line in lines
        for m in [ASSIGNMENT.fullmatch(line.rstrip("\r\n"))]
    )


def _step_check(value: Any, label: str, *, scale: int, maximum: float, step_err: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a strict number")
    num = finite_number(value, label, minimum=0.0, maximum=maximum)
    scaled = Decimal(str(num)) * scale
    if scaled != scaled.to_integral_value():
        raise ValueError(step_err)
    return float(num)


def _age_year(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 50:
        raise ValueError(
            "simulation_year must be a strict integer"
            if isinstance(value, bool) or not isinstance(value, int)
            else "simulation_year must be in 1..50"
        )
    return value


def _age_pct(value: Any, label: str, maximum: float) -> float:
    return _step_check(value, label, scale=100, maximum=maximum, step_err=f"{label} must use 0.01 increments")


def _age_sharing(value: Any) -> float:
    return _step_check(
        value, IMP_SHARING_KEY, scale=10, maximum=100.0, step_err=f"{IMP_SHARING_KEY} must use 0.1 increments"
    )


def normalize_ageing_spec(raw: Any) -> dict[str, Any]:
    """Validate and normalize an ageing configuration dict for zero-template rendering."""
    if not isinstance(raw, dict):
        raise ValueError("losses.ageing must be an object")
    if extra := sorted(set(raw) - _AGE_SPEC_KEYS):
        raise ValueError(f"Unknown ageing field(s): {', '.join(extra)}")
    if (RMS_ISC_KEY in raw) != (RMS_VOC_KEY in raw):
        raise ValueError(f"{RMS_ISC_KEY} and {RMS_VOC_KEY} must be supplied together")
    enabled = raw.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("ageing.enabled must be a strict boolean")
    keep_mc = raw.get("keep_mc_values", False)
    if type(keep_mc) is not bool:
        raise ValueError("ageing.keep_mc_values must be a strict boolean")
    return {
        "enabled": enabled,
        "simulation_year": _age_year(raw.get("simulation_year", 1)),
        "annual_module_degradation_percent": _age_pct(
            raw.get("annual_module_degradation_percent", 0.4), "annual_module_degradation_percent", 5.0
        ),
        IMP_SHARING_KEY: _age_sharing(raw.get(IMP_SHARING_KEY, 80.0)),
        "keep_mc_values": keep_mc,
        RMS_ISC_KEY: _age_pct(raw.get(RMS_ISC_KEY, 0.0), RMS_ISC_KEY, 100.0),
        RMS_VOC_KEY: _age_pct(raw.get(RMS_VOC_KEY, 0.0), RMS_VOC_KEY, 100.0),
    }


def render_ageing_lines(spec: dict[str, Any], *, indent: str = "  ") -> list[str]:
    """Render native PVsyst 8.1.7 NoYearSimul + DegradLossFactor/Mism + PVObject_Ageing=pvAgeing lines."""
    norm = normalize_ageing_spec(spec)
    year = norm["simulation_year"]
    deg = norm["annual_module_degradation_percent"]
    sharing = norm[IMP_SHARING_KEY]
    rms_isc = norm[RMS_ISC_KEY]
    rms_voc = norm[RMS_VOC_KEY]
    stored_loss = min(100.0, max(0.0, (year - 0.5) * deg))
    keep_str = "True" if norm["keep_mc_values"] else "False"
    points = [f"{indent}    Point_{i}={yr:.1f},{min(100.0, yr * deg):.3f}" for i, yr in enumerate(_AGE_PURE_YEARS, start=1)]
    return [
        f"{indent}{YEAR_FIELD}={year:d}",
        f"{indent}{STORED_LOSS}={stored_loss:.2f}",
        f"{indent}{STORED_MISMATCH}=0.00",
        f"{indent}PVObject_Ageing=pvAgeing",
        f"{indent}  Flags=$00",
        f"{indent}  {KEEP_MC}={keep_str}",
        f"{indent}  PlotKind=1",
        f"{indent}  CurveWarranty=1",
        f"{indent}  ProgressMode=1",
        f"{indent}  NSigmaLimit=2.0",
        f"{indent}  {MEAN_FIELD}={deg:.2f}",
        f"{indent}  {IMP_SHARING}={sharing:.1f}",
        f"{indent}  {RMS_ISC}={rms_isc:.2f}",
        f"{indent}  {RMS_VOC}={rms_voc:.2f}",
        f"{indent}  ModuleYearWarr=0,10,20,25,",
        f"{indent}  ModuleWarranty=98.0000,91.0000,84.0000,80.0000,",
        f"{indent}  AgeingLossPure=TCubicProfile",
        f"{indent}    NPtsMax=7",
        f"{indent}    NPtsEff=7",
        f"{indent}    LastCompile=$0080",
        f"{indent}    Mode=3",
        *points,
        f"{indent}  End of TCubicProfile",
        f"{indent}End of TAgeing",
    ]


def _main_context(lines: list[str], bounds: Any):
    if not isinstance(bounds, tuple) or len(bounds) != 2:
        raise ValueError("Require one owning main-array bounds")
    shift = bounds[0]
    blocks = objects("".join(lines[shift:bounds[1] + 1]))
    roots = [b for b in blocks if not b.path]
    ages = [b for b in blocks if b.object_type == "pvAgeing"]
    if len(roots) != 1 or roots[0].object_type != "pvMainArray":
        raise ValueError("Require one owning pvMainArray root object")
    root = roots[0]
    header_end = next((i for i, line in enumerate(lines[1:], 1) if line.strip().startswith("PVObject_")), shift)
    versions = [
        m[4] for i in range(1, header_end)
        if (m := ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))) is not None and m[2] == "Version"
    ]
    if len(versions) != 1 or versions[0] not in NATIVE_TEXT_OR_SEED:
        raise ValueError(f"Ageing model editing requires root Version={SEED_LABEL}")
    raw_f2 = one(root.fields, "Flags2")[1]
    if not isinstance(raw_f2, str) or not _HEX_FLAG.fullmatch(raw_f2):
        raise ValueError("main Flags2 must be a hexadecimal $ value of 1..8 digits")
    one(root.fields, "Flags")
    f2_val = int(raw_f2[1:], 16)
    return shift, blocks, root, ages, f2_val, bool(f2_val & AGEING_FLAG2_BIT)


def read_ageing(
    lines: list[str], bounds: Any, *, require_enabled: bool = False, require_present: bool = True
) -> tuple[int, Any, Any, dict[str, Any]]:
    """Inspect mounted or absent pvAgeing state inside pvMainArray across 8.1.0/8.1.6/8.1.7 variants."""
    shift, blocks, root, ages, _, enabled = _main_context(lines, bounds)
    if not ages:
        if enabled:
            raise ValueError("Main array Flags2 enables ageing without a PVObject_Ageing block")
        if require_present or require_enabled:
            raise ValueError("PVObject_Ageing block is absent")
        return shift, root, None, {"mode": "absent", "enabled": False}
    if len(ages) != 1 or ages[0].path != "PVObject_Ageing":
        raise ValueError("Require exactly one direct PVObject_Ageing block")
    age = ages[0]
    if require_enabled and not enabled:
        raise ValueError("Ageing model is disabled; enable ageing before updating")
    if "Flags" in age.fields and one(age.fields, "Flags")[1] != "$00":
        raise ValueError("pvAgeing Flags must be $00")
    keep_mc = one(age.fields, KEEP_MC)[1] if KEEP_MC in age.fields else "False"
    if keep_mc not in ("True", "False"):
        raise ValueError("KeepMCValues must be a native boolean")
    rms_vals: dict[str, float] = {}
    for key in (RMS_ISC, RMS_VOC):
        if key in age.fields:
            raw_rms = one(age.fields, key)[1]
            try:
                rms_vals[key] = _age_pct(float(raw_rms), key, 100.0)
            except (ValueError, OverflowError):
                raise ValueError("Require a finite native " + key) from None
        else:
            rms_vals[key] = 0.0
    sharing = 80.0
    if IMP_SHARING in age.fields:
        try:
            sharing = finite_number(float(one(age.fields, IMP_SHARING)[1]), IMP_SHARING, minimum=0.0, maximum=100.0)
        except (ValueError, OverflowError):
            raise ValueError("Require a finite native ImpDegradSharing") from None
    for key, owner in ((YEAR_FIELD, root.fields), (STORED_LOSS, root.fields), (STORED_MISMATCH, root.fields), (MEAN_FIELD, age.fields)):
        if key in owner and _count_field(lines, key) != len(owner.get(key, [])):
            raise ValueError(f"Require {key} only in its owning object")
    if not re.fullmatch(r"[0-9]+", year_raw := one(root.fields, YEAR_FIELD)[1]):
        raise ValueError("Require a native integer NoYearSimul")
    stored = {}
    for k in (STORED_LOSS, STORED_MISMATCH):
        if k in root.fields:
            try:
                stored[k] = finite_number(float(one(root.fields, k)[1]), k, minimum=0.0, maximum=100.0)
            except (ValueError, OverflowError):
                raise ValueError("Require a finite native " + k) from None
    try:
        mean_pct = _age_pct(float(one(age.fields, MEAN_FIELD)[1]), "annual_module_degradation_percent", 5.0)
    except (ValueError, OverflowError):
        raise ValueError("Require a finite native ModuleDegradFactor") from None
    has_cubic = any(isinstance(b, Profile) and b.path.startswith("PVObject_Ageing/") for b in blocks)
    return shift, root, age, {
        "mode": "mounted",
        "enabled": enabled,
        "simulation_year": _age_year(int(year_raw)),
        "annual_module_degradation_percent": mean_pct,
        IMP_SHARING_KEY: sharing,
        "keep_mc_values": keep_mc == "True",
        RMS_ISC_KEY: rms_vals[RMS_ISC],
        RMS_VOC_KEY: rms_vals[RMS_VOC],
        "stored_loss_factor_percent": stored.get(STORED_LOSS),
        "stored_mismatch_factor_percent": stored.get(STORED_MISMATCH),
        "has_cubic_profile": has_cubic,
    }


def ageing_model_state(lines: list[str], bounds: Any) -> dict[str, Any]:
    """Read mounted or absent ageing state; malformed blocks report mode None and an issue."""
    try:
        return read_ageing(lines, bounds, require_enabled=False, require_present=False)[3]
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}


def _update_ageing_pure_curve(candidate: list[str], shift: int, blocks: list[Any], deg: float) -> None:
    pure = next((b for b in blocks if isinstance(b, Profile) and b.path == "PVObject_Ageing/AgeingLossPure"), None)
    if pure is None or pure.fields.get("NPtsEff", [("", "")])[0][1] != "7":
        return
    for idx, yr in enumerate(_AGE_PURE_YEARS, start=1):
        pk = f"Point_{idx}"
        if pk in pure.fields:
            pos = shift + one(pure.fields, pk)[0]
            raw_pt = one(pure.fields, pk)[1].split(",", 1)[0].strip()
            try:
                if abs(float(raw_pt) - yr) < 1e-6:
                    candidate[pos] = replace_value(candidate[pos], f"{yr:.1f},{min(100.0, yr * deg):.3f}")
            except ValueError:
                continue


def replace_ageing_model(lines: list[str], bounds: Any, updates: dict[str, Any]) -> dict[str, Any]:
    """Mount or update pvAgeing in pvMainArray and enable Flags2 bit $00100000 atomically."""
    if not isinstance(updates, dict):
        raise ValueError("Require a mapping of ageing model inputs")
    if (set(updates) - set(_AGE_UPDATE_REQ) - set(_AGE_UPDATE_OPT)) or not set(_AGE_UPDATE_REQ) <= set(updates):
        raise ValueError(
            f"Require simulation_year and annual_module_degradation_percent, "
            f"with optional keep_mc_values, {IMP_SHARING_KEY}, {RMS_ISC_KEY} and {RMS_VOC_KEY}"
        )
    if (RMS_ISC_KEY in updates) != (RMS_VOC_KEY in updates):
        raise ValueError(f"{RMS_ISC_KEY} and {RMS_VOC_KEY} must be supplied together")
    year = _age_year(updates["simulation_year"])
    deg = _age_pct(updates["annual_module_degradation_percent"], "annual_module_degradation_percent", 5.0)
    plan_extra: list[tuple[str, str]] = []
    if "keep_mc_values" in updates:
        if type(updates["keep_mc_values"]) is not bool:
            raise ValueError("keep_mc_values must be a strict boolean")
        plan_extra.append((KEEP_MC, "True" if updates["keep_mc_values"] else "False"))
    if IMP_SHARING_KEY in updates:
        plan_extra.append((IMP_SHARING, f"{_age_sharing(updates[IMP_SHARING_KEY]):.1f}"))
    if RMS_ISC_KEY in updates:
        plan_extra.extend([
            (RMS_ISC, f"{_age_pct(updates[RMS_ISC_KEY], RMS_ISC_KEY, 100.0):.2f}"),
            (RMS_VOC, f"{_age_pct(updates[RMS_VOC_KEY], RMS_VOC_KEY, 100.0):.2f}"),
        ])
    shift, root, age, before = read_ageing(lines, bounds, require_enabled=False, require_present=False)
    eol = "\r\n" if lines[shift].endswith("\r\n") else "\n"
    candidate = list(lines)
    if age is None:
        anchor = next(
            (i for i in range(shift + 1, bounds[1]) if any(lines[i].strip().startswith(p) for p in _MAIN_ANCHORS)),
            bounds[1],
        )
        first_anchor = candidate[anchor]
        field_indent = first_anchor[:len(first_anchor) - len(first_anchor.lstrip())]
        if anchor == bounds[1]:
            root_indent = candidate[shift][:len(candidate[shift]) - len(candidate[shift].lstrip())]
            field_indent = root_indent + "  "
        spec = {"enabled": True, "simulation_year": year, "annual_module_degradation_percent": deg,
                **{k: updates[k] for k in updates if k not in _AGE_UPDATE_REQ}}
        inserted = [f"{ln}{eol}" for ln in render_ageing_lines(spec, indent=field_indent)]
        candidate[anchor:anchor] = inserted
        f2_idx, f2_raw = one(root.fields, "Flags2")
        f2_val = int(f2_raw[1:], 16)
        candidate[shift + f2_idx] = replace_value(candidate[shift + f2_idx], format_flags2(f2_val | AGEING_FLAG2_BIT))
        new_bounds = (bounds[0], bounds[1] + len(inserted))
        after = read_ageing(candidate, new_bounds, require_enabled=True, require_present=True)[3]
        lines[:] = candidate
        return {"before": before, "after": after, "mounted": True}

    _, blocks, _, _, f2_val, _ = _main_context(candidate, bounds)
    updates_map: list[tuple[dict, str, str]] = [(root.fields, YEAR_FIELD, f"{year:d}"), (age.fields, MEAN_FIELD, f"{deg:.2f}")]
    if STORED_LOSS in root.fields:
        updates_map.append((root.fields, STORED_LOSS, f"{min(100.0, max(0.0, (year - 0.5) * deg)):.2f}"))
    for key, raw in plan_extra:
        if key not in age.fields:
            raise ValueError(f"Mounted pvAgeing block lacks optional target field {key}")
        updates_map.append((age.fields, key, raw))
    for owner_fields, key, raw in updates_map:
        idx = shift + one(owner_fields, key)[0]
        candidate[idx] = replace_value(candidate[idx], raw)
    _update_ageing_pure_curve(candidate, shift, blocks, deg)
    if not (f2_val & AGEING_FLAG2_BIT):
        f2_idx = shift + one(root.fields, "Flags2")[0]
        candidate[f2_idx] = replace_value(candidate[f2_idx], format_flags2(f2_val | AGEING_FLAG2_BIT))
    after = read_ageing(candidate, bounds, require_enabled=True, require_present=True)[3]
    lines[:] = candidate
    return {"before": before, "after": after, "mounted": False}


def replace_ageing_enabled(lines: list[str], bounds: Any, enabled: Any) -> dict[str, Any]:
    """Toggle only pvMainArray Flags2 bit $00100000 while preserving all non-ageing bits."""
    if type(enabled) is not bool:
        raise ValueError("enabled must be a strict boolean")
    shift, root, age, before = read_ageing(lines, bounds, require_enabled=False, require_present=False)
    if age is None and enabled:
        raise ValueError("Cannot enable ageing before mounting PVObject_Ageing; supply ageing_updates first")
    candidate = list(lines)
    idx, raw_f2 = one(root.fields, "Flags2")
    value = int(raw_f2[1:], 16)
    target = (value | AGEING_FLAG2_BIT) if enabled else (value & ~AGEING_FLAG2_BIT)
    if target != value:
        candidate[shift + idx] = replace_value(candidate[shift + idx], format_flags2(target))
    after = read_ageing(candidate, bounds, require_enabled=False, require_present=False)[3]
    if after["enabled"] is not enabled:
        raise ValueError("The candidate variant did not reach the requested ageing enable state")
    lines[:] = candidate
    return {"before": before, "after": after}


# --- Economics piecewise-linear ageing curves ---

def normalize_yearly_energy_anchors(raw: Any, context: str = "yearly_energy_kwh") -> dict[int, float]:
    """Validate >= 2 year -> kWh anchor points (accepting int or integer-string JSON keys in 1..50)."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{context} must be a non-empty object mapping year -> kWh")
    anchors: dict[int, float] = {}
    for raw_key, raw_val in raw.items():
        if type(raw_key) is int:
            year = raw_key
        elif isinstance(raw_key, str) and re.fullmatch(r"[0-9]+", raw_key.strip()):
            year = int(raw_key.strip())
        else:
            raise ValueError(f"{context}: invalid year key {raw_key!r}; expected integer 1..50")
        if not 1 <= year <= 50:
            raise ValueError(f"{context}: year {year} must be in 1..50")
        if year in anchors:
            raise ValueError(f"{context}: duplicate year anchor {year}")
        anchors[year] = finite_number(raw_val, f"{context}[{year}]", minimum=0.0)
    if len(anchors) < 2:
        raise ValueError(f"{context} requires at least 2 year anchors")
    if max(anchors.values()) <= 0.0:
        raise ValueError(f"{context} must include at least one positive energy value")
    return anchors


def load_ageing_batch_curve(batch_path: Path, energy_unit_to_kwh: dict[str, float]) -> dict[int, float]:
    """Load Simulation for Year -> E_Grid [kWh] anchors from a PVsyst UserBatch *Results*.CSV file."""
    from pvsyst_auto.native.cli import parse_batch_results

    parsed = parse_batch_results(batch_path, columns=("E_Grid",))
    unit = parsed.get("units", {}).get("E_Grid", "kWh")
    if unit not in energy_unit_to_kwh:
        raise ValueError(f"Expected energy unit for batch E_Grid in {batch_path.name}, got {unit!r}")
    scale = energy_unit_to_kwh[unit]
    anchors: dict[int, float] = {}
    for row in parsed.get("scenarios") or ():
        if row.get("error"):
            continue
        params = row.get("parameters") or {}
        year_key = next((k for k in params if k.strip().casefold().replace("_", " ") in _BATCH_YEAR_KEYS), None)
        if year_key is None:
            raise ValueError(f"{batch_path.name} does not contain a 'Simulation for Year' parameter column")
        raw_year = params[year_key]
        if type(raw_year) is not int or not 1 <= raw_year <= 50:
            raise ValueError(f"{batch_path.name}: invalid simulation_year value {raw_year!r}")
        if raw_year in anchors:
            raise ValueError(f"{batch_path.name}: duplicate simulation_year {raw_year}")
        e_grid = (row.get("values") or {}).get("E_Grid")
        if isinstance(e_grid, bool) or not isinstance(e_grid, (int, float)) or not math.isfinite(e_grid) or e_grid < 0.0:
            raise ValueError(f"{batch_path.name}: invalid E_Grid for year {raw_year}")
        anchors[raw_year] = float(e_grid) * scale
    if len(anchors) < 2:
        raise ValueError(f"{batch_path.name} requires at least 2 error-free simulation_year rows")
    if max(anchors.values()) <= 0.0:
        raise ValueError(f"{batch_path.name} must contain positive E_Grid values")
    return anchors


def interpolate_yearly_energy(anchors: dict[int, float], project_life_years: int) -> dict[int, float]:
    """Piecewise-linear interpolation between adjacent sorted anchors and linear edge extrapolation."""
    years = sorted(anchors)
    if len(years) < 2:
        raise ValueError("Piecewise-linear ageing curve requires at least 2 year anchors")
    out: dict[int, float] = {}
    for y in range(1, project_life_years + 1):
        if y in anchors:
            val = anchors[y]
        elif y < years[0]:
            y0, y1 = years[0], years[1]
            slope = (anchors[y1] - anchors[y0]) / (y1 - y0)
            val = anchors[y0] + slope * (y - y0)
        elif y > years[-1]:
            y0, y1 = years[-2], years[-1]
            slope = (anchors[y1] - anchors[y0]) / (y1 - y0)
            val = anchors[y1] + slope * (y - y1)
        else:
            pos = next(i for i in range(len(years) - 1) if years[i] < y < years[i + 1])
            y0, y1 = years[pos], years[pos + 1]
            val = anchors[y0] + (anchors[y1] - anchors[y0]) * (y - y0) / (y1 - y0)
        out[y] = max(0.0, float(val))
    return out


def yearly_degradation_factors(anchors: dict[int, float], project_life_years: int) -> tuple[dict[int, float], dict[int, float]]:
    """Return (yearly_energy_kwh, year->relative_factor vs Year 1) for economics cashflows."""
    yearly = interpolate_yearly_energy(anchors, project_life_years)
    base = yearly[1]
    if base <= 0.0:
        raise ValueError("Interpolated Year-1 ageing energy must be greater than zero")
    return yearly, {y: yearly[y] / base for y in range(1, project_life_years + 1)}

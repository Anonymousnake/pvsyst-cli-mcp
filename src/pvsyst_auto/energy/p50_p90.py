"""Zero-template PVsyst P50/P90 (TP50_P90) serialization, normal-CDF yields, and bankability metrics."""
from __future__ import annotations

from decimal import Decimal
import math
import re
from statistics import NormalDist
from typing import Any

from pvsyst_auto.components.editor import ASSIGNMENT, objects, replace_value
from pvsyst_auto.validation import NATIVE_TEXT_OR_SEED, SEED_LABEL, finite_number

_P50_HEAD = "P50_P90=TP50_P90"
_P50_END = "EndTags"
_P50_PLOTS = "SimulPlots=Start"
_P50_ROOT_TYPE = "pvVCalcul"
_P50_ROOT_FLAGS = 0x01800001
_P50_ORDER = (
    "MeteoKind", "P90Custom", "PxxCustom", "PxxReport", "PxxMainResults", "PlotKind",
    "YearDevFromAver", "MeteoAnnualVariab", "PVModelVariab", "InverterVariab",
    "SoilingVariab", "DegradVariab", "CustomVariab", "CustomVarLabel",
)
_P50_INTS = ("P90Custom", "PxxCustom")
_P50_BOOLS = ("PxxReport", "PxxMainResults")
_P50_VARIAB = (
    "MeteoAnnualVariab", "PVModelVariab", "InverterVariab",
    "SoilingVariab", "DegradVariab", "CustomVariab",
)
_P50_PLOT_KIND = 1
_P50_METEO_KINDS = ("SpecYear", "TMY", "MonthAverage")
_P50_OMITTABLE_ON_READ = frozenset(("YearDevFromAver", "CustomVariab"))
_P50_HEX = re.compile(r"\$[0-9A-Fa-f]{1,8}")
_NORMAL = NormalDist()

DEFAULT_P50_NATIVE: dict[str, Any] = {
    "MeteoKind": "TMY",
    "P90Custom": 90,
    "PxxCustom": 95,
    "PxxReport": True,
    "PxxMainResults": False,
    "PlotKind": 1,
    "YearDevFromAver": 0.0,
    "MeteoAnnualVariab": 4.0,
    "PVModelVariab": 1.0,
    "InverterVariab": 0.5,
    "SoilingVariab": 1.0,
    "DegradVariab": 1.0,
    "CustomVariab": 0.0,
    "CustomVarLabel": "Custom variability",
}

_SNAKE_TO_NATIVE: dict[str, str] = {
    "meteo_kind": "MeteoKind",
    "p90_custom": "P90Custom",
    "pxx_custom": "PxxCustom",
    "pxx_report": "PxxReport",
    "pxx_main_results": "PxxMainResults",
    "plot_kind": "PlotKind",
    "year_dev_from_aver_pct": "YearDevFromAver",
    "meteo_annual_variab_pct": "MeteoAnnualVariab",
    "pv_model_variab_pct": "PVModelVariab",
    "inverter_variab_pct": "InverterVariab",
    "soiling_variab_pct": "SoilingVariab",
    "degrad_variab_pct": "DegradVariab",
    "custom_variab_pct": "CustomVariab",
    "custom_var_label": "CustomVarLabel",
}
_SPEC_EXTRA_KEYS = frozenset(("enabled", "evaluation_years"))
_ALL_SPEC_KEYS = frozenset(_P50_ORDER) | frozenset(_SNAKE_TO_NATIVE) | _SPEC_EXTRA_KEYS


def _p50_step(value: Any, label: str, low: float, high: float) -> float:
    """Validate finite 0.001-step P50/P90 numeric values and normalize -0.0 to 0.0."""
    number = finite_number(value, label, minimum=low, maximum=high)
    scaled = Decimal(str(number)) * 1000
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{label} must use 0.001 increments")
    return 0.0 if number == 0 else number


def _p50_label(value: Any) -> str:
    """Validate CustomVarLabel as 1..80 printable ASCII bytes without outer whitespace."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("CustomVarLabel must be a trimmed single-line string")
    if not value.isascii() or not value.isprintable() or len(value.encode("ascii")) > 80:
        raise ValueError("CustomVarLabel must be 1..80 printable ASCII bytes")
    return value


def _p50_validate(values: Any) -> dict[str, Any]:
    """Strictly validate all 14 native TP50_P90 fields."""
    if not isinstance(values, dict) or set(values) != set(_P50_ORDER):
        raise ValueError("Require exactly the fourteen native P50 keys")
    out: dict[str, Any] = {"MeteoKind": values["MeteoKind"]}
    if out["MeteoKind"] not in _P50_METEO_KINDS:
        raise ValueError("MeteoKind must be SpecYear, TMY, or MonthAverage")
    for key in _P50_INTS:
        if type(values[key]) is not int:
            raise ValueError(f"{key} must be a strict integer")
        if not 51 <= values[key] <= 99:
            raise ValueError(f"{key} must stay inside 51..99")
        out[key] = values[key]
    if not 51 <= out["P90Custom"] < out["PxxCustom"] <= 99:
        raise ValueError("Require 51 <= P90Custom < PxxCustom <= 99")
    for key in _P50_BOOLS:
        if type(values[key]) is not bool:
            raise ValueError(f"{key} must be a strict boolean")
        out[key] = values[key]
    if type(values["PlotKind"]) is not int or values["PlotKind"] != _P50_PLOT_KIND:
        raise ValueError(f"PlotKind must be the integer {_P50_PLOT_KIND}")
    out["PlotKind"] = _P50_PLOT_KIND
    year_dev = _p50_step(values["YearDevFromAver"], "YearDevFromAver", -100, 100)
    if year_dev <= -100.0:
        raise ValueError("YearDevFromAver must exceed -100%")
    out["YearDevFromAver"] = year_dev
    for key in _P50_VARIAB:
        out[key] = _p50_step(values[key], key, 0, 100)
    out["CustomVarLabel"] = _p50_label(values["CustomVarLabel"])
    return out


def normalize_p50_spec(raw: Any) -> dict[str, Any]:
    """Normalize snake_case, PascalCase, or already-normalized P50/P90 config into canonical form."""
    if not isinstance(raw, dict):
        raise ValueError("p50_p90 configuration must be an object")
    if set(raw) == {"enabled", "evaluation_years", "native"} and isinstance(raw.get("native"), dict):
        enabled = raw["enabled"]
        eval_years = raw["evaluation_years"]
        if type(enabled) is not bool:
            raise ValueError("p50_p90.enabled must be a strict boolean")
        if type(eval_years) is not int or not 1 <= eval_years <= 50:
            raise ValueError("p50_p90.evaluation_years must be an integer in 1..50")
        return {"enabled": enabled, "evaluation_years": eval_years, "native": _p50_validate(raw["native"])}
    if extra := sorted(set(raw) - _ALL_SPEC_KEYS):
        raise ValueError(f"Unknown p50_p90 field(s): {', '.join(extra)}")
    enabled = raw.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("p50_p90.enabled must be a strict boolean")
    eval_years = raw.get("evaluation_years", 1)
    if type(eval_years) is not int or not 1 <= eval_years <= 50:
        raise ValueError("p50_p90.evaluation_years must be an integer in 1..50")
    merged = dict(DEFAULT_P50_NATIVE)
    for snake_key, native_key in _SNAKE_TO_NATIVE.items():
        has_snake = snake_key in raw
        has_native = native_key in raw
        if has_snake and has_native:
            raise ValueError(f"Specify either {snake_key} or {native_key}, not both")
        if has_snake:
            merged[native_key] = raw[snake_key]
        elif has_native:
            merged[native_key] = raw[native_key]
    return {
        "enabled": enabled,
        "evaluation_years": eval_years,
        "native": _p50_validate(merged),
    }


def resolve_p50_p90_config(p50_p90: Any = None, uncertainty: Any = None) -> dict[str, Any] | None:
    """Resolve optional p50_p90 / uncertainty config; returns None when omitted or disabled."""
    if p50_p90 is not None and uncertainty is not None:
        raise ValueError("Specify either p50_p90 or uncertainty, not both")
    raw = p50_p90 if p50_p90 is not None else uncertainty
    if raw is None:
        return None
    norm = normalize_p50_spec(raw)
    return norm if norm["enabled"] else None


def _format_p50_value(key: str, val: Any) -> str:
    if key in _P50_BOOLS:
        return "True" if val is True else "False"
    if key in _P50_VARIAB or key == "YearDevFromAver":
        return format(val, ".3f")
    return str(val)


def render_p50_lines(spec: dict[str, Any], *, indent: str = "  ") -> list[str]:
    """Serialize a normalized or raw P50/P90 configuration into P50_P90=TP50_P90 lines."""
    if set(spec) == set(_P50_ORDER):
        native = _p50_validate(spec)
    else:
        native = normalize_p50_spec(spec)["native"]
    body = [f"{indent}  {key}={_format_p50_value(key, native[key])}" for key in _P50_ORDER]
    return [f"{indent}{_P50_HEAD}", *body, f"{indent}{_P50_END}"]


def _validate_p50_root_header(lines: list[str], end_pos: int) -> None:
    """Validate single root pvVCalcul Version and hex Flags up to `end_pos`."""
    if not lines:
        raise ValueError(f"Require a single {_P50_ROOT_TYPE} root object")
    eol = "\r\n" if lines[0].endswith("\r\n") else "\n"
    try:
        blocks = objects("".join(lines[:end_pos]) + f"End of PVObject {_P50_ROOT_TYPE}{eol}")
    except ValueError:
        raise ValueError("Root object prefix is unbalanced") from None
    roots = [b for b in blocks if not b.path]
    if len(roots) != 1 or roots[0].object_type != _P50_ROOT_TYPE:
        raise ValueError(f"Require a single {_P50_ROOT_TYPE} root object")
    if roots[0].one("Version")[1] not in NATIVE_TEXT_OR_SEED:
        raise ValueError(f"P50 requires root Version={SEED_LABEL}")
    raw_flags = roots[0].one("Flags")[1]
    if not _P50_HEX.fullmatch(raw_flags):
        raise ValueError("P50 root Flags must be a hexadecimal token")


def _p50_read_block(lines: list[str], require_template: bool = False):
    """Parse an existing P50_P90=TP50_P90 block; returns (head, tail, fields, validated) or None."""
    del require_template  # Zero-template editing no longer requires $01800001 root Flags.
    heads = [i for i, ln in enumerate(lines) if ln.strip() == _P50_HEAD]
    if len(heads) > 1:
        raise ValueError("Require at most one root P50_P90 block")
    if not heads:
        return None
    head = heads[0]
    tail = next((i for i in range(head + 1, len(lines)) if lines[i].strip() == _P50_END), None)
    if tail is None:
        raise ValueError("Unclosed P50_P90 block")
    plots = [i for i, ln in enumerate(lines) if ln.strip() == _P50_PLOTS]
    if len(plots) > 1 or (plots and (head > plots[0] or tail > plots[0])):
        raise ValueError("Require SimulPlots marker after the whole P50_P90 block")
    _validate_p50_root_header(lines, head)
    fields: dict[str, tuple[int, str]] = {}
    for i in range(head + 1, tail):
        if lines[i].strip().startswith(("PVObject_", "End of ", _P50_HEAD)):
            raise ValueError("P50_P90 must stay a flat tag block")
        match = ASSIGNMENT.fullmatch(lines[i].rstrip("\r\n"))
        if match is None or match[2] not in _P50_ORDER or match[2] in fields:
            raise ValueError("Unknown or duplicate P50 field inside the block")
        fields[match[2]] = (i, match[4])
    missing = set(_P50_ORDER) - set(fields)
    if missing - _P50_OMITTABLE_ON_READ:
        raise ValueError("P50_P90 is missing required native fields: " + ", ".join(sorted(missing)))
    decoded: dict[str, Any] = {}
    for key in _P50_ORDER:
        if key not in fields:
            decoded[key] = 0.0
            continue
        raw = fields[key][1]
        if key in _P50_BOOLS:
            if raw not in ("True", "False"):
                raise ValueError(f"{key} must be native True or False")
            decoded[key] = raw == "True"
        elif key in _P50_INTS or key == "PlotKind":
            if not re.fullmatch(r"-?[0-9]+", raw):
                raise ValueError(f"{key} must be a native integer")
            decoded[key] = int(raw)
        elif key in ("MeteoKind", "CustomVarLabel"):
            decoded[key] = raw
        else:
            try:
                decoded[key] = float(raw)
            except (ValueError, OverflowError):
                raise ValueError(f"{key} must be a native number") from None
    return head, tail, fields, _p50_validate(decoded)


def _p50_read(lines: list[str], require_template: bool = False):
    """Backward-compatible wrapper returning (fields, validated) or None."""
    got = _p50_read_block(lines, require_template=require_template)
    return None if got is None else (got[2], got[3])


def p50_state(lines: list[str]) -> dict[str, Any]:
    """Return native P50 state; absent blocks report 'none', invalid ones report an issue."""
    try:
        got = _p50_read_block(lines)
    except ValueError as exc:
        return {"mode": None, "issue": str(exc)}
    return {"mode": "none", "values": None} if got is None else {"mode": "configured", "values": got[3]}


def _p50_insert_index(lines: list[str]) -> int:
    """Locate the canonical pvVCalcul anchor matching _DEMO_Commercial.VC2 (after pvShading/pvSystem)."""
    markers = ("PVObject_CarbonBalance=", "EconomicEval=TEcoEvaluation", _P50_PLOTS, f"End of PVObject {_P50_ROOT_TYPE}")
    earliest_marker = next((i for i, ln in enumerate(lines) if ln.strip().startswith(markers)), None)
    if earliest_marker is None:
        raise ValueError(f"Require an {_P50_ROOT_TYPE} closing anchor to mount P50_P90")
    for close_tag in ("End of PVObject pvShading", "End of PVObject pvSystem"):
        idx = next((i for i, ln in enumerate(lines[:earliest_marker]) if ln.strip() == close_tag), None)
        if idx is not None:
            return idx + 1
    return earliest_marker


def replace_p50(lines: list[str], updates: dict[str, Any]) -> dict[str, Any]:
    """Mount, update, or remove root P50_P90=TP50_P90 cleanly without requiring a template block."""
    if not isinstance(updates, dict):
        raise ValueError("p50_p90_updates must be an object")
    got = _p50_read_block(lines)
    before = got[3] if got is not None else None
    mode = updates.get("mode")
    if mode is not None and mode not in ("set", "none"):
        raise ValueError("p50_p90_updates.mode must be 'set' or 'none'")
    if mode == "none":
        if set(updates) != {"mode"}:
            raise ValueError("p50_p90_updates with mode='none' accepts no additional keys")
        return _remove_p50_block(lines, got, before)

    cleaned = {k: v for k, v in updates.items() if k != "mode"}
    norm = normalize_p50_spec(cleaned) if set(cleaned) != set(_P50_ORDER) else {
        "enabled": True, "evaluation_years": 1, "native": _p50_validate(cleaned),
    }
    if not norm["enabled"]:
        return _remove_p50_block(lines, got, before)

    target = norm["native"]
    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    cand = list(lines)

    if got is not None:
        head, tail, fields, _ = got
        if before == target and set(fields) == set(_P50_ORDER):
            return {"before": before, "after": before, "mounted": False}
        if set(fields) == set(_P50_ORDER):
            for key, (index, raw) in fields.items():
                rendered = _format_p50_value(key, target[key])
                if rendered != raw:
                    cand[index] = replace_value(cand[index], rendered)
        else:
            indent = cand[head][:len(cand[head]) - len(cand[head].lstrip())]
            cand[head:tail + 1] = [f"{ln}{eol}" for ln in render_p50_lines(target, indent=indent)]
        verified = _p50_read_block(cand)
        if verified is None:
            raise ValueError("Mounted P50_P90 block failed verification")
        lines[:] = cand
        return {"before": before, "after": verified[3], "mounted": False}

    root_end = next((i for i, ln in enumerate(cand) if ln.strip() == f"End of PVObject {_P50_ROOT_TYPE}"), None)
    if root_end is None:
        raise ValueError(f"Require a complete {_P50_ROOT_TYPE} root object to mount P50_P90")
    _validate_p50_root_header(cand, root_end)
    plots = [i for i, ln in enumerate(cand) if ln.strip() == _P50_PLOTS]
    if len(plots) > 1:
        raise ValueError("Require at most one SimulPlots marker in the variant")
    pos = _p50_insert_index(cand)
    block = [f"{ln}{eol}" for ln in render_p50_lines(target, indent="  ")]
    prefix = [eol] if pos > 0 and cand[pos - 1].strip() else []
    suffix = [eol] if pos < len(cand) and cand[pos].strip() else []
    cand[pos:pos] = [*prefix, *block, *suffix]
    verified = _p50_read_block(cand)
    if verified is None:
        raise ValueError("Mounted P50_P90 block failed verification")
    lines[:] = cand
    return {"before": None, "after": verified[3], "mounted": True}


def _remove_p50_block(lines: list[str], got, before: dict[str, Any] | None) -> dict[str, Any]:
    if got is None:
        root_end = next((i for i, ln in enumerate(lines) if ln.strip() == f"End of PVObject {_P50_ROOT_TYPE}"), None)
        if root_end is None:
            raise ValueError(f"Require a complete {_P50_ROOT_TYPE} root object")
        _validate_p50_root_header(lines, root_end)
        return {"before": None, "after": None, "mounted": False, "removed": False}
    head, tail, _, _ = got
    cand = list(lines)
    start = head - 1 if head > 0 and not cand[head - 1].strip() and tail + 1 < len(cand) and not cand[tail + 1].strip() else head
    del cand[start:tail + 1]
    lines[:] = cand
    return {"before": before, "after": None, "mounted": False, "removed": True}


# --- Pure-Python normal-CDF P50/P75/P90/P99 & bankability math ---

def normal_cdf(z: float) -> float:
    """Standard normal cumulative distribution function Phi(z)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def normal_inv_cdf(p: float) -> float:
    """Inverse standard normal CDF Phi^-1(p) for 0 < p < 1."""
    if not 0.0 < p < 1.0:
        raise ValueError("Probability p must lie strictly inside (0, 1)")
    return float(_NORMAL.inv_cdf(p))


def combined_uncertainty_pct(native: dict[str, Any], *, evaluation_years: int = 1) -> float:
    """Compute RSS combined uncertainty (%) with inter-annual weather variance scaled by 1/N."""
    if type(evaluation_years) is not int or not 1 <= evaluation_years <= 50:
        raise ValueError("evaluation_years must be an integer in 1..50")
    meteo_var = (float(native["MeteoAnnualVariab"]) ** 2) / float(evaluation_years)
    other_var = sum(
        float(native[k]) ** 2
        for k in ("PVModelVariab", "InverterVariab", "SoilingVariab", "DegradVariab", "CustomVariab")
    )
    return math.sqrt(meteo_var + other_var)


def compute_p50_p90_yields(
    annual_yield_kwh: float,
    spec: dict[str, Any],
    *,
    installed_dc_kw: float | None = None,
) -> dict[str, Any]:
    """Compute P50/P75/P90/P99 yields and exceedance curve from pure-Python normal-CDF math."""
    sim_kwh = finite_number(annual_yield_kwh, "annual_yield_kwh", minimum=0.0)
    norm = normalize_p50_spec(spec)
    native = norm["native"]
    eval_years = norm["evaluation_years"]
    year_dev = float(native["YearDevFromAver"])
    if year_dev <= -100.0:
        raise ValueError("YearDevFromAver must exceed -100%")
    p50_factor_vs_sim = 1.0 / (1.0 + year_dev / 100.0)
    p50_kwh = sim_kwh * p50_factor_vs_sim
    sigma_pct = combined_uncertainty_pct(native, evaluation_years=eval_years)
    sigma_frac = sigma_pct / 100.0

    def _for_prob(prob_pct: int) -> tuple[float, float, float]:
        z = 0.0 if prob_pct == 50 else normal_inv_cdf(1.0 - prob_pct / 100.0)
        factor_p50 = max(0.0, 1.0 + z * sigma_frac)
        return z, factor_p50, p50_kwh * factor_p50

    std_probs = (50, 75, 90, 99)
    extra_probs = tuple(p for p in (native["P90Custom"], native["PxxCustom"]) if p not in std_probs)
    yields_kwh: dict[str, float] = {}
    factors_vs_sim: dict[str, float] = {}
    for p in (*std_probs, *extra_probs):
        _, f_p50, y_kwh = _for_prob(p)
        key = f"P{p}"
        yields_kwh[key] = round(y_kwh, 2)
        factors_vs_sim[key] = round(f_p50 * p50_factor_vs_sim, 6)

    grid_probs = sorted({
        1, 5, 10, 20, 25, 30, 40, 50, 60, 70, 75, 80, 85, 90, 95, 99,
        native["P90Custom"], native["PxxCustom"],
    })
    curve = []
    for p in grid_probs:
        z, f_p50, y_kwh = _for_prob(p)
        curve.append({
            "probability_pct": p,
            "z_score": round(z, 4),
            "factor_vs_p50": round(f_p50, 6),
            "annual_yield_kwh": round(y_kwh, 2),
        })

    specific = None
    if installed_dc_kw is not None and installed_dc_kw > 0:
        specific = {k: round(v / installed_dc_kw, 2) for k, v in yields_kwh.items()}

    return {
        "simulated_yield_kwh": round(sim_kwh, 2),
        "p50_yield_kwh": yields_kwh["P50"],
        "combined_uncertainty_pct": round(sigma_pct, 4),
        "evaluation_years": eval_years,
        "year_dev_from_aver_pct": round(year_dev, 3),
        "meteo_kind": native["MeteoKind"],
        "components_pct": {
            **{k: float(native[k]) for k in _P50_VARIAB},
            "CustomVarLabel": native["CustomVarLabel"],
        },
        "yields_kwh": yields_kwh,
        "factors_vs_sim": factors_vs_sim,
        "specific_yields_kwh_per_kwp": specific,
        "exceedance_curve": curve,
    }


def compute_llcr(
    cashflow_schedule: list[dict[str, Any]],
    *,
    debt_principal: float,
    loan_tenor_years: int,
    loan_interest_rate_pct: float,
) -> float | None:
    """Compute Loan Life Coverage Ratio: PV(CFADS over loan tenor at loan rate) / Debt."""
    if debt_principal <= 1e-9 or loan_tenor_years <= 0 or not cashflow_schedule:
        return None
    r_debt = loan_interest_rate_pct / 100.0
    pv_cfads = 0.0
    for row in cashflow_schedule:
        yr = row.get("year")
        if type(yr) is int and 1 <= yr <= loan_tenor_years:
            cfads_y = float(row.get("equity_cashflow") or 0.0) + float(row.get("debt_service") or 0.0)
            pv_cfads += cfads_y / ((1.0 + r_debt) ** yr)
    return round(pv_cfads / debt_principal, 3)


def compute_probabilistic_economics(
    *,
    year1_energy_kwh: float,
    p50_spec: dict[str, Any],
    base_metrics: dict[str, Any],
    run_cashflow_for_scale,
    financial: dict[str, Any],
    installed_dc_kw: float | None = None,
) -> dict[str, Any]:
    """Evaluate P50/P75/P90/P99 cashflows, DSCR, and LLCR bankability metrics."""
    p50_data = compute_p50_p90_yields(year1_energy_kwh, p50_spec, installed_dc_kw=installed_dc_kw)
    fin_loan = financial.get("financing")
    fin_by_p: dict[str, dict[str, Any]] = {}
    bankability: dict[str, Any] = {}

    for label in ("P50", "P75", "P90", "P99"):
        scale = p50_data["factors_vs_sim"][label]
        metrics = base_metrics if abs(scale - 1.0) < 1e-12 else run_cashflow_for_scale(scale)
        item: dict[str, Any] = {
            "annual_yield_kwh": p50_data["yields_kwh"][label],
            "npv": metrics["npv"],
            "irr_pct": metrics["irr_pct"],
            "lcoe": metrics["lcoe"],
            "simple_payback_years": metrics["simple_payback_years"],
            "discounted_payback_years": metrics["discounted_payback_years"],
        }
        fin_sum = metrics.get("financing_summary")
        if isinstance(fin_sum, dict) and fin_loan is not None:
            llcr = compute_llcr(
                metrics.get("cashflow_schedule") or [],
                debt_principal=float(fin_sum.get("debt_principal") or 0.0),
                loan_tenor_years=int(fin_sum.get("loan_tenor_years") or 0),
                loan_interest_rate_pct=float(fin_sum.get("loan_interest_rate_pct") or 0.0),
            )
            item.update({
                "equity_irr_pct": fin_sum.get("equity_irr_pct"),
                "min_dscr": fin_sum.get("min_dscr"),
                "avg_dscr": fin_sum.get("avg_dscr"),
                "llcr": llcr,
            })
            if float(fin_sum.get("debt_principal") or 0.0) > 0.0:
                if not bankability:
                    bankability = {
                        "debt_principal": fin_sum["debt_principal"],
                        "loan_tenor_years": fin_sum["loan_tenor_years"],
                        "loan_interest_rate_pct": fin_sum["loan_interest_rate_pct"],
                    }
                pfx = label.lower()
                bankability[f"{pfx}_min_dscr"] = fin_sum.get("min_dscr")
                bankability[f"{pfx}_avg_dscr"] = fin_sum.get("avg_dscr")
                bankability[f"{pfx}_llcr"] = llcr
        fin_by_p[label] = item

    p50_data["financials"] = fin_by_p
    if bankability:
        p50_data["financing_bankability"] = bankability
    return p50_data

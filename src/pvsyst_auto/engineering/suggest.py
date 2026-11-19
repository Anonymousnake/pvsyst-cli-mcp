"""Read-only electrical sizing assistant deriving recommended capacity bounds from readable PAN/OND files.

Never mutates any workspace file and never pretends estimates are audited manufacturer declarations:
every derived value is accompanied by explicit assumptions and formulas, and auto-sizing runs are
marked ``needs_review`` downstream.
"""
from __future__ import annotations

import math
from pathlib import Path

from pvsyst_auto.components.editor import objects
from pvsyst_auto.components.pan import validation_fields as pan_validation_fields
from pvsyst_auto.components.store import electrical_inventory

DEFAULT_BETA_VOC_PCT_PER_C = -0.30
DEFAULT_BETA_VMP_PCT_PER_C = -0.35
DEFAULT_ALPHA_ISC_PCT_PER_C = 0.05

_ASYMMETRIC_MPPT_PAIRS = (
    ("IscMaxMain", "IscMaxSec", "A"),
    ("IMaxMain", "IMaxSec", "A"),
    ("IMaxDCMain", "IMaxDCSec", "A"),
    ("IDCMaxMain", "IDCMaxSec", "A"),
    ("PNomMain", "PNomSec", "kW"),
    ("PMaxMain", "PMaxSec", "kW"),
    ("NbInputsMain", "NbInputsSec", "inputs"),
    ("NbMPPTMain", "NbMPPTSec", "MPPTs"),
)


def _inspect_ond_mppt_limits(ond_text: str) -> tuple[list[str], float | None]:
    """Check an OND for asymmetric Main/Sec MPPT limits and any uniform declared Isc limit.

    Returns ``(asymmetry_issues, uniform_isc_limit_a)``.
    """
    fields: dict[str, list[float]] = {}
    for block in objects(ond_text):
        if block.path not in ("", "Converter"):
            continue
        for key, entries in block.fields.items():
            for _, raw in entries:
                try:
                    val = float(raw)
                except ValueError:
                    continue
                if math.isfinite(val):
                    fields.setdefault(key, []).append(val)

    issues: list[str] = []
    for main_key, sec_key, unit in _ASYMMETRIC_MPPT_PAIRS:
        main_vals = [v for v in fields.get(main_key, ()) if v > 0]
        sec_vals = [v for v in fields.get(sec_key, ()) if v > 0]
        if main_vals and sec_vals:
            if not math.isclose(main_vals[0], sec_vals[0], rel_tol=1e-9, abs_tol=1e-9):
                issues.append(
                    f"OND declares asymmetric MPPT limits ({main_key}={main_vals[0]:g} {unit} != "
                    f"{sec_key}={sec_vals[0]:g} {unit}); uniform per-MPPT sizing cannot guarantee "
                    "every input stays within rating"
                )
        elif sec_vals and not main_vals:
            issues.append(
                f"OND declares secondary MPPT limit {sec_key}={sec_vals[0]:g} {unit} without a matching "
                f"{main_key}; uniform per-MPPT sizing cannot assume equal inputs"
            )

    uniform_isc: float | None = None
    isc_main = [v for v in fields.get("IscMaxMain", ()) if v > 0]
    isc_sec = [v for v in fields.get("IscMaxSec", ()) if v > 0]
    if isc_main and isc_sec and math.isclose(isc_main[0], isc_sec[0], rel_tol=1e-9, abs_tol=1e-9):
        uniform_isc = isc_main[0]
    elif isc_main and not isc_sec:
        uniform_isc = isc_main[0]
    return issues, uniform_isc


def _finite(value, label: str, *, minimum: float | None = None, maximum: float | None = None,
            strict_min: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if minimum is not None and (number <= minimum if strict_min else number < minimum):
        op = ">" if strict_min else ">="
        raise ValueError(f"{label} must be {op} {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} must be <= {maximum}")
    return number


def _resolve_loose_component(components, kind: str, filename: str) -> tuple[Path, str, str]:
    for library in ("workspace", "builtin"):
        if library == "builtin" and components.builtin is None:
            continue
        path = components._path(kind, filename, library)
        if path.is_file():
            raw = components._read(path)
            info = components._inspect(kind, raw)
            components._require_valid(info, complete=True)
            return path, library, raw.decode("utf-8-sig")
    raise FileNotFoundError(
        f"No readable loose {kind} file {filename!r} found in workspace or builtin library; "
        "sizing estimation requires readable text PAN and OND files"
    )


def _infer_beta_voc_pct(pan_fields: dict, voc: float, caller_pct: float | None,
                        assumptions: list[str]) -> float:
    if caller_pct is not None:
        val = _finite(caller_pct, "beta_voc_pct_per_c", minimum=-1.0, maximum=-0.05)
        assumptions.append(f"beta_voc={val:.3f} %/degC (caller-supplied)")
        return val
    raw_list = pan_fields.get("muVocSpec") or []
    if len(raw_list) == 1:
        try:
            raw_val = float(raw_list[0])
            if math.isfinite(raw_val) and raw_val < 0:
                # PVsyst .PAN commonly stores muVocSpec in mV/degC (e.g. -148.5 for ~50V module),
                # while some text fixtures store V/degC (e.g. -0.1485).
                v_per_c = raw_val / 1000.0 if abs(raw_val) > 1.0 else raw_val
                pct = (v_per_c / voc) * 100.0
                if -0.60 <= pct <= -0.15:
                    unit_note = "mV/degC" if abs(raw_val) > 1.0 else "V/degC"
                    assumptions.append(
                        f"beta_voc={pct:.3f} %/degC derived from PAN muVocSpec={raw_val} ({unit_note}) / Voc={voc:.2f} V"
                    )
                    return pct
        except ValueError:
            pass
    assumptions.append(
        f"beta_voc={DEFAULT_BETA_VOC_PCT_PER_C:.3f} %/degC (standard c-Si default; PAN muVocSpec absent or out of range)"
    )
    return DEFAULT_BETA_VOC_PCT_PER_C


def suggest_sizing(
    store, *,
    pan_name: str,
    ond_name: str,
    target_dc_kw: float,
    voltage_standard: str = "IEC",
    min_ambient_c: float = -10.0,
    min_operating_c: float = 0.0,
    max_operating_c: float = 65.0,
    min_dc_ac_ratio: float = 0.85,
    max_dc_ac_ratio: float = 1.25,
    capacity_tolerance_pct: float = 8.0,
    beta_voc_pct_per_c: float | None = None,
    beta_vmp_pct_per_c: float | None = None,
    alpha_isc_pct_per_c: float | None = None,
) -> dict:
    """Estimate a safe electrical capacity envelope and recommended string/inverter layout from PAN/OND."""
    result = {
        "status": "blocked",
        "estimated": True,
        "requires_review": True,
        "issues": [],
        "assumptions": [],
        "recommended": None,
        "capacity": None,
    }
    assumptions: list[str] = result["assumptions"]

    def issue(field: str, message: str) -> None:
        result["issues"].append({"field": field, "message": message})

    try:
        target = _finite(target_dc_kw, "target_dc_kw", minimum=0.0, strict_min=True)
        if voltage_standard not in ("IEC", "UL"):
            raise ValueError("voltage_standard must be 'IEC' or 'UL'")
        t_min = _finite(min_ambient_c, "min_ambient_c", minimum=-60.0, maximum=40.0)
        t_op_min = _finite(min_operating_c, "min_operating_c", minimum=-60.0, maximum=50.0)
        t_op_max = _finite(max_operating_c, "max_operating_c", minimum=10.0, maximum=100.0)
        if not (t_min <= t_op_min <= t_op_max):
            raise ValueError("Require min_ambient_c <= min_operating_c <= max_operating_c")
        min_ratio = _finite(min_dc_ac_ratio, "min_dc_ac_ratio", minimum=0.5, maximum=2.0)
        max_ratio = _finite(max_dc_ac_ratio, "max_dc_ac_ratio", minimum=0.5, maximum=2.5)
        if not (min_ratio <= max_ratio):
            raise ValueError("min_dc_ac_ratio must be <= max_dc_ac_ratio")
        tol = _finite(capacity_tolerance_pct, "capacity_tolerance_pct", minimum=0.0, maximum=20.0)
    except ValueError as exc:
        issue("inputs", str(exc))
        return result

    try:
        pan_path, pan_lib, pan_text = _resolve_loose_component(store.components, "PAN", pan_name)
        ond_path, ond_lib, ond_text = _resolve_loose_component(store.components, "OND", ond_name)
        pan_elec = electrical_inventory("PAN", pan_text)
        ond_elec = electrical_inventory("OND", ond_text)
        pan_fields = pan_validation_fields(pan_text)
    except (ValueError, OSError) as exc:
        issue("components", str(exc))
        return result

    def _req(inv: dict, name: str, kind: str) -> float:
        entry = (inv.get("fields") or {}).get(name)
        if not isinstance(entry, dict) or entry.get("status") != "declared":
            raise ValueError(f"{kind} does not declare a valid positive {name}")
        return float(entry["value"])

    try:
        pnom_w = _req(pan_elec, "PNom", "PAN")
        voc = _req(pan_elec, "Voc", "PAN")
        vmp = _req(pan_elec, "Vmp", "PAN")
        imp = _req(pan_elec, "Imp", "PAN")
        isc = _req(pan_elec, "ISC", "PAN")
        v_max_mod = _req(pan_elec, "VMax" + voltage_standard, "PAN")
        pnom_ac = _req(ond_elec, "PNomConv", "OND")
        vmpp_min = _req(ond_elec, "VMppMin", "OND")
        vmpp_max = _req(ond_elec, "VMPPMax", "OND")
        vabs_max = _req(ond_elec, "VAbsMax", "OND")
        nb_mppt_field = (ond_elec.get("fields") or {}).get("NbMPPT") or {}
        nb_mppt = int(nb_mppt_field["value"]) if nb_mppt_field.get("status") == "declared" else 1
        if nb_mppt < 1:
            nb_mppt = 1
    except ValueError as exc:
        issue("components", str(exc))
        return result

    try:
        beta_voc = _infer_beta_voc_pct(pan_fields, voc, beta_voc_pct_per_c, assumptions)
        if beta_vmp_pct_per_c is not None:
            beta_vmp = _finite(beta_vmp_pct_per_c, "beta_vmp_pct_per_c", minimum=-1.0, maximum=-0.05)
            assumptions.append(f"beta_vmp={beta_vmp:.3f} %/degC (caller-supplied)")
        else:
            beta_vmp = DEFAULT_BETA_VMP_PCT_PER_C
            assumptions.append(f"beta_vmp={beta_vmp:.3f} %/degC (standard c-Si default)")
        if alpha_isc_pct_per_c is not None:
            alpha_isc = _finite(alpha_isc_pct_per_c, "alpha_isc_pct_per_c", minimum=0.0, maximum=0.3)
            assumptions.append(f"alpha_isc={alpha_isc:.3f} %/degC (caller-supplied)")
        else:
            alpha_isc = DEFAULT_ALPHA_ISC_PCT_PER_C
            assumptions.append(f"alpha_isc={alpha_isc:.3f} %/degC (standard c-Si default)")
    except ValueError as exc:
        issue("coefficients", str(exc))
        return result

    assumptions.append(
        f"Design temperatures: min_ambient={t_min:g} degC, min_operating={t_op_min:g} degC, max_operating={t_op_max:g} degC"
    )

    cold_voc = round(max(voc, voc * (1.0 + (beta_voc / 100.0) * (t_min - 25.0))), 3)
    cold_vmp = round(max(vmp, vmp * (1.0 + (beta_vmp / 100.0) * (t_op_min - 25.0))), 3)
    hot_vmp = round(min(vmp, vmp * (1.0 + (beta_vmp / 100.0) * (t_op_max - 25.0))), 3)
    if hot_vmp <= 0 or not (hot_vmp <= cold_vmp <= cold_voc):
        issue("voltage_envelope", "Derived temperature-adjusted voltage envelope is not monotonic/positive")
        return result

    mod_op_a = round(max(imp, imp * (1.0 + (alpha_isc / 100.0) * max(0.0, t_op_max - 25.0))), 3)
    mod_sc_a = round(max(isc, isc * (1.0 + (alpha_isc / 100.0) * max(0.0, t_op_max - 25.0))), 3)

    mppt_asymmetry_issues, declared_uniform_isc_a = _inspect_ond_mppt_limits(ond_text)
    if mppt_asymmetry_issues:
        for msg in mppt_asymmetry_issues:
            issue("mppt_limits", msg)
        return result

    idc_field = (ond_elec.get("fields") or {}).get("IDCMax") or {}
    if idc_field.get("status") == "declared" and float(idc_field["value"]) > 0:
        total_idc = float(idc_field["value"])
        mppt_op_a = round(total_idc / nb_mppt, 3)
        mppt_sc_a = round(mppt_op_a * 1.25, 3)
        assumptions.append(
            f"MPPT current limit derived from OND IDCMax={total_idc:g} A / NbMPPT={nb_mppt} "
            f"(operating={mppt_op_a:g} A, short-circuit={mppt_sc_a:g} A)"
        )
    else:
        # Estimate per-MPPT DC current capability from inverter AC power at lower-mid MPPT voltage
        v_ref_dc = max(vmpp_min, 0.5 * (vmpp_min + vmpp_max))
        mppt_op_a = round(max(mod_op_a * 2.0, (pnom_ac * 1000.0 * max_ratio) / (nb_mppt * v_ref_dc)), 3)
        mppt_sc_a = round(max(mod_sc_a * 2.0, mppt_op_a * 1.25), 3)
        assumptions.append(
            f"OND IDCMax not declared; estimated per-MPPT current limit from PNomConv={pnom_ac:g} kW "
            f"at Vref={v_ref_dc:g} V (operating={mppt_op_a:g} A, short-circuit={mppt_sc_a:g} A)"
        )
    if declared_uniform_isc_a is not None and declared_uniform_isc_a < mppt_sc_a:
        mppt_sc_a = round(declared_uniform_isc_a, 3)
        assumptions.append(f"Capped per-MPPT short-circuit limit to declared OND IscMax={mppt_sc_a:g} A")

    v_abs_limit = min(vabs_max, v_max_mod)
    min_series = max(1, math.ceil(vmpp_min / hot_vmp))
    max_series = min(100, math.floor(min(vmpp_max / cold_vmp, v_abs_limit / cold_voc)))
    if min_series > max_series:
        issue(
            "voltage_window",
            f"No feasible series count: hot Vmp requires >= {min_series} modules (VMppMin={vmpp_min} V) "
            f"but cold Voc/Vmp caps at <= {max_series} modules (Vmax={v_abs_limit} V, VMPPMax={vmpp_max} V)",
        )
        return result

    max_parallel = int(min(100, mppt_op_a / mod_op_a, mppt_sc_a / mod_sc_a))
    if max_parallel < 1:
        issue("current_window", f"Module current ({mod_op_a} A / {mod_sc_a} A) exceeds MPPT limit ({mppt_op_a} A)")
        return result

    module_kw = pnom_w / 1000.0
    mid_series = 0.5 * (min_series + max_series)
    preferred_ratio = min(max_ratio, max(min_ratio, 1.10))

    candidates = []
    for series in range(min_series, max_series + 1):
        for parallel in range(1, max_parallel + 1):
            per_inv_kw = series * nb_mppt * parallel * module_kw
            ratio = per_inv_kw / pnom_ac
            if not (min_ratio <= ratio <= max_ratio + 1e-9):
                continue
            ideal = min(10000, max(1, target / per_inv_kw))
            for count in {math.floor(ideal), math.ceil(ideal)}:
                if count < 1:
                    continue
                installed_dc = per_inv_kw * count
                err_pct = abs(installed_dc - target) / target * 100.0
                if err_pct <= tol + 1e-9:
                    # Prefer realistic DC/AC ratio near 1.10, low capacity error, then balanced string length
                    score = (
                        round(err_pct, 4),
                        round(abs(ratio - preferred_ratio), 4),
                        count,
                        abs(series - mid_series),
                        series,
                        parallel,
                    )
                    candidates.append((score, series, parallel, count, installed_dc, ratio, err_pct))

    if not candidates:
        issue(
            "sizing",
            f"No integral configuration meets target_dc_kw={target:g} kW (+/-{tol:g}%) with "
            f"DC/AC in [{min_ratio:g}, {max_ratio:g}], series in [{min_series}, {max_series}], "
            f"and parallel/MPPT in [1, {max_parallel}]",
        )
        return result

    _, series, parallel, count, installed_dc, ratio, err_pct = min(candidates)
    strings = count * nb_mppt * parallel
    installed_ac = count * pnom_ac

    basis_text = (
        f"Auto-estimated from {pan_path.name} ({pan_lib}) and {ond_path.name} ({ond_lib}): "
        f"T=[{t_min:g}, {t_op_min:g}, {t_op_max:g}] degC, "
        f"beta_voc={beta_voc:.2f}%/C, beta_vmp={beta_vmp:.2f}%/C, DC/AC in [{min_ratio:g}, {max_ratio:g}]"
    )
    capacity_payload = {
        "target_dc_kw": target,
        "min_dc_ac_ratio": min_ratio,
        "max_dc_ac_ratio": max_ratio,
        "capacity_tolerance_pct": tol,
        "voltage_standard": voltage_standard,
        "cold_voc_v": cold_voc,
        "cold_vmp_v": cold_vmp,
        "hot_vmp_v": hot_vmp,
        "module_max_operating_current_a": mod_op_a,
        "module_max_short_circuit_current_a": mod_sc_a,
        "mppt_max_operating_current_a": mppt_op_a,
        "mppt_max_short_circuit_current_a": mppt_sc_a,
        "design_min_temperature_c": t_min,
        "design_min_operating_temperature_c": t_op_min,
        "design_max_operating_temperature_c": t_op_max,
        "design_basis": basis_text,
    }
    result.update(
        status="ready",
        recommended={
            "series_modules": series,
            "parallel_strings_per_mppt": parallel,
            "mppt_per_inverter": nb_mppt,
            "inverter_count": count,
            "string_count": strings,
            "module_count": strings * series,
            "installed_dc_kw": round(installed_dc, 4),
            "installed_ac_kw": round(installed_ac, 4),
            "dc_ac_ratio": round(ratio, 4),
            "capacity_error_pct": round((installed_dc - target) / target * 100.0, 4),
            "feasible_series_range": [min_series, max_series],
            "max_parallel_strings_per_mppt": max_parallel,
            "voltage_checks": {
                "hot_string_vmp_v": round(series * hot_vmp, 2),
                "cold_string_vmp_v": round(series * cold_vmp, 2),
                "cold_string_voc_v": round(series * cold_voc, 2),
                "inverter_vmpp_range_v": [vmpp_min, vmpp_max],
                "system_vmax_v": v_abs_limit,
            },
        },
        capacity=capacity_payload,
        components={
            "PAN": {"name": pan_path.name, "library": pan_lib},
            "OND": {"name": ond_path.name, "library": ond_lib},
        },
    )
    return result

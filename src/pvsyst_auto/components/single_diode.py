"""Zero-template datasheet synthesizer for PVsyst 8.1.7 .PAN (single-diode) and .OND files.

Derives physically consistent single-diode parameters (RShunt, Rp_0, RSerie, Gamma, muGamma)
directly from commercial module STC & temperature-coefficient datasheet values, and builds
valid Mode-1 efficiency curves (ProfilPIO) for string/central inverters.
"""
from __future__ import annotations

import math
from typing import Any

from pvsyst_auto.validation import pos_finite as _pos_finite
from pvsyst_auto.versions import SYNTH_VERSION

# Boltzmann constant * 298.15 K / elementary charge (thermal voltage at 25 degC STC)
_VT_25C = 1.380649e-23 * 298.15 / 1.602176634e-19  # ~0.025692579 V
_VALID_TECHNOLS = (
    "mtSiMono",
    "mtSiPoly",
    "mtCdTe",
    "mtCIS",
    "mtSiAmor",
    "mtSiThin",
    "mtGaAs",
)


def _component_commercial_header(
    obj_type: str,
    *,
    manufacturer: str,
    model: str,
    source_note: str,
    flags: str,
    width_m: float,
    height_m: float,
    depth_m: float,
    weight_kg: float,
) -> list[str]:
    return [
        f"PVObject_={obj_type}",
        f"  Comment={manufacturer}\t{model}\t{source_note}",
        f"  Version={SYNTH_VERSION}",
        f"  Flags={flags}",
        "  PVObject_Commercial=pvCommercial",
        "    Flags=$0041",
        f"    Manufacturer={manufacturer}",
        f"    Model={model}",
        f"    DataSource={source_note}",
        f"    Width={width_m:.3f}",
        f"    Height={height_m:.3f}",
        f"    Depth={depth_m:.3f}",
        f"    Weight={weight_kg:.2f}",
        "  End of PVObject pvCommercial",
    ]


def _solve_rs_for_gamma(
    *, voc: float, isc: float, vmp: float, imp: float, r_sh: float, a_mod: float
) -> tuple[float, float, float] | None:
    """Solve RSerie > 0 for a given modified ideality voltage a_mod = Gamma * Ns * Vt.

    Returns (r_s, i_l, i_0) or None if no physically positive RSerie exists.
    """
    exp_voc = math.exp(min(voc / a_mod, 700.0))
    if exp_voc <= 1.0 + 1e-12:
        return None
    denom = exp_voc - 1.0
    if isc <= voc / r_sh:
        return None
    i_0 = (isc - voc / r_sh) / denom
    if i_0 <= 0.0 or not math.isfinite(i_0):
        return None

    # Fixed-point / bisection on f(rs) = I_L(rs) - I_0*(exp((Vmp + Imp*rs)/a) - 1) - (Vmp + Imp*rs)/Rsh - Imp = 0
    # Note I_L(rs) = Isc*(1 + rs/Rsh)
    rs_max = max(1e-4, 0.95 * (voc - vmp) / imp)

    def residual(rs: float) -> float:
        i_l = isc * (1.0 + rs / r_sh)
        arg = (vmp + imp * rs) / a_mod
        if arg > 700.0:
            return -1e30
        return i_l - i_0 * (math.exp(arg) - 1.0) - (vmp + imp * rs) / r_sh - imp

    f_lo = residual(0.0)
    f_hi = residual(rs_max)
    if f_lo < 0.0:
        return None
    if f_hi > 0.0:
        return None

    lo, hi = 0.0, rs_max
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if residual(mid) > 0.0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-9:
            break
    rs = 0.5 * (lo + hi)
    i_l = isc * (1.0 + rs / r_sh)
    return rs, i_l, i_0


def derive_single_diode_parameters(
    *,
    voc: float,
    isc: float,
    vmp: float,
    imp: float,
    n_cells_s: int,
    r_shunt: float | None = None,
) -> dict[str, float]:
    """Derive physically consistent PVsyst single-diode parameters from STC datasheet values.

    Solves for (RShunt, Rp_0, Rp_Exp, RSerie, Gamma, muGamma, I0Ref) such that the I-V curve
    passes through (0, Isc), (Vmp, Imp), and (Voc, 0) with dP/dV(Vmp) ~= 0.
    """
    voc = _pos_finite(voc, "Voc")
    isc = _pos_finite(isc, "ISC")
    vmp = _pos_finite(vmp, "Vmp")
    imp = _pos_finite(imp, "Imp")
    if type(n_cells_s) is not int or not 1 <= n_cells_s <= 500:
        raise ValueError("NCelS must be an integer in 1..500")
    if vmp >= voc or imp >= isc:
        raise ValueError("Require Vmp < Voc and Imp < ISC at STC")

    # Estimate RShunt from low-voltage slope if caller did not supply one
    if r_shunt is not None:
        r_sh = _pos_finite(r_shunt, "RShunt")
    else:
        # Empirical c-Si PVsyst rule: RShunt ~= Vmp / (0.015 * (Isc - Imp)), bounded in [150, 2000] Ohm
        r_sh = max(150.0, min(2000.0, vmp / max(0.015 * (isc - imp), 1e-4)))

    vt_mod = n_cells_s * _VT_25C
    best: tuple[float, float, float, float, float] | None = None  # (|dP/dV|, gamma, rs, i_l, i_0)

    # Coarse-to-fine scan on diode ideality factor Gamma in [0.88, 1.45]
    for step_idx in range(115):
        gamma = 0.88 + step_idx * 0.005
        a_mod = gamma * vt_mod
        solved = _solve_rs_for_gamma(voc=voc, isc=isc, vmp=vmp, imp=imp, r_sh=r_sh, a_mod=a_mod)
        if solved is None:
            continue
        rs, i_l, i_0 = solved
        if rs <= 0.005:
            continue
        # Evaluate dP/dV = Imp + Vmp * (dI/dV) at (Vmp, Imp)
        exp_m = math.exp(min((vmp + imp * rs) / a_mod, 700.0))
        g_d = (i_0 / a_mod) * exp_m + (1.0 / r_sh)
        di_dv = -g_d / (1.0 + rs * g_d)
        dp_dv = abs(imp + vmp * di_dv)
        if best is None or dp_dv < best[0]:
            best = (dp_dv, gamma, rs, i_l, i_0)

    used_fallback = best is None
    if used_fallback:
        # Fallback for unusual thin-film or ultra-high-FF specs: pick conservative physically valid defaults
        gamma = 1.05
        rs = max(0.05, min(0.5, 0.25 * (voc - vmp) / imp))
        a_mod = gamma * vt_mod
        i_0 = (isc - voc / r_sh) / max(math.exp(min(voc / a_mod, 700.0)) - 1.0, 1.0)
        exp_m = math.exp(min((vmp + imp * rs) / a_mod, 700.0))
        g_d = (i_0 / a_mod) * exp_m + (1.0 / r_sh)
        dp_dv = abs(imp - vmp * g_d / (1.0 + rs * g_d))
    else:
        dp_dv, gamma, rs, _, i_0 = best
        # Fine local refinement around best gamma
        lo_g = max(0.85, gamma - 0.006)
        hi_g = min(1.50, gamma + 0.006)
        for k in range(25):
            g_try = lo_g + (hi_g - lo_g) * (k / 24.0)
            a_mod = g_try * vt_mod
            solved = _solve_rs_for_gamma(voc=voc, isc=isc, vmp=vmp, imp=imp, r_sh=r_sh, a_mod=a_mod)
            if solved is None:
                continue
            rs_try, _, i0_try = solved
            if rs_try <= 0.005:
                continue
            exp_m = math.exp(min((vmp + imp * rs_try) / a_mod, 700.0))
            g_d = (i0_try / a_mod) * exp_m + (1.0 / r_sh)
            di_dv = -g_d / (1.0 + rs_try * g_d)
            err = abs(imp + vmp * di_dv)
            if err < dp_dv:
                dp_dv, gamma, rs, i_0 = err, g_try, rs_try, i0_try

    rp_0 = round(r_sh * 4.0, 1)
    return {
        "RShunt": round(r_sh, 2),
        "Rp_0": rp_0,
        "Rp_Exp": 5.50,
        "RSerie": round(rs, 4),
        "Gamma": round(gamma, 4),
        "muGamma": -0.0004,
        "I0Ref_A": i_0,
        "mpp_slope_residual_a": round(dp_dv, 6),
        "fallback": used_fallback,
    }


def synthesize_pan_from_datasheet(
    *,
    manufacturer: str,
    model: str,
    pnom_w: float,
    isc_a: float,
    voc_v: float,
    imp_a: float,
    vmp_v: float,
    width_m: float,
    height_m: float,
    depth_m: float = 0.035,
    weight_kg: float = 28.0,
    n_cells_s: int = 72,
    n_cells_p: int = 2,
    n_diode: int = 3,
    technol: str = "mtSiMono",
    mu_isc_ma_per_c: float | None = None,
    mu_voc_mv_per_c: float | None = None,
    mu_pmp_pct_per_c: float = -0.32,
    bifaciality_factor: float | None = None,
    vmax_iec_v: float = 1500.0,
    vmax_ul_v: float = 1500.0,
    source_note: str = "Zero-template datasheet synthesis",
) -> tuple[str, dict[str, Any]]:
    """Generate complete PVsyst 8.1.7 UTF-8 CRLF .PAN text from datasheet electrical specs."""
    if technol not in _VALID_TECHNOLS:
        raise ValueError(f"technol must be one of: {', '.join(_VALID_TECHNOLS)}")
    pnom_w = _pos_finite(pnom_w, "PNom")
    isc_a = _pos_finite(isc_a, "ISC")
    voc_v = _pos_finite(voc_v, "Voc")
    imp_a = _pos_finite(imp_a, "Imp")
    vmp_v = _pos_finite(vmp_v, "Vmp")
    width_m = _pos_finite(width_m, "Width")
    height_m = _pos_finite(height_m, "Height")
    depth_m = _pos_finite(depth_m, "Depth")
    weight_kg = _pos_finite(weight_kg, "Weight")
    vmax_iec_v = _pos_finite(vmax_iec_v, "VMaxIEC")
    vmax_ul_v = _pos_finite(vmax_ul_v, "VMaxUL")
    if type(n_cells_s) is not int or not 1 <= n_cells_s <= 500:
        raise ValueError("NCelS must be an integer in 1..500")
    if type(n_cells_p) is not int or not 1 <= n_cells_p <= 16:
        raise ValueError("NCelP must be an integer in 1..16")
    if type(n_diode) is not int or not 1 <= n_diode <= 12:
        raise ValueError("NDiode must be an integer in 1..12")

    if bifaciality_factor is not None:
        bifaciality_factor = _pos_finite(bifaciality_factor, "BifacialityFactor", allow_zero=True)
        if bifaciality_factor > 1.0:
            raise ValueError("BifacialityFactor must be in [0, 1]")

    # Default temperature coefficients from typical c-Si physics if omitted
    eff_mu_isc = mu_isc_ma_per_c if mu_isc_ma_per_c is not None else round(isc_a * 0.00045 * 1000.0, 2)
    eff_mu_voc = mu_voc_mv_per_c if mu_voc_mv_per_c is not None else round(-0.0026 * voc_v * 1000.0, 2)
    eff_mu_pmp = float(mu_pmp_pct_per_c)
    if not math.isfinite(eff_mu_pmp) or not -1.0 <= eff_mu_pmp <= -0.05:
        raise ValueError("muPmpReq must be in [-1.0, -0.05] %/degC")

    diode = derive_single_diode_parameters(
        voc=voc_v, isc=isc_a, vmp=vmp_v, imp=imp_a, n_cells_s=n_cells_s,
    )
    is_bifi = bifaciality_factor is not None and bifaciality_factor > 0.0
    flags = "$00800000" if is_bifi else "$00"
    layout = "slTwinHalfCells" if n_cells_p >= 2 else "slInLength"

    lines = [
        *_component_commercial_header(
            "pvModule",
            manufacturer=manufacturer,
            model=model,
            source_note=source_note,
            flags=flags,
            width_m=width_m,
            height_m=height_m,
            depth_m=depth_m,
            weight_kg=weight_kg,
        ),
        f"  Technol={technol}",
        f"  NCelS={n_cells_s}",
        f"  NCelP={n_cells_p}",
        f"  NDiode={n_diode}",
        f"  SubModuleLayout={layout}",
        f"  PNom={pnom_w:.1f}",
        "  PNomTolLow=0.0",
        "  PNomTolUp=3.0",
    ]
    if is_bifi:
        lines.append(f"  BifacialityFactor={bifaciality_factor:.3f}")
    lines.extend([
        f"  LargApp={width_m:.3f}",
        f"  LongApp={height_m:.3f}",
        "  GRef=1000",
        "  TRef=25",
        "  Absorb=0.90",
        f"  ISC={isc_a:.3f}",
        f"  Voc={voc_v:.2f}",
        f"  Imp={imp_a:.3f}",
        f"  Vmp={vmp_v:.2f}",
        f"  MuISC={eff_mu_isc:.2f}",
        f"  muVocSpec={eff_mu_voc:.2f}",
        f"  muPmpReq={eff_mu_pmp:.3f}",
        f"  RShunt={diode['RShunt']:.2f}",
        f"  Rp_0={diode['Rp_0']:.1f}",
        f"  Rp_Exp={diode['Rp_Exp']:.2f}",
        f"  RSerie={diode['RSerie']:.4f}",
        f"  Gamma={diode['Gamma']:.4f}",
        f"  muGamma={diode['muGamma']:.4f}",
        f"  VMaxIEC={int(round(vmax_iec_v))}",
        f"  VMaxUL={int(round(vmax_ul_v))}",
        "End of PVObject pvModule",
    ])
    text = "\r\n".join(lines) + "\r\n"
    return text, {
        "single_diode": diode,
        "bifacial": is_bifi,
        "flags": flags,
        "mu_isc_ma_per_c": eff_mu_isc,
        "mu_voc_mv_per_c": eff_mu_voc,
        "mu_pmp_pct_per_c": eff_mu_pmp,
    }


def _build_ond_curve_points(
    *,
    pnom_w: float,
    p_seuil: float,
    effic_max_pct: float,
    effic_euro_pct: float,
) -> list[tuple[int, int]]:
    """Build a strictly monotonic 8-point Mode-1 P_out(P_in) efficiency profile in W.

    Scale the template floors proportionally when EfficMax < 98% so lower-efficiency inverters
    preserve a realistic light-load / peak / full-load curve instead of flattening to EfficMax.
    """
    eta_max = effic_max_pct / 100.0
    eta_delta = max(0.001, (effic_max_pct - effic_euro_pct) / 100.0)
    floor_scale = min(1.0, eta_max / 0.98)
    fracs_and_etas = tuple(
        (frac, min(eta_max, max(floor * floor_scale, eta_max - drop * eta_delta)))
        for frac, floor, drop in (
            (0.03, 0.85, 4.5),
            (0.05, 0.88, 3.0),
            (0.10, 0.92, 1.6),
            (0.20, 0.94, 0.6),
            (0.30, 0.95, 0.2),
            (0.50, 0.98, 0.0),
            (1.00, 0.94, 0.4),
        )
    )
    points: list[tuple[int, int]] = [(int(round(p_seuil)), 0)]
    p_in_max = pnom_w / fracs_and_etas[-1][1]
    for frac, eta in fracs_and_etas:
        p_in = max(points[-1][0] + 1, int(round(p_in_max * frac)))
        p_out_cap = min(p_in, int(math.floor(p_in * eta_max + 1e-9)))
        p_out = max(points[-1][1] + 1, min(p_out_cap, int(round(p_in * eta))))
        points.append((p_in, p_out))
    return points


def _emit_cubic_profile(name: str, points: list[tuple[float, float]] | list[list[float]], *, indent: str = "    ") -> list[str]:
    n_pts = len(points)
    n_max = max(11, n_pts)
    out = [
        f"{indent}{name}=TCubicProfile",
        f"{indent}  NPtsMax={n_max}",
        f"{indent}  NPtsEff={n_pts}",
        f"{indent}  LastCompile=$008D",
        f"{indent}  Mode=1",
    ]
    for idx, (xv, yv) in enumerate(points, start=1):
        out.append(f"{indent}  Point_{idx}={xv:.17g},{yv:.17g}")
    for idx in range(n_pts + 1, n_max + 1):
        out.append(f"{indent}  Point_{idx}=0,0")
    out.append(f"{indent}End of TCubicProfile")
    return out


def synthesize_ond_from_datasheet(
    *,
    manufacturer: str,
    model: str,
    pnom_ac_kw: float,
    pmax_ac_kw: float | None = None,
    vmpp_min_v: float,
    vmpp_max_v: float,
    vabs_max_v: float,
    effic_max_pct: float = 98.5,
    effic_euro_pct: float | None = None,
    vout_ac_v: float | None = None,
    nb_mppt: int = 2,
    nb_inputs: int | None = None,
    idc_max_a: float = 0.0,
    width_m: float = 0.70,
    height_m: float = 0.65,
    depth_m: float = 0.35,
    weight_kg: float = 75.0,
    three_voltage_voltages: tuple[float, float, float] | list[float] | None = None,
    three_voltage_effic_max: tuple[float, float, float] | list[float] | None = None,
    three_voltage_effic_euro: tuple[float, float, float] | list[float] | None = None,
    three_voltage_curves: dict[str, list[list[float]]] | None = None,
    source_note: str = "Zero-template datasheet synthesis",
) -> tuple[str, dict[str, Any]]:
    """Generate complete PVsyst 8.1.7 UTF-8 CRLF .OND text (single-voltage or 3-voltage Mode-1 ProfilPIO)."""
    pnom_ac_kw = _pos_finite(pnom_ac_kw, "PNomConv")
    pmax_ac_kw = _pos_finite(pmax_ac_kw, "PMaxOUT") if pmax_ac_kw is not None else pnom_ac_kw
    if pmax_ac_kw < pnom_ac_kw:
        raise ValueError("Require PMaxOUT >= PNomConv")
    vmpp_min_v = _pos_finite(vmpp_min_v, "VMppMin")
    vmpp_max_v = _pos_finite(vmpp_max_v, "VMPPMax")
    vabs_max_v = _pos_finite(vabs_max_v, "VAbsMax")
    if not (vmpp_min_v < vmpp_max_v <= vabs_max_v):
        raise ValueError("Require VMppMin < VMPPMax <= VAbsMax")
    effic_max_pct = _pos_finite(effic_max_pct, "EfficMax")
    if not 50.0 <= effic_max_pct <= 99.8:
        raise ValueError("EfficMax must be in [50.0, 99.8] %")
    eff_euro = _pos_finite(effic_euro_pct, "EfficEuro") if effic_euro_pct is not None else round(effic_max_pct - 0.3, 2)
    if eff_euro > effic_max_pct:
        raise ValueError("Require EfficEuro <= EfficMax")
    if type(nb_mppt) is not int or not 1 <= nb_mppt <= 64:
        raise ValueError("NbMPPT must be an integer in 1..64")
    eff_inputs = nb_inputs if nb_inputs is not None else nb_mppt * 2
    if type(eff_inputs) is not int or eff_inputs < nb_mppt or eff_inputs > 256:
        raise ValueError("NbInputs must be an integer >= NbMPPT and <= 256")
    idc_max_a = _pos_finite(idc_max_a, "IDCMax", allow_zero=True)
    v_out = _pos_finite(vout_ac_v, "VOutConv") if vout_ac_v is not None else (800.0 if pnom_ac_kw >= 100.0 else 400.0)

    is_3v = (
        three_voltage_voltages is not None
        or three_voltage_effic_max is not None
        or three_voltage_effic_euro is not None
        or three_voltage_curves is not None
    )
    v_tri: tuple[float, float, float] | None = None
    emax_tri: tuple[float, float, float] | None = None
    eeuro_tri: tuple[float, float, float] | None = None
    v_curves: dict[str, list[tuple[float, float]]] = {}

    pnom_w = pnom_ac_kw * 1000.0
    p_seuil = round(max(5.0, 0.005 * pnom_w), 2)
    points = _build_ond_curve_points(
        pnom_w=pnom_w, p_seuil=p_seuil, effic_max_pct=effic_max_pct, effic_euro_pct=eff_euro,
    )

    if is_3v:
        if three_voltage_voltages is None or len(three_voltage_voltages) != 3:
            raise ValueError("three_voltage_voltages must provide exactly 3 voltages [VMin, VNom, VMax]")
        v1, v2, v3 = (_pos_finite(v, f"VNomEff[{i}]") for i, v in enumerate(three_voltage_voltages))
        if not (v1 < v2 < v3):
            raise ValueError("three_voltage_voltages must be strictly increasing (VMin < VNom < VMax)")
        if v1 < vmpp_min_v or v3 > vabs_max_v:
            raise ValueError("three_voltage_voltages must lie within [VMppMin, VAbsMax]")
        v_tri = (v1, v2, v3)

        if three_voltage_effic_max is not None:
            if len(three_voltage_effic_max) != 3:
                raise ValueError("three_voltage_effic_max must contain 3 percentages")
            emax_tri = tuple(_pos_finite(e, f"EfficMaxV[{i}]") for i, e in enumerate(three_voltage_effic_max))  # type: ignore[assignment]
            if any(not 50.0 <= e <= 99.8 for e in emax_tri):
                raise ValueError("three_voltage_effic_max values must be in [50.0, 99.8] %")
        else:
            emax_tri = (
                round(max(50.0, effic_max_pct - 0.35), 2),
                round(effic_max_pct, 2),
                round(max(50.0, effic_max_pct - 0.45), 2),
            )

        if three_voltage_effic_euro is not None:
            if len(three_voltage_effic_euro) != 3:
                raise ValueError("three_voltage_effic_euro must contain 3 percentages")
            eeuro_tri = tuple(_pos_finite(e, f"EfficEuroV[{i}]") for i, e in enumerate(three_voltage_effic_euro))  # type: ignore[assignment]
            if any(eeuro_tri[i] > emax_tri[i] for i in range(3)):
                raise ValueError("Each EfficEuroV must be <= corresponding EfficMaxV")
        else:
            delta_nom = max(0.1, effic_max_pct - eff_euro)
            eeuro_tri = tuple(round(max(50.0, emax_tri[i] - delta_nom), 2) for i in range(3))  # type: ignore[assignment]

        for idx_v, tag in enumerate(("ProfilPIOV1", "ProfilPIOV2", "ProfilPIOV3")):
            if three_voltage_curves and tag in three_voltage_curves:
                pts_in = three_voltage_curves[tag]
                if not isinstance(pts_in, list) or not 4 <= len(pts_in) <= 11:
                    raise ValueError(f"{tag} requires 4..11 [p_in, p_out] points")
                parsed_pts: list[tuple[float, float]] = []
                for p_i, pt in enumerate(pts_in):
                    if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                        raise ValueError(f"{tag}[{p_i}] must be a [p_in, p_out] pair")
                    pin = _pos_finite(pt[0], f"{tag}[{p_i}][0]")
                    pout = _pos_finite(pt[1], f"{tag}[{p_i}][1]", allow_zero=True)
                    if pout > pin + 1e-6:
                        raise ValueError(f"{tag}[{p_i}] p_out ({pout}) cannot exceed p_in ({pin})")
                    if parsed_pts:
                        if pin <= parsed_pts[-1][0]:
                            raise ValueError(f"{tag}[{p_i}] p_in must be strictly increasing")
                        if pout < parsed_pts[-1][1]:
                            raise ValueError(f"{tag}[{p_i}] p_out must be non-decreasing")
                    parsed_pts.append((pin, pout))
                v_curves[tag] = parsed_pts
            else:
                v_curves[tag] = _build_ond_curve_points(
                    pnom_w=pnom_w, p_seuil=p_seuil,
                    effic_max_pct=emax_tri[idx_v], effic_euro_pct=eeuro_tri[idx_v],
                )
        # Keep main ProfilPIO synced with nominal V2 curve
        points = v_curves["ProfilPIOV2"]

    p_in_eff_max = float(points[min(6, len(points) - 1)][0])
    i_nom_ac = round(pnom_w / (math.sqrt(3.0) * v_out), 3)
    night_loss = round(max(1.0, 0.0001 * pnom_w), 2)
    # Bit 12 ($1000) enables 3-voltage efficiency curves; clear bit 4 ($0010) so file curves are used in 3V mode
    flags_str = "$1560" if is_3v else "$0570"

    lines = [
        *_component_commercial_header(
            "pvGInverter",
            manufacturer=manufacturer,
            model=model,
            source_note=source_note,
            flags=flags_str,
            width_m=width_m,
            height_m=height_m,
            depth_m=depth_m,
            weight_kg=weight_kg,
        ),
        "  Transfo=Without",
        "  Converter=TConverter",
        f"    PNomConv={pnom_ac_kw:.3f}",
        f"    PMaxOUT={pmax_ac_kw:.3f}",
        f"    VOutConv={v_out:.1f}",
        f"    VMppMin={vmpp_min_v:.1f}",
        f"    VMPPMax={vmpp_max_v:.1f}",
        f"    VAbsMax={vabs_max_v:.1f}",
        f"    PSeuil={p_seuil:.2f}",
        f"    EfficMax={effic_max_pct:.2f}",
        f"    EfficEuro={eff_euro:.2f}",
        "    FResNorm=3.00",
        "    ModeOper=MPPT",
        "    CompPMax=Lim",
        "    CompVMax=Lim",
        "    MonoTri=Tri",
        "    ModeAffEnum=Efficf_PIn",
        "    UnitAffEnum=kW",
        f"    IDCMax={idc_max_a:.3f}",
        f"    INomAC={i_nom_ac:.3f}",
        "    TPNom=50.0",
        "    TPMax=60.0",
        "    TPLim1=60.0",
        f"    PInEffMax={p_in_eff_max:.3f}",
        f"    PThreshEff={round(p_seuil * 0.5, 2):.2f}",
        "    HasdefaultPThresh=True",
    ]
    lines.extend(_emit_cubic_profile("ProfilPIO", points))
    if is_3v and v_tri is not None and emax_tri is not None and eeuro_tri is not None:
        lines.append(f"    VNomEff={v_tri[0]:.1f},{v_tri[1]:.1f},{v_tri[2]:.1f},")
        lines.append(f"    EfficMaxV={emax_tri[0]:.3f},{emax_tri[1]:.3f},{emax_tri[2]:.3f},")
        lines.append(f"    EfficEuroV={eeuro_tri[0]:.3f},{eeuro_tri[1]:.3f},{eeuro_tri[2]:.3f},")
        for tag in ("ProfilPIOV1", "ProfilPIOV2", "ProfilPIOV3"):
            lines.extend(_emit_cubic_profile(tag, v_curves[tag]))
    lines.extend([
        "  End of TConverter",
        f"  NbInputs={eff_inputs}",
        f"  NbMPPT={nb_mppt}",
        f"  NbMSInterne={nb_mppt}",
        "  MasterSlave=No_M_S",
        "  IsolSurvey=Yes",
        "  DC_Switch=Yes",
        "  AC_Switch=No",
        "  DiscAdjust=Yes",
        "  ENS_Protec=Yes",
        "  MS_Thresh=0.8",
        f"  Night_Loss={night_loss:.2f}",
        "End of PVObject pvGInverter",
    ])
    text = "\r\n".join(lines) + "\r\n"
    return text, {
        "nb_mppt": nb_mppt,
        "nb_inputs": eff_inputs,
        "vout_ac_v": v_out,
        "curve_points": len(points),
        "three_voltage": is_3v,
        "flags": flags_str,
        "v_nom_eff": list(v_tri) if v_tri else None,
        "effic_max_v": list(emax_tri) if emax_tri else None,
        "effic_euro_v": list(eeuro_tri) if eeuro_tri else None,
    }


_LI_CHEM_DEFAULTS = {
    "LFP": {
        "batt_technol": "Li_LFP",
        "v_nom_cell": 3.20,
        "v_half_soc": 3.30,
        "alpha_soc": 0.16,
        "v_max_charge_cell": 3.60,
        "v_min_discharge_cell": 2.80,
        "coulombic_eff": 0.985,
        "cycle_life_80dod": 6000,
        "lifetime_years_20c": 15.0,
        "ir_drop_i10_mv": 16.0,
    },
    "NMC": {
        "batt_technol": "Li_NMC",
        "v_nom_cell": 3.65,
        "v_half_soc": 3.70,
        "alpha_soc": 0.70,
        "v_max_charge_cell": 4.15,
        "v_min_discharge_cell": 3.00,
        "coulombic_eff": 0.980,
        "cycle_life_80dod": 3500,
        "lifetime_years_20c": 12.0,
        "ir_drop_i10_mv": 18.0,
    },
}


def synthesize_btr_from_datasheet(
    *,
    manufacturer: str,
    model: str,
    chemistry: str = "LFP",
    capacity_ah: float | None = None,
    energy_kwh: float | None = None,
    nominal_voltage_v: float | None = None,
    n_cells_s: int | None = None,
    n_cells_p: int = 1,
    internal_resistance_mohm: float | None = None,
    coulombic_efficiency: float | None = None,
    max_charge_current_a: float | None = None,
    max_discharge_current_a: float | None = None,
    cycle_life_80dod: int | None = None,
    soh_eol: float = 0.80,
    lifetime_years_20c: float | None = None,
    width_m: float = 0.60,
    height_m: float = 0.25,
    depth_m: float = 0.55,
    weight_kg: float = 85.0,
    source_note: str = "Zero-template datasheet synthesis",
) -> tuple[str, dict[str, Any]]:
    """Generate a complete PVsyst 8.1.7 UTF-8 CRLF Li-ion (LFP/NMC) .BTR file directly from datasheet values."""
    chem_key = str(chemistry or "LFP").strip().upper()
    if chem_key not in _LI_CHEM_DEFAULTS:
        raise ValueError("chemistry must be 'LFP' or 'NMC'")
    chem = _LI_CHEM_DEFAULTS[chem_key]
    v_cell = chem["v_nom_cell"]

    if type(n_cells_p) is not int or not 1 <= n_cells_p <= 200:
        raise ValueError("NCellParall must be an integer in 1..200")

    if n_cells_s is not None:
        if type(n_cells_s) is not int or not 1 <= n_cells_s <= 500:
            raise ValueError("NCellSerie must be an integer in 1..500")
        eff_ns = n_cells_s
        eff_vnom = round(eff_ns * v_cell, 2) if nominal_voltage_v is None else _pos_finite(nominal_voltage_v, "VNom")
    elif nominal_voltage_v is not None:
        eff_vnom = _pos_finite(nominal_voltage_v, "VNom")
        eff_ns = max(1, int(round(eff_vnom / v_cell)))
    else:
        eff_ns = 16 if chem_key == "LFP" else 14
        eff_vnom = round(eff_ns * v_cell, 2)

    if capacity_ah is not None:
        cap_ah = _pos_finite(capacity_ah, "CapNomC10")
    elif energy_kwh is not None:
        e_kwh = _pos_finite(energy_kwh, "EnergyKWh")
        cap_ah = (e_kwh * 1000.0) / eff_vnom
    else:
        raise ValueError("Zero-template BTR synthesis requires capacity_ah (CapNomC10) or energy_kwh (EnergyKWh)")

    v_cell_eff = eff_vnom / eff_ns
    v_min_cell_chk = chem["v_min_discharge_cell"]
    v_max_cell_chk = chem["v_max_charge_cell"]
    if not (v_min_cell_chk < v_cell_eff < v_max_cell_chk):
        raise ValueError(
            f"Per-cell nominal voltage VNom/NCellSerie ({v_cell_eff:.3f} V) must lie strictly within "
            f"({v_min_cell_chk:.2f}, {v_max_cell_chk:.2f}) V for {chem_key}"
        )
    eff_energy_kwh = round((cap_ah * eff_vnom) / 1000.0, 3)

    if internal_resistance_mohm is not None:
        # PVsyst stores ResInterne per cell in Ohm and checks (CapNomC10/10)*ResInterne*1000 <= 40 mV for Li-ion
        res_ohm = _pos_finite(internal_resistance_mohm, "ResInterne") / 1000.0
        drop_mv = (cap_ah / 10.0) * res_ohm * 1000.0
        if drop_mv > 40.0:
            raise ValueError(
                f"Per-cell internal resistance {internal_resistance_mohm} mOhm produces {drop_mv:.1f} mV drop at I10, "
                "exceeding PVsyst Li-ion limit of 40 mV/cell (pass per-cell mOhm, e.g. 0.5..1.5 mOhm for 200Ah)"
            )
    else:
        # Match PVsyst TBattery.ValDefault: ~16 mV voltage drop per cell at I10 (C10 / 10)
        res_ohm = max(0.0001, round((chem["ir_drop_i10_mv"] / 1000.0) / max(0.1, cap_ah / 10.0), 5))

    eff_i = _pos_finite(coulombic_efficiency, "EfficI") if coulombic_efficiency is not None else chem["coulombic_eff"]
    if not 0.88 <= eff_i <= 0.99:
        raise ValueError("coulombic_efficiency (EfficI) must be in [0.88, 0.99] for Li-ion")
    i_max_ch = _pos_finite(max_charge_current_a, "IMaxCharge") if max_charge_current_a is not None else round(0.5 * cap_ah, 2)
    i_max_dis = _pos_finite(max_discharge_current_a, "IMaxDischarge") if max_discharge_current_a is not None else round(1.0 * cap_ah, 2)
    soh_end = _pos_finite(soh_eol, "SOHEOL")
    if not 0.5 <= soh_end <= 0.95:
        raise ValueError("SOHEOL must be in [0.50, 0.95]")
    life_yr = _pos_finite(lifetime_years_20c, "DureeVie20") if lifetime_years_20c is not None else chem["lifetime_years_20c"]
    if cycle_life_80dod is not None:
        if isinstance(cycle_life_80dod, bool) or not isinstance(cycle_life_80dod, int):
            raise ValueError("cycle_life_80dod must be an integer in 500..30000")
        cyc_80 = cycle_life_80dod
    else:
        cyc_80 = int(chem["cycle_life_80dod"])
    if not 500 <= cyc_80 <= 30000:
        raise ValueError("cycle_life_80dod must be in 500..30000")

    # Mode-1 Li-ion capacity vs discharge duration (h) relative to C10 (flat Li-ion Peukert behavior)
    capa_curve = [
        (0.5, 0.94),
        (1.0, 0.97),
        (2.0, 0.99),
        (5.0, 1.00),
        (10.0, 1.00),
        (20.0, 1.01),
        (100.0, 1.02),
    ]
    # Mode-1 temperature capacity derating curve (degC -> relative capacity)
    temp_curve = [
        (-10.0, 0.72),
        (0.0, 0.85),
        (10.0, 0.95),
        (20.0, 1.00),
        (25.0, 1.00),
        (45.0, 1.01),
    ]
    # Mode-1 cycling life vs DOD (%)
    cycle_curve = [
        (10.0, round(cyc_80 * 4.5, 0)),
        (20.0, round(cyc_80 * 2.8, 0)),
        (50.0, round(cyc_80 * 1.5, 0)),
        (80.0, float(cyc_80)),
        (90.0, round(cyc_80 * 0.82, 0)),
        (100.0, round(cyc_80 * 0.65, 0)),
    ]

    # PVsyst TBattery stores VMax_Charge and VMinDischarge per cell (checked against V_cell in VerifBattery)
    v_max_cell = round(chem["v_max_charge_cell"], 3)
    v_min_cell = round(chem["v_min_discharge_cell"], 3)
    v_half_cell = round(chem["v_half_soc"], 3)
    alpha_soc = round(chem["alpha_soc"], 3)

    lines = [
        *_component_commercial_header(
            "pvBattery",
            manufacturer=manufacturer,
            model=model,
            source_note=source_note,
            flags="$0000",
            width_m=width_m,
            height_m=height_m,
            depth_m=depth_m,
            weight_kg=weight_kg,
        ),
        "  BattModeAff=PackBatt",
        f"  BattTechnol={chem['batt_technol']}",
        "  Category=BatteryModule",
        f"  NCellSerie={eff_ns}",
        f"  NCellParall={n_cells_p}",
        "  NBlockSerie=1",
        "  NBlockParall=1",
        f"  CapNomC10={cap_ah:.2f}",
        f"  DataSheetCap={cap_ah:.2f}",
        "  DataShDishHours=10.0",
        "  PeukertCoeff=1.02",
        f"  VNomCell={v_cell_eff:.3f}",
        f"  VHalfSOC_TRef={v_half_cell:.3f}",
        f"  AlphaSOC={alpha_soc:.3f}",
        "  BattTempRef=25.0",
        "  BattCoeffTemp=-0.0005",
        f"  ResInterne={res_ohm:.5f}",
        f"  EfficI={eff_i:.4f}",
        "  SOCLD=0.10",
        "  DVDech=0.15",
        "  SOCLG=0.95",
        f"  VMax_Charge={v_max_cell:.3f}",
        f"  VMinDischarge={v_min_cell:.3f}",
        f"  IMaxCharge={i_max_ch:.2f}",
        f"  IMaxDischarge={i_max_dis:.2f}",
        "  TempMinCharge=0.0",
        "  TempMinDisch=-10.0",
        "  ActivE_ResInt=0.0",
        "  IAutoDech=0.001",
        f"  DureeVie20={life_yr:.1f}",
        f"  SOHEOL={soh_end:.2f}",
    ]
    lines.extend(_emit_cubic_profile("CapaCourant", capa_curve, indent="  "))
    lines.extend(_emit_cubic_profile("CapaTemper", temp_curve, indent="  "))
    lines.extend(_emit_cubic_profile("NbCycleMax", cycle_curve, indent="  "))
    lines.append("End of PVObject pvBattery")

    text = "\r\n".join(lines) + "\r\n"
    return text, {
        "chemistry": chem_key,
        "batt_technol": chem["batt_technol"],
        "n_cells_s": eff_ns,
        "n_cells_p": n_cells_p,
        "nominal_voltage_v": round(eff_vnom, 2),
        "capacity_ah": round(cap_ah, 2),
        "energy_kwh": eff_energy_kwh,
        "internal_resistance_ohm": round(res_ohm, 5),
        "coulombic_efficiency": eff_i,
        "max_charge_current_a": i_max_ch,
        "max_discharge_current_a": i_max_dis,
        "cycle_life_80dod": cyc_80,
    }

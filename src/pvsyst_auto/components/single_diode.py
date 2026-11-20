"""Zero-template datasheet synthesizer for PVsyst 8.1.7 .PAN (single-diode) and .OND files.

Derives physically consistent single-diode parameters (RShunt, Rp_0, RSerie, Gamma, muGamma)
directly from commercial module STC & temperature-coefficient datasheet values, and builds
valid Mode-1 efficiency curves (ProfilPIO) for string/central inverters.
"""
from __future__ import annotations

import math
from typing import Any

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


def _pos_finite(value: Any, label: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    num = float(value)
    if not math.isfinite(num) or (num < 0 if allow_zero else num <= 0):
        raise ValueError(f"{label} must be {'non-negative' if allow_zero else 'positive'} and finite")
    return num


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

    if best is None:
        # Fallback for unusual thin-film or ultra-high-FF specs: pick conservative physically valid defaults
        gamma = 1.05
        rs = max(0.05, min(0.5, 0.25 * (voc - vmp) / imp))
        a_mod = gamma * vt_mod
        i_0 = (isc - voc / r_sh) / max(math.exp(min(voc / a_mod, 700.0)) - 1.0, 1.0)
        dp_dv = 0.0
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
        "PVObject_=pvModule",
        f"  Comment={manufacturer}\t{model}\t{source_note}",
        "  Version=8.1.7",
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
    source_note: str = "Zero-template datasheet synthesis",
) -> tuple[str, dict[str, Any]]:
    """Generate complete PVsyst 8.1.7 UTF-8 CRLF .OND text with Mode-1 ProfilPIO curve."""
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

    pnom_w = pnom_ac_kw * 1000.0
    p_seuil = round(max(5.0, 0.005 * pnom_w), 2)
    eta_max = effic_max_pct / 100.0
    eta_delta = max(0.001, (effic_max_pct - eff_euro) / 100.0)
    # Construct strictly monotonic 8-point Mode-1 P_out(P_in) efficiency profile in W, capped at EfficMax
    fracs_and_etas = tuple((frac, min(eta, eta_max)) for frac, eta in (
        (0.03, max(0.85, eta_max - 4.5 * eta_delta)),
        (0.05, max(0.88, eta_max - 3.0 * eta_delta)),
        (0.10, max(0.92, eta_max - 1.6 * eta_delta)),
        (0.20, max(0.94, eta_max - 0.6 * eta_delta)),
        (0.30, max(0.95, eta_max - 0.2 * eta_delta)),
        (0.50, eta_max),
        (1.00, max(0.94, eta_max - 0.4 * eta_delta)),
    ))
    points: list[tuple[int, int]] = [(int(round(p_seuil)), 0)]
    p_in_max = pnom_w / fracs_and_etas[-1][1]
    for frac, eta in fracs_and_etas:
        p_in = max(points[-1][0] + 1, int(round(p_in_max * frac)))
        p_out = max(points[-1][1] + 1, min(p_in, int(round(p_in * eta))))
        points.append((p_in, p_out))
    p_in_eff_max = float(points[6][0])  # 50% load point
    i_nom_ac = round(pnom_w / (math.sqrt(3.0) * v_out), 3)
    night_loss = round(max(1.0, 0.0001 * pnom_w), 2)

    lines = [
        "PVObject_=pvGInverter",
        f"  Comment={manufacturer}\t{model}\t{source_note}",
        "  Version=8.1.7",
        "  Flags=$0570",
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
        "    ProfilPIO=TCubicProfile",
        "      NPtsMax=11",
        f"      NPtsEff={len(points)}",
        "      LastCompile=$008D",
        "      Mode=1",
    ]
    for idx, (pin, pout) in enumerate(points, start=1):
        lines.append(f"      Point_{idx}={pin},{pout}")
    for idx in range(len(points) + 1, 12):
        lines.append(f"      Point_{idx}=0,0")
    lines.extend([
        "    End of TCubicProfile",
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
    }

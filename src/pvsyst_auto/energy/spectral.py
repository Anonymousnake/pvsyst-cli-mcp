"""Native PVsyst 8.1.7 spectral correction (First Solar / precipitable water) reader and writer."""
from __future__ import annotations

import math
from typing import Any

from pvsyst_auto.components.editor import objects, replace_value

COEFF_SETS = ("SiMono", "SiPoly", "CdTe", "CIS", "aSi", "Custom")


def spectral_state(lines: list[str], main: tuple[int, int] | None) -> dict[str, Any]:
    """Read current UseSpectralCorr / SpectralCorrection settings from pvMainArray."""
    if main is None:
        return {"configured": False, "enabled": False}
    start, end = main
    enabled = False
    use_default = True
    coeff_set = None
    use_rel_hum = None
    in_block = False
    for idx in range(start + 1, end):
        s = lines[idx].strip()
        if s == "SpectralCorrection=Start":
            in_block = True
            continue
        if s == "SpectralCorrection=End":
            in_block = False
            continue
        if in_block:
            if s.startswith("CoeffSet="):
                coeff_set = s.split("=", 1)[1].strip()
            elif s.startswith("UseRelHum="):
                use_rel_hum = s.split("=", 1)[1].strip() == "True"
        else:
            if s.startswith("UseSpectralCorr="):
                enabled = s.split("=", 1)[1].strip() == "True"
            elif s.startswith("UseSpctCrrDefCoeffs="):
                use_default = s.split("=", 1)[1].strip() == "True"
    return {
        "configured": enabled or coeff_set is not None,
        "enabled": enabled,
        "use_default_coeffs": use_default,
        "coeff_set": coeff_set,
        "use_rel_hum": use_rel_hum,
    }


def replace_spectral_correction(lines: list[str], main: tuple[int, int] | None, updates: dict[str, Any]) -> dict[str, Any]:
    """Enable, update, or disable native UseSpectralCorr + SpectralCorrection block in pvMainArray."""
    if not isinstance(updates, dict) or "enabled" not in updates:
        raise ValueError("spectral_updates requires an explicit boolean 'enabled'")
    allowed = {"enabled", "coeff_set", "use_default_coeffs", "use_rel_hum", "coefficients"}
    if not set(updates) <= allowed:
        raise ValueError("Unknown field in spectral_updates: " + ", ".join(sorted(set(updates) - allowed)))
    enabled = updates["enabled"]
    if type(enabled) is not bool:
        raise ValueError("spectral_updates.enabled must be a strict boolean")
    coeff_set = str(updates.get("coeff_set") or "SiMono")
    if coeff_set not in COEFF_SETS:
        raise ValueError(f"spectral_updates.coeff_set must be one of {COEFF_SETS}")
    use_default = updates.get("use_default_coeffs", coeff_set != "Custom")
    if type(use_default) is not bool:
        raise ValueError("spectral_updates.use_default_coeffs must be a strict boolean")
    use_rel_hum = updates.get("use_rel_hum", True)
    if type(use_rel_hum) is not bool:
        raise ValueError("spectral_updates.use_rel_hum must be a strict boolean")
    coeffs = updates.get("coefficients")
    if coeff_set == "Custom":
        if not isinstance(coeffs, list) or len(coeffs) != 6 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in coeffs
        ):
            raise ValueError("spectral_updates.coefficients must be 6 finite numbers when coeff_set='Custom'")
        use_default = False
    elif coeffs is not None:
        raise ValueError("spectral_updates.coefficients is only accepted when coeff_set='Custom'")

    if main is None:
        raise ValueError("Spectral correction requires a pvMainArray section")
    start, end = main
    before = spectral_state(lines, main)
    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"

    # Find pvCircuit anchor inside pvMainArray (spectral block sits immediately before pvCircuit)
    circ_idx = next(
        (i for i in range(start + 1, end) if lines[i].strip() == "PVObject_SystemCircuit=pvCircuit"),
        end,
    )
    kept: list[str] = []
    in_spec_block = False
    for idx in range(start, circ_idx):
        s = lines[idx].strip()
        if s == "SpectralCorrection=Start":
            in_spec_block = True
            continue
        if in_spec_block:
            if s == "SpectralCorrection=End":
                in_spec_block = False
            continue
        if s.startswith("UseSpectralCorr=") or s.startswith("UseSpctCrrDefCoeffs="):
            continue
        kept.append(lines[idx])

    new_spec: list[str] = [f"  UseSpectralCorr={'True' if enabled else 'False'}{eol}"]
    if enabled:
        new_spec.append(f"  UseSpctCrrDefCoeffs={'True' if use_default else 'False'}{eol}")
        new_spec.append(f"  SpectralCorrection=Start{eol}")
        new_spec.append(f"    CoeffSet={coeff_set}{eol}")
        if coeff_set == "Custom" and coeffs is not None:
            new_spec.append("    Coefficients=" + ",".join(f"{float(v):.6f}" for v in coeffs) + f",{eol}")
        new_spec.append(f"    UseRelHum={'True' if use_rel_hum else 'False'}{eol}")
        new_spec.append(f"  SpectralCorrection=End{eol}")

    candidate = list(lines[:start]) + kept + new_spec + list(lines[circ_idx:])
    delta = len(candidate) - len(lines)
    after = spectral_state(candidate, (start, end + delta))
    lines[:] = candidate
    return {"before": before, "after": after}

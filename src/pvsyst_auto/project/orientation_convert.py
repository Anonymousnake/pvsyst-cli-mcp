"""In-place cross-orientation conversion for existing Grid variants (Fixed <-> Seasonal <-> Sheds <-> Tracker <-> Dome)."""
from __future__ import annotations

import math
import re
from typing import Any

from pvsyst_auto.components.editor import objects, replace_value
from pvsyst_auto.native.headless import strip_result_cache
from pvsyst_auto.project.variant_writer import run_new_variant_transaction

TARGET_TYPES = {
    "fixed": "FixedPlane",
    "seasonal": "SeasonalTilt",
    "sheds": "Sheds",
    "tracker_ns": "UnlimTracking",
    "tracker_2axis": "TrackTwoAxis",
    "dome": "Dome",
}


def _finite(val: Any, name: str, lo: float, hi: float) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
        raise ValueError(f"{name} must be a finite number")
    out = float(val)
    if not lo <= out <= hi:
        raise ValueError(f"{name} must lie in [{lo}, {hi}]")
    return out


def _strip_block(lines: list[str], start_marker: str, end_marker: str) -> tuple[list[str], bool]:
    out: list[str] = []
    inside = False
    removed = False
    for ln in lines:
        s = ln.strip()
        if not inside and s == start_marker:
            inside = True
            removed = True
            continue
        if inside:
            if s == end_marker:
                inside = False
            continue
        out.append(ln)
    return out, removed


def _build_orientation_block(
    target_type: str,
    params: dict[str, Any],
    *,
    eol: str,
    version: str = "8.1.7",
) -> tuple[list[str], dict[str, Any]]:
    ft = TARGET_TYPES[target_type]
    if target_type == "fixed":
        tilt = _finite(params.get("tilt_deg", 25.0), "tilt_deg", 0.0, 90.0)
        azim = _finite(params.get("azimuth_deg", 0.0), "azimuth_deg", -180.0, 180.0)
        slope = _finite(params.get("base_slope_deg", 0.0), "base_slope_deg", -45.0, 45.0)
        comment = f"Fixed, Tilt {tilt:.1f}°, Azim. {azim:.1f}°"
        body = [
            f"    FieldType={ft}",
            f"    FieldTilt={tilt:.1f}",
            f"    FieldAzim={azim:.1f}",
            f"    BaseSlope={slope:.1f}",
            "    UsesTiltAver=False",
            "    UsesAzimAver=False",
            "    UsesSlopeAver=False",
            "    BTNoTrackers=-1,-1,",
        ]
        meta = {"field_type": ft, "tilt_deg": tilt, "azimuth_deg": azim, "base_slope_deg": slope}

    elif target_type == "seasonal":
        s_tilt = _finite(params.get("summer_tilt_deg", 15.0), "summer_tilt_deg", 0.0, 90.0)
        w_tilt = _finite(params.get("winter_tilt_deg", 45.0), "winter_tilt_deg", 0.0, 90.0)
        azim = _finite(params.get("azimuth_deg", 0.0), "azimuth_deg", -180.0, 180.0)
        w_months = params.get("winter_months", [1, 2, 3, 10, 11, 12])
        if not isinstance(w_months, list) or not w_months or any(type(m) is not int or not 1 <= m <= 12 for m in w_months):
            raise ValueError("winter_months must be a non-empty list of month integers in 1..12")
        mask = 0
        for m in set(w_months):
            mask |= 1 << (m - 1)
        comment = f"Seasonal, Summer {s_tilt:.1f}° / Winter {w_tilt:.1f}°, Azim. {azim:.1f}°"
        body = [
            f"    FieldType={ft}",
            "    FieldTilt=0.0",
            f"    FieldAzim={azim:.1f}",
            f"    SummerTilt={s_tilt:.1f}",
            f"    WinterTilt={w_tilt:.1f}",
            f"    WinterMonths=${mask:04X}",
            "    UsesTiltAver=False",
            "    UsesAzimAver=False",
            "    UsesSlopeAver=False",
            "    BTNoTrackers=-1,-1,",
        ]
        meta = {"field_type": ft, "summer_tilt_deg": s_tilt, "winter_tilt_deg": w_tilt, "azimuth_deg": azim, "winter_months": sorted(set(w_months))}

    elif target_type == "sheds":
        tilt = _finite(params.get("tilt_deg", 20.0), "tilt_deg", 0.0, 90.0)
        azim = _finite(params.get("azimuth_deg", 0.0), "azimuth_deg", -180.0, 180.0)
        pitch = _finite(params.get("pitch_m", 10.0), "pitch_m", 0.5, 200.0)
        width = _finite(params.get("collector_width_m", 4.5), "collector_width_m", 0.2, pitch - 0.05)
        n_sheds = params.get("shed_count", 10)
        if type(n_sheds) is not int or not 1 <= n_sheds <= 1000:
            raise ValueError("shed_count must be an integer in 1..1000")
        comment = f"Unlimited Sheds, Tilt {tilt:.1f}°, Pitch {pitch:.1f}m"
        body = [
            f"    FieldType={ft}",
            f"    FieldTilt={tilt:.1f}",
            f"    FieldAzim={azim:.1f}",
            f"    Pitch={pitch:.3f}",
            f"    CollectorWidth={width:.3f}",
            "    InactiveBandWidth1=0.020",
            "    InactiveBandWidth2=0.020",
            f"    NSheds={n_sheds}",
            "    UsesElectricalEffect=False",
            "    FracShadingFactor=1.000",
            "    NModInWidth=4",
            "    CellWidth=0.156",
            "    UsesTiltAver=False",
            "    UsesAzimAver=False",
            "    UsesSlopeAver=False",
            "    BTNoTrackers=-1,-1,",
        ]
        meta = {"field_type": ft, "tilt_deg": tilt, "azimuth_deg": azim, "pitch_m": pitch, "collector_width_m": width, "shed_count": n_sheds}

    elif target_type == "tracker_ns":
        axis_az = _finite(params.get("axis_azimuth_deg", 0.0), "axis_azimuth_deg", -180.0, 180.0)
        min_phi = _finite(params.get("min_phi_deg", -60.0), "min_phi_deg", -90.0, 0.0)
        max_phi = _finite(params.get("max_phi_deg", 60.0), "max_phi_deg", 0.0, 90.0)
        if min_phi >= max_phi:
            raise ValueError("min_phi_deg must be < max_phi_deg")
        pitch = _finite(params.get("pitch_m", 6.0), "pitch_m", 0.5, 200.0)
        width = _finite(params.get("collector_width_m", 2.3), "collector_width_m", 0.2, pitch - 0.05)
        bt = params.get("backtracking", True)
        if type(bt) is not bool:
            raise ValueError("backtracking must be a strict boolean")
        comment = f"Horizontal N-S Axis Tracker, Phi {min_phi:.0f}°..{max_phi:.0f}°"
        body = [
            f"    FieldType={ft}",
            f"    AxisAzim={axis_az:.1f}",
            f"    MinPhi={min_phi:.1f}",
            f"    MaxPhi={max_phi:.1f}",
            f"    UsesBacktracking={'True' if bt else 'False'}",
            "    UsesIrradOptimiz=False",
            f"    Pitch={pitch:.3f}",
            f"    CollectorWidth={width:.3f}",
            "    InactiveBandWidth1=0.020",
            "    InactiveBandWidth2=0.020",
            "    NSheds=10",
            "    UsesElectricalEffect=False",
            "    FracShadingFactor=1.000",
            "    NModInWidth=2",
            "    CellWidth=0.156",
            "    UsesTiltAver=False",
            "    UsesAzimAver=False",
            "    UsesSlopeAver=False",
            "    BTNoTrackers=-1,-1,",
        ]
        meta = {"field_type": ft, "axis_azimuth_deg": axis_az, "min_phi_deg": min_phi, "max_phi_deg": max_phi,
                "pitch_m": pitch, "collector_width_m": width, "backtracking": bt}

    elif target_type == "tracker_2axis":
        axis_az = _finite(params.get("axis_azimuth_deg", 0.0), "axis_azimuth_deg", -180.0, 180.0)
        min_phi = _finite(params.get("min_phi_deg", -120.0), "min_phi_deg", -180.0, 0.0)
        max_phi = _finite(params.get("max_phi_deg", 120.0), "max_phi_deg", 0.0, 180.0)
        min_tilt = _finite(params.get("min_tilt_deg", 10.0), "min_tilt_deg", 0.0, 85.0)
        max_tilt = _finite(params.get("max_tilt_deg", 80.0), "max_tilt_deg", min_tilt + 1.0, 90.0)
        comment = f"Two-Axis Tracker, Tilt {min_tilt:.0f}°..{max_tilt:.0f}°, Azim {min_phi:.0f}°..{max_phi:.0f}°"
        body = [
            f"    FieldType={ft}",
            f"    AxisAzim={axis_az:.1f}",
            f"    MinAzim={min_phi:.1f}",
            f"    MaxAzim={max_phi:.1f}",
            f"    MinTilt={min_tilt:.1f}",
            f"    MaxTilt={max_tilt:.1f}",
            "    UsesTiltAver=False",
            "    UsesAzimAver=False",
            "    UsesSlopeAver=False",
            "    BTNoTrackers=-1,-1,",
        ]
        meta = {"field_type": ft, "axis_azimuth_deg": axis_az, "min_tilt_deg": min_tilt, "max_tilt_deg": max_tilt,
                "min_phi_deg": min_phi, "max_phi_deg": max_phi}

    else:  # dome (East-West dual-tilt dome -> 2 opposite FixedPlane orientations)
        tilt = _finite(params.get("tilt_deg", 10.0), "tilt_deg", 1.0, 45.0)
        azim = _finite(params.get("azimuth_deg", -90.0), "azimuth_deg", -180.0, 180.0)
        azim2 = azim + 180.0 if azim <= 0.0 else azim - 180.0
        comment = f"Dome {tilt:.1f}° ({azim:.0f}° / {azim2:.0f}°)"
        orient1 = [
            f"    PVObject_=pvOrient{eol}",
            f"      Comment={comment} Face 1{eol}",
            f"      Version={version}{eol}",
            f"      Flags=$00{eol}",
            f"    NoOrient=1{eol}",
            f"    FieldType=FixedPlane{eol}",
            f"    FieldTilt={tilt:.1f}{eol}",
            f"    FieldAzim={azim:.1f}{eol}",
            f"    BaseSlope=0.0{eol}",
            f"    UsesTiltAver=False{eol}",
            f"    UsesAzimAver=False{eol}",
            f"    UsesSlopeAver=False{eol}",
            f"    BTNoTrackers=-1,-1,{eol}",
            f"    End of TOrientGroup{eol}",
        ]
        orient2 = [
            f"    PVObject_=pvOrient{eol}",
            f"      Comment={comment} Face 2{eol}",
            f"      Version={version}{eol}",
            f"      Flags=$00{eol}",
            f"    NoOrient=2{eol}",
            f"    FieldType=FixedPlane{eol}",
            f"    FieldTilt={tilt:.1f}{eol}",
            f"    FieldAzim={azim2:.1f}{eol}",
            f"    BaseSlope=0.0{eol}",
            f"    UsesTiltAver=False{eol}",
            f"    UsesAzimAver=False{eol}",
            f"    UsesSlopeAver=False{eol}",
            f"    BTNoTrackers=-1,-1,{eol}",
            f"    End of TOrientGroup{eol}",
        ]
        header = [f"  Orientations, list of=2 TOrientGroup{eol}", eol]
        footer = [f"  End of Orientations{eol}"]
        return header + orient1 + [eol] + orient2 + footer, {
            "field_type": "Dome", "tilt_deg": tilt, "azimuth_face1_deg": azim, "azimuth_face2_deg": azim2,
        }

    block = [
        f"  Orientations, list of=1 TOrientGroup{eol}",
        eol,
        f"    PVObject_=pvOrient{eol}",
        f"      Comment={comment}{eol}",
        f"      Version={version}{eol}",
        f"      Flags=$00{eol}",
        f"    NoOrient=1{eol}",
        *[ln + eol for ln in body],
        f"    End of TOrientGroup{eol}",
        f"  End of Orientations{eol}",
    ]
    return block, meta


def convert_orientation_lines(
    lines: list[str],
    *,
    target_type: str,
    params: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Replace the Orientations list of a variant with a new orientation type, stripping incompatible 3D caches."""
    if target_type not in TARGET_TYPES:
        raise ValueError(f"target_type must be one of {sorted(TARGET_TYPES)}")
    subarrays = [block for block in objects("".join(lines)) if block.object_type == "pvSubArray"]
    if target_type == "dome" or any("NoOrientation2" in block.fields for block in subarrays):
        raise ValueError("Conversions to or from paired dome orientations are not supported; dome faces need native pvDomeOrient pairing and string allocation")
    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"

    start = next((i for i, ln in enumerate(lines) if ln.strip().startswith("Orientations, list of=")), None)
    end = next(
        (i for i, ln in enumerate(lines) if ln.strip() in ("End of Orientations", "End of List Orientations")),
        None,
    )
    if start is None or end is None or start >= end:
        raise ValueError("Variant does not contain a complete Orientations list")

    # Record prior FieldType
    prior_types = [
        ln.strip().split("=", 1)[1]
        for ln in lines[start:end + 1]
        if ln.strip().startswith("FieldType=")
    ]
    new_block, meta = _build_orientation_block(target_type, params or {}, eol=eol)
    cand = list(lines[:start]) + new_block + list(lines[end + 1:])

    # Strip incompatible 3D shading / module_layout / VarList caches
    cand, removed_3d = _strip_block(cand, "PVObject_Ombrage=pvShading", "End of PVObject pvShading")
    cand, removed_ml = _strip_block(cand, "PVObject_ModuleLayout=pvModuleLayout", "End of PVObject pvModuleLayout")
    cand, _ = strip_result_cache(cand)

    # Clear root shading/module_layout flags if 3D scene was stripped
    if removed_3d or removed_ml:
        for idx in range(min(15, len(cand))):
            m = re.match(r"^(\s*Flags=)\$([0-9A-Fa-f]{1,8})(\r?\n?)$", cand[idx])
            if m:
                val = int(m.group(2), 16)
                # Clear near-shading (bit 6/10/11) and simulation-done bit 0/2 while keeping load/system bits
                val &= ~0x00000C41
                cand[idx] = f"{m.group(1)}${val:08X}{m.group(3)}"
                break

    # Ensure all subarrays point to NoOrientation=1 (unless Dome with 2+ subarrays)
    if target_type != "dome":
        for idx, ln in enumerate(cand):
            if ln.strip().startswith("NoOrientation="):
                cand[idx] = replace_value(ln, "1")

    return cand, {
        "previous_field_types": prior_types,
        "target_type": target_type,
        "converted": meta,
        "stripped_3d_shading": removed_3d,
        "stripped_module_layout": removed_ml,
    }


def convert_orientation(
    store,
    *,
    project: str,
    source_variant: str,
    new_variant: str,
    target_type: str,
    params: dict[str, Any] | None = None,
    dry_run: bool = True,
    variant_name: str | None = None,
    add_utf8_bom: bool = False,
) -> dict[str, Any]:
    """Create a new variant converting an existing variant's orientation type in-place."""
    def preflight():
        if target_type not in TARGET_TYPES:
            raise ValueError(f"target_type must be one of {sorted(TARGET_TYPES)}")

    def build(*, lines, **_):
        candidate_lines, summary = convert_orientation_lines(
            lines, target_type=target_type, params=params or {},
        )

        def verify(checked, _, res):
            orients = [b for b in objects("".join(checked)) if b.object_type == "pvOrient"]
            if not orients:
                raise ValueError("Converted variant lost its pvOrient block")
            res["orientation_conversion"] = summary

        return {"candidate_lines": candidate_lines, "verify": verify}

    return run_new_variant_transaction(
        store,
        project=project,
        source_variant=source_variant,
        new_variant=new_variant,
        dry_run=dry_run,
        add_utf8_bom=add_utf8_bom,
        variant_name=variant_name,
        preflight=preflight,
        build=build,
        source_changed_error="Source variant changed before orientation conversion",
        write_failure_warning="New variant may contain a partial artifact; original source is preserved.",
    )

"""Pure-text zero-template synthesizer for PVsyst 8.1.7 .PRJ and .VC0 files.

Generates complete, valid UTF-8 BOM + CRLF project and calculation-variant text
directly from structured site, meteo, component, orientation, subarray, and
circuit inputs without reading any DataRO _DEMO_* seed files.
"""
from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

from pvsyst_auto.components.store import FIELD
from pvsyst_auto.project.bifacial import render_bifacial_block
from pvsyst_auto.project.circuit import MAX_COUNT, inventory as grid_inventory
from pvsyst_auto.project.paths import safe_filename, workspace_file

SYNTH_VERSION = "8.1.7"
DEFAULT_PAN_NAME = "Generic_Mono_300W.PAN"
DEFAULT_OND_NAME = "Generic_7.5kW.OND"
DEFAULT_PROJECT_SITE = "TemplateSite.SIT"
DEFAULT_PROJECT_SITE_NAME = "TemplateSite"
DEFAULT_PROJECT_MET = "TemplateMeteo.MET"
_NORMAL_TOL = 1e-6

# Standard Fresnel AR IAM 9-point cubic profile used by PVsyst 8.1 grid variants.
_DEFAULT_IAM_LINES = (
    "      PVObject_IAM=pvIAM",
    "        Flags=$00",
    "        IAMMode=FresnelAR",
    "        N_Glass=1.526",
    "        N_EVA=1.490",
    "        N_ARCoating=1.290",
    "        DGlass=4.00",
    "        DArCoating=0.100",
    "        IAMProfile=TCubicProfile",
    "          NPtsMax=9",
    "          NPtsEff=9",
    "          LastCompile=$B18D",
    "          Mode=3",
    "          Point_1=0.0,1.00000",
    "          Point_2=30.0,0.99896",
    "          Point_3=50.0,0.98727",
    "          Point_4=60.0,0.96252",
    "          Point_5=70.0,0.89160",
    "          Point_6=75.0,0.81433",
    "          Point_7=80.0,0.67885",
    "          Point_8=85.0,0.43754",
    "          Point_9=90.0,0.00000",
    "        End of TCubicProfile",
    "      End of PVObject pvIAM",
)

_DEFAULT_WIRING_LINES = (
    "      WiringResistance=TWiringResistance",
    "        TypeLayout=1",
    "        TypeMetal=1",
    "        NGroupS=1",
    "        NGroupP=1",
    "        NChamp=1",
    "        Resistivity=18.800",
    "        TargetLoss=0.015",
    "        NoSect=1,1,1,",
    "      EndTags",
)

_DEFAULT_SHADING_LINES = (
    "  PVObject_Ombrage=pvShading",
    "    Comment=No shading scene defined",
    "    Flags=$00080000",
    "    Precision=1",
    "    FracOmbreMod=1.000",
    "    FracOmbreThin=0.400",
    "    GCROffset=1.0",
    "    DisplayedRef=refGeographical",
    "  End of PVObject pvShading",
)


def _pos_int(value, context: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_COUNT:
        raise ValueError(f"{context} must be a positive 32-bit integer")
    return value


def _finite(value, context: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{context} must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(f"{context} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{context} must be <= {maximum}")
    return number


def _fmt(value: float, decimals: int = 1) -> str:
    number = 0.0 if abs(value) < 1e-9 else float(value)
    return f"{number:.{decimals}f}"


def _clean_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ch in value for ch in ("\r", "\n", "\x00")):
        raise ValueError(f"{label} must be single-line non-empty text")
    return value.strip()


def _normalize_azimuth(degrees: float) -> float:
    """Normalize PVsyst azimuth into [-180, 180] degrees (0 = south, west positive)."""
    value = ((degrees + 180.0) % 360.0) - 180.0
    if abs(value + 180.0) < 1e-9:
        value = 180.0
    return 0.0 if abs(value) < 1e-9 else value


def placeholder_site_block(site_name: str = DEFAULT_PROJECT_SITE,
                           site_identity: str = DEFAULT_PROJECT_SITE_NAME) -> tuple[str, list[str]]:
    """Return minimal valid pvSite body lines used only before real weather is bound."""
    safe_filename(site_name, ".SIT")
    ident = _clean_text(site_identity, "site_identity")
    return ident, [
        f"Comment={ident};;",
        f"Version={SYNTH_VERSION}",
        "Flags=$0006083F",
        f"NomF={site_name}",
        f"Site={ident}",
        "Latitude=46.2400",
        "Longitude=6.1100",
        "Altitude=425",
        "TimeZone=1.0",
        "HLeg=True",
        "UnitsDisp=kWh_m2_m",
        "Albedo=0.20",
    ]


def load_site_block(site_path: Path) -> tuple[str, str, list[str]]:
    """Parse a workspace .SIT file into (nom_f, site_identity, body_lines)."""
    raw = site_path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"SIT file {site_path.name} must be UTF-8 text") from exc
    if "\x00" in text:
        raise ValueError(f"SIT file {site_path.name} contains NUL bytes")
    lines = [line.rstrip("\r\n") for line in text.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 3 or lines[0].strip() != "PVObject_=pvSite" or lines[-1].strip() != "End of PVObject pvSite":
        raise ValueError(f"SIT file {site_path.name} is not a valid pvSite object")
    body = [line.strip() for line in lines[1:-1] if line.strip()]
    if any(line.startswith(("PVObject_", "End of PVObject")) for line in body):
        raise ValueError(f"SIT file {site_path.name} must contain one non-nested pvSite object")
    nom_f = None
    site_identity = None
    updated_body = []
    for line in body:
        match = FIELD.fullmatch(line)
        if match:
            if match[1] == "NomF":
                nom_f = match[2]
                updated_body.append(f"NomF={site_path.name}")
                continue
            if match[1] == "Site":
                site_identity = match[2]
        updated_body.append(line)
    if nom_f is None:
        updated_body.insert(0, f"NomF={site_path.name}")
        nom_f = site_path.name
    if not site_identity:
        site_identity = site_path.stem
        updated_body.insert(1, f"Site={site_identity}")
    return site_path.name, site_identity, updated_body


def load_meteo_header(met_path: Path, site_identity: str, site_body: list[str]) -> tuple[str, list[str]]:
    """Build PVObject_MeteoSimul=pvMeteo block lines from a workspace .MET header."""
    raw = met_path.read_bytes()
    header_fields: list[tuple[str, str]] = []
    embedded_site: list[str] = []
    try:
        # Read only the header section before VarMeteo daily data rows.
        head_text = raw[:65536].decode("utf-8-sig", errors="strict")
        lines = [line.rstrip("\r\n") for line in head_text.splitlines()]
    except UnicodeDecodeError:
        lines = []

    if lines and lines[0].strip() == "PVObject_=pvMeteo":
        in_site = False
        for line in lines[1:]:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("VarMeteo, list of=") or stripped == "End of PVObject pvMeteo":
                break
            if stripped == "PVObject_SiteMet=pvSite":
                in_site = True
                continue
            if in_site:
                if stripped == "End of PVObject pvSite":
                    in_site = False
                    continue
                if not stripped.startswith(("PVObject_", "End of PVObject")):
                    embedded_site.append(stripped)
                continue
            match = FIELD.fullmatch(stripped)
            if match:
                key, value = match[1], match[2]
                if key not in ("Comment", "Version"):
                    header_fields.append((key, value))

    field_map = dict(header_fields)
    ordered_keys = [
        ("Flags", field_map.get("Flags", "$010084E1")),
        ("NomF", met_path.name),
        ("SiteM", site_identity),
    ]
    for key, value in header_fields:
        if key in ("Flags", "NomF", "SiteM"):
            continue
        ordered_keys.append((key, value))

    # Ensure embedded SiteMet carries consistent NomF/Site/coordinates from site_body.
    site_lines = list(site_body)
    if embedded_site and not any(line.startswith("Latitude=") for line in site_body):
        site_lines = embedded_site

    meteo_lines = ["  PVObject_MeteoSimul=pvMeteo"]
    for key, value in ordered_keys:
        meteo_lines.append(f"    {key}={value}")
    meteo_lines.append("")
    meteo_lines.append("    PVObject_SiteMet=pvSite")
    for line in site_lines:
        meteo_lines.append(f"      {line}")
    meteo_lines.append("    End of PVObject pvSite")
    meteo_lines.append("  End of PVObject pvMeteo")
    return met_path.name, meteo_lines


def synthesize_meteo_from_site(met_name: str, site_identity: str, site_body: list[str],
                               *, pas_accum: int = 60) -> list[str]:
    """Build PVObject_MeteoSimul=pvMeteo lines when no workspace .MET header is parsed yet."""
    safe_filename(met_name, ".MET")
    ident = _clean_text(site_identity, "site_identity")
    lines = [
        "  PVObject_MeteoSimul=pvMeteo",
        "    Flags=$010084E1",
        f"    NomF={met_name}",
        f"    SiteM={ident}",
        "    UsesPerez=True",
        "    IsSourceUt=False",
        "    HourShift=0",
        "    TimeShift=0",
        "    TimeShiftTotalMin=0",
        f"    PasAccum={pas_accum}",
        "",
        "    PVObject_SiteMet=pvSite",
    ]
    for line in site_body:
        lines.append(f"      {line}")
    lines.append("    End of PVObject pvSite")
    lines.append("  End of PVObject pvMeteo")
    return lines


def synthesize_site_block(site_name: str, site_identity: str, lat: float, lon: float, alt: float) -> list[str]:
    """Synthesize a standalone pvSite block."""
    safe_filename(site_name, ".SIT")
    ident = _clean_text(site_identity, "site_identity")
    return [
        "PVObject_=pvSite",
        f"  Comment={ident};;",
        f"  Version={SYNTH_VERSION}",
        "  Flags=$0006083F",
        f"  NomF={site_name}",
        f"  Site={ident}",
        f"  Latitude={_finite(lat, 'lat', minimum=-90.0, maximum=90.0):.4f}",
        f"  Longitude={_finite(lon, 'lon', minimum=-180.0, maximum=180.0):.4f}",
        f"  Altitude={int(round(_finite(alt, 'alt')))}",
        "  TimeZone=0.0",
        "  HLeg=False",
        "  UnitsDisp=kWh_m2_m",
        "  Albedo=0.20",
        "End of PVObject pvSite",
    ]


def synthesize_meteo_block(met_name: str, site_identity: str) -> list[str]:
    """Synthesize a minimal standalone pvMeteo block."""
    safe_filename(met_name, ".MET")
    ident = _clean_text(site_identity, "site_identity")
    return [
        "PVObject_=pvMeteo",
        "  Flags=$010084E1",
        f"  NomF={met_name}",
        f"  SiteM={ident}",
        "End of PVObject pvMeteo",
    ]


def synthesize_tracker_shading_object(id: int, comment: str, origin: tuple[float, float, float],
                                      inclination: float) -> list[str]:
    """Synthesize a tracker shading array tracking object."""
    return [
        f"      PVObject_{id}=pvShdArrayTracking",
        f"        Comment={comment}",
        f"        Version={SYNTH_VERSION}",
        "        Flags=$02208064",
        f"        NoObjCreated={1000 + id}",
        "        ColorTechn=$00FF0000",
        "        ColorSurf=$00FF9999",
        "        ColorBord=$00505050",
        f"        DOrigine={origin[0]:.6f},{origin[1]:.6f},{origin[2]:.6f}",
        f"        DInclin={inclination:.6f}",
        "        BaseSlope=0.000000",
        "        DAzimut=0.000000",
        "        Source=osPVsyst",
        "      NElemChamps=1",
        "      NoOrient=2",
        "      PanneauPVName=TemplateModule;1.000;2.000",
        "      ModSpacingX=0.020",
        "      ModSpacingY=0.020",
        "      NModInHeight=2",
        "      NModInWidth=25",
        "      ModuleOrient=moPortrait",
        "      DXModule=2.141",
        "      DYModule=26.780",
        "        LongShed=4.282000",
        "        LargShed=26.780000",
        "        Cadre=0.000,0.000,0.000,0.000,",
        "        TypeOrigine=2",
        "        PitchNS=0.000",
        "        PitchEW=0.000",
        "        AxisOrig=2",
        "      End of PVObject pvShdArrayTracking",
    ]


def synthesize_shed_shading_object(id: int, comment: str, origin: tuple[float, float, float],
                                   tilt: float) -> list[str]:
    """Synthesize a fixed-shed shading array object."""
    return [
        f"      PVObject_{id}=pvShdArray",
        f"        Comment={comment}",
        f"        Version={SYNTH_VERSION}",
        "        Flags=$02208064",
        f"        NoObjCreated={2000 + id}",
        "        ColorTechn=$00FF0000",
        "        ColorSurf=$00FF9999",
        "        ColorBord=$00505050",
        f"        DOrigine={origin[0]:.6f},{origin[1]:.6f},{origin[2]:.6f}",
        f"        DInclin={tilt:.6f}",
        "        BaseSlope=0.000000",
        "        DAzimut=0.000000",
        "        Source=osPVsyst",
        "      NElemChamps=1",
        "      NoOrient=2",
        "      PanneauPVName=TemplateModule;1.000;2.000",
        "      ModSpacingX=0.020",
        "      ModSpacingY=0.020",
        "      NModInHeight=2",
        "      NModInWidth=25",
        "      ModuleOrient=moPortrait",
        "      DXModule=2.141",
        "      DYModule=26.780",
        "        LongShed=4.282000",
        "        LargShed=26.780000",
        "        Cadre=0.000,0.000,0.000,0.000,",
        "        TypeOrigine=2",
        "        PitchNS=0.000",
        "        PitchEW=0.000",
        "        AxisOrig=2",
        "      End of PVObject pvShdArray",
    ]


def _normalize_orientation(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Each orientation specification must be an object")
    oid = _pos_int(item.get("id", 1), "orientation.id")
    raw_ft = item.get("field_type", "FixedPlane")
    # Map friendly aliases to native PVsyst 8.1 FieldType tokens.
    if raw_ft == "UnlimSheds":
        field_type = "Sheds"
    elif raw_ft == "RegularSheds":
        field_type = "FixedPlane"
    else:
        field_type = raw_ft
    if field_type not in ("FixedPlane", "Sheds", "UnlimTracking", "TrackHorizNSAxis"):
        raise ValueError(f"Unsupported orientation field_type: {raw_ft!r}")
    tilt = _finite(item.get("tilt_deg", 25.0), "orientation.tilt_deg", minimum=0.0, maximum=90.0)
    azim = _finite(item.get("azimuth_deg", 0.0), "orientation.azimuth_deg", minimum=-180.0, maximum=180.0)
    base_slope = _finite(item.get("base_slope_deg", 0.0), "orientation.base_slope_deg", minimum=-90.0, maximum=90.0)
    extra = dict(item.get("extra_fields") or {})

    default_comment = {
        "FixedPlane": f"Fixed, Tilt {_fmt(tilt)} deg, Azim. {_fmt(azim)} deg",
        "Sheds": f"Unlimited sheds, Tilt {_fmt(tilt)} deg, Azim. {_fmt(azim)} deg",
        "UnlimTracking": "Unlimited trackers, horizontal axis N-S",
        "TrackHorizNSAxis": "Tracking, horizontal axis N-S",
    }[field_type]

    spec = {
        "id": oid,
        "field_type": field_type,
        "tilt_deg": tilt,
        "azimuth_deg": azim,
        "base_slope_deg": base_slope,
        "comment": item.get("comment") or default_comment,
        "extra_fields": extra,
        "bifacial": item.get("bifacial"),
    }
    if field_type == "Sheds":
        spec.update({
            "pitch_m": _finite(item.get("pitch_m", 10.0), "orientation.pitch_m", minimum=0.1),
            "collector_width_m": _finite(item.get("collector_width_m", 4.28), "orientation.collector_width_m", minimum=0.1),
            "inactive_band_width_1_m": _finite(item.get("inactive_band_width_1_m", 0.02), "orientation.inactive_band_width_1_m", minimum=0.0),
            "inactive_band_width_2_m": _finite(item.get("inactive_band_width_2_m", 0.02), "orientation.inactive_band_width_2_m", minimum=0.0),
            "shed_count": _pos_int(item.get("shed_count", 10), "orientation.shed_count"),
            "electrical_effect": bool(item.get("electrical_effect", True)),
            "shading_factor": _finite(item.get("shading_factor", 1.0), "orientation.shading_factor", minimum=0.0, maximum=1.0),
            "module_columns": _pos_int(item.get("module_columns", 4), "orientation.module_columns"),
            "cell_width_m": _finite(item.get("cell_width_m", 0.156), "orientation.cell_width_m", minimum=0.01),
        })
    elif field_type in ("UnlimTracking", "TrackHorizNSAxis"):
        min_phi = _finite(item.get("min_phi_deg", -60.0), "orientation.min_phi_deg", minimum=-90.0, maximum=0.0)
        max_phi = _finite(item.get("max_phi_deg", 60.0), "orientation.max_phi_deg", minimum=0.0, maximum=90.0)
        if not min_phi < max_phi:
            raise ValueError("orientation.min_phi_deg must be less than max_phi_deg")
        spec.update({
            "axis_azimuth_deg": _finite(item.get("axis_azimuth_deg", 0.0), "orientation.axis_azimuth_deg", minimum=-180.0, maximum=180.0),
            "min_phi_deg": min_phi,
            "max_phi_deg": max_phi,
            "backtracking": bool(item.get("backtracking", True)),
            "irrad_optimiz": bool(item.get("irrad_optimiz", False)),
            "pitch_m": _finite(item.get("pitch_m", 12.0), "orientation.pitch_m", minimum=0.1),
            "collector_width_m": _finite(item.get("collector_width_m", 4.282), "orientation.collector_width_m", minimum=0.1),
            "inactive_band_width_1_m": _finite(item.get("inactive_band_width_1_m", 0.02 if field_type == "UnlimTracking" else 0.0), "orientation.inactive_band_width_1_m", minimum=0.0),
            "inactive_band_width_2_m": _finite(item.get("inactive_band_width_2_m", 0.02 if field_type == "UnlimTracking" else 0.0), "orientation.inactive_band_width_2_m", minimum=0.0),
        })
        if field_type == "UnlimTracking":
            spec.update({
                "shed_count": _pos_int(item.get("shed_count", 10), "orientation.shed_count"),
                "electrical_effect": bool(item.get("electrical_effect", False)),
                "shading_factor": _finite(item.get("shading_factor", 1.0), "orientation.shading_factor", minimum=0.0, maximum=1.0),
                "module_columns": _pos_int(item.get("module_columns", 3), "orientation.module_columns"),
                "cell_width_m": _finite(item.get("cell_width_m", 0.156), "orientation.cell_width_m", minimum=0.01),
            })
        else:
            spec.update({
                "central_gap_m": _finite(item.get("central_gap_m", 0.0), "orientation.central_gap_m", minimum=0.0),
                "pitch_slope_deg": _finite(item.get("pitch_slope_deg", 0.0), "orientation.pitch_slope_deg", minimum=-45.0, maximum=45.0),
                "wind_stow": bool(item.get("wind_stow", False)),
                "wind_stow_speed_ms": _finite(item.get("wind_stow_speed_ms", 12.0), "orientation.wind_stow_speed_ms", minimum=0.0),
                "wind_stow_position_deg": _finite(item.get("wind_stow_position_deg", 0.0), "orientation.wind_stow_position_deg", minimum=-90.0, maximum=90.0),
            })
    return spec


def synthesize_orientation_block(orient: dict, *, version: str = SYNTH_VERSION) -> list[str]:
    """Serialize one TOrientGroup / pvOrient record (including optional pvBiFacial)."""
    spec = _normalize_orientation(orient)
    ft = spec["field_type"]
    lines = [
        "    PVObject_=pvOrient",
        f"      Comment={_clean_text(spec['comment'], 'orientation.comment')}",
        f"      Version={version}",
        "      Flags=$00",
        f"    NoOrient={spec['id']}",
        f"    FieldType={ft}",
    ]
    if ft == "FixedPlane":
        lines.extend([
            f"    FieldTilt={_fmt(spec['tilt_deg'])}",
            f"    FieldAzim={_fmt(spec['azimuth_deg'])}",
            f"    BaseSlope={_fmt(spec['base_slope_deg'])}",
        ])
    elif ft == "Sheds":
        lines.extend([
            f"    FieldTilt={_fmt(spec['tilt_deg'])}",
            f"    FieldAzim={_fmt(spec['azimuth_deg'])}",
            f"    Pitch={spec['pitch_m']:.3f}",
            f"    CollectorWidth={spec['collector_width_m']:.3f}",
            f"    InactiveBandWidth1={spec['inactive_band_width_1_m']:.3f}",
            f"    InactiveBandWidth2={spec['inactive_band_width_2_m']:.3f}",
            f"    NSheds={spec['shed_count']}",
            f"    UsesElectricalEffect={'True' if spec['electrical_effect'] else 'False'}",
            f"    FracShadingFactor={spec['shading_factor']:.3f}",
            f"    NModInWidth={spec['module_columns']}",
            f"    CellWidth={spec['cell_width_m']:.3f}",
        ])
    elif ft == "UnlimTracking":
        lines.extend([
            f"    AxisAzim={_fmt(spec['axis_azimuth_deg'])}",
            f"    MinPhi={_fmt(spec['min_phi_deg'])}",
            f"    MaxPhi={_fmt(spec['max_phi_deg'])}",
            f"    UsesBacktracking={'True' if spec['backtracking'] else 'False'}",
            f"    UsesIrradOptimiz={'True' if spec['irrad_optimiz'] else 'False'}",
            f"    Pitch={spec['pitch_m']:.3f}",
            f"    CollectorWidth={spec['collector_width_m']:.3f}",
            f"    InactiveBandWidth1={spec['inactive_band_width_1_m']:.3f}",
            f"    InactiveBandWidth2={spec['inactive_band_width_2_m']:.3f}",
            f"    NSheds={spec['shed_count']}",
            f"    UsesElectricalEffect={'True' if spec['electrical_effect'] else 'False'}",
            f"    FracShadingFactor={spec['shading_factor']:.3f}",
            f"    NModInWidth={spec['module_columns']}",
            f"    CellWidth={spec['cell_width_m']:.3f}",
        ])
    elif ft == "TrackHorizNSAxis":
        lines.extend([
            f"    AxisAzim={_fmt(spec['axis_azimuth_deg'])}",
            f"    MinPhi={_fmt(spec['min_phi_deg'])}",
            f"    MaxPhi={_fmt(spec['max_phi_deg'])}",
            f"    CentralGap={spec['central_gap_m']:.2f}",
            f"    PitchSlope={_fmt(spec['pitch_slope_deg'])}",
            f"    UsesBacktracking={'True' if spec['backtracking'] else 'False'}",
            "    PartialSystemNoRefField=-1",
            "    PartialSystemNoSubField=1",
            "    PartialSystemMode=psmUserAll",
            "    PartialSystemInclObj=True",
            f"    UsesIrradOptimiz={'True' if spec['irrad_optimiz'] else 'False'}",
            f"    UsesWindStow={'True' if spec['wind_stow'] else 'False'}",
            f"    WindStowSpeed={spec['wind_stow_speed_ms']:.3f}",
            f"    WindStowPosition={spec['wind_stow_position_deg']:.3f}",
            f"    Pitch={spec['pitch_m']:.3f}",
            f"    CollectorWidth={spec['collector_width_m']:.3f}",
            f"    InactiveBandWidth1={spec['inactive_band_width_1_m']:.3f}",
            f"    InactiveBandWidth2={spec['inactive_band_width_2_m']:.3f}",
        ])

    lines.extend([
        "    UsesTiltAver=False",
        "    UsesAzimAver=False",
        "    UsesSlopeAver=False",
        "    BTNoTrackers=-1,-1,",
    ])
    for key, value in spec["extra_fields"].items():
        lines.append(f"    {key}={value}")
    if spec["bifacial"] is not None:
        lines.extend(render_bifacial_block(spec["bifacial"], field_type=ft, indent="    "))
    lines.append("    End of TOrientGroup")
    return lines


def _normalize_subarray(item: dict, *, default_pan: str, default_ond: str) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Each subarray specification must be an object")
    sid = _pos_int(item.get("subarray_id", 1), "subarray.subarray_id")
    oid = _pos_int(item.get("orientation_id", 1), "subarray.orientation_id")
    comment = _clean_text(item.get("comment") or (f"PV Array" if sid == 1 else f"Sub-array #{sid}"),
                          f"subarray {sid} comment")
    pan_name = _clean_text(item.get("pan_name") or default_pan, f"subarray {sid} pan_name")
    ond_name = _clean_text(item.get("ond_name") or default_ond, f"subarray {sid} ond_name")
    safe_filename(pan_name, ".PAN")
    safe_filename(ond_name, ".OND")
    n_mod_serie = _pos_int(item.get("n_mod_serie", 15), f"subarray {sid} n_mod_serie")
    n_string_ch = _pos_int(item.get("n_string_ch", 2), f"subarray {sid} n_string_ch")
    n_inverter = _pos_int(item.get("n_inverter", 1), f"subarray {sid} n_inverter")
    ps_raw = item.get("power_sharing_inverter_id", -1)
    if type(ps_raw) is not int or ps_raw < -1:
        raise ValueError(f"subarray {sid} power_sharing_inverter_id must be -1 or a non-negative integer")
    independent_mppt = item.get("independent_mppt")
    if independent_mppt is None:
        independent_mppt = (ps_raw >= 0) or (item.get("n_inv_mppt") is not None)
    if type(independent_mppt) is not bool:
        raise ValueError(f"subarray {sid} independent_mppt must be a strict boolean")
    if ps_raw >= 0 and not independent_mppt:
        raise ValueError(f"subarray {sid} power_sharing_inverter_id >= 0 requires independent_mppt=True")
    if independent_mppt:
        n_inv_mppt = _pos_int(item.get("n_inv_mppt", n_string_ch if n_string_ch % n_inverter == 0 else n_inverter),
                              f"subarray {sid} n_inv_mppt")
        if n_inv_mppt % n_inverter != 0:
            raise ValueError(f"subarray {sid}: n_inv_mppt ({n_inv_mppt}) must be divisible by n_inverter ({n_inverter})")
        if n_string_ch % n_inv_mppt != 0:
            raise ValueError(f"subarray {sid}: n_string_ch ({n_string_ch}) must be divisible by n_inv_mppt ({n_inv_mppt})")
    else:
        n_inv_mppt = _pos_int(item.get("n_inv_mppt", 1), f"subarray {sid} n_inv_mppt")
        if n_inv_mppt != 1:
            raise ValueError(f"subarray {sid}: ordinary mode (independent_mppt=False) requires n_inv_mppt=1")
        if n_string_ch % n_inverter != 0:
            raise ValueError(f"subarray {sid}: n_string_ch ({n_string_ch}) must be divisible by n_inverter ({n_inverter})")
    pnom_ac_in_mppt = _finite(item.get("pnom_ac_in_mppt", 7.5), f"subarray {sid} pnom_ac_in_mppt", minimum=0.0)
    lid_loss = _finite(item.get("lid_loss", 0.0), f"subarray {sid} lid_loss", minimum=0.0, maximum=100.0)
    pmism_mpp = _finite(item.get("pmism_mpp", 2.0), f"subarray {sid} pmism_mpp", minimum=0.0, maximum=100.0)
    pqual = _finite(item.get("module_quality_loss", 3.0), f"subarray {sid} module_quality_loss", minimum=-5.0, maximum=20.0)
    dc_loss_pct = _finite(item.get("dc_wiring_loss_pct", 1.5), f"subarray {sid} dc_wiring_loss_pct", minimum=0.0, maximum=100.0)
    dc_res_mohm = _finite(item.get("dc_wiring_resistance_mohm", 0.0), f"subarray {sid} dc_wiring_resistance_mohm", minimum=0.0)
    return {
        "subarray_id": sid,
        "orientation_id": oid,
        "comment": comment,
        "pan_name": pan_name,
        "ond_name": ond_name,
        "n_mod_serie": n_mod_serie,
        "n_string_ch": n_string_ch,
        "n_inverter": n_inverter,
        "n_inv_mppt": n_inv_mppt,
        "independent_mppt": independent_mppt,
        "power_sharing_inverter_id": ps_raw,
        "pnom_ac_in_mppt": pnom_ac_in_mppt,
        "lid_loss": lid_loss,
        "pmism_mpp": pmism_mpp,
        "module_quality_loss": pqual,
        "dc_wiring_loss_pct": dc_loss_pct,
        "dc_wiring_resistance_mohm": dc_res_mohm,
    }


def synthesize_circuit_and_subarrays(subarrays: list[dict], *,
                                     default_pan: str = DEFAULT_PAN_NAME,
                                     default_ond: str = DEFAULT_OND_NAME,
                                     version: str = SYNTH_VERSION) -> tuple[list[str], list[str], list[dict]]:
    """Build synchronized PVObject_SystemCircuit and SubArrays blocks."""
    if not isinstance(subarrays, list) or not subarrays:
        raise ValueError("At least one subarray specification is required")
    normalized = [_normalize_subarray(item, default_pan=default_pan, default_ond=default_ond) for item in subarrays]
    seen_ids = set()
    for sub in normalized:
        if sub["subarray_id"] in seen_ids:
            raise ValueError(f"Duplicate subarray_id {sub['subarray_id']}")
        seen_ids.add(sub["subarray_id"])

    # Validate PowerSharing groups.
    shared_groups: dict[int, list[dict]] = defaultdict(list)
    for sub in normalized:
        gid = sub["power_sharing_inverter_id"]
        if gid >= 0:
            shared_groups[gid].append(sub)
    for gid, members in shared_groups.items():
        if len(members) < 2:
            raise ValueError(f"PowerSharingInverterId={gid} requires at least two member subarrays")
        if len({m["ond_name"].casefold() for m in members}) != 1:
            raise ValueError(f"PowerSharingInverterId={gid} members must share the same OND inverter")
        if len({m["n_inverter"] for m in members}) != 1:
            raise ValueError(f"PowerSharingInverterId={gid} members must declare the same n_inverter")

    circuit_lines = [
        "  PVObject_SystemCircuit=pvCircuit",
        f"    Version={version}",
        "    Flags=$00",
        "    PaperOrient=poPortrait",
        "    ShowLegend=True",
        "    BaseNode=Start",
        "      InjectionPointNode Start;",
        "        NElements=1",
        "        Children=Start",
    ]

    emitted_groups: set[int] = set()
    for sub in normalized:
        gid = sub["power_sharing_inverter_id"]
        if gid >= 0:
            if gid in emitted_groups:
                continue
            emitted_groups.add(gid)
            members = shared_groups[gid]
            group_inv = members[0]["n_inverter"]
            shared_name = "; ".join(m["comment"] for m in members)
            circuit_lines.extend([
                "          InverterNode Start;",
                f"            NElements={group_inv}",
                "            SubArrayId=0",
                f"            SubArrayName={shared_name}",
                "            Children=Start",
            ])
            for m in members:
                mppt_per_inv = m["n_inv_mppt"] // group_inv
                str_per_mppt = m["n_string_ch"] // m["n_inv_mppt"]
                circuit_lines.extend([
                    "              MPPTNode Start;",
                    f"                NElements={mppt_per_inv}",
                    f"                SubArrayId={m['subarray_id']}",
                    f"                SubArrayName={m['comment']}",
                    "                Children=Start",
                    "                  StringNode Start;",
                    f"                    NElements={str_per_mppt}",
                    f"                    SubArrayId={m['subarray_id']}",
                    f"                    SubArrayName={m['comment']}",
                    "                  StringNode End;",
                    "                Children=End",
                    "              MPPTNode End;",
                ])
            circuit_lines.extend([
                "            Children=End",
                "          InverterNode End;",
            ])
        elif sub["independent_mppt"]:
            mppt_per_inv = sub["n_inv_mppt"] // sub["n_inverter"]
            str_per_mppt = sub["n_string_ch"] // sub["n_inv_mppt"]
            circuit_lines.extend([
                "          InverterNode Start;",
                f"            NElements={sub['n_inverter']}",
                f"            SubArrayId={sub['subarray_id']}",
                f"            SubArrayName={sub['comment']}",
                "            Children=Start",
                "              MPPTNode Start;",
                f"                NElements={mppt_per_inv}",
                f"                SubArrayId={sub['subarray_id']}",
                f"                SubArrayName={sub['comment']}",
                "                Children=Start",
                "                  StringNode Start;",
                f"                    NElements={str_per_mppt}",
                f"                    SubArrayId={sub['subarray_id']}",
                f"                    SubArrayName={sub['comment']}",
                "                  StringNode End;",
                "                Children=End",
                "              MPPTNode End;",
                "            Children=End",
                "          InverterNode End;",
            ])
        else:
            str_per_inv = sub["n_string_ch"] // sub["n_inverter"]
            circuit_lines.extend([
                "          InverterNode Start;",
                f"            NElements={sub['n_inverter']}",
                f"            SubArrayId={sub['subarray_id']}",
                f"            SubArrayName={sub['comment']}",
                "            Children=Start",
                "              StringNode Start;",
                f"                NElements={str_per_inv}",
                f"                SubArrayId={sub['subarray_id']}",
                f"                SubArrayName={sub['comment']}",
                "              StringNode End;",
                "            Children=End",
                "          InverterNode End;",
            ])

    circuit_lines.extend([
        "        Children=End",
        "      InjectionPointNode End;",
        "    BaseNode=End",
        "  End of PVObject pvCircuit",
    ])

    subarray_lines = ["  SubArrays=Start"]
    for sub in normalized:
        if sub["power_sharing_inverter_id"] >= 0:
            flags = "$001E0502"
        elif sub["independent_mppt"]:
            flags = "$001E0402"
        else:
            flags = "$001E0002"
        subarray_lines.extend([
            "    PVObject_=pvSubArray",
            f"      Comment={sub['comment']}",
            f"      Version={version}",
            f"      Flags={flags}",
            "      Flags2=$0020",
            f"      SubArrayId={sub['subarray_id']}",
            f"      PVModule={sub['pan_name']}",
            f"      GInverter={sub['ond_name']}",
            f"      NModSerie={sub['n_mod_serie']}",
            f"      NStringCh={sub['n_string_ch']}",
            f"      NStrOrient1={sub['n_string_ch']}",
            f"      NoOrientation={sub['orientation_id']}",
            f"      NInverter={sub['n_inverter']}",
            f"      NInvMPPT={sub['n_inv_mppt']}",
            "      SEIsHeterogeneousString=False,",
            f"      PowerSharingInverterId={sub['power_sharing_inverter_id']}",
            f"      PNomAC_InMPPT={sub['pnom_ac_in_mppt']:.2f}",
            f"      PQualityModU={sub['module_quality_loss']:.2f}",
            f"      PQualityModI={sub['module_quality_loss']:.2f}",
            f"      PQualityModP={sub['module_quality_loss']:.2f}",
            f"      PMismMPP={sub['pmism_mpp']:.2f}",
            "      PMismUFixe=2.50",
            f"      LIDLoss={sub['lid_loss']:.2f}",
            "      LightSoaking=0.00",
            f"      RSCablageCh={sub['dc_wiring_resistance_mohm']:.3f}",
            f"      RSCablagePC={sub['dc_wiring_loss_pct']:.2f}",
            "      VDiodeSerie=0.00",
            *_DEFAULT_IAM_LINES,
            *_DEFAULT_WIRING_LINES,
            "      ACWireNoSect=0",
            "      ACWireLength=0.0",
            "    End of PVObject pvSubArray",
        ])
    subarray_lines.append("  EndTags")
    return circuit_lines, subarray_lines, normalized


def synthesize_project_text(*, project_comment: str, met_name: str, site_body: list[str],
                            version: str = SYNTH_VERSION, design_temp_min: float = -10.0,
                            temp_oper: float = 50.0, temp_oper_min: float = 20.0,
                            temp_oper_max: float = 60.0, albedo: float = 0.20,
                            with_bom: bool = True) -> str:
    """Synthesize a complete CRLF PVsyst .PRJ text string."""
    comment = _clean_text(project_comment, "project_comment")
    safe_filename(met_name, ".MET")
    alb = _finite(albedo, "albedo", minimum=0.0, maximum=1.0)
    albedo_row = ",".join(f"{alb:.3f}" for _ in range(12)) + ","
    lines = [
        "PVObject_=pvProject",
        f"  Comment={comment}",
        f"  Version={version}",
        "  Flags=$00080010",
        "PrintSettings=TPrintSettings",
        "  FirstLineOpt=1",
        "  PrintCoverPage=True",
        "  ClientDesignation=Client",
        "  UserDesignation=Author",
        "  PCNDec=1",
        "EndTags",
        f"MeteoFileName={met_name}",
        "PVObject_SitePrj=pvSite",
        *(f"  {line}" for line in site_body),
        "End of PVObject pvSite",
        f"Albedo={albedo_row}",
        f"DesignTempMin={int(round(_finite(design_temp_min, 'design_temp_min')))}",
        f"TempOper={int(round(_finite(temp_oper, 'temp_oper')))}",
        f"TempOperMin={int(round(_finite(temp_oper_min, 'temp_oper_min')))}",
        f"TempOperMax={int(round(_finite(temp_oper_max, 'temp_oper_max')))}",
        "LimOverloadLoss=3.00",
        "DiscrOrientDiff=6.00",
        "SpreadMaxDiff=3.00",
        "MaxSystem_ShdRatio=1.50",
        "RegionMeteo=1",
        "CorrAlttemp=-0.0055",
        "SiteMeteoMaxDistForList=10",
        "TranspositionAlgorithm=CircSolSeparate",
        "ACLossesRef=STC",
        "End of PVObject pvProject",
        "",
    ]
    text = "\r\n".join(lines)
    return ("\ufeff" + text) if with_bom else text


def synthesize_variant_text(*, variant_comment: str, site_body: list[str], meteo_lines: list[str],
                            orientations: list[dict], subarrays: list[dict],
                            default_pan: str = DEFAULT_PAN_NAME, default_ond: str = DEFAULT_OND_NAME,
                            version: str = SYNTH_VERSION, u_factor: float = 20.0,
                            shading_lines: list[str] | None = None,
                            bifacial_lines: list[str] | None = None,
                            storage_lines: list[str] | None = None,
                            with_bom: bool = True) -> str:
    """Synthesize a complete CRLF PVsyst .VC0 calculation variant string."""
    comment = _clean_text(variant_comment, "variant_comment")
    if not isinstance(orientations, list) or not orientations:
        raise ValueError("At least one orientation is required")
    norm_orients = [_normalize_orientation(item) for item in orientations]
    orient_ids = [item["id"] for item in norm_orients]
    if len(set(orient_ids)) != len(orient_ids):
        raise ValueError("Orientation IDs must be unique")

    circuit_lines, subarray_lines, norm_subs = synthesize_circuit_and_subarrays(
        subarrays, default_pan=default_pan, default_ond=default_ond, version=version
    )
    referenced_orients = {sub["orientation_id"] for sub in norm_subs}
    if not referenced_orients.issubset(set(orient_ids)):
        raise ValueError("Subarray references an undeclared orientation ID")
    if set(orient_ids) - referenced_orients:
        raise ValueError("Every declared orientation must be referenced by at least one subarray")

    uf = _finite(u_factor, "u_factor", minimum=15.0, maximum=100.0)
    lines = [
        "PVObject_=pvVCalcul",
        f"  Comment={comment}",
        f"  Version={version}",
        "  Flags=$00100001",
        "  SystemKindCustom=False",
        "  PRCorrTemp=False",
        "",
        "  PVObject_SiteSimul=pvSite",
        *(f"    {line}" for line in site_body),
        "  End of PVObject pvSite",
        "",
        *meteo_lines,
        "",
        f"  Orientations, list of={len(norm_orients)} TOrientGroup",
        "",
    ]
    for orient in norm_orients:
        lines.extend(synthesize_orientation_block(orient, version=version))
    lines.extend([
        "  End of Orientations",
        "",
        "  PerezUsedInSimul=True",
        "  TranspositionAlgorithm=CircSolSeparate",
        "  ACLossesRef=STC",
        "",
        "  PVObject_PVMainArray=pvMainArray",
        "    Flags=$00100400",
        "  Flags2=$082A",
        f"  UFactor={uf:.1f}",
        "  Alpha=0.9",
        "  UFAverEffic=18.4",
        "  MVNbTransfos=1",
        "  UseSpectralCorr=False",
        *circuit_lines,
        *subarray_lines,
        "  End of PVObject pvMainArray",
        "",
        "  PVObject_System=pvSystem",
        "    Flags=$00",
        "    SystemType=Grid",
        "  End of PVObject pvSystem",
        "",
        *(list(shading_lines) if shading_lines is not None else list(_DEFAULT_SHADING_LINES)),
    ])
    if bifacial_lines:
        lines.extend(["", *bifacial_lines])
    if storage_lines:
        lines.extend(["", *storage_lines])
    lines.extend(["", "End of PVObject pvVCalcul", ""])

    body_text = "\r\n".join(lines)
    # Self-check the synthesized circuit counts before returning.
    check_lines = body_text.splitlines(keepends=True)
    arrays: dict[int, tuple[int, int]] = {}
    i = 0
    while i < len(check_lines):
        if check_lines[i].strip() == "PVObject_=pvSubArray":
            end = next(j for j in range(i + 1, len(check_lines))
                       if check_lines[j].strip() == "End of PVObject pvSubArray")
            sid = next(int(FIELD.fullmatch(check_lines[k].rstrip("\r\n"))[2])
                       for k in range(i + 1, end)
                       if (m := FIELD.fullmatch(check_lines[k].rstrip("\r\n"))) and m[1] == "SubArrayId")
            arrays[sid] = (i, end)
            i = end + 1
            continue
        i += 1
    checked = grid_inventory(check_lines, arrays)
    if checked["counts_valid"] is not True:
        raise ValueError("Synthesized variant failed circuit count validation: "
                         + "; ".join(checked["issues"] + checked["unchecked_checks"]))
    return ("\ufeff" + body_text) if with_bom else body_text


def synthesize_project_and_variant(
    store=None,
    *,
    project_comment: str = "Synthesized PV Project",
    variant_comment: str = "Initial simulation",
    site_name: str | None = None,
    met_name: str | None = None,
    orientations: list[dict] | None = None,
    subarrays: list[dict] | None = None,
    pan_name: str | None = None,
    ond_name: str | None = None,
    n_mod_serie: int = 15,
    n_string_ch: int = 2,
    n_inverter: int = 1,
    n_inv_mppt: int | None = None,
    independent_mppt: bool = True,
    pnom_ac_in_mppt: float = 7.5,
    tilt_deg: float = 25.0,
    azimuth_deg: float = 0.0,
    u_factor: float = 20.0,
    shading_lines: list[str] | None = None,
    bifacial_lines: list[str] | None = None,
    storage_lines: list[str] | None = None,
    version: str = SYNTH_VERSION,
    with_bom: bool = True,
) -> dict:
    """Synthesize a matched (.PRJ, .VC0) pair directly in memory with zero demo templates."""
    resolved_pan = pan_name or DEFAULT_PAN_NAME
    resolved_ond = ond_name or DEFAULT_OND_NAME

    if site_name is not None:
        safe_filename(site_name, ".SIT")
    if met_name is not None:
        safe_filename(met_name, ".MET")

    site_body: list[str]
    site_identity: str
    resolved_site: str
    resolved_met: str
    meteo_lines: list[str]

    if store is not None and site_name is not None:
        site_path = workspace_file(store.workspace, "Sites", site_name, ".SIT")
        if not site_path.is_file():
            raise FileNotFoundError(f"Workspace SIT file is missing: {site_name}")
        resolved_site, site_identity, site_body = load_site_block(site_path)
    else:
        resolved_site = site_name or DEFAULT_PROJECT_SITE
        site_identity, site_body = placeholder_site_block(
            resolved_site, Path(resolved_site).stem or DEFAULT_PROJECT_SITE_NAME
        )

    if store is not None and met_name is not None:
        met_path = workspace_file(store.workspace, "Meteo", met_name, ".MET")
        if not met_path.is_file():
            raise FileNotFoundError(f"Workspace MET file is missing: {met_name}")
        resolved_met, meteo_lines = load_meteo_header(met_path, site_identity, site_body)
    else:
        resolved_met = met_name or DEFAULT_PROJECT_MET
        meteo_lines = synthesize_meteo_from_site(resolved_met, site_identity, site_body)

    if orientations is None:
        orientations = [{"id": 1, "field_type": "FixedPlane", "tilt_deg": tilt_deg, "azimuth_deg": azimuth_deg}]
    if subarrays is None:
        sub_mppt = n_inv_mppt if n_inv_mppt is not None else (n_string_ch if independent_mppt else 1)
        subarrays = [{
            "subarray_id": 1,
            "orientation_id": orientations[0]["id"],
            "comment": "PV Array",
            "pan_name": resolved_pan,
            "ond_name": resolved_ond,
            "n_mod_serie": n_mod_serie,
            "n_string_ch": n_string_ch,
            "n_inverter": n_inverter,
            "n_inv_mppt": sub_mppt,
            "independent_mppt": independent_mppt,
            "pnom_ac_in_mppt": pnom_ac_in_mppt,
        }]

    prj_text = synthesize_project_text(
        project_comment=project_comment,
        met_name=resolved_met,
        site_body=site_body,
        version=version,
        with_bom=with_bom,
    )
    vc0_text = synthesize_variant_text(
        variant_comment=variant_comment,
        site_body=site_body,
        meteo_lines=meteo_lines,
        orientations=orientations,
        subarrays=subarrays,
        default_pan=resolved_pan,
        default_ond=resolved_ond,
        version=version,
        u_factor=u_factor,
        shading_lines=shading_lines,
        bifacial_lines=bifacial_lines,
        storage_lines=storage_lines,
        with_bom=with_bom,
    )
    return {
        "prj_text": prj_text,
        "vc0_text": vc0_text,
        "site_name": resolved_site,
        "site_identity": site_identity,
        "met_name": resolved_met,
        "orientations": [_normalize_orientation(item) for item in orientations],
        "subarrays": [
            _normalize_subarray(item, default_pan=resolved_pan, default_ond=resolved_ond)
            for item in subarrays
        ],
    }


def _roof_plane_angles(mesh: dict) -> tuple[float, float]:
    """Return (tilt_deg, local_rotation_deg) for a planar upward-facing house roof mesh.

    Matches scene/author.py rack_mesh convention: anchor plane (rotation_deg=0)
    faces -Y with upward normal (0, -sin(tilt), cos(tilt)).
    """
    vertices = mesh["vertices"]
    triangles = mesh["triangles"]
    normals = []
    for a, b, c in triangles:
        va, vb, vc = vertices[a], vertices[b], vertices[c]
        ux, uy, uz = vb[0] - va[0], vb[1] - va[1], vb[2] - va[2]
        vx, vy, vz = vc[0] - va[0], vc[1] - va[1], vc[2] - va[2]
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        length = math.hypot(nx, ny, nz)
        if length <= _NORMAL_TOL:
            continue
        nx, ny, nz = nx / length, ny / length, nz / length
        if nz < 0:
            nx, ny, nz = -nx, -ny, -nz
        normals.append((nx, ny, nz))
    if not normals:
        raise ValueError(f"Roof mesh {mesh['name']!r} has no non-degenerate triangles")
    nx, ny, nz = normals[0]
    if nz <= _NORMAL_TOL:
        raise ValueError(f"Roof mesh {mesh['name']!r} is vertical and cannot host a PV plane")
    nz = max(-1.0, min(1.0, nz))
    tilt_deg = round(math.degrees(math.acos(nz)), 6)
    if tilt_deg < 1e-4:
        return 0.0, 0.0
    rot_deg = round(math.degrees(math.atan2(nx, -ny)), 6)
    return tilt_deg, rot_deg


def _infer_house_orientations_and_subarrays(request: dict, pocket: dict) -> tuple[list[dict], list[dict]]:
    """Derive orientations and subarrays from request + house meshes + layout when omitted."""
    meshes = {m["name"]: m for m in pocket["meshes"]}
    layout = request["layout"]
    entries = layout.get("tables") if isinstance(layout, dict) and "tables" in layout else [layout]
    default_pan = request.get("pan_name") or DEFAULT_PAN_NAME
    default_ond = request.get("ond_name") or DEFAULT_OND_NAME
    mode = request.get("shading_mode") or "linear"

    explicit_orients = request.get("orientations")
    explicit_subs = request.get("subarrays")

    # Determine orientation_ids referenced by tables.
    has_explicit_oids = any(isinstance(e, dict) and e.get("orientation_id") is not None for e in entries)
    if explicit_orients:
        orientations = [_normalize_orientation(item) for item in explicit_orients]
    else:
        oid_to_mesh: dict[int, dict] = {}
        for entry in entries:
            oid = entry.get("orientation_id") if has_explicit_oids else 1
            if type(oid) is not int or oid < 1:
                raise ValueError("Each table orientation_id must be a positive integer")
            mesh_name = entry.get("roof_mesh")
            if mesh_name not in meshes:
                raise ValueError(f"Unknown roof_mesh {mesh_name!r} in layout")
            oid_to_mesh.setdefault(oid, meshes[mesh_name])
        ordered_oids = sorted(oid_to_mesh)
        anchor_tilt, anchor_rot = _roof_plane_angles(oid_to_mesh[ordered_oids[0]])
        anchor_azim = _finite(request.get("anchor_azimuth_deg", 0.0), "anchor_azimuth_deg",
                              minimum=-180.0, maximum=180.0)
        orientations = []
        for oid in ordered_oids:
            tilt, rot = _roof_plane_angles(oid_to_mesh[oid])
            if oid == ordered_oids[0]:
                azim = anchor_azim
            else:
                azim = _normalize_azimuth(anchor_azim - (rot - anchor_rot))
            orientations.append({
                "id": oid,
                "field_type": "FixedPlane",
                "tilt_deg": tilt,
                "azimuth_deg": azim,
            })

    if explicit_subs:
        subarrays = [_normalize_subarray(item, default_pan=default_pan, default_ond=default_ond)
                     for item in explicit_subs]
    else:
        # Group table module counts by (source_subarray_id or orientation_id).
        owner_totals: dict[int, dict] = {}
        for entry in entries:
            oid = entry.get("orientation_id") if has_explicit_oids else orientations[0]["id"]
            owner = entry.get("source_subarray_id") or oid
            total = int(entry["module_rows"]) * int(entry["module_columns"])
            if owner not in owner_totals:
                owner_totals[owner] = {
                    "subarray_id": owner,
                    "orientation_id": oid,
                    "module_total": 0,
                    "table_modules": [],
                }
            owner_totals[owner]["module_total"] += total
            owner_totals[owner]["table_modules"].append(total)

        subarrays = []
        grouped = len(owner_totals) > 1
        for owner in sorted(owner_totals):
            info = owner_totals[owner]
            total = info["module_total"]
            table_mods = info["table_modules"]
            # Choose stringing that matches headless._support_gate cleanly:
            # - Single-owner implicit module_layout with 2 equal tables requires 2 strings/2 inv/2 mppt.
            # - Grouped implicit module_layout (no string_layout) requires 1 string/1 inv/1 mppt per subarray.
            if (not grouped and mode == "module_layout" and request.get("string_layout") is None
                    and len(table_mods) == 2 and table_mods[0] == table_mods[1]):
                n_mod_serie, n_string_ch, n_inverter, n_inv_mppt = table_mods[0], 2, 2, 2
            elif grouped and mode == "module_layout" and request.get("string_layout") is None:
                n_mod_serie, n_string_ch, n_inverter, n_inv_mppt = total, 1, 1, 1
            else:
                n_mod_serie, n_string_ch, n_inverter, n_inv_mppt = total, 1, 1, 1
            subarrays.append({
                "subarray_id": info["subarray_id"],
                "orientation_id": info["orientation_id"],
                "comment": "PV Array" if info["subarray_id"] == 1 else f"Sub-array #{info['subarray_id']}",
                "pan_name": default_pan,
                "ond_name": default_ond,
                "n_mod_serie": n_mod_serie,
                "n_string_ch": n_string_ch,
                "n_inverter": n_inverter,
                "n_inv_mppt": n_inv_mppt,
                "independent_mppt": True,
            })

    return orientations, subarrays


def synthesize_house_source(store, components, request: dict) -> dict:
    """Synthesize a clean zero-template (.PRJ, .VC0) source for run_house_task."""
    from pvsyst_auto.scene.author import house_profile

    site_name = request.get("site_name")
    met_name = request.get("met_name")
    if not isinstance(site_name, str) or not site_name.strip():
        raise ValueError("Zero-template house task requires an explicit workspace site_name (.SIT)")
    if not isinstance(met_name, str) or not met_name.strip():
        raise ValueError("Zero-template house task requires an explicit workspace met_name (.MET)")
    pocket = house_profile(request["house"])
    if pocket["kind"] != "mesh":
        raise ValueError("The house task requires a complete AI/caller-authored mesh house")
    orientations, subarrays = _infer_house_orientations_and_subarrays(request, pocket)
    synth = synthesize_project_and_variant(
        store,
        project_comment=f"{Path(request['new_project']).stem} (zero-template)",
        variant_comment=request["variant_name"],
        site_name=site_name,
        met_name=met_name,
        orientations=orientations,
        subarrays=subarrays,
        pan_name=request.get("pan_name"),
        ond_name=request.get("ond_name"),
        with_bom=True,
    )
    synth["synthesized"] = True
    return synth


def synthesize_ground_shed_shading(*, shed_count: int = 10, pitch_m: float = 10.0,
                                   tilt_deg: float = 20.0, azimuth_deg: float = 0.0,
                                   module_rows: int = 2, module_columns: int = 25,
                                   module_width_m: float = 1.052, module_length_m: float = 2.131,
                                   module_name: str = "PV Module",
                                   module_orient: str = "moPortrait",
                                   spacing_x_m: float = 0.020, spacing_y_m: float = 0.020,
                                   origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
                                   orientation_id: int = 1,
                                   version: str = SYNTH_VERSION) -> list[str]:
    """Synthesize a complete 3D PVObject_Ombrage=pvShading containing a pvShdArrayShed field."""
    n_sheds = _pos_int(shed_count, "shed_count")
    n_rows = _pos_int(module_rows, "module_rows")
    n_cols = _pos_int(module_columns, "module_columns")
    pitch = _finite(pitch_m, "pitch_m", minimum=0.1)
    tilt = _finite(tilt_deg, "tilt_deg", minimum=0.0, maximum=90.0)
    azim = _finite(azimuth_deg, "azimuth_deg", minimum=-180.0, maximum=180.0)
    mw = _finite(module_width_m, "module_width_m", minimum=0.1)
    ml = _finite(module_length_m, "module_length_m", minimum=0.1)
    sx = _finite(spacing_x_m, "spacing_x_m", minimum=0.0)
    sy = _finite(spacing_y_m, "spacing_y_m", minimum=0.0)
    if module_orient not in ("moPortrait", "moLandscape"):
        raise ValueError("module_orient must be 'moPortrait' or 'moLandscape'")
    cell_w = mw if module_orient == "moPortrait" else ml
    cell_h = ml if module_orient == "moPortrait" else mw
    larg_shed = n_rows * cell_h + max(0, n_rows - 1) * sy
    long_shed = n_cols * cell_w + max(0, n_cols - 1) * sx
    name = _clean_text(module_name, "module_name")
    return [
        "  PVObject_Ombrage=pvShading",
        "    Comment=Ground shed 3D shading scene",
        "    Flags=$000A0300",
        "    Precision=1",
        "    FracOmbreMod=1.000",
        "    FracOmbreThin=0.400",
        "    GCROffset=1.0",
        "    DisplayedRef=refGeographical",
        "",
        "    ListeObjets, list of=1 TShdObject",
        "",
        "      PVObject_1=pvShdArrayShed",
        "        Comment=Shed field #1",
        f"        Version={version}",
        "        Flags=$02200074",
        "        NoObjCreated=1",
        "        ColorTechn=$00FF0000",
        "        ColorSurf=$00FF9999",
        "        ColorBord=$00505050",
        f"        DOrigine={origin[0]:.6f},{origin[1]:.6f},{origin[2]:.6f}",
        f"        DInclin={tilt:.6f}",
        "        BaseSlope=0.000000",
        f"        DAzimut={azim:.6f}",
        "        Source=osPVsyst",
        f"      NElemChamps={n_sheds}",
        f"      NoOrient={_pos_int(orientation_id, 'orientation_id')}",
        f"      PanneauPVName={name};{mw:.3f};{ml:.3f}",
        f"      ModSpacingX={sx:.3f}",
        f"      ModSpacingY={sy:.3f}",
        f"      NModInHeight={n_rows}",
        f"      NModInWidth={n_cols}",
        f"      ModuleOrient={module_orient}",
        f"      DXModule={cell_w + sx:.3f}",
        f"      DYModule={cell_h + sy:.3f}",
        f"        LongShed={long_shed:.6f}",
        f"        LargShed={larg_shed:.6f}",
        "        Cadre=0.000,0.000,0.000,0.000,",
        "        TypeOrigine=2",
        f"        PitchNS={pitch:.3f}",
        "        PitchEW=0.000",
        "      End of PVObject pvShdArrayShed",
        "    End of ListeObjets",
        "  End of PVObject pvShading",
    ]


def synthesize_ground_tracker_shading(*, tracker_count: int = 10, pitch_ew_m: float = 12.0,
                                      axis_azimuth_deg: float = 0.0,
                                      module_rows: int = 2, module_columns: int = 25,
                                      module_width_m: float = 1.052, module_length_m: float = 2.131,
                                      module_name: str = "PV Module",
                                      module_orient: str = "moPortrait",
                                      spacing_x_m: float = 0.020, spacing_y_m: float = 0.020,
                                      origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
                                      orientation_id: int = 1,
                                      version: str = SYNTH_VERSION) -> list[str]:
    """Synthesize a complete 3D PVObject_Ombrage=pvShading containing a pvShdArrayTracking field."""
    n_trk = _pos_int(tracker_count, "tracker_count")
    n_rows = _pos_int(module_rows, "module_rows")
    n_cols = _pos_int(module_columns, "module_columns")
    pitch = _finite(pitch_ew_m, "pitch_ew_m", minimum=0.1)
    azim = _finite(axis_azimuth_deg, "axis_azimuth_deg", minimum=-180.0, maximum=180.0)
    mw = _finite(module_width_m, "module_width_m", minimum=0.1)
    ml = _finite(module_length_m, "module_length_m", minimum=0.1)
    sx = _finite(spacing_x_m, "spacing_x_m", minimum=0.0)
    sy = _finite(spacing_y_m, "spacing_y_m", minimum=0.0)
    if module_orient not in ("moPortrait", "moLandscape"):
        raise ValueError("module_orient must be 'moPortrait' or 'moLandscape'")
    # In PVsyst pvShdArrayTracking (verified against _DEMO_Utility.VC7):
    # LongShed = tracker cross-axis chord width (CollectorWidth), LargShed = tracker N-S length.
    chord_mod = ml if module_orient == "moPortrait" else mw
    length_mod = mw if module_orient == "moPortrait" else ml
    long_shed = n_rows * chord_mod + max(0, n_rows - 1) * sy
    larg_shed = n_cols * length_mod + max(0, n_cols - 1) * sx
    name = _clean_text(module_name, "module_name")
    return [
        "  PVObject_Ombrage=pvShading",
        "    Comment=Ground tracker 3D shading scene",
        "    Flags=$000A0300",
        "    Precision=1",
        "    FracOmbreMod=1.000",
        "    FracOmbreThin=0.400",
        "    GCROffset=1.0",
        "    DisplayedRef=refGeographical",
        "",
        "    ListeObjets, list of=1 TShdObject",
        "",
        "      PVObject_1=pvShdArrayTracking",
        "        Comment=Array of trackers #1",
        f"        Version={version}",
        "        Flags=$02200074",
        "        NoObjCreated=1",
        "        ColorTechn=$00FF0000",
        "        ColorSurf=$00FF9999",
        "        ColorBord=$00505050",
        f"        DOrigine={origin[0]:.6f},{origin[1]:.6f},{origin[2]:.6f}",
        "        DInclin=0.000000",
        "        BaseSlope=0.000000",
        f"        DAzimut={azim:.6f}",
        "        Source=osPVsyst",
        f"      NElemChamps={n_trk}",
        f"      NoOrient={_pos_int(orientation_id, 'orientation_id')}",
        f"      PanneauPVName={name};{mw:.3f};{ml:.3f}",
        f"      ModSpacingX={sx:.3f}",
        f"      ModSpacingY={sy:.3f}",
        f"      NModInHeight={n_rows}",
        f"      NModInWidth={n_cols}",
        f"      ModuleOrient={module_orient}",
        f"      DXModule={length_mod + sx:.3f}",
        f"      DYModule={larg_shed:.3f}",
        f"        LongShed={long_shed:.6f}",
        f"        LargShed={larg_shed:.6f}",
        "        Cadre=0.000,0.000,0.000,0.000,",
        "        TypeOrigine=2",
        "        PitchNS=0.000",
        f"        PitchEW={pitch:.3f}",
        "        AxisOrig=2",
        "      End of PVObject pvShdArrayTracking",
        "    End of ListeObjets",
        "  End of PVObject pvShading",
    ]


def _resolve_pan_module_dimensions(store, pan_name: str) -> dict | None:
    """Auto-extract (width_m, length_m, model_name) from a readable workspace/builtin .PAN file."""
    components = getattr(store, "components", None) if store is not None else None
    if components is None:
        return None
    from pvsyst_auto.components.pan import validation_fields as pan_validation_fields

    for library in ("workspace", "builtin"):
        if library == "builtin" and getattr(components, "builtin", None) is None:
            continue
        try:
            path = components._path("PAN", pan_name, library)
        except (ValueError, OSError):
            continue
        if not path.is_file():
            continue
        try:
            raw = components._read(path)
            info = components._inspect("PAN", raw)
            if info.get("format") != "text" or info.get("structural_errors") or info.get("known_value_errors"):
                continue
            _, text = components._text("PAN", raw)
            fields = pan_validation_fields(text)
            widths = fields.get("Width") or fields.get("LargApp") or []
            heights = fields.get("Height") or fields.get("LongApp") or []
            if len(widths) != 1 or len(heights) != 1:
                continue
            width_m = float(widths[0])
            length_m = float(heights[0])
            if not (math.isfinite(width_m) and width_m > 0 and math.isfinite(length_m) and length_m > 0):
                continue
            models = fields.get("Model") or []
            model_name = models[0].strip() if len(models) == 1 and models[0].strip() else None
            return {
                "width_m": width_m,
                "length_m": length_m,
                "model_name": model_name,
                "library": library,
                "path": str(path),
            }
        except (ValueError, OSError, KeyError):
            continue
    return None


def synthesize_utility_project(
    store, *,
    project_comment: str = "Utility PV Project",
    variant_comment: str = "Utility Variant",
    site_name: str,
    met_name: str,
    field_type: str = "Sheds",
    pan_name: str = "Generic_Mono_440W_Half_Bifacial.PAN",
    ond_name: str = "Generic_50kW.OND",
    n_mod_serie: int = 20,
    n_string_ch: int = 6,
    n_inverter: int = 1,
    n_inv_mppt: int = 1,
    independent_mppt: bool = False,
    pnom_ac_in_mppt: float = 50.0,
    tilt_deg: float = 20.0,
    azimuth_deg: float = 0.0,
    axis_azimuth_deg: float = 0.0,
    min_phi_deg: float = -60.0,
    max_phi_deg: float = 60.0,
    backtracking: bool = True,
    irrad_optimiz: bool = False,
    central_gap_m: float = 0.0,
    pitch_slope_deg: float = 0.0,
    wind_stow: bool = False,
    wind_stow_speed_ms: float = 12.0,
    wind_stow_position_deg: float = 0.0,
    pitch_m: float = 10.0,
    collector_width_m: float | None = None,
    shed_count: int = 4,
    inactive_band_width_1_m: float | None = None,
    inactive_band_width_2_m: float | None = None,
    electrical_effect: bool | None = None,
    shading_factor: float | None = None,
    cell_width_m: float | None = None,
    module_rows: int = 2,
    module_columns: int = 15,
    module_orient: str = "moPortrait",
    spacing_x_m: float = 0.020,
    spacing_y_m: float = 0.020,
    module_width_m: float | None = None,
    module_length_m: float | None = None,
    module_name: str | None = None,
    bifacial: dict | None = None,
    include_3d_scene: bool | None = None,
    u_factor: float = 29.0,
    lid_loss: float = 0.0,
    pmism_mpp: float = 2.0,
    module_quality_loss: float = 3.0,
    dc_wiring_loss_pct: float = 1.5,
    dc_wiring_resistance_mohm: float = 0.0,
    version: str = SYNTH_VERSION,
    with_bom: bool = True,
) -> dict:
    """Zero-template synthesizer for utility ground sheds or single-axis trackers (mono or bifacial)."""
    if field_type not in ("Sheds", "UnlimSheds", "RegularSheds", "UnlimTracking", "TrackHorizNSAxis"):
        raise ValueError(f"Unsupported utility field_type: {field_type!r}")
    if module_orient not in ("moPortrait", "moLandscape"):
        raise ValueError("module_orient must be 'moPortrait' or 'moLandscape'")

    pan_dims = _resolve_pan_module_dimensions(store, pan_name)
    if module_width_m is not None and module_length_m is not None:
        mw = _finite(module_width_m, "module_width_m", minimum=0.1)
        ml = _finite(module_length_m, "module_length_m", minimum=0.1)
        dim_source = "caller"
    elif pan_dims is not None:
        mw = _finite(module_width_m, "module_width_m", minimum=0.1) if module_width_m is not None else pan_dims["width_m"]
        ml = _finite(module_length_m, "module_length_m", minimum=0.1) if module_length_m is not None else pan_dims["length_m"]
        dim_source = f"pan:{pan_dims['library']}"
    else:
        mw = _finite(module_width_m, "module_width_m", minimum=0.1) if module_width_m is not None else 1.052
        ml = _finite(module_length_m, "module_length_m", minimum=0.1) if module_length_m is not None else 2.131
        dim_source = "default"
    resolved_mod_name = (
        module_name
        or (pan_dims["model_name"] if pan_dims and pan_dims.get("model_name") else None)
        or "Mono 440 Wp Twin 144 half-cells bifacial"
    )

    n_rows = _pos_int(module_rows, "module_rows")
    n_cols = _pos_int(module_columns, "module_columns")
    sy = _finite(spacing_y_m, "spacing_y_m", minimum=0.0)
    sx = _finite(spacing_x_m, "spacing_x_m", minimum=0.0)
    chord_mod = ml if module_orient == "moPortrait" else mw
    resolved_collector_width = (
        _finite(collector_width_m, "collector_width_m", minimum=0.1)
        if collector_width_m is not None
        else round(n_rows * chord_mod + max(0, n_rows - 1) * sy, 3)
    )
    resolved_pitch = _finite(pitch_m, "pitch_m", minimum=0.1)
    if resolved_pitch <= resolved_collector_width:
        raise ValueError(
            f"pitch_m ({resolved_pitch:.3f}) must exceed collector_width_m ({resolved_collector_width:.3f}) "
            "so adjacent ground rows do not overlap (GCR < 1.0)"
        )

    need_3d = (
        include_3d_scene
        if include_3d_scene is not None
        else field_type in ("RegularSheds", "TrackHorizNSAxis")
    )
    orient_spec = {
        "id": 1,
        "field_type": field_type,
        "tilt_deg": tilt_deg,
        "azimuth_deg": azimuth_deg,
        "axis_azimuth_deg": axis_azimuth_deg,
        "min_phi_deg": min_phi_deg,
        "max_phi_deg": max_phi_deg,
        "backtracking": backtracking,
        "irrad_optimiz": irrad_optimiz,
        "central_gap_m": central_gap_m,
        "pitch_slope_deg": pitch_slope_deg,
        "wind_stow": wind_stow,
        "wind_stow_speed_ms": wind_stow_speed_ms,
        "wind_stow_position_deg": wind_stow_position_deg,
        "pitch_m": resolved_pitch,
        "collector_width_m": resolved_collector_width,
        "shed_count": shed_count,
        "module_columns": n_rows if field_type in ("Sheds", "UnlimSheds", "UnlimTracking") else n_cols,
        "bifacial": bifacial,
    }
    if inactive_band_width_1_m is not None:
        orient_spec["inactive_band_width_1_m"] = inactive_band_width_1_m
    if inactive_band_width_2_m is not None:
        orient_spec["inactive_band_width_2_m"] = inactive_band_width_2_m
    if electrical_effect is not None:
        orient_spec["electrical_effect"] = electrical_effect
    if shading_factor is not None:
        orient_spec["shading_factor"] = shading_factor
    if cell_width_m is not None:
        orient_spec["cell_width_m"] = cell_width_m

    shading_lines = None
    if need_3d:
        if field_type in ("TrackHorizNSAxis", "UnlimTracking"):
            shading_lines = synthesize_ground_tracker_shading(
                tracker_count=shed_count,
                pitch_ew_m=resolved_pitch,
                axis_azimuth_deg=axis_azimuth_deg,
                module_rows=n_rows,
                module_columns=n_cols,
                module_width_m=mw,
                module_length_m=ml,
                module_name=resolved_mod_name,
                module_orient=module_orient,
                spacing_x_m=sx,
                spacing_y_m=sy,
                version=version,
            )
        else:
            shading_lines = synthesize_ground_shed_shading(
                shed_count=shed_count,
                pitch_m=resolved_pitch,
                tilt_deg=tilt_deg,
                azimuth_deg=azimuth_deg,
                module_rows=n_rows,
                module_columns=n_cols,
                module_width_m=mw,
                module_length_m=ml,
                module_name=resolved_mod_name,
                module_orient=module_orient,
                spacing_x_m=sx,
                spacing_y_m=sy,
                version=version,
            )

    sub_mppt = n_inv_mppt if n_inv_mppt is not None else (n_string_ch if independent_mppt else 1)
    subarrays = [{
        "subarray_id": 1,
        "orientation_id": 1,
        "comment": "PV Array",
        "pan_name": pan_name,
        "ond_name": ond_name,
        "n_mod_serie": n_mod_serie,
        "n_string_ch": n_string_ch,
        "n_inverter": n_inverter,
        "n_inv_mppt": sub_mppt,
        "independent_mppt": independent_mppt,
        "pnom_ac_in_mppt": pnom_ac_in_mppt,
        "lid_loss": lid_loss,
        "pmism_mpp": pmism_mpp,
        "module_quality_loss": module_quality_loss,
        "dc_wiring_loss_pct": dc_wiring_loss_pct,
        "dc_wiring_resistance_mohm": dc_wiring_resistance_mohm,
    }]

    result = synthesize_project_and_variant(
        store,
        project_comment=project_comment,
        variant_comment=variant_comment,
        site_name=site_name,
        met_name=met_name,
        orientations=[orient_spec],
        subarrays=subarrays,
        pan_name=pan_name,
        ond_name=ond_name,
        u_factor=u_factor,
        shading_lines=shading_lines,
        version=version,
        with_bom=with_bom,
    )
    result["utility_summary"] = {
        "field_type": field_type,
        "include_3d_scene": need_3d,
        "pitch_m": resolved_pitch,
        "collector_width_m": resolved_collector_width,
        "gcr": round(resolved_collector_width / resolved_pitch, 4),
        "shed_count": shed_count,
        "module_rows": n_rows,
        "module_columns": n_cols,
        "module_orient": module_orient,
        "module_width_m": mw,
        "module_length_m": ml,
        "module_dimension_source": dim_source,
        "bifacial_enabled": bool(bifacial and bifacial.get("enabled", True)),
    }
    return result

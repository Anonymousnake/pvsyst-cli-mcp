"""Pure-text zero-template synthesizer for PVsyst 8.1.7 .PRJ and .VC0 files."""
from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

from pvsyst_auto.components.store import FIELD
from pvsyst_auto.project.bifacial import render_bifacial_block
from pvsyst_auto.project.circuit import MAX_COUNT, inventory as grid_inventory
from pvsyst_auto.project.paths import safe_filename, workspace_file
from pvsyst_auto.validation import finite_number as _finite
from pvsyst_auto.versions import SYNTH_VERSION
from pvsyst_auto.weather.preparation import read_object_prefix

DEFAULT_PAN_NAME, DEFAULT_OND_NAME = "Generic_Mono_300W.PAN", "Generic_7.5kW.OND"
DEFAULT_PROJECT_SITE, DEFAULT_PROJECT_SITE_NAME, DEFAULT_PROJECT_MET = "TemplateSite.SIT", "TemplateSite", "TemplateMeteo.MET"
_NORMAL_TOL = 1e-6

# Standard Fresnel AR IAM 9-point cubic profile used by PVsyst 8.1 grid variants.
_DEFAULT_IAM_LINES = (
    "      PVObject_IAM=pvIAM", "        Flags=$00", "        IAMMode=FresnelAR",
    "        N_Glass=1.526", "        N_EVA=1.490", "        N_ARCoating=1.290",
    "        DGlass=4.00", "        DArCoating=0.100", "        IAMProfile=TCubicProfile",
    "          NPtsMax=9", "          NPtsEff=9", "          LastCompile=$B18D", "          Mode=3",
    "          Point_1=0.0,1.00000", "          Point_2=30.0,0.99896", "          Point_3=50.0,0.98727",
    "          Point_4=60.0,0.96252", "          Point_5=70.0,0.89160", "          Point_6=75.0,0.81433",
    "          Point_7=80.0,0.67885", "          Point_8=85.0,0.43754", "          Point_9=90.0,0.00000",
    "        End of TCubicProfile", "      End of PVObject pvIAM",
)
_DEFAULT_WIRING_LINES = (
    "      WiringResistance=TWiringResistance", "        TypeLayout=1", "        TypeMetal=1",
    "        NGroupS=1", "        NGroupP=1", "        NChamp=1", "        Resistivity=18.800",
    "        TargetLoss=0.015", "        NoSect=1,1,1,", "      EndTags",
)
_DEFAULT_SHADING_LINES = (
    "  PVObject_Ombrage=pvShading", "    Comment=No shading scene defined", "    Flags=$00080000",
    "    Precision=1", "    FracOmbreMod=1.000", "    FracOmbreThin=0.400", "    GCROffset=1.0",
    "    DisplayedRef=refGeographical", "  End of PVObject pvShading",
)


def _pos_int(value, context: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_COUNT:
        raise ValueError(f"{context} must be a positive 32-bit integer")
    return value


def _fmt_angle(value: float, max_decimals: int = 3) -> str:
    """Format an angle preserving up to 3 decimal places while keeping exact 0.1-step values compact."""
    number = 0.0 if abs(value) < 1e-9 else round(float(value), max_decimals)
    number = 0.0 if abs(number) < 1e-9 else number
    text = f"{number:.{max_decimals}f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def _clean_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ch in value for ch in ("\r", "\n", "\x00")):
        raise ValueError(f"{label} must be single-line non-empty text")
    return value.strip()


def _normalize_azimuth(degrees: float) -> float:
    """Normalize PVsyst azimuth into [-180, 180] degrees (0 = south, west positive)."""
    value = ((degrees + 180.0) % 360.0) - 180.0
    value = 180.0 if abs(value + 180.0) < 1e-9 else value
    return 0.0 if abs(value) < 1e-9 else value


def placeholder_site_block(site_name: str = DEFAULT_PROJECT_SITE,
                           site_identity: str = DEFAULT_PROJECT_SITE_NAME) -> tuple[str, list[str]]:
    """Return minimal valid pvSite body lines used only before real weather is bound."""
    safe_filename(site_name, ".SIT")
    ident = _clean_text(site_identity, "site_identity")
    return ident, [
        f"Comment={ident};;", f"Version={SYNTH_VERSION}", "Flags=$0006083F", f"NomF={site_name}",
        f"Site={ident}", "Latitude=46.2400", "Longitude=6.1100", "Altitude=425",
        "TimeZone=1.0", "HLeg=True", "UnitsDisp=kWh_m2_m", "Albedo=0.20",
    ]


def load_site_block(site_path: Path) -> tuple[str, str, list[str]]:
    """Parse a workspace .SIT file into (nom_f, site_identity, body_lines)."""
    try:
        text = site_path.read_bytes().decode("utf-8-sig")
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
    nom_f, site_identity, updated_body = None, None, []
    for line in body:
        if match := FIELD.fullmatch(line):
            if match[1] == "NomF":
                nom_f = match[2]
                updated_body.append(f"NomF={site_path.name}")
                continue
            if match[1] == "Site":
                site_identity = match[2]
        updated_body.append(line)
    if nom_f is None:
        updated_body.insert(0, f"NomF={site_path.name}")
    if not site_identity:
        site_identity = site_path.stem
        updated_body.insert(1, f"Site={site_identity}")
    return site_path.name, site_identity, updated_body


def _is_pvmeteo_file(met_path: Path) -> bool:
    """Check whether the first non-empty header line declares a pvMeteo object."""
    with met_path.open("rb") as stream:
        prefix = stream.read(4096)
    for index, raw_line in enumerate(prefix.split(b"\n"), start=1):
        line = raw_line.rstrip(b"\r")
        if index == 1 and line.startswith(b"\xef\xbb\xbf"):
            line = line[3:]
        for enc in ("utf-8", "cp1252"):
            try:
                text = line.decode(enc)
                break
            except UnicodeDecodeError:
                text = None
        if text is None:
            return False
        if stripped := text.strip():
            return stripped == "PVObject_=pvMeteo"
    return False


def load_meteo_header(met_path: Path, site_identity: str, site_body: list[str]) -> tuple[str, list[str]]:
    """Build PVObject_MeteoSimul=pvMeteo block lines from a workspace .MET header."""
    header_fields, embedded_site = [], []
    if _is_pvmeteo_file(met_path):
        segments, _ = read_object_prefix(met_path)
        sorted_entries = lambda sec: sorted(
            ((entry["line"], key, entry["value"]) for key, items in segments.get(sec, {}).items() for entry in items),
            key=lambda item: item[0],
        )
        header_fields = [(k, v) for _, k, v in sorted_entries("root") if k not in ("Comment", "Version")]
        embedded_site = [f"{k}={v}" for _, k, v in sorted_entries("SiteMet")]

    field_map = dict(header_fields)
    ordered_keys = [
        ("Flags", field_map.get("Flags", "$010084E1")),
        ("NomF", met_path.name),
        ("SiteM", field_map.get("SiteM") or site_identity),
        *((k, v) for k, v in header_fields if k not in ("Flags", "NomF", "SiteM")),
    ]
    site_lines = list(embedded_site) if embedded_site else list(site_body)
    return met_path.name, [
        "  PVObject_MeteoSimul=pvMeteo",
        *(f"    {k}={v}" for k, v in ordered_keys),
        "", "    PVObject_SiteMet=pvSite",
        *(f"      {line}" for line in site_lines),
        "    End of PVObject pvSite", "  End of PVObject pvMeteo",
    ]


def synthesize_meteo_from_site(met_name: str, site_identity: str, site_body: list[str], *, pas_accum: int = 60) -> list[str]:
    """Build PVObject_MeteoSimul=pvMeteo lines when no workspace .MET header is parsed yet."""
    safe_filename(met_name, ".MET")
    ident = _clean_text(site_identity, "site_identity")
    return [
        "  PVObject_MeteoSimul=pvMeteo", "    Flags=$010084E1", f"    NomF={met_name}", f"    SiteM={ident}",
        "    UsesPerez=True", "    IsSourceUt=False", "    HourShift=0", "    TimeShift=0",
        "    TimeShiftTotalMin=0", f"    PasAccum={pas_accum}", "", "    PVObject_SiteMet=pvSite",
        *(f"      {line}" for line in site_body), "    End of PVObject pvSite", "  End of PVObject pvMeteo",
    ]


def _normalize_orientation(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Each orientation specification must be an object")
    oid = _pos_int(item.get("id", 1), "orientation.id")
    raw_ft = item.get("field_type", "FixedPlane")
    field_type = {"UnlimSheds": "Sheds", "RegularSheds": "FixedPlane"}.get(raw_ft, raw_ft)
    if field_type not in ("FixedPlane", "Sheds", "UnlimTracking", "TrackHorizNSAxis"):
        raise ValueError(f"Unsupported orientation field_type: {raw_ft!r}")
    tilt = _finite(item.get("tilt_deg", 25.0), "orientation.tilt_deg", minimum=0.0, maximum=90.0)
    azim = _finite(item.get("azimuth_deg", 0.0), "orientation.azimuth_deg", minimum=-180.0, maximum=180.0)
    base_slope = _finite(item.get("base_slope_deg", 0.0), "orientation.base_slope_deg", minimum=-90.0, maximum=90.0)
    default_comment = {
        "FixedPlane": f"Fixed, Tilt {_fmt_angle(tilt)} deg, Azim. {_fmt_angle(azim)} deg",
        "Sheds": f"Unlimited sheds, Tilt {_fmt_angle(tilt)} deg, Azim. {_fmt_angle(azim)} deg",
        "UnlimTracking": "Unlimited trackers, horizontal axis N-S",
        "TrackHorizNSAxis": "Tracking, horizontal axis N-S",
    }[field_type]
    spec = {
        "id": oid, "field_type": field_type, "tilt_deg": tilt, "azimuth_deg": azim,
        "base_slope_deg": base_slope, "comment": item.get("comment") or default_comment,
        "extra_fields": dict(item.get("extra_fields") or {}), "bifacial": item.get("bifacial"),
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
        unlim = field_type == "UnlimTracking"
        spec.update({
            "axis_azimuth_deg": _finite(item.get("axis_azimuth_deg", 0.0), "orientation.axis_azimuth_deg", minimum=-180.0, maximum=180.0),
            "min_phi_deg": min_phi, "max_phi_deg": max_phi,
            "backtracking": bool(item.get("backtracking", True)), "irrad_optimiz": bool(item.get("irrad_optimiz", False)),
            "pitch_m": _finite(item.get("pitch_m", 12.0), "orientation.pitch_m", minimum=0.1),
            "collector_width_m": _finite(item.get("collector_width_m", 4.282), "orientation.collector_width_m", minimum=0.1),
            "inactive_band_width_1_m": _finite(item.get("inactive_band_width_1_m", 0.02 if unlim else 0.0), "orientation.inactive_band_width_1_m", minimum=0.0),
            "inactive_band_width_2_m": _finite(item.get("inactive_band_width_2_m", 0.02 if unlim else 0.0), "orientation.inactive_band_width_2_m", minimum=0.0),
        })
        if unlim:
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


def _orient_shelf_lines(spec: dict) -> list[str]:
    return [
        f"    Pitch={spec['pitch_m']:.3f}", f"    CollectorWidth={spec['collector_width_m']:.3f}",
        f"    InactiveBandWidth1={spec['inactive_band_width_1_m']:.3f}",
        f"    InactiveBandWidth2={spec['inactive_band_width_2_m']:.3f}",
    ]


def _orient_shed_geometry_lines(spec: dict) -> list[str]:
    return [
        *_orient_shelf_lines(spec), f"    NSheds={spec['shed_count']}",
        f"    UsesElectricalEffect={'True' if spec['electrical_effect'] else 'False'}",
        f"    FracShadingFactor={spec['shading_factor']:.3f}", f"    NModInWidth={spec['module_columns']}",
        f"    CellWidth={spec['cell_width_m']:.3f}",
    ]


def synthesize_orientation_block(orient: dict, *, version: str = SYNTH_VERSION) -> list[str]:
    """Serialize one TOrientGroup / pvOrient record (including optional pvBiFacial)."""
    spec = _normalize_orientation(orient)
    ft = spec["field_type"]
    lines = [
        "    PVObject_=pvOrient", f"      Comment={_clean_text(spec['comment'], 'orientation.comment')}",
        f"      Version={version}", "      Flags=$00", f"    NoOrient={spec['id']}", f"    FieldType={ft}",
    ]
    if ft in ("FixedPlane", "Sheds"):
        lines.extend([f"    FieldTilt={_fmt_angle(spec['tilt_deg'])}", f"    FieldAzim={_fmt_angle(spec['azimuth_deg'])}"])
        lines.extend([f"    BaseSlope={_fmt_angle(spec['base_slope_deg'])}"] if ft == "FixedPlane" else _orient_shed_geometry_lines(spec))
    elif ft == "UnlimTracking":
        lines.extend([
            f"    AxisAzim={_fmt_angle(spec['axis_azimuth_deg'])}", f"    MinPhi={_fmt_angle(spec['min_phi_deg'])}",
            f"    MaxPhi={_fmt_angle(spec['max_phi_deg'])}", f"    UsesBacktracking={'True' if spec['backtracking'] else 'False'}",
            f"    UsesIrradOptimiz={'True' if spec['irrad_optimiz'] else 'False'}", *_orient_shed_geometry_lines(spec),
        ])
    elif ft == "TrackHorizNSAxis":
        lines.extend([
            f"    AxisAzim={_fmt_angle(spec['axis_azimuth_deg'])}", f"    MinPhi={_fmt_angle(spec['min_phi_deg'])}",
            f"    MaxPhi={_fmt_angle(spec['max_phi_deg'])}", f"    CentralGap={spec['central_gap_m']:.2f}",
            f"    PitchSlope={_fmt_angle(spec['pitch_slope_deg'])}", f"    UsesBacktracking={'True' if spec['backtracking'] else 'False'}",
            "    PartialSystemNoRefField=-1", "    PartialSystemNoSubField=1", "    PartialSystemMode=psmUserAll",
            "    PartialSystemInclObj=True", f"    UsesIrradOptimiz={'True' if spec['irrad_optimiz'] else 'False'}",
            f"    UsesWindStow={'True' if spec['wind_stow'] else 'False'}", f"    WindStowSpeed={spec['wind_stow_speed_ms']:.3f}",
            f"    WindStowPosition={spec['wind_stow_position_deg']:.3f}", *_orient_shelf_lines(spec),
        ])
    lines.extend(["    UsesTiltAver=False", "    UsesAzimAver=False", "    UsesSlopeAver=False", "    BTNoTrackers=-1,-1,",
                  *(f"    {k}={v}" for k, v in spec["extra_fields"].items())])
    if spec["bifacial"] is not None:
        lines.extend(render_bifacial_block(spec["bifacial"], field_type=ft, indent="    "))
    lines.append("    End of TOrientGroup")
    return lines


def _normalize_subarray(item: dict, *, default_pan: str, default_ond: str) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Each subarray specification must be an object")
    sid = _pos_int(item.get("subarray_id", 1), "subarray.subarray_id")
    oid = _pos_int(item.get("orientation_id", 1), "subarray.orientation_id")
    comment = _clean_text(item.get("comment") or ("PV Array" if sid == 1 else f"Sub-array #{sid}"), f"subarray {sid} comment")
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
        default_mppt = n_string_ch if n_string_ch % n_inverter == 0 else n_inverter
        n_inv_mppt = _pos_int(item.get("n_inv_mppt", default_mppt), f"subarray {sid} n_inv_mppt")
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
    pmism_raw, pqual_raw = item.get("pmism_mpp"), item.get("module_quality_loss")
    pmism_explicit = pmism_raw is not None if item.get("pmism_mpp_explicit") is None else item.get("pmism_mpp_explicit")
    pqual_explicit = pqual_raw is not None if item.get("module_quality_loss_explicit") is None else item.get("module_quality_loss_explicit")
    return {
        "subarray_id": sid, "orientation_id": oid, "comment": comment, "pan_name": pan_name, "ond_name": ond_name,
        "n_mod_serie": n_mod_serie, "n_string_ch": n_string_ch, "n_inverter": n_inverter, "n_inv_mppt": n_inv_mppt,
        "independent_mppt": independent_mppt, "power_sharing_inverter_id": ps_raw,
        "pnom_ac_in_mppt": _finite(item.get("pnom_ac_in_mppt", 7.5), f"subarray {sid} pnom_ac_in_mppt", minimum=0.0),
        "lid_loss": _finite(item.get("lid_loss", 0.0), f"subarray {sid} lid_loss", minimum=0.0, maximum=100.0),
        "pmism_mpp": _finite(2.0 if pmism_raw is None else pmism_raw, f"subarray {sid} pmism_mpp", minimum=0.0, maximum=100.0),
        "pmism_mpp_explicit": bool(pmism_explicit),
        "module_quality_loss": _finite(3.0 if pqual_raw is None else pqual_raw, f"subarray {sid} module_quality_loss", minimum=-5.0, maximum=20.0),
        "module_quality_loss_explicit": bool(pqual_explicit),
        "dc_wiring_loss_pct": _finite(item.get("dc_wiring_loss_pct", 1.5), f"subarray {sid} dc_wiring_loss_pct", minimum=0.0, maximum=100.0),
        "dc_wiring_resistance_mohm": _finite(item.get("dc_wiring_resistance_mohm", 0.0), f"subarray {sid} dc_wiring_resistance_mohm", minimum=0.0),
    }


def synthesize_circuit_and_subarrays(subarrays: list[dict], *, default_pan: str = DEFAULT_PAN_NAME,
                                     default_ond: str = DEFAULT_OND_NAME, version: str = SYNTH_VERSION) -> tuple[list[str], list[str], list[dict]]:
    """Build synchronized PVObject_SystemCircuit and SubArrays blocks."""
    if not isinstance(subarrays, list) or not subarrays:
        raise ValueError("At least one subarray specification is required")
    normalized = [_normalize_subarray(item, default_pan=default_pan, default_ond=default_ond) for item in subarrays]
    seen_ids = set()
    for sub in normalized:
        if sub["subarray_id"] in seen_ids:
            raise ValueError(f"Duplicate subarray_id {sub['subarray_id']}")
        seen_ids.add(sub["subarray_id"])

    shared_groups: dict[int, list[dict]] = defaultdict(list)
    for sub in normalized:
        if sub["power_sharing_inverter_id"] >= 0:
            shared_groups[sub["power_sharing_inverter_id"]].append(sub)
    for gid, members in shared_groups.items():
        if len(members) < 2:
            raise ValueError(f"PowerSharingInverterId={gid} requires at least two member subarrays")
        if len({m["ond_name"].casefold() for m in members}) != 1:
            raise ValueError(f"PowerSharingInverterId={gid} members must share the same OND inverter")
        if len({m["n_inverter"] for m in members}) != 1:
            raise ValueError(f"PowerSharingInverterId={gid} members must declare the same n_inverter")

    circuit_lines = [
        "  PVObject_SystemCircuit=pvCircuit", f"    Version={version}", "    Flags=$00", "    PaperOrient=poPortrait",
        "    ShowLegend=True", "    BaseNode=Start", "      InjectionPointNode Start;", "        NElements=1", "        Children=Start",
    ]
    _str_node = lambda cnt, sid, cmt, ind: [f"{ind}StringNode Start;", f"{ind}  NElements={cnt}", f"{ind}  SubArrayId={sid}", f"{ind}  SubArrayName={cmt}", f"{ind}StringNode End;"]
    _mppt_node = lambda m_cnt, s_cnt, sid, cmt: ["              MPPTNode Start;", f"                NElements={m_cnt}", f"                SubArrayId={sid}", f"                SubArrayName={cmt}", "                Children=Start", *_str_node(s_cnt, sid, cmt, "                  "), "                Children=End", "              MPPTNode End;"]
    _inv_node = lambda i_cnt, sid, cmt, ch: ["          InverterNode Start;", f"            NElements={i_cnt}", f"            SubArrayId={sid}", f"            SubArrayName={cmt}", "            Children=Start", *ch, "            Children=End", "          InverterNode End;"]

    emitted_groups: set[int] = set()
    for sub in normalized:
        gid = sub["power_sharing_inverter_id"]
        if gid >= 0:
            if gid in emitted_groups:
                continue
            emitted_groups.add(gid)
            members = shared_groups[gid]
            group_inv = members[0]["n_inverter"]
            mppt_children = [ln for m in members for ln in _mppt_node(m["n_inv_mppt"] // group_inv, m["n_string_ch"] // m["n_inv_mppt"], m["subarray_id"], m["comment"])]
            circuit_lines.extend(_inv_node(group_inv, 0, "; ".join(m["comment"] for m in members), mppt_children))
        elif sub["independent_mppt"]:
            circuit_lines.extend(_inv_node(sub["n_inverter"], sub["subarray_id"], sub["comment"],
                                           _mppt_node(sub["n_inv_mppt"] // sub["n_inverter"], sub["n_string_ch"] // sub["n_inv_mppt"], sub["subarray_id"], sub["comment"])))
        else:
            circuit_lines.extend(_inv_node(sub["n_inverter"], sub["subarray_id"], sub["comment"],
                                           _str_node(sub["n_string_ch"] // sub["n_inverter"], sub["subarray_id"], sub["comment"], "              ")))
    circuit_lines.extend(["        Children=End", "      InjectionPointNode End;", "    BaseNode=End", "  End of PVObject pvCircuit"])

    subarray_lines = ["  SubArrays=Start"]
    for sub in normalized:
        flags_int = 0x001E0502 if sub["power_sharing_inverter_id"] >= 0 else (0x001E0402 if sub["independent_mppt"] else 0x001E0002)
        if sub["pmism_mpp_explicit"]:
            flags_int &= ~0x00040000
        if sub["module_quality_loss_explicit"]:
            flags_int &= ~0x00100000
        subarray_lines.extend([
            "    PVObject_=pvSubArray", f"      Comment={sub['comment']}", f"      Version={version}",
            f"      Flags=${flags_int:08X}", "      Flags2=$0020", f"      SubArrayId={sub['subarray_id']}",
            f"      PVModule={sub['pan_name']}", f"      GInverter={sub['ond_name']}",
            f"      NModSerie={sub['n_mod_serie']}", f"      NStringCh={sub['n_string_ch']}",
            f"      NStrOrient1={sub['n_string_ch']}", f"      NoOrientation={sub['orientation_id']}",
            f"      NInverter={sub['n_inverter']}", f"      NInvMPPT={sub['n_inv_mppt']}",
            "      SEIsHeterogeneousString=False,", f"      PowerSharingInverterId={sub['power_sharing_inverter_id']}",
            f"      PNomAC_InMPPT={sub['pnom_ac_in_mppt']:.2f}", f"      PQualityModU={sub['module_quality_loss']:.2f}",
            f"      PQualityModI={sub['module_quality_loss']:.2f}", f"      PQualityModP={sub['module_quality_loss']:.2f}",
            f"      PMismMPP={sub['pmism_mpp']:.2f}", "      PMismUFixe=2.50", f"      LIDLoss={sub['lid_loss']:.2f}",
            "      LightSoaking=0.00", f"      RSCablageCh={sub['dc_wiring_resistance_mohm']:.3f}",
            f"      RSCablagePC={sub['dc_wiring_loss_pct']:.2f}", "      VDiodeSerie=0.00",
            *_DEFAULT_IAM_LINES, *_DEFAULT_WIRING_LINES, "      ACWireNoSect=0", "      ACWireLength=0.0",
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
    lines = [
        "PVObject_=pvProject", f"  Comment={comment}", f"  Version={version}", "  Flags=$00080010",
        "PrintSettings=TPrintSettings", "  FirstLineOpt=1", "  PrintCoverPage=True", "  ClientDesignation=Client",
        "  UserDesignation=Author", "  PCNDec=1", "EndTags", f"MeteoFileName={met_name}",
        "PVObject_SitePrj=pvSite", *(f"  {line}" for line in site_body), "End of PVObject pvSite",
        f"Albedo={','.join(f'{alb:.3f}' for _ in range(12)) + ','}",
        f"DesignTempMin={int(round(_finite(design_temp_min, 'design_temp_min')))}",
        f"TempOper={int(round(_finite(temp_oper, 'temp_oper')))}",
        f"TempOperMin={int(round(_finite(temp_oper_min, 'temp_oper_min')))}",
        f"TempOperMax={int(round(_finite(temp_oper_max, 'temp_oper_max')))}",
        "LimOverloadLoss=3.00", "DiscrOrientDiff=6.00", "SpreadMaxDiff=3.00", "MaxSystem_ShdRatio=1.50",
        "RegionMeteo=1", "CorrAlttemp=-0.0055", "SiteMeteoMaxDistForList=10",
        "TranspositionAlgorithm=CircSolSeparate", "ACLossesRef=STC", "End of PVObject pvProject", "",
    ]
    text = "\r\n".join(lines)
    return ("\ufeff" + text) if with_bom else text


def synthesize_variant_text(*, variant_comment: str, site_body: list[str], meteo_lines: list[str],
                            orientations: list[dict], subarrays: list[dict],
                            default_pan: str = DEFAULT_PAN_NAME, default_ond: str = DEFAULT_OND_NAME,
                            version: str = SYNTH_VERSION, u_factor: float = 20.0,
                            shading_lines: list[str] | None = None, bifacial_lines: list[str] | None = None,
                            storage_lines: list[str] | None = None, with_bom: bool = True) -> str:
    """Synthesize a complete CRLF PVsyst .VC0 calculation variant string."""
    comment = _clean_text(variant_comment, "variant_comment")
    if not isinstance(orientations, list) or not orientations:
        raise ValueError("At least one orientation is required")
    norm_orients = [_normalize_orientation(item) for item in orientations]
    orient_ids = [item["id"] for item in norm_orients]
    if len(set(orient_ids)) != len(orient_ids):
        raise ValueError("Orientation IDs must be unique")
    circuit_lines, subarray_lines, norm_subs = synthesize_circuit_and_subarrays(subarrays, default_pan=default_pan, default_ond=default_ond, version=version)
    referenced_orients = {sub["orientation_id"] for sub in norm_subs}
    if not referenced_orients.issubset(set(orient_ids)):
        raise ValueError("Subarray references an undeclared orientation ID")
    if set(orient_ids) - referenced_orients:
        raise ValueError("Every declared orientation must be referenced by at least one subarray")

    uf = _finite(u_factor, "u_factor", minimum=15.0, maximum=100.0)
    lines = [
        "PVObject_=pvVCalcul", f"  Comment={comment}", f"  Version={version}", "  Flags=$00100001",
        "  SystemKindCustom=False", "  PRCorrTemp=False", "",
        "  PVObject_SiteSimul=pvSite", *(f"    {line}" for line in site_body), "  End of PVObject pvSite", "",
        *meteo_lines, "", f"  Orientations, list of={len(norm_orients)} TOrientGroup", "",
        *(ln for orient in norm_orients for ln in synthesize_orientation_block(orient, version=version)),
        "  End of Orientations", "", "  PerezUsedInSimul=True", "  TranspositionAlgorithm=CircSolSeparate", "  ACLossesRef=STC", "",
        "  PVObject_PVMainArray=pvMainArray", "    Flags=$00100400", "  Flags2=$082A", f"  UFactor={uf:.1f}",
        "  Alpha=0.9", "  UFAverEffic=18.4", "  MVNbTransfos=1", "  UseSpectralCorr=False",
        *circuit_lines, *subarray_lines, "  End of PVObject pvMainArray", "",
        "  PVObject_System=pvSystem", "    Flags=$00", "    SystemType=Grid", "  End of PVObject pvSystem", "",
        *(list(shading_lines) if shading_lines is not None else list(_DEFAULT_SHADING_LINES)),
    ]
    if bifacial_lines:
        lines.extend(["", *bifacial_lines])
    if storage_lines:
        lines.extend(["", *storage_lines])
    lines.extend(["", "End of PVObject pvVCalcul", ""])

    body_text = "\r\n".join(lines)
    check_lines, arrays, i = body_text.splitlines(keepends=True), {}, 0
    while i < len(check_lines):
        if check_lines[i].strip() == "PVObject_=pvSubArray":
            end = next(j for j in range(i + 1, len(check_lines)) if check_lines[j].strip() == "End of PVObject pvSubArray")
            sid = next(int(m[2]) for k in range(i + 1, end) if (m := FIELD.fullmatch(check_lines[k].rstrip("\r\n"))) and m[1] == "SubArrayId")
            arrays[sid], i = (i, end), end + 1
            continue
        i += 1
    checked = grid_inventory(check_lines, arrays)
    if checked["counts_valid"] is not True:
        raise ValueError("Synthesized variant failed circuit count validation: " + "; ".join(checked["issues"] + checked["unchecked_checks"]))
    return ("\ufeff" + body_text) if with_bom else body_text


def synthesize_project_and_variant(
    store=None, *, project_comment: str = "Synthesized PV Project", variant_comment: str = "Initial simulation",
    site_name: str | None = None, met_name: str | None = None, orientations: list[dict] | None = None,
    subarrays: list[dict] | None = None, pan_name: str | None = None, ond_name: str | None = None,
    n_mod_serie: int = 15, n_string_ch: int = 2, n_inverter: int = 1, n_inv_mppt: int | None = None,
    independent_mppt: bool = True, pnom_ac_in_mppt: float = 7.5, tilt_deg: float = 25.0, azimuth_deg: float = 0.0,
    u_factor: float = 20.0, shading_lines: list[str] | None = None, bifacial_lines: list[str] | None = None,
    storage_lines: list[str] | None = None, version: str = SYNTH_VERSION, with_bom: bool = True,
    allow_missing_met: bool = False,
) -> dict:
    """Synthesize a matched (.PRJ, .VC0) pair directly in memory with zero demo templates."""
    resolved_pan, resolved_ond = pan_name or DEFAULT_PAN_NAME, ond_name or DEFAULT_OND_NAME
    if site_name is not None:
        safe_filename(site_name, ".SIT")
    if met_name is not None:
        safe_filename(met_name, ".MET")

    if store is not None and site_name is not None:
        if not (site_path := workspace_file(store.workspace, "Sites", site_name, ".SIT")).is_file():
            raise FileNotFoundError(f"Workspace SIT file is missing: {site_name}")
        resolved_site, site_identity, site_body = load_site_block(site_path)
    else:
        resolved_site = site_name or DEFAULT_PROJECT_SITE
        site_identity, site_body = placeholder_site_block(resolved_site, Path(resolved_site).stem or DEFAULT_PROJECT_SITE_NAME)

    if store is not None and met_name is not None:
        met_path = workspace_file(store.workspace, "Meteo", met_name, ".MET")
        if met_path.is_file():
            resolved_met, meteo_lines = load_meteo_header(met_path, site_identity, site_body)
        elif allow_missing_met:
            resolved_met, meteo_lines = met_name, synthesize_meteo_from_site(met_name, site_identity, site_body)
        else:
            raise FileNotFoundError(f"Workspace MET file is missing: {met_name}")
    else:
        resolved_met = met_name or DEFAULT_PROJECT_MET
        meteo_lines = synthesize_meteo_from_site(resolved_met, site_identity, site_body)

    if orientations is None:
        orientations = [{"id": 1, "field_type": "FixedPlane", "tilt_deg": tilt_deg, "azimuth_deg": azimuth_deg}]
    if subarrays is None:
        sub_mppt = n_inv_mppt if n_inv_mppt is not None else (n_string_ch if independent_mppt else 1)
        subarrays = [{
            "subarray_id": 1, "orientation_id": orientations[0]["id"], "comment": "PV Array",
            "pan_name": resolved_pan, "ond_name": resolved_ond, "n_mod_serie": n_mod_serie,
            "n_string_ch": n_string_ch, "n_inverter": n_inverter, "n_inv_mppt": sub_mppt,
            "independent_mppt": independent_mppt, "pnom_ac_in_mppt": pnom_ac_in_mppt,
        }]

    prj_text = synthesize_project_text(project_comment=project_comment, met_name=resolved_met, site_body=site_body, version=version, with_bom=with_bom)
    vc0_text = synthesize_variant_text(
        variant_comment=variant_comment, site_body=site_body, meteo_lines=meteo_lines, orientations=orientations,
        subarrays=subarrays, default_pan=resolved_pan, default_ond=resolved_ond, version=version, u_factor=u_factor,
        shading_lines=shading_lines, bifacial_lines=bifacial_lines, storage_lines=storage_lines, with_bom=with_bom,
    )
    return {
        "prj_text": prj_text, "vc0_text": vc0_text, "site_name": resolved_site, "site_identity": site_identity, "met_name": resolved_met,
        "orientations": [_normalize_orientation(item) for item in orientations],
        "subarrays": [_normalize_subarray(item, default_pan=resolved_pan, default_ond=resolved_ond) for item in subarrays],
    }


def _roof_plane_angles(mesh: dict) -> tuple[float, float]:
    """Return (tilt_deg, local_rotation_deg) for a planar upward-facing house roof mesh."""
    vertices, triangles, normals = mesh["vertices"], mesh["triangles"], []
    for a, b, c in triangles:
        va, vb, vc = vertices[a], vertices[b], vertices[c]
        ux, uy, uz = vb[0] - va[0], vb[1] - va[1], vb[2] - va[2]
        vx, vy, vz = vc[0] - va[0], vc[1] - va[1], vc[2] - va[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        if (length := math.hypot(nx, ny, nz)) <= _NORMAL_TOL:
            continue
        nx, ny, nz = nx / length, ny / length, nz / length
        normals.append((-nx, -ny, -nz) if nz < 0 else (nx, ny, nz))
    if not normals:
        raise ValueError(f"Roof mesh {mesh['name']!r} has no non-degenerate triangles")
    nx, ny, nz = normals[0]
    if nz <= _NORMAL_TOL:
        raise ValueError(f"Roof mesh {mesh['name']!r} is vertical and cannot host a PV plane")
    tilt_deg = round(math.degrees(math.acos(max(-1.0, min(1.0, nz)))), 6)
    return (0.0, 0.0) if tilt_deg < 1e-4 else (tilt_deg, round(math.degrees(math.atan2(nx, -ny)), 6))


def _infer_house_orientations_and_subarrays(request: dict, pocket: dict) -> tuple[list[dict], list[dict]]:
    """Derive orientations and subarrays from request + house meshes + layout when omitted."""
    meshes, layout = {m["name"]: m for m in pocket["meshes"]}, request["layout"]
    entries = layout.get("tables") if isinstance(layout, dict) and "tables" in layout else [layout]
    default_pan, default_ond = request.get("pan_name") or DEFAULT_PAN_NAME, request.get("ond_name") or DEFAULT_OND_NAME
    mode = request.get("shading_mode") or "linear"
    has_explicit_oids = any(isinstance(e, dict) and e.get("orientation_id") is not None for e in entries)

    if explicit_orients := request.get("orientations"):
        orientations = [_normalize_orientation(item) for item in explicit_orients]
    else:
        oid_to_mesh: dict[int, dict] = {}
        for entry in entries:
            oid = entry.get("orientation_id") if has_explicit_oids else 1
            if type(oid) is not int or oid < 1:
                raise ValueError("Each table orientation_id must be a positive integer")
            if (mesh_name := entry.get("roof_mesh")) not in meshes:
                raise ValueError(f"Unknown roof_mesh {mesh_name!r} in layout")
            oid_to_mesh.setdefault(oid, meshes[mesh_name])
        ordered_oids = sorted(oid_to_mesh)
        anchor_tilt, anchor_rot = _roof_plane_angles(oid_to_mesh[ordered_oids[0]])
        anchor_azim = _finite(request.get("anchor_azimuth_deg", 0.0), "anchor_azimuth_deg", minimum=-180.0, maximum=180.0)
        orientations = []
        for oid in ordered_oids:
            tilt, rot = _roof_plane_angles(oid_to_mesh[oid])
            azim = anchor_azim if oid == ordered_oids[0] else _normalize_azimuth(anchor_azim - (rot - anchor_rot))
            orientations.append({"id": oid, "field_type": "FixedPlane", "tilt_deg": tilt, "azimuth_deg": azim})

    if explicit_subs := request.get("subarrays"):
        subarrays = [_normalize_subarray(item, default_pan=default_pan, default_ond=default_ond) for item in explicit_subs]
    else:
        owner_totals: dict[int, dict] = {}
        for entry in entries:
            oid = entry.get("orientation_id") if has_explicit_oids else orientations[0]["id"]
            owner = entry.get("source_subarray_id") or oid
            total = int(entry["module_rows"]) * int(entry["module_columns"])
            slot = owner_totals.setdefault(owner, {"subarray_id": owner, "orientation_id": oid, "module_total": 0, "table_modules": []})
            slot["module_total"] += total
            slot["table_modules"].append(total)

        subarrays, grouped = [], len(owner_totals) > 1
        for owner in sorted(owner_totals):
            info = owner_totals[owner]
            total, table_mods = info["module_total"], info["table_modules"]
            if not grouped and mode == "module_layout" and request.get("string_layout") is None and len(table_mods) == 2 and table_mods[0] == table_mods[1]:
                n_mod_serie, n_string_ch, n_inverter, n_inv_mppt = table_mods[0], 2, 2, 2
            else:
                n_mod_serie, n_string_ch, n_inverter, n_inv_mppt = total, 1, 1, 1
            subarrays.append({
                "subarray_id": info["subarray_id"], "orientation_id": info["orientation_id"],
                "comment": "PV Array" if info["subarray_id"] == 1 else f"Sub-array #{info['subarray_id']}",
                "pan_name": default_pan, "ond_name": default_ond, "n_mod_serie": n_mod_serie,
                "n_string_ch": n_string_ch, "n_inverter": n_inverter, "n_inv_mppt": n_inv_mppt, "independent_mppt": True,
            })
    return orientations, subarrays


def synthesize_house_source(store, components, request: dict) -> dict:
    """Synthesize a clean zero-template (.PRJ, .VC0) source for run_house_task."""
    from pvsyst_auto.scene.author import house_profile

    site_name, met_name = request.get("site_name"), request.get("met_name")
    if not isinstance(site_name, str) or not site_name.strip():
        raise ValueError("Zero-template house task requires an explicit workspace site_name (.SIT)")
    if not isinstance(met_name, str) or not met_name.strip():
        raise ValueError("Zero-template house task requires an explicit workspace met_name (.MET)")
    pocket = house_profile(request["house"])
    if pocket["kind"] != "mesh":
        raise ValueError("The house task requires a complete AI/caller-authored mesh house")
    orientations, subarrays = _infer_house_orientations_and_subarrays(request, pocket)
    synth = synthesize_project_and_variant(
        store, project_comment=f"{Path(request['new_project']).stem} (zero-template)",
        variant_comment=request["variant_name"], site_name=site_name, met_name=met_name,
        orientations=orientations, subarrays=subarrays, pan_name=request.get("pan_name"),
        ond_name=request.get("ond_name"), with_bom=True,
    )
    synth["synthesized"] = True
    return synth


_MOVED_TO_TEMPLATE = {
    "_validate_ground_array_modules", "_synthesize_ground_array_shading",
    "synthesize_ground_shed_shading", "synthesize_ground_tracker_shading",
    "_resolve_pan_module_dimensions", "synthesize_utility_project",
}


def __getattr__(name: str):
    if name in _MOVED_TO_TEMPLATE:
        from pvsyst_auto.project import template
        return getattr(template, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

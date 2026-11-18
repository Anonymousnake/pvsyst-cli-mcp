"""Pure-text zero-template synthesizer for PVsyst 8.1.7 .PRJ and .VC0 files.

Generates complete, valid project and calculation-variant text directly from
declared site, meteo, component, orientation, subarray, and circuit inputs
without reading any DataRO _DEMO_* seed files.
"""
from __future__ import annotations

import math
from collections import defaultdict
from pvsyst_auto.components.store import FIELD
from pvsyst_auto.project.circuit import MAX_COUNT, inventory as grid_inventory
from pvsyst_auto.project.paths import safe_filename


def _pos_int(value, context: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_COUNT:
        raise ValueError(f"{context} must be a positive 32-bit integer")
    return value


SYNTH_VERSION = "8.1.7"
DEFAULT_PROJECT_SITE = "TemplateSite.SIT"
DEFAULT_PROJECT_SITE_NAME = "TemplateSite"
DEFAULT_PROJECT_MET = "TemplateMeteo.MET"
_NORMAL_TOL = 1e-6


def _fmt(value: float, decimals: int = 1) -> str:
    number = 0.0 if abs(value) < 1e-9 else float(value)
    return f"{number:.{decimals}f}"


def _clean_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ch in value for ch in ("\r", "\n", "\x00")):
        raise ValueError(f"{label} must be single-line non-empty text")
    return value.strip()


def placeholder_site_block(site_name: str = DEFAULT_PROJECT_SITE,
                           site_identity: str = DEFAULT_PROJECT_SITE_NAME) -> tuple[str, list[str]]:
    """Return minimal valid pvSite lines used only before real weather is bound."""
    safe_filename(site_name, ".SIT")
    ident = _clean_text(site_identity, "site_identity")
    return ident, [
        f"Comment={ident};;",
        f"Version={SYNTH_VERSION}",
        "Flags=$0006083F",
        f"NomF={site_name}",
        f"Site={ident}",
        "Latitude=0.0000",
        "Longitude=0.0000",
        "Altitude=0",
        "TimeZone=0.0",
        "HLeg=False",
        "UnitsDisp=kWh_m2_m",
        "Albedo=0.20",
    ]


def synthesize_tracker_shading_object(id: int, comment: str, origin: tuple[float, float, float], inclination: float) -> list[str]:
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

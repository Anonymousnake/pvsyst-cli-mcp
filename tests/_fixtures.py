"""Shared lightweight test fixtures, fake CLI base, and minimal PVsyst .VC0 builders."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from pvsyst_auto.native.cli import PVsystError

WORKSPACE_DIRS = (
    "Projects",
    "Sites",
    "Meteo",
    "UserData",
    "Models",
    "Results",
    "ComposPV/PVmodules",
    "ComposPV/Inverters",
)


def init_workspace(root: Path, *, extra_dirs: tuple[str, ...] = ()) -> Path:
    """Create standard PVsyst workspace subdirectories under ``root`` and return ``root``."""
    for sub in (*WORKSPACE_DIRS, *extra_dirs):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def write_utf8_crlf(path: Path, text: str, *, bom: bool = True) -> None:
    """Write text with CRLF line endings (and optional UTF-8 BOM)."""
    payload = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + payload)


def roof_mesh(
    name: str,
    ox: float,
    oy: float,
    tilt_deg: float,
    rot_deg: float = 0.0,
    *,
    x_bounds: tuple[float, float] = (-2.0, 40.0),
    y_bounds: tuple[float, float] = (-2.0, 25.0),
    z_offset: float = 0.0,
) -> dict[str, Any]:
    """Build a 2-triangle planar roof mesh at ``(ox, oy, z_offset)`` with given tilt and Z-rotation."""
    t, r = math.radians(tilt_deg), math.radians(rot_deg)

    def pt(x: float, y: float) -> list[float]:
        dy, dz = y * math.cos(t), y * math.sin(t)
        return [
            round(ox + x * math.cos(r) - dy * math.sin(r), 6),
            round(oy + x * math.sin(r) + dy * math.cos(r), 6),
            round(z_offset + dz, 6),
        ]

    x0, x1 = x_bounds
    y0, y1 = y_bounds
    return {
        "name": name,
        "vertices": [pt(x0, y0), pt(x1, y0), pt(x1, y1), pt(x0, y1)],
        "triangles": [[0, 1, 2], [0, 2, 3]],
    }


def sloped_roof_mesh(
    name: str,
    origin: list[float],
    wx: float,
    sy: float,
    tilt_deg: float,
    azim_deg: float,
) -> dict[str, Any]:
    """Build a 2-triangle sloped roof mesh rotated by ``-azim_deg`` around Z."""
    from pvsyst_auto.scene.author import _rotate_z

    t = math.radians(tilt_deg)
    pts = [
        (0.0, 0.0, 0.0),
        (wx, 0.0, 0.0),
        (wx, sy * math.cos(t), sy * math.sin(t)),
        (0.0, sy * math.cos(t), sy * math.sin(t)),
    ]
    verts = [[round(origin[k] + _rotate_z(p, -azim_deg)[k], 6) for k in range(3)] for p in pts]
    return {"name": name, "vertices": verts, "triangles": [[0, 1, 2], [0, 2, 3]]}


class FakeCliBase:
    """Minimal offline CLI double implementing the standard preflight/version/license hooks."""

    def __init__(
        self,
        workspace: Path | None = None,
        *,
        cli: Path | str | None = None,
        version: str = "8.1.7",
        license_status: str = "LICENSED",
        remaining_days: int = 30,
        remaining_executions: int | None = None,
        failure: str | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve() if workspace is not None else None
        self.cli = Path(cli).resolve() if cli is not None else None
        self._version = version
        self._license_status = license_status
        self._remaining_days = remaining_days
        self._remaining_executions = remaining_executions
        self.failure = failure
        self.calls: list[tuple[str, str]] = []

    def _require(self, command: str, option: str, *, allow_window_creation=None):
        self.calls.append((command, option))
        if self.failure:
            raise PVsystError(self.failure)
        return None

    def command_help(self, command: str = "", *, allow_window_creation=None) -> str:
        return ""

    def version(self, *, allow_window_creation=None) -> str:
        return self._version

    def capabilities(self, *, allow_window_creation=None) -> dict[str, list[str]]:
        return {"run-simulation": ["start-date"], "create-site": []}

    def license_info(self, *, allow_window_creation=None) -> dict[str, Any]:
        info: dict[str, Any] = {"status": self._license_status}
        if self._remaining_executions is not None:
            info["remaining_executions"] = self._remaining_executions
        else:
            info["remaining_days"] = self._remaining_days
        return info

    def _redact(self, text) -> str:
        return str(text)


def build_grid_vc0(
    *,
    orientations: list[dict[str, Any]],
    subarrays: list[dict[str, Any]],
    circuit_inverters: list[dict[str, Any]] | None = None,
    comment: str = "BaseSingle",
    version: str = "8.1.6",
    main_flags: str = "$00100400",
    main_flags2: str | None = "$082A",
    include_site: bool = True,
    include_site_met: bool = True,
    include_shading: bool = True,
    base_node_wrapper: bool = False,
    subarrays_wrapper: bool = True,
    orient_version: bool = False,
    mv_nb_transfos: int | None = None,
) -> str:
    """Render a minimal, valid grid-connected PVsyst ``.VC0`` text fixture."""
    lines = [
        "PVObject_=pvVCalcul",
        f"  Version={version}",
        "  Flags=$00100000",
        f"  Comment={comment}",
    ]
    if include_site:
        lines.extend([
            "  PVObject_SiteSimul=pvSite",
            "    NomF=Demo.SIT",
            "    Site=Demo",
            "    Latitude=45.0",
            "    Longitude=5.0",
            "    Altitude=10.0",
            "  End of PVObject pvSite",
            "  PVObject_MeteoSimul=pvMeteo",
            "    NomF=Demo.MET",
            "    SiteM=Demo",
        ])
        if include_site_met:
            lines.extend([
                "    PVObject_SiteMet=pvSite",
                "      NomF=Demo.SIT",
                "      Site=Demo",
                "      Latitude=45.0",
                "      Longitude=5.0",
                "      Altitude=10.0",
                "    End of PVObject pvSite",
            ])
        lines.append("  End of PVObject pvMeteo")

    lines.append(f"  Orientations, list of={len(orientations)} TOrientGroup")
    for orient in orientations:
        lines.append("    PVObject_=pvOrient")
        if orient_version:
            lines.append(f"      Version={version}")
        lines.extend([
            f"      NoOrient={orient['id']}",
            "      Flags=$00",
            f"      FieldType={orient.get('field_type', 'FixedPlane')}",
            f"      FieldTilt={orient['tilt']}",
            f"      FieldAzim={orient['azim']}",
            f"      BaseSlope={orient.get('base_slope', 0.0)}",
            "      UsesTiltAver=False",
            "      UsesAzimAver=False",
            "      UsesSlopeAver=False",
            "    End of TOrientGroup",
        ])
    lines.append("  End of Orientations")

    lines.extend([
        "  PVObject_PVMainArray=pvMainArray",
        f"    Flags={main_flags}",
    ])
    if main_flags2 is not None:
        lines.append(f"    Flags2={main_flags2}")
    if mv_nb_transfos is not None:
        lines.append(f"    MVNbTransfos={mv_nb_transfos}")

    lines.extend([
        "    PVObject_SystemCircuit=pvCircuit",
        f"      Version={version}",
        "      Flags=$00",
    ])
    if base_node_wrapper:
        lines.append("      BaseNode=Start")
    lines.append("      InjectionPointNode Start;")
    if base_node_wrapper:
        lines.extend(["        NElements=1", "        SubArrayId=0"])
    lines.append("        Children=Start")

    inv_specs = circuit_inverters
    if inv_specs is None:
        inv_specs = [
            {
                "n_elements": sa.get("n_inv", 1),
                "subarray_id": sa["id"],
                "name": sa.get("name", f"Array {sa['id']}"),
                "mppts": None if base_node_wrapper else [
                    {
                        "n_elements": sa.get("mppt_elements", 1),
                        "subarray_id": sa["id"],
                        "name": sa.get("name", f"Array {sa['id']}"),
                        "strings": sa.get("string_elements", 1),
                    }
                ],
                "strings": sa.get("n_str", 1),
            }
            for sa in subarrays
        ]

    for inv in inv_specs:
        lines.extend([
            "          InverterNode Start;",
            f"            NElements={inv.get('n_elements', 1)}",
        ])
        if inv.get("subarray_id") is not None:
            lines.append(f"            SubArrayId={inv['subarray_id']}")
        lines.extend([
            f"            SubArrayName={inv['name']}",
            "            Children=Start",
        ])
        if inv.get("mppts") is None:
            lines.extend([
                "              StringNode Start;",
                f"                NElements={inv.get('strings', 1)}",
                f"                SubArrayId={inv['subarray_id']}",
                f"                SubArrayName={inv['name']}",
                "              StringNode End;",
            ])
        else:
            for mppt in inv["mppts"]:
                lines.extend([
                    "              MPPTNode Start;",
                    f"                NElements={mppt.get('n_elements', 1)}",
                    f"                SubArrayId={mppt['subarray_id']}",
                    f"                SubArrayName={mppt['name']}",
                    "                Children=Start",
                    "                  StringNode Start;",
                    f"                    NElements={mppt.get('strings', 1)}",
                    f"                    SubArrayId={mppt['subarray_id']}",
                    f"                    SubArrayName={mppt['name']}",
                    "                  StringNode End;",
                    "                Children=End",
                    "              MPPTNode End;",
                ])
        lines.extend([
            "            Children=End",
            "          InverterNode End;",
        ])

    lines.extend([
        "        Children=End",
        "      InjectionPointNode End;",
    ])
    if base_node_wrapper:
        lines.append("      BaseNode=End")
    lines.append("    End of PVObject pvCircuit")

    if subarrays_wrapper:
        lines.append("    SubArrays=Start")
    for sa in subarrays:
        sa_name = sa.get("name", f"Array {sa['id']}")
        lines.extend([
            "      PVObject_=pvSubArray",
            f"        Comment={sa_name}",
            f"        Version={version}",
            f"        Flags={sa.get('flags', '$001E0402')}",
        ])
        if sa.get("flags2") is not None:
            lines.append(f"        Flags2={sa['flags2']}")
        lines.extend([
            f"        SubArrayId={sa['id']}",
            f"        PVModule={sa.get('pan', 'Mod600.PAN')}",
            f"        GInverter={sa.get('ond', 'Inv50.OND')}",
            f"        NoOrientation={sa['orient']}",
            f"        NModSerie={sa.get('n_mod', 12)}",
            f"        NStringCh={sa.get('n_str', 1)}",
            f"        NStrOrient1={sa.get('n_str', 1)}",
            f"        NInverter={sa.get('n_inv', 1)}",
            f"        NInvMPPT={sa.get('n_mppt', sa.get('n_inv', 1))}",
            f"        PowerSharingInverterId={sa.get('power_sharing_id', -1)}",
        ])
        for extra in sa.get("extra_lines") or ():
            lines.append(f"        {extra}")
        lines.append("      End of PVObject pvSubArray")
    if subarrays_wrapper:
        lines.append("    EndTags")
    lines.append("  End of PVObject pvMainArray")

    if include_shading:
        lines.extend([
            "  PVObject_Ombrage=pvShading",
            "    Flags=$00",
            "  End of PVObject pvShading",
        ])
    lines.extend([
        "  PVObject_System=pvSystem",
        "    Flags=$00",
        "    SystemType=Grid",
        "  End of PVObject pvSystem",
        "End of PVObject pvVCalcul",
    ])
    return "\n".join(lines) + "\n"

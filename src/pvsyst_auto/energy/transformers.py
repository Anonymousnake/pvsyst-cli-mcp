"""Zero-template MV / HV multi-stage transformer & AC collection network builder/editor."""
from __future__ import annotations

import math
import re
from typing import Any

from pvsyst_auto.components.editor import objects, replace_value
from pvsyst_auto.project.circuit import circuit_nodes, transformer_state
from pvsyst_auto.project.variant_writer import run_new_variant_transaction

# TMainArray.Flags (+0x84) bit definitions (PVsyst 8.1.7 verified)
MV_DS_FLAG = 0x00040000         # bit 18: IsMVTrfFromDataSheets
HV_FLAG = 0x00080000            # bit 19: IsHVTransfoDefined
MV_FLAG = 0x00400000            # bit 22: IsMVTransfoDefined
IRON_DEFAULT_FLAG = 0x00800000  # bit 23: IsTrfIronLossDefault
RES_DEFAULT_FLAG = 0x01000000   # bit 24: IsTrfResLossDefault
NIGHT_FLAG = 0x20000000         # bit 29: UsesTrfNightDisconnect

METALS = ("Copper", "Aluminium")

# Standard ISO 60228 cable cross sections (mm2) -> PVsyst WireNoSect index
ISO60228_SECTIONS: dict[int, int] = {
    10: 5,
    16: 6,
    25: 7,
    35: 8,
    50: 9,
    70: 10,
    95: 11,
    120: 12,
    150: 13,
    185: 14,
    240: 15,
    300: 16,
    400: 17,
    500: 18,
    630: 19,
    800: 20,
    1000: 21,
    1200: 22,
}

_MAIN_TRF_KEYS = {
    "VOutTransfo", "MVWireNoSect", "MVWireLength", "MVWireMetal",
    "VOutHVTransfo", "HVWireNoSect", "HVWireLength", "HVWireMetal",
    "MVNbTransfos", "TrfIronLossFrac", "TrfResLossFrac",
    "MVTrfDSNomPower", "MVTrfDSIronLoss", "MVTrfDSCopperLoss",
    "HVTrfDSNomPower", "HVTrfDSIronLoss", "HVTrfDSCopperLoss",
}


def _finite_pos(val: Any, name: str, *, allow_zero: bool = False) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    out = float(val)
    if not math.isfinite(out) or (out < 0.0 if allow_zero else out <= 0.0):
        op = ">= 0" if allow_zero else "> 0"
        raise ValueError(f"{name} must be {op}")
    return out


def _normalize_cable(raw: dict[str, Any] | None, label: str, *, default_section: int, default_metal: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{label}.cable must be an object")
    length_m = _finite_pos(raw.get("length_m", 0.0), f"{label}.cable.length_m", allow_zero=True)
    if length_m > 100000.0:
        raise ValueError(f"{label}.cable.length_m must be <= 100000 m")
    metal = str(raw.get("metal", default_metal))
    if metal not in METALS:
        raise ValueError(f"{label}.cable.metal must be one of {METALS}")
    sec_raw = raw.get("section_mm2", default_section)
    if isinstance(sec_raw, bool) or not isinstance(sec_raw, int) or sec_raw not in ISO60228_SECTIONS:
        raise ValueError(f"{label}.cable.section_mm2 must be one of {sorted(ISO60228_SECTIONS)}")
    return {
        "length_m": round(length_m, 1),
        "metal": metal,
        "section_mm2": sec_raw,
        "std_section": f"ISO60228_{sec_raw}",
        "wire_no_sect": ISO60228_SECTIONS[sec_raw],
    }


def _normalize_mv(raw: dict[str, Any], default_ref_kw: float) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("mv must be an object")
    count = raw.get("count", 1)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 200:
        raise ValueError("mv.count must be an integer in 1..200")
    v_kv = _finite_pos(raw.get("output_voltage_kv", 20.0), "mv.output_voltage_kv")
    if not 1.0 <= v_kv <= 66.0:
        raise ValueError("mv.output_voltage_kv must be in [1.0, 66.0] kV")
    ref_kw = _finite_pos(raw["stc_ref_power_kw"], "mv.stc_ref_power_kw") if raw.get("stc_ref_power_kw") is not None else default_ref_kw

    ds_nom = raw.get("nominal_power_kva")
    ds_iron = raw.get("iron_loss_kw")
    ds_copper = raw.get("copper_loss_kw")
    has_ds = any(v is not None for v in (ds_nom, ds_iron, ds_copper))
    if has_ds:
        if any(v is None for v in (ds_nom, ds_iron, ds_copper)):
            raise ValueError("mv datasheet mode requires nominal_power_kva, iron_loss_kw, and copper_loss_kw together")
        ds_nom_f = _finite_pos(ds_nom, "mv.nominal_power_kva")
        ds_iron_f = _finite_pos(ds_iron, "mv.iron_loss_kw", allow_zero=True)
        ds_copper_f = _finite_pos(ds_copper, "mv.copper_loss_kw", allow_zero=True)
        iron_frac = ds_iron_f / ref_kw if ref_kw > 0 else 0.001
        copper_frac = ds_copper_f * (ref_kw / (ds_nom_f * ds_nom_f)) if ds_nom_f > 0 else 0.01
    else:
        ds_nom_f = ds_iron_f = ds_copper_f = None
        iron_frac = _finite_pos(raw.get("iron_loss_frac", 0.001), "mv.iron_loss_frac", allow_zero=True)
        copper_frac = _finite_pos(raw.get("copper_loss_frac", 0.010), "mv.copper_loss_frac", allow_zero=True)
    if iron_frac > 0.05 or copper_frac > 0.15:
        raise ValueError("MV transformer loss fractions out of realistic bounds (iron <= 5%, copper <= 15%)")

    night = raw.get("night_disconnect", False)
    if type(night) is not bool:
        raise ValueError("mv.night_disconnect must be a strict boolean")
    cable = _normalize_cable(raw.get("cable"), "mv", default_section=150, default_metal="Copper")
    return {
        "count": count,
        "output_voltage_kv": v_kv,
        "output_voltage_v": int(round(v_kv * 1000.0)),
        "stc_ref_power_kw": round(ref_kw, 5),
        "iron_loss_frac": round(iron_frac, 5),
        "iron_loss_kw": round(ref_kw * iron_frac, 5),
        "copper_loss_frac": round(copper_frac, 5),
        "from_datasheet": has_ds,
        "ds_nom_power": round(ds_nom_f, 5) if ds_nom_f is not None else None,
        "ds_iron_loss": round(ds_iron_f, 5) if ds_iron_f is not None else None,
        "ds_copper_loss": round(ds_copper_f, 5) if ds_copper_f is not None else None,
        "night_disconnect": night,
        "cable": cable,
    }


def _normalize_hv(raw: dict[str, Any], default_ref_kw: float) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("hv must be an object")
    v_kv = _finite_pos(raw.get("output_voltage_kv", 110.0), "hv.output_voltage_kv")
    if not 30.0 <= v_kv <= 500.0:
        raise ValueError("hv.output_voltage_kv must be in [30.0, 500.0] kV")
    ref_kw = _finite_pos(raw["stc_ref_power_kw"], "hv.stc_ref_power_kw") if raw.get("stc_ref_power_kw") is not None else default_ref_kw
    # PVsyst 8.1.7 ALWAYS requires HV datasheet specifications (HVTrfDSNomPower / IronLoss / CopperLoss)
    ds_nom = _finite_pos(raw["nominal_power_kva"], "hv.nominal_power_kva") if raw.get("nominal_power_kva") is not None else round(ref_kw * 1.15, 2)
    iron_frac_in = raw.get("iron_loss_frac")
    copper_frac_in = raw.get("copper_loss_frac")
    if raw.get("iron_loss_kw") is not None:
        ds_iron = _finite_pos(raw["iron_loss_kw"], "hv.iron_loss_kw", allow_zero=True)
        iron_frac = ds_iron / ref_kw if ref_kw > 0 else 0.0007
    else:
        iron_frac = _finite_pos(iron_frac_in if iron_frac_in is not None else 0.0007, "hv.iron_loss_frac", allow_zero=True)
        ds_iron = round(ds_nom * iron_frac, 5)
    if raw.get("copper_loss_kw") is not None:
        ds_copper = _finite_pos(raw["copper_loss_kw"], "hv.copper_loss_kw", allow_zero=True)
        copper_frac = ds_copper * (ref_kw / (ds_nom * ds_nom)) if ds_nom > 0 else 0.0065
    else:
        copper_frac = _finite_pos(copper_frac_in if copper_frac_in is not None else 0.0065, "hv.copper_loss_frac", allow_zero=True)
        ds_copper = round(ds_nom * copper_frac, 5)
    if iron_frac > 0.05 or copper_frac > 0.15:
        raise ValueError("HV transformer loss fractions out of realistic bounds (iron <= 5%, copper <= 15%)")
    night = raw.get("night_disconnect", False)
    if type(night) is not bool:
        raise ValueError("hv.night_disconnect must be a strict boolean")
    cable = _normalize_cable(raw.get("cable"), "hv", default_section=240, default_metal="Aluminium")
    return {
        "output_voltage_kv": v_kv,
        "output_voltage_v": int(round(v_kv * 1000.0)),
        "stc_ref_power_kw": round(ref_kw, 5),
        "iron_loss_frac": round(iron_frac, 5),
        "iron_loss_kw": round(ref_kw * iron_frac, 5),
        "copper_loss_frac": round(copper_frac, 5),
        "ds_nom_power": round(ds_nom, 5),
        "ds_iron_loss": round(ds_iron, 5),
        "ds_copper_loss": round(ds_copper, 5),
        "night_disconnect": night,
        "cable": cable,
    }


def _infer_ref_power_kw(lines: list[str], default_ref_kw: float | None = None) -> float:
    if default_ref_kw is not None and default_ref_kw > 0:
        return float(default_ref_kw)
    blocks = objects("".join(lines))
    total_pnom = 0.0
    for b in blocks:
        if b.object_type == "pvSubArray":
            if "PNomSizing" in b.fields:
                try:
                    val = float(b.one("PNomSizing")[1])
                    if val > 0:
                        total_pnom += val
                except (ValueError, TypeError):
                    pass
    if total_pnom > 0:
        return total_pnom
    # Fallback to existing TransformerNode STCRefPower if present
    for b_line in lines:
        m = re.match(r"^\s*STCRefPower=([0-9.]+)\s*$", b_line)
        if m:
            val = float(m.group(1))
            if val > 0:
                return val
    return 1000.0


def _cable_block(cable: dict[str, Any] | None, indent: str, eol: str) -> list[str]:
    if not cable or cable["length_m"] <= 0:
        return []
    length_str = f"{cable['length_m']:g}"
    return [
        f"{indent}CableDefinition=Start{eol}",
        f"{indent}    CustomResistance=False{eol}",
        f"{indent}    Length={length_str} m{eol}",
        f"{indent}    CableType=Start{eol}",
        f"{indent}        Material={cable['metal']}{eol}",
        f"{indent}        StdCrossSect={cable['std_section']}{eol}",
        f"{indent}    CableType=End{eol}",
        f"{indent}CableDefinition=End{eol}",
    ]


def _extract_inverter_subtrees(lines: list[str]) -> tuple[int, int, list[list[str]], str]:
    """Locate BaseNode..End in pvCircuit and extract all InverterNode raw subtrees."""
    roots = circuit_nodes(lines)
    if len(roots) != 1 or roots[0].kind != "InjectionPointNode":
        raise ValueError("pvCircuit must have a single root InjectionPointNode")
    inj = roots[0]
    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    inv_blocks: list[list[str]] = []

    def collect_inverters(node):
        if node.kind == "InverterNode":
            raw = lines[node.line - 1:node.end_line]
            # Normalize minimum indentation to 0 so we can cleanly re-indent at any nesting depth
            non_empty = [ln for ln in raw if ln.strip()]
            min_indent = min(len(ln) - len(ln.lstrip(" ")) for ln in non_empty) if non_empty else 0
            inv_blocks.append([ln[min_indent:] if len(ln) >= min_indent else ln for ln in raw])
            return
        for ch in node.children:
            collect_inverters(ch)

    collect_inverters(inj)
    if not inv_blocks:
        raise ValueError("pvCircuit contains no InverterNode to attach under TransformerNode")
    return inj.line - 1, inj.end_line, inv_blocks, eol


def _format_kv(v_kv: float) -> str:
    return f"{v_kv:g} kV"


def _render_transformer_node(
    spec: dict[str, Any],
    *,
    is_hv: bool,
    children_lines: list[str],
    indent: str,
    eol: str,
) -> list[str]:
    out = [f"{indent}TransformerNode Start;{eol}"]
    inner = indent + "  "
    out.extend(_cable_block(spec.get("cable"), inner, eol))
    out.append(f"{inner}Children=Start{eol}")
    out.extend(children_lines)
    out.append(f"{inner}Children=End{eol}")
    if not is_hv and spec.get("count", 1) > 1:
        out.append(f"{indent}NTransformers={spec['count']}{eol}")
    out.append(f"{indent}OutputVoltage={_format_kv(spec['output_voltage_kv'])}{eol}")
    out.append(f"{indent}STCRefPower={spec['stc_ref_power_kw']:.5f}{eol}")
    out.append(f"{indent}STCIronLossFrac={spec['iron_loss_frac']:.5f}{eol}")
    out.append(f"{indent}STCIronLoss={spec['iron_loss_kw']:.5f}{eol}")
    out.append(f"{indent}STCCopperLossFrac={spec['copper_loss_frac']:.5f}{eol}")
    if is_hv or spec.get("from_datasheet"):
        out.append(f"{indent}DSNomPower={spec['ds_nom_power']:.5f}{eol}")
        out.append(f"{indent}DSIronLoss={spec['ds_iron_loss']:.5f}{eol}")
        out.append(f"{indent}DSCopperLoss={spec['ds_copper_loss']:.5f}{eol}")
    out.append(f"{indent}NightDisconnect={'True' if spec['night_disconnect'] else 'False'}{eol}")
    out.append(f"{indent}IsHVTransformer={'True' if is_hv else 'False'}{eol}")
    out.append(f"{indent}TransformerNode End;{eol}")
    return out


def _strip_varlist_and_clear_sim_flag(lines: list[str]) -> list[str]:
    """Drop stale saved simulation cache (pvVarList) so PVsystCLI recomputes fresh losses."""
    out: list[str] = []
    in_varlist = False
    for line in lines:
        stripped = line.strip()
        if not in_varlist and stripped == "PVObject_VarList=pvVarList":
            in_varlist = True
            continue
        if in_varlist:
            if stripped == "End of PVObject pvVarList":
                in_varlist = False
            continue
        out.append(line)
    return out


def apply_transformers_to_variant_lines(
    lines: list[str],
    *,
    mv: dict[str, Any] | None = None,
    hv: dict[str, Any] | None = None,
    remove_transformers: bool = False,
    default_ref_power_kw: float | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Pure text transformer builder/editor operating on parsed .VCx lines."""
    if remove_transformers and (mv is not None or hv is not None):
        raise ValueError("Cannot combine remove_transformers=True with mv/hv specifications")
    if not remove_transformers and mv is None:
        raise ValueError("Provide mv transformer specification (with optional hv), or set remove_transformers=True")

    blocks = objects("".join(lines))
    mains = [b for b in blocks if b.object_type == "pvMainArray"]
    systems = [b for b in blocks if b.object_type == "pvSystem"]
    if len(mains) != 1 or not mains[0].end_line:
        raise ValueError("Expected exactly one complete pvMainArray block")
    if len(systems) == 1 and "SystemType" in systems[0].fields and systems[0].one("SystemType")[1] != "Grid":
        raise ValueError("MV/HV transformers are only supported on Grid-connected variants")
    main = mains[0]
    flags_idx, raw_flags = main.one("Flags")
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw_flags):
        raise ValueError("pvMainArray Flags must be hex ($XXXXXXXX)")
    flags_val = int(raw_flags[1:], 16)

    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    inj_start, inj_end, inv_blocks, _ = _extract_inverter_subtrees(lines)
    base_indent = lines[inj_start][:len(lines[inj_start]) - len(lines[inj_start].lstrip(" "))]

    ref_kw = _infer_ref_power_kw(lines, default_ref_power_kw)
    mv_norm = None if remove_transformers else _normalize_mv(mv, ref_kw)  # type: ignore[arg-type]
    hv_norm = None if (remove_transformers or hv is None) else _normalize_hv(hv, ref_kw)

    # 1. Build new InjectionPointNode tree
    new_inj: list[str] = [f"{base_indent}InjectionPointNode Start;{eol}"]
    child_indent = base_indent + "  "
    new_inj.append(f"{child_indent}Children=Start{eol}")
    if remove_transformers:
        inv_indent = child_indent + "  "
        for blk in inv_blocks:
            for ln in blk:
                new_inj.append((inv_indent + ln) if ln.strip() else ln)
    elif hv_norm is None:
        assert mv_norm is not None
        mv_indent = child_indent + "  "
        inv_indent = mv_indent + "    "
        inv_lines: list[str] = []
        for blk in inv_blocks:
            for ln in blk:
                inv_lines.append((inv_indent + ln) if ln.strip() else ln)
        new_inj.extend(_render_transformer_node(mv_norm, is_hv=False, children_lines=inv_lines, indent=mv_indent, eol=eol))
    else:
        assert mv_norm is not None
        hv_indent = child_indent + "  "
        mv_indent = hv_indent + "    "
        inv_indent = mv_indent + "    "
        inv_lines = []
        for blk in inv_blocks:
            for ln in blk:
                inv_lines.append((inv_indent + ln) if ln.strip() else ln)
        mv_lines = _render_transformer_node(mv_norm, is_hv=False, children_lines=inv_lines, indent=mv_indent, eol=eol)
        new_inj.extend(_render_transformer_node(hv_norm, is_hv=True, children_lines=mv_lines, indent=hv_indent, eol=eol))
    new_inj.append(f"{child_indent}Children=End{eol}")
    new_inj.append(f"{base_indent}InjectionPointNode End;{eol}")

    # 2. Replace InjectionPointNode in candidate lines
    cand = list(lines[:inj_start]) + new_inj + list(lines[inj_end:])

    # 3. Update pvMainArray Flags and header transformer scalar tags
    # Re-locate pvMainArray header (lines before PVObject_SystemCircuit=pvCircuit)
    main_start = next(i for i, ln in enumerate(cand) if ln.strip() == "PVObject_PVMainArray=pvMainArray")
    circ_start = next(i for i in range(main_start, len(cand)) if ln_strip(cand[i]) == "PVObject_SystemCircuit=pvCircuit")

    # Compute updated pvMainArray.Flags
    flags_val &= ~(MV_FLAG | HV_FLAG | MV_DS_FLAG | NIGHT_FLAG)
    if not remove_transformers:
        assert mv_norm is not None
        flags_val |= MV_FLAG
        if mv_norm["from_datasheet"]:
            flags_val |= MV_DS_FLAG
        if mv_norm["night_disconnect"]:
            flags_val |= NIGHT_FLAG
        if hv_norm is not None:
            flags_val |= HV_FLAG
        # Clear default-loss bits so custom iron/copper fractions are respected
        flags_val &= ~(IRON_DEFAULT_FLAG | RES_DEFAULT_FLAG)

    # Rebuild pvMainArray header keeping non-transformer lines intact
    header_indent = "  "
    kept_header: list[str] = []
    insert_pos: int | None = None
    for idx in range(main_start, circ_start):
        ln = cand[idx]
        s = ln.strip()
        if idx == main_start:
            kept_header.append(ln)
            continue
        if s.startswith("Flags="):
            kept_header.append(replace_value(ln, f"${flags_val:08X}"))
            continue
        key = s.split("=", 1)[0] if "=" in s else ""
        if key in _MAIN_TRF_KEYS:
            if insert_pos is None:
                insert_pos = len(kept_header)
            continue
        if key == "UseSpectralCorr" and insert_pos is None:
            insert_pos = len(kept_header)
        kept_header.append(ln)

    if insert_pos is None:
        insert_pos = len(kept_header)

    trf_header: list[str] = []
    if remove_transformers:
        trf_header.append(f"{header_indent}MVNbTransfos=0{eol}")
    else:
        assert mv_norm is not None
        mv_cab = mv_norm.get("cable")
        mv_sect = mv_cab["wire_no_sect"] if mv_cab else 13
        mv_len = mv_cab["length_m"] if mv_cab else 0.0
        mv_metal = mv_cab["metal"] if mv_cab else "Copper"
        trf_header.extend([
            f"{header_indent}VOutTransfo={mv_norm['output_voltage_v']}{eol}",
            f"{header_indent}MVWireNoSect={mv_sect}{eol}",
            f"{header_indent}MVWireLength={mv_len:.1f}{eol}",
            f"{header_indent}MVWireMetal={mv_metal}{eol}",
        ])
        if hv_norm is not None:
            hv_cab = hv_norm.get("cable")
            hv_sect = hv_cab["wire_no_sect"] if hv_cab else 15
            hv_len = hv_cab["length_m"] if hv_cab else 0.0
            hv_metal = hv_cab["metal"] if hv_cab else "Aluminium"
            trf_header.extend([
                f"{header_indent}VOutHVTransfo={hv_norm['output_voltage_v']}{eol}",
                f"{header_indent}HVWireNoSect={hv_sect}{eol}",
                f"{header_indent}HVWireLength={hv_len:.1f}{eol}",
                f"{header_indent}HVWireMetal={hv_metal}{eol}",
            ])
        trf_header.extend([
            f"{header_indent}MVNbTransfos={mv_norm['count']}{eol}",
            f"{header_indent}TrfIronLossFrac={mv_norm['iron_loss_frac']:.5f}{eol}",
            f"{header_indent}TrfResLossFrac={mv_norm['copper_loss_frac']:.5f}{eol}",
        ])
        if mv_norm["from_datasheet"]:
            trf_header.extend([
                f"{header_indent}MVTrfDSNomPower={mv_norm['ds_nom_power']:.2f}{eol}",
                f"{header_indent}MVTrfDSIronLoss={mv_norm['ds_iron_loss']:.4f}{eol}",
                f"{header_indent}MVTrfDSCopperLoss={mv_norm['ds_copper_loss']:.4f}{eol}",
            ])
        if hv_norm is not None:
            trf_header.extend([
                f"{header_indent}HVTrfDSNomPower={hv_norm['ds_nom_power']:.2f}{eol}",
                f"{header_indent}HVTrfDSIronLoss={hv_norm['ds_iron_loss']:.4f}{eol}",
                f"{header_indent}HVTrfDSCopperLoss={hv_norm['ds_copper_loss']:.4f}{eol}",
            ])

    final_header = kept_header[:insert_pos] + trf_header + kept_header[insert_pos:]
    cand = cand[:main_start] + final_header + cand[circ_start:]
    cand = _strip_varlist_and_clear_sim_flag(cand)

    # Verify structural integrity immediately
    state = transformer_state(cand)
    if state["issues"]:
        raise ValueError("Generated transformer circuit failed validation: " + "; ".join(state["issues"]))
    expected_layers = 0 if remove_transformers else (2 if hv_norm is not None else 1)
    if state["count"] != expected_layers:
        raise ValueError(f"Expected {expected_layers} TransformerNode layer(s), found {state['count']}")

    summary = {
        "remove_transformers": remove_transformers,
        "main_array_flags": f"${flags_val:08X}",
        "mv": mv_norm,
        "hv": hv_norm,
        "transformer_state": state,
    }
    return cand, summary


def ln_strip(s: str) -> str:
    return s.strip()


def apply_transformers_to_variant_text(
    vc_text: str,
    *,
    mv: dict[str, Any] | None = None,
    hv: dict[str, Any] | None = None,
    remove_transformers: bool = False,
    default_ref_power_kw: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """Convenience wrapper operating on a full .VCx text string."""
    lines = vc_text.splitlines(keepends=True)
    new_lines, summary = apply_transformers_to_variant_lines(
        lines,
        mv=mv,
        hv=hv,
        remove_transformers=remove_transformers,
        default_ref_power_kw=default_ref_power_kw,
    )
    return "".join(new_lines), summary


def configure_transformers(
    store,
    *,
    project: str,
    source_variant: str,
    new_variant: str,
    mv: dict[str, Any] | None = None,
    hv: dict[str, Any] | None = None,
    remove_transformers: bool = False,
    dry_run: bool = True,
    variant_name: str | None = None,
    add_utf8_bom: bool = False,
) -> dict[str, Any]:
    """Create a new variant with zero-template MV / MV+HV transformers and AC collection cables."""
    def preflight():
        if type(remove_transformers) is not bool:
            raise ValueError("remove_transformers must be a strict boolean")
        if remove_transformers and (mv is not None or hv is not None):
            raise ValueError("Cannot specify mv/hv when remove_transformers=True")
        if not remove_transformers and mv is None:
            raise ValueError("Provide mv specification (and optional hv), or set remove_transformers=True")

    def build(*, lines, **_):
        candidate_lines, summary = apply_transformers_to_variant_lines(
            lines,
            mv=mv,
            hv=hv,
            remove_transformers=remove_transformers,
        )

        def verify(checked, _, res):
            verified = transformer_state(checked)
            if verified["issues"]:
                raise ValueError("Verified variant transformer circuit has issues: " + "; ".join(verified["issues"]))
            res["transformers"] = {
                "remove_transformers": remove_transformers,
                "main_array_flags": summary["main_array_flags"],
                "mv": summary["mv"],
                "hv": summary["hv"],
                "layers": verified["count"],
                "nested_hv_mv": verified["nested"],
            }

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
        source_changed_error="Source variant changed before transformer configuration",
        write_failure_warning="New variant may contain a partial artifact; original source is preserved.",
    )

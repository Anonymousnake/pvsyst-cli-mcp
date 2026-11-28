"""Zero-template MV / HV multi-stage transformer & AC collection network builder/editor."""
from __future__ import annotations

from decimal import Decimal
import math
import re
from typing import Any

from pvsyst_auto.components.editor import objects, replace_value
from pvsyst_auto.native.headless import strip_result_cache
from pvsyst_auto.project.circuit import circuit_nodes, transformer_state
from pvsyst_auto.project.variant_updates import run_new_variant_transaction
from pvsyst_auto.validation import finite_number

MV_DS_FLAG, HV_FLAG, MV_FLAG = 0x00040000, 0x00080000, 0x00400000
IRON_DEFAULT_FLAG, RES_DEFAULT_FLAG, NIGHT_FLAG = 0x00800000, 0x01000000, 0x20000000
NATIVE_MIN_POWER_RATIO, NATIVE_MAX_POWER_RATIO = 0.80, 2.00
NATIVE_MAX_IRON_LOSS_FRAC, NATIVE_MAX_COPPER_LOSS_FRAC = 0.01, 0.05
NATIVE_MIN_DATASHEET_VALUE = 1e-9
_GRID_LIMIT_ENABLED_BIT, _INDEPENDENT_MPPT_BIT, _BOUND_EPS = 0x01000000, 0x400, 1e-9
METALS = ("Copper", "Aluminium")
ISO60228_SECTIONS: dict[int, int] = dict(zip((10, 16, 25, 35, 50, 70, 95, 120, 150, 185, 240, 300, 400, 500, 630, 800, 1000, 1200), range(5, 23)))
_MAIN_TRF_KEYS = {
    "VOutTransfo", "MVWireNoSect", "MVWireLength", "MVWireMetal",
    "VOutHVTransfo", "HVWireNoSect", "HVWireLength", "HVWireMetal",
    "MVNbTransfos", "TrfIronLossFrac", "TrfResLossFrac",
    "MVTrfDSNomPower", "MVTrfDSIronLoss", "MVTrfDSCopperLoss",
    "HVTrfDSNomPower", "HVTrfDSIronLoss", "HVTrfDSCopperLoss",
}
def ln_strip(s: str) -> str:
    return s.strip()


def _finite_pos(val: Any, name: str, *, allow_zero: bool = False) -> float:
    return finite_number(val, name, minimum=0, strict_min=not allow_zero)


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
    return {"length_m": round(length_m, 1), "metal": metal, "section_mm2": sec_raw, "std_section": f"ISO60228_{sec_raw}", "wire_no_sect": ISO60228_SECTIONS[sec_raw]}


def _check_native_power_bounds(*, stage: str, ds_nom_kva: float, inverter_ac_kw: float | None, grid_limit_kw: float | None, count: int = 1) -> None:
    if inverter_ac_kw is None or inverter_ac_kw <= 0:
        return
    ac_per_trf = inverter_ac_kw / count
    min_base_total = min(inverter_ac_kw, grid_limit_kw) if (grid_limit_kw is not None and grid_limit_kw > 0) else inverter_ac_kw
    min_kva, max_kva = NATIVE_MIN_POWER_RATIO * (min_base_total / count), NATIVE_MAX_POWER_RATIO * ac_per_trf
    unit_suffix, div_suffix = (f" per transformer (count={count})" if count > 1 else ""), (f" / {count}" if count > 1 else "")
    if ds_nom_kva < min_kva - _BOUND_EPS:
        basis_label = f"min(PNomInvAC={inverter_ac_kw:g} kW, PLimGrid={grid_limit_kw:g} kW)" if (grid_limit_kw is not None and grid_limit_kw < inverter_ac_kw) else f"PNomInvAC={inverter_ac_kw:g} kW"
        raise ValueError(f"{stage} transformer nominal_power_kva ({ds_nom_kva:g} kVA{unit_suffix}) is below PVsyst 8.1.7 native undersizing limit ({min_kva:g} kVA = 80% of {basis_label}{div_suffix}; TransfoPowerLTInverterXX/GridLimXX).")
    if ds_nom_kva > max_kva + _BOUND_EPS:
        raise ValueError(f"{stage} transformer nominal_power_kva ({ds_nom_kva:g} kVA{unit_suffix}) exceeds PVsyst 8.1.7 native oversizing limit ({max_kva:g} kVA = 200% of PNomInvAC={inverter_ac_kw:g} kW{div_suffix}; TransfoPowerGTInverterXX).")


def _stage_common(raw: dict[str, Any], label: str, default_ref_kw: float | None, *, default_kv: float, kv_range: tuple[float, float], default_sec: int, default_metal: str):
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    v_kv = _finite_pos(raw.get("output_voltage_kv", default_kv), f"{label}.output_voltage_kv")
    if not kv_range[0] <= v_kv <= kv_range[1]:
        raise ValueError(f"{label}.output_voltage_kv must be in [{kv_range[0]:.1f}, {kv_range[1]:.1f}] kV")
    night = raw.get("night_disconnect", False)
    if type(night) is not bool:
        raise ValueError(f"{label}.night_disconnect must be a strict boolean")
    cable = _normalize_cable(raw.get("cable"), label, default_section=default_sec, default_metal=default_metal)
    ref = raw.get("stc_ref_power_kw") if raw.get("stc_ref_power_kw") is not None else default_ref_kw
    if ref is None:
        raise ValueError(f"{label}.stc_ref_power_kw is required when the source has no reference power (cannot infer from variant or .PAN files)")
    return v_kv, night, cable, _finite_pos(ref, f"{label}.stc_ref_power_kw")


def _normalize_mv(raw: dict[str, Any], default_ref_kw: float | None, *, inverter_ac_kw: float | None = None, grid_limit_kw: float | None = None) -> dict[str, Any]:
    v_kv, night, cable, ref_kw = _stage_common(raw, "mv", default_ref_kw, default_kv=20.0, kv_range=(1.0, 66.0), default_sec=150, default_metal="Copper")
    count = raw.get("count", 1)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 200:
        raise ValueError("mv.count must be an integer in 1..200")
    ref_per_trf = ref_kw / count
    ac_per_trf = (inverter_ac_kw / count) if (inverter_ac_kw is not None and inverter_ac_kw > 0) else None
    ds_nom, ds_iron, ds_copper = raw.get("nominal_power_kva"), raw.get("iron_loss_kw"), raw.get("copper_loss_kw")
    has_ds = any(v is not None for v in (ds_nom, ds_iron, ds_copper))
    if has_ds:
        if any(v is None for v in (ds_nom, ds_iron, ds_copper)):
            raise ValueError("mv datasheet mode requires nominal_power_kva, iron_loss_kw, and copper_loss_kw together")
        ds_nom_f, ds_iron_f, ds_copper_f = _finite_pos(ds_nom, "mv.nominal_power_kva"), _finite_pos(ds_iron, "mv.iron_loss_kw"), _finite_pos(ds_copper, "mv.copper_loss_kw")
        if min(ds_nom_f, ds_iron_f, ds_copper_f) < NATIVE_MIN_DATASHEET_VALUE:
            raise ValueError("mv datasheet nominal_power_kva, iron_loss_kw, and copper_loss_kw must be strictly positive (>= 1e-9; PVsyst TransfoDataSheetsNotWellDef)")
        _check_native_power_bounds(stage="MV", ds_nom_kva=ds_nom_f, inverter_ac_kw=inverter_ac_kw, grid_limit_kw=grid_limit_kw, count=count)
        stc_iron_frac, stc_copper_frac = ds_iron_f / ref_per_trf, ds_copper_f * (ref_per_trf / (ds_nom_f * ds_nom_f))
        basis_per_trf = ac_per_trf if ac_per_trf is not None else ref_per_trf
        main_iron_frac, main_copper_frac = ds_iron_f / basis_per_trf, ds_copper_f * (basis_per_trf / (ds_nom_f * ds_nom_f))
        total_iron_kw, loss_basis, loss_basis_kw = count * ds_iron_f, "datasheet_per_transformer", count * ds_nom_f
    else:
        ds_nom_f = ds_iron_f = ds_copper_f = None
        main_iron_frac = _finite_pos(raw.get("iron_loss_frac", 0.001), "mv.iron_loss_frac", allow_zero=True)
        main_copper_frac = _finite_pos(raw.get("copper_loss_frac", 0.010), "mv.copper_loss_frac", allow_zero=True)
        has_ac = inverter_ac_kw is not None and inverter_ac_kw > 0
        eff_basis_kw = inverter_ac_kw if has_ac else ref_kw
        total_iron_kw = eff_basis_kw * main_iron_frac
        stc_iron_frac, stc_copper_frac = total_iron_kw / ref_kw, main_copper_frac * (ref_kw / eff_basis_kw)
        loss_basis, loss_basis_kw = ("inverter_ac_nominal" if has_ac else "stc_ref_power_fallback"), eff_basis_kw
    if stc_iron_frac > NATIVE_MAX_IRON_LOSS_FRAC + _BOUND_EPS or stc_copper_frac > NATIVE_MAX_COPPER_LOSS_FRAC + _BOUND_EPS:
        ac_suffix = f", inverter_ac_power_kw={inverter_ac_kw:g} kW" if inverter_ac_kw is not None else ""
        raise ValueError(f"MV transformer loss fractions exceed PVsyst 8.1.7 native limits (iron <= {NATIVE_MAX_IRON_LOSS_FRAC * 100:g}%, copper <= {NATIVE_MAX_COPPER_LOSS_FRAC * 100:g}%; got iron={stc_iron_frac * 100:.2f}%, copper={stc_copper_frac * 100:.2f}% using stc_ref_power_kw={ref_kw:g} kW{ac_suffix})")
    return {
        "count": count, "output_voltage_kv": v_kv, "output_voltage_v": int(round(v_kv * 1000.0)), "stc_ref_power_kw": round(ref_kw, 5),
        "iron_loss_frac": round(stc_iron_frac, 5) if has_ds else round(main_iron_frac, 9),
        "copper_loss_frac": round(stc_copper_frac, 5) if has_ds else round(main_copper_frac, 9),
        "main_iron_loss_frac": round(main_iron_frac, 9), "main_copper_loss_frac": round(main_copper_frac, 9),
        "stc_iron_loss_frac": round(stc_iron_frac, 5), "stc_copper_loss_frac": round(stc_copper_frac, 5),
        "iron_loss_kw": round(total_iron_kw, 9), "loss_basis": loss_basis, "loss_basis_power_kw": round(loss_basis_kw, 5),
        "from_datasheet": has_ds, "ds_nom_power": round(ds_nom_f, 9) if ds_nom_f is not None else None,
        "ds_iron_loss": round(ds_iron_f, 9) if ds_iron_f is not None else None,
        "ds_copper_loss": round(ds_copper_f, 9) if ds_copper_f is not None else None,
        "night_disconnect": night, "cable": cable,
    }


def _normalize_hv(raw: dict[str, Any], default_ref_kw: float | None, *, inverter_ac_kw: float | None = None, grid_limit_kw: float | None = None) -> dict[str, Any]:
    v_kv, night, cable, ref_kw = _stage_common(raw, "hv", default_ref_kw, default_kv=110.0, kv_range=(30.0, 500.0), default_sec=240, default_metal="Aluminium")
    default_nom_base = inverter_ac_kw if (inverter_ac_kw is not None and inverter_ac_kw > 0) else ref_kw
    ds_nom = _finite_pos(raw["nominal_power_kva"], "hv.nominal_power_kva") if raw.get("nominal_power_kva") is not None else round(default_nom_base * 1.15, 2)
    _check_native_power_bounds(stage="HV", ds_nom_kva=ds_nom, inverter_ac_kw=inverter_ac_kw, grid_limit_kw=grid_limit_kw, count=1)
    if raw.get("iron_loss_kw") is not None:
        ds_iron = _finite_pos(raw["iron_loss_kw"], "hv.iron_loss_kw")
        iron_frac = ds_iron / ref_kw
    else:
        iron_frac = _finite_pos(raw["iron_loss_frac"] if raw.get("iron_loss_frac") is not None else 0.0007, "hv.iron_loss_frac")
        ds_iron = ref_kw * iron_frac
    if raw.get("copper_loss_kw") is not None:
        ds_copper = _finite_pos(raw["copper_loss_kw"], "hv.copper_loss_kw")
        copper_frac = ds_copper * (ref_kw / (ds_nom * ds_nom))
    else:
        copper_frac = _finite_pos(raw["copper_loss_frac"] if raw.get("copper_loss_frac") is not None else 0.0065, "hv.copper_loss_frac")
        ds_copper = copper_frac * ds_nom * ds_nom / ref_kw
    if min(ds_nom, ds_iron, ds_copper) < NATIVE_MIN_DATASHEET_VALUE:
        raise ValueError("hv datasheet nominal_power_kva, iron_loss_kw, and copper_loss_kw must be strictly positive (>= 1e-9; PVsyst TransfoDataSheetsNotWellDef)")
    nom_iron_frac, nom_copper_frac = ds_iron / ds_nom, ds_copper / ds_nom
    if nom_iron_frac > NATIVE_MAX_IRON_LOSS_FRAC + _BOUND_EPS or nom_copper_frac > NATIVE_MAX_COPPER_LOSS_FRAC + _BOUND_EPS:
        raise ValueError(f"HV transformer datasheet losses exceed PVsyst 8.1.7 native limits (iron/nom <= {NATIVE_MAX_IRON_LOSS_FRAC * 100:g}%, copper/nom <= {NATIVE_MAX_COPPER_LOSS_FRAC * 100:g}%; got iron={nom_iron_frac * 100:.2f}%, copper={nom_copper_frac * 100:.2f}% for nominal_power_kva={ds_nom:g} kVA)")
    return {
        "output_voltage_kv": v_kv, "output_voltage_v": int(round(v_kv * 1000.0)), "stc_ref_power_kw": round(ref_kw, 5),
        "iron_loss_frac": round(iron_frac, 5), "iron_loss_kw": round(ds_iron, 9), "copper_loss_frac": round(copper_frac, 5),
        "nom_iron_loss_frac": round(nom_iron_frac, 5), "nom_copper_loss_frac": round(nom_copper_frac, 5),
        "loss_basis": "datasheet_hv", "ds_nom_power": round(ds_nom, 9), "ds_iron_loss": round(ds_iron, 9),
        "ds_copper_loss": round(ds_copper, 9), "night_disconnect": night, "cable": cable,
    }


def _read_component_positive_float(components, kind: str, name: str, object_types: tuple[str, ...], field: str) -> float | None:
    if components is None or not name:
        return None
    for library in ("workspace", "builtin"):
        try:
            comp_path = components._path(kind, name, library)
            if not comp_path.is_file():
                continue
            for blk in objects(components._read(comp_path).decode("utf-8-sig", errors="replace")):
                if blk.object_type in object_types and field in blk.fields:
                    val = float(blk.one(field)[1])
                    if math.isfinite(val) and val > 0:
                        return val
        except (ValueError, OSError, KeyError, AttributeError):
            continue
    return None


def _subarray_int_field(sa, key: str) -> int:
    try:
        return int(sa.one(key)[1].strip()) if key in sa.fields else 0
    except ValueError:
        return 0


def _infer_inverter_ac_power_kw(lines: list[str], *, components=None) -> tuple[float | None, str]:
    """Infer total inverter nominal AC power (TMainArray.PNomInvAC in PVsyst 8.1.7)."""
    subarrays = [b for b in objects("".join(lines)) if b.object_type == "pvSubArray"]
    if not subarrays:
        return None, "unknown"
    total_ac, sources = 0.0, set()
    for sa in subarrays:
        raw_flags = sa.one("Flags")[1].strip() if "Flags" in sa.fields else ""
        is_indep = bool(re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw_flags) and (int(raw_flags[1:], 16) & _INDEPENDENT_MPPT_BIT))
        pnom_mppt = None
        if "PNomAC_InMPPT" in sa.fields:
            try:
                val = float(sa.one("PNomAC_InMPPT")[1].strip())
                if math.isfinite(val) and val > 0:
                    pnom_mppt = val
            except (ValueError, TypeError):
                pass
        ond_pnom = _read_component_positive_float(components, "OND", sa.one("GInverter")[1].strip() if "GInverter" in sa.fields else "", ("TConverter", "pvGInverter"), "PNomConv")
        sub_ac = None
        if is_indep and (n_mppt := _subarray_int_field(sa, "NInvMPPT")) > 0 and pnom_mppt is not None:
            sub_ac = n_mppt * pnom_mppt
            sources.add("subarray_independent_mppt")
        if sub_ac is None:
            n_inv = _subarray_int_field(sa, "NInverter")
            if n_inv > 0 and ond_pnom is not None:
                sub_ac = n_inv * ond_pnom
                sources.add("ond_subarray_ac")
            elif not is_indep and n_inv > 0 and pnom_mppt is not None:
                sub_ac = n_inv * pnom_mppt
                sources.add("subarray_pnom_ac_in_mppt")
        if sub_ac is None or sub_ac <= 0:
            return None, "unknown"
        total_ac += sub_ac
    return (round(total_ac, 5), next(iter(sources)) if len(sources) == 1 else "subarray_mixed_ac") if total_ac > 0 else (None, "unknown")


def _infer_grid_limit_kw(lines: list[str]) -> float | None:
    """Return active pvSystem PLimGrid (kW) when pvSystem.Flags bit 24 is set."""
    systems = [b for b in objects("".join(lines)) if b.object_type == "pvSystem"]
    if len(systems) != 1 or "PLimGrid" not in systems[0].fields:
        return None
    raw_flags = systems[0].one("Flags")[1].strip() if "Flags" in systems[0].fields else ""
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw_flags) or not (int(raw_flags[1:], 16) & _GRID_LIMIT_ENABLED_BIT):
        return None
    try:
        val = float(systems[0].one("PLimGrid")[1].strip())
        return val if math.isfinite(val) and val > 0 else None
    except (ValueError, TypeError):
        return None


def _infer_ref_power_kw(lines: list[str], default_ref_kw: float | None = None, *, components=None) -> tuple[float | None, str]:
    if default_ref_kw is not None and default_ref_kw > 0:
        return float(default_ref_kw), "explicit_default"
    blocks = objects("".join(lines))
    total_pnom = 0.0
    for b in blocks:
        if b.object_type == "pvSubArray" and "PNomSizing" in b.fields:
            try:
                val = float(b.one("PNomSizing")[1])
                if math.isfinite(val) and val > 0:
                    total_pnom += val
            except (ValueError, TypeError):
                pass
    if total_pnom > 0:
        return total_pnom, "pnom_sizing"
    if components is not None and (subarrays := [b for b in blocks if b.object_type == "pvSubArray"]):
        derived_kw = 0.0
        for sa in subarrays:
            try:
                pan_name, n_serie, n_str = sa.one("PVModule")[1].strip(), int(sa.one("NModSerie")[1].strip()), int(sa.one("NStringCh")[1].strip())
            except (ValueError, KeyError):
                break
            pnom_wp = _read_component_positive_float(components, "PAN", pan_name, ("pvModule",), "PNom")
            if pnom_wp is None or n_serie <= 0 or n_str <= 0:
                break
            derived_kw += (n_serie * n_str * pnom_wp) / 1000.0
        else:
            if derived_kw > 0:
                return round(derived_kw, 5), "pan_subarray_stc"
    for b_line in lines:
        if (m := re.match(r"^\s*STCRefPower=([0-9.]+)\s*$", b_line)) and math.isfinite(val := float(m.group(1))) and val > 0:
            return val, "existing_stc_ref_power"
    return None, "unknown"


def _cable_block(cable: dict[str, Any] | None, indent: str, eol: str) -> list[str]:
    if not cable or cable["length_m"] <= 0:
        return []
    return [
        f"{indent}CableDefinition=Start{eol}", f"{indent}    CustomResistance=False{eol}",
        f"{indent}    Length={cable['length_m']:g} m{eol}", f"{indent}    CableType=Start{eol}",
        f"{indent}        Material={cable['metal']}{eol}", f"{indent}        StdCrossSect={cable['std_section']}{eol}",
        f"{indent}    CableType=End{eol}", f"{indent}CableDefinition=End{eol}",
    ]


def _extract_inverter_subtrees(lines: list[str]) -> tuple[int, int, list[list[str]], str]:
    roots = circuit_nodes(lines)
    if len(roots) != 1 or roots[0].kind != "InjectionPointNode":
        raise ValueError("pvCircuit must have a single root InjectionPointNode")
    inj, eol, inv_blocks = roots[0], ("\r\n" if lines and lines[0].endswith("\r\n") else "\n"), []

    def collect(node):
        if node.kind == "InverterNode":
            raw = lines[node.line - 1:node.end_line]
            non_empty = [ln for ln in raw if ln.strip()]
            min_indent = min(len(ln) - len(ln.lstrip(" ")) for ln in non_empty) if non_empty else 0
            inv_blocks.append([ln[min_indent:] if len(ln) >= min_indent else ln for ln in raw])
            return
        for ch in node.children:
            collect(ch)

    collect(inj)
    if not inv_blocks:
        raise ValueError("pvCircuit contains no InverterNode to attach under TransformerNode")
    return inj.line - 1, inj.end_line, inv_blocks, eol


def _native_decimal(value: float, places: int, *, max_places: int = 9) -> str:
    """Keep native padding when exact, otherwise retain up to max_places decimals without trailing zeros."""
    if value == 0.0:
        return f"{0.0:.{places}f}"
    padded = f"{value:.{places}f}"
    if float(padded) == value:
        return padded
    bounded = round(value, max(places, max_places))
    if bounded == 0.0:
        return f"{0.0:.{places}f}"
    rendered = format(Decimal(str(bounded)), "f").rstrip("0").rstrip(".")
    return rendered if "." in rendered and len(rendered.split(".", 1)[1]) > places else f"{bounded:.{places}f}"


def _format_kv(v_kv: float) -> str:
    return f"{v_kv:g} kV"


def _render_transformer_node(spec: dict[str, Any], *, is_hv: bool, children_lines: list[str], indent: str, eol: str) -> list[str]:
    inner = indent + "  "
    out = [f"{indent}TransformerNode Start;{eol}", *_cable_block(spec.get("cable"), inner, eol), f"{inner}Children=Start{eol}", *children_lines, f"{inner}Children=End{eol}"]
    if not is_hv and spec.get("count", 1) > 1:
        out.append(f"{indent}NTransformers={spec['count']}{eol}")
    node_iron = spec["iron_loss_frac"] if is_hv else spec["stc_iron_loss_frac"]
    node_copper = spec["copper_loss_frac"] if is_hv else spec["stc_copper_loss_frac"]
    out.extend([
        f"{indent}OutputVoltage={_format_kv(spec['output_voltage_kv'])}{eol}",
        f"{indent}STCRefPower={spec['stc_ref_power_kw']:.5f}{eol}",
        f"{indent}STCIronLossFrac={node_iron:.5f}{eol}", f"{indent}STCIronLoss={_native_decimal(spec['iron_loss_kw'], 5)}{eol}",
        f"{indent}STCCopperLossFrac={node_copper:.5f}{eol}",
    ])
    if is_hv or spec.get("from_datasheet"):
        out.extend([f"{indent}DSNomPower={_native_decimal(spec['ds_nom_power'], 5)}{eol}", f"{indent}DSIronLoss={_native_decimal(spec['ds_iron_loss'], 5)}{eol}", f"{indent}DSCopperLoss={_native_decimal(spec['ds_copper_loss'], 5)}{eol}"])
    out.extend([f"{indent}NightDisconnect={'True' if spec['night_disconnect'] else 'False'}{eol}", f"{indent}IsHVTransformer={'True' if is_hv else 'False'}{eol}", f"{indent}TransformerNode End;{eol}"])
    return out


def _walk_nodes(node):
    yield node
    for child in node.children:
        yield from _walk_nodes(child)


def _toggle_existing_mv_night_disconnect(
    clean_lines: list[str],
    main_block,
    flags_val: int,
    *,
    night_disconnect: Any,
    has_bom: bool,
    ref_kw: float | None,
    ref_source: str,
    inverter_ac_kw: float | None,
    ac_source: str,
    grid_limit_kw: float | None,
) -> tuple[list[str], dict[str, Any]] | None:
    """In-place NightDisconnect toggle when mv only sets night_disconnect on an existing MV variant."""
    if type(night_disconnect) is not bool:
        raise ValueError("mv.night_disconnect must be a strict boolean")
    if not (flags_val & MV_FLAG) or (flags_val & HV_FLAG):
        return None
    trf_nodes = [n for r in circuit_nodes(clean_lines) for n in _walk_nodes(r) if n.kind == "TransformerNode"]
    flags_entries = main_block.fields.get("Flags", ())
    if (
        len(trf_nodes) != 1
        or trf_nodes[0].fields.get("IsHVTransformer") != ["False"]
        or len(trf_nodes[0].field_lines.get("NightDisconnect", ())) != 1
        or len(flags_entries) != 1
    ):
        return None
    mv_node = trf_nodes[0]
    new_flags = (flags_val | NIGHT_FLAG) if night_disconnect else (flags_val & ~NIGHT_FLAG)
    cand = list(clean_lines)
    flags_line = flags_entries[0][0]
    cand[flags_line] = replace_value(cand[flags_line], f"${new_flags:08X}")
    nd_line = mv_node.field_lines["NightDisconnect"][0]
    cand[nd_line] = replace_value(cand[nd_line], "True" if night_disconnect else "False")
    cand, _ = strip_result_cache(cand)
    objects("".join(cand))
    state = transformer_state(cand)
    if state["issues"] or state["count"] != 1:
        raise ValueError("Generated transformer circuit failed validation: " + "; ".join(state["issues"] or ["expected 1 MV layer"]))

    def _opt_float(block, key: str) -> float | None:
        if key not in block.fields:
            return None
        try:
            raw = block.one(key)
            val = raw[1] if isinstance(raw, tuple) else raw
            return float(val)
        except (TypeError, ValueError):
            return None

    v_out_mv = _opt_float(main_block, "VOutTransfo")
    mv_count = int(main_block.one("MVNbTransfos")[1]) if "MVNbTransfos" in main_block.fields else 1
    stc_ref = _opt_float(mv_node, "STCRefPower") or ref_kw
    mv_summary = {
        "count": mv_count,
        "output_voltage_kv": round(v_out_mv / 1000.0, 5) if v_out_mv is not None else None,
        "output_voltage_v": int(round(v_out_mv)) if v_out_mv is not None else None,
        "stc_ref_power_kw": stc_ref,
        "iron_loss_frac": _opt_float(mv_node, "STCIronLossFrac") or _opt_float(main_block, "TrfIronLossFrac"),
        "copper_loss_frac": _opt_float(mv_node, "STCCopperLossFrac") or _opt_float(main_block, "TrfResLossFrac"),
        "iron_loss_kw": _opt_float(mv_node, "STCIronLoss"),
        "from_datasheet": bool(new_flags & MV_DS_FLAG),
        "night_disconnect": night_disconnect,
        "preserved_existing_mv": True,
    }
    if has_bom and cand:
        cand[0] = "\ufeff" + cand[0]
    return cand, {
        "remove_transformers": False,
        "main_array_flags": f"${new_flags:08X}",
        "stc_ref_power_kw": stc_ref,
        "stc_ref_power_source": ref_source,
        "inverter_ac_power_kw": inverter_ac_kw,
        "inverter_ac_power_source": ac_source,
        "grid_limit_kw": grid_limit_kw,
        "mv": mv_summary,
        "hv": None,
        "transformer_state": state,
        "warnings": [
            "Toggled MV NightDisconnect and pvMainArray bit 29 in-place; "
            "existing MV rating, losses, default-loss flags, and cable configuration were preserved."
        ],
    }


def apply_transformers_to_variant_lines(lines: list[str], *, mv: dict[str, Any] | None = None, hv: dict[str, Any] | None = None,
                                        remove_transformers: bool = False, default_ref_power_kw: float | None = None, components=None) -> tuple[list[str], dict[str, Any]]:
    """Pure text transformer builder/editor operating on parsed .VCx lines."""
    if remove_transformers and (mv is not None or hv is not None):
        raise ValueError("Cannot combine remove_transformers=True with mv/hv specifications")
    if not remove_transformers and mv is None:
        raise ValueError("Provide mv transformer specification (with optional hv), or set remove_transformers=True")
    has_bom = bool(lines and lines[0].startswith("\ufeff"))
    clean_lines = [lines[0].lstrip("\ufeff"), *lines[1:]] if has_bom else list(lines)
    blocks = objects("".join(clean_lines))
    mains, systems = [b for b in blocks if b.object_type == "pvMainArray"], [b for b in blocks if b.object_type == "pvSystem"]
    if len(mains) != 1 or not mains[0].end_line:
        raise ValueError("Expected exactly one complete pvMainArray block")
    if len(systems) == 1 and "SystemType" in systems[0].fields and systems[0].one("SystemType")[1] != "Grid":
        raise ValueError("MV/HV transformers are only supported on Grid-connected variants")
    raw_flags = mains[0].one("Flags")[1]
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw_flags):
        raise ValueError("pvMainArray Flags must be hex ($XXXXXXXX)")
    flags_val = int(raw_flags[1:], 16)
    eol = "\r\n" if clean_lines and clean_lines[0].endswith("\r\n") else "\n"
    inj_start, inj_end, inv_blocks, _ = _extract_inverter_subtrees(clean_lines)
    base_indent = clean_lines[inj_start][:len(clean_lines[inj_start]) - len(clean_lines[inj_start].lstrip(" "))]
    ref_kw, ref_source = _infer_ref_power_kw(clean_lines, default_ref_power_kw, components=components)
    inverter_ac_kw, ac_source = _infer_inverter_ac_power_kw(clean_lines, components=components)
    grid_limit_kw = _infer_grid_limit_kw(clean_lines)
    if not remove_transformers and hv is None and isinstance(mv, dict) and set(mv.keys()) == {"night_disconnect"}:
        in_place = _toggle_existing_mv_night_disconnect(
            clean_lines, mains[0], flags_val, night_disconnect=mv["night_disconnect"], has_bom=has_bom,
            ref_kw=ref_kw, ref_source=ref_source, inverter_ac_kw=inverter_ac_kw, ac_source=ac_source,
            grid_limit_kw=grid_limit_kw,
        )
        if in_place is not None:
            return in_place
    mv_norm = None if remove_transformers else _normalize_mv(mv, ref_kw, inverter_ac_kw=inverter_ac_kw, grid_limit_kw=grid_limit_kw)  # type: ignore[arg-type]
    hv_default_ref = ref_kw if ref_kw is not None else (mv_norm["stc_ref_power_kw"] if mv_norm is not None else None)
    hv_norm = None if (remove_transformers or hv is None) else _normalize_hv(hv, hv_default_ref, inverter_ac_kw=inverter_ac_kw, grid_limit_kw=grid_limit_kw)
    if mv_norm is not None and hv_norm is not None and hv_norm["output_voltage_kv"] < mv_norm["output_voltage_kv"]:
        raise ValueError(f"hv.output_voltage_kv ({hv_norm['output_voltage_kv']:g} kV) must be >= mv.output_voltage_kv ({mv_norm['output_voltage_kv']:g} kV); PVsyst requires VOutHVTransfo >= VOutTransfo (TransfoHT_DefGridVoltage)")

    warnings: list[str] = []
    if mv_norm is not None and not mv_norm["from_datasheet"] and inverter_ac_kw is not None and mv is not None and mv.get("stc_ref_power_kw") is not None and abs(inverter_ac_kw - mv_norm["stc_ref_power_kw"]) / mv_norm["stc_ref_power_kw"] > 0.01:
        warnings.append(f"PVsyst 8.1.7 evaluates MV fractional losses (TrfIronLossFrac/TrfResLossFrac) on inverter nominal AC power (PNomInvAC={inverter_ac_kw:g} kW), not stc_ref_power_kw ({mv_norm['stc_ref_power_kw']:g} kW); simulated constant iron loss is {mv_norm['iron_loss_kw']:g} kW. Provide nominal_power_kva, iron_loss_kw, and copper_loss_kw for explicit nameplate losses.")
    if mv_norm is not None and mv_norm.get("ds_nom_power") is not None and inverter_ac_kw is None:
        mv_total_kva = mv_norm["count"] * mv_norm["ds_nom_power"]
        if (ratio := mv_total_kva / mv_norm["stc_ref_power_kw"]) < 0.2 or ratio > 5.0:
            warnings.append(f"MV total transformer rating ({mv_norm['count']} x {mv_norm['ds_nom_power']:g} = {mv_total_kva:g} kVA) is {ratio:.2f}x of stc_ref_power_kw ({mv_norm['stc_ref_power_kw']:g} kW); verify rating/count/units.")
    if hv_norm is not None and hv_norm.get("ds_nom_power") is not None and inverter_ac_kw is None:
        if (ratio := hv_norm["ds_nom_power"] / hv_norm["stc_ref_power_kw"]) < 0.2 or ratio > 5.0:
            warnings.append(f"HV transformer rating ({hv_norm['ds_nom_power']:g} kVA) is {ratio:.2f}x of stc_ref_power_kw ({hv_norm['stc_ref_power_kw']:g} kW); verify rating/units.")

    child_indent = base_indent + "  "
    indent_lines = lambda ind: [(ind + ln) if ln.strip() else ln for blk in inv_blocks for ln in blk]
    if remove_transformers:
        inner_inj = indent_lines(child_indent + "  ")
    elif hv_norm is None:
        assert mv_norm is not None
        mv_indent = child_indent + "  "
        inner_inj = _render_transformer_node(mv_norm, is_hv=False, children_lines=indent_lines(mv_indent + "    "), indent=mv_indent, eol=eol)
    else:
        assert mv_norm is not None
        hv_indent, mv_indent = child_indent + "  ", child_indent + "      "
        mv_lines = _render_transformer_node(mv_norm, is_hv=False, children_lines=indent_lines(mv_indent + "    "), indent=mv_indent, eol=eol)
        inner_inj = _render_transformer_node(hv_norm, is_hv=True, children_lines=mv_lines, indent=hv_indent, eol=eol)
    new_inj = [f"{base_indent}InjectionPointNode Start;{eol}", f"{child_indent}Children=Start{eol}", *inner_inj, f"{child_indent}Children=End{eol}", f"{base_indent}InjectionPointNode End;{eol}"]
    cand = list(clean_lines[:inj_start]) + new_inj + list(clean_lines[inj_end:])
    main_start = next(i for i, ln in enumerate(cand) if ln.strip() == "PVObject_PVMainArray=pvMainArray")
    circ_start = next(i for i in range(main_start, len(cand)) if ln_strip(cand[i]) == "PVObject_SystemCircuit=pvCircuit")
    flags_val &= ~(MV_FLAG | HV_FLAG | MV_DS_FLAG | NIGHT_FLAG)
    if not remove_transformers:
        assert mv_norm is not None
        flags_val = (flags_val | MV_FLAG | (MV_DS_FLAG if mv_norm["from_datasheet"] else 0) | (NIGHT_FLAG if mv_norm["night_disconnect"] else 0) | (HV_FLAG if hv_norm is not None else 0)) & ~(IRON_DEFAULT_FLAG | RES_DEFAULT_FLAG)
    kept_header, insert_pos = [], None
    for idx in range(main_start, circ_start):
        ln, s = cand[idx], cand[idx].strip()
        if idx == main_start:
            kept_header.append(ln)
        elif s.startswith("Flags="):
            kept_header.append(replace_value(ln, f"${flags_val:08X}"))
        elif (key := s.split("=", 1)[0] if "=" in s else "") in _MAIN_TRF_KEYS:
            if insert_pos is None:
                insert_pos = len(kept_header)
        else:
            if key == "UseSpectralCorr" and insert_pos is None:
                insert_pos = len(kept_header)
            kept_header.append(ln)
    insert_pos = len(kept_header) if insert_pos is None else insert_pos
    hdr = "  "
    if remove_transformers:
        trf_header = [f"{hdr}MVNbTransfos=0{eol}"]
    else:
        assert mv_norm is not None
        mv_cab = mv_norm.get("cable") or {}
        trf_header = [
            f"{hdr}VOutTransfo={mv_norm['output_voltage_v']}{eol}", f"{hdr}MVWireNoSect={mv_cab.get('wire_no_sect', 13)}{eol}",
            f"{hdr}MVWireLength={mv_cab.get('length_m', 0.0):.1f}{eol}", f"{hdr}MVWireMetal={mv_cab.get('metal', 'Copper')}{eol}",
        ]
        if hv_norm is not None:
            hv_cab = hv_norm.get("cable") or {}
            trf_header.extend([
                f"{hdr}VOutHVTransfo={hv_norm['output_voltage_v']}{eol}", f"{hdr}HVWireNoSect={hv_cab.get('wire_no_sect', 15)}{eol}",
                f"{hdr}HVWireLength={hv_cab.get('length_m', 0.0):.1f}{eol}", f"{hdr}HVWireMetal={hv_cab.get('metal', 'Aluminium')}{eol}",
            ])
        trf_header.extend([f"{hdr}MVNbTransfos={mv_norm['count']}{eol}", f"{hdr}TrfIronLossFrac={_native_decimal(mv_norm['main_iron_loss_frac'], 5)}{eol}", f"{hdr}TrfResLossFrac={_native_decimal(mv_norm['main_copper_loss_frac'], 5)}{eol}"])
        if mv_norm["from_datasheet"]:
            trf_header.extend([f"{hdr}MVTrfDSNomPower={_native_decimal(mv_norm['ds_nom_power'], 2)}{eol}", f"{hdr}MVTrfDSIronLoss={_native_decimal(mv_norm['ds_iron_loss'], 4)}{eol}", f"{hdr}MVTrfDSCopperLoss={_native_decimal(mv_norm['ds_copper_loss'], 4)}{eol}"])
        if hv_norm is not None:
            trf_header.extend([f"{hdr}HVTrfDSNomPower={_native_decimal(hv_norm['ds_nom_power'], 2)}{eol}", f"{hdr}HVTrfDSIronLoss={_native_decimal(hv_norm['ds_iron_loss'], 4)}{eol}", f"{hdr}HVTrfDSCopperLoss={_native_decimal(hv_norm['ds_copper_loss'], 4)}{eol}"])
    cand = cand[:main_start] + kept_header[:insert_pos] + trf_header + kept_header[insert_pos:] + cand[circ_start:]
    cand, _ = strip_result_cache(cand)
    objects("".join(cand))
    state = transformer_state(cand)
    if state["issues"]:
        raise ValueError("Generated transformer circuit failed validation: " + "; ".join(state["issues"]))
    expected_layers = 0 if remove_transformers else (2 if hv_norm is not None else 1)
    if state["count"] != expected_layers:
        raise ValueError(f"Expected {expected_layers} TransformerNode layer(s), found {state['count']}")
    if has_bom and cand:
        cand[0] = "\ufeff" + cand[0]
    return cand, {
        "remove_transformers": remove_transformers, "main_array_flags": f"${flags_val:08X}",
        "stc_ref_power_kw": mv_norm["stc_ref_power_kw"] if mv_norm is not None else ref_kw,
        "stc_ref_power_source": "explicit_mv" if (mv is not None and mv.get("stc_ref_power_kw") is not None) else ref_source,
        "inverter_ac_power_kw": inverter_ac_kw, "inverter_ac_power_source": ac_source, "grid_limit_kw": grid_limit_kw,
        "mv": mv_norm, "hv": hv_norm, "transformer_state": state, "warnings": warnings,
    }


def apply_transformers_to_variant_text(vc_text: str, *, mv: dict[str, Any] | None = None, hv: dict[str, Any] | None = None,
                                       remove_transformers: bool = False, default_ref_power_kw: float | None = None, components=None) -> tuple[str, dict[str, Any]]:
    """Convenience wrapper operating on a full .VCx text string."""
    new_lines, summary = apply_transformers_to_variant_lines(vc_text.splitlines(keepends=True), mv=mv, hv=hv, remove_transformers=remove_transformers, default_ref_power_kw=default_ref_power_kw, components=components)
    return "".join(new_lines), summary


def configure_transformers(store, *, project: str, source_variant: str, new_variant: str, mv: dict[str, Any] | None = None,
                           hv: dict[str, Any] | None = None, remove_transformers: bool = False, dry_run: bool = True,
                           variant_name: str | None = None, add_utf8_bom: bool = False) -> dict[str, Any]:
    """Create a new variant with zero-template MV / MV+HV transformers and AC collection cables."""
    def preflight():
        if type(remove_transformers) is not bool:
            raise ValueError("remove_transformers must be a strict boolean")
        if remove_transformers and (mv is not None or hv is not None):
            raise ValueError("Cannot specify mv/hv when remove_transformers=True")
        if not remove_transformers and mv is None:
            raise ValueError("Provide mv specification (and optional hv), or set remove_transformers=True")

    def build(*, lines, **_):
        candidate_lines, summary = apply_transformers_to_variant_lines(lines, mv=mv, hv=hv, remove_transformers=remove_transformers, components=getattr(store, "components", None))

        def verify(checked, _, res):
            verified = transformer_state(checked)
            if verified["issues"]:
                raise ValueError("Verified variant transformer circuit has issues: " + "; ".join(verified["issues"]))
            res["warnings"].extend(summary.get("warnings") or ())
            res["transformers"] = {
                "remove_transformers": remove_transformers, "main_array_flags": summary["main_array_flags"],
                "stc_ref_power_kw": summary["stc_ref_power_kw"], "stc_ref_power_source": summary["stc_ref_power_source"],
                "inverter_ac_power_kw": summary["inverter_ac_power_kw"], "inverter_ac_power_source": summary["inverter_ac_power_source"],
                "grid_limit_kw": summary["grid_limit_kw"], "mv": summary["mv"], "hv": summary["hv"],
                "layers": verified["count"], "nested_hv_mv": verified["nested"],
            }

        return {"candidate_lines": candidate_lines, "verify": verify}

    return run_new_variant_transaction(
        store, project=project, source_variant=source_variant, new_variant=new_variant,
        dry_run=dry_run, add_utf8_bom=add_utf8_bom, variant_name=variant_name,
        preflight=preflight, build=build, source_changed_error="Source variant changed before transformer configuration",
        write_failure_warning="New variant may contain a partial artifact; original source is preserved.",
    )

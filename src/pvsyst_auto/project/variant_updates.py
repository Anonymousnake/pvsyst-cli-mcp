"""Transactional parameter updates, new-variant transactions, native economics writer, and variant inspection."""
from __future__ import annotations

import difflib
import hashlib
import itertools
import math
import re
from typing import Any

from pvsyst_auto.components.editor import generator_state, has_object, objects, replace_generator, replace_value
from pvsyst_auto.components.store import FIELD
from pvsyst_auto.native.load import grid_limit_state, horizon_state, p50_state, replace_grid_limit, replace_horizon, replace_p50
from pvsyst_auto.project.circuit import (
    inventory as grid_inventory,
    require_plane_branch_mode,
    resize_capacity,
    set_independent_mppt,
    set_power_sharing,
    subarray_branches,
    topology_state,
    transformer_state,
)
from pvsyst_auto.validation import finite_number
from pvsyst_auto.validation import NATIVE_TEXT, NATIVE_TEXT_OR_SEED, SEED_LABEL, TEXT_LABEL

BOM = b"\xef\xbb\xbf"

ALLOWED_SUBARRAY = {
    "NModSerie": (1, 100), "NInverter": (1, 10000), "NStringCh": (1, 2147483647),
    "NoOrientation": (1, 2147483647), "NInvMPPT": (1, 10000), "VBkUpEncl_syst": (0, 1),
    "VBkUpDecl_syst": (0, 1), "LIDLoss": (0, 100), "PMismMPP": (0, 100),
    "ModuleQualityLoss": (-5, 20), "PNomAC_InMPPT": (0, math.inf),
}
ALLOWED_ORIENTATION = {"FieldTilt": (0, 90), "FieldAzim": (-180, 180)}
ALLOWED_MAIN_ARRAY = {"UFactor": (15, 100), "UVFactor": (-math.inf, math.inf)}

_EXCLUSIVE_CONTROLS = (
    ("system_unavailability_updates", "System-unavailability removal", "system_unavailability", "requires"),
    ("soiling_updates", "Soiling", "soiling", "updates require"),
    ("grid_limit_updates", "Grid limit", "grid_limit", "updates require"),
    ("auxiliary_updates", "Auxiliary", "auxiliaries", "updates require"),
    ("seasonal_updates", "Seasonal", "seasonal_orientation", "updates require"),
    ("horizon_updates", "Horizon", "horizon", "updates require"),
    ("inverter_temperature_updates", "Inverter temperature", "inverter_temperature", "updates require"),
    ("power_factor_updates", "Power factor", "power_factor", "updates require"),
    ("p50_p90_updates", "P50/P90", "p50_p90", "updates require"),
    ("power_controls_updates", "Combined power-control", "power_controls", "updates require"),
    ("string_mismatch_updates", "String-mismatch", "string_mismatch", "updates require"),
    ("ageing_updates", "Ageing model", "ageing", "updates require"),
    ("iam_updates", "IAM model", "iam", "updates require"),
    ("ageing_enabled", "Ageing enable", "ageing", "updates require"),
    ("bifacial_updates", "Bifacial model", "bifacial", "updates require"),
    ("spectral_updates", "Spectral correction", "spectral", "updates require"),
    ("native_economics_updates", "Native economics", "native_economics", "updates require"),
)

_COMPOSABLE_CONTROLS = frozenset({
    "soiling_updates", "system_unavailability_updates", "grid_limit_updates", "auxiliary_updates",
    "inverter_temperature_updates", "power_factor_updates", "p50_p90_updates",
    "power_controls_updates", "bifacial_updates", "spectral_updates", "native_economics_updates",
})


# --- New-variant transaction runner ---

def apply_variant_name(store, candidate_lines: list[str], variant_name: str | None):
    """Rewrite header Comment when `variant_name` is given and return (lines, name_change)."""
    if variant_name is None:
        return candidate_lines, None
    store._validate_display_name(variant_name, "variant_name")
    index, = store._display_name_fields(candidate_lines, complete=True)
    before = candidate_lines[index].partition("=")[2].strip()
    candidate_lines[index] = replace_value(candidate_lines[index], variant_name)
    return candidate_lines, {"before": before, "after": variant_name}


def resolve_bom_prefix(store, original: bytes, candidate_text: str, *, variant_name: str | None = None,
                       add_utf8_bom: bool = False, bom_display_target: str | None = None) -> bytes:
    """Compute UTF-8 BOM prefix so BOM-less sources carrying non-ASCII text stay valid."""
    target = variant_name if variant_name is not None else bom_display_target
    if target is not None:
        return store._display_name_prefix(original, target, add_utf8_bom, "variant_name", content=candidate_text)
    return BOM if (original.startswith(BOM) or add_utf8_bom or not candidate_text.isascii()) else b""


def run_new_variant_transaction(
    store, *, project: str, source_variant: str, new_variant: str, dry_run: bool, build,
    add_utf8_bom: bool | None = None, variant_name: str | None = None, bom_display_target: str | None = None,
    preflight=None, initial_fields: dict | None = None, exists_prefix: str = "Choose a new variant: ",
    source_changed_error: str = "Source variant changed before creation",
    write_failure_warning: str = "The new variant may contain a partial artifact; inspect the disclosed path before retrying. Original template is retained.",
    extra_exceptions: tuple[type[Exception], ...] = (), include_bom_added: bool = True, uppercase_variant: bool = True,
) -> dict:
    """Preview or atomically create a new VC variant under `store.lock`."""
    result = {
        "status": "blocked", "stage": "preflight", "dry_run": dry_run, "issues": [], "warnings": [],
        "artifacts": {}, "variant_created": False, "native_consumption_verified": False, **(initial_fields or {}),
    }
    caught = (ValueError, OSError, UnicodeError, KeyError, TypeError, StopIteration, *extra_exceptions)
    with store.lock:
        try:
            if type(dry_run) is not bool or (add_utf8_bom is not None and type(add_utf8_bom) is not bool):
                raise ValueError("Use strict booleans")
            if preflight is not None:
                preflight()
            store._check_project(project)
            source, target = store._path(project, source_variant), store._path(project, new_variant)
            if any(p.name.casefold() == target.name.casefold() for p in target.parent.iterdir()):
                raise FileExistsError(exists_prefix + target.name)
            original = store._read(source)
            text, lines = store._text(original)
            prepared = build(text=text, lines=lines, original=original, source=source, target=target, result=result)
            if "candidate_text" in prepared:
                candidate_text, name_change = prepared["candidate_text"], prepared.get("name_change")
            else:
                candidate_lines, name_change = apply_variant_name(store, list(prepared["candidate_lines"]), variant_name)
                candidate_text = "".join(candidate_lines)
            bom_prefix = resolve_bom_prefix(store, original, candidate_text, variant_name=variant_name,
                                            add_utf8_bom=bool(add_utf8_bom), bom_display_target=bom_display_target)
            candidate = bom_prefix + candidate_text.encode("utf-8")
            _, checked = store._text(candidate)
            if prepared.get("verify") is not None:
                prepared["verify"](checked, candidate, result)
            if name_change is not None:
                result["name_change"] = name_change
            diff = "".join(difflib.unified_diff(text.splitlines(keepends=True), candidate_text.splitlines(keepends=True)))
            result.update(
                status="ready", project=project, source_variant=source_variant,
                variant=target.suffix[1:].upper() if uppercase_variant else new_variant,
                diff=diff[:32000], diff_truncated=len(diff) > 32000,
            )
            result.setdefault("artifacts", {})["variant"] = str(target)
            if include_bom_added:
                result["bom_added"] = bool(bom_prefix) and not original.startswith(BOM)
            if store._read(source) != original:
                raise ValueError(source_changed_error)
            if dry_run:
                return result
            result["stage"] = "write"
            with target.open("xb") as stream:
                stream.write(candidate)
            result.update(status="created", stage="created", variant_created=True)
        except caught as exc:
            result["issues"].append(str(exc))
            result["status"] = "failed" if result["stage"] == "write" else "blocked"
            if result["status"] == "failed":
                result["warnings"].append(write_failure_warning)
    return result


# --- Native economics block ---

def _eco_finite(val: Any, name: str, *, allow_zero: bool = True) -> float:
    return finite_number(val, name, minimum=0, strict_min=not allow_zero)


def format_native_economics_block(cfg: dict[str, Any], eol: str) -> list[str]:
    """Serialize a clean, valid PVsyst 8.1.7 EconomicEval=TEcoEvaluation block."""
    life = cfg.get("project_life_years", 25)
    if type(life) is not int or not 1 <= life <= 60:
        raise ValueError("project_life_years must be an integer in 1..60")
    currency = str(cfg.get("currency") or "USD").strip().upper()
    if not 2 <= len(currency) <= 6 or not currency.isalpha():
        raise ValueError("currency must be a 2..6-letter ISO currency code (e.g. USD, EUR, CNY)")
    capex_total = _eco_finite(cfg.get("capex_total", 0.0), "capex_total", allow_zero=False)
    annual_opex = _eco_finite(cfg.get("annual_opex", 0.0), "annual_opex")
    subsidies = _eco_finite(cfg.get("subsidies", 0.0), "subsidies")
    discount_pct = _eco_finite(cfg.get("discount_rate_pct", 5.0), "discount_rate_pct")
    inflation_pct = _eco_finite(cfg.get("inflation_rate_pct", 2.0), "inflation_rate_pct")
    sale_tariff = _eco_finite(cfg.get("sale_tariff", 0.08), "sale_tariff")
    cons_tariff = _eco_finite(cfg.get("consumption_tariff", 0.15), "consumption_tariff")
    tariff_esc_pct = _eco_finite(cfg.get("tariff_escalation_pct", 0.0), "tariff_escalation_pct")
    inv_life = _eco_finite(cfg.get("inverter_lifetime_years", 12.0), "inverter_lifetime_years", allow_zero=False)
    batt_life = _eco_finite(cfg.get("battery_lifetime_years", 10.0), "battery_lifetime_years", allow_zero=False)
    fuel_price = _eco_finite(cfg.get("fuel_price_per_liter", 1.50), "fuel_price_per_liter")
    lines = [
        f"  EconomicEval=TEcoEvaluation{eol}", f"    ProjectLife={life}{eol}", f"    PriceKind=Global{eol}",
        f"    Currency={currency}{eol}", f"    InvestmentCosts, list of=1 InvestmentCost{eol}", f"      InvestmentCost=1{eol}",
        f"          CostIdent=invOtherComponents{eol}", f"          DepreciationType=deLinear{eol}",
        f"          DeprecDuration={life}{eol}", f"          RedDeprecCoeff=1.00{eol}", f"          CostCredit=incCost{eol}",
        f"          ValueType=valAmount{eol}", f"          Name=Total PV/BESS Turnkey CAPEX{eol}",
        f"          IsComponent=False{eol}", f"          SubFieldNb=1{eol}", f"          Quantity=1.00{eol}",
        f"          UnitPrice={capex_total:.5f}{eol}", f"      EndTags{eol}", f"    End of List InvestmentCosts{eol}",
        f"    OperatingCosts, list of=1 OperatingCost{eol}", f"      OperatingCost=1{eol}",
        f"          CostIdent=opeMaintenance{eol}", f"          CostCredit=incCost{eol}", f"          Name=Annual O&M{eol}",
        f"          Price={annual_opex:.2f}{eol}", f"      EndTags{eol}", f"    End of List OperatingCosts{eol}",
        f"    OwnFunds={max(0.0, capex_total - subsidies):.2f}{eol}",
        *( [f"    Subsidies={subsidies:.2f}{eol}"] if subsidies > 0.0 else [] ),
        f"    InflationRate={inflation_pct / 100.0:.4f}{eol}", f"    DiscountRate={discount_pct / 100.0:.4f}{eol}",
        f"    InvertersLifeTime={inv_life:.2f}{eol}", f"    BatteriesLifeTime={batt_life:.2f}{eol}",
        f"    FuelPricePerLiter={fuel_price:.2f}{eol}", f"    TariffType=taFixed{eol}",
        f"    TariffHourlyPeak=False{eol}", f"    TariffSeasonal=False{eol}", f"    TariffCsv=False{eol}",
        f"    SummerPeakSaleTariff={sale_tariff:.5f}{eol}", f"    SummerOffPeakSaleTariff={sale_tariff:.5f}{eol}",
        f"    SaleTariffEvol={tariff_esc_pct / 100.0:.4f}{eol}", f"    ConsumptionTariffType=taFixed{eol}",
        f"    SummerPeakConsumptionTariff={cons_tariff:.5f}{eol}", f"    SummerOffPeakConsumptionTariff={cons_tariff:.5f}{eol}",
        f"    ConsumptionTariffHourlyPeak=False{eol}", f"    ConsumptionTariffSeasonal=False{eol}",
        f"    ConsumptionTariffCsv=False{eol}", f"  End of TEcoEvaluation{eol}",
    ]
    return lines


def replace_native_economics(lines: list[str], updates: dict[str, Any]) -> dict[str, Any]:
    """Write or remove native EconomicEval=TEcoEvaluation inside a .VCx variant."""
    if not isinstance(updates, dict) or (mode := updates.get("mode", "set")) not in ("set", "none"):
        raise ValueError("native_economics_updates must be an object" if not isinstance(updates, dict) else "native_economics_updates.mode must be 'set' or 'none'")
    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "EconomicEval=TEcoEvaluation"), None)
    end = next((i for i, ln in enumerate(lines) if ln.strip() == "End of TEcoEvaluation"), None)
    had_block = start is not None and end is not None and start < end
    cand = list(lines)
    if had_block:
        del cand[start:end + 1]
    if mode == "set":
        anchor = next((i for i, ln in enumerate(cand) if ln.strip() in ("SimulPlots=Start", "End of PVObject pvVCalcul")), len(cand) - 1)
        cand[anchor:anchor] = [eol, *format_native_economics_block(updates, eol), eol]
    lines[:] = cand
    return {
        "mode": mode, "previously_configured": had_block, "configured": mode == "set",
        "currency": updates.get("currency", "USD") if mode == "set" else None,
        "capex_total": updates.get("capex_total") if mode == "set" else None,
        "project_life_years": updates.get("project_life_years", 25) if mode == "set" else None,
    }


# --- Transactional parameter updates ---

def _validate_request(store, *, requested, generator_updates, dry_run, add_utf8_bom, variant_name,
                      power_sharing_enabled, independent_mppt_enabled, control_params):
    if (ae := control_params.get("ageing_enabled")) is not None and type(ae) is not bool:
        raise ValueError("ageing_enabled must be a strict boolean")
    has_core_edit = (any(changes for changes, _ in requested) or generator_updates is not None
                     or power_sharing_enabled is not None or independent_mppt_enabled is not None)
    active = [(p, lbl, v) for p, lbl, _, v in _EXCLUSIVE_CONTROLS if control_params.get(p) is not None]
    if active and (has_core_edit or (len(active) > 1 and not all(p in _COMPOSABLE_CONTROLS for p, _, _ in active))):
        _, label, verb = active[0]
        if any(p == "system_unavailability_updates" for p, _, _ in active):
            label, verb = "System-unavailability removal", "requires"
        raise ValueError(f"{label} {verb} a separate transaction; variant_name may accompany it")
    if not isinstance(dry_run, bool) or not isinstance(add_utf8_bom, bool):
        raise ValueError("dry_run must be a boolean" if not isinstance(dry_run, bool) else "add_utf8_bom must be a boolean")
    if add_utf8_bom and variant_name is None:
        raise ValueError("add_utf8_bom requires variant_name")
    if variant_name is not None:
        store._validate_display_name(variant_name, "variant_name")
    for name, switch in (("power_sharing_enabled", power_sharing_enabled), ("independent_mppt_enabled", independent_mppt_enabled)):
        if switch is not None:
            if not isinstance(switch, bool):
                raise ValueError(f"{name} must be a boolean")
            if (any(changes for changes, _ in requested) or generator_updates is not None
                    or variant_name is not None or (power_sharing_enabled is not None and independent_mppt_enabled is not None)):
                raise ValueError("Mode/sharing changes require separate parameter transactions")
    if (not any(c for c, _ in requested) and not generator_updates and variant_name is None
            and power_sharing_enabled is None and independent_mppt_enabled is None and not active) or any(
            c is not None and (not isinstance(c, dict) or any(k not in allowed for k in c)) for c, allowed in requested):
        raise ValueError("Provide supported parameter updates or variant_name")
    if generator_updates is not None:
        if not isinstance(generator_updates, dict) or not generator_updates:
            raise ValueError("generator_updates must be a nonempty dictionary")
        if set(requested[0][0] or {}) & {"VBkUpEncl_syst", "VBkUpDecl_syst"}:
            raise ValueError("Use generator_updates.thresholds for thresholds in a generator transaction")


def _apply_core_edits(store, lines, arrays, system, *, subarray_id, orientation_id, subarray_updates,
                      orientation_updates, main_array_updates, power_sharing_enabled, independent_mppt_enabled):
    main_array = store._main_array(lines) if main_array_updates else None
    uv_only = bool(main_array_updates) and set(main_array_updates) == {"UVFactor"}
    if main_array_updates and "UVFactor" in main_array_updates and not uv_only:
        raise ValueError("UVFactor edits are a separate transaction; do not combine with UFactor")
    if main_array_updates and not uv_only:
        kinds = store._field_indices(lines, system, "SystemType") if system is not None else []
        versions = [store._field_indices(lines, bounds, "Version") for bounds in arrays.values()]
        if (main_array is None or len(kinds) != 1 or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid"
                or any(len(idx) != 1 or FIELD.fullmatch(lines[idx[0]].rstrip("\r\n"))[2] not in NATIVE_TEXT_OR_SEED for idx in versions)):
            raise ValueError(f"Thermal editing requires a verified {SEED_LABEL} grid main array")
        flags, flags2 = store._field_indices(lines, main_array, "Flags"), store._field_indices(lines, main_array, "Flags2")
        if (len(flags) != 1 or not store._is_verified_main_flags(FIELD.fullmatch(lines[flags[0]].rstrip("\r\n"))[2])
                or len(flags2) != 1 or not store._is_verified_main_flags2(FIELD.fullmatch(lines[flags2[0]].rstrip("\r\n"))[2])):
            raise ValueError("Thermal editing requires verified main-array Flags/Flags2")
    elif uv_only:
        store._uv_edit_gate(lines, system, arrays, main_array)
    if (subarray_updates or power_sharing_enabled is not None or independent_mppt_enabled is not None) and (
            not isinstance(subarray_id, int) or isinstance(subarray_id, bool) or subarray_id not in arrays):
        raise ValueError("Subarray ID does not exist")
    for loss in set(subarray_updates or {}) & {"LIDLoss", "PMismMPP", "ModuleQualityLoss"}:
        versions = store._field_indices(lines, arrays[subarray_id], "Version")
        kinds = store._field_indices(lines, system, "SystemType") if system is not None else []
        if (len(versions) != 1 or FIELD.fullmatch(lines[versions[0]].rstrip("\r\n"))[2] not in NATIVE_TEXT_OR_SEED
                or len(kinds) != 1 or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid"):
            raise ValueError(f"{loss} editing requires a verified {SEED_LABEL} grid subarray")
    if "NModSerie" in (subarray_updates or {}):
        store._require_no_geometry(lines)
    loss_modes = {}
    for key, label, mask in (("PMismMPP", "Mismatch", 0x00040000), ("ModuleQualityLoss", "Module quality", 0x00100000)):
        if key not in (subarray_updates or {}):
            continue
        indices = store._field_indices(lines, arrays[subarray_id], "Flags")
        if len(indices) != 1:
            raise ValueError(f"{label} editing requires one owning Flags field")
        raw = FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]
        if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", raw):
            raise ValueError(f"{label} editing requires hexadecimal Flags")
        flags = int(raw[1:], 16)
        loss_modes[key] = {"default_before": bool(flags & mask), "default_after": False}
        if loss_modes[key]["default_before"]:
            lines[indices[0]] = replace_value(lines[indices[0]], f"${flags & ~mask:0{len(raw) - 1}X}")
    targets = [(subarray_updates, ALLOWED_SUBARRAY, arrays[subarray_id])] if subarray_updates else []
    if main_array_updates:
        targets.append((main_array_updates, ALLOWED_MAIN_ARRAY, main_array))
    if orientation_updates:
        bounds = store._orientation(lines, orientation_id)
        if any("PVObject_ShdTable" in line for line in lines[bounds[0]:bounds[1] + 1]):
            raise ValueError("Orientation has a dependent shading table")
        store._require_no_geometry(lines)
        store._ordinary_fixed_plane(lines, bounds, allow_missing_flags=True, allow_sheds=True)
        targets.append((orientation_updates, ALLOWED_ORIENTATION, bounds))
    uv_changed = False
    for changes, allowed, bounds in targets:
        for name, value in changes.items():
            if (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
                    or not allowed[name][0] <= value <= allowed[name][1]
                    or (name in ("NModSerie", "NInverter", "NStringCh", "NInvMPPT", "NoOrientation") and int(value) != value)):
                raise ValueError(f"Invalid {name} value")
            if name == "PNomAC_InMPPT" and float(f"{value:.2f}") <= 0:
                raise ValueError(f"Invalid {name} value")
            if name == "ModuleQualityLoss":
                for field in ("PQualityModU", "PQualityModI"):
                    indices = store._field_indices(lines, bounds, field)
                    if len(indices) != 1:
                        raise ValueError(f"{field} must occur exactly once in its section")
                    lines[indices[0]] = replace_value(lines[indices[0]], f"{value:.3f}")
                continue
            indices = store._field_indices(lines, bounds, name)
            if len(indices) != 1:
                raise ValueError(f"{name} must occur exactly once in its section")
            if name in ("NInverter", "NStringCh", "NInvMPPT", "NoOrientation"):
                continue
            idx, old = indices[0], lines[indices[0]]
            ending = "\r\n" if old.endswith("\r\n") else "\n" if old.endswith("\n") else ""
            rendered = str(int(value)) if name == "NModSerie" else f"{value:.2f}" if name == "PNomAC_InMPPT" else f"{value:.3f}"
            lines[idx] = f"{old[:old.index('=') + 1]}{rendered}{ending}"
            if name == "UVFactor":
                uv_changed = rendered != FIELD.fullmatch(old.rstrip("\r\n"))[2]
    extras = {}
    if independent_mppt_enabled is not None or power_sharing_enabled is not None or (set(subarray_updates or {}) & {"NInverter", "NStringCh", "NInvMPPT", "NoOrientation"}):
        kinds = store._field_indices(lines, system, "SystemType") if system is not None else []
        if len(kinds) != 1 or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid":
            raise ValueError("Inverter capacity editing requires a grid system")
        store._require_no_geometry(lines, require_shading_section=True)
        fields = store._field_indices(lines, arrays[subarray_id], "NoOrientation")
        if len(fields) != 1:
            raise ValueError("Capacity editing requires one fixed-plane orientation")
        orientation_raw = FIELD.fullmatch(lines[fields[0]].rstrip("\r\n"))[2]
        if not orientation_raw.isdigit():
            raise ValueError("Capacity orientation ID must be an integer")
        orientation = store._orientation(lines, int(orientation_raw))
        types = store._field_indices(lines, orientation, "FieldType")
        if len(types) != 1 or FIELD.fullmatch(lines[types[0]].rstrip("\r\n"))[2] not in ("FixedPlane", "Sheds"):
            raise ValueError("Capacity editing requires a fixed-plane or unlimited sheds orientation")
        if independent_mppt_enabled is not None:
            extras["mppt_mode"] = set_independent_mppt(lines, arrays, subarray_id, independent_mppt_enabled)
        if power_sharing_enabled is not None:
            extras["power_sharing"] = set_power_sharing(lines, arrays, subarray_id, power_sharing_enabled)
        if set(subarray_updates or {}) & {"NInverter", "NStringCh", "NInvMPPT"}:
            extras["capacity"] = resize_capacity(
                lines, arrays, subarray_id,
                **{k: int(subarray_updates[f]) for k, f in (("target_inverters", "NInverter"), ("target_strings", "NStringCh"), ("target_mppt", "NInvMPPT")) if f in subarray_updates},
            )
        if "NoOrientation" in (subarray_updates or {}):
            if grid_inventory(lines, arrays)["counts_valid"] is not True:
                raise ValueError("Orientation assignment requires complete consistent grid counts")
            if [gid for _, _, gid in store._orientation_list(lines)[1]] != list(range(1, len(store._orientation_list(lines)[1]) + 1)):
                raise ValueError("Orientation assignment requires sequential ordered IDs")
            branches, root = subarray_branches(lines, arrays, subarray_id)
            require_plane_branch_mode(root, branches)
            if root.one("NStrOrient1")[1] != root.one("NStringCh")[1] or any(k.startswith("NStrOrient") and k != "NStrOrient1" and root.one(k)[1] != "0" for k in root.fields):
                raise ValueError("Orientation assignment requires all strings in one orientation")
            destination = int(subarray_updates["NoOrientation"])
            store._ordinary_fixed_plane(lines, orientation)
            store._ordinary_fixed_plane(lines, store._orientation(lines, destination))
            if int(orientation_raw) != destination:
                idx = arrays[subarray_id][0] + root.one("NoOrientation")[0]
                lines[idx] = replace_value(lines[idx], str(destination))
            store._orientation_list(lines)
            extras["orientation_assignment"] = {"subarray_id": subarray_id, "before": int(orientation_raw), "after": destination}
    if "PMismMPP" in loss_modes:
        extras["mismatch"] = loss_modes["PMismMPP"]
    if "ModuleQualityLoss" in loss_modes:
        extras["module_quality"] = loss_modes["ModuleQualityLoss"]
    return uv_changed, extras


def _apply_one_control(store, lines, arrays, system, *, param_key, control, result_key, payload,
                       subarray_id, orientation_id, bom_prefix):
    from pvsyst_auto.energy.dc import (
        replace_ageing_enabled, replace_ageing_model, replace_iam_model,
        replace_soiling, replace_spectral_correction, replace_string_mismatch,
    )
    from pvsyst_auto.energy.profiles import replace_system_unavailability
    from pvsyst_auto.energy.system import (
        replace_auxiliaries, replace_inverter_temperature, replace_power_controls, replace_power_factor,
    )
    from pvsyst_auto.project.bifacial import bifacial_state, pan_is_bifacial, replace_bifacial, replace_seasonal

    ageing_tx = param_key in ("ageing_updates", "ageing_enabled")
    if param_key == "power_controls_updates":
        control = "Combined power controls"
    if param_key == "bifacial_updates":
        orient_bounds = store._orientation(lines, orientation_id)
        if isinstance(payload, dict) and payload.get("enabled", True) is not False and bifacial_state(lines, orient_bounds).get("enabled") is False:
            for bounds in arrays.values():
                orient_idx = store._field_indices(lines, bounds, "NoOrientation")
                if len(orient_idx) == 1 and FIELD.fullmatch(lines[orient_idx[0]].rstrip("\r\n"))[2].strip() == str(orientation_id):
                    pan_idx = store._field_indices(lines, bounds, "PVModule")
                    if len(pan_idx) == 1:
                        pan_name = FIELD.fullmatch(lines[pan_idx[0]].rstrip("\r\n"))[2].strip()
                        if pan_is_bifacial(store.components, pan_name) is False:
                            raise ValueError(f"Selected PAN {pan_name} is monofacial (pvModule Flags lacks bifacial bit 23 / $00800000); PVsystCLI ignores pvBiFacial unless the .PAN module declares bifaciality")
        return {result_key: replace_bifacial(lines, orient_bounds, payload)}
    if param_key == "spectral_updates":
        return {result_key: replace_spectral_correction(lines, store._main_array(lines), payload)}
    if param_key == "native_economics_updates":
        return {result_key: replace_native_economics(lines, payload)}

    main = store._main_array(lines)
    versions = [store._field_indices(lines, b, "Version") for b in arrays.values()]
    header_end = next(i for i in range(1, len(lines)) if lines[i].strip().startswith("PVObject_"))
    root_versions = store._field_indices(lines, (0, header_end), "Version")
    kinds = store._field_indices(lines, system, "SystemType") if system is not None else []
    whole_system_control = param_key in (
        "soiling_updates", "grid_limit_updates", "auxiliary_updates", "inverter_temperature_updates",
        "power_factor_updates", "p50_p90_updates", "power_controls_updates", "ageing_updates",
        "ageing_enabled", "system_unavailability_updates",
    )
    accepted_versions = NATIVE_TEXT_OR_SEED if (param_key == "system_unavailability_updates" or ageing_tx) else NATIVE_TEXT
    version_label = SEED_LABEL if (param_key == "system_unavailability_updates" or ageing_tx) else TEXT_LABEL
    if (len(root_versions) != 1 or FIELD.fullmatch(lines[root_versions[0]].rstrip("\r\n"))[2] not in accepted_versions
            or main is None or (not whole_system_control and len(arrays) != 1) or len(kinds) != 1
            or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid"
            or any(len(v) != 1 or FIELD.fullmatch(lines[v[0]].rstrip("\r\n"))[2] not in accepted_versions for v in versions)
            or (param_key != "system_unavailability_updates" and not ageing_tx and has_object(lines, "pvGridStorage"))):
        raise ValueError(f"{control} editing requires a native {version_label} PV-only single-subarray Grid variant")
    if param_key == "ageing_updates":
        return {result_key: replace_ageing_model(lines, main, payload)}
    if param_key == "ageing_enabled":
        return {result_key: replace_ageing_enabled(lines, main, payload)}
    flags_idx = store._field_indices(lines, main, "Flags")
    if len(flags_idx) != 1 or not store._is_verified_main_flags(FIELD.fullmatch(lines[flags_idx[0]].rstrip("\r\n"))[2]):
        raise ValueError(f"{control} editing requires verified main-array Flags/Flags2")
    if not ageing_tx:
        flags2_idx = store._field_indices(lines, main, "Flags2")
        if len(flags2_idx) != 1 or not store._is_verified_main_flags2(FIELD.fullmatch(lines[flags2_idx[0]].rstrip("\r\n"))[2]):
            raise ValueError(f"{control} editing requires verified main-array Flags/Flags2")
    if param_key == "seasonal_updates":
        store._require_no_geometry(lines)
    if not whole_system_control:
        if sum(line.strip() == "PVObject_=pvOrient" for line in lines) != 1:
            raise ValueError(f"{control} editing currently verifies one fixed plane")
        if param_key != "seasonal_updates":
            store._ordinary_fixed_plane(lines, store._orientation(lines, 1))
    if param_key == "soiling_updates":
        return {result_key: replace_soiling(lines, main, payload)}
    if param_key == "grid_limit_updates":
        return {result_key: replace_grid_limit(lines, system, payload)}
    if param_key == "auxiliary_updates" and payload.get("mode") != "profile":
        return {result_key: replace_auxiliaries(lines, system, payload)}
    if param_key == "system_unavailability_updates":
        root_flags = store._field_indices(lines, (0, header_end), "Flags")
        if len(root_flags) != 1 or not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", FIELD.fullmatch(lines[root_flags[0]].rstrip("\r\n"))[2]):
            raise ValueError("System unavailability requires hexadecimal root Flags")
        return {result_key: replace_system_unavailability(lines, system, payload)}

    horizon_sensitive = param_key in (
        "seasonal_updates", "inverter_temperature_updates", "power_factor_updates", "p50_p90_updates",
        "power_controls_updates", "string_mismatch_updates", "ageing_updates", "ageing_enabled", "iam_updates",
    ) or (param_key == "auxiliary_updates" and payload.get("mode") == "profile")
    if (not whole_system_control and orientation_id != 1
            or (not whole_system_control and [g[2] for g in store._orientation_list(lines)[1]] != [1])
            or (param_key not in ("inverter_temperature_updates", "power_factor_updates", "power_controls_updates", "p50_p90_updates", "auxiliary_updates")
                and has_object(lines, "pvLoad"))
            or (horizon_sensitive and any("PVObject_Horizon=" in line for line in lines))):
        raise ValueError(f"{control} editing requires one orientation, no loads and a supported horizon state")
    if param_key not in ("inverter_temperature_updates", "power_factor_updates", "power_controls_updates", "auxiliary_updates"):
        flags = store._field_indices(lines, system, "Flags")
        if len(flags) != 1 or not re.fullmatch(r"\$0{1,8}", FIELD.fullmatch(lines[flags[0]].rstrip("\r\n"))[2]):
            raise ValueError(f"{control} editing requires plain Grid system Flags=$00")
    bounds = None
    if not whole_system_control:
        bounds = next(iter(arrays.values()))
        root = next(b for b in objects("".join(lines[bounds[0]:bounds[1] + 1])) if not b.path)
        strings = root.one("NStringCh")[1]
        if (root.one("NoOrientation")[1] != "1" or not strings.isdigit() or int(strings) < 1
                or root.one("NStrOrient1")[1] != strings
                or any(k.startswith("NStrOrient") and k != "NStrOrient1" and root.one(k)[1] != "0" for k in root.fields)
                or grid_inventory(lines, arrays)["counts_valid"] is not True):
            raise ValueError(f"{control} editing requires complete circuit counts and every string in orientation 1")
        if ageing_tx or param_key == "iam_updates":
            sflags = store._field_indices(lines, bounds, "Flags")
            if len(sflags) != 1 or FIELD.fullmatch(lines[sflags[0]].rstrip("\r\n"))[2] != "$019E1002" or any(re.match(r"\s*P50_P90\s*=", ln) for ln in lines):
                raise ValueError(f"{control} editing requires the verified 019E1002 subarray Flags and no P50/P90 assignment")
        if param_key == "string_mismatch_updates":
            sflags = store._field_indices(lines, bounds, "Flags")
            if (len(sflags) != 1 or int(FIELD.fullmatch(lines[sflags[0]].rstrip("\r\n"))[2].lstrip("$"), 16) & ~0x00020000 != 0x019C1002
                    or any("P50_P90=TP50_P90" in ln for ln in lines)):
                raise ValueError(f"{control} editing requires the verified 0x019C1002 subarray flag family and no P50/P90 block")
    dispatch = {
        "inverter_temperature_updates": lambda: replace_inverter_temperature(lines, system, payload),
        "power_factor_updates": lambda: replace_power_factor(lines, system, payload),
        "p50_p90_updates": lambda: replace_p50(lines, payload),
        "power_controls_updates": lambda: replace_power_controls(lines, system, payload),
        "auxiliary_updates": lambda: replace_auxiliaries(lines, system, payload),
        "string_mismatch_updates": lambda: replace_string_mismatch(lines, bounds, payload),
        "ageing_updates": lambda: replace_ageing_model(lines, main, payload),
        "ageing_enabled": lambda: replace_ageing_enabled(lines, main, payload),
        "iam_updates": lambda: replace_iam_model(lines, bounds, payload),
    }
    if param_key in dispatch:
        return {result_key: dispatch[param_key]()}
    name_key = "orientation_name" if param_key == "seasonal_updates" else "profile_name"
    display_name = payload.get(name_key) if isinstance(payload, dict) else None
    if display_name is not None:
        store._validate_display_name(display_name, name_key)
        if not display_name.isascii() and not bom_prefix:
            raise ValueError(f"Non-ASCII {name_key} requires an explicit UTF-8 BOM")
    return {result_key: replace_seasonal(lines, store._orientation(lines, 1), payload) if param_key == "seasonal_updates" else replace_horizon(lines, payload)}


def update_parameters(store, project: str, variant: str, expected_sha256: str | None = None, subarray_id: int = 1,
                      orientation_id: int = 1, subarray_updates: dict[str, float] | None = None,
                      orientation_updates: dict[str, float] | None = None, generator_updates: dict | None = None,
                      dry_run: bool = False, power_sharing_enabled: bool | None = None,
                      independent_mppt_enabled: bool | None = None, main_array_updates: dict[str, float] | None = None,
                      variant_name: str | None = None, add_utf8_bom: bool = False, **control_params) -> dict:
    expected_controls = {k for k, _, _, _ in _EXCLUSIVE_CONTROLS}
    if unknown := (set(control_params) - expected_controls):
        raise TypeError(f"Unexpected update parameter(s): {sorted(unknown)}")
    requested = ((subarray_updates, ALLOWED_SUBARRAY), (orientation_updates, ALLOWED_ORIENTATION), (main_array_updates, ALLOWED_MAIN_ARRAY))
    _validate_request(
        store, requested=requested, generator_updates=generator_updates, dry_run=dry_run, add_utf8_bom=add_utf8_bom,
        variant_name=variant_name, power_sharing_enabled=power_sharing_enabled,
        independent_mppt_enabled=independent_mppt_enabled, control_params=control_params,
    )
    with store.lock:
        store._check_project(project)
        path = store._path(project, variant)
        data = store._read(path)
        store._check_hash(data, expected_sha256)
        text, lines = store._text(data)
        arrays, system = store._sections(lines)
        name_change, bom_prefix = None, (BOM if data.startswith(BOM) else b"")
        if variant_name is not None:
            bom_prefix = store._display_name_prefix(data, variant_name, add_utf8_bom, "variant_name")
            index, = store._display_name_fields(lines, complete=True)
            name_change = {"before": FIELD.fullmatch(lines[index].rstrip("\r\n"))[2], "after": variant_name}
            lines[index] = replace_value(lines[index], variant_name)
        uv_changed, core_extras = _apply_core_edits(
            store, lines, arrays, system, subarray_id=subarray_id, orientation_id=orientation_id,
            subarray_updates=subarray_updates, orientation_updates=orientation_updates,
            main_array_updates=main_array_updates, power_sharing_enabled=power_sharing_enabled,
            independent_mppt_enabled=independent_mppt_enabled,
        )
        generator = replace_generator(lines, arrays, system, generator_updates, store.components) if generator_updates is not None else None
        active = [(p, lbl, rkey) for p, lbl, rkey, _ in _EXCLUSIVE_CONTROLS if control_params.get(p) is not None]
        control_extras = {}
        for param_key, control, result_key in active:
            cur_arrays, cur_system = store._sections(lines)
            control_extras.update(_apply_one_control(
                store, lines, cur_arrays, cur_system, param_key=param_key, control=control,
                result_key=result_key, payload=control_params[param_key], subarray_id=subarray_id,
                orientation_id=orientation_id, bom_prefix=bom_prefix,
            ))
        cache_clear = None
        non_title_changed = bool(subarray_updates or orientation_updates or main_array_updates or generator_updates
                                 or power_sharing_enabled is not None or independent_mppt_enabled is not None or control_params
                                 ) and "".join(lines).encode("utf-8") != (data[3:] if data.startswith(BOM) else data)
        if uv_changed or non_title_changed:
            from pvsyst_auto.native.headless import strip_result_cache
            lines, cache_clear = strip_result_cache(lines)
        candidate = bom_prefix + "".join(lines).encode("utf-8")
        store._text(candidate)
        diff = "".join(itertools.islice(difflib.unified_diff(text.splitlines(keepends=True), lines, fromfile=path.name, tofile=path.name + " (candidate)"), 501))
        result = {
            "project": project, "variant": variant.upper(), "before_sha256": hashlib.sha256(data).hexdigest(),
            "sha256": hashlib.sha256(candidate).hexdigest(), "changed": candidate != data, "dry_run": dry_run,
            "diff": diff[:32000], "diff_truncated": len(diff) > 32000 or len(diff.splitlines()) > 500,
        }
        if cache_clear and any(cache_clear.values()):
            result["derived_cache"] = cache_clear
        result.update(control_extras)
        if name_change is not None:
            result.update(name_change=name_change, bom_added=bool(bom_prefix) and not data.startswith(BOM))
        result.update(core_extras)
        if generator is not None:
            new_arrays, new_system = store._sections(lines)
            result.update(generator_before=generator["before"], generator_after=generator_state(lines, new_arrays, new_system),
                          generator_component=generator["component"], warnings=generator["warnings"])
        if dry_run or candidate == data:
            return result

        def generator_unchanged():
            if generator is not None and generator["component"] is not None:
                comp = generator["component"]
                if comp["library"] == "builtin" and store.components._path("GEN", comp["filename"]).exists():
                    raise ValueError("Generator component changed: workspace now shadows builtin")
                if hashlib.sha256(store.components._read(store.components._path("GEN", comp["filename"], comp["library"]))).hexdigest() != comp["sha256"]:
                    raise ValueError("Generator component changed while preparing the edit")

        backup = store._replace_checked(path, data, candidate, prepare=lambda: store._backup(path, data), verify=generator_unchanged)
        return {**result, "backup_name": backup}


# --- Read-only variant inspection ---

def legacy_dcgrid_read_scope(store, lines: list[str], system) -> dict | None:
    """Read-only declarations for legacy 8.0 DCGrid variants."""
    if system is None:
        return None
    header_end = next((i for i in range(1, len(lines)) if lines[i].strip().startswith("PVObject_")), len(lines))
    root_versions, kinds = store._field_indices(lines, (0, header_end), "Version"), store._field_indices(lines, system, "SystemType")
    if len(root_versions) != 1 or len(kinds) != 1:
        return None
    version = FIELD.fullmatch(lines[root_versions[0]].rstrip("\r\n"))[2]
    system_type = FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2]
    if not re.fullmatch(r"8\.0(?:\.[0-9]+)?", version) or system_type != "DCGrid":
        return None
    declared = {"SystemType": system_type}
    for name in ("TypeRegul", "VNomAlim", "VPVMax", "RGridPV", "RGridAlim"):
        if len(indices := store._field_indices(lines, system, name)) == 1:
            declared[name] = FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]
    return {"present": True, "readonly": True, "format_family": "DCGrid", "source_version": version, "system_declarations": declared}


def inspect_parameters(store, project: str, variant: str, include_saved_results: bool = False,
                       include_economics: bool = False, include_topology: bool = False) -> dict:
    from pvsyst_auto.energy.dc import ageing_model_state, dc_cabling_state, iam_model_state, soiling_state, string_mismatch_state
    from pvsyst_auto.energy.profiles import system_unavail_state
    from pvsyst_auto.energy.storage import grid_storage_state, peak_shaving_state
    from pvsyst_auto.energy.system import (
        ac_cabling_state, auxiliary_state, inverter_temperature_state, power_controls_state, power_factor_state,
    )
    from pvsyst_auto.project.bifacial import SHED_TYPES, SHED_WRITABLE as SHED_FIELDS, bifacial_state
    from pvsyst_auto.project.tracking import WRITABLE as TRACKING_FIELDS
    from pvsyst_auto.project.variants import economic_block, saved_results
    from pvsyst_auto.scene.reader import shading_state

    with store.lock:
        store._check_project(project)
        data = store._read(store._path(project, variant))
        text, lines = store._text(data)
        arrays, system = store._sections(lines)
        fields = lambda b, names: {n: vals[0] for n in names if len(vals := [FIELD.fullmatch(lines[i].rstrip("\r\n"))[2] for i in store._field_indices(lines, b, n)]) == 1}
        single = lambda b, n: (FIELD.fullmatch(lines[idx[0]].rstrip("\r\n"))[2] if len(idx := store._field_indices(lines, b, n)) == 1 else None)

        def inverter_state(bounds):
            state = {n: val for n in ("PVModule", "GInverter") if (val := single(bounds, n)) is not None}
            for n in ("NInverter", "NInvMPPT", "PowerSharingInverterId"):
                if (val := single(bounds, n)) is not None and re.fullmatch(r"-?[0-9]+", val):
                    state[n] = int(val)
            if (raw := single(bounds, "PNomAC_InMPPT")) is not None:
                try:
                    val = float(raw)
                    if math.isfinite(val):
                        state["PNomAC_InMPPT"] = val
                except ValueError:
                    pass
            return state

        orientations = []
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped not in ("PVObject_=pvOrient", "PVObject_=pvDomeOrient"):
                continue
            is_dome = stripped == "PVObject_=pvDomeOrient"
            closing = "End of TDomeOrientGroup" if is_dome else "End of TOrientGroup"
            end = next((i for i in range(index + 1, len(lines)) if lines[i].strip() == closing), None)
            if end is None:
                raise ValueError("Unclosed orientation")
            indices = store._field_indices(lines, (index, end), "NoOrient")
            if len(indices) != 1:
                raise ValueError("Orientation requires one ID")
            number = FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2]
            if not number.isdigit():
                raise ValueError("Orientation ID must be numeric")
            store._orientation(lines, int(number))
            ori_fields = ("FieldType", "FieldTilt", "FieldAzim", "SummerTilt", "WinterTilt", "WinterMonths", "UsesTiltAver", "UsesAzimAver", "UsesSlopeAver")
            if is_dome:
                ori_fields = (*ori_fields, "BaseSlope", "NoPairOrient", "IsMainOrient")
            vals = fields((index, end), ori_fields)
            vals.update(fields((index, end), TRACKING_FIELDS.get(vals.get("FieldType"), ())))
            if vals.get("FieldType") in SHED_TYPES:
                vals.update(fields((index, end), SHED_FIELDS.values()))
            orient_entry = {"id": int(number), "values": vals}
            if (bif := bifacial_state(lines, (index, end))).get("enabled") or bif.get("issue"):
                orient_entry["bifacial"] = bif
            orientations.append(orient_entry)

        main_array, names = store._main_array(lines), store._display_name_fields(lines)
        result = {
            "project": project, "variant": variant.upper(),
            "variant_name": FIELD.fullmatch(lines[names[0]].rstrip("\r\n"))[2] if len(names) == 1 else None,
            "utf8_bom": data.startswith(BOM),
            "main_array": {"values": fields(main_array, ("UFactor", "UVFactor", "Flags", "Flags2", "MVNbTransfos", "VOutTransfo")) if main_array is not None else {},
                           "soiling": soiling_state(lines, main_array) if main_array is not None else None},
            "sha256": hashlib.sha256(data).hexdigest(), "orientations": orientations,
            "system_parameters": fields(system, ("SystemType", "Flags", "TempShift", "TempFixed", "TanPhi", "TanPhiM", "PLimGrid", "LimGrSeparLoss")) if system is not None else {},
            "generator": generator_state(lines, arrays, system), "grid_limit": grid_limit_state(lines, system),
            "auxiliaries": auxiliary_state(lines, system), "horizon": horizon_state(lines),
            "power_factor": power_factor_state(lines, system), "p50_p90": p50_state(lines),
            "power_controls": power_controls_state(lines, system),
            "ageing": ageing_model_state(lines, main_array) if main_array is not None else None,
            "inverter_temperature": inverter_temperature_state(lines, system), "transformers": transformer_state(lines),
            "ac_cabling": ac_cabling_state(lines), "dc_cabling": dc_cabling_state(lines),
            "system_unavailability": system_unavail_state(lines, system), "peak_shaving": peak_shaving_state(lines),
            "grid_storage": grid_storage_state(lines, system), "shading_scene": shading_state(lines),
            "source_metadata": {"stored_source_values": True},
            "subarrays": [
                {
                    "id": identifier,
                    "values": fields(bounds, ("NModSerie", "NStringCh", "NStrOrient1", "NInverter", "NInvMPPT", "NoOrientation", "NoOrientation2", "VBkUpEncl_syst", "VBkUpDecl_syst", "LIDLoss", "PMismMPP", "PQualityModU", "PQualityModI", "PQualityModP", "Flags")),
                    "inverter_state": inverter_state(bounds), "string_mismatch": string_mismatch_state(lines, bounds),
                    "iam": iam_model_state(lines, bounds),
                }
                for identifier, bounds in sorted(arrays.items())
            ],
        }
        if (legacy_scope := legacy_dcgrid_read_scope(store, lines, system)) is not None:
            result["legacy_read_scope"] = legacy_scope
        if include_saved_results:
            result["saved_results"] = saved_results(text, lines)
        if include_economics:
            result["economics"] = economic_block(lines)
        if include_topology:
            result["topology"] = topology_state(lines)
        return result

"""Transactional parameter updates for PVsyst variants."""
from __future__ import annotations

import difflib
import hashlib
import itertools
import math
import re

from pvsyst_auto.components.editor import has_object, objects, replace_value
from pvsyst_auto.components.generator import replace_generator, state as generator_state
from pvsyst_auto.components.store import FIELD
from pvsyst_auto.energy.ageing import replace_ageing_enabled, replace_ageing_model
from pvsyst_auto.energy.auxiliaries import replace_auxiliaries
from pvsyst_auto.energy.iam import replace_iam_model
from pvsyst_auto.energy.inverter_temperature import replace_inverter_temperature
from pvsyst_auto.energy.mismatch import replace_string_mismatch
from pvsyst_auto.energy.power import replace_power_controls
from pvsyst_auto.energy.power_factor import replace_power_factor
from pvsyst_auto.energy.soiling import replace_soiling
from pvsyst_auto.energy.unavailability import replace_system_unavailability
from pvsyst_auto.native.grid_limit import replace_grid_limit
from pvsyst_auto.native.horizon import replace_horizon
from pvsyst_auto.native.p50 import replace_p50
from pvsyst_auto.versions import NATIVE_TEXT, NATIVE_TEXT_OR_SEED, SEED_LABEL, TEXT_LABEL
from pvsyst_auto.project.bifacial import replace_bifacial
from pvsyst_auto.project.circuit import (
    inventory as grid_inventory,
    resize_capacity,
    set_independent_mppt,
    set_power_sharing,
)
from pvsyst_auto.project.seasonal import replace_seasonal
from pvsyst_auto.project.variant_topology import require_plane_branch_mode, subarray_branches

ALLOWED_SUBARRAY = {
    "NModSerie": (1, 100),
    "NInverter": (1, 10000),
    "NStringCh": (1, 2147483647),
    "NoOrientation": (1, 2147483647),
    "NInvMPPT": (1, 10000),
    "VBkUpEncl_syst": (0, 1),
    "VBkUpDecl_syst": (0, 1),
    "LIDLoss": (0, 100),
    "PMismMPP": (0, 100),
    "ModuleQualityLoss": (-5, 20),
    "PNomAC_InMPPT": (0, math.inf),
}
ALLOWED_ORIENTATION = {"FieldTilt": (0, 90), "FieldAzim": (-180, 180)}
ALLOWED_MAIN_ARRAY = {"UFactor": (15, 100), "UVFactor": (-math.inf, math.inf)}

# (param_key, label, result_key, error_verb)
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
)


def _validate_request(store, *, requested, generator_updates, dry_run, add_utf8_bom, variant_name,
                      power_sharing_enabled, independent_mppt_enabled, control_params):
    ageing_enabled = control_params.get("ageing_enabled")
    if ageing_enabled is not None and type(ageing_enabled) is not bool:
        raise ValueError("ageing_enabled must be a strict boolean")

    has_core_edit = (
        any(changes for changes, _ in requested)
        or generator_updates is not None
        or power_sharing_enabled is not None
        or independent_mppt_enabled is not None
    )
    active_controls = [(param, label, verb) for param, label, _, verb in _EXCLUSIVE_CONTROLS
                       if control_params.get(param) is not None]
    if active_controls and (has_core_edit or len(active_controls) > 1):
        _, label, verb = active_controls[0]
        if any(p == "system_unavailability_updates" for p, _, _ in active_controls):
            label, verb = "System-unavailability removal", "requires"
        raise ValueError(f"{label} {verb} a separate transaction; variant_name may accompany it")

    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    if not isinstance(add_utf8_bom, bool):
        raise ValueError("add_utf8_bom must be a boolean")
    if add_utf8_bom and variant_name is None:
        raise ValueError("add_utf8_bom requires variant_name")
    if variant_name is not None:
        store._validate_display_name(variant_name, "variant_name")

    for name, switch in (("power_sharing_enabled", power_sharing_enabled),
                         ("independent_mppt_enabled", independent_mppt_enabled)):
        if switch is not None:
            if not isinstance(switch, bool):
                raise ValueError(f"{name} must be a boolean")
            if (any(changes for changes, _ in requested) or generator_updates is not None
                    or variant_name is not None
                    or (power_sharing_enabled is not None and independent_mppt_enabled is not None)):
                raise ValueError("Mode/sharing changes require separate parameter transactions")

    if (not any(changes for changes, _ in requested) and not generator_updates
            and variant_name is None and power_sharing_enabled is None
            and independent_mppt_enabled is None and not active_controls) or any(
            changes is not None and (not isinstance(changes, dict) or any(key not in allowed for key in changes))
            for changes, allowed in requested):
        raise ValueError("Provide supported parameter updates or variant_name")

    subarray_updates = requested[0][0]
    if generator_updates is not None and (not isinstance(generator_updates, dict) or not generator_updates):
        raise ValueError("generator_updates must be a nonempty dictionary")
    if generator_updates is not None and set(subarray_updates or {}) & {"VBkUpEncl_syst", "VBkUpDecl_syst"}:
        raise ValueError("Use generator_updates.thresholds for thresholds in a generator transaction")


def _apply_core_edits(store, lines, arrays, system, *, subarray_id, orientation_id,
                      subarray_updates, orientation_updates, main_array_updates,
                      power_sharing_enabled, independent_mppt_enabled):
    main_array = store._main_array(lines) if main_array_updates else None
    uv_only = bool(main_array_updates) and set(main_array_updates) == {"UVFactor"}
    if main_array_updates and "UVFactor" in main_array_updates and not uv_only:
        raise ValueError("UVFactor edits are a separate transaction; do not combine with UFactor")
    if main_array_updates and not uv_only:
        kinds = store._field_indices(lines, system, "SystemType") if system is not None else []
        versions = [store._field_indices(lines, bounds, "Version") for bounds in arrays.values()]
        if (main_array is None or len(kinds) != 1 or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid"
                or any(len(indices) != 1 or FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2] not in NATIVE_TEXT_OR_SEED
                       for indices in versions)):
            raise ValueError(f"Thermal editing requires a verified {SEED_LABEL} grid main array")
        for key, accepted in (("Flags", ("$00100400", "$01900400")), ("Flags2", ("$082A",))):
            indices = store._field_indices(lines, main_array, key)
            if len(indices) != 1 or FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2] not in accepted:
                raise ValueError("Thermal editing requires verified main-array Flags/Flags2")
    elif uv_only:
        store._uv_edit_gate(lines, system, arrays, main_array)

    if subarray_updates or power_sharing_enabled is not None or independent_mppt_enabled is not None:
        if not isinstance(subarray_id, int) or isinstance(subarray_id, bool) or subarray_id not in arrays:
            raise ValueError("Subarray ID does not exist")
    for loss in set(subarray_updates or {}) & {"LIDLoss", "PMismMPP", "ModuleQualityLoss"}:
        versions = store._field_indices(lines, arrays[subarray_id], "Version")
        kinds = store._field_indices(lines, system, "SystemType") if system is not None else []
        supported = NATIVE_TEXT_OR_SEED
        if (len(versions) != 1 or FIELD.fullmatch(lines[versions[0]].rstrip("\r\n"))[2] not in supported
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
        index = indices[0]
        raw = FIELD.fullmatch(lines[index].rstrip("\r\n"))[2]
        if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", raw):
            raise ValueError(f"{label} editing requires hexadecimal Flags")
        flags = int(raw[1:], 16)
        loss_modes[key] = {"default_before": bool(flags & mask), "default_after": False}
        if loss_modes[key]["default_before"]:
            lines[index] = replace_value(lines[index], f"${flags & ~mask:0{len(raw) - 1}X}")

    targets = [(subarray_updates, ALLOWED_SUBARRAY, arrays[subarray_id])] if subarray_updates else []
    if main_array_updates:
        targets.append((main_array_updates, ALLOWED_MAIN_ARRAY, main_array))
    if orientation_updates:
        bounds = store._orientation(lines, orientation_id)
        if any("PVObject_ShdTable" in line for line in lines[bounds[0]:bounds[1] + 1]):
            raise ValueError("Orientation has a dependent shading table")
        store._require_no_geometry(lines)
        store._ordinary_fixed_plane(lines, bounds, allow_missing_flags=True)
        targets.append((orientation_updates, ALLOWED_ORIENTATION, bounds))

    uv_changed = False
    for changes, allowed, bounds in targets:
        for name, value in changes.items():
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or not allowed[name][0] <= value <= allowed[name][1]
                    or name in ("NModSerie", "NInverter", "NStringCh", "NInvMPPT", "NoOrientation") and int(value) != value):
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
            index = indices[0]
            old = lines[index]
            prefix = old[:old.index("=") + 1]
            ending = "\r\n" if old.endswith("\r\n") else "\n" if old.endswith("\n") else ""
            rendered = (str(int(value)) if name == "NModSerie"
                        else f"{value:.2f}" if name == "PNomAC_InMPPT" else f"{value:.3f}")
            lines[index] = f"{prefix}{rendered}{ending}"
            if name == "UVFactor":
                uv_changed = rendered != FIELD.fullmatch(old.rstrip("\r\n"))[2]

    extras = {}
    if independent_mppt_enabled is not None or power_sharing_enabled is not None or (
            subarray_updates and set(subarray_updates) & {"NInverter", "NStringCh", "NInvMPPT", "NoOrientation"}):
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
        if len(types) != 1 or FIELD.fullmatch(lines[types[0]].rstrip("\r\n"))[2] != "FixedPlane":
            raise ValueError("Capacity editing requires a fixed-plane orientation")
        if independent_mppt_enabled is not None:
            extras["mppt_mode"] = set_independent_mppt(lines, arrays, subarray_id, independent_mppt_enabled)
        if power_sharing_enabled is not None:
            extras["power_sharing"] = set_power_sharing(lines, arrays, subarray_id, power_sharing_enabled)
        if set(subarray_updates or {}) & {"NInverter", "NStringCh", "NInvMPPT"}:
            extras["capacity"] = resize_capacity(
                lines, arrays, subarray_id,
                **{key: int(subarray_updates[field]) for key, field in (
                    ("target_inverters", "NInverter"),
                    ("target_strings", "NStringCh"),
                    ("target_mppt", "NInvMPPT"),
                ) if field in subarray_updates},
            )
        if "NoOrientation" in (subarray_updates or {}):
            if grid_inventory(lines, arrays)["counts_valid"] is not True:
                raise ValueError("Orientation assignment requires complete consistent grid counts")
            _, groups = store._orientation_list(lines)
            if [identifier for _, _, identifier in groups] != list(range(1, len(groups) + 1)):
                raise ValueError("Orientation assignment requires sequential ordered IDs")
            branches, root = subarray_branches(lines, arrays, subarray_id)
            require_plane_branch_mode(root, branches)
            if (root.one("NStrOrient1")[1] != root.one("NStringCh")[1] or any(
                    key.startswith("NStrOrient") and key != "NStrOrient1" and root.one(key)[1] != "0"
                    for key in root.fields)):
                raise ValueError("Orientation assignment requires all strings in one orientation")
            destination = int(subarray_updates["NoOrientation"])
            store._ordinary_fixed_plane(lines, orientation)
            store._ordinary_fixed_plane(lines, store._orientation(lines, destination))
            if int(orientation_raw) != destination:
                index = arrays[subarray_id][0] + root.one("NoOrientation")[0]
                lines[index] = replace_value(lines[index], str(destination))
            store._orientation_list(lines)
            extras["orientation_assignment"] = {
                "subarray_id": subarray_id,
                "before": int(orientation_raw),
                "after": destination,
            }

    if "PMismMPP" in loss_modes:
        extras["mismatch"] = loss_modes["PMismMPP"]
    if "ModuleQualityLoss" in loss_modes:
        extras["module_quality"] = loss_modes["ModuleQualityLoss"]
    return uv_changed, extras


def _apply_single_control(store, lines, arrays, system, *, subarray_id, orientation_id,
                          bom_prefix, control_params):
    active = [(p, label, rkey) for p, label, rkey, _ in _EXCLUSIVE_CONTROLS
              if control_params.get(p) is not None]
    if not active:
        return {}
    param_key, control, result_key = active[0]
    payload = control_params[param_key]
    ageing_tx = param_key in ("ageing_updates", "ageing_enabled")
    if param_key == "power_controls_updates":
        control = "Combined power controls"

    if param_key == "bifacial_updates":
        orient_bounds = store._orientation(lines, orientation_id)
        return {result_key: replace_bifacial(lines, orient_bounds, payload)}

    main = store._main_array(lines)
    versions = [store._field_indices(lines, b, "Version") for b in arrays.values()]
    header_end = next(i for i in range(1, len(lines)) if lines[i].strip().startswith("PVObject_"))
    root_versions = store._field_indices(lines, (0, header_end), "Version")
    kinds = store._field_indices(lines, system, "SystemType") if system is not None else []
    whole_system_control = param_key in (
        "soiling_updates", "grid_limit_updates", "auxiliary_updates",
        "inverter_temperature_updates", "power_factor_updates", "p50_p90_updates",
        "power_controls_updates", "ageing_updates", "ageing_enabled", "system_unavailability_updates",
    )
    accepted_versions = NATIVE_TEXT_OR_SEED if param_key == "system_unavailability_updates" else NATIVE_TEXT
    version_label = SEED_LABEL if param_key == "system_unavailability_updates" else TEXT_LABEL
    if (len(root_versions) != 1 or FIELD.fullmatch(lines[root_versions[0]].rstrip("\r\n"))[2] not in accepted_versions
            or main is None or (not whole_system_control and len(arrays) != 1) or len(kinds) != 1
            or FIELD.fullmatch(lines[kinds[0]].rstrip("\r\n"))[2] != "Grid"
            or any(len(v) != 1 or FIELD.fullmatch(lines[v[0]].rstrip("\r\n"))[2] not in accepted_versions for v in versions)
            or (param_key != "system_unavailability_updates" and has_object(lines, "pvGridStorage"))):
        raise ValueError(f"{control} editing requires a native {version_label} PV-only single-subarray Grid variant")

    for key, accepted in (("Flags", ("$00100400", "$01900400")), ("Flags2", ("$082A",))):
        if key == "Flags2" and ageing_tx:
            continue
        indices = store._field_indices(lines, main, key)
        if len(indices) != 1 or FIELD.fullmatch(lines[indices[0]].rstrip("\r\n"))[2] not in accepted:
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
        "seasonal_updates", "inverter_temperature_updates", "power_factor_updates",
        "p50_p90_updates", "power_controls_updates", "string_mismatch_updates",
        "ageing_updates", "ageing_enabled", "iam_updates",
    ) or (param_key == "auxiliary_updates" and payload.get("mode") == "profile")
    if (not whole_system_control and orientation_id != 1
            or (not whole_system_control and [g[2] for g in store._orientation_list(lines)[1]] != [1])
            or (param_key not in ("inverter_temperature_updates", "power_factor_updates", "power_controls_updates", "p50_p90_updates", "auxiliary_updates")
                and has_object(lines, "pvLoad"))
            or (horizon_sensitive and any("PVObject_Horizon=" in line for line in lines))):
        raise ValueError(f"{control} editing requires one orientation, no loads and a supported horizon state")

    if param_key not in ("inverter_temperature_updates", "power_factor_updates",
                         "power_controls_updates", "auxiliary_updates"):
        flags = store._field_indices(lines, system, "Flags")
        if len(flags) != 1 or not re.fullmatch(r"\$0{1,8}", FIELD.fullmatch(lines[flags[0]].rstrip("\r\n"))[2]):
            raise ValueError(f"{control} editing requires plain Grid system Flags=$00")

    if not whole_system_control:
        bounds = next(iter(arrays.values()))
        root = next(block for block in objects("".join(lines[bounds[0]:bounds[1] + 1])) if not block.path)
        strings = root.one("NStringCh")[1]
        if (root.one("NoOrientation")[1] != "1" or not strings.isdigit() or int(strings) < 1
                or root.one("NStrOrient1")[1] != strings
                or any(key.startswith("NStrOrient") and key != "NStrOrient1" and root.one(key)[1] != "0"
                       for key in root.fields) or grid_inventory(lines, arrays)["counts_valid"] is not True):
            raise ValueError(f"{control} editing requires complete circuit counts and every string in orientation 1")

        if ageing_tx or param_key == "iam_updates":
            sflags = store._field_indices(lines, bounds, "Flags")
            if (len(sflags) != 1 or FIELD.fullmatch(lines[sflags[0]].rstrip("\r\n"))[2] != "$019E1002"
                    or any(re.match(r"\s*P50_P90\s*=", line) for line in lines)):
                raise ValueError(f"{control} editing requires the verified 019E1002 subarray Flags and no P50/P90 assignment")
        if param_key == "string_mismatch_updates":
            sflags = store._field_indices(lines, bounds, "Flags")
            if (len(sflags) != 1
                    or int(FIELD.fullmatch(lines[sflags[0]].rstrip("\r\n"))[2].lstrip("$"), 16) & ~0x00020000 != 0x019C1002
                    or any("P50_P90=TP50_P90" in line for line in lines)):
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
    if param_key == "seasonal_updates":
        return {result_key: replace_seasonal(lines, store._orientation(lines, 1), payload)}
    return {result_key: replace_horizon(lines, payload)}


def update_parameters(store, project: str, variant: str, expected_sha256: str | None = None,
                      subarray_id: int = 1, orientation_id: int = 1,
                      subarray_updates: dict[str, float] | None = None,
                      orientation_updates: dict[str, float] | None = None,
                      generator_updates: dict | None = None,
                      dry_run: bool = False, power_sharing_enabled: bool | None = None,
                      independent_mppt_enabled: bool | None = None,
                      main_array_updates: dict[str, float] | None = None,
                      variant_name: str | None = None, add_utf8_bom: bool = False,
                      **control_params) -> dict:
    expected_controls = {k for k, _, _, _ in _EXCLUSIVE_CONTROLS}
    unknown = set(control_params) - expected_controls
    if unknown:
        raise TypeError(f"Unexpected update parameter(s): {sorted(unknown)}")

    requested = ((subarray_updates, ALLOWED_SUBARRAY),
                 (orientation_updates, ALLOWED_ORIENTATION),
                 (main_array_updates, ALLOWED_MAIN_ARRAY))
    _validate_request(
        store, requested=requested, generator_updates=generator_updates,
        dry_run=dry_run, add_utf8_bom=add_utf8_bom, variant_name=variant_name,
        power_sharing_enabled=power_sharing_enabled,
        independent_mppt_enabled=independent_mppt_enabled,
        control_params=control_params,
    )

    with store.lock:
        store._check_project(project)
        path = store._path(project, variant)
        data = store._read(path)
        store._check_hash(data, expected_sha256)
        text, lines = store._text(data)
        arrays, system = store._sections(lines)

        name_change = None
        bom_prefix = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
        if variant_name is not None:
            bom_prefix = store._display_name_prefix(data, variant_name, add_utf8_bom, "variant_name")
            index, = store._display_name_fields(lines, complete=True)
            name_change = {"before": FIELD.fullmatch(lines[index].rstrip("\r\n"))[2], "after": variant_name}
            lines[index] = replace_value(lines[index], variant_name)

        uv_changed, core_extras = _apply_core_edits(
            store, lines, arrays, system,
            subarray_id=subarray_id, orientation_id=orientation_id,
            subarray_updates=subarray_updates, orientation_updates=orientation_updates,
            main_array_updates=main_array_updates,
            power_sharing_enabled=power_sharing_enabled,
            independent_mppt_enabled=independent_mppt_enabled,
        )
        generator = replace_generator(lines, arrays, system, generator_updates, store.components) if generator_updates is not None else None
        control_extras = _apply_single_control(
            store, lines, arrays, system,
            subarray_id=subarray_id, orientation_id=orientation_id,
            bom_prefix=bom_prefix, control_params=control_params,
        )

        cache_clear = None
        non_title_changed = bool(
            subarray_updates or orientation_updates or main_array_updates or generator_updates
            or power_sharing_enabled is not None or independent_mppt_enabled is not None
            or control_params
        ) and "".join(lines).encode("utf-8") != (data[3:] if data.startswith(b"\xef\xbb\xbf") else data)
        if uv_changed or (non_title_changed and any(line.strip().startswith("VarList, list of=") for line in lines)):
            from pvsyst_auto.native.headless import strip_result_cache
            lines, cache_clear = strip_result_cache(lines)

        edited_text = "".join(lines)
        candidate = bom_prefix + edited_text.encode("utf-8")
        store._text(candidate)
        diff = "".join(itertools.islice(difflib.unified_diff(
            text.splitlines(keepends=True), lines, fromfile=path.name,
            tofile=path.name + " (candidate)"), 501))
        result = {
            "project": project,
            "variant": variant.upper(),
            "before_sha256": hashlib.sha256(data).hexdigest(),
            "sha256": hashlib.sha256(candidate).hexdigest(),
            "changed": candidate != data,
            "dry_run": dry_run,
            "diff": diff[:32000],
            "diff_truncated": len(diff) > 32000 or len(diff.splitlines()) > 500,
        }
        if cache_clear is not None:
            result["derived_cache"] = cache_clear
        result.update(control_extras)
        if name_change is not None:
            result.update(name_change=name_change, bom_added=bool(bom_prefix) and not data.startswith(b"\xef\xbb\xbf"))
        result.update(core_extras)
        if generator is not None:
            new_arrays, new_system = store._sections(lines)
            result.update(
                generator_before=generator["before"],
                generator_after=generator_state(lines, new_arrays, new_system),
                generator_component=generator["component"],
                warnings=generator["warnings"],
            )
        if dry_run or candidate == data:
            return result

        def generator_unchanged():
            if generator is not None and generator["component"] is not None:
                component = generator["component"]
                if (component["library"] == "builtin" and
                        store.components._path("GEN", component["filename"]).exists()):
                    raise ValueError("Generator component changed: workspace now shadows builtin")
                target = store.components._path("GEN", component["filename"], component["library"])
                if hashlib.sha256(store.components._read(target)).hexdigest() != component["sha256"]:
                    raise ValueError("Generator component changed while preparing the edit")

        backup = store._replace_checked(path, data, candidate, prepare=lambda: store._backup(path, data),
                                       verify=generator_unchanged)
        return {**result, "backup_name": backup}

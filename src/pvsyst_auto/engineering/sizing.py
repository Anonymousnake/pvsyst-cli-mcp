"""Bounded uniform-MPPT capacity planning against explicitly declared design bounds."""
from __future__ import annotations

import math
import re

from pvsyst_auto.components.editor import replace_value, root_block
from pvsyst_auto.project.circuit import circuit_nodes, inventory, positive_integer, resize_capacity

NUMBERS = (
    "target_dc_kw", "max_dc_ac_ratio", "cold_voc_v", "hot_vmp_v", "cold_vmp_v",
    "module_max_operating_current_a", "module_max_short_circuit_current_a",
    "mppt_max_operating_current_a", "mppt_max_short_circuit_current_a",
    "design_min_temperature_c", "design_min_operating_temperature_c", "design_max_operating_temperature_c",
)


def plan_capacity(store, lines, array_id, pan, inverter, request):
    """No writes/fitting: keep mode and full homogeneous hardware MPPT allocation."""
    result = {"status": "blocked", "issues": [], "scope": "Declared design envelope, not native temperature-model reproduction or design certification"}
    def issue(field, message):
        result["issues"].append({"field": "capacity." + field, "message": message})
    allowed = {*NUMBERS, "capacity_tolerance_pct", "voltage_standard", "design_basis"}
    if not isinstance(request, dict):
        issue("request", "Use a capacity object")
        return result
    for key in request.keys() - allowed:
        issue(key, "Unknown capacity input")
    for name in (*NUMBERS, "voltage_standard", "design_basis"):
        if request.get(name) is None or request.get(name) == "":
            issue(name, "Required design input is missing; no value is inferred")
    values = {**request, "capacity_tolerance_pct": request.get("capacity_tolerance_pct", 5)}
    for name in (*NUMBERS, "capacity_tolerance_pct"):
        value = values.get(name)
        if value is None and name == "capacity_tolerance_pct":
            issue(name, "Use a finite strict numeric tolerance, not null")
        elif value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            issue(name, "Use a finite strict numeric value")
    if result["issues"]:
        return result
    for name in NUMBERS:
        if not name.startswith("design_") and values[name] <= 0:
            issue(name, "Design magnitudes must be positive")
    if not 0 <= values["capacity_tolerance_pct"] <= 20:
        issue("capacity_tolerance_pct", "Use 0..20 percent")
    if values["voltage_standard"] not in ("IEC", "UL"):
        issue("voltage_standard", "Explicitly choose IEC or UL")
    basis = values["design_basis"]
    if not isinstance(basis, str) or not basis.strip() or len(basis) > 2000 or not basis.isprintable():
        issue("design_basis", "Provide a printable source/method for the declared design envelope, up to 2000 characters")
    if not (-100 <= values["design_min_temperature_c"] <= values["design_min_operating_temperature_c"]
            <= values["design_max_operating_temperature_c"] <= 150):
        issue("design_min_temperature_c", "Require -100 <= ambient minimum <= operating minimum <= operating maximum <= 150 degC")
    if not values["hot_vmp_v"] <= values["cold_vmp_v"] <= values["cold_voc_v"]:
        issue("cold_voc_v", "Require positive hot Vmp <= cold operating Vmp <= minimum-temperature Voc")
    if values["module_max_short_circuit_current_a"] < values["module_max_operating_current_a"]:
        issue("module_max_short_circuit_current_a", "Short-circuit envelope must cover operating-current envelope")
    if values["mppt_max_short_circuit_current_a"] < values["mppt_max_operating_current_a"]:
        issue("mppt_max_short_circuit_current_a", "Short-circuit rating must cover operating-current rating")
    if result["issues"]:
        return result
    try:
        arrays, system = store._sections(lines)
        if len(arrays) != 1 or array_id not in arrays:
            raise ValueError("Capacity planning requires one subarray")
        store._require_no_geometry(lines, require_shading_section=True)
        bounds = arrays[array_id]
        root = root_block(lines, bounds)
        raw_flags = root.one("Flags")[1]
        if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", raw_flags):
            raise ValueError("Require explicit hexadecimal subarray Flags")
        flags = int(raw_flags[1:], 16)
        types = store._field_indices(lines, system, "SystemType") if system is not None else []
        if len(types) != 1 or lines[types[0]].split("=", 1)[1].strip() != "Grid":
            raise ValueError("Capacity planning requires a Grid system")
        if (root.one("NStrOrient1")[1] != root.one("NStringCh")[1] or any(
                key.startswith("NStrOrient") and key != "NStrOrient1" and root.one(key)[1] != "0" for key in root.fields)):
            raise ValueError("Require all strings in the common fixed plane")
        if (root.one("Version")[1] not in ("8.1.0", "8.1.6") or not flags & 0x400
                or flags & 0x100 or root.one("PowerSharingInverterId")[1] != "-1"):
            raise ValueError("Require verified independent MPPT mode with disabled power sharing")
        plane = positive_integer(root.one("NoOrientation")[1], "NoOrientation")
        store._ordinary_fixed_plane(lines, store._orientation(lines, plane))
        checked = inventory(lines, arrays)
        if checked["counts_valid"] is not True:
            raise ValueError("Require complete consistent grid counts")
        def walk(node):
            yield node
            for child in node.children:
                yield from walk(child)
        groups = [node for top in circuit_nodes(lines) for node in walk(top) if node.kind == "InverterNode"]
        if len(groups) != 1 or len(groups[0].children) != 1 or groups[0].children[0].kind != "MPPTNode":
            raise ValueError("Require one exclusive homogeneous inverter group and one repeatable MPPT branch")
        group, port = groups[0], groups[0].children[0]
        if len(port.children) != 1 or port.children[0].kind != "StringNode":
            raise ValueError("Require one homogeneous parallel-string leaf per MPPT")
        leaf = port.children[0]
        if "SEIsHeterogeneousString" in root.fields and root.one("SEIsHeterogeneousString")[1].rstrip(",") != "False":
            raise ValueError("Heterogeneous/optimizer string declarations require separate design handling")
        if any(inverter.get("modes", {}).get(key) != value for key, value in
               (("ModeOper", "MPPT"), ("CompPMax", "Lim"), ("CompVMax", "Lim"))):
            raise ValueError("Require explicitly declared MPPT operation and Lim voltage/power modes")
        ports = positive_integer(port.one("NElements", "1"), "MPPT multiplicity")
        old_inv = positive_integer(group.one("NElements", "1"), "Inverter multiplicity")
        old_leaf = positive_integer(leaf.one("NElements", "1"), "String multiplicity")
        old_series = positive_integer(root.one("NModSerie")[1], "Series modules")
        def declared(name):
            item = inverter["fields"][name]
            if item["status"] != "declared":
                raise ValueError(f"Require a positive declared inverter {name}")
            return item["value"]
        if ports != declared("NbMPPT"):
            raise ValueError("Template must populate every hardware MPPT; partial power budgets are not inferred")
        voltage_min, voltage_max, absolute_max = (declared(name) for name in ("VMppMin", "VMPPMax", "VAbsMax"))
        ac_kw = declared("PNomConv")
        if pan["Technol"] not in (["mtSiMono"], ["mtSiPoly"]) or float(pan["GRef"][0]) != 1000 or float(pan["TRef"][0]) != 25:
            raise ValueError("Require crystalline PAN with GRef=1000 W/m2 and TRef=25 degC")
        module_limit_values = pan.get("VMax" + values["voltage_standard"], [])
        if len(module_limit_values) != 1:
            raise ValueError("Require one PAN module-system voltage rating for the selected standard")
        module_limit = float(module_limit_values[0])
        module_kw = float(pan["PNom"][0]) / 1000
        if not all(math.isfinite(v) and v > 0 for v in (module_limit, module_kw)):
            raise ValueError("Require finite positive module-system voltage and power")
        if (values["design_min_operating_temperature_c"] <= 25 <= values["design_max_operating_temperature_c"]
                and not values["hot_vmp_v"] <= float(pan["Vmp"][0]) <= values["cold_vmp_v"]):
            raise ValueError("Declared operating-voltage envelope must cover PAN reference Vmp when TRef is in range")
        if values["design_min_temperature_c"] <= 25 <= values["design_max_operating_temperature_c"] and values["cold_voc_v"] < float(pan["Voc"][0]):
            raise ValueError("Declared maximum Voc must cover PAN reference Voc")
        if (values["module_max_operating_current_a"] < float(pan["Imp"][0])
                or values["module_max_short_circuit_current_a"] < float(pan["ISC"][0])):
            raise ValueError("Declared module current envelopes must cover PAN reference-point currents")
        max_leaf = int(min(100, values["mppt_max_operating_current_a"] / values["module_max_operating_current_a"],
                           values["mppt_max_short_circuit_current_a"] / values["module_max_short_circuit_current_a"]))
        candidates = []
        for series in range(1, 101):
            if (series * values["hot_vmp_v"] < voltage_min or series * values["cold_vmp_v"] > voltage_max
                    or series * values["cold_voc_v"] > min(absolute_max, module_limit)):
                continue
            for parallel in range(1, max_leaf + 1):
                if "NElements" not in leaf.fields and parallel != old_leaf:
                    continue
                per_inverter = series * ports * parallel * module_kw
                if not math.isfinite(per_inverter) or per_inverter <= 0 or per_inverter / ac_kw > values["max_dc_ac_ratio"]:
                    continue
                ideal = min(10000, max(1, values["target_dc_kw"] / per_inverter))
                for count in {math.floor(ideal), math.ceil(ideal)}:
                    if "NElements" not in group.fields and count != old_inv:
                        continue
                    power = per_inverter * count
                    error = abs(power - values["target_dc_kw"]) / values["target_dc_kw"] * 100
                    if error <= values["capacity_tolerance_pct"] + 1e-9:
                        score = (round(error, 9), count, abs(series - old_series), series, parallel)
                        candidates.append((score, series, parallel, count, power))
        if not candidates:
            raise ValueError("No supported integral plan meets the declared voltage/current/ratio/tolerance bounds; explicit editable node counts may be required")
        _, series, parallel, count, power = min(candidates)
        strings = count * ports * parallel
        candidate = lines.copy()
        steps = []
        if count != old_inv:
            resize_capacity(candidate, arrays, array_id, target_inverters=count)
            steps.append({"NInverter": count})
        current_strings = old_leaf * ports * count
        if strings != current_strings:
            resize_capacity(candidate, arrays, array_id, target_strings=strings)
            steps.append({"NStringCh": strings})
        if series != old_series:
            index, = store._field_indices(candidate, bounds, "NModSerie")
            candidate[index] = replace_value(candidate[index], str(series))
            steps.append({"NModSerie": series})
        final = inventory(candidate, arrays)
        if final["counts_valid"] is not True or final["subarrays"][0]["declared_module_count"] != strings * series:
            raise ValueError("Planned serialized counts do not match the intended capacity")
        result.update(status="ready", design=values, series_modules=series, parallel_strings_per_mppt=parallel,
            mppt_per_inverter=ports, inverter_count=count, string_count=strings, module_count=strings * series,
            installed_dc_kw=power, installed_ac_kw=count * ac_kw, dc_ac_ratio=power / (count * ac_kw),
            capacity_error_pct=(power - values["target_dc_kw"]) / values["target_dc_kw"] * 100, steps=steps,
            checks={"hot_string_vmp_v": series * values["hot_vmp_v"], "cold_string_vmp_v": series * values["cold_vmp_v"],
                    "cold_string_voc_v": series * values["cold_voc_v"], "inverter_absolute_max_v": absolute_max,
                    "module_system_max_v": module_limit,
                    "mppt_operating_current_a": parallel * values["module_max_operating_current_a"],
                    "mppt_short_circuit_current_a": parallel * values["module_max_short_circuit_current_a"]},
            search_bounds={"series_modules": [1, 100], "parallel_strings_per_mppt": [1, 100], "inverter_count": [1, 10000]},
            unverified_checks=["Declared envelope accuracy and climate coverage", "Native temperature model/fitting and project sizing-temperature settings",
                               "Other manufacturer contractual limits and physical connectors", "Combiner, cable, protection and loss-parameter refitting"],
            selection="capacity error rounded to 1e-9 percent, then fewer inverters, then series closest to template; no energy/cost optimization")
    except (ValueError, KeyError) as exc:
        issue("template_or_components", str(exc))
    return result

"""Construct explicitly declared native AC storage on a preserved PV grid design."""
from __future__ import annotations

import csv
import math
import re

from pvsyst_auto.components.editor import replace_value
from pvsyst_auto.components.generator import direct_fields, finite, one
from pvsyst_auto.energy.availability import native_availability
from pvsyst_auto.energy.load import hourly_load_block, load_snapshot, upsert_load_block
from pvsyst_auto.energy.storage import (
    CONTROL_UPDATE_KEYS,
    NUMERIC_FIELDS,
    STRATEGIES,
    _system_grid_limit,
    decimal_text,
    render_grid_storage_block,
    resolve_storage_controls,
    set_grid_export,
    set_system_grid_limit,
    storage_state,
    validate_storage,
)
from pvsyst_auto.native.headless import strip_result_cache
from pvsyst_auto.project.variant_writer import run_new_variant_transaction
from pvsyst_auto.versions import NATIVE_TEXT_OR_SEED


def create_grid_storage_system(store, *, project, source_variant, new_variant, battery,
                               storage, load_manifest_name=None, dry_run=True,
                               availability=None, allow_grid_export=None):
    parsed = {}

    def preflight():
        required_batt = {"file_name", "series_count", "parallel_count", "monthly_temperature_c", "cycle_sow", "static_sow"}
        if not isinstance(battery, dict) or not required_batt <= set(battery) or set(battery) - (required_batt | {"library"}):
            raise ValueError("Complete explicit battery declaration is required")
        batt_lib = battery.get("library", "workspace")
        if batt_lib not in ("workspace", "builtin"):
            raise ValueError("battery.library must be 'workspace' or 'builtin'")
        if not isinstance(storage, dict):
            raise ValueError("Complete explicit native storage declaration is required")
        storage_clean = {k: v for k, v in storage.items() if v is not None}
        allowed_storage = set(NUMERIC_FIELDS) | CONTROL_UPDATE_KEYS
        if not (set(NUMERIC_FIELDS) | {"strategy"}) <= set(storage_clean) or set(storage_clean) - allowed_storage:
            raise ValueError("Complete explicit battery and native storage declarations are required")
        for key in ("series_count", "parallel_count"):
            if type(battery[key]) is not int or not 1 <= battery[key] <= 10000:
                raise ValueError(key + " must be a bounded positive integer")
        temperatures = battery["monthly_temperature_c"]
        if not isinstance(temperatures, list) or len(temperatures) != 12:
            raise ValueError("Declare all twelve battery monthly temperatures")
        temperatures = [finite(value, "monthly battery temperature", -50, 80) for value in temperatures]
        cycle = finite(battery["cycle_sow"], "cycle_sow", 0, 1, positive=True)
        static = finite(battery["static_sow"], "static_sow", 0, 1, positive=True)
        strat_key = storage_clean.get("strategy")
        if strat_key not in STRATEGIES:
            raise ValueError("Use a verified native self_consumption, weak_grid, peak_shaving, or power_shift strategy")
        weak = strat_key == "weak_grid"
        if (availability is not None) != weak:
            raise ValueError("Supply complete availability exactly when creating WeakGrid")
        if strat_key in ("self_consumption", "weak_grid") and not load_manifest_name:
            raise ValueError(f"{strat_key} requires an explicit load_manifest_name")
        # For peak_shaving, grid_limit_kw may live in storage_clean or already exist as PLimGrid on source pvSystem.
        preview_controls = resolve_storage_controls(
            storage_clean,
            system_grid_limit=1.0 if strat_key == "peak_shaving" and "grid_limit_kw" not in storage_clean else None,
        )
        state = {
            "strategy": preview_controls["strategy"],
            "charging_strategy": preview_controls["charging_strategy"],
            "discharging_strategy": preview_controls["discharging_strategy"],
        }
        for name, (_, low, high) in NUMERIC_FIELDS.items():
            state[name] = finite(storage_clean[name], name, low, high if high is not None else math.inf,
                                 positive=name.endswith("kw") or name.endswith("pct"))
        validate_storage(
            {**state, **preview_controls},
            allow_weak=weak,
        )
        parsed.update(
            temperatures=temperatures,
            cycle=cycle,
            static=static,
            weak=weak,
            batt_lib=batt_lib,
            storage_clean=storage_clean,
            base_state=state,
        )

    def build(*, text, lines, result, **_):
        temperatures, cycle, static = parsed["temperatures"], parsed["cycle"], parsed["static"]
        weak, batt_lib = parsed["weak"], parsed["batt_lib"]
        storage_clean, base_state = parsed["storage_clean"], parsed["base_state"]
        _, system = store._sections(lines)
        first_child = next(i for i, line in enumerate(lines[1:], 1) if line.strip().startswith("PVObject_"))
        root_fields = direct_fields(lines, (0, first_child))
        if one(root_fields, "Version")[1] not in NATIVE_TEXT_OR_SEED or system is None:
            raise ValueError("A complete verified-version PV-only Grid template is required")
        system_fields = direct_fields(lines, system)
        if one(system_fields, "SystemType")[1] != "Grid":
            raise ValueError("SystemType must be Grid")
        if any("PVObject_GridStorage=" in line for line in lines) or any(key in system_fields for key in ("BatteryFile", "NBattSerie", "NBattParall")):
            raise ValueError("Use the existing-storage editor for a template that already declares battery/storage hardware")
        if weak and any(line.strip().startswith("PVObject_GridUnavail=") for line in lines):
            raise ValueError("Preserve the source's existing grid-unavailability object instead of adding a duplicate")

        controls = resolve_storage_controls(storage_clean, system_grid_limit=_system_grid_limit(lines, system))
        state = {**base_state, **controls}
        validate_storage(state, allow_weak=weak)

        if batt_lib == "workspace":
            component_path = store.components._path("BTR", battery["file_name"])
            component_bytes = store.components._read(component_path)
            store.components._require_valid(store.components._inspect("BTR", component_bytes), complete=True)
            battery_artifact = str(component_path)
        else:
            store.components._path("BTR", battery["file_name"])
            if store.components.builtin is not None:
                builtin_path = store.components._path("BTR", battery["file_name"], "builtin")
                if builtin_path.is_file():
                    builtin_bytes = store.components._read(builtin_path)
                    store.components._require_valid(store.components._inspect("BTR", builtin_bytes), complete=True)
            battery_artifact = f"builtin:{battery['file_name']}"

        newline = "\r\n" if "\r\n" in text else "\n"
        load_record = load_profile = load_values = None
        if load_manifest_name is not None:
            load_record, load_profile, load_values = load_snapshot(store.workspace, load_manifest_name)

        grid_profile = ""
        if weak:
            grid_profile, grid_metadata = native_availability(store.workspace, availability, newline)
            if (grid_metadata["year"] != load_record["year"]
                    or grid_metadata["utc_offset_hours"] != load_record["utc_offset_hours"]):
                raise ValueError("Grid availability and prepared load must match year and standard UTC offset")
            result["grid_availability"] = grid_metadata

        root_index, root_flags = one(root_fields, "Flags")
        system_index, system_flags = one(system_fields, "Flags")
        for flags in (root_flags, system_flags):
            if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", flags):
                raise ValueError("Owning flags must be unambiguous hexadecimal values")
        if load_record is not None:
            # Bit19 enables user-load consumption.
            lines[root_index] = replace_value(lines[root_index], f"${int(root_flags[1:], 16) | 0x00080000:08X}")
        # Bit28 + 0x10040 enables native centralized AC grid storage.
        lines[system_index] = replace_value(lines[system_index], f"${int(system_flags[1:], 16) | 0x10010040:08X}")
        set_grid_export(lines, system, allow_grid_export)
        if "grid_limit_kw" in controls and ("grid_limit_kw" in storage_clean or controls["strategy"] == "PeakShaving"):
            set_system_grid_limit(lines, system, controls["grid_limit_kw"])
            _, system = store._sections(lines)
            system_fields = direct_fields(lines, system)
        additions = {
            "BatteryFile": battery["file_name"],
            "NBattSerie": str(battery["series_count"]),
            "NBattParall": str(battery["parallel_count"]),
            "CycleSOWBeg": decimal_text(cycle),
            "StaticSOWBeg": decimal_text(static),
            "TypeTempBatt": "1",
            "TempBattRef": decimal_text(temperatures[0]),
            "TempBattMois": ",".join(decimal_text(value) for value in temperatures) + ",",
        }
        new_fields = []
        for key, value in additions.items():
            existing = one(system_fields, key, False)
            if existing:
                lines[existing[0]] = replace_value(lines[existing[0]], value)
            else:
                new_fields.append("    " + key + "=" + value + newline)
        lines[system[1]:system[1]] = new_fields
        if any(line.strip().startswith("VarList,") for line in lines):
            strip_result_cache(lines)
        candidate_text = "".join(lines)

        load = ""
        encoding = None
        if load_record is not None:
            load, encoding = hourly_load_block(load_profile, load_values, load_record["year"], newline)
            candidate_text, replaced = upsert_load_block(candidate_text, load)
            if replaced:
                load = ""

        unavail_lines = grid_profile.splitlines() if grid_profile else None
        storage_lines = render_grid_storage_block(state, unavail_lines)
        closing = candidate_text.rfind("\n", 0, candidate_text.rindex("End of PVObject pvVCalcul")) + 1
        candidate_text = candidate_text[:closing] + load + newline.join(storage_lines) + newline + candidate_text[closing:]

        def verify(checked, _, res):
            _, checked_system = store._sections(checked)
            configured, _, _ = storage_state(checked, checked_system)
            validate_storage(configured, allow_weak=weak)
            artifacts = {"battery": battery_artifact}
            if load_profile is not None:
                artifacts["load_csv"] = str(load_profile)
            res.update(
                artifacts=artifacts,
                battery_library=batt_lib,
                configuration=configured,
                battery_temperature_monthly_c=temperatures,
                cycle_sow=cycle,
                static_sow=static,
            )
            if encoding is not None:
                res.update(
                    load_encoding=encoding,
                    year=load_record["year"],
                    utc_offset_hours=load_record["utc_offset_hours"],
                )
            if batt_lib == "builtin":
                res["warnings"].append(
                    "Builtin BTR model is resolved from PVsyst's embedded database at simulation time; verify with pvsyst_run_simulation."
                )

        return {"candidate_text": candidate_text, "verify": verify}

    return run_new_variant_transaction(
        store,
        project=project,
        source_variant=source_variant,
        new_variant=new_variant,
        dry_run=dry_run,
        bom_display_target=new_variant,
        preflight=preflight,
        build=build,
        initial_fields={"backend": "pvsyst_native_ac_coupled"},
        exists_prefix="Choose a new storage system variant: ",
        source_changed_error="A source battery/load/template changed before creation",
        write_failure_warning="Inspect the disclosed new variant artifact before retrying; original PV template and input files remain preserved.",
        extra_exceptions=(csv.Error,),
        uppercase_variant=False,
    )

"""Construct explicitly declared native AC storage on a preserved PV grid design."""
from __future__ import annotations

import csv
import re

from pvsyst_auto.components.editor import replace_value
from pvsyst_auto.components.generator import direct_fields, finite, one
from pvsyst_auto.energy.availability import native_availability
from pvsyst_auto.energy.storage import NUMERIC_FIELDS, STRATEGIES, decimal_text, set_grid_export, storage_state, validate_storage
from pvsyst_auto.energy.load import hourly_load_block, load_snapshot, upsert_load_block
from pvsyst_auto.project.variant_writer import run_new_variant_transaction
from pvsyst_auto.versions import NATIVE_TEXT_OR_SEED


def create_grid_storage_system(store, *, project, source_variant, new_variant, battery,
                               storage, load_manifest_name, dry_run=True, availability=None,
                               allow_grid_export=None):
    parsed = {}

    def preflight():
        required = {"file_name", "series_count", "parallel_count", "monthly_temperature_c", "cycle_sow", "static_sow"}
        if set(battery) != required or set(storage) != set(NUMERIC_FIELDS) | {"strategy"}:
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
        if storage["strategy"] not in STRATEGIES:
            raise ValueError("Use a verified native self_consumption or weak_grid strategy")
        weak = storage["strategy"] == "weak_grid"
        if (availability is not None) != weak:
            raise ValueError("Supply complete availability exactly when creating WeakGrid")
        state = {"strategy": STRATEGIES[storage["strategy"]], "charging_strategy": "PVAvailable", "discharging_strategy": "Immediate"}
        for name, (_, low, high) in NUMERIC_FIELDS.items():
            state[name] = finite(storage[name], name, low, high if high is not None else float("inf"), positive=name.endswith("kw") or name.endswith("pct"))
        validate_storage(state, allow_weak=weak)
        parsed.update(temperatures=temperatures, cycle=cycle, static=static, weak=weak, state=state)

    def build(*, text, lines, result, **_):
        temperatures, cycle, static = parsed["temperatures"], parsed["cycle"], parsed["static"]
        weak, state = parsed["weak"], parsed["state"]
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
        component_path = store.components._path("BTR", battery["file_name"])
        component_bytes = store.components._read(component_path)
        store.components._require_valid(store.components._inspect("BTR", component_bytes), complete=True)
        record, profile, values = load_snapshot(store.workspace, load_manifest_name)
        newline = "\r\n" if "\r\n" in text else "\n"
        grid_profile = ""
        if weak:
            grid_profile, grid_metadata = native_availability(store.workspace, availability, newline)
            if (grid_metadata["year"] != record["year"]
                    or grid_metadata["utc_offset_hours"] != record["utc_offset_hours"]):
                raise ValueError("Grid availability and prepared load must match year and standard UTC offset")
            result["grid_availability"] = grid_metadata
        root_index, root_flags = one(root_fields, "Flags")
        system_index, system_flags = one(system_fields, "Flags")
        for flags in (root_flags, system_flags):
            if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", flags):
                raise ValueError("Owning flags must be unambiguous hexadecimal values")
        # Bit19 enables user-load consumption; bit28 enables native grid storage.
        lines[root_index] = replace_value(lines[root_index], f"${int(root_flags[1:], 16) | 0x00080000:08X}")
        lines[system_index] = replace_value(lines[system_index], f"${int(system_flags[1:], 16) | 0x10010040:08X}")
        set_grid_export(lines, system, allow_grid_export)
        additions = {"BatteryFile": battery["file_name"], "NBattSerie": str(battery["series_count"]),
                     "NBattParall": str(battery["parallel_count"]), "CycleSOWBeg": decimal_text(cycle),
                     "StaticSOWBeg": decimal_text(static), "TypeTempBatt": "1", "TempBattRef": decimal_text(temperatures[0]),
                     "TempBattMois": ",".join(decimal_text(value) for value in temperatures) + ","}
        new_fields = []
        for key, value in additions.items():
            existing = one(system_fields, key, False)
            if existing:
                lines[existing[0]] = replace_value(lines[existing[0]], value)
            else:
                new_fields.append("    " + key + "=" + value + newline)
        lines[system[1]:system[1]] = new_fields
        candidate_text = "".join(lines)
        load, encoding = hourly_load_block(profile, values, record["year"], newline)
        candidate_text, replaced = upsert_load_block(candidate_text, load)
        if replaced:
            load = ""
        storage_lines = ["  PVObject_GridStorage=pvGridStorage", "    Flags=$00", "    Strategy=" + state["strategy"],
                         "    ChargingStrategy=PVAvailable", "    DischStrategy=Immediate"]
        storage_lines.extend("    " + key + "=" + decimal_text(state[name]) for name, (key, _, _) in NUMERIC_FIELDS.items())
        if grid_profile:
            storage_lines.extend(grid_profile.splitlines())
        storage_lines.append("  End of PVObject pvGridStorage")
        closing = candidate_text.rfind("\n", 0, candidate_text.rindex("End of PVObject pvVCalcul")) + 1
        candidate_text = candidate_text[:closing] + load + newline.join(storage_lines) + newline + candidate_text[closing:]

        def verify(checked, _, res):
            _, checked_system = store._sections(checked)
            configured, _, _ = storage_state(checked, checked_system)
            validate_storage(configured, allow_weak=weak)
            res.update(
                artifacts={"battery": str(component_path), "load_csv": str(profile)},
                configuration=configured,
                battery_temperature_monthly_c=temperatures,
                cycle_sow=cycle,
                static_sow=static,
                load_encoding=encoding,
                year=record["year"],
                utc_offset_hours=record["utc_offset_hours"],
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

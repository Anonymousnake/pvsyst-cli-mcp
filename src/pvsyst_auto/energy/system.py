"""Construct explicitly declared native AC storage on a preserved PV grid design."""
from __future__ import annotations

import csv
import difflib
import re

from pvsyst_auto.components.editor import replace_value
from pvsyst_auto.components.generator import direct_fields, finite, one
from pvsyst_auto.energy.availability import native_availability
from pvsyst_auto.energy.storage import NUMERIC_FIELDS, STRATEGIES, decimal_text, set_grid_export, storage_state, validate_storage
from pvsyst_auto.energy.load import hourly_load_block, load_snapshot


def create_grid_storage_system(store, *, project, source_variant, new_variant, battery,
                               storage, load_manifest_name, dry_run=True, availability=None,
                               allow_grid_export=None):
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run, "issues": [],
              "warnings": [], "artifacts": {}, "variant_created": False,
              "native_consumption_verified": False, "backend": "pvsyst_native_ac_coupled"}
    with store.lock:
        try:
            if type(dry_run) is not bool:
                raise ValueError("Use a strict boolean dry_run")
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
            store._check_project(project)
            source, target = store._path(project, source_variant), store._path(project, new_variant)
            if any(path.name.casefold() == target.name.casefold() for path in target.parent.iterdir()):
                raise FileExistsError("Choose a new storage system variant: " + target.name)
            original = store._read(source)
            text, lines = store._text(original)
            _, system = store._sections(lines)
            first_child = next(i for i, line in enumerate(lines[1:], 1) if line.strip().startswith("PVObject_"))
            root_fields = direct_fields(lines, (0, first_child))
            if one(root_fields, "Version")[1] not in ("8.1.0", "8.1.6") or system is None:
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
            record, profile, values, snapshots = load_snapshot(store.workspace, load_manifest_name)
            snapshots[component_path] = component_bytes
            newline = "\r\n" if "\r\n" in text else "\n"
            grid_profile = ""
            if weak:
                grid_profile, grid_metadata, grid_path, grid_bytes = native_availability(store.workspace, availability, newline)
                if (grid_metadata["year"] != record["year"]
                        or grid_metadata["utc_offset_hours"] != record["utc_offset_hours"]):
                    raise ValueError("Grid availability and prepared load must match year and standard UTC offset")
                snapshots[grid_path] = grid_bytes
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
            # TempBattRef has no separate declaration: it repeats the first (January) monthly
            # value. That adapter choice is disclosed in the warnings, not presented as declared.
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
            starts = list(re.finditer(r"(?m)^[ \t]*PVObject_UserNeeds=pvLoad[ \t]*\r?$", candidate_text))
            ends = list(re.finditer(r"(?m)^[ \t]*End of PVObject pvLoad[ \t]*\r?$", candidate_text))
            if len(starts) > 1 or len(ends) > 1 or bool(starts) != bool(ends):
                raise ValueError("Ambiguous owning load block")
            if starts:
                if starts[0].start() >= ends[0].end():
                    raise ValueError("Unmatched owning load block")
                # The block ends with its own newline, so the replaced closing line's goes too.
                finish = ends[0].end() + candidate_text.startswith("\n", ends[0].end())
                candidate_text = candidate_text[:starts[0].start()] + load + candidate_text[finish:]
                load = ""
            storage_lines = ["  PVObject_GridStorage=pvGridStorage", "    Flags=$00", "    Strategy=" + state["strategy"],
                             "    ChargingStrategy=PVAvailable", "    DischStrategy=Immediate"]
            storage_lines.extend("    " + key + "=" + decimal_text(state[name]) for name, (key, _, _) in NUMERIC_FIELDS.items())
            if grid_profile:
                storage_lines.extend(grid_profile.splitlines())
            storage_lines.append("  End of PVObject pvGridStorage")
            # Insert at the start of the closing line so its indentation stays on that line.
            closing = candidate_text.rfind("\n", 0, candidate_text.rindex("End of PVObject pvVCalcul")) + 1
            candidate_text = candidate_text[:closing] + load + newline.join(storage_lines) + newline + candidate_text[closing:]
            # Reuse the shared boundary with the FINAL body. The declared battery filename and
            # load profile path are already embedded in candidate_text, so the old explicit
            # ASCII checks are subsumed; a non-ASCII inherited body still forces the BOM.
            bom_prefix = store._display_name_prefix(original, new_variant, False, "variant_name",
                                                    content=candidate_text)
            candidate = bom_prefix + candidate_text.encode("utf-8")
            _, checked = store._text(candidate)
            _, checked_system = store._sections(checked)
            configured, _, _ = storage_state(checked, checked_system)
            validate_storage(configured, allow_weak=weak)
            diff = "".join(difflib.unified_diff(text.splitlines(keepends=True), candidate_text.splitlines(keepends=True)))
            result.update(status="ready", project=project, source_variant=source_variant, variant=new_variant,
                          artifacts={"variant": str(target), "battery": str(component_path), "load_csv": str(profile)},
                          configuration=configured, battery_temperature_monthly_c=temperatures,
                          cycle_sow=cycle, static_sow=static, load_encoding=encoding, year=record["year"],
                          utc_offset_hours=record["utc_offset_hours"], diff=diff[:32000], diff_truncated=len(diff)>32000,
                          bom_added=bool(bom_prefix) and not original.startswith(b"\xef\xbb\xbf"))
            result["warnings"].append("This constructs native centralized AC-coupled storage, not hybrid DC battery ports or a grid-charging tariff controller. DC port/BMS compatibility and hardware sizing are not certified.")
            result["warnings"].append("Battery model/counts/temperature/SOW and efficiencies are caller-declared, not inferred from PV or climate. SOCDischSelf is the WeakGrid self-use reserve; it remains unused as a backup reserve under SelfConsumption.")
            result["warnings"].append(f"TempBattRef has no separate input: it repeats the first (January) declared monthly battery temperature, {decimal_text(temperatures[0])} C. This adapter choice is not a declared or verified battery reference temperature.")
            result["warnings"].append("PV design, geometry, component references and site/weather remain inherited. Run matching native year/timezone and verify loads, SOC and energy before claiming consumption.")
            if store._read(source) != original or any(path.read_bytes() != data for path, data in snapshots.items()):
                raise ValueError("A source battery/load/template changed before creation")
            if dry_run:
                return result
            result["stage"] = "write"
            with target.open("xb") as stream:
                stream.write(candidate)
            result.update(status="created", stage="created", variant_created=True)
        except (ValueError, OSError, UnicodeError, KeyError, TypeError, StopIteration, csv.Error) as exc:
            result["issues"].append(str(exc))
            result["status"] = "failed" if result["stage"] == "write" else "blocked"
            if result["status"] == "failed":
                result["warnings"].append("Inspect the disclosed new variant artifact before retrying; original PV template and input files remain preserved.")
    return result

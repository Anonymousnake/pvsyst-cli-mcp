"""Bind an existing native AC-storage revision to prepared loads and annual outputs."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from decimal import Decimal
import math
from pathlib import Path
import re

from pvsyst_cli import iter_result_csv, result_units
from pvsyst_component_editor import objects
from pvsyst_grid_storage import create_grid_storage_variant, storage_state, validate_storage
from pvsyst_load_variant import hourly_load_block, load_snapshot
from pvsyst_paths import workspace_file
from pvsyst_results import fingerprint
from pvsyst_tracking import _view


def _load_signature(text):
    """Keep cache structure/tokens; display text and historical paths are metadata."""
    signature = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if separator and name in ("Comment", "PathName"):
            continue
        if separator and name == "FileName":
            value = value.casefold()
        if separator and name in ("WRFactor", "OriginalScale", "RenormVal", "NColDate", "DateFormat"):
            number = Decimal(value)
            if not number.is_finite():
                raise ValueError("Nonfinite native load field: " + name)
            value = number
        signature.append((name, value) if separator else line)
    return signature


def prepare_storage_revision(store, project, variant, new_variant, updates,
                             load_manifest_name, year, availability=None):
    """Preview an existing native storage edit and bind its actual embedded load."""
    result = {"status": "blocked", "issues": [], "warnings": [], "input_assets": {},
              "database_references": []}
    with store.lock:
        try:
            if type(year) is not int or not 1969 <= year <= 2068:
                raise ValueError("Storage year must be an integer in 1969..2068")
            source = store._path(project, variant)
            original = store._read(source)
            _, lines = store._text(original)
            _, system = store._sections(lines)
            before, _, _ = storage_state(lines, system)
            blocks = objects(_view(lines))
            root = next(block for block in blocks if not block.path)
            flags = root.one("Flags")[1]
            if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", flags) or not int(flags[1:], 16) & 0x00080000:
                raise ValueError("Storage delivery requires the enabled native user-load bit")
            loads = [block for block in blocks if block.object_type == "pvLoad"]
            if len(loads) != 1 or loads[0].path != "PVObject_UserNeeds" or not loads[0].end_line:
                raise ValueError("Require one complete directly owned native hourly load")
            record, profile, values, snapshots = load_snapshot(store.workspace, load_manifest_name)
            if record["year"] != year:
                raise ValueError("Prepared load and native annual execution must use the same year")
            expected, encoding = hourly_load_block(profile, values, year, "\n")
            load = loads[0]
            stored = "\n".join(lines[load.line - 1:load.end_line])
            if _load_signature(stored) != _load_signature(expected):
                raise ValueError("Native hourly load cache/time settings differ from the prepared load; rebind using the existing load tool")
            if availability is not None:
                if not isinstance(availability, dict):
                    raise ValueError("Grid availability must be an explicit declaration")
                if any(availability.get(key) != record[key] for key in ("year", "utc_offset_hours", "time_basis")):
                    raise ValueError("Grid availability and prepared load must use the same year and standard time basis")
                path = workspace_file(store.workspace, "UserData", availability["csv_name"], ".csv")
                snapshots[path] = path.read_bytes()
            if updates == {} and availability is None:
                validate_storage(before)
                if store._path(project, new_variant).exists():
                    raise FileExistsError("Choose a new grid storage variant")
                preview = {"status": "ready", "before": before, "after": before, "warnings": []}
            else:
                preview = create_grid_storage_variant(store, project=project, source_variant=variant,
                    new_variant=new_variant, updates=updates, availability=availability, dry_run=True)
            result["preview"] = preview
            if preview["status"] != "ready":
                raise ValueError("Storage revision is blocked: " + "; ".join(preview["issues"]))
            assets = {}
            for label, folder, name in (
                    ("load_manifest", "Results", load_manifest_name),
                    ("load_source", "UserData", record["source"]["name"]),
                    ("load_profile", "UserHourlyParams", profile.name)):
                path = workspace_file(store.workspace, folder, name, ".json" if label == "load_manifest" else ".csv")
                assets[label] = {"folder": folder, "name": name, "library": "workspace", "sha256": fingerprint(path)}
            if availability is not None:
                path = workspace_file(store.workspace, "UserData", availability["csv_name"], ".csv")
                assets["grid_availability"] = {"folder": "UserData", "name": availability["csv_name"],
                                              "library": "workspace", "sha256": fingerprint(path)}
            libraries = ("workspace", "builtin") if store.components.builtin else ("workspace",)
            battery = before["battery_file"]
            library = next((item for item in libraries if store.components._path("BTR", battery, item).is_file()), None)
            if library is not None:
                path = store.components._path("BTR", battery, library)
                assets["BTR"] = {"folder": "ComposPV/Batteries", "name": battery,
                                 "library": library, "sha256": fingerprint(path)}
            else:
                result["database_references"].append({"kind": "BTR", "name": battery,
                                                      "resolution": "native_database_uninspected"})
                result["warnings"].append("Inherited native battery database contents are uninspected; hardware/BMS compatibility is not certified.")
            if store._read(source) != original or any(path.read_bytes() != data for path, data in snapshots.items()):
                raise ValueError("A source variant or prepared load/availability input changed during preflight")
            result.update(status="ready", before=before, after=preview["after"],
                          load_record={**record, "native_encoding": encoding}, load_values=values,
                          input_assets=assets, utc_offset_hours=record["utc_offset_hours"],
                          time_basis=record["time_basis"], load_encoding=encoding)
            result["warnings"].extend(preview["warnings"])
        except (ValueError, OSError, KeyError, TypeError, StopIteration, ArithmeticError, csv.Error) as exc:
            result["issues"].append(str(exc))
    return result


def _power_error(value):
    # Existing native SFI uses six significant figures; retain the 0.001 kW export floor.
    return max(0.001, 0.5 * 10 ** (math.floor(math.log10(abs(value))) - 5)) if value else 0.001


def read_storage_year(csv_path, pdf_path, year, load_record, load_values, state):
    """Interpret native hourly storage flows without the grid-only PR formula."""
    if type(year) is not int or not 1969 <= year <= 2068 or load_record["year"] != year:
        raise ValueError("Storage output and prepared load require the same supported year")
    units = result_units(csv_path, strict=True)
    required = {"E_User", "EFrGrid", "E_Grid", "SOC_Beg", "SOC_End", "EBatCh", "EBatDis", "CL_Chrg", "CL_InvB"}
    if required - set(units):
        raise ValueError("Storage output is missing required load/SOC/AC power columns: " + ", ".join(sorted(required - set(units))))
    if any(not unit for unit in units.values()):
        raise ValueError("Every actual storage result column requires an explicit unit")
    power_names = {"E_User", "EFrGrid", "E_Grid", "E_Miss", "E_Avail", "EBatCh", "EBatDis",
                   "CL_Chrg", "CL_InvB", "CL_Over", "EUnused", "EArray", "IL_Pmax"}
    power_scales = {name: {"W": 0.001, "kW": 1}.get(units[name]) for name in power_names & set(units)}
    soc_scales = {name: {"ratio": 1, "%": 0.01}.get(units[name]) for name in ("SOC_Beg", "SOC_End")}
    if any(scale is None for scale in (*power_scales.values(), *soc_scales.values())):
        raise ValueError("Storage power columns require W/kW and SOC requires ratio or percent")
    balance_names = {"E_Avail", "EFrGrid", "EBatDis", "E_User", "E_Miss", "E_Grid",
                     "EBatCh", "CL_Chrg", "CL_InvB", "CL_Over", "EUnused"}
    missing_balance = sorted(balance_names - set(power_scales))
    totals = dict.fromkeys(power_scales, 0.0)
    soc_min = dict.fromkeys(soc_scales, math.inf)
    soc_max = dict.fromkeys(soc_scales, -math.inf)
    first = expected = datetime(year, 1, 1)
    end = datetime(year + 1, 1, 1)
    indices = None
    count = 0
    initial_soc = None
    load_error = charge_max = discharge_max = balance_max = balance_bound_max = 0.0
    encoding_error = load_record.get("native_encoding", {}).get("encoding_error_bound_kw", 0.0)
    for headers, row in iter_result_csv(csv_path, strict=True):
        if indices is None:
            if len(set(headers)) != len(headers) or set(headers[1:]) != set(units):
                raise ValueError("Storage output requires unique columns matching its units row")
            indices = {name: headers.index(name) for name in units}
        if row[0] != expected or expected >= end or count >= len(load_values):
            raise ValueError("Storage annual hourly coverage differs from the declared year/load")
        if any(not math.isfinite(value) for value in row[1:]):
            raise ValueError("Storage output contains missing or nonfinite numeric data")
        load_stamp, load_power = load_values[count]
        if load_stamp != expected:
            raise ValueError("Prepared load timestamps differ from the native output")
        power = {name: row[indices[name]] * scale for name, scale in power_scales.items()}
        soc = {name: row[indices[name]] * scale for name, scale in soc_scales.items()}
        if any(not math.isfinite(value) for value in (*power.values(), *soc.values())):
            raise ValueError("Storage unit conversion exceeds finite numeric range")
        difference = abs(power["E_User"] - load_power)
        load_error = max(load_error, difference)
        if difference > encoding_error + _power_error(power["E_User"]):
            raise ValueError("Native E_User differs from the bound prepared hourly load")
        for name, value in soc.items():
            if not state["soc_min"] - 0.0001 <= value <= state["soc_max"] + 0.0001:
                raise ValueError("Native SOC is outside the declared bounds")
            soc_min[name], soc_max[name] = min(soc_min[name], value), max(soc_max[name], value)
        if initial_soc is None:
            initial_soc = soc["SOC_Beg"]
            if abs(initial_soc - state["initial_soc"]) > 0.0001:
                raise ValueError("Native initial SOC differs from the declared initial SOC")
        charge = power["EBatCh"] + power["CL_Chrg"]
        discharge = power["EBatDis"] - power["CL_InvB"]
        if charge > state["max_charge_ac_kw"] + _power_error(power["EBatCh"]) + _power_error(power["CL_Chrg"]):
            raise ValueError("Native charging AC input exceeds the declared limit")
        if discharge > state["max_discharge_ac_kw"] + _power_error(power["EBatDis"]) + _power_error(power["CL_InvB"]):
            raise ValueError("Native battery-inverter AC output exceeds the declared limit")
        charge_max, discharge_max = max(charge_max, charge), max(discharge_max, discharge)
        if not missing_balance:
            try:
                residual = math.fsum(power[name] for name in ("E_Avail", "EFrGrid", "EBatDis", "E_Miss")) - math.fsum(
                    power[name] for name in ("E_User", "E_Grid", "EBatCh", "CL_Chrg", "CL_InvB", "CL_Over", "EUnused"))
            except OverflowError:
                raise ValueError("Storage AC balance exceeds finite numeric range") from None
            bound = math.fsum(_power_error(power[name]) for name in balance_names)
            if not math.isfinite(residual) or not math.isfinite(bound):
                raise ValueError("Storage AC balance exceeds finite numeric range")
            balance_max, balance_bound_max = max(balance_max, abs(residual)), max(balance_bound_max, bound)
            if abs(residual) > bound:
                raise ValueError("Native storage AC energy-flow residual exceeds its column export precision bound")
        for name, value in power.items():
            totals[name] += value  # Complete fixed-hour native kW samples integrate to kWh.
        expected += timedelta(hours=1)
        count += 1
    if expected != end or count != len(load_values):
        raise ValueError("Storage output does not cover the entire prepared hourly year")
    if any(not math.isfinite(value) for value in totals.values()):
        raise ValueError("Storage annual totals exceed finite numeric range")
    with Path(pdf_path).open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError("Native storage report is not a PDF")
    names = {"grid_import_kwh": "EFrGrid", "grid_export_kwh": "E_Grid", "load_energy_kwh": "E_User",
             "unmet_load_kwh": "E_Miss", "battery_charge_kwh": "EBatCh", "battery_discharge_kwh": "EBatDis",
             "unused_energy_kwh": "EUnused", "array_energy_kwh": "EArray", "clipping_loss_kwh": "IL_Pmax"}
    flags = (["Complete AC balance was not checked; native output lacks: " + ", ".join(missing_balance)]
             if missing_balance else [])
    metrics = {"year": year, "rows": count, "first": str(first), "last": str(end - timedelta(hours=1)),
               "complete_year": True, "step_minutes": 60, "diagnostic_flags": flags,
               **{name: totals.get(column) for name, column in names.items()},
               "missing_metrics": [name for name, column in names.items() if column not in totals],
               "soc_begin_min": soc_min["SOC_Beg"], "soc_begin_max": soc_max["SOC_Beg"],
               "soc_end_min": soc_min["SOC_End"], "soc_end_max": soc_max["SOC_End"], "initial_soc": initial_soc,
               "pr_method": "Not applied: storage/self-consumption E_Grid is exported energy, not grid-only PV yield"}
    return {"metrics": metrics, "hourly_rows": count, "numeric_columns": headers[1:],
            "storage_output": {"units": units, "utc_offset_hours": load_record["utc_offset_hours"],
                "time_basis": load_record["time_basis"], "max_load_error_kw": load_error,
                "max_charge_ac_kw": charge_max, "max_discharge_ac_kw": discharge_max,
                "soc_export_tolerance_ratio": 0.0001,
                "power_export_precision": "Six significant figures with a 0.001 kW per-column floor",
                "ac_balance_verified": not missing_balance, "ac_balance_missing_columns": missing_balance,
                "max_ac_balance_residual_kw": balance_max if not missing_balance else None,
                "max_ac_balance_export_bound_kw": balance_bound_max if not missing_balance else None,
                "battery_energy_scope": "Native battery DC-side charge/discharge totals; no capacity-times-SOC model",
                "hardware_compatibility_certified": False}}

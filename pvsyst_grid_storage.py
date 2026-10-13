"""Native AC-coupled grid storage fields; never masquerade as hybrid DC ports."""
from __future__ import annotations

import csv
import difflib
import re

from pvsyst_grid_availability import native_availability

from pvsyst_component_editor import replace_value
from pvsyst_generator import direct_fields, finite, one

NUMERIC_FIELDS = {
    "soc_min": ("SOCDischDecl", 0, 1), "soc_max": ("SOCChDecl", 0, 1),
    "initial_soc": ("BattSoCInit", 0, 1), "reserve_soc": ("SOCDischSelf", 0, 1),
    "max_charge_ac_kw": ("PMaxCharge", 0, None), "max_discharge_ac_kw": ("PMaxDisch", 0, None),
    "charger_max_efficiency_pct": ("BattRectMaxEffic", 0, 100),
    "charger_euro_efficiency_pct": ("BattRectEuroEffic", 0, 100),
    "battery_inverter_max_efficiency_pct": ("BattInvMaxEffic", 0, 100),
    "battery_inverter_euro_efficiency_pct": ("BattInvEuroEffic", 0, 100),
}
STRATEGIES = {"self_consumption": "SelfConsumption", "weak_grid": "WeakGrid"}


def storage_state(lines, system):
    """Read owning declared fields only; do not infer an active or certified physical device."""
    root_end = next((i for i,line in enumerate(lines[1:],1) if line.strip().startswith("PVObject_")), len(lines)-1)
    if one(direct_fields(lines,(0,root_end)), "Version")[1] not in ("8.1.0", "8.1.6"):
        raise ValueError("Grid storage requires a verified 8.1.0/8.1.6 text variant")
    if system is None:
        raise ValueError("One grid pvSystem is required")
    fields = direct_fields(lines, system)
    if one(fields, "SystemType")[1] != "Grid":
        raise ValueError("Grid storage requires SystemType=Grid, not standalone backup")
    flags = one(fields, "Flags")[1]
    if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", flags) or int(flags[1:],16) & 0x10000040 != 0x10000040:
        raise ValueError("The template must already declare the checked grid-storage/battery enabling bits; dormant fields alone do not configure storage")
    battery = one(fields, "BatteryFile")[1]
    if not battery or any(char in battery for char in ("/", "\\")) or not battery.lower().endswith(".btr"):
        raise ValueError("Existing battery reference must be one plain BTR filename")
    counts = {}
    for key in ("NBattSerie", "NBattParall"):
        raw = one(fields,key)[1]
        if not raw.isdigit() or not 1 <= int(raw) <= 10000:
            raise ValueError("Battery pack counts must be positive bounded integers")
        counts[key] = int(raw)
    starts = [i for i,line in enumerate(lines) if line.strip() == "PVObject_GridStorage=pvGridStorage"]
    ends = [i for i,line in enumerate(lines) if line.strip() == "End of PVObject pvGridStorage"]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError("An existing complete unique pvGridStorage template is required")
    bounds = (starts[0],ends[0])
    values = direct_fields(lines,bounds)
    if one(values,"Flags")[1] != "$00":
        raise ValueError("Only the checked flat grid storage Flags=$00 form is supported")
    state = {"battery_file":battery, "pack_counts":counts, "system_flags":one(fields,"Flags")[1],
             "strategy":one(values,"Strategy")[1], "charging_strategy":one(values,"ChargingStrategy")[1],
             "discharging_strategy":one(values,"DischStrategy")[1], "backend":"pvsyst_native_ac_coupled"}
    for name,(key,low,high) in NUMERIC_FIELDS.items():
        try:
            value = float(one(values,key)[1])
        except ValueError:
            raise ValueError("Invalid declared storage field: " + key) from None
        state[name] = finite(value,key,low,high if high is not None else float("inf"),positive=name.endswith("kw") or name.endswith("pct"))
    return state,bounds,values


def validate_storage(state, *, allow_weak=False):
    if not state["soc_min"] < state["soc_max"]:
        raise ValueError("Require soc_min < soc_max")
    for name in ("initial_soc", "reserve_soc"):
        if not state["soc_min"] <= state[name] <= state["soc_max"]:
            raise ValueError(name + " must lie within the declared SOC bounds")
    for prefix in ("charger", "battery_inverter"):
        if state[prefix+"_euro_efficiency_pct"] > state[prefix+"_max_efficiency_pct"]:
            raise ValueError(prefix + " Euro efficiency must not exceed max efficiency")
    allowed = ("SelfConsumption", "WeakGrid") if allow_weak else ("SelfConsumption",)
    if (state["strategy"] not in allowed or state["charging_strategy"] != "PVAvailable"
            or state["discharging_strategy"] != "Immediate"):
        raise ValueError("Only the checked SelfConsumption/PVAvailable/Immediate triplet is supported; WeakGrid needs separately validated availability input")


def create_grid_storage_variant(store, *, project, source_variant, new_variant, updates, dry_run=True,
                                availability=None):
    """Create a new variant from an existing native storage system with bounded scalar edits."""
    result={"status":"blocked","stage":"preflight","dry_run":dry_run,"issues":[],"warnings":[],
            "artifacts":{},"variant_created":False,"native_consumption_verified":False,
            "scope":"Existing centralized native AC-coupled Grid storage. No hybrid DC-port construction, new hardware sizing or grid-charging tariff controller is inferred."}
    with store.lock:
        try:
            if type(dry_run) is not bool or not isinstance(updates,dict) or not updates:
                raise ValueError("Supply a nonempty strict storage update object and boolean dry_run")
            if set(updates)-set(NUMERIC_FIELDS)-{"strategy"}:
                raise ValueError("Unknown storage update fields")
            store._check_project(project)
            source,target=store._path(project,source_variant),store._path(project,new_variant)
            if any(path.name.casefold()==target.name.casefold() for path in target.parent.iterdir()):
                raise FileExistsError("Choose a new grid storage variant: "+target.name)
            original=store._read(source)
            text,lines=store._text(original)
            _,system=store._sections(lines)
            before,bounds,fields=storage_state(lines,system)
            edits={}
            for name,value in updates.items():
                if name=="strategy":
                    if value not in STRATEGIES:
                        raise ValueError("Only the verified self_consumption triplet is accepted; WeakGrid additionally needs a valid availability profile")
                    edits.update(Strategy=STRATEGIES[value],ChargingStrategy="PVAvailable",DischStrategy="Immediate")
                else:
                    key,low,high=NUMERIC_FIELDS[name]
                    edits[key]=str(finite(value,name,low,high if high is not None else float("inf"),positive=name.endswith("kw") or name.endswith("pct")))
            for key,value in edits.items():
                index,_=one(fields,key)
                lines[index]=replace_value(lines[index],value)
            after,_,_=storage_state(lines,system)
            weak = after["strategy"] == "WeakGrid"
            validate_storage(after, allow_weak=availability is not None)
            if (availability is not None) != weak:
                raise ValueError("Supply a fresh complete availability declaration exactly when configuring WeakGrid")
            candidate_text="".join(lines)
            availability_snapshot = None
            if weak:
                first_child = next(i for i,line in enumerate(lines[1:],1) if line.strip().startswith("PVObject_"))
                root = direct_fields(lines, (0, first_child))
                root_flags = one(root, "Flags")[1]
                if not re.fullmatch(r"\$[0-9a-fA-F]{1,8}", root_flags) or not int(root_flags[1:],16) & 0x80000:
                    raise ValueError("WeakGrid requires an already enabled native self-consumption load")
                newline = "\r\n" if "\r\n" in text else "\n"
                profile, metadata, input_path, input_bytes = native_availability(store.workspace, availability, newline)
                load_starts = [i for i,line in enumerate(lines) if line.strip()=="PVObject_UserNeeds=pvLoad"]
                load_ends = [i for i,line in enumerate(lines) if line.strip()=="End of PVObject pvLoad"]
                if len(load_starts)!=1 or len(load_ends)!=1 or load_starts[0]>=load_ends[0]:
                    raise ValueError("WeakGrid requires one complete native load snapshot")
                load_lines = lines[load_starts[0]:load_ends[0]]
                year = metadata["year"] % 100
                if (sum(line.strip()==f"DateBeg=01/01/{year:02d} 00:00" for line in load_lines)!=1
                        or sum(line.strip()==f"DateEnd=31/12/{year:02d} 23:00" for line in load_lines)!=1):
                    raise ValueError("Availability must match the complete owning load snapshot year")
                local = "".join(lines[bounds[0]:bounds[1]+1])
                starts = list(re.finditer(r"(?m)^[ \t]*PVObject_GridUnavail=pvUnavail[ \t]*\r?$", local))
                ends = list(re.finditer(r"(?m)^[ \t]*End of PVObject pvUnavail[ \t]*\r?$", local))
                if len(starts)>1 or len(ends)>1 or bool(starts)!=bool(ends):
                    raise ValueError("Ambiguous owning grid availability object")
                if starts:
                    if starts[0].start()>=ends[0].end():
                        raise ValueError("Unmatched grid availability object")
                    local = local[:starts[0].start()] + profile + local[ends[0].end():]
                else:
                    close = local.rindex("End of PVObject pvGridStorage")
                    line_start = local.rfind("\n",0,close)+1
                    local = local[:line_start] + profile + local[line_start:]
                candidate_text = "".join(lines[:bounds[0]]) + local + "".join(lines[bounds[1]+1:])
                availability_snapshot = (input_path, input_bytes)
                result["grid_availability"] = metadata
            candidate=(b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"")+candidate_text.encode("utf-8")
            store._text(candidate)
            diff="".join(difflib.unified_diff(text.splitlines(keepends=True),candidate_text.splitlines(keepends=True)))
            result.update(status="ready",project=project,source_variant=source_variant,variant=new_variant,
                          before=before,after=after,artifacts={"variant":str(target)},diff=diff[:32000],diff_truncated=len(diff)>32000)
            result["warnings"].append("SOC values are fractions. SOCDischSelf is the WeakGrid reserve threshold: grid availability preserves it; outages permit discharge toward SOCDischDecl. It is unused as backup reserve in SelfConsumption. No grid-charge schedule or independent controller is added.")
            result["warnings"].append("PMaxCharge is the charger's AC input limit; PMaxDisch is the battery inverter's AC output limit. They are not DC port powers. Battery current/voltage and BMS compatibility stay unverified.")
            result["warnings"].append("Source PRJ/VC, PV components, battery filename/counts, temperature/SOW, load snapshot, site and topology stay unchanged. No grid-charge schedule or target SOC is invented.")
            if store._read(source)!=original or (availability_snapshot and availability_snapshot[0].read_bytes()!=availability_snapshot[1]):
                raise ValueError("Storage template changed before creation")
            if dry_run:
                return result
            result["stage"]="write"
            with target.open("xb") as stream:
                stream.write(candidate)
            result.update(status="created",stage="created",variant_created=True)
        except (ValueError,OSError,UnicodeError,TypeError,KeyError,StopIteration,csv.Error) as exc:
            result["issues"].append(str(exc))
            result["status"]="failed" if result["stage"]=="write" else "blocked"
            if result["status"]=="failed":
                result["warnings"].append("New variant may be a partial artifact; inspect the disclosed path. Original template is preserved.")
    return result

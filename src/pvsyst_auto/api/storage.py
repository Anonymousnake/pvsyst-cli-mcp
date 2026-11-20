"""Loads & Storage MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import Field, StrictBool, StrictInt, StrictStr
from pvsyst_auto.energy.profiles import prepare_load_profile
from pvsyst_auto.energy.storage import create_grid_storage_variant
from pvsyst_auto.energy.system import create_grid_storage_system
from pvsyst_auto.energy.tou_planner import plan_tou_storage_schedule
from pvsyst_auto.native.load import create_native_load_variant
from pvsyst_auto.api.schemas import (
    BatteryPackDesign,
    EconomicsTouPeriod,
    GridAvailabilityProfile,
    GridStorageDesign,
    GridStorageUpdates,
    NativeLoadRequest,
    StrictNum,
)
from pvsyst_auto.api.context import categorized_tool, cli, variant_store


@categorized_tool()
def pvsyst_prepare_load_profile(csv_name: StrictStr, year: StrictInt, output_name: StrictStr,
                                 utc_offset_hours: StrictInt,
                                 time_basis: Literal["local_standard_interval_start"],
                                 date_format: Literal["iso", "pvsyst_dot", "pvsyst_slash"] = "iso",
                                 date_column: StrictStr = "Date", load_column: StrictStr = "P Load",
                                 dry_run: StrictBool = True) -> dict:
    """Validate a complete 8760/8784-hour load CSV in workspace/UserData and emit a
    normalized kW CSV in UserHourlyParams plus a provenance manifest in Results."""
    return prepare_load_profile(cli(), csv_name=csv_name, year=year, output_name=output_name,
                                utc_offset_hours=utc_offset_hours, time_basis=time_basis,
                                date_format=date_format, date_column=date_column,
                                load_column=load_column, dry_run=dry_run)

@categorized_tool()
def pvsyst_create_native_load_variant(project: StrictStr, source_variant: StrictStr,
                                      new_variant: StrictStr, load: NativeLoadRequest,
                                      dry_run: StrictBool = True,
                                      variant_name: StrictStr | None = None,
                                      add_utf8_bom: StrictBool = False) -> dict:
    """Create a new PV-only self-consumption variant with a native constant (kW),
    monthly (12 x kWh), daily (24 x kW), or prepared hourly manifest load profile."""
    return create_native_load_variant(variant_store(), project=project, source_variant=source_variant,
                                      new_variant=new_variant, load=load.model_dump(),
                                      dry_run=dry_run, variant_name=variant_name,
                                      add_utf8_bom=add_utf8_bom)

@categorized_tool()
def pvsyst_create_grid_storage_variant(project: StrictStr, source_variant: StrictStr,
                                       new_variant: StrictStr, updates: GridStorageUpdates,
                                       dry_run: StrictBool = True,
                                       availability: GridAvailabilityProfile | None = None,
                                       allow_grid_export: StrictBool | None = None) -> dict:
    """Create a new variant updating SOC thresholds, AC charge/discharge power limits,
    converter efficiencies, strategy/schedules (SelfConsumption, WeakGrid, PeakShaving,
    PowerShift), grid_limit_kw, or allow_grid_export on an existing AC storage variant."""
    return create_grid_storage_variant(variant_store(), project=project, source_variant=source_variant,
                                       new_variant=new_variant,
                                       updates=updates.model_dump(exclude_none=True), dry_run=dry_run,
                                       availability=availability.model_dump() if availability else None,
                                       allow_grid_export=allow_grid_export)

@categorized_tool()
def pvsyst_create_grid_storage_system(project: StrictStr, source_variant: StrictStr,
                                     new_variant: StrictStr, battery: BatteryPackDesign,
                                     storage: GridStorageDesign,
                                     load_manifest_name: StrictStr | None = None,
                                     dry_run: StrictBool = True,
                                     availability: GridAvailabilityProfile | None = None,
                                     allow_grid_export: StrictBool | None = None) -> dict:
    """Create a new centralized AC-coupled storage variant (SelfConsumption, WeakGrid,
    PeakShaving, or PowerShift) with workspace or builtin battery pack hardware, SOC/power/
    efficiency parameters, optional 48-slot schedules or grid limit, and optional hourly load."""
    return create_grid_storage_system(variant_store(), project=project, source_variant=source_variant,
                                      new_variant=new_variant, battery=battery.model_dump(),
                                      storage=storage.model_dump(exclude_none=True),
                                      load_manifest_name=load_manifest_name,
                                      dry_run=dry_run,
                                      availability=availability.model_dump() if availability else None,
                                      allow_grid_export=allow_grid_export)


@categorized_tool()
def pvsyst_plan_tou_storage_schedule(
        tou_schedule: Annotated[list[EconomicsTouPeriod], Field(min_length=1, max_length=48)],
        charge_power_kw: StrictNum,
        discharge_power_kw: StrictNum | None = None,
        usable_energy_kwh: StrictNum | None = None,
        cycles_per_day: Annotated[StrictInt, Field(ge=1, le=2)] = 2,
        round_trip_efficiency: StrictNum = 0.88,
        min_spread: StrictNum = 0.05,
        month: Annotated[StrictInt, Field(ge=1, le=12)] = 7,
        day_type: Literal["all", "weekday", "weekend"] = "weekday",
        charging_strategy: Literal["spec_charg_power", "pv_prod_fraction"] = "spec_charg_power",
        default_import_rate: StrictNum = 0.0,
        default_export_rate: StrictNum = 0.0) -> dict:
    """Deterministically convert a time-of-use (TOU) tariff schedule into PVsyst PowerShift 48-slot
    half-hourly charge/discharge schedules (1 or 2 cycles/day) and a drop-in storage_updates dict."""
    return plan_tou_storage_schedule(
        tou_schedule=[p.model_dump(exclude_none=True) for p in tou_schedule],
        charge_power_kw=charge_power_kw,
        discharge_power_kw=discharge_power_kw,
        usable_energy_kwh=usable_energy_kwh,
        cycles_per_day=cycles_per_day,
        round_trip_efficiency=round_trip_efficiency,
        min_spread=min_spread,
        month=month,
        day_type=day_type,
        charging_strategy=charging_strategy,
        default_import_rate=default_import_rate,
        default_export_rate=default_export_rate,
    )

"""Loads & Storage MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from typing import Literal
from pydantic import StrictBool, StrictInt, StrictStr
from pvsyst_auto.energy.profiles import prepare_load_profile
from pvsyst_auto.energy.load import create_load_variant
from pvsyst_auto.energy.storage import create_grid_storage_variant
from pvsyst_auto.energy.system import create_grid_storage_system
from pvsyst_auto.native.load import create_native_load_variant
from pvsyst_auto.energy.peak import create_peak_shaving_variant
from pvsyst_auto.api.schemas import BatteryPackDesign, GridAvailabilityProfile, GridStorageDesign, GridStorageUpdates, NativeLoadRequest, PeakDischarge
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
def pvsyst_create_load_variant(project: StrictStr, source_variant: StrictStr, new_variant: StrictStr,
                                load_manifest_name: StrictStr, dry_run: StrictBool = True) -> dict:
    """Create a new variant binding a prepared hourly load manifest into pvLoad using
    signed-base62 Energy/MJ hourly encoding."""
    return create_load_variant(variant_store(), project=project, source_variant=source_variant,
                               new_variant=new_variant, load_manifest_name=load_manifest_name,
                               dry_run=dry_run)

@categorized_tool()
def pvsyst_create_peak_shaving_variant(project: StrictStr, source_variant: StrictStr,
                                       new_variant: StrictStr, discharge: PeakDischarge,
                                       dry_run: StrictBool = True,
                                       variant_name: StrictStr | None = None,
                                       add_utf8_bom: StrictBool = False) -> dict:
    """Create a new variant updating the PeakShaving discharge timing (after_sunset or
    hour_window with 0..23 start/end hours) on an existing PeakShaving/PVToGridMin template."""
    return create_peak_shaving_variant(variant_store(), project=project, source_variant=source_variant,
                                       new_variant=new_variant, discharge=discharge.model_dump(),
                                       dry_run=dry_run, variant_name=variant_name,
                                       add_utf8_bom=add_utf8_bom)

@categorized_tool()
def pvsyst_create_native_load_variant(project: StrictStr, source_variant: StrictStr,
                                      new_variant: StrictStr, load: NativeLoadRequest,
                                      dry_run: StrictBool = True,
                                      variant_name: StrictStr | None = None,
                                      add_utf8_bom: StrictBool = False) -> dict:
    """Create a new PV-only self-consumption variant with a native constant (kW),
    monthly (12 x kWh), or daily (24 x kW) load profile."""
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
    converter efficiencies, strategy, or allow_grid_export on an existing AC storage variant."""
    return create_grid_storage_variant(variant_store(), project=project, source_variant=source_variant,
                                       new_variant=new_variant,
                                       updates=updates.model_dump(exclude_none=True), dry_run=dry_run,
                                       availability=availability.model_dump() if availability else None,
                                       allow_grid_export=allow_grid_export)

@categorized_tool()
def pvsyst_create_grid_storage_system(project: StrictStr, source_variant: StrictStr,
                                     new_variant: StrictStr, battery: BatteryPackDesign,
                                     storage: GridStorageDesign, load_manifest_name: StrictStr,
                                     dry_run: StrictBool = True,
                                     availability: GridAvailabilityProfile | None = None,
                                     allow_grid_export: StrictBool | None = None) -> dict:
    """Create a new centralized AC-coupled storage variant (SelfConsumption or WeakGrid)
    with battery pack hardware, SOC/power/efficiency parameters, and a bound hourly load."""
    return create_grid_storage_system(variant_store(), project=project, source_variant=source_variant,
                                      new_variant=new_variant, battery=battery.model_dump(),
                                      storage=storage.model_dump(), load_manifest_name=load_manifest_name,
                                      dry_run=dry_run,
                                      availability=availability.model_dump() if availability else None,
                                      allow_grid_export=allow_grid_export)

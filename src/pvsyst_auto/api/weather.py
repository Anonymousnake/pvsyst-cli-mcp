"""Sites & Weather MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from typing import Annotated, Literal
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, StrictBool, StrictInt, StrictStr
from pvsyst_auto.energy.profiles import prepare_load_profile
from pvsyst_auto.energy.storage import create_grid_storage_variant, plan_tou_storage_schedule
from pvsyst_auto.energy.system import create_grid_storage_system
from pvsyst_auto.engineering.diagnostics import diagnose_simulation_csv
from pvsyst_auto.native.cli import (
    build_monthly_weather_csv, is_batch_summary_name, parse_batch_results,
    read_result_page, summarize_results,
)
from pvsyst_auto.native.load import create_native_load_variant
from pvsyst_auto.results import compare_results
from pvsyst_auto.weather.acquisition import download_weather, search_locations
from pvsyst_auto.weather.preparation import inspect_weather_csv, prepare_weather
from pvsyst_auto.weather.review import review_weather
from pvsyst_auto.api.schemas import (
    BatteryPackDesign, EconomicsTouPeriod, GridAvailabilityProfile,
    GridStorageDesign, GridStorageUpdates, NativeLoadRequest, StrictNum,
)
from pvsyst_auto.api.context import categorized_tool, cli, result_path, variant_store, workspace_path


@categorized_tool()
def pvsyst_inspect_weather_csv(csv_name: str, expected_year: StrictInt | None = None,
                               step_minutes: StrictInt | None = None) -> dict:
    """Validate a PVsyst standard hourly or sub-hourly (5/10/15/30/60 min) weather CSV in
    workspace/Meteo (headers, explicit units, full-year calendar including leap day, and monthly summaries)."""
    try:
        return inspect_weather_csv(workspace_path("Meteo", csv_name, ".csv"), expected_year,
                                   step_minutes=step_minutes)
    except (ValueError, OSError) as exc:
        raise ToolError(str(exc)) from None

@categorized_tool()
def pvsyst_prepare_weather(csv_name: StrictStr = "", year: StrictInt | None = None,
                            sit_name: StrictStr = "", met_name: StrictStr = "",
                            country_code: StrictStr = "", time_basis: StrictStr = "",
                            step_minutes: StrictInt | None = None,
                            dry_run: StrictBool = True) -> dict:
    """Convert a validated full-year hourly or sub-hourly (5/10/15/30/60 min) weather CSV into
    new SIT and MET files and write a provenance manifest under workspace/Results."""
    return prepare_weather(cli(), csv_name=csv_name, year=year, sit_name=sit_name,
                           met_name=met_name, country_code=country_code,
                           time_basis=time_basis, step_minutes=step_minutes, dry_run=dry_run)

@categorized_tool()
def pvsyst_resolve_location(query: StrictStr, country_code: StrictStr = "") -> dict:
    """Query Open-Meteo geocoding for a place name (2..100 chars) and return candidates
    with location_id, coordinates, ISO alpha-2 country code, and IANA timezone."""
    return search_locations(query, country_code)

@categorized_tool()
def pvsyst_acquire_weather(csv_name: StrictStr = "", year: StrictInt | None = None,
                           location_id: StrictInt | None = None,
                           latitude: StrictNum | None = None, longitude: StrictNum | None = None,
                           site_name: StrictStr = "", country_code: StrictStr = "",
                           altitude: StrictNum | None = None, timezone_name: StrictStr = "",
                           temperature_method: StrictStr = "endpoint_mean",
                           provider: Literal["open_meteo_era5", "pvgis_tmy", "nasa_power"] = "open_meteo_era5",
                           strict_dhi: StrictBool = False,
                           prepare: StrictBool = False,
                           sit_name: StrictStr = "", met_name: StrictStr = "",
                           dry_run: StrictBool = True) -> dict:
    """Download a full-year hourly weather dataset from Open-Meteo ERA5, PVGIS v5.3 TMY, or
    NASA POWER aligned to local winter standard time, saving raw JSON, CSV, and manifest.
    By default, small twilight satellite inversion excess (0 < GHI < 30 W/m2 and DHI - GHI <= 15 W/m2)
    is clamped to DHI = GHI with a manifest warning; pass strict_dhi=true to reject instead.
    Set prepare=true to chain directly into prepare_weather and emit SIT/MEF/MET."""
    current = cli()
    with current._lock:
        acquired = download_weather(
            current.workspace, csv_name=csv_name, year=year,
            location_id=location_id, latitude=latitude, longitude=longitude,
            site_name=site_name, country_code=country_code, altitude=altitude,
            timezone_name=timezone_name, temperature_method=temperature_method,
            provider=provider, strict_dhi=strict_dhi, dry_run=dry_run,
        )
    if prepare:
        stem = (csv_name[:-4] if csv_name.lower().endswith(".csv") else csv_name) or "AcquiredWeather"
        target_sit = sit_name or f"{stem}.SIT"
        target_met = met_name or f"{stem}.MET"
        effective_country = country_code or ((acquired.get("location") or {}).get("country_code") or "")
        effective_year = acquired.get("year")
        if dry_run:
            acquired["preparation_plan"] = {
                "sit_name": target_sit,
                "met_name": target_met,
                "year": effective_year,
                "country_code": effective_country,
                "time_basis": "local_standard_interval_start",
            }
        elif acquired.get("status") == "prepared":
            acquired["preparation"] = prepare_weather(
                current,
                csv_name=csv_name,
                year=effective_year,
                sit_name=target_sit,
                met_name=target_met,
                country_code=effective_country,
                time_basis="local_standard_interval_start",
                dry_run=False,
            )
            if acquired["preparation"]["status"] != "prepared":
                acquired.update(status=acquired["preparation"]["status"], stage="preparation")
    return acquired

@categorized_tool()
def pvsyst_review_weather(csv_name: StrictStr, native_csv_name: StrictStr, year: StrictInt,
                          acquisition_manifest_name: StrictStr = "",
                          weather_manifest_name: StrictStr = "") -> dict:
    """Compare a prepared hourly weather CSV in Meteo against exported simulation hourly
    values in Results and inspect the associated SIT/MET header metadata."""
    return review_weather(cli().workspace, csv_name=csv_name, native_csv_name=native_csv_name,
                          year=year, acquisition_manifest_name=acquisition_manifest_name,
                          weather_manifest_name=weather_manifest_name)

@categorized_tool()
def pvsyst_build_monthly_weather(filename: str, global_h: list[StrictNum],
                                 temperature: list[StrictNum],
                                 diffuse_h: list[StrictNum] | None = None,
                                 wind_velocity: list[StrictNum] | None = None) -> dict:
    """Create the 14-row monthly weather CSV for create-site in workspace/Meteo."""
    with cli()._lock:
        target = workspace_path("Meteo", filename, ".csv")
        build_monthly_weather_csv(target, global_h, temperature, diffuse_h, wind_velocity)
    return {"weather_csv": str(target)}

@categorized_tool()
def pvsyst_create_site(site_name: str, latitude: StrictNum, longitude: StrictNum,
                       weather_csv_name: str, sit_name: str,
                       altitude: StrictNum | None = None, timezone: StrictNum | None = None,
                       country_code: str = "",
                       region: Literal[
                           "", "africa", "antarctica", "asia", "australia",
                           "europe", "north_america", "pacific", "south_america",
                       ] = "",
                       source: str = "",
                       albedo: StrictNum | None = None, log_level: StrictInt | None = None) -> dict:
    """Use CLI create-site to create a NEW SIT in workspace/Sites (8.1+).
    Stage or generate monthly weather CSV in workspace/Meteo. Allowed region values:
    africa, antarctica, asia, australia, europe, north_america, pacific, south_america (lowercase).
    If altitude, timezone or country are omitted, the CLI may query an external service."""
    weather = workspace_path("Meteo", weather_csv_name, ".csv")
    target = workspace_path("Sites", sit_name, ".sit")
    output = cli().create_site(site_name, latitude, longitude, weather, target,
                               altitude=altitude, timezone=timezone,
                               country_code=country_code, region=region, source=source,
                               albedo=albedo, log_level=log_level)
    return {"site": str(output)}

@categorized_tool()
def pvsyst_convert_meteo(csv_name: str, mef_name: str, sit_name: str,
                         met_name: str, timeshift: StrictInt = 0,
                         log_level: StrictInt | None = None) -> dict:
    """Convert a staged CSV + MEF in Meteo and SIT in Sites to a NEW MET."""
    source = workspace_path("Meteo", csv_name, ".csv")
    mef = workspace_path("Meteo", mef_name, ".mef")
    sit = workspace_path("Sites", sit_name, ".sit")
    target = workspace_path("Meteo", met_name, ".met")
    return {"met": str(cli().convert_meteo(source, mef, sit, target, timeshift,
                                            log_level=log_level))}


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
                                       availability=availability.model_dump(exclude_none=True) if availability else None,
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
                                      availability=availability.model_dump(exclude_none=True) if availability else None,
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


@categorized_tool()
def pvsyst_read_results(csv_name: str, columns: str = "E_Grid,PR",
                        folder: str = "Results", compare_to: str = "",
                        compare_folder: str | None = None) -> dict:
    """Summarize hourly or sub-hourly SFI rows in Results or UserHourly, or diff
    target (csv_name) minus baseline (compare_to) across 1..64 aligned columns."""
    requested = tuple(column.strip() for column in columns.split(",") if column.strip())
    if not 1 <= len(requested) <= 64 or len(set(requested)) != len(requested):
        raise ValueError("Select 1..64 distinct numeric columns")
    if compare_to:
        baseline_folder = folder if compare_folder is None else compare_folder
        result = compare_results(result_path(baseline_folder, compare_to),
                                 result_path(folder, csv_name), requested)
        result["baseline"]["folder"] = baseline_folder
        result["target"]["folder"] = folder
        return result
    if compare_folder is not None:
        raise ValueError("compare_folder requires compare_to")
    return summarize_results(result_path(folder, csv_name), requested)

@categorized_tool()
def pvsyst_read_rows(csv_name: str, columns: str = "E_Grid,PR",
                     offset: StrictInt = 0, limit: StrictInt = 100,
                     folder: str = "Results") -> dict:
    """Read a bounded page from Results or batch UserHourly CSVs."""
    if offset < 0 or not 1 <= limit <= 500:
        raise ValueError("Offset must be >= 0 and limit between 1 and 500")
    selected = [s.strip() for s in columns.split(",") if s.strip()]
    return read_result_page(result_path(folder, csv_name), selected, offset, limit)

@categorized_tool()
def pvsyst_read_batch_results(summary_name: str, columns: str = "E_Grid,PR") -> dict:
    """Read SIM_* scenario values from a UserBatch/*Results[_<n>].CSV summary."""
    if not is_batch_summary_name(summary_name):
        raise ValueError("Expected a batch Results.CSV filename")
    target = workspace_path("UserBatch", summary_name, ".csv")
    requested = tuple(s.strip() for s in columns.split(",") if s.strip())
    if not requested:
        raise ValueError("Select at least one batch result column")
    return parse_batch_results(target, requested)

@categorized_tool()
def pvsyst_diagnose_results(
        csv_name: StrictStr,
        folder: Literal["Results", "UserHourly"] = "Results",
        project: StrictStr | None = None,
        variant: StrictStr | None = None,
        thresholds: dict[str, StrictNum] | None = None) -> dict:
    """Run deterministic physics loss-chain and time-series anomaly diagnostics on an SFI result
    CSV (PR bounds, inverter clipping/voltage/current limits, shading/curtailment, storage balance,
    daytime zero-output and phase lag), with optional .VC0 root-cause attribution."""
    target = result_path(folder, csv_name)
    store = variant_store() if (project or variant) else None
    return diagnose_simulation_csv(
        target, store=store, project=project, variant=variant, thresholds=thresholds,
    )

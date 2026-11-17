"""Sites & Weather MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import StrictBool, StrictInt, StrictStr
from pvsyst_auto.native.cli import build_monthly_weather_csv
from pvsyst_auto.weather.csv import inspect_weather_csv
from pvsyst_auto.weather.acquisition import download_weather, search_locations
from pvsyst_auto.weather.preparation import prepare_weather
from pvsyst_auto.weather.review import review_weather
from pvsyst_auto.api.schemas import StrictNum
from pvsyst_auto.api.context import categorized_tool, cli, workspace_path


@categorized_tool()
def pvsyst_inspect_weather_csv(csv_name: str, expected_year: StrictInt | None = None) -> dict:
    """Validate a PVsyst standard hourly weather CSV in workspace/Meteo (headers, explicit
    units, full-year hourly calendar including leap day, and monthly GHI/Tamb summaries)."""
    try:
        return inspect_weather_csv(workspace_path("Meteo", csv_name, ".csv"), expected_year)
    except (ValueError, OSError) as exc:
        raise ToolError(str(exc)) from None

@categorized_tool()
def pvsyst_prepare_weather(csv_name: StrictStr = "", year: StrictInt | None = None,
                            sit_name: StrictStr = "", met_name: StrictStr = "",
                            country_code: StrictStr = "", time_basis: StrictStr = "",
                            dry_run: StrictBool = True) -> dict:
    """Convert a validated full-year hourly weather CSV into new SIT and MET files
    and write a provenance manifest under workspace/Results."""
    return prepare_weather(cli(), csv_name=csv_name, year=year, sit_name=sit_name,
                           met_name=met_name, country_code=country_code,
                           time_basis=time_basis, dry_run=dry_run)

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
                           dry_run: StrictBool = True) -> dict:
    """Download a full calendar year (1969..2068) of Open-Meteo ERA5 hourly irradiance and
    temperature aligned to local winter standard time, saving raw JSON, CSV, and manifest."""
    return download_weather(cli().workspace, csv_name=csv_name, year=year,
                            location_id=location_id, latitude=latitude, longitude=longitude,
                            site_name=site_name, country_code=country_code, altitude=altitude,
                            timezone_name=timezone_name, temperature_method=temperature_method,
                            dry_run=dry_run)

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
def pvsyst_build_monthly_weather(filename: str, global_h: list[float],
                                 temperature: list[float],
                                 diffuse_h: list[float] | None = None,
                                 wind_velocity: list[float] | None = None) -> dict:
    """Create the 14-row monthly weather CSV for create-site in workspace/Meteo."""
    target = workspace_path("Meteo", filename, ".csv")
    build_monthly_weather_csv(target, global_h, temperature, diffuse_h, wind_velocity)
    return {"weather_csv": str(target)}

@categorized_tool()
def pvsyst_create_site(site_name: str, latitude: float, longitude: float,
                       weather_csv_name: str, sit_name: str,
                       altitude: float | None = None, timezone: float | None = None,
                       country_code: str = "", region: str = "", source: str = "",
                       albedo: float | None = None, log_level: int | None = None) -> dict:
    """Use CLI create-site to create a NEW SIT in workspace/Sites (8.1+).
    Stage or generate monthly weather CSV in workspace/Meteo. If altitude,
    timezone or country are omitted, the CLI may query an external service."""
    weather = workspace_path("Meteo", weather_csv_name, ".csv")
    target = workspace_path("Sites", sit_name, ".sit")
    output = cli().create_site(site_name, latitude, longitude, weather, target,
                               altitude=altitude, timezone=timezone,
                               country_code=country_code, region=region, source=source,
                               albedo=albedo, log_level=log_level)
    return {"site": str(output)}

@categorized_tool()
def pvsyst_convert_meteo(csv_name: str, mef_name: str, sit_name: str,
                         met_name: str, timeshift: int = 0,
                         log_level: int | None = None) -> dict:
    """Convert a staged CSV + MEF in Meteo and SIT in Sites to a NEW MET."""
    source = workspace_path("Meteo", csv_name, ".csv")
    mef = workspace_path("Meteo", mef_name, ".mef")
    sit = workspace_path("Sites", sit_name, ".sit")
    target = workspace_path("Meteo", met_name, ".met")
    return {"met": str(cli().convert_meteo(source, mef, sit, target, timeshift,
                                            log_level=log_level))}

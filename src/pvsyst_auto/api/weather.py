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
    """Check a PVsyst standard hourly CSV in Meteo before preparing a full-year run.
    Read-only: reports metadata/units, gaps, duplicates, bad numbers and observed
    monthly irradiation/temperature with input SHA-256. GHI/Tamb required; optional
    DHI/DNI and weather columns supported. Raw timestamps must cover one whole year
    at 00:00..23:00 (including leap day); no silent filling or time conversion.
    Nonzero shifts, explicit time references and POA need separate review. valid
    covers this CSV profile only, not MEF/SIT matching or native MET correctness."""
    try:
        return inspect_weather_csv(workspace_path("Meteo", csv_name, ".csv"), expected_year)
    except (ValueError, OSError) as exc:
        raise ToolError(str(exc)) from None

@categorized_tool()
def pvsyst_prepare_weather(csv_name: StrictStr = "", year: StrictInt | None = None,
                            sit_name: StrictStr = "", met_name: StrictStr = "",
                            country_code: StrictStr = "", time_basis: StrictStr = "",
                            dry_run: StrictBool = True) -> dict:
    """Prepare NEW native SIT/MET and a provenance manifest from one full hourly CSV.
    GHI, DHI, Tamb only; explicit uppercase ISO country code and time_basis
    local_standard_interval_start are required (fixed winter timezone, no DST).
    Coordinates, altitude, timezone, site and source come from verified CSV metadata.
    dry_run=true writes nothing. Execution preserves raw bytes, normalizes only
    columns/units, creates monthly CSV/MEF, runs create-site and convert-meteo.
    Conversion warnings are retained as needs_review; prepared is not native
    MET certification. Pass artifacts.manifest filename as weather_manifest_name
    to simulate_template for explicit SIT/MET loading and hourly weather comparison.
    No UTC/DST shifts, missing-data filling, ignored columns or binary parsing.
    Existing outputs are never overwritten; failure artifacts/manifest are retained."""
    return prepare_weather(cli(), csv_name=csv_name, year=year, sit_name=sit_name,
                           met_name=met_name, country_code=country_code,
                           time_basis=time_basis, dry_run=dry_run)

@categorized_tool()
def pvsyst_resolve_location(query: StrictStr, country_code: StrictStr = "") -> dict:
    """Resolve a place name into provider geocoding candidates without selecting one.
    Read-only geocoding lookup: returns every candidate with its integer location_id,
    coordinates, ISO country code and IANA zone, and sets selection_required unless
    exactly one candidate matched. query must be 2..100 characters of a place name;
    country_code, when given, must be an uppercase ISO alpha-2 code. No candidate is
    chosen automatically, nothing is inferred from a single hit and no weather is
    downloaded or written."""
    return search_locations(query, country_code)

@categorized_tool()
def pvsyst_acquire_weather(csv_name: StrictStr = "", year: StrictInt | None = None,
                           location_id: StrictInt | None = None,
                           latitude: StrictNum | None = None, longitude: StrictNum | None = None,
                           site_name: StrictStr = "", country_code: StrictStr = "",
                           altitude: StrictNum | None = None, timezone_name: StrictStr = "",
                           temperature_method: StrictStr = "endpoint_mean",
                           dry_run: StrictBool = True) -> dict:
    """Plan or execute one Open-Meteo ERA5 year as a PVsyst standard hourly weather CSV.
    Give either location_id from pvsyst_resolve_location, or explicit latitude/longitude
    together with site_name, country_code and timezone_name; mixing both is blocked.
    year is an integer in 1940..2068 and the requested year must already be fully
    published by the provider. temperature_method=endpoint_mean is the only verified
    method: the unweighted mean of the two UTC instants bounding each local standard
    hour, disclosed as a linear approximation. dry_run=true (default) downloads nothing
    and writes nothing; an execution run writes the raw provider response, the standard
    CSV and a provenance manifest, refuses to overwrite existing files and never fills,
    zeroes or interpolates a null hour. Half-hour IANA offsets and standard offsets that
    change inside the requested year are blocked instead of being rebinned. Native MET
    conversion and pyranometer quality are outside this scope."""
    return download_weather(cli().workspace, csv_name=csv_name, year=year,
                            location_id=location_id, latitude=latitude, longitude=longitude,
                            site_name=site_name, country_code=country_code, altitude=altitude,
                            timezone_name=timezone_name, temperature_method=temperature_method,
                            dry_run=dry_run)

@categorized_tool()
def pvsyst_review_weather(csv_name: StrictStr, native_csv_name: StrictStr, year: StrictInt,
                          acquisition_manifest_name: StrictStr = "",
                          weather_manifest_name: StrictStr = "") -> dict:
    """Review one prepared weather CSV against one native result, read-only.
    csv_name names the prepared standard CSV in workspace/Meteo and native_csv_name the
    exported hourly result in workspace/Results; year is the strict integer year under
    review. The CSV is re-inspected with the hash-free value checker, so a historical
    manifest report is never trusted; the optional acquisition_manifest_name and
    weather_manifest_name add the stored acquisition provenance and the declared time
    basis. Provider coverage and native hourly values are compared as measured
    quantities, and native .MET/.SIT declared fields are read from their file headers
    only. This review does not certify the effective sun-position time shift inside the
    native run, does not claim that the endpoint_mean ambient temperature is a measured
    hourly mean, and does not certify climate extremes. Nothing is written, hashed or
    simulated. status is blocked, needs_review or completed: review items are listed in
    the result instead of being dropped, and blocked means the evidence could not be
    read at all."""
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

"""Bounded Open-Meteo ERA5 acquisition into a PVsyst standard hourly weather CSV.

Provider facts this module depends on (verified against the live public API and the
published documentation):

* ``archive-api.open-meteo.com/v1/archive`` with ``models=era5`` serves the ERA5
  reanalysis on its native 0.25 degree grid (1940 onwards). The echoed
  ``latitude``/``longitude`` are snapped grid centres and the echoed ``elevation``
  is a DEM value, not the geocoded place elevation; both are recorded as provider
  evidence instead of being written into site metadata.
* ``shortwave_radiation`` (GHI) and ``diffuse_radiation`` (DHI) are ``preceding hour
  mean`` values: the ISO8601 label marks the UTC interval END. ``temperature_2m`` is
  an INSTANTANEOUS value at the labelled UTC hour and is not an observed hourly mean.
* Units are requested and verified explicitly: W/m2 for both irradiances, degrees C
  for temperature. Wind is never requested: the provider default is km/h and the
  verified import profile maps GHI, DHI and Tamb only.
* Unpublished hours (roughly the last five days) come back as JSON ``null``; they are
  never filled, zeroed or interpolated here.

Time basis of the emitted CSV is ``local_standard_interval_start``: one fixed
standard (winter) UTC offset derived as ``utcoffset - dst`` from the IANA zone, hourly
interval-start labels in local standard time, no daylight saving, and no half-hour
offsets. A half-hour offset or a standard offset that changes inside the requested
year is reported as a concrete block instead of being silently rebinned.

No hash, fingerprint or pre-existing repository backend is used: the module talks to
two fixed public endpoints with the standard library only, and validates the provider
payload with its own bounded checker.
"""
from __future__ import annotations

import calendar
import csv
import http.client
import io
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pvsyst_auto.project.paths import workspace_file

GEOCODING_SEARCH_URL = "https://geocoding-api.open-meteo.com/v1/search"
GEOCODING_GET_URL = "https://geocoding-api.open-meteo.com/v1/get"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
PVGIS_TMY_URL = "https://re.jrc.ec.europa.eu/api/v5_3/tmy"
NASA_POWER_URL = "https://power.larc.nasa.gov/api/temporal/hourly/point"
ALLOWED_HOSTS = frozenset({
    "geocoding-api.open-meteo.com",
    "archive-api.open-meteo.com",
    "re.jrc.ec.europa.eu",
    "power.larc.nasa.gov",
})
PROVIDERS = ("open_meteo_era5", "pvgis_tmy", "nasa_power")

PROVIDER_MODEL = "era5"
PROVIDER_LABEL = ("Open-Meteo ERA5 reanalysis "
                  "(archive-api.open-meteo.com/v1/archive, models=era5, timezone=GMT)")
GEOCODING_LABEL = "Open-Meteo Geocoding API (GeoNames)"
HOURLY_VARIABLES = ("shortwave_radiation", "diffuse_radiation", "temperature_2m")
CSV_COLUMNS = ("GHI", "DHI", "Tamb")
CSV_UNITS = {"GHI": "W/m2", "DHI": "W/m2", "Tamb": "deg.C"}
ACCEPTED_UNITS = {"shortwave_radiation": ("W/m\u00b2", "W/m2"),
                  "diffuse_radiation": ("W/m\u00b2", "W/m2"),
                  "temperature_2m": ("\u00b0C", "degC")}
TEMPERATURE_METHODS = ("endpoint_mean",)
REQUEST_TIMEOUT_SECONDS = 60
REQUEST_DEADLINE_SECONDS = 300   # whole-request budget; the timeout above bounds one socket wait
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 64 * 1024
MIN_YEAR = 1940          # ERA5 archive start, verified with a live request.
MAX_YEAR = 2068          # same upper bound as the verified PVsyst import profile.
PREPARED_MIN_YEAR = 1969  # prepare_weather/review_weather floor: native dates carry two-digit years
PUBLICATION_LAG_DAYS = 5  # ERA5 is published roughly five days behind real time
MIN_TIME_ZONE_HOURS = -12
MAX_TIME_ZONE_HOURS = 12   # installed PVsyst CLI profile, verified as a create-site bound
GEOCODING_COUNT = 10
NAME_BYTES = 200           # same printable one-line bound the import profile applies
TIMEZONE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_+\-/]{0,63}")
ISO_COUNTRY = re.compile(r"[A-Z]{2}")
TEMPERATURE_WARNING = ("temperature_2m is an instantaneous 2 m air temperature at each "
                       "labelled UTC hour; the hourly Tamb is the unweighted mean of the two "
                       "UTC instants bounding each local standard hour (endpoint_mean, a "
                       "disclosed linear approximation), not an observed hourly mean")
GRID_WARNING = ("the raw response echoes ERA5 0.25 degree grid centres and a DEM elevation; "
                "site metadata keeps the requested place coordinates instead")


class WeatherAcquisitionError(RuntimeError):
    """Transport, provider or payload failure carrying a readable reason."""


def _number_text(value):
    """Stable decimal text without scientific notation or a negative zero."""
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _finite_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WeatherAcquisitionError(f"{label} must be a number, got {type(value).__name__}")
    result = float(value)
    if not math.isfinite(result):
        raise WeatherAcquisitionError(f"{label} must be finite")
    return result


def _printable_text(value, label, limit=NAME_BYTES):
    """One printable line of at most `limit` UTF-8 bytes, as the import profile expects."""
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit
            or any(not character.isprintable() for character in value)):
        raise WeatherAcquisitionError(
            f"{label} must be 1..{limit} printable UTF-8 bytes on a single line")
    return value


def _bounded_write(path, text):
    """Create a new file; an existing path is never truncated or replaced."""
    with path.open("x", encoding="utf-8", newline="") as stream:
        stream.write(text)


def _read_bounded(response, deadline=None):
    """Read at most MAX_RESPONSE_BYTES from one provider response, ending by `deadline`.

    The socket timeout bounds a single wait only, so a body trickling in slowly is also cut
    off at the monotonic `deadline`. A real HTTP response is read with ``read1`` (at most one
    socket read per call) so the deadline is checked between socket reads.
    """
    headers = getattr(response, "headers", None)
    declared = headers.get("Content-Length") if headers is not None else None
    if declared is not None:
        try:
            if int(declared) > MAX_RESPONSE_BYTES:
                raise WeatherAcquisitionError(
                    f"provider response of {declared} bytes exceeds the "
                    f"{MAX_RESPONSE_BYTES}-byte request limit")
        except ValueError:
            pass
    read = response.read1 if isinstance(response, http.client.HTTPResponse) else response.read
    chunks, total = [], 0
    while True:
        chunk = read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise WeatherAcquisitionError(
                f"provider response exceeds the {MAX_RESPONSE_BYTES}-byte request limit")
        chunks.append(chunk)
        if deadline is not None and time.monotonic() > deadline:
            raise WeatherAcquisitionError(
                f"provider response did not complete within the {REQUEST_DEADLINE_SECONDS}-second "
                "request deadline")
    return b"".join(chunks)


def _error_reason(error):
    """Read a bounded error body and prefer the provider's own reason text."""
    try:
        body = error.read(MAX_ERROR_BODY_BYTES)
    except Exception:  # pragma: no cover - defensive only around a broken stream
        return "no readable error body"
    if not body:
        return "empty error body"
    text = body.decode("utf-8", "replace").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("reason"), str):
        return payload["reason"]
    return text[:200]


def _provider_get(url, params, *, timeout=REQUEST_TIMEOUT_SECONDS):
    """GET one fixed public endpoint; returns (payload, raw_text, request_url)."""
    host = urllib.parse.urlsplit(url).netloc
    if host not in ALLOWED_HOSTS:
        raise WeatherAcquisitionError(f"refusing to call the non-provider host {host!r}")
    request_url = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        request_url, headers={"User-Agent": "pvsyst-auto-weather/1.0 (stdlib urllib)"})
    deadline = time.monotonic() + REQUEST_DEADLINE_SECONDS
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = _read_bounded(response, deadline)
    except urllib.error.HTTPError as error:
        raise WeatherAcquisitionError(
            f"HTTP {error.code} from {host}: {_error_reason(error)}") from None
    except urllib.error.URLError as error:
        raise WeatherAcquisitionError(f"transport failure for {host}: {error.reason}") from None
    except TimeoutError:
        raise WeatherAcquisitionError(
            f"no response from {host} within {timeout} seconds") from None
    except (http.client.HTTPException, OSError) as error:
        # A body cut off mid-read (IncompleteRead, connection reset) is a transport failure too.
        raise WeatherAcquisitionError(
            f"transport failure for {host}: {type(error).__name__}: {error}") from None
    try:
        raw_text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WeatherAcquisitionError(
            f"invalid UTF-8 from {host} at byte {error.start}") from None
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise WeatherAcquisitionError(
            f"invalid JSON from {host}: {error.msg} at position {error.pos}") from None
    if not isinstance(payload, dict):
        raise WeatherAcquisitionError(f"expected a JSON object from {host}")
    if payload.get("error") is True:
        raise WeatherAcquisitionError(
            f"provider error for {host}: {payload.get('reason', 'no reason given')}")
    return payload, raw_text, request_url


def _have_timezone_database():
    """True when this interpreter can resolve IANA zone keys at all.

    On Windows CPython ships no zone data, so ``ZoneInfo`` only works once the
    ``tzdata`` package (or an equivalent TZPATH entry) is present.
    """
    if zoneinfo.TZPATH:
        return True
    try:
        import tzdata  # noqa: F401  (presence is the evidence)
    except ImportError:
        return False
    return True


def standard_utc_offset_hours(timezone_name, year):
    """Fixed standard (winter) offset in whole hours, checked on every day of `year`.

    The offset is ``utcoffset - dst`` so daylight saving never leaks into the value.
    Half-hour offsets and offsets that vary inside the year are rejected here because
    the import profile keeps hourly interval-start labels without rebinning.
    """
    if not isinstance(timezone_name, str) or not TIMEZONE_NAME.fullmatch(timezone_name):
        raise WeatherAcquisitionError(
            f"timezone_name must be an IANA zone name such as Asia/Shanghai, got {timezone_name!r}")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        if _have_timezone_database():
            raise WeatherAcquisitionError(
                f"unknown IANA zone {timezone_name!r}; pass a key from the IANA time zone "
                "database such as Asia/Shanghai") from None
        raise WeatherAcquisitionError(
            f"cannot resolve the IANA zone {timezone_name!r}: no IANA time zone database is "
            "available to Python on this host (install the tzdata package)") from None
    sampled, negative = {}, []
    day = date(year, 1, 1)
    while day.year == year:
        noon = datetime(day.year, day.month, day.day, 12, 0)
        offset = zone.utcoffset(noon)
        if offset is None:  # pragma: no cover - tzdata zones always report an offset
            raise WeatherAcquisitionError(f"{timezone_name} returned no UTC offset")
        daylight = zone.dst(noon) or timedelta(0)
        if daylight < timedelta(0) and len(negative) < 3:
            negative.append(f"{day} dst={daylight}")
        sampled[day] = offset - daylight
        day += timedelta(days=1)
    if negative:
        raise WeatherAcquisitionError(
            f"{timezone_name!r} uses a negative daylight-saving offset ({'; '.join(negative)}), as "
            "Europe/Dublin does in winter. utcoffset - dst then reports the legal standard offset "
            "(+1 h for Dublin) while the actual winter wall-clock offset is 0 h, so this module "
            "cannot tell which convention a local winter standard time means. No offset is guessed "
            "and the minimum is not substituted, because negative DST is not always a winter "
            "signal (Morocco uses it during Ramadan): pass a fixed, verified IANA zone whose winter "
            "offset you have confirmed, or a zone without negative daylight saving")
    distinct = sorted(set(sampled.values()))
    if len(distinct) != 1:
        examples = ", ".join(f"{item}={value}" for item, value in sorted(sampled.items())[:3])
        raise WeatherAcquisitionError(
            f"standard offset of {timezone_name!r} is not constant during {year} "
            f"(observed {len(distinct)} values: {[str(value) for value in distinct]}); "
            f"no single local standard time basis exists, examples {examples}")
    seconds = int(distinct[0].total_seconds())
    if seconds % 3600:
        raise WeatherAcquisitionError(
            f"standard offset of {timezone_name!r} is {seconds / 3600} h, not a whole hour; "
            "half-hour offsets would need UTC-hour rebinning, which this module does not do")
    hours = seconds // 3600
    if not MIN_TIME_ZONE_HOURS <= hours <= MAX_TIME_ZONE_HOURS:
        raise WeatherAcquisitionError(
            f"standard offset {hours:+d} h is outside the installed PVsyst CLI profile "
            f"{MIN_TIME_ZONE_HOURS}..{MAX_TIME_ZONE_HOURS}. The IANA offset itself is real "
            "geography, not a data error: this module simply does not create-site or shift a "
            "time zone that the installed profile cannot express, so the run is blocked instead "
            "of silently rewriting the offset")
    return hours


def _candidate(entry, *, index):
    if not isinstance(entry, dict):
        raise WeatherAcquisitionError("provider returned a non-object geocoding candidate")
    location_id = entry.get("id")
    if isinstance(location_id, bool) or not isinstance(location_id, int):
        raise WeatherAcquisitionError("geocoding candidate is missing an integer id")
    latitude = _finite_number(entry.get("latitude"), f"candidate {index} latitude")
    longitude = _finite_number(entry.get("longitude"), f"candidate {index} longitude")
    if not -90 <= latitude <= 90:
        raise WeatherAcquisitionError(f"candidate {index} latitude {latitude} is outside -90..90")
    if not -180 <= longitude <= 180:
        raise WeatherAcquisitionError(
            f"candidate {index} longitude {longitude} is outside -180..180")
    elevation = entry.get("elevation")
    population = entry.get("population")
    if isinstance(population, bool) or not isinstance(population, int):
        population = None
    return {
        "location_id": location_id,
        "name": entry.get("name") if isinstance(entry.get("name"), str) else "",
        "latitude": latitude,
        "longitude": longitude,
        "elevation_m": None if elevation is None else _finite_number(elevation, "candidate elevation"),
        "country_code": entry.get("country_code") if isinstance(entry.get("country_code"), str) else "",
        "country": entry.get("country") if isinstance(entry.get("country"), str) else "",
        "admin1": entry.get("admin1") if isinstance(entry.get("admin1"), str) else "",
        "timezone_name": entry.get("timezone") if isinstance(entry.get("timezone"), str) else "",
        "feature_code": entry.get("feature_code") if isinstance(entry.get("feature_code"), str) else "",
        "population": population,
        "get_url": f"{GEOCODING_GET_URL}?id={location_id}",
    }


def search_locations(query, country_code=""):
    """Return every geocoding candidate for a place name; never choose one silently."""
    result = {"status": "blocked", "query": query, "country_code": country_code,
              "provider": GEOCODING_LABEL, "candidate_count": 0, "candidates": [],
              "ambiguous": False, "selection_required": True, "request_url": None,
              "issues": [], "warnings": []}
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 100:
        result["issues"].append("query must be 2..100 characters of a place name")
        return result
    if country_code and (not isinstance(country_code, str) or not ISO_COUNTRY.fullmatch(country_code)):
        result["issues"].append("country_code must be an uppercase ISO alpha-2 code")
        return result
    params = {"name": query.strip(), "count": GEOCODING_COUNT, "language": "en", "format": "json"}
    if country_code:
        params["countryCode"] = country_code
    try:
        payload, _raw, request_url = _provider_get(GEOCODING_SEARCH_URL, params)
    except WeatherAcquisitionError as error:
        result["issues"].append(str(error))
        return result
    result["request_url"] = request_url
    entries = payload.get("results")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        result["issues"].append("provider returned a non-list geocoding result set")
        return result
    try:
        candidates = [_candidate(entry, index=index) for index, entry in enumerate(entries)]
    except WeatherAcquisitionError as error:
        result["issues"].append(str(error))
        return result
    result["candidates"] = candidates
    result["candidate_count"] = len(candidates)
    result["ambiguous"] = len(candidates) > 1
    result["selection_required"] = len(candidates) != 1
    if not candidates:
        result["issues"].append(
            f"no geocoding candidate matched {query!r}"
            + (f" in {country_code}" if country_code else "")
            + "; add a country or administrative qualifier, or pass explicit coordinates")
        return result
    if len(candidates) > 1:
        result["warnings"].append(
            f"{len(candidates)} candidates matched; pass one location_id to download_weather "
            "because this module never takes the first hit for an ambiguous name")
    result["status"] = "ready"
    return result


def _geocode_location(location_id):
    if isinstance(location_id, bool) or not isinstance(location_id, int) or location_id <= 0:
        raise WeatherAcquisitionError("location_id must be a positive integer from search_locations")
    payload, _raw, request_url = _provider_get(GEOCODING_GET_URL, {"id": location_id})
    if "latitude" not in payload or "longitude" not in payload:
        raise WeatherAcquisitionError(
            f"geocoding lookup for location_id {location_id} returned no coordinates")
    returned = payload.get("id")
    if isinstance(returned, bool) or not isinstance(returned, int) or returned != location_id:
        raise WeatherAcquisitionError(
            f"geocoding lookup for location_id {location_id} answered with id {returned!r}; "
            "refusing to use a different place")
    candidate = _candidate(payload, index=0)
    candidate["get_url"] = request_url
    return candidate


def _resolve_location(*, location_id, latitude, longitude, site_name, country_code,
                      altitude, timezone_name):
    """Resolve one explicit place; ambiguous names and missing evidence are blocked."""
    notes = []
    explicit = latitude is not None or longitude is not None
    if location_id is not None and explicit:
        raise WeatherAcquisitionError(
            "pass either location_id or explicit latitude/longitude, not both")
    if country_code and not ISO_COUNTRY.fullmatch(country_code):
        raise WeatherAcquisitionError("country_code must be an uppercase ISO alpha-2 code")
    if timezone_name and not TIMEZONE_NAME.fullmatch(timezone_name):
        raise WeatherAcquisitionError(f"invalid IANA timezone_name {timezone_name!r}")
    if location_id is not None:
        candidate = _geocode_location(location_id)
        if not candidate["country_code"] or not candidate["timezone_name"]:
            raise WeatherAcquisitionError(
                "the geocoded place has no ISO country code or IANA timezone; pass explicit "
                "coordinates with country_code and timezone_name instead")
        if not ISO_COUNTRY.fullmatch(candidate["country_code"]):
            raise WeatherAcquisitionError(
                f"geocoded country code {candidate['country_code']!r} for location_id "
                f"{candidate['location_id']} is not an uppercase ISO alpha-2 code")
        if country_code and candidate["country_code"] != country_code:
            raise WeatherAcquisitionError(
                f"country_code {country_code} contradicts the geocoded ISO code "
                f"{candidate['country_code']} for location_id {candidate['location_id']}")
        if timezone_name and timezone_name != candidate["timezone_name"]:
            notes.append(f"caller timezone {timezone_name} replaces the geocoded zone "
                         f"{candidate['timezone_name']} for location_id {candidate['location_id']}")
        site = {"location_id": candidate["location_id"], "name": site_name or candidate["name"],
                "latitude": candidate["latitude"], "longitude": candidate["longitude"],
                "country_code": candidate["country_code"],
                "timezone_name": timezone_name or candidate["timezone_name"],
                "elevation_m": candidate["elevation_m"], "admin1": candidate["admin1"],
                "evidence": {"coordinates": "Open-Meteo geocoding (GeoNames) location_id "
                                            f"{candidate['location_id']}",
                             "get_url": candidate["get_url"],
                             "timezone": "GeoNames IANA zone" if not timezone_name
                                         else "caller supplied IANA zone"}}
    elif explicit:
        if latitude is None or longitude is None:
            raise WeatherAcquisitionError("explicit coordinates need both latitude and longitude")
        latitude = _finite_number(latitude, "latitude")
        longitude = _finite_number(longitude, "longitude")
        if not -90 <= latitude <= 90:
            raise WeatherAcquisitionError("latitude must lie in -90..90")
        if not -180 <= longitude <= 180:
            raise WeatherAcquisitionError("longitude must lie in -180..180")
        missing = [label for label, value in (("site_name", site_name),
                                              ("country_code", country_code),
                                              ("timezone_name", timezone_name)) if not value]
        if missing:
            raise WeatherAcquisitionError(
                "explicit coordinates require " + ", ".join(missing)
                + "; nothing is inferred from the provider for a manual location")
        site = {"location_id": None, "name": site_name, "latitude": latitude, "longitude": longitude,
                "country_code": country_code, "timezone_name": timezone_name, "elevation_m": None,
                "admin1": "",
                "evidence": {"coordinates": "caller supplied coordinates",
                             "timezone": "caller supplied IANA zone"}}
    else:
        raise WeatherAcquisitionError(
            "pass location_id from search_locations, or explicit latitude/longitude with "
            "site_name, country_code and timezone_name")
    if altitude is not None:
        site["elevation_m"] = _finite_number(altitude, "altitude")
        site["evidence"]["elevation"] = "caller supplied altitude"
    elif site["elevation_m"] is not None:
        site["evidence"]["elevation"] = "GeoNames place elevation from the geocoding candidate"
    site["name"] = _printable_text(site["name"], "site name")
    return site, notes


def _year_hours(year):
    return (366 if calendar.isleap(year) else 365) * 24


def _archive_params(latitude, longitude, start_date, end_date):
    return {"latitude": _number_text(latitude), "longitude": _number_text(longitude),
            "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
            "hourly": ",".join(HOURLY_VARIABLES), "models": PROVIDER_MODEL, "timezone": "GMT",
            "timeformat": "iso8601", "temperature_unit": "celsius"}


def _index_payload(payload):
    """Return (values_by_utc_label, labels) or raise on a provider contract violation."""
    hourly = payload.get("hourly")
    units = payload.get("hourly_units")
    if not isinstance(hourly, dict) or not isinstance(units, dict):
        raise WeatherAcquisitionError("provider payload has no hourly block or unit block")
    if payload.get("timezone") != "GMT" or payload.get("utc_offset_seconds") != 0:
        raise WeatherAcquisitionError(
            "provider did not echo the requested GMT context (timezone=%r, utc_offset_seconds=%r)"
            % (payload.get("timezone"), payload.get("utc_offset_seconds")))
    stamps = hourly.get("time")
    if not isinstance(stamps, list) or not stamps:
        raise WeatherAcquisitionError("provider payload has no hourly time array")
    labels = []
    for item in stamps:
        if not isinstance(item, str):
            raise WeatherAcquisitionError("provider returned a non-string hourly label")
        try:
            stamp = datetime.fromisoformat(item)
        except ValueError:
            raise WeatherAcquisitionError(f"unparsable provider label {item!r}") from None
        if stamp.tzinfo is not None:
            raise WeatherAcquisitionError(f"expected a bare GMT label, got {item!r}")
        labels.append(stamp)
    if len(set(labels)) != len(labels):
        raise WeatherAcquisitionError("provider returned duplicate hourly labels")
    values = {stamp: {} for stamp in labels}
    for name in HOURLY_VARIABLES:
        series = hourly.get(name)
        if not isinstance(series, list) or len(series) != len(labels):
            raise WeatherAcquisitionError(
                f"provider series {name} is missing or has the wrong length")
        unit = units.get(name)
        if unit not in ACCEPTED_UNITS[name]:
            raise WeatherAcquisitionError(
                f"provider unit {unit!r} for {name} is not one of {ACCEPTED_UNITS[name]}")
        for stamp, cell in zip(labels, series):
            values[stamp][name] = cell
    return values, labels


TWILIGHT_GHI_MAX_WM2 = 30.0
TWILIGHT_DHI_EXCESS_MAX_WM2 = 15.0


def _clamp_dhi(ghi: float, dhi: float, *, strict_dhi: bool, allow_rounding: bool = True) -> tuple[float, str | None]:
    """Return (clamped_dhi, reason) where reason is None, 'rounding', 'twilight', or 'reject'."""
    if dhi <= ghi:
        return dhi, None
    excess = dhi - ghi
    if allow_rounding and excess <= 2.0:
        return ghi, "rounding"
    if not strict_dhi and 0.0 < ghi < TWILIGHT_GHI_MAX_WM2 and excess <= TWILIGHT_DHI_EXCESS_MAX_WM2:
        return ghi, "twilight"
    return dhi, "reject"


class _DhiClampStats:
    """Track rounding and twilight DHI->GHI clamps across provider row builders."""

    def __init__(self) -> None:
        self.rounding_count = 0
        self.twilight_count = 0
        self.twilight_max_excess = 0.0
        self.twilight_sample: str | None = None

    def check(self, ghi: float, dhi: float, sample_label: str, *, strict_dhi: bool,
              allow_rounding: bool = True) -> tuple[float, str | None]:
        clamped, reason = _clamp_dhi(ghi, dhi, strict_dhi=strict_dhi, allow_rounding=allow_rounding)
        if reason == "rounding":
            self.rounding_count += 1
        elif reason == "twilight":
            excess = dhi - ghi
            self.twilight_count += 1
            if excess > self.twilight_max_excess:
                self.twilight_max_excess = excess
                self.twilight_sample = f"{sample_label} GHI={ghi:.2f} DHI={dhi:.2f}"
        return clamped, reason

    def append_warnings(self, warnings: list[str]) -> None:
        if self.rounding_count:
            warnings.append(
                f"Clamped tiny rounding excess (<= 2 W/m2) where DHI > GHI on {self.rounding_count} hours."
            )
        if self.twilight_count:
            warnings.append(
                f"Clamped {self.twilight_count} low-irradiance twilight hour(s) where DHI slightly exceeded "
                f"GHI (0 < GHI < {TWILIGHT_GHI_MAX_WM2:g} W/m2, max excess {self.twilight_max_excess:.2f} W/m2, "
                f"e.g. {self.twilight_sample}); pass strict_dhi=true to reject instead."
            )


def _build_rows(values, *, year, utc_start, strict_dhi=False):
    """Build local-standard interval-start rows; every gap is a block, never a fill."""
    hours = _year_hours(year)
    local_start = datetime(year, 1, 1)
    missing, missing_count = [], 0
    inverted, negative, extreme = [], [], []
    dhi_stats = _DhiClampStats()
    rows = []
    for index in range(hours):
        local = local_start + timedelta(hours=index)
        radiation_label = utc_start + timedelta(hours=index + 1)
        temperature_start = utc_start + timedelta(hours=index)
        temperature_end = temperature_start + timedelta(hours=1)
        taken = {}
        broken = False
        for label, name in ((radiation_label, "shortwave_radiation"),
                            (radiation_label, "diffuse_radiation"),
                            (temperature_start, "temperature_2m"),
                            (temperature_end, "temperature_2m")):
            cell = values.get(label)
            value = None if cell is None else cell.get(name)
            if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(float(value)):
                broken = True
                missing_count += 1
                if len(missing) < 12:
                    missing.append(f"{name}@{label.isoformat()}Z"
                                   + (" (unpublished or absent)" if value is None
                                      else " (not a finite number)"))
            else:
                taken[(label, name)] = float(value)
        if broken:
            continue
        ghi = taken[(radiation_label, "shortwave_radiation")]
        dhi = taken[(radiation_label, "diffuse_radiation")]
        tamb = (taken[(temperature_start, "temperature_2m")]
                + taken[(temperature_end, "temperature_2m")]) / 2
        if (ghi < 0 or dhi < 0) and len(negative) < 6:
            negative.append(f"{local.isoformat()} GHI={ghi} DHI={dhi}")
        if dhi > ghi:
            dhi, reason = dhi_stats.check(ghi, dhi, local.isoformat(), strict_dhi=strict_dhi, allow_rounding=False)
            if reason == "reject" and len(inverted) < 6:
                inverted.append(f"{local.isoformat()} GHI={ghi} DHI={dhi}")
        if tamb <= -273.15 and len(extreme) < 6:
            extreme.append(f"{local.isoformat()} Tamb={tamb}")
        rows.append((local, ghi, dhi, tamb))
    issues = []
    if missing_count:
        issues.append(f"{missing_count} provider hours are missing or null; nothing is filled, "
                      f"zeroed or interpolated: " + "; ".join(missing))
    if negative:
        issues.append("negative irradiance: " + "; ".join(negative))
    if inverted:
        issues.append("diffuse irradiance exceeds global horizontal irradiance: " + "; ".join(inverted))
    if extreme:
        issues.append("temperature at or below absolute zero: " + "; ".join(extreme))
    if not issues and len(rows) != hours:
        issues.append(f"expected {hours} rows, built {len(rows)}")
    warnings = []
    dhi_stats.append_warnings(warnings)
    if rows and any(row[3] < -60 or row[3] > 60 for row in rows):
        warnings.append("ambient temperatures outside -60..60 deg.C need source review")
    return rows, issues, warnings


def _standard_csv_text(rows, *, site, year, offset_hours, data_source_label=PROVIDER_LABEL):
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=";", lineterminator="\n")
    writer.writerows([
        ["#Weather data hourly data"],
        ["#Site", site["name"]],
        ["#Country", site["country_code"]],
        ["#Data Source", data_source_label],
        ["#Time step", "Hour"],
        ["#Year", year],
        ["#Latitude", _number_text(site["latitude"])],
        ["#Longitude", _number_text(site["longitude"])],
        ["#Altitude", _number_text(site["elevation_m"])],
        ["#Time Zone", _number_text(offset_hours)],
    ])
    writer.writerow(["Year", "Month", "Day", "Hour", *(CSV_COLUMNS)])
    writer.writerow(["", "", "", "", *(CSV_UNITS[name] for name in CSV_COLUMNS)])
    for stamp, ghi, dhi, tamb in rows:
        writer.writerow([stamp.year, stamp.month, stamp.day, stamp.hour,
                         _number_text(ghi), _number_text(dhi), _number_text(tamb)])
    return stream.getvalue()


def _prepare_weather_paths(
    workspace_path: Path,
    *,
    csv_name: str,
    raw_suffix: str,
    dry_run: bool,
    result: dict,
    empty_note: str = "csv_name is empty; supply it before an execution run",
    conflict_suffix: str = "",
) -> tuple[Path, Path, Path] | None:
    if not csv_name:
        if dry_run:
            result["warnings"].append(empty_note)
            result["status"] = "ready"
            return None
        raise WeatherAcquisitionError("csv_name is required for an execution run")
    if not dry_run:
        (workspace_path / "Meteo").mkdir(parents=True, exist_ok=True)
        (workspace_path / "Results").mkdir(parents=True, exist_ok=True)
    elif not workspace_path.is_dir():
        raise WeatherAcquisitionError(
            f"workspace {workspace_path} must be an existing directory for a plan run"
        )
    csv_path = workspace_file(workspace_path, "Meteo", csv_name, ".csv")
    stem = csv_path.stem
    raw_path = workspace_file(workspace_path, "Meteo", stem + raw_suffix, ".json")
    manifest_path = workspace_file(workspace_path, "Results", stem + "_acquisition.json", ".json")
    result["artifacts"] = {"csv": str(csv_path), "raw_json": str(raw_path), "manifest": str(manifest_path)}
    existing = [str(p) for p in (csv_path, raw_path, manifest_path) if p.exists()]
    if existing:
        raise WeatherAcquisitionError("refusing to overwrite existing files: " + ", ".join(existing) + conflict_suffix)
    if dry_run:
        result["status"] = "ready"
        return None
    return csv_path, raw_path, manifest_path


def _fetch_and_save_raw(url: str, params: dict, raw_path: Path, result: dict) -> dict:
    result["stage"] = "request"
    payload, raw_text, request_url = _provider_get(url, params)
    raw_bytes = raw_text.encode("utf-8")
    if len(raw_bytes) > MAX_RESPONSE_BYTES:
        result["stage"] = "raw"
        raise WeatherAcquisitionError("provider response exceeds the raw storage limit")
    result["plan"]["request_url"] = request_url
    result["stage"] = "raw"
    _bounded_write(raw_path, raw_text)
    result["created_files"]["raw_json"] = {"name": raw_path.name, "bytes": len(raw_bytes)}
    result["raw_preserved"] = True
    return payload


def _finalize_weather_csv(
    csv_path: Path,
    rows: list,
    *,
    site: dict,
    year: int,
    offset_hours: int | float,
    label: str,
    result: dict,
    verified_extra: dict | None = None,
) -> None:
    result["stage"] = "write"
    _bounded_write(csv_path, _standard_csv_text(
        rows, site=site, year=year, offset_hours=offset_hours, data_source_label=label,
    ))
    result["created_files"]["csv"] = {"name": csv_path.name, "rows": len(rows)}
    verified = {"rows": len(rows), "dhi_le_ghi": True, "no_missing_hours": True}
    if verified_extra:
        verified.update(verified_extra)
    result["plan"]["verified"] = verified
    result.update(status="prepared", stage="done")


def _write_manifest_checkpoint(manifest_path: Path | None, result: dict, *, dry_run: bool) -> None:
    if not dry_run and manifest_path is not None:
        # New-only provenance on disk, so a blocked or failed execution keeps its reason
        # next to the raw response instead of leaving no trace.
        result["created_files"]["manifest"] = {"name": manifest_path.name}
        try:
            _bounded_write(manifest_path, json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
        except (OSError, ValueError) as error:
            result["created_files"].pop("manifest", None)
            result["issues"].append("manifest write failed: " + str(error))
            if result["status"] == "prepared":
                result.update(status="failed", stage="manifest")


def _download_pvgis_tmy(
    workspace_path: Path,
    *,
    csv_name: str,
    year: int | None,
    site: dict,
    notes: list[str],
    dry_run: bool,
    strict_dhi: bool = False,
) -> dict:
    tmy_year = 2021 if year is None else year
    if type(tmy_year) is not int or not PREPARED_MIN_YEAR <= tmy_year <= MAX_YEAR:
        raise WeatherAcquisitionError(f"year for pvgis_tmy must be an integer in {PREPARED_MIN_YEAR}..{MAX_YEAR}")
    if calendar.isleap(tmy_year):
        raise WeatherAcquisitionError(
            f"PVGIS TMY has 8760 non-leap hours; choose a non-leap label year instead of {tmy_year}"
        )
    offset_hours = standard_utc_offset_hours(site["timezone_name"], tmy_year)
    params = {
        "lat": _number_text(site["latitude"]),
        "lon": _number_text(site["longitude"]),
        "outputformat": "json",
        "usehorizon": 1,
    }
    label = "PVGIS v5.3 TMY (re.jrc.ec.europa.eu/api/v5_3/tmy)"
    result = {
        "status": "blocked", "stage": "plan", "dry_run": dry_run, "year": tmy_year,
        "provider": {"name": "pvgis_tmy", "model": "tmy_v5_3", "endpoints": {"tmy": PVGIS_TMY_URL}},
        "time_basis": "local_standard_interval_start", "method": "pvgis_tmy_cyclic_shift",
        "location": site, "standard_utc_offset_hours": offset_hours,
        "plan": {
            "request_url": PVGIS_TMY_URL + "?" + urllib.parse.urlencode(params),
            "request_params": params,
            "target_rows": 8760,
            "leap_year": False,
            "row_labelling": f"local standard interval start 00:00..23:00 at UTC{offset_hours:+d}",
        },
        "units": dict(CSV_UNITS), "artifacts": {}, "created_files": {},
        "provider_echo": None, "raw_preserved": False, "issues": [],
        "warnings": list(notes) + [
            f"PVGIS v5.3 TMY synthesizes 12 representative months into an 8760-hour typical year labelled as {tmy_year}."
        ],
        "review_required": ["Verify PVGIS TMY coverage and representative years for the target site"],
    }
    manifest_path = None
    try:
        paths = _prepare_weather_paths(
            workspace_path, csv_name=csv_name, raw_suffix="_pvgis_tmy_raw.json", dry_run=dry_run, result=result,
        )
        if paths is None:
            return result
        csv_path, raw_path, manifest_path = paths
        payload = _fetch_and_save_raw(PVGIS_TMY_URL, params, raw_path, result)

        inputs = payload.get("inputs") or {}
        loc_echo = inputs.get("location") or {}
        result["provider_echo"] = {
            "latitude": loc_echo.get("latitude"),
            "longitude": loc_echo.get("longitude"),
            "elevation_m": loc_echo.get("elevation"),
            "meteo_data": inputs.get("meteo_data"),
        }
        if site["elevation_m"] is None:
            elev = loc_echo.get("elevation")
            if isinstance(elev, (int, float)) and not isinstance(elev, bool) and math.isfinite(float(elev)):
                site["elevation_m"] = float(elev)
                site["evidence"]["elevation"] = "PVGIS v5.3 location elevation echo"
            else:
                raise WeatherAcquisitionError("no altitude supplied and PVGIS echoed none")

        result["stage"] = "validation"
        hourly = (payload.get("outputs") or {}).get("tmy_hourly")
        if not isinstance(hourly, list) or len(hourly) != 8760:
            raise WeatherAcquisitionError(f"PVGIS TMY returned {len(hourly) if isinstance(hourly, list) else 'invalid'} rows; expected 8760")

        # Map each UTC row into its 0..8759 hour-of-non-leap-year index, then shift by offset_hours
        base_start = datetime(tmy_year, 1, 1, 0, 0)
        by_local_idx: dict[int, tuple[float, float, float]] = {}
        dhi_stats = _DhiClampStats()
        for row_idx, item in enumerate(hourly):
            if not isinstance(item, dict):
                raise WeatherAcquisitionError(f"PVGIS TMY row {row_idx} is not an object")
            raw_time = str(item.get("time(UTC)") or "")
            try:
                mm = int(raw_time[4:6])
                dd = int(raw_time[6:8])
                hh = int(raw_time[9:11])
                utc_dt = datetime(tmy_year, mm, dd, hh, 0)
            except (ValueError, IndexError) as exc:
                raise WeatherAcquisitionError(f"Invalid PVGIS time(UTC) label {raw_time!r}") from exc
            utc_index = int((utc_dt - base_start).total_seconds() // 3600)
            local_index = (utc_index + offset_hours) % 8760
            ghi = _finite_number(item.get("G(h)"), f"PVGIS row {row_idx} G(h)")
            dhi = _finite_number(item.get("Gd(h)"), f"PVGIS row {row_idx} Gd(h)")
            tamb = _finite_number(item.get("T2m"), f"PVGIS row {row_idx} T2m")
            if ghi < 0 or dhi < 0:
                raise WeatherAcquisitionError(f"Negative irradiance in PVGIS row {row_idx}")
            if dhi > ghi:
                dhi, reason = dhi_stats.check(ghi, dhi, raw_time, strict_dhi=strict_dhi)
                if reason == "reject":
                    raise WeatherAcquisitionError(f"PVGIS row {row_idx} has DHI ({dhi}) > GHI ({ghi})")
            by_local_idx[local_index] = (ghi, dhi, tamb)

        if len(by_local_idx) != 8760:
            raise WeatherAcquisitionError(f"PVGIS TMY mapped {len(by_local_idx)} unique local hours; expected 8760")
        dhi_stats.append_warnings(result["warnings"])
        rows = [(base_start + timedelta(hours=h), *by_local_idx[h]) for h in range(8760)]

        _finalize_weather_csv(
            csv_path, rows, site=site, year=tmy_year, offset_hours=offset_hours, label=label, result=result,
        )
    except WeatherAcquisitionError as error:
        result.update(status="failed" if result["stage"] == "request" else "blocked")
        result["issues"].append(str(error))
    except (OSError, ValueError) as error:
        result.update(status="failed")
        result["issues"].append(str(error))
    finally:
        _write_manifest_checkpoint(manifest_path, result, dry_run=dry_run)
    return result


def _download_nasa_power(
    workspace_path: Path,
    *,
    csv_name: str,
    year: int | None,
    site: dict,
    notes: list[str],
    dry_run: bool,
    strict_dhi: bool = False,
) -> dict:
    if type(year) is not int or not 2001 <= year <= MAX_YEAR:
        raise WeatherAcquisitionError(f"year for nasa_power must be an integer in 2001..{MAX_YEAR}")
    offset_hours = standard_utc_offset_hours(site["timezone_name"], year)
    hours = _year_hours(year)
    utc_start = datetime(year, 1, 1) - timedelta(hours=offset_hours)
    utc_end = utc_start + timedelta(hours=hours - 1)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    if utc_end + timedelta(hours=1) > now_utc:
        raise WeatherAcquisitionError(
            f"the requested year {year} is not complete yet: the required UTC window ends at "
            f"{(utc_end + timedelta(hours=1)).isoformat()}Z while current UTC time is "
            f"{now_utc.replace(microsecond=0).isoformat()}Z; choose a completed historical year"
        )
    params = {
        "parameters": "ALLSKY_SFC_SW_DWN,ALLSKY_SFC_SW_DIFF,T2M",
        "community": "RE",
        "longitude": _number_text(site["longitude"]),
        "latitude": _number_text(site["latitude"]),
        "start": utc_start.strftime("%Y%m%d"),
        "end": utc_end.strftime("%Y%m%d"),
        "format": "JSON",
        "time-standard": "UTC",
    }
    label = "NASA POWER Hourly RE (power.larc.nasa.gov/api/temporal/hourly/point)"
    result = {
        "status": "blocked", "stage": "plan", "dry_run": dry_run, "year": year,
        "provider": {"name": "nasa_power", "model": "power_re_hourly", "endpoints": {"hourly": NASA_POWER_URL}},
        "time_basis": "local_standard_interval_start", "method": "utc_interval_shift",
        "location": site, "standard_utc_offset_hours": offset_hours,
        "plan": {
            "request_url": NASA_POWER_URL + "?" + urllib.parse.urlencode(params),
            "request_params": params,
            "target_rows": hours,
            "leap_year": calendar.isleap(year),
            "row_labelling": f"local standard interval start 00:00..23:00 at UTC{offset_hours:+d}",
        },
        "units": dict(CSV_UNITS), "artifacts": {}, "created_files": {},
        "provider_echo": None, "raw_preserved": False, "issues": [],
        "warnings": list(notes) + [
            "NASA POWER hourly irradiance is satellite-derived (CERES/MERRA-2); review against site measurements if available."
        ],
        "review_required": ["Verify NASA POWER grid resolution and satellite diffuse irradiance"],
    }
    manifest_path = None
    try:
        paths = _prepare_weather_paths(
            workspace_path, csv_name=csv_name, raw_suffix="_nasapower_raw.json", dry_run=dry_run, result=result,
        )
        if paths is None:
            return result
        csv_path, raw_path, manifest_path = paths
        payload = _fetch_and_save_raw(NASA_POWER_URL, params, raw_path, result)

        coords = (payload.get("geometry") or {}).get("coordinates") or []
        elev_echo = coords[2] if len(coords) >= 3 else None
        result["provider_echo"] = {
            "longitude": coords[0] if len(coords) >= 1 else None,
            "latitude": coords[1] if len(coords) >= 2 else None,
            "elevation_m": elev_echo,
        }
        if site["elevation_m"] is None:
            if isinstance(elev_echo, (int, float)) and not isinstance(elev_echo, bool) and math.isfinite(float(elev_echo)):
                site["elevation_m"] = float(elev_echo)
                site["evidence"]["elevation"] = "NASA POWER geometry elevation echo"
            else:
                raise WeatherAcquisitionError("no altitude supplied and NASA POWER echoed none")

        result["stage"] = "validation"
        param_map = ((payload.get("properties") or {}).get("parameter") or {})
        ghi_map = param_map.get("ALLSKY_SFC_SW_DWN")
        dhi_map = param_map.get("ALLSKY_SFC_SW_DIFF")
        t2m_map = param_map.get("T2M")
        if not all(isinstance(m, dict) for m in (ghi_map, dhi_map, t2m_map)):
            raise WeatherAcquisitionError("NASA POWER response missing required parameter maps")

        rows = []
        dhi_stats = _DhiClampStats()
        for step in range(hours):
            utc_dt = utc_start + timedelta(hours=step)
            local_dt = datetime(year, 1, 1) + timedelta(hours=step)
            key = utc_dt.strftime("%Y%m%d%H")
            if key not in ghi_map or key not in dhi_map or key not in t2m_map:
                raise WeatherAcquisitionError(f"NASA POWER missing UTC hour {key}")
            ghi = _finite_number(ghi_map[key], f"NASA POWER GHI {key}")
            dhi = _finite_number(dhi_map[key], f"NASA POWER DHI {key}")
            tamb = _finite_number(t2m_map[key], f"NASA POWER T2M {key}")
            if ghi <= -900.0 or dhi <= -900.0 or tamb <= -900.0:
                raise WeatherAcquisitionError(f"NASA POWER sentinel missing value at UTC hour {key}")
            if ghi < 0 or dhi < 0:
                raise WeatherAcquisitionError(f"NASA POWER negative irradiance at UTC hour {key}")
            if dhi > ghi:
                dhi, reason = dhi_stats.check(ghi, dhi, key, strict_dhi=strict_dhi)
                if reason == "reject":
                    raise WeatherAcquisitionError(f"NASA POWER DHI ({dhi}) > GHI ({ghi}) at UTC hour {key}")
            rows.append((local_dt, ghi, dhi, tamb))

        dhi_stats.append_warnings(result["warnings"])
        _finalize_weather_csv(
            csv_path, rows, site=site, year=year, offset_hours=offset_hours, label=label, result=result,
        )
    except WeatherAcquisitionError as error:
        result.update(status="failed" if result["stage"] == "request" else "blocked")
        result["issues"].append(str(error))
    except (OSError, ValueError) as error:
        result.update(status="failed")
        result["issues"].append(str(error))
    finally:
        _write_manifest_checkpoint(manifest_path, result, dry_run=dry_run)
    return result


def download_weather(workspace, *, csv_name="", year=None, location_id=None, latitude=None,
                     longitude=None, site_name="", country_code="", altitude=None,
                     timezone_name="", temperature_method="endpoint_mean",
                     provider="open_meteo_era5", strict_dhi=False, dry_run=True):
    """Plan or execute weather acquisition as a PVsyst standard hourly CSV.

    Supports ``provider="open_meteo_era5"`` (default), ``"pvgis_tmy"`` (PVGIS v5.3 TMY),
    and ``"nasa_power"`` (NASA POWER hourly RE).
    """
    if provider not in PROVIDERS:
        return {
            "status": "blocked", "stage": "preflight", "dry_run": dry_run, "year": year,
            "provider": {"name": str(provider)}, "issues": [f"provider must be one of {list(PROVIDERS)}"],
            "warnings": [], "artifacts": {}, "created_files": {},
        }
    if type(strict_dhi) is not bool:
        return {
            "status": "blocked", "stage": "preflight", "dry_run": dry_run, "year": year,
            "provider": {"name": provider}, "issues": ["strict_dhi must be a strict boolean"],
            "warnings": [], "artifacts": {}, "created_files": {},
        }
    if provider in ("pvgis_tmy", "nasa_power"):
        if type(dry_run) is not bool:
            return {
                "status": "blocked", "stage": "preflight", "dry_run": dry_run, "year": year,
                "provider": {"name": provider}, "issues": ["dry_run must be a strict boolean"],
                "warnings": [], "artifacts": {}, "created_files": {},
            }
        try:
            site, notes = _resolve_location(
                location_id=location_id, latitude=latitude, longitude=longitude,
                site_name=site_name, country_code=country_code, altitude=altitude,
                timezone_name=timezone_name,
            )
            if provider == "pvgis_tmy":
                return _download_pvgis_tmy(
                    Path(workspace), csv_name=csv_name, year=year, site=site, notes=notes,
                    dry_run=dry_run, strict_dhi=strict_dhi,
                )
            return _download_nasa_power(
                Path(workspace), csv_name=csv_name, year=year, site=site, notes=notes,
                dry_run=dry_run, strict_dhi=strict_dhi,
            )
        except WeatherAcquisitionError as error:
            return {
                "status": "blocked", "stage": "preflight", "dry_run": dry_run, "year": year,
                "provider": {"name": provider}, "issues": [str(error)],
                "warnings": [], "artifacts": {}, "created_files": {},
            }
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run, "year": year,
              "provider": {"name": "open-meteo", "model": PROVIDER_MODEL,
                           "endpoints": {"geocoding_search": GEOCODING_SEARCH_URL,
                                         "geocoding_get": GEOCODING_GET_URL,
                                         "archive": ARCHIVE_URL}},
              "time_basis": "local_standard_interval_start", "method": temperature_method,
              "location": None, "standard_utc_offset_hours": None, "plan": None,
              "units": dict(CSV_UNITS), "artifacts": {}, "created_files": {},
              "provider_echo": None, "raw_preserved": False, "issues": [], "warnings": [],
              "review_required": []}
    manifest_path = None
    try:
        if type(dry_run) is not bool:
            raise WeatherAcquisitionError("dry_run must be a strict boolean")
        if type(year) is not int or not MIN_YEAR <= year <= MAX_YEAR:
            raise WeatherAcquisitionError(
                f"year must be an integer in {MIN_YEAR}..{MAX_YEAR} (ERA5 starts in 1940)")
        if year < PREPARED_MIN_YEAR:
            raise WeatherAcquisitionError(
                f"year {year} is in the ERA5 archive, but prepare_weather and review_weather accept "
                f"only {PREPARED_MIN_YEAR}..{MAX_YEAR} because native PVsyst dates carry two-digit "
                "years; a CSV for this year could never be prepared, so it is blocked before any "
                "request")
        if temperature_method not in TEMPERATURE_METHODS:
            raise WeatherAcquisitionError(
                f"temperature_method must be one of {list(TEMPERATURE_METHODS)}")
        workspace_path = Path(workspace)
        site, notes = _resolve_location(location_id=location_id, latitude=latitude,
                                        longitude=longitude, site_name=site_name,
                                        country_code=country_code, altitude=altitude,
                                        timezone_name=timezone_name)
        offset_hours = standard_utc_offset_hours(site["timezone_name"], year)
        hours = _year_hours(year)
        utc_start = datetime(year, 1, 1) - timedelta(hours=offset_hours)
        utc_end = utc_start + timedelta(hours=hours)
        # The archive accepts end dates up to today, but the last few days are still null.
        published_through = (datetime.now(timezone.utc).date()
                             - timedelta(days=PUBLICATION_LAG_DAYS))
        if utc_end.date() > published_through:
            raise WeatherAcquisitionError(
                f"the requested year {year} is not fully published by the provider: the local "
                f"standard year ends at UTC {utc_end.date().isoformat()} while ERA5 runs roughly "
                f"{PUBLICATION_LAG_DAYS} days behind, so only dates through about "
                f"{published_through.isoformat()} are complete. Unpublished hours come back as "
                "null, which this module never fills; choose a year that is already complete")
        params = _archive_params(site["latitude"], site["longitude"], utc_start.date(),
                                 utc_end.date())
        result.update(stage="plan", location=site, standard_utc_offset_hours=offset_hours,
                      plan={"request_url": ARCHIVE_URL + "?" + urllib.parse.urlencode(params),
                            "request_params": params,
                            "utc_interval_range": [utc_start.isoformat() + "Z",
                                                   utc_end.isoformat() + "Z"],
                            "request_start_date": params["start_date"],
                            "request_end_date": params["end_date"],
                            "target_rows": hours,
                            "temperature_instants": hours + 1,
                            "leap_year": calendar.isleap(year),
                            "row_labelling": "local standard interval start 00:00..23:00 at "
                                             f"UTC{offset_hours:+d}"})
        result["warnings"].extend(notes)
        result["warnings"].append(TEMPERATURE_WARNING)
        result["warnings"].append(GRID_WARNING)
        if year >= published_through.year:
            result["warnings"].append(
                f"{year} touches the current provider year ({published_through.isoformat()}); ERA5 "
                "runs about five days behind, so recent hours may still be null and would block "
                "the run instead of being filled")
        result["review_required"].append(
            "hourly Tamb uses the disclosed endpoint_mean approximation")
        if site["elevation_m"] is None:
            result["warnings"].append(
                "no altitude is available yet; execution reuses the Open-Meteo grid elevation "
                "echo and records it as provider evidence")
        paths = _prepare_weather_paths(
            workspace_path,
            csv_name=csv_name,
            raw_suffix="_openmeteo_raw.json",
            dry_run=dry_run,
            result=result,
            empty_note="csv_name is empty; supply it before an execution run to name the new CSV",
            conflict_suffix="; choose new names",
        )
        if paths is None:
            return result
        csv_path, raw_path, manifest_path = paths
        payload = _fetch_and_save_raw(ARCHIVE_URL, params, raw_path, result)
        result["provider_echo"] = {
            "grid_latitude": payload.get("latitude"), "grid_longitude": payload.get("longitude"),
            "grid_elevation_m": payload.get("elevation"), "timezone": payload.get("timezone"),
            "utc_offset_seconds": payload.get("utc_offset_seconds"),
            "hourly_units": payload.get("hourly_units"),
            "generationtime_ms": payload.get("generationtime_ms")}
        if site["elevation_m"] is None:
            echo_elevation = payload.get("elevation")
            if isinstance(echo_elevation, (int, float)) and not isinstance(echo_elevation, bool) \
                    and math.isfinite(float(echo_elevation)):
                site["elevation_m"] = float(echo_elevation)
                site["evidence"]["elevation"] = "Open-Meteo ERA5 grid elevation (DEM) echo"
                result["warnings"].append(
                    "altitude was not supplied; the CSV uses the Open-Meteo grid elevation echo "
                    f"{site['elevation_m']} m, a grid DEM value rather than a surveyed site "
                    "altitude")
            else:
                result.update(status="blocked", stage="raw")
                result["issues"].append("no altitude supplied and the provider echoed none")
                return result

        result["stage"] = "validation"
        values, labels = _index_payload(payload)
        rows, issues, warnings = _build_rows(values, year=year, utc_start=utc_start, strict_dhi=strict_dhi)
        result["warnings"].extend(warnings)
        result["plan"]["provider_labels"] = len(labels)
        if issues:
            result.update(status="blocked", stage="validation")
            result["issues"].extend(issues)
            return result

        _finalize_weather_csv(
            csv_path,
            rows,
            site=site,
            year=year,
            offset_hours=offset_hours,
            label=PROVIDER_LABEL,
            result=result,
            verified_extra={"provider_labels": len(labels), "labels_match_local_year": True},
        )
    except WeatherAcquisitionError as error:
        result.update(status="failed" if result["stage"] == "request" else "blocked")
        result["issues"].append(str(error))
    except (OSError, ValueError) as error:
        result.update(status="failed")
        result["issues"].append(str(error))
    finally:
        _write_manifest_checkpoint(manifest_path, result, dry_run=dry_run)
    return result

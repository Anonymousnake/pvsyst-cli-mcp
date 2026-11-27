"""Bounded PVGIS v5.3 printhorizon fetcher and PVsyst horizon profile converter."""
from __future__ import annotations

import http.client
import json
import math
from pathlib import Path
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from pvsyst_auto.components.store import FIELD
from pvsyst_auto.native.load import _horizon_body, _horizon_check_name, _horizon_check_points
from pvsyst_auto.validation import finite_number

PVGIS_HORIZON_URL = "https://re.jrc.ec.europa.eu/api/v5_3/printhorizon"
ALLOWED_HORIZON_HOSTS = frozenset({"re.jrc.ec.europa.eu"})
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_DEADLINE_SECONDS = 60
MAX_RESPONSE_BYTES = 1 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 16 * 1024
HORIZON_MIN_POINTS = 4
HORIZON_MAX_POINTS = 100
_HORIZON_SPEC_KEYS = frozenset(("mode", "points", "profile_name", "latitude", "longitude", "timeout_seconds"))


class HorizonFetchError(RuntimeError):
    """Transport, provider, or payload failure while retrieving a PVGIS horizon profile."""


def _clean_zero(value: float) -> float:
    return 0.0 if value == 0.0 else value


def _q1(value: float) -> float:
    rounded = round(float(value), 1)
    return 0.0 if rounded == 0.0 else rounded


def _num_text(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _read_bounded(response: Any, *, max_bytes: int, deadline: float) -> bytes:
    headers = getattr(response, "headers", None)
    declared = headers.get("Content-Length") if headers is not None else None
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise HorizonFetchError(
                    f"PVGIS horizon response of {declared} bytes exceeds {max_bytes}-byte limit"
                )
        except (ValueError, TypeError):
            pass
    read = response.read1 if isinstance(response, http.client.HTTPResponse) else response.read
    chunks: list[bytes] = []
    total = 0
    while chunk := read(65536):
        total += len(chunk)
        if total > max_bytes:
            raise HorizonFetchError(f"PVGIS horizon response exceeds {max_bytes}-byte limit")
        chunks.append(chunk)
        if time.monotonic() > deadline:
            raise HorizonFetchError("PVGIS horizon response exceeded request deadline")
    return b"".join(chunks)


def _error_reason(error: urllib.error.HTTPError) -> str:
    try:
        body = error.read(MAX_ERROR_BODY_BYTES)
    except Exception:
        return "no readable error body"
    if not body:
        return "empty error body"
    text = body.decode("utf-8", "replace").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        for key in ("message", "reason", "error"):
            if isinstance(payload.get(key), str) and payload[key].strip():
                return payload[key].strip()[:200]
    return text[:200]


def _pvgis_horizon_get(
    url: str,
    params: dict[str, Any],
    *,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[dict[str, Any], str]:
    """GET allow-listed PVGIS horizon endpoint with strict timeout and byte caps."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc
    if parsed.scheme != "https" or host not in ALLOWED_HORIZON_HOSTS:
        raise HorizonFetchError(f"Refusing to call non-PVGIS host {url!r}")
    request_url = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        request_url,
        headers={"User-Agent": "pvsyst-auto-horizon/1.0 (stdlib urllib)"},
    )
    deadline = time.monotonic() + max(float(timeout), REQUEST_DEADLINE_SECONDS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = _read_bounded(response, max_bytes=max_bytes, deadline=deadline)
    except urllib.error.HTTPError as exc:
        raise HorizonFetchError(f"HTTP {exc.code} from {host}: {_error_reason(exc)}") from None
    except urllib.error.URLError as exc:
        raise HorizonFetchError(f"Transport failure for {host}: {exc.reason}") from None
    except TimeoutError:
        raise HorizonFetchError(f"No response from {host} within {timeout} seconds") from None
    except (http.client.HTTPException, OSError) as exc:
        raise HorizonFetchError(f"Transport failure for {host}: {type(exc).__name__}: {exc}") from None

    try:
        raw_text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HorizonFetchError(f"Invalid UTF-8 from {host} at byte {exc.start}") from None
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise HorizonFetchError(f"Invalid JSON from {host}: {exc.msg} at position {exc.pos}") from None
    if not isinstance(payload, dict):
        raise HorizonFetchError(f"Expected a JSON object from {host}")
    if payload.get("error") is True:
        raise HorizonFetchError(f"PVGIS error from {host}: {payload.get('reason', 'unknown error')}")
    return payload, request_url


def pvgis_to_pvsyst_azimuth(az_deg: float, latitude: float) -> float:
    """Convert PVGIS azimuth (0=South, +90=West, -90=East) into PVsyst hemisphere azimuth."""
    az = float(az_deg)
    lat = float(latitude)
    raw = az if lat >= 0.0 else (180.0 - az)
    wrapped = ((raw + 180.0) % 360.0) - 180.0
    if abs(wrapped + 180.0) < 1e-9 and raw > 0.0:
        wrapped = 180.0
    return 0.0 if abs(wrapped) < 1e-9 else wrapped


def _downsample_points(points: list[list[float]], limit: int = HORIZON_MAX_POINTS) -> list[list[float]]:
    n = len(points)
    if n <= limit:
        return points
    keep = {0, n - 1}
    peaks = [
        i for i in range(1, n - 1)
        if points[i][1] >= points[i - 1][1]
        and points[i][1] >= points[i + 1][1]
        and points[i][1] > min(points[i - 1][1], points[i + 1][1])
    ]
    if len(peaks) >= limit - 2:
        ranked = sorted(peaks, key=lambda idx: (-points[idx][1], idx))[: limit - 2]
        keep.update(ranked)
    else:
        keep.update(peaks)
        remaining = [i for i in range(1, n - 1) if i not in keep]
        need = limit - len(keep)
        if need > 0 and remaining:
            if len(remaining) <= need:
                keep.update(remaining)
            else:
                for slot in range(need):
                    pos = int(round(slot * (len(remaining) - 1) / max(1, need - 1)))
                    keep.add(remaining[pos])
    return [points[i] for i in sorted(keep)]


def convert_pvgis_horizon_points(raw_profile: list[dict[str, Any]], *, latitude: float) -> list[list[float]]:
    """Convert PVGIS outputs.horizon_profile entries into monotonic 0.1-deg [azimuth, height] points."""
    if not isinstance(raw_profile, list) or not raw_profile:
        raise HorizonFetchError("PVGIS horizon_profile must be a non-empty list")
    lat = finite_number(latitude, "latitude", minimum=-90.0, maximum=90.0)
    by_az: dict[float, float] = {}
    for idx, item in enumerate(raw_profile):
        if not isinstance(item, dict):
            raise HorizonFetchError(f"PVGIS horizon_profile[{idx}] must be an object")
        raw_az = item.get("A") if "A" in item else item.get("A_hor")
        raw_h = item.get("H_hor")
        if raw_az is None or raw_h is None:
            raise HorizonFetchError(f"PVGIS horizon_profile[{idx}] missing A or H_hor")
        try:
            az_val = finite_number(raw_az, f"horizon_profile[{idx}].A")
            h_val = finite_number(raw_h, f"horizon_profile[{idx}].H_hor", maximum=90.0)
        except ValueError as exc:
            raise HorizonFetchError(str(exc)) from None
        # Small negative elevation values can appear in DEM lookups over sea/valleys; clamp to 0.
        clamped_h = _q1(max(0.0, h_val))
        pvsyst_az = _q1(pvgis_to_pvsyst_azimuth(az_val, lat))
        if pvsyst_az < -180.0:
            pvsyst_az = -180.0
        elif pvsyst_az > 180.0:
            pvsyst_az = 180.0
        prev = by_az.get(pvsyst_az)
        by_az[pvsyst_az] = clamped_h if prev is None else max(prev, clamped_h)

    if -180.0 in by_az and 180.0 not in by_az:
        by_az[180.0] = by_az[-180.0]
    elif 180.0 in by_az and -180.0 not in by_az:
        by_az[-180.0] = by_az[180.0]

    ordered = [[_clean_zero(az), _clean_zero(by_az[az])] for az in sorted(by_az)]
    if len(ordered) < HORIZON_MIN_POINTS:
        raise HorizonFetchError(
            f"PVGIS horizon profile produced {len(ordered)} points; need at least {HORIZON_MIN_POINTS}"
        )
    return _downsample_points(ordered, HORIZON_MAX_POINTS)


def _default_pvgis_profile_name(latitude: float, longitude: float) -> str:
    label = f"PVGIS Horizon ({latitude:.4f}, {longitude:.4f})"
    while len(label.encode("utf-8")) > 80:
        label = label[:-1]
    return label


def fetch_pvgis_horizon(
    latitude: float,
    longitude: float,
    *,
    profile_name: str | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Fetch far-horizon profile from PVGIS v5.3 printhorizon and normalize for pvHorizon."""
    lat = finite_number(latitude, "latitude", minimum=-90.0, maximum=90.0)
    lon = finite_number(longitude, "longitude", minimum=-180.0, maximum=180.0)
    req_timeout = finite_number(timeout, "timeout", minimum=1.0, maximum=60.0)
    if type(max_bytes) is not int or not 1024 <= max_bytes <= 10 * 1024 * 1024:
        raise ValueError("max_bytes must be an integer in 1024..10485760")
    resolved_name = _horizon_check_name(
        profile_name if profile_name is not None else _default_pvgis_profile_name(lat, lon)
    )
    params = {"lat": _num_text(lat), "lon": _num_text(lon), "outputformat": "json"}
    payload, request_url = _pvgis_horizon_get(
        PVGIS_HORIZON_URL, params, timeout=req_timeout, max_bytes=max_bytes,
    )
    outputs = payload.get("outputs")
    if not isinstance(outputs, dict) or "horizon_profile" not in outputs:
        raise HorizonFetchError("PVGIS printhorizon response missing outputs.horizon_profile")
    points = convert_pvgis_horizon_points(outputs["horizon_profile"], latitude=lat)
    heights = [pt[1] for pt in points]
    if not any(h > 0.0 for h in heights):
        return {
            "mode": "none",
            "is_flat": True,
            "profile_name": None,
            "points": [],
            "point_count": 0,
            "max_elevation_deg": 0.0,
            "mean_elevation_deg": 0.0,
            "source": "pvgis_v5_3",
            "latitude": lat,
            "longitude": lon,
            "request_url": request_url,
            "warnings": ["PVGIS returned an all-zero flat horizon; no PVObject_Horizon block was written."],
        }
    _horizon_check_points(points)
    return {
        "mode": "profile",
        "is_flat": False,
        "profile_name": resolved_name,
        "points": points,
        "point_count": len(points),
        "max_elevation_deg": round(max(heights), 1),
        "mean_elevation_deg": round(sum(heights) / len(heights), 2),
        "source": "pvgis_v5_3",
        "latitude": lat,
        "longitude": lon,
        "request_url": request_url,
        "warnings": [],
    }


def read_site_coordinates(site_path: Path) -> tuple[float, float] | None:
    """Safely read (latitude, longitude) from a workspace .SIT file without raising."""
    try:
        if not site_path.is_file():
            return None
        text = site_path.read_bytes().decode("utf-8-sig", errors="replace")
    except OSError:
        return None
    lat = lon = None
    for line in text.splitlines():
        if m := FIELD.fullmatch(line.strip()):
            if m[1] == "Latitude":
                try:
                    lat = float(m[2])
                except ValueError:
                    return None
            elif m[1] == "Longitude":
                try:
                    lon = float(m[2])
                except ValueError:
                    return None
    if lat is None or lon is None or not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def normalize_horizon_spec(raw: Any) -> dict[str, Any]:
    """Validate and normalize an explicit horizon request dict without network I/O."""
    if not isinstance(raw, dict):
        raise ValueError("horizon configuration must be an object")
    if extra := sorted(set(raw) - _HORIZON_SPEC_KEYS):
        raise ValueError(f"Unknown horizon field(s): {', '.join(extra)}")
    mode = raw.get("mode", "points")
    if mode not in ("none", "points", "pvgis"):
        raise ValueError("horizon.mode must be 'none', 'points', or 'pvgis'")
    if mode == "none":
        if set(raw) - {"mode"}:
            raise ValueError("horizon.mode='none' accepts no additional fields")
        return {"mode": "none", "source": "none", "profile_name": None, "points": []}
    if mode == "points":
        for bad in ("latitude", "longitude", "timeout_seconds"):
            if bad in raw:
                raise ValueError(f"horizon.{bad} is only valid for mode='pvgis'")
        raw_points = raw.get("points")
        if raw_points is None:
            raise ValueError("horizon.mode='points' requires points")
        checked = _horizon_check_points(raw_points)
        name = _horizon_check_name(raw.get("profile_name", "Custom Horizon"))
        return {
            "mode": "profile",
            "source": "explicit_points",
            "profile_name": name,
            "points": [[x, y] for x, y in checked],
        }
    if "points" in raw:
        raise ValueError("horizon.points is only valid for mode='points'")
    has_lat, has_lon = "latitude" in raw, "longitude" in raw
    if has_lat != has_lon:
        raise ValueError("horizon.latitude and horizon.longitude must be supplied together")
    lat = finite_number(raw["latitude"], "horizon.latitude", minimum=-90.0, maximum=90.0) if has_lat else None
    lon = finite_number(raw["longitude"], "horizon.longitude", minimum=-180.0, maximum=180.0) if has_lon else None
    profile_name = _horizon_check_name(raw["profile_name"]) if raw.get("profile_name") is not None else None
    timeout = (
        finite_number(raw["timeout_seconds"], "horizon.timeout_seconds", minimum=1.0, maximum=60.0)
        if raw.get("timeout_seconds") is not None else float(REQUEST_TIMEOUT_SECONDS)
    )
    return {
        "mode": "pvgis",
        "source": "pvgis_v5_3",
        "latitude": lat,
        "longitude": lon,
        "profile_name": profile_name,
        "timeout_seconds": timeout,
    }


def build_pvgis_horizon_plan(
    latitude: float | None,
    longitude: float | None,
    *,
    profile_name: str | None = None,
    coordinate_source: str = "explicit",
) -> dict[str, Any]:
    """Build a zero-network dry-run preview plan for PVGIS printhorizon acquisition."""
    request_url = None
    if latitude is not None and longitude is not None:
        params = {"lat": _num_text(latitude), "lon": _num_text(longitude), "outputformat": "json"}
        request_url = PVGIS_HORIZON_URL + "?" + urllib.parse.urlencode(params)
    return {
        "mode": "pvgis",
        "source": "pvgis_v5_3",
        "endpoint": PVGIS_HORIZON_URL,
        "coordinate_source": coordinate_source,
        "latitude": latitude,
        "longitude": longitude,
        "profile_name": profile_name or (
            _default_pvgis_profile_name(latitude, longitude)
            if latitude is not None and longitude is not None else None
        ),
        "request_url": request_url,
    }


def resolve_design_horizon(
    *,
    workspace: Path,
    horizon_spec: dict[str, Any] | None,
    weather_spec: dict[str, Any],
    sit_name: str,
    acquired_location: dict[str, Any] | None = None,
    dry_run: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve optional horizon config for design flows; zero network calls unless opt-in and live."""
    fetch_flag = bool(weather_spec.get("fetch_horizon"))
    norm = normalize_horizon_spec(horizon_spec) if horizon_spec is not None else None
    if norm is None and not fetch_flag:
        return None, None
    if norm is not None and norm["mode"] == "none":
        return norm, norm
    if norm is not None and norm["mode"] == "profile":
        return norm, norm

    lat = lon = None
    coord_source = "unresolved"
    if norm is not None and norm.get("latitude") is not None:
        lat, lon, coord_source = float(norm["latitude"]), float(norm["longitude"]), "horizon"
    elif isinstance(acquired_location, dict) and acquired_location.get("latitude") is not None and acquired_location.get("longitude") is not None:
        lat, lon, coord_source = float(acquired_location["latitude"]), float(acquired_location["longitude"]), "weather_acquisition"
    elif weather_spec.get("latitude") is not None and weather_spec.get("longitude") is not None:
        lat, lon, coord_source = float(weather_spec["latitude"]), float(weather_spec["longitude"]), "weather"
    elif sit_name:
        if (coords := read_site_coordinates(workspace / "Sites" / sit_name)) is not None:
            lat, lon, coord_source = coords[0], coords[1], "site_file"

    profile_name = norm.get("profile_name") if norm is not None else None
    timeout = float(norm.get("timeout_seconds") or REQUEST_TIMEOUT_SECONDS) if norm is not None else float(REQUEST_TIMEOUT_SECONDS)
    if dry_run:
        return None, build_pvgis_horizon_plan(lat, lon, profile_name=profile_name, coordinate_source=coord_source)
    if lat is None or lon is None:
        raise HorizonFetchError("Could not resolve site latitude/longitude for PVGIS horizon fetch")
    fetched = fetch_pvgis_horizon(lat, lon, profile_name=profile_name, timeout=timeout)
    return (fetched if fetched["mode"] == "profile" else {"mode": "none"}), fetched


def render_horizon_lines(horizon: dict[str, Any], *, indent: str = "  ") -> list[str]:
    """Render PVObject_Horizon=pvHorizon lines (without trailing EOLs) for synthesize.py."""
    if not isinstance(horizon, dict):
        raise ValueError("horizon must be an object")
    if horizon.get("mode") == "points":
        norm = normalize_horizon_spec(horizon)
        name, pts = norm["profile_name"], norm["points"]
    elif horizon.get("mode") == "profile":
        pts = _horizon_check_points(horizon.get("points"))
        name = _horizon_check_name(horizon.get("profile_name") or "Custom Horizon")
        pts = [[x, y] for x, y in pts]
    else:
        raise ValueError("render_horizon_lines requires horizon mode 'profile' or 'points'")
    raw = _horizon_body(indent, name, pts, "\n")
    return [ln.rstrip("\n") for ln in raw]

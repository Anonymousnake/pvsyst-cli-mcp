"""Read-only weather review backend: prepared CSV -> native result, plus provider evidence.

This module answers one question without touching the CLI, the licence, any hash or any
file on disk: *do the numbers and the time labels in this workspace agree, and what is
still unverified?* It never writes, never runs a simulation and never fingerprints a file.

Contract of the returned report:

* ``status`` is ``blocked`` / ``needs_review`` / ``completed``. ``completed`` means only
  that no numeric-level review item is left; it is not a physical certification.
* ``provider_to_csv`` compares the raw provider payload rebuilt with the very same
  converter the acquisition module uses. That is a same-converter consistency check, not
  an independent algorithm proof.
* ``time_basis`` reports what the native ``.MET`` header and the ``.SIT`` file *declare*.
  Hourly values and labels matching row by row does **not** show that the effective
  sun-position time shift is zero; no public field exposes that residual.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

from pvsyst_cli import result_units
from pvsyst_paths import workspace_file
from pvsyst_weather import inspect_weather_values
from pvsyst_weather_acquisition import (
    TEMPERATURE_METHODS,
    WeatherAcquisitionError,
    _build_rows,
    _index_payload,
    standard_utc_offset_hours,
)
from pvsyst_weather_flow import (
    compare_native_weather,
    declared_fields,
    normalized_rows,
    read_object_prefix,
)

MIN_YEAR, MAX_YEAR = 1969, 2068
MAX_CSV_BYTES = 32 * 1024 * 1024
MAX_ACQUISITION_BYTES = 512 * 1024
MAX_WEATHER_MANIFEST_BYTES = 256 * 1024
MAX_MET_HEADER_BYTES = 256 * 1024
TIME_BASIS = "local_standard_interval_start"
PROVIDER_EPSILON = 1e-9
REQUIRED_COLUMNS = ("GHI", "DHI", "Tamb")

MET_ROOT_FIELDS = ("Version", "Flags", "IsSourceUt", "HourShift", "TimeShift",
                   "TimeShiftTotalMin", "PasAccum", "VarMeteoVarAcc", "VarMeteoStepDuration",
                   "DateDebF", "DateFinF")
MET_SITE_FIELDS = ("Version", "Site", "Latitude", "Longitude", "Altitude", "TimeZone", "HLeg")
SIT_ROOT_FIELDS = ("Version", "Site", "Latitude", "Longitude", "Altitude", "TimeZone", "HLeg")

SCOPE = ("Read-only hourly-value, label and declared-field review of one prepared CSV against one "
         "native result. No simulation, no licence, no CLI call, no write and no hashing: the CSV is "
         "re-inspected through the hash-free inspector, so this report carries no checksum field. "
         "Hourly data and label agreement does not certify the effective sun-position time shift, the "
         "clear-sky model or the ERA5 source quality, and ``completed`` would mean only that no "
         "numeric-level review item is left, never a physical certification.")


def _read_declarations(path: Path, limit: int, root_fields: tuple[str, ...],
                       site_fields: tuple[str, ...] = ()):
    """Bounded declaration read, sharing the prepare guard's reader instead of duplicating it."""
    segments, stop = read_object_prefix(path, limit)
    return (declared_fields(segments, "root", root_fields),
            declared_fields(segments, "SiteMet", site_fields) if site_fields else {}, stop)


def _read_json_bounded(path: Path, limit: int, label: str):
    size = path.stat().st_size
    if size > limit:
        raise ValueError(f"{label} exceeds its {limit}-byte read limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a JSON object")
    return data


def _named_asset(workspace: Path, folder: str, manifest: dict, section: str, key: str,
                 extension: str):
    """Resolve a manifest-declared asset by filename only; an absolute path is never trusted."""
    entry = (manifest.get(section) or {}).get(key)
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
        raise ValueError(f"{section}.{key} must declare a filename")
    return workspace_file(workspace, folder, entry["name"], extension)


def _source_gate(report: dict) -> list[str]:
    """Every hard reason the prepared CSV itself cannot be reviewed as a standard hourly year."""
    issues = []
    if report.get("valid") is not True:
        issues.append("Prepared CSV is not a clean full-year standard hourly file: "
                      + "; ".join(report.get("issues", []) + report.get("unchecked_checks", [])))
    ignored = report.get("ignored_columns") or []
    if ignored:
        issues.append("Prepared CSV carries columns outside GHI/DHI/Tamb: " + ", ".join(ignored))
    extra = sorted(set(report.get("units", {})) - {"Year", "Month", "Day", "Hour", "Minute"}
                   - set(REQUIRED_COLUMNS))
    if extra:
        issues.append("Prepared CSV carries unmapped weather columns: " + ", ".join(extra))
    metadata = report.get("metadata", {})
    for tag in ("#Hour Shift", "#Time Shift", "#Time reference"):
        if metadata.get(tag):
            issues.append(f"Prepared CSV declares {tag}={metadata[tag]}; shifted labels are not reviewed here")
    if report.get("nonzero_minute_records"):
        issues.append("Prepared CSV has sub-hourly labels; only whole-hour interval starts are reviewed")
    if not report.get("raw_hourly_coverage_complete"):
        issues.append("Prepared CSV does not cover one complete raw hourly year")
    return issues


def _native_gate(native_path: Path) -> list[str]:
    """Cheap native-side gate: size and the three weather column units only.

    Row count, label alignment, header uniqueness and numeric finiteness of the three required
    columns are established by ``compare_native_weather`` itself, so the file is not walked
    twice. Unrelated exported columns are deliberately not inspected here.
    """
    issues = []
    if native_path.stat().st_size > MAX_CSV_BYTES:
        issues.append(f"Native result exceeds the {MAX_CSV_BYTES}-byte read limit")
        return issues
    units = result_units(native_path, strict=True)
    for key in ("GlobHor", "DiffHor"):
        if units.get(key) not in ("W/m2", "W/m\u00b2"):
            issues.append(f"Native {key} must be in W/m2, got {units.get(key)!r}")
    if units.get("T_Amb") not in ("degC", "deg.C", "\u00b0C"):
        issues.append(f"Native T_Amb must be in degrees C, got {units.get('T_Amb')!r}")
    return issues


def _provider_to_csv(source: Path, report: dict, acquisition: dict, workspace: Path, year: int,
                     csv_name: str):
    """Rebuild the provider payload with the acquisition converter and compare with the CSV."""
    location = acquisition.get("location")
    if not isinstance(location, dict) or not isinstance(location.get("timezone_name"), str):
        raise ValueError("Acquisition manifest declares no usable location timezone")
    created = acquisition.get("created_files")
    if not isinstance(created, dict):
        raise ValueError("Acquisition manifest created_files must be a JSON object")
    declared_csv = created.get("csv")
    if not isinstance(declared_csv, dict) or declared_csv.get("name") != csv_name:
        raise ValueError("Acquisition manifest created_files.csv.name does not name the reviewed CSV")
    raw_path = _named_asset(workspace, "Meteo", acquisition, "created_files", "raw_json", ".json")
    if raw_path.stat().st_size > MAX_CSV_BYTES:
        raise ValueError(f"Provider raw response exceeds the {MAX_CSV_BYTES}-byte read limit")
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Provider raw response is not a JSON object")
    values, labels = _index_payload(payload)
    offset_hours = standard_utc_offset_hours(location["timezone_name"], year)
    utc_start = datetime(year, 1, 1) - timedelta(hours=offset_hours)
    rebuilt_rows, build_issues, build_warnings = _build_rows(values, year=year, utc_start=utc_start)
    rebuilt = {stamp: (ghi, dhi, tamb) for stamp, ghi, dhi, tamb in rebuilt_rows}

    stats = {name: {"samples": 0, "equal": 0, "max_abs_delta": 0.0, "signed_delta_sum": 0.0}
             for name in REQUIRED_COLUMNS}
    unbuilt, examples = 0, []
    for stamp, ghi, dhi, tamb in normalized_rows(source, report):
        got = rebuilt.get(stamp)
        if got is None:
            unbuilt += 1
            if len(examples) < 6:
                examples.append({"label": str(stamp), "reason": "not rebuilt from the provider payload"})
            continue
        for name, reference, value in zip(REQUIRED_COLUMNS, (ghi, dhi, tamb), got):
            delta = value - reference
            state = stats[name]
            state["samples"] += 1
            state["max_abs_delta"] = max(state["max_abs_delta"], abs(delta))
            state["signed_delta_sum"] += delta
            if abs(delta) <= PROVIDER_EPSILON:
                state["equal"] += 1
            elif len(examples) < 6:
                examples.append({"label": str(stamp), "column": name, "csv": reference, "provider": value})
    for state in stats.values():
        samples = state["samples"]
        state["equal_rate"] = state["equal"] / samples if samples else None
        state["mean_signed"] = state["signed_delta_sum"] / samples if samples else None
    matches = (unbuilt == 0 and not build_issues
               and all(state["max_abs_delta"] <= PROVIDER_EPSILON for state in stats.values()))
    return {
        "raw_json": raw_path.name,
        "matches": matches,
        "provider_label_count": len(labels),
        "provider_label_span": [labels[0].isoformat(), labels[-1].isoformat()] if labels else None,
        "utc_interval_start": utc_start.isoformat() + "Z",
        "standard_utc_offset_hours": offset_hours,
        "declared_offset_hours_in_csv": report.get("metadata", {}).get("#Time Zone"),
        "columns": stats,
        "rows_compared": stats["GHI"]["samples"],
        "rows_without_a_provider_row": unbuilt,
        "annual_signed_delta_kwh_m2": {"GHI": stats["GHI"]["signed_delta_sum"] / 1000.0,
                                       "DHI": stats["DHI"]["signed_delta_sum"] / 1000.0},
        "temperature_mean_delta_degC": stats["Tamb"]["mean_signed"],
        "build_issues": build_issues,
        "build_warnings": build_warnings,
        "examples": examples,
        "epsilon": PROVIDER_EPSILON,
        "method": "Same acquisition converter rebuilt from the stored provider payload; a "
                  "same-converter consistency check, not an independent algorithm proof.",
    }


def _acquisition_evidence(workspace: Path, source: Path, report: dict, name: str, year: int,
                          csv_name: str):
    """Validate the acquisition manifest against the CSV and rebuild the provider rows.

    Returns ``(provider_report, review_reasons)``. A manifest that cannot be validated yields
    ``None`` plus readable reasons instead of a half-verified provider block. A payload that
    rebuilds but disagrees with the CSV keeps its concrete statistics and is reported as a
    named review reason with ``matches`` false; it is never silently treated as verified.
    """
    if not name:
        return None, ["No acquisition manifest was supplied: the provider leg of this chain has no "
                      "evidence here, and a CSV #Data Source label alone does not verify transport."]
    manifest = _read_json_bounded(workspace_file(workspace, "Results", name, ".json"),
                                  MAX_ACQUISITION_BYTES, "Acquisition manifest")
    issues = []
    if manifest.get("status") not in ("prepared", "needs_review"):
        issues.append(f"Acquisition manifest status is {manifest.get('status')!r}, not a prepared run")
    if manifest.get("year") != year:
        issues.append(f"Acquisition manifest year {manifest.get('year')!r} differs from the requested {year}")
    if manifest.get("method") not in TEMPERATURE_METHODS:
        issues.append(f"Acquisition temperature method is {manifest.get('method')!r}, "
                      f"not one of {list(TEMPERATURE_METHODS)}")
    if manifest.get("time_basis") != TIME_BASIS:
        issues.append(f"Acquisition time basis is {manifest.get('time_basis')!r}, not {TIME_BASIS}")
    location = manifest.get("location")
    if not isinstance(location, dict):
        return None, ["Acquisition manifest location must be a JSON object"]
    metadata = report.get("metadata", {})
    for key, tag in (("name", "#Site"), ("country_code", "#Country")):
        if location.get(key) and metadata.get(tag) and location[key] != metadata[tag]:
            issues.append(f"Acquisition location {key} {location[key]!r} differs from CSV {tag} "
                          f"{metadata[tag]!r}")
    if isinstance(location.get("latitude"), (int, float)) and metadata.get("#Latitude"):
        if abs(float(location["latitude"]) - float(metadata["#Latitude"])) > 5e-5:
            issues.append("Acquisition latitude differs from the CSV #Latitude beyond its printed precision")
    if isinstance(location.get("longitude"), (int, float)) and metadata.get("#Longitude"):
        if abs(float(location["longitude"]) - float(metadata["#Longitude"])) > 5e-5:
            issues.append("Acquisition longitude differs from the CSV #Longitude beyond its printed precision")
    if issues:
        return None, issues
    try:
        provider = _provider_to_csv(source, report, manifest, workspace, year, csv_name)
    except (ValueError, OSError, AttributeError, KeyError, TypeError,
            WeatherAcquisitionError, json.JSONDecodeError) as exc:
        return None, [f"Provider evidence could not be rebuilt: {exc}"]
    declared_offset = provider["declared_offset_hours_in_csv"]
    if declared_offset not in (None, "") \
            and float(declared_offset) != float(provider["standard_utc_offset_hours"]):
        return None, ["CSV #Time Zone differs from the declared IANA zone standard offset"]
    provider["manifest_name"] = name
    reasons = []
    if provider["build_issues"]:
        reasons.append("The provider payload did not rebuild into a complete standard hourly year: "
                       + "; ".join(provider["build_issues"][:3]))
    if provider["rows_without_a_provider_row"]:
        reasons.append(f"{provider['rows_without_a_provider_row']} prepared rows have no provider row "
                       "(unpublished or null hours are never filled): those rows are not corroborated "
                       "by the stored payload.")
    if not provider["matches"]:
        worst_name, worst_state = max(provider["columns"].items(),
                                      key=lambda item: item[1]["max_abs_delta"])
        reasons.append("The rebuilt provider rows do not reproduce the prepared CSV within the disclosed "
                       f"epsilon {provider['epsilon']!r}: worst column {worst_name} "
                       f"max_abs_delta={worst_state['max_abs_delta']!r}. The per-column statistics are "
                       "reported in provider_to_csv and nothing is attributed to a cause here.")
    return provider, reasons


def _native_context(native_path: Path):
    """Scan the native result lead-in for the SIT and MET files it actually consumed.

    This is a plain text scan of the rows before the ``date`` header; it loads no solution
    function and hashes nothing. A missing or duplicated context row is reported, not repaired.
    """
    context, duplicates = {}, []
    with native_path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
        for row in csv.reader(stream, delimiter=";"):
            cells = [cell.strip() for cell in row]
            if not cells or not any(cells):
                continue
            head = cells[0].lower()
            if head.startswith("date"):
                break
            if head in ("geographical site", "weather data"):
                if head in context:
                    duplicates.append(head)
                elif len(cells) > 1:
                    context[head] = cells[1]
    return context, duplicates


def _declared_source_year(met_fields: dict):
    """The year the .MET root declares for its own data, from DateDebF; never a calendar default."""
    entry = (met_fields.get("DateDebF") or [{}])[0].get("value", "")
    for fmt in ("%d/%m/%y %H:%M", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(entry, fmt).year
        except ValueError:
            continue
    return None


def _time_basis_declaration(workspace: Path, weather_manifest_name: str, native_path: Path,
                            csv_name: str, year: int):
    """Report what the native .MET and .SIT declare, and whether the native run consumed them.

    Only ``Key=Value`` declarations are read, with nesting tracked, so the root time fields and
    the nested ``PVObject_SiteMet`` site fields stay separate and a root key never inherits a
    nested value. Declarations are attributed to the native run only when the result file's own
    lead-in names those exact files; otherwise the mismatch is reported and no claim is made.
    Nothing here reports an effective sun-position shift, and no file is hashed.
    """
    context, duplicates = _native_context(native_path)
    result = {"met_name": None, "sit_name": None, "met_fields": None, "met_site_fields": None,
              "sit_fields": None, "met_header_read_stop": None, "conflicts": [], "reviewed": False,
              "consumed_met": context.get("weather data"), "consumed_sit": context.get("geographical site"),
              "duplicate_context_rows": duplicates, "context_matches": None,
              "declared_source_year": None}
    if not weather_manifest_name:
        result["conflicts"].append(
            "No weather preparation manifest was supplied, so no .MET/.SIT declaration was read here "
            f"(the native lead-in names {result['consumed_sit']!r} and {result['consumed_met']!r})")
        return result
    manifest = _read_json_bounded(workspace_file(workspace, "Results", weather_manifest_name, ".json"),
                                  MAX_WEATHER_MANIFEST_BYTES, "Weather manifest")
    declaration_issues = []
    if manifest.get("status") not in ("prepared", "needs_review"):
        declaration_issues.append(f"Weather manifest status is {manifest.get('status')!r}, "
                                  "not a prepared run")
    if manifest.get("time_basis") != TIME_BASIS:
        declaration_issues.append(f"Weather manifest time basis is {manifest.get('time_basis')!r}, "
                                  f"not {TIME_BASIS}")
    if manifest.get("year") != year:
        declaration_issues.append(f"Weather manifest year {manifest.get('year')!r} differs from the "
                                  f"reviewed year {year}")
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("name") != csv_name:
        declaration_issues.append("Weather manifest source.name does not name the reviewed CSV")
    met_path = _named_asset(workspace, "Meteo", manifest, "created_files", "met", ".met")
    sit_path = _named_asset(workspace, "Sites", manifest, "created_files", "sit", ".sit")
    met_fields, met_site, stop = _read_declarations(met_path, MAX_MET_HEADER_BYTES,
                                                   MET_ROOT_FIELDS, MET_SITE_FIELDS)
    sit_fields, _, _ = _read_declarations(sit_path, MAX_MET_HEADER_BYTES, SIT_ROOT_FIELDS)
    result.update(met_name=met_path.name, sit_name=sit_path.name, met_fields=met_fields,
                  met_site_fields=met_site, sit_fields=sit_fields,
                  met_header_read_stop=stop, reviewed=True,
                  declared_source_year=_declared_source_year(met_fields))
    result["met_header_read_scope"] = (
        "Declared owning fields only, and that bound is the narrow contract of this review entry point "
        "rather than a claim that a PVsyst object can never be parsed: the root time fields of the "
        "pvMeteo object and the site fields nested in its PVObject_SiteMet child are taken from at most "
        f"{MAX_MET_HEADER_BYTES} bytes of leading text, and the read ends at the first measured-value "
        "list, unknown nested object, closing marker or non-declaration line. Both the UTF-8 and the "
        "cp1252 decoding of a site label are accepted, so a legal non-ASCII name cannot hide the time "
        "fields after it. No hour array, curve table, compressed or binary block is read or "
        "interpreted, and no nested value is ever reported as a root owning field.")
    result["conflicts"].extend(declaration_issues)
    result["context_matches"] = (result["consumed_met"] == met_path.name
                                 and result["consumed_sit"] == sit_path.name
                                 and not duplicates)
    if not result["context_matches"]:
        result["conflicts"].append(
            "The native result lead-in does not name the manifest's .MET/.SIT files "
            f"(lead-in names {result['consumed_met']!r}/{result['consumed_sit']!r}, manifest declares "
            f"{met_path.name!r}/{sit_path.name!r}, duplicate context rows: {duplicates}); the fields "
            "below describe the declared files and are not attributed to the native run.")
    if result["declared_source_year"] != year:
        result["conflicts"].append(
            f"The .MET root declares its own data year as {result['declared_source_year']!r}, not the "
            f"reviewed {year}; the SIT calendar defaults and historical DateBeg values are not treated "
            "as the source year.")
    met_legal = (met_site.get("HLeg") or [{}])[0].get("value")
    sit_legal = (sit_fields.get("HLeg") or [{}])[0].get("value")
    if met_legal is not None and sit_legal is not None and met_legal != sit_legal:
        result["conflicts"].append(
            f"Declared legal-time flag differs: .SIT HLeg={sit_legal} but the .MET site snapshot "
            f"HLeg={met_legal}. Both files describe the same site, so the legal-time basis is not "
            "self-consistent between them; no symbol or binary interpretation is attempted here")
    return result


def review_weather(workspace, *, csv_name, native_csv_name, year, acquisition_manifest_name="",
                   weather_manifest_name=""):
    """Review one prepared weather CSV against one native result, read-only.

    ``year`` must be a strict ``int`` in 1969..2068. The CSV is re-inspected with the
    hash-free inspector, so a historical manifest report is never trusted. Nothing is
    written, hashed, simulated or certified.
    """
    result = {"status": "blocked", "issues": [], "warnings": [], "source_summary": None,
              "provider_to_csv": None, "csv_to_native": None, "review_items": [], "time_basis": None,
              "scope": SCOPE}
    try:
        if not isinstance(workspace, (str, Path)):
            raise ValueError("workspace must be a path")
        workspace = Path(workspace)
        if type(year) is not int or not MIN_YEAR <= year <= MAX_YEAR:
            raise ValueError(f"year must be a strict integer in {MIN_YEAR}..{MAX_YEAR}")
        for label, value, extension in (("csv_name", csv_name, ".csv"),
                                        ("native_csv_name", native_csv_name, ".csv")):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{label} must be a filename without directories")
            if not value.lower().endswith(extension):
                raise ValueError(f"{label} must name a {extension} file")
        for label, value in (("acquisition_manifest_name", acquisition_manifest_name),
                             ("weather_manifest_name", weather_manifest_name)):
            if not isinstance(value, str):
                raise ValueError(f"{label} must be a string")
        source = workspace_file(workspace, "Meteo", csv_name, ".csv")
        native_path = workspace_file(workspace, "Results", native_csv_name, ".csv")
        for path in (source, native_path):
            if not path.is_file():
                raise ValueError(f"{path.name} does not exist in its workspace folder")
    except (ValueError, OSError) as exc:
        result["issues"].append(str(exc))
        return result

    try:
        report = inspect_weather_values(source, year)
    except (ValueError, OSError) as exc:
        result["issues"].append(f"Prepared CSV inspection failed: {exc}")
        return result

    result["source_summary"] = {
        "filename": report["filename"], "rows": report["rows"], "expected_rows": report["expected_rows"],
        "expected_year": report["expected_year"], "metadata": report["metadata"], "units": report["units"],
        "first": report["first"], "last": report["last"], "valid": report["valid"],
        "raw_hourly_coverage_complete": report["raw_hourly_coverage_complete"],
        "nonzero_minute_records": report["nonzero_minute_records"], "months": report["months"],
    }
    gate = _source_gate(report)
    if gate:
        result["issues"].extend(gate)
        return result
    result["warnings"].extend(report["warnings"])

    try:
        native_gate = _native_gate(native_path)
        if native_gate:
            result["issues"].extend(native_gate)
            return result
        result["csv_to_native"] = compare_native_weather(native_path, source, report)
    except (ValueError, OSError) as exc:
        result["issues"].append(f"Native weather comparison failed: {exc}")
        return result

    try:
        provider, provider_issues = _acquisition_evidence(workspace, source, report,
                                                         acquisition_manifest_name, year, csv_name)
    except (ValueError, OSError, AttributeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        provider, provider_issues = None, [f"Provider evidence could not be read: {exc}"]
    result["provider_to_csv"] = provider

    try:
        time_basis = _time_basis_declaration(workspace, weather_manifest_name, native_path,
                                             csv_name, year)
    except (ValueError, OSError, AttributeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        time_basis = {"conflicts": [f"Native time declaration could not be read: {exc}"],
                      "reviewed": False, "context_matches": None}
    result["time_basis"] = time_basis

    review_items = []
    if provider is None or provider_issues:
        review_items.extend(provider_issues or ["Provider evidence is unavailable"])
        review_items.append("The provider leg is unverified: the CSV declares its own source, but the "
                            "manifest, raw payload and rebuilt rows were not all confirmed here.")
    if provider is not None and not provider_issues:
        review_items.append("Hourly Tamb uses the disclosed endpoint_mean approximation (the mean of the two "
                            "bounding provider instants), not a measured hourly mean; no sub-hourly "
                            "observation exists in the raw payload, so no hourly-mean accuracy is claimed.")
    if result["csv_to_native"] is not None:
        columns = result["csv_to_native"]["columns"]
        if any(state["outside_review_threshold"] for state in columns.values()):
            review_items.append("Native values differ from the prepared CSV beyond the disclosed review "
                                "thresholds. The per-column (max_abs, MAE, RMSE, mean_signed) and per-month "
                                "deltas are reported in csv_to_native; no cause is attributed here and no "
                                "rounding or storage behaviour is certified.")
        else:
            review_items.append("Native hourly values and labels match the prepared CSV within the disclosed "
                                "review thresholds; this is a data-level agreement, not a physical check.")
    review_items.extend(time_basis["conflicts"])
    review_items.append("The effective sun-position time shift inside the native run is unverified in this "
                        "evidence: the declared HourShift/TimeShift/TimeShiftTotalMin fields and row-by-row "
                        "label agreement do not establish that the residual applied to the sun position is "
                        "zero. This states what this report cannot show, not that such a field is absent "
                        "everywhere.")
    result["review_items"] = review_items
    # Every item above is a standing review obligation of this profile: the endpoint_mean temperature
    # approximation and the unverified sun-position residual never disappear, so ``completed`` is
    # reachable only if that set of obligations genuinely becomes empty.
    result["status"] = "needs_review" if review_items else "completed"
    return result

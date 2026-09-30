"""Typed Windows adapter for installed PVsystCLI 8.0/8.1 releases.

CLI capabilities are discovered from the running executable's own help output.
No PVsyst binaries, licenses, or proprietary project data are included.
"""
from __future__ import annotations

import csv
import math
import os
import re
import subprocess
import threading
from functools import wraps
from datetime import datetime
from pathlib import Path
from typing import Iterator

from pvsyst_paths import project_name, project_root, safe_filename, workspace_file, workspace_folder

VERIFIED_VARIABLES = (
    "viGlobInc", "viGlobEff", "viEArray", "viE_Grid", "viPR",
    "viInvLoss", "viIL_Oper", "viIL_Pmin", "viIL_Vmin", "viIL_Pmax",
    "viILPmxSH", "viIL_Vmax", "viIL_Imax",
)
VAR_GROUPS = {
    "energy": ("viGlobInc", "viGlobEff", "viEArray", "viE_Grid", "viPR"),
    "inverter_losses": ("viInvLoss", "viIL_Oper", "viIL_Pmin", "viIL_Vmin",
                        "viIL_Pmax", "viILPmxSH", "viIL_Vmax", "viIL_Imax"),
    "all_common": VERIFIED_VARIABLES,
}
SFI_TEMPLATE = """PVObject_=pvSimulFile
  Comment={comment}
  Version=8.0.0
  Flags=$0001
  FileNameWr=results.CSV
  VarIdentWr={variables},
  Units=uikW,uiW_m2,uim3_h,uimeterW,
  TypeValW=Hour
  Separator=$003B
  DecimalSep=$002E
  NCarCol=6
  ASCIIFmt=1
  DatesFmt=1
End of TSimulFich
"""
REPORT_PAGES = frozenset(
    "cover summary notes params horizon shadings usersneeds results econeval "
    "circuit losses financialbalance p50p90 carbonbalance predefgraphs".split()
)
REGIONS = frozenset(
    "africa antarctica australia asia europe north_america south_america pacific".split()
)


class PVsystError(RuntimeError):
    """The installed CLI rejected a request or failed to create an artifact."""


def build_sfi(path: str | os.PathLike, variables: list[str] | str,
              comment: str = "Hourly Results") -> Path:
    """Create an hourly SFI from verified groups or explicit vi* identifiers."""
    if isinstance(variables, str):
        variables = list(VAR_GROUPS[variables])
    if not variables or any(not re.fullmatch(r"vi[A-Za-z0-9_]+", v) for v in variables):
        raise ValueError("Expected a nonempty list of vi* identifiers")
    if not comment.strip() or "\n" in comment or "\r" in comment:
        raise ValueError("SFI comment must be one nonempty line")
    target = Path(path)
    if target.suffix.lower() != ".sfi":
        raise ValueError("SFI destination must end in .sfi")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8", newline="\n") as file:
        file.write(SFI_TEMPLATE.format(comment=comment, variables=",".join(variables)))
    return target


def build_monthly_weather_csv(path: str | os.PathLike, global_h: list[float],
                              temperature: list[float],
                              diffuse_h: list[float] | None = None,
                              wind_velocity: list[float] | None = None) -> Path:
    """Create the 14-row monthly input format required by `create-site`."""
    series = {"GlobH": global_h, "Temp": temperature}
    if diffuse_h is not None:
        series["DiffH"] = diffuse_h
    if wind_velocity is not None:
        series["WindVel"] = wind_velocity
    if any(len(values) != 12 or any(not math.isfinite(v) for v in values)
           for values in series.values()):
        raise ValueError("Every weather series must contain 12 finite monthly values")
    if any(v < 0 for v in global_h) or (diffuse_h is not None and
                                       any(d < 0 or d > g for d, g in zip(diffuse_h, global_h))):
        raise ValueError("Monthly irradiation values must be nonnegative and diffuse <= global")
    target = Path(path)
    if target.suffix.lower() != ".csv":
        raise ValueError("Monthly weather destination must end in .csv")
    target.parent.mkdir(parents=True, exist_ok=True)
    units = {"GlobH": "kWh/m2/mth", "Temp": "degC", "DiffH": "kWh/m2/mth",
             "WindVel": "m/s"}
    with target.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter=";", lineterminator="\n")
        writer.writerow(series)
        writer.writerow([units[name] for name in series])
        writer.writerows(zip(*series.values()))
    return target


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class PVsystCLI:
    def __init__(self, cli_path: str | os.PathLike | None = None,
                 workspace: str | os.PathLike | None = None, timeout: int = 3600):
        cli_path = cli_path or os.environ.get("PVSYST_CLI")
        workspace = workspace or os.environ.get("PVSYST_WORKSPACE")
        if not cli_path or not workspace:
            raise ValueError("Set PVSYST_CLI and PVSYST_WORKSPACE or pass both paths")
        self.cli = Path(cli_path).resolve(strict=True)
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.cli.is_file() or not self.workspace.is_dir():
            raise ValueError("CLI must be a file and workspace must be a directory")
        if timeout < 1:
            raise ValueError("Timeout must be positive")
        self.timeout = timeout
        self._help_cache: dict[str, str] = {}
        self._lock = threading.RLock()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        # Argument arrays preserve spaces in paths; serialize CLI operations on one workspace.
        with self._lock:
            return subprocess.run([str(self.cli), *args], cwd=self.cli.parent,
                                  capture_output=True, text=True, errors="replace",
                                  timeout=self.timeout, check=False)

    @staticmethod
    def _output(result: subprocess.CompletedProcess[str]) -> str:
        return (result.stdout or "") + "\n" + (result.stderr or "")

    @staticmethod
    def _redact(text: str) -> str:
        return "\n".join("[redacted]" if re.search(
            r"(?i)(license key|host id|customer.id|activation key)", line) else line
                         for line in text.splitlines())

    def command_help(self, command: str = "") -> str:
        if command not in self._help_cache:
            result = self._run("help", *([command] if command else []))
            text = self._output(result).strip()
            if result.returncode != 0 or "Unknown command" in text:
                raise PVsystError(f"CLI does not support {command or 'help'}")
            self._help_cache[command] = text
        return self._help_cache[command]

    def _options(self, command: str) -> set[str]:
        return set(re.findall(r"(?m)^\s*--([a-z][a-z-]+)(?=\||:|\s)",
                              self.command_help(command)))

    def capabilities(self) -> dict[str, list[str]]:
        main = self.command_help()
        commands = re.findall(r"(?m)^\s+([a-z][a-z-]+):", main)
        return {name: sorted(self._options(name)) for name in commands}

    def _require(self, command: str, option: str) -> None:
        if option not in self._options(command):
            raise PVsystError(f"Installed PVsystCLI lacks {command} --{option}")

    def version(self) -> str:
        result = self._run("version")
        if result.returncode != 0:
            raise PVsystError("PVsystCLI version query failed")
        return self._output(result).strip().splitlines()[0]

    def license_info(self) -> dict:
        result = self._run("lic-info")
        text = self._output(result)
        if result.returncode != 0:
            raise PVsystError("PVsystCLI license query failed")
        info: dict = {"status": "UNSPECIFIED"}
        for key, value in re.findall(r"(?m)^\s*(Status|Remaining days|Remaining executions|Expiration date):\s*([^\r\n]*)", text):
            name = {"Status": "status", "Remaining days": "remaining_days",
                    "Remaining executions": "remaining_executions",
                    "Expiration date": "expiration_date"}[key]
            info[name] = (int(value.strip()) if name in ("remaining_days", "remaining_executions")
                          and value.strip().isdigit() else value.strip() or None)
        if not any(k in info for k in ("remaining_days", "expiration_date")) and info["status"] == "UNSPECIFIED":
            raise PVsystError("PVsystCLI license query returned no recognized fields")
        # Never return raw output: it may contain the key and hardware identifiers.
        return info

    @_serialized
    def _license_change(self, command: str, *args: str, confirm: bool) -> dict:
        if confirm is not True:
            raise ValueError("License changes require confirm=True")
        try:
            result = self._run(command, *args)
        except subprocess.TimeoutExpired:
            # TimeoutExpired includes the command line (and possibly a license key).
            raise PVsystError(f"{command} timed out") from None
        if result.returncode != 0:
            raise PVsystError(f"{command} failed (exit {result.returncode})")
        return {"command": command, "exit_code": result.returncode,
                "license": self.license_info()}

    def license_activate(self, key: str, *, confirm: bool = False) -> dict:
        if not key.strip() or "\n" in key or "\r" in key:
            raise ValueError("A single-line license key is required")
        return self._license_change("lic-activate", f"-k:{key}", confirm=confirm)

    def license_deactivate(self, customer_id: str, *, confirm: bool = False) -> dict:
        if not customer_id.strip() or "\n" in customer_id or "\r" in customer_id:
            raise ValueError("A single-line customer ID is required")
        return self._license_change("lic-deactivate", f"-c:{customer_id}", confirm=confirm)

    def license_sync(self, *, confirm: bool = False) -> dict:
        return self._license_change("lic-sync", confirm=confirm)

    @_serialized
    def export_logs(self, folder: str | os.PathLike) -> Path:
        target = Path(folder).resolve()
        target.mkdir(parents=True, exist_ok=True)
        before = {p.name for p in target.glob("*.zip")}
        result = self._run("export-logs", f"-t:{target}")
        created = sorted((p for p in target.glob("*.zip") if p.name not in before),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if result.returncode != 0 or not created:
            raise PVsystError("Log export did not produce a new ZIP file")
        return created[0]

    def list_projects(self) -> list[str]:
        return sorted(p.name for p in (self.workspace / "Projects").glob("*.PRJ") if p.is_file())

    def list_variants(self, project: str) -> list[str]:
        stem = Path(self._project_name(project)).stem
        return sorted(p.suffix[1:] for p in project_root(self.workspace).iterdir()
                      if p.stem.casefold() == stem.casefold() and p.is_file()
                      and re.fullmatch(r"VC[A-Za-z0-9]+", p.suffix[1:], re.I))

    @staticmethod
    def _project_name(project: str) -> str:
        return project_name(project)

    @staticmethod
    def _input(path: str | os.PathLike, suffix: str | None = None) -> Path:
        item = Path(path).resolve(strict=True)
        if not item.is_file() or (suffix and item.suffix.lower() != suffix):
            raise ValueError(f"Input must be an existing {suffix or 'regular'} file")
        return item

    @staticmethod
    def _target(path: str | os.PathLike, suffix: str) -> Path:
        item = Path(path).resolve()
        if item.suffix.lower() != suffix or item.exists():
            raise ValueError(f"Output must be a new {suffix} file")
        item.parent.mkdir(parents=True, exist_ok=True)
        return item

    @staticmethod
    def _log_level(value: int | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3:
            raise ValueError("Log level must be 0, 1, 2, or 3")
        return f"-ll:{value}"

    def _batch_outputs(self, batch_params: Path) -> tuple[list[str], dict[str, tuple[int, int]]]:
        """Validate hourly output names before a batch can write workspace files."""
        if not batch_params.stem.lower().endswith("params"):
            raise ValueError("Batch parameters filename must end in Params.CSV")
        workspace_folder(self.workspace, "UserHourly")
        batch_dir = workspace_folder(self.workspace, "UserBatch")
        names: list[str] = []
        with batch_params.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
            for fields in csv.reader(stream, delimiter=";"):
                if fields and re.fullmatch(r"SIM_[A-Za-z0-9_-]+", fields[0].strip()):
                    if len(fields) > 1 and fields[1].strip():
                        name = fields[1].strip()
                        try:
                            safe_filename(name, ".csv")
                        except ValueError:
                            raise ValueError("Batch hourly output must be a unique CSV filename") from None
                        if name.casefold() in (item.casefold() for item in names):
                            raise ValueError("Batch hourly output must be a unique CSV filename")
                        if workspace_file(self.workspace, "UserHourly", name, ".csv").exists():
                            raise ValueError(f"Batch hourly output already exists: {name}")
                        names.append(name)
        summary = workspace_file(self.workspace, "UserBatch",
                                 batch_params.stem[:-6] + "Results.CSV", ".csv")
        if summary.exists():
            raise ValueError(f"Batch summary already exists: {summary.name}")
        before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                  for p in batch_dir.glob("*Results.CSV") if p.is_file()}
        return names, before

    @_serialized
    def run_simulation(self, project: str, variant: str, *,
                       sfi: str | os.PathLike | None = None,
                       out_csv: str | os.PathLike | None = None,
                       met: str | os.PathLike | None = None,
                       report_pdf: str | os.PathLike | None = None,
                       rvt: str | os.PathLike | None = None,
                       params: str | os.PathLike | None = None,
                       batch_params: str | os.PathLike | None = None,
                       batch_rvt: str | os.PathLike | None = None,
                       site: str | os.PathLike | None = None,
                       synthetic_weather: bool = False,
                       time_step: str | None = None,
                       start_date: str | None = None, end_date: str | None = None,
                       report_language: str | None = None,
                       report_pages: list[str] | None = None,
                       recompute_shading: bool | None = None,
                       log_level: int | None = None) -> dict:
        project = self._project_name(project)
        if project not in self.list_projects() or variant not in self.list_variants(project):
            raise ValueError("Project and variant must exist in the workspace")
        if batch_params and (out_csv is not None or report_pdf is not None):
            raise ValueError("Batch output is generated by the CLI in UserBatch/UserHourly")
        if out_csv is None and report_pdf is None and not batch_params:
            raise ValueError("Request a CSV and/or a PDF output")
        args = ["run-simulation", f"-w:{self.workspace}", f"-p:{project}", f"-v:{variant}"]
        for option, path, ext in (("input-sfi-file", sfi, ".sfi"),
                                  ("input-rvt-file", rvt, ".rvt"),
                                  ("input-met-file", met, ".met"),
                                  ("input-param-file", params, None),
                                  ("batch-params-file", batch_params, None),
                                  ("batch-rvt-file", batch_rvt, ".rvt"),
                                  ("site", site, ".sit")):
            if path:
                self._require("run-simulation", option)
                flag = {"input-sfi-file": "isf", "input-rvt-file": "irf",
                        "input-met-file": "imf", "input-param-file": "ipf",
                        "batch-params-file": "bpf", "batch-rvt-file": "brf", "site": "s"}[option]
                args.append(f"-{flag}:{self._input(path, ext)}")
        if batch_rvt and not batch_params:
            raise ValueError("Batch RVT requires a batch parameters file")
        if synthetic_weather:
            if not site or met:
                raise ValueError("Synthetic weather requires a site and no MET input")
            self._require("run-simulation", "synthetic-weatherdata-generation")
            args.append("-swg")
        if time_step:
            if time_step not in ("hour", "subhour"):
                raise ValueError("Time step must be hour or subhour")
            self._require("run-simulation", "time-step")
            args.append(f"-ts:{time_step}")
        for flag, value in (("sd", start_date), ("ed", end_date)):
            if value:
                formats = ((r"\d{2}\.\d{2}", "%m.%d"),
                           (r"\d{2}\.\d{2}\.\d{2}", "%y.%m.%d"),
                           (r"\d{4}\.\d{2}\.\d{2}", "%Y.%m.%d"))
                matching = next((fmt for pattern, fmt in formats
                                 if re.fullmatch(pattern, value)), None)
                if matching is None:
                    raise ValueError("Date must use MM.DD, YY.MM.DD, or YYYY.MM.DD")
                try:
                    datetime.strptime(value, matching)
                except ValueError:
                    raise ValueError(f"Invalid calendar date: {value}") from None
                self._require("run-simulation", "start-date" if flag == "sd" else "end-date")
                args.append(f"-{flag}:{value}")
        if report_language:
            langs = re.search(r"Available language:\s*\[([^]]+)\]", self.command_help("run-simulation"))
            allowed = langs.group(1).split(", ") if langs else ["en", "fr", "de", "es", "it", "pt", "tr", "ko", "zh", "ja", "pl", "ru"]
            if report_language not in allowed:
                raise ValueError(f"Unsupported report language: {report_language}")
            args.append(f"-rl:{report_language}")
        if report_pages is not None:
            if not report_pages or any(page not in REPORT_PAGES for page in report_pages):
                raise ValueError("Unknown or empty PDF report page list")
            args.append("-rp:" + ",".join(report_pages))
        if recompute_shading is not None:
            self._require("run-simulation", "recompute-shadingfactors-tables")
            args.append("-rst:1" if recompute_shading else "-rst:0")
        level = self._log_level(log_level)
        if level:
            args.append(level)
        csv_target = self._target(out_csv, ".csv") if out_csv else None
        pdf_target = self._target(report_pdf, ".pdf") if report_pdf else None
        if csv_target:
            args.append(f"-ocf:{csv_target}")
        if pdf_target:
            args.append(f"-rpf:{pdf_target}")
        batch_names, batch_before = (self._batch_outputs(self._input(batch_params, ".csv"))
                                     if batch_params else ([], {}))
        result = self._run(*args)
        text = self._output(result)
        if (result.returncode != 0 or "Simulation done in " not in text
                or (csv_target and not csv_target.is_file())
                or (pdf_target and not pdf_target.is_file())):
            raise PVsystError("Simulation did not complete or produce requested output: " + self._redact(text)[-3000:])
        summary = None
        hourly = []
        runs = None
        if batch_params:
            changed = [p for p in workspace_folder(self.workspace, "UserBatch").glob("*Results.CSV")
                       if p.is_file() and batch_before.get(p.name) !=
                       (p.stat().st_size, p.stat().st_mtime_ns)]
            if len(changed) != 1:
                raise PVsystError("Batch simulation did not produce exactly one new summary CSV")
            summary = workspace_file(self.workspace, "UserBatch", changed[0].name, ".csv")
            hourly = [workspace_file(self.workspace, "UserHourly", name, ".csv") for name in batch_names]
            if any(not p.is_file() for p in hourly):
                raise PVsystError("Batch simulation did not produce all requested hourly CSVs")
            runs = len(parse_batch_results(summary, columns=())["scenarios"])
            if not runs:
                raise PVsystError("Batch summary contains no completed scenarios")
        timing = re.search(r"Simulation done in (\d+)min\s*(\d+)sec", text)
        return {"csv": str(csv_target) if csv_target else None,
                "pdf": str(pdf_target) if pdf_target else None,
                "seconds": int(timing[1]) * 60 + int(timing[2]) if timing else None,
                "batch_summary": str(summary) if summary else None,
                "batch_hourly": [str(p) for p in hourly], "batch_runs": runs}

    @_serialized
    def convert_meteo(self, input_csv: str | os.PathLike, mef: str | os.PathLike,
                      sit: str | os.PathLike, out_met: str | os.PathLike,
                      timeshift: int = 0, log_level: int | None = None) -> Path:
        if not -30 <= timeshift <= 30:
            raise ValueError("MEF timeshift must be between -30 and 30")
        sources = [self._input(path, ext) for path, ext in
                   ((input_csv, ".csv"), (mef, ".mef"), (sit, ".sit"))]
        target = self._target(out_met, ".met")
        args = ["convert-meteo", f"-icf:{sources[0]}", f"-imf:{sources[1]}",
                f"-isf:{sources[2]}", f"-omf:{target}", f"-imt:{timeshift}"]
        level = self._log_level(log_level)
        if level:
            args.append(level)
        result = self._run(*args)
        if result.returncode != 0 or not target.is_file():
            raise PVsystError("Conversion did not produce the expected MET: " +
                              self._redact(self._output(result))[-3000:])
        return target

    @_serialized
    def create_site(self, name: str, latitude: float, longitude: float,
                    weather_csv: str | os.PathLike, out_sit: str | os.PathLike,
                    *, altitude: float | None = None,
                    timezone: float | None = None, country_code: str = "",
                    region: str = "", source: str = "", albedo: float | None = None,
                    log_level: int | None = None) -> Path:
        self._require("create-site", "site-name")
        if not name.strip() or "\n" in name or "\r" in name:
            raise ValueError("Site name must be one nonempty line")
        for value, low, high, label in ((latitude, -90, 90, "Latitude"),
                                        (longitude, -180, 180, "Longitude"),
                                        (timezone, -12, 12, "Timezone"),
                                        (albedo, 0, 1, "Albedo")):
            if value is not None and (not math.isfinite(value) or not low <= value <= high):
                raise ValueError(f"{label} outside [{low}, {high}]")
        if altitude is not None and not math.isfinite(altitude):
            raise ValueError("Altitude must be finite")
        if country_code and not re.fullmatch(r"[A-Za-z]{2}", country_code):
            raise ValueError("Country code must have two letters")
        if region and region not in REGIONS:
            raise ValueError("Unknown region")
        if "\r" in source or "\n" in source:
            raise ValueError("Source must be one line")
        input_path = self._input(weather_csv, ".csv")
        target = self._target(out_sit, ".sit")
        args = ["create-site", f"-sn:{name}", f"-lat:{latitude}", f"-lon:{longitude}",
                f"-wdf:{input_path}", f"-osf:{target}"]
        for flag, value in (("alt", altitude), ("tz", timezone), ("cc", country_code),
                            ("r", region), ("s", source), ("alb", albedo)):
            if value is not None and value != "":
                args.append(f"-{flag}:{value}")
        level = self._log_level(log_level)
        if level:
            args.append(level)
        result = self._run(*args)
        if result.returncode != 0 or not target.is_file():
            raise PVsystError("Site creation did not produce a SIT file: " +
                              self._redact(self._output(result))[-3000:])
        return target


def parse_batch_results(path: str | os.PathLike,
                        columns: tuple[str, ...] = ("E_Grid", "PR")) -> dict:
    """Read the SIM_* rows of a PVsyst UserBatch/*Results.CSV summary."""
    with open(path, "rb") as probe:
        encoding = "utf-8-sig" if probe.read(3) == b"\xef\xbb\xbf" else "cp1252"
    headers: list[str] = []
    scenarios: list[dict] = []
    with open(path, "r", encoding=encoding, errors="replace", newline="") as stream:
        for fields in csv.reader(stream, delimiter=";"):
            if len(fields) > 1 and fields[0].strip() == "Ident" and fields[1].strip() == "Create hourly":
                headers = [field.strip() for field in fields]
                for column in columns:
                    if column not in headers:
                        raise ValueError(f"Batch result column {column!r} missing")
            elif headers and fields and re.fullmatch(r"SIM_[A-Za-z0-9_-]+", fields[0].strip()):
                def field(name: str) -> str:
                    index = headers.index(name)
                    return fields[index].strip() if index < len(fields) else ""

                values = {}
                for column in columns:
                    try:
                        values[column] = float(field(column))
                    except ValueError:
                        values[column] = None
                scenarios.append({"id": fields[0].strip(), "hourly_file": field("Create hourly"),
                                  "error": field("Error") if "Error" in headers else "",
                                  "values": values})
    if not headers or not scenarios:
        raise ValueError("No SIM_* rows found in batch summary")
    return {"columns": list(columns), "scenarios": scenarios}


def result_units(path: str | os.PathLike, *, strict: bool = False) -> dict[str, str]:
    """Read variable units from the row following a date header."""
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as stream:
        for line in stream:
            if re.match(r"^\s*date[;,]", line, re.IGNORECASE):
                separator = line.lstrip()[4]
                headers = [h.strip() for h in next(csv.reader([line], delimiter=separator))]
                unit_line = next(stream, "")
                units = [u.strip() for u in next(csv.reader([unit_line], delimiter=separator))]
                if strict and (len(units) != len(headers) or units[0]):
                    raise ValueError("Comparison requires a complete units row after the SFI header")
                return dict(zip(headers[1:], units[1:]))
    raise ValueError("Missing SFI result header")


def iter_result_csv(path: str | os.PathLike, *, strict: bool = False) -> Iterator[tuple[list[str], list]]:
    """Yield SFI rows (semicolon or comma) without loading the full file."""
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as stream:
        for line in stream:
            if re.match(r"^\s*date[;,]", line, re.IGNORECASE):
                separator = line.lstrip()[4]
                headers = [h.strip() for h in next(csv.reader([line], delimiter=separator))]
                next(stream, None)  # units row
                break
        else:
            raise ValueError("Missing SFI result header (date; or date,)")
        if len(headers) < 2:
            raise ValueError("SFI result has no numeric columns")
        seen = False
        for parts in csv.reader(stream, delimiter=separator):
            if not parts or not any(part.strip() for part in parts):
                continue
            try:
                stamp = datetime.strptime(parts[0].strip(), "%d/%m/%y %H:%M")
            except ValueError:
                if strict:
                    raise ValueError("Invalid timestamp or unexpected row after SFI header") from None
                continue
            if strict and len(parts) > len(headers):
                raise ValueError("Result row contains more values than its header")
            parts += [""] * max(0, len(headers) - len(parts))
            row = [stamp]
            for value in parts[1:len(headers)]:
                try:
                    row.append(float(value.strip()))
                except ValueError:
                    row.append(math.nan)
            seen = True
            yield headers, row
    if not seen:
        raise ValueError("Result CSV contains no simulation rows")


def parse_result_csv(path: str | os.PathLike) -> tuple[list[str], list[list]]:
    """Return all hourly SFI rows for callers that need random access."""
    rows: list[list] = []
    headers: list[str] = []
    for headers, row in iter_result_csv(path):
        rows.append(row)
    return headers, rows


def summarize_results(path: str | os.PathLike, columns: tuple[str, ...]) -> dict:
    """Shared streaming summaries; completeness covers timestamped rows in this file."""
    indices = None
    stats = {name: {"samples": 0, "sum": 0.0, "max": -math.inf} for name in columns}
    count = 0
    first = last = previous = step_minutes = None
    regular = True
    for headers, row in iter_result_csv(path):
        if indices is None:
            if not columns or any(name not in headers[1:] for name in columns):
                raise ValueError(f"Choose columns from {headers[1:]}")
            indices = {name: headers.index(name) for name in columns}
            first = str(row[0])
        if previous is not None:
            delta = (row[0] - previous).total_seconds() / 60
            if delta <= 0 or (step_minutes is not None and delta != step_minutes):
                regular = False
            elif step_minutes is None:
                step_minutes = delta
        previous = row[0]
        count += 1
        last = str(row[0])
        for name, index in indices.items():
            value = row[index]
            if math.isfinite(value):
                stats[name]["samples"] += 1
                stats[name]["sum"] += value
                stats[name]["max"] = max(stats[name]["max"], value)
    step = step_minutes if regular else None
    units = result_units(path)
    summary = {}
    for name, state in stats.items():
        samples = state["samples"]
        unit = units.get(name, "")
        observed = None
        if samples and name in ("E_Grid", "EArray") and step is not None and unit in ("W", "kW"):
            observed = state["sum"] * step / 60 * (0.001 if unit == "W" else 1)
        complete = samples == count
        summary[name] = {**state, "max": state["max"] if samples else None,
                         "mean": state["sum"] / samples if samples else None,
                         "unit": unit, "missing_samples": count - samples,
                         "coverage": samples / count, "complete": complete,
                         "observed_energy_kwh": observed,
                         "energy_kwh": observed if complete else None}
    return {"columns": headers, "rows": count, "first": first, "last": last,
            "step_minutes": step, "summary": summary}


def summarize(path: str | os.PathLike, column: str = "E_Grid") -> dict:
    """Summarize one column with the same missing-data contract as MCP."""
    result = summarize_results(path, (column,))
    return {"column": column, "rows": result["rows"], "step_minutes": result["step_minutes"],
            **result["summary"][column]}

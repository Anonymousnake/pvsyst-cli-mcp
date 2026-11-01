"""Typed Windows adapter for installed PVsystCLI 8.0/8.1 releases.

CLI capabilities are discovered from the running executable's own help output.
No PVsyst binaries, licenses, or proprietary project data are included.
"""
from __future__ import annotations

import csv
import locale
import math
import os
import re
import subprocess
import threading
from functools import wraps
from datetime import datetime
from pathlib import Path
from typing import Iterator

from pvsyst_auto.project.paths import project_name, project_root, safe_filename, variant_id, workspace_file, workspace_folder

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
    "shading": (*VERIFIED_VARIABLES, "viGlobShd", "viShdElec"),
    "soiling": (*VERIFIED_VARIABLES, "viGlobSlg", "viSlgLoss"),
    "grid_limit": (*VERIFIED_VARIABLES, "viPGridLim", "viEGrdLim", "viEUnused", "viEOutInv"),
    "grid_storage": (*VERIFIED_VARIABLES, "viE_User", "viEUseful", "viE_Miss", "viE_Avail",
                     "viEBatCh", "viEBatDis", "viEFrGrid", "viSOC_Beg", "viSOC_End",
                     "viEArrMPP", "viEDirUse", "viCL_Chrg", "viCL_InvB", "viCL_Over",
                     "viEUnused", "viEBatLss", "viE_Solar", "viSolFrac"),
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
# Weather operations report shifts, clamped values and validation errors as plain lines.
WEATHER_WARNING_RE = re.compile(
    r"\b(warn(?:ing)?s?|caution|shifted|too low|too high|invalid|error)\b", re.I)

# run-simulation input options in the exact argv order they are appended.
_RUN_SIM_INPUTS = (
    ("input-sfi-file", "isf", ".sfi"),
    ("input-rvt-file", "irf", ".rvt"),
    ("input-met-file", "imf", ".met"),
    ("input-param-file", "ipf", None),
    ("batch-params-file", "bpf", ".csv"),
    ("batch-rvt-file", "brf", ".rvt"),
    ("site", "s", ".sit"),
)


class PVsystError(RuntimeError):
    """The installed CLI rejected a request or failed to create an artifact."""

    def __init__(self, message: str, diagnostics: str | None = None):
        """`diagnostics` is the complete redacted stdout+stderr transcript of the
        failing native command: always a `str` when a weather operation captured it
        (never truncated, stdout first then stderr, same shape as the `diagnostics`
        string of that operation's successful details dict) or when a command timed
        out (its partial capture), otherwise `None`.
        """
        super().__init__(message)
        self.diagnostics = diagnostics


def validate_report_pages(pages):
    """Use the native page vocabulary at every entry point, before side effects."""
    if pages is not None and (not isinstance(pages, (list, tuple)) or not pages
            or any(not isinstance(page, str) or page not in REPORT_PAGES for page in pages)):
        raise ValueError("Unknown or empty PDF report page list")


def _is_plain_csv_run(args) -> bool:
    """True only for a bare hourly-CSV numeric simulation.

    ``run-simulation`` with an ``-ocf:`` CSV target and none of ``-rpf:`` (PDF),
    ``-bpf:`` (batch params) or ``-brf:`` (batch RVT). That path needs no window,
    so it is the only one allowed to deny DESKTOP_CREATEWINDOW; PDF and batch keep
    the old private-desktop behavior that may create windows.
    """
    if not args or args[0] != "run-simulation":
        return False
    has_csv = any(arg.startswith("-ocf:") for arg in args)
    keeps_windows = any(arg.startswith(("-rpf:", "-bpf:", "-brf:")) for arg in args)
    return has_csv and not keeps_windows


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


def _weather_warnings(text: str) -> list[str]:
    """Select the review-worthy lines of an already redacted weather transcript."""
    return [line.strip() for line in text.splitlines() if WEATHER_WARNING_RE.search(line)]


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
        # Historical entry: every call keeps its per-command default window policy.
        return self._launch(args, allow_window_creation=None)

    def _launch(self, args, *,
                allow_window_creation: bool | None) -> subprocess.CompletedProcess[str]:
        # Argument arrays preserve spaces in paths; serialize CLI operations on one workspace.
        argv = [str(self.cli), *args]
        with self._lock:
            if os.name == "nt":
                # Every real CLI call runs on its own private, non-input desktop
                # inside an owned Job; same argv/cwd/env/output/exit/timeout shape.
                # allow_window_creation=None keeps the per-command default: a bare
                # hourly-CSV numeric run denies DESKTOP_CREATEWINDOW on that desktop,
                # while PDF/batch/other commands keep the old behavior. An explicit
                # False always denies (never falls back to an allowing desktop); True
                # always allows. The value is per call and never stored globally.
                from pvsyst_auto.native import _winlaunch
                allow = ((not _is_plain_csv_run(args)) if allow_window_creation is None
                         else allow_window_creation)
                return _winlaunch.run(argv, self.cli.parent, self.timeout,
                                      allow_window_creation=allow)
            return subprocess.run(argv, cwd=self.cli.parent, capture_output=True,
                                  text=True, errors="replace", timeout=self.timeout,
                                  check=False)

    @staticmethod
    def _output(result: subprocess.CompletedProcess[str]) -> str:
        return (result.stdout or "") + "\n" + (result.stderr or "")

    @staticmethod
    def _redact(text: str) -> str:
        return "\n".join("[redacted]" if re.search(
            r"(?i)(license key|host id|customer.id|activation key)", line) else line
                         for line in text.splitlines())

    @staticmethod
    def _captured_text(value: str | bytes | None) -> str:
        """Normalize one captured stream of a TimeoutExpired.

        `run()` decodes normal results, but the timeout path can still hand back raw
        bytes (POSIX keeps the undecoded capture), so decode with the same encoding and
        error handling the CLI itself is read with.
        """
        if isinstance(value, bytes):
            return value.decode(locale.getpreferredencoding(False), errors="replace")
        return value or ""

    def _checked_run(self, label: str, *args: str,
                     allow_window_creation: bool | None = None) -> subprocess.CompletedProcess[str]:
        """Run one command; a timeout raises PVsystError, never the raw TimeoutExpired.

        TimeoutExpired carries the complete command line, so only the redacted partial
        capture leaves this adapter, attached as `diagnostics`.
        """
        try:
            if allow_window_creation is None:
                # Historical default path: unchanged call shape so any _run replacement
                # (tests, subclass) keeps working exactly as before.
                return self._run(*args)
            return self._launch(args, allow_window_creation=allow_window_creation)
        except subprocess.TimeoutExpired as exc:
            # subprocess.run fills TimeoutExpired stdout/stderr with the partial capture.
            captured = (self._captured_text(exc.stdout) + "\n"
                        + self._captured_text(exc.stderr))
            raise PVsystError(f"{label} timed out after {self.timeout}s",
                              diagnostics=self._redact(captured)) from None

    def _weather_command(self, label: str, args: list[str], *,
                         allow_window_creation: bool | None = None) -> str:
        """Run one weather command and return its complete redacted output.

        A timeout or non-zero exit raises PVsystError with that same output attached as
        `diagnostics`, so the exception message stays short.
        """
        result = self._checked_run(label, *args, allow_window_creation=allow_window_creation)
        text = self._redact(self._output(result))
        if result.returncode != 0:
            raise PVsystError(f"{label} failed with exit code {result.returncode}",
                              diagnostics=text)
        return text

    def command_help(self, command: str = "", *,
                     allow_window_creation: bool | None = None) -> str:
        if command not in self._help_cache:
            result = self._checked_run("PVsystCLI help", "help", *([command] if command else []),
                                       allow_window_creation=allow_window_creation)
            text = self._output(result).strip()
            if result.returncode != 0 or "Unknown command" in text:
                raise PVsystError(f"CLI does not support {command or 'help'}")
            self._help_cache[command] = text
        return self._help_cache[command]

    def _options(self, command: str, *, allow_window_creation: bool | None = None) -> set[str]:
        return set(re.findall(r"(?m)^\s*--([a-z][a-z-]+)(?=\||:|\s)",
                              self.command_help(command, allow_window_creation=allow_window_creation)))

    def capabilities(self, *, allow_window_creation: bool | None = None) -> dict[str, list[str]]:
        main = self.command_help(allow_window_creation=allow_window_creation)
        commands = re.findall(r"(?m)^\s+([a-z][a-z-]+):", main)
        return {name: sorted(self._options(name, allow_window_creation=allow_window_creation))
                for name in commands}

    def _require(self, command: str, option: str, *,
                 allow_window_creation: bool | None = None) -> None:
        if option not in self._options(command, allow_window_creation=allow_window_creation):
            raise PVsystError(f"Installed PVsystCLI lacks {command} --{option}")

    def version(self, *, allow_window_creation: bool | None = None) -> str:
        result = self._checked_run("PVsystCLI version query", "version",
                                   allow_window_creation=allow_window_creation)
        if result.returncode != 0:
            raise PVsystError("PVsystCLI version query failed")
        lines = self._output(result).strip().splitlines()
        if not lines:
            raise PVsystError("PVsystCLI version query returned no output")
        return lines[0]

    def license_info(self, *, allow_window_creation: bool | None = None) -> dict:
        result = self._checked_run("PVsystCLI license query", "lic-info",
                                   allow_window_creation=allow_window_creation)
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
        result = self._checked_run("Log export", "export-logs", f"-t:{target}")
        created = sorted((p for p in target.glob("*.zip") if p.name not in before),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if result.returncode != 0 or not created:
            raise PVsystError("Log export did not produce a new ZIP file")
        return created[0]

    def list_projects(self) -> list[str]:
        return sorted(p.name for p in project_root(self.workspace).glob("*.PRJ") if p.is_file())

    def list_variants(self, project: str) -> list[str]:
        # Canonical upper-case IDs, matching variant_id(), whatever the on-disk suffix case.
        stem = Path(self._project_name(project)).stem
        return sorted({p.suffix[1:].upper() for p in project_root(self.workspace).iterdir()
                       if p.stem.casefold() == stem.casefold() and p.is_file()
                       and re.fullmatch(r"VC[A-Za-z0-9]+", p.suffix[1:], re.I)})

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
    def _target_path(path: str | os.PathLike, suffix: str) -> Path:
        """Validate a new output path without creating its parent (pure check)."""
        item = Path(path).resolve()
        if item.suffix.lower() != suffix or item.exists():
            raise ValueError(f"Output must be a new {suffix} file")
        return item

    @staticmethod
    def _target(path: str | os.PathLike, suffix: str) -> Path:
        item = PVsystCLI._target_path(path, suffix)
        item.parent.mkdir(parents=True, exist_ok=True)
        return item

    @staticmethod
    def _log_level(value: int | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3:
            raise ValueError("Log level must be 0, 1, 2, or 3")
        return f"-ll:{value}"

    def _batch_outputs(self, batch_params: Path) -> tuple[list[str], dict[str, tuple[int, int]], Path]:
        """Validate hourly output names before a batch can write workspace files."""
        summary = workspace_file(self.workspace, "UserBatch",
                                 batch_summary_name(batch_params.name), ".csv")
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
        if summary.exists():
            raise ValueError(f"Batch summary already exists: {summary.name}")
        before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                  for p in (batch_dir.iterdir() if batch_dir.is_dir() else ())
                  if p.is_file() and is_batch_summary_name(p.name)}
        return names, before, summary

    def preflight_run_simulation(self, project: str, variant: str, *,
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
                                 log_level: int | None = None,
                                 allow_window_creation: bool | None = None) -> dict:
        """Pure pre-launch checks for run_simulation: no mkdir, no output, no child CLI.

        ``allow_window_creation`` is accepted only so the public call signature stays
        uniform with ``run_simulation``; this pure check launches nothing and so never
        selects a window policy itself.

        Everything decidable from the arguments and workspace alone is rejected here
        before any license query or help launch: project/variant existence, output
        requirement, batch conflicts, input existence+suffix, time step, dates, report
        pages, output collisions (no mkdir) and batch output names. The real license
        gate, option/help verification and the run stay in run_simulation, which calls
        this first and re-checks outputs right before executing (TOCTOU guard)."""
        project = self._project_name(project)
        variant = variant_id(variant)
        if project not in self.list_projects() or variant not in self.list_variants(project):
            raise ValueError("Project and variant must exist in the workspace")
        if batch_params and (out_csv is not None or report_pdf is not None):
            raise ValueError("Batch output is generated by the CLI in UserBatch/UserHourly")
        if out_csv is None and report_pdf is None and not batch_params:
            raise ValueError("Request a CSV and/or a PDF output")
        if batch_rvt and not batch_params:
            raise ValueError("Batch RVT requires a batch parameters file")
        if synthetic_weather and (not site or met):
            raise ValueError("Synthetic weather requires a site and no MET input")
        if time_step and time_step not in ("hour", "subhour"):
            raise ValueError("Time step must be hour or subhour")
        for value in (start_date, end_date):
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
        validate_report_pages(report_pages)
        self._log_level(log_level)
        values = {"input-sfi-file": sfi, "input-rvt-file": rvt, "input-met-file": met,
                  "input-param-file": params, "batch-params-file": batch_params,
                  "batch-rvt-file": batch_rvt, "site": site}
        inputs = {option: (self._input(values[option], ext) if values[option] else None)
                  for option, _flag, ext in _RUN_SIM_INPUTS}
        if out_csv:
            self._target_path(out_csv, ".csv")
        if report_pdf:
            self._target_path(report_pdf, ".pdf")
        if batch_params:
            self._batch_outputs(inputs["batch-params-file"])
        return {"project": project, "variant": variant, "inputs": inputs}

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
                       log_level: int | None = None,
                       allow_window_creation: bool | None = None) -> dict:
        # allow_window_creation selects the Native window policy for THIS request and is
        # threaded to every CLI process this call starts (option help, the run). None keeps
        # the historical per-command default; False constrains Native window creation for
        # this run (it never means every task is already certified GUI-free); True allows.
        pre = self.preflight_run_simulation(
            project, variant, sfi=sfi, out_csv=out_csv, met=met, report_pdf=report_pdf,
            rvt=rvt, params=params, batch_params=batch_params, batch_rvt=batch_rvt,
            site=site, synthetic_weather=synthetic_weather, time_step=time_step,
            start_date=start_date, end_date=end_date, report_language=report_language,
            report_pages=report_pages, recompute_shading=recompute_shading,
            log_level=log_level, allow_window_creation=allow_window_creation)
        project, variant, inputs = pre["project"], pre["variant"], pre["inputs"]
        args = ["run-simulation", f"-w:{self.workspace}", f"-p:{project}", f"-v:{variant}"]
        for option, flag, _ext in _RUN_SIM_INPUTS:
            path = inputs[option]
            if path:
                self._require("run-simulation", option, allow_window_creation=allow_window_creation)
                args.append(f"-{flag}:{path}")
        if synthetic_weather:
            self._require("run-simulation", "synthetic-weatherdata-generation",
                          allow_window_creation=allow_window_creation)
            args.append("-swg")
        if time_step:
            self._require("run-simulation", "time-step", allow_window_creation=allow_window_creation)
            args.append(f"-ts:{time_step}")
        for flag, value in (("sd", start_date), ("ed", end_date)):
            if value:
                self._require("run-simulation", "start-date" if flag == "sd" else "end-date",
                              allow_window_creation=allow_window_creation)
                args.append(f"-{flag}:{value}")
        if report_language:
            langs = re.search(r"Available language:\s*\[([^]]+)\]",
                              self.command_help("run-simulation", allow_window_creation=allow_window_creation))
            allowed = langs.group(1).split(", ") if langs else ["en", "fr", "de", "es", "it", "pt", "tr", "ko", "zh", "ja", "pl", "ru"]
            if report_language not in allowed:
                raise ValueError(f"Unsupported report language: {report_language}")
            args.append(f"-rl:{report_language}")
        if report_pages is not None:
            args.append("-rp:" + ",".join(report_pages))
        if recompute_shading is not None:
            self._require("run-simulation", "recompute-shadingfactors-tables",
                          allow_window_creation=allow_window_creation)
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
        batch_names, batch_before, batch_summary = (self._batch_outputs(inputs["batch-params-file"])
                                                    if batch_params else ([], {}, None))
        result = self._checked_run("Simulation", *args, allow_window_creation=allow_window_creation)
        text = self._output(result)
        if (result.returncode != 0
                or (log_level in (None, 3) and "Simulation done in " not in text)
                or (csv_target and not csv_target.is_file())
                or (pdf_target and not pdf_target.is_file())):
            raise PVsystError("Simulation did not complete or produce requested output: " + self._redact(text)[-3000:])
        summary = None
        hourly = []
        runs = None
        if batch_params:
            batch_dir = workspace_folder(self.workspace, "UserBatch")
            changed = [p for p in (batch_dir.iterdir() if batch_dir.is_dir() else ())
                       if p.is_file() and is_batch_summary_name(p.name)
                       and batch_before.get(p.name) !=
                       (p.stat().st_size, p.stat().st_mtime_ns)]
            # Only the summary named after the parameters file belongs to this run.
            if len(changed) != 1 or changed[0].name.casefold() != batch_summary.name.casefold():
                raise PVsystError("Batch simulation did not produce exactly one new summary CSV: "
                                  + batch_summary.name)
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
                "batch_hourly": [str(p) for p in hourly], "batch_runs": runs,
                "warnings": [line.strip() for line in self._redact(text).splitlines()
                             if re.search(r"\b(warn(?:ing)?s?|caution)\b", line, re.I)],
                "diagnostics": self._redact(text)[-12000:]}

    def convert_meteo(self, input_csv: str | os.PathLike, mef: str | os.PathLike,
                      sit: str | os.PathLike, out_met: str | os.PathLike,
                      timeshift: int = 0, log_level: int | None = None, *,
                      allow_window_creation: bool | None = None) -> Path:
        return Path(self.convert_meteo_details(input_csv, mef, sit, out_met, timeshift, log_level,
                                               allow_window_creation=allow_window_creation)["met"])

    @_serialized
    def convert_meteo_details(self, input_csv: str | os.PathLike, mef: str | os.PathLike,
                              sit: str | os.PathLike, out_met: str | os.PathLike,
                              timeshift: int = 0, log_level: int | None = None, *,
                              allow_window_creation: bool | None = None) -> dict:
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
        text = self._weather_command("Weather conversion", args, allow_window_creation=allow_window_creation)
        if "Conversion ended successfully" not in text or not target.is_file():
            raise PVsystError("Conversion did not produce the expected MET",
                              diagnostics=text)
        return {"met": str(target), "diagnostics": text,
                "warnings": _weather_warnings(text)}

    def create_site(self, name: str, latitude: float, longitude: float,
                    weather_csv: str | os.PathLike, out_sit: str | os.PathLike,
                    *, altitude: float | None = None,
                    timezone: float | None = None, country_code: str = "",
                    region: str = "", source: str = "", albedo: float | None = None,
                    log_level: int | None = None,
                    allow_window_creation: bool | None = None) -> Path:
        return Path(self.create_site_details(
            name, latitude, longitude, weather_csv, out_sit, altitude=altitude,
            timezone=timezone, country_code=country_code, region=region,
            source=source, albedo=albedo, log_level=log_level,
            allow_window_creation=allow_window_creation)["sit"])

    @_serialized
    def create_site_details(self, name: str, latitude: float, longitude: float,
                            weather_csv: str | os.PathLike, out_sit: str | os.PathLike,
                            *, altitude: float | None = None,
                            timezone: float | None = None, country_code: str = "",
                            region: str = "", source: str = "", albedo: float | None = None,
                            log_level: int | None = None,
                            allow_window_creation: bool | None = None) -> dict:
        self._require("create-site", "site-name", allow_window_creation=allow_window_creation)
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
        text = self._weather_command("Site creation", args, allow_window_creation=allow_window_creation)
        if not target.is_file():
            raise PVsystError("Site creation did not produce a SIT file", diagnostics=text)
        return {"sit": str(target), "diagnostics": text, "warnings": _weather_warnings(text)}


_BATCH_NAME = re.compile(r"(?P<prefix>.*?)(?P<kind>params|results)(?P<index>_\d+)?\.csv", re.IGNORECASE)


def batch_summary_name(params_filename: str) -> str:
    """Derive <prefix>Results[_<n>].CSV from a <prefix>Params[_<n>].CSV batch file."""
    match = _BATCH_NAME.fullmatch(params_filename)
    if match is None or match.group("kind").lower() != "params":
        raise ValueError("Batch parameters filename must be <name>Params[_<n>].CSV")
    return f"{match.group('prefix')}Results{match.group('index') or ''}.CSV"


def is_batch_summary_name(filename: str) -> bool:
    """True for the documented <prefix>Results[_<n>].CSV batch summary names."""
    match = _BATCH_NAME.fullmatch(filename)
    return match is not None and match.group("kind").lower() == "results"


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


_SFI_TIMESTAMP = re.compile(r"[0-9]{2}/[0-9]{2}/[0-9]{2} [0-9]{2}:[0-9]{2}")


def _result_timestamp(value):
    # Preserve strptime's 1969..2068 year window and its noncanonical fallback.
    if _SFI_TIMESTAMP.fullmatch(value):
        century = "19" if value[6:8] >= "69" else "20"
        return datetime.fromisoformat(
            f"{century}{value[6:8]}-{value[3:5]}-{value[:2]}T{value[9:]}")
    return datetime.strptime(value, "%d/%m/%y %H:%M")


def _iter_result_cells(path, *, strict=False):
    """Validate every timestamp and row shape before projecting numeric cells."""
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
                stamp = _result_timestamp(parts[0].strip())
            except ValueError:
                if strict:
                    raise ValueError("Invalid timestamp or unexpected row after SFI header") from None
                continue
            if strict and len(parts) > len(headers):
                raise ValueError("Result row contains more values than its header")
            parts += [""] * max(0, len(headers) - len(parts))
            seen = True
            yield headers, stamp, parts[1:len(headers)]
    if not seen:
        raise ValueError("Result CSV contains no simulation rows")


def _result_number(value):
    try:
        return float(value.strip())
    except ValueError:
        return math.nan


def iter_result_csv(path: str | os.PathLike, *, strict: bool = False) -> Iterator[tuple[list[str], list]]:
    """Yield complete numeric SFI rows without loading the full file."""
    for headers, stamp, values in _iter_result_cells(path, strict=strict):
        row = [stamp]
        for value in values:
            try:
                row.append(float(value.strip()))
            except ValueError:
                row.append(math.nan)
        yield headers, row


def read_result_page(path, columns, offset, limit):
    """Count all timestamped rows; convert only the requested page's columns."""
    indices = None
    total = 0
    page = []
    for headers, stamp, values in _iter_result_cells(path):
        if indices is None:
            if not columns or any(name not in headers[1:] for name in columns):
                raise ValueError(f"Choose columns from {headers[1:]}")
            indices = [headers.index(name) - 1 for name in columns]
        if offset <= total < offset + limit:
            selected = [_result_number(values[index]) for index in indices]
            page.append([str(stamp), *(value if math.isfinite(value) else None
                                       for value in selected)])
        total += 1
    units = result_units(path)
    return {"total": total, "offset": offset, "columns": ["date", *columns],
            "units": {name: units.get(name, "") for name in columns}, "rows": page}


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
    for headers, stamp, values in _iter_result_cells(path):
        if indices is None:
            if not columns or any(name not in headers[1:] for name in columns):
                raise ValueError(f"Choose columns from {headers[1:]}")
            indices = {name: headers.index(name) - 1 for name in columns}
            first = str(stamp)
        if previous is not None:
            delta = (stamp - previous).total_seconds() / 60
            if delta <= 0 or (step_minutes is not None and delta != step_minutes):
                regular = False
            elif step_minutes is None:
                step_minutes = delta
        previous = stamp
        count += 1
        last = str(stamp)
        for name, index in indices.items():
            value = _result_number(values[index])
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

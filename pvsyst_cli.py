"""Small Windows adapter for PVsystCLI 8.0.6.

Only the command options confirmed by `PVsystCLI.exe help <command>` are used.
PVsyst and PVsystCLI are trademarks of their respective owner; this is
an independent integration, not a copy of the application.
"""
from __future__ import annotations

import csv
import math
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

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


class PVsystError(RuntimeError):
    """PVsystCLI invocation or output validation failed."""


def build_sfi(path: str | os.PathLike, variables: list[str] | str,
              comment: str = "Hourly Results") -> Path:
    """Generate an hourly SFI definition using a verified group or vi* names."""
    if isinstance(variables, str):
        variables = list(VAR_GROUPS[variables])
    if not variables or any(not re.fullmatch(r"vi[A-Za-z0-9_]+", v) for v in variables):
        raise ValueError("Expected a nonempty list of PVsyst vi* identifiers")
    if "\n" in comment or "\r" in comment or not comment.strip():
        raise ValueError("SFI comment must be one nonempty line")
    target = Path(path)
    if target.suffix.lower() != ".sfi":
        raise ValueError("SFI destination must end in .sfi")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8", newline="\n") as file:
        file.write(SFI_TEMPLATE.format(comment=comment, variables=",".join(variables)))
    return target


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

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        # No shell: argument boundaries survive whitespace in Windows paths.
        result = subprocess.run([str(self.cli), *args], cwd=self.cli.parent,
                                capture_output=True, text=True, errors="replace",
                                timeout=self.timeout, check=False)
        return result

    @staticmethod
    def _output(result: subprocess.CompletedProcess[str]) -> str:
        return (result.stdout or "") + "\n" + (result.stderr or "")

    def license_info(self) -> dict:
        result = self._run("lic-info")
        text = self._output(result)
        if result.returncode != 0:
            raise PVsystError(f"lic-info exited {result.returncode}: {text.strip()}")
        info: dict = {}
        for key, value in re.findall(r"(?m)^\s*(Status|Remaining days|Remaining executions):\s*([^\r\n]+)", text):
            info[{"Status": "status", "Remaining days": "remaining_days",
                  "Remaining executions": "remaining_executions"}[key]] = (
                      value.strip() if key == "Status" else int(value.strip()))
        if "status" not in info:
            raise PVsystError(f"lic-info returned no status: {text.strip()}")
        # Host ID and raw stdout are deliberately excluded from MCP responses.
        return info

    def list_projects(self) -> list[str]:
        return sorted(p.name for p in (self.workspace / "Projects").glob("*.PRJ") if p.is_file())

    def list_variants(self, project: str) -> list[str]:
        name = self._project_name(project)
        stem = Path(name).stem
        return sorted(p.suffix[1:] for p in (self.workspace / "Projects").glob(stem + ".VC*")
                      if p.is_file() and re.fullmatch(r"VC[A-Za-z0-9]+", p.suffix[1:]))

    @staticmethod
    def _project_name(project: str) -> str:
        if (not project or Path(project).name != project or "\\" in project
                or project in (".", "..") or not project.upper().endswith(".PRJ")):
            raise ValueError("Project must be a .PRJ filename in the workspace")
        return project

    def run_simulation(self, project: str, variant: str, *, sfi: str | os.PathLike,
                       out_csv: str | os.PathLike, met: str | os.PathLike | None = None,
                       report_pdf: str | os.PathLike | None = None) -> dict:
        project = self._project_name(project)
        if project not in self.list_projects() or variant not in self.list_variants(project):
            raise ValueError("Project and variant must exist in the workspace")
        sfi_path = Path(sfi).resolve(strict=True)
        if not sfi_path.is_file():
            raise ValueError("SFI input must be a file")
        target = Path(out_csv).resolve()
        if target.suffix.lower() != ".csv" or target.exists():
            raise ValueError("CSV output must be a new .csv file")
        target.parent.mkdir(parents=True, exist_ok=True)
        args = ["run-simulation", f"-w:{self.workspace}", f"-p:{project}",
                f"-v:{variant}", f"-isf:{sfi_path}", f"-ocf:{target}"]
        if met:
            met_path = Path(met).resolve(strict=True)
            if not met_path.is_file():
                raise ValueError("MET input must be a file")
            args.append(f"-imf:{met_path}")
        pdf_path = None
        if report_pdf:
            pdf_path = Path(report_pdf).resolve()
            if pdf_path.suffix.lower() != ".pdf" or pdf_path.exists():
                raise ValueError("PDF output must be a new .pdf file")
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            args.append(f"-rpf:{pdf_path}")
        result = self._run(*args)
        text = self._output(result)
        if result.returncode != 0 or "Simulation done in " not in text or not target.is_file():
            raise PVsystError(f"Simulation did not produce the expected CSV: {text.strip()}")
        if pdf_path is not None and not pdf_path.is_file():
            raise PVsystError("Simulation completed without the requested PDF")
        timing = re.search(r"Simulation done in (\d+)min\s*(\d+)sec", text)
        return {"csv": str(target), "pdf": str(pdf_path) if pdf_path else None,
                "seconds": int(timing[1]) * 60 + int(timing[2]) if timing else None}

    def convert_meteo(self, input_csv: str | os.PathLike, mef: str | os.PathLike,
                      sit: str | os.PathLike, out_met: str | os.PathLike,
                      timeshift: int = 0) -> Path:
        if not -30 <= timeshift <= 30:
            raise ValueError("MEF timeshift must be between -30 and 30")
        sources = [Path(p).resolve(strict=True) for p in (input_csv, mef, sit)]
        if not all(p.is_file() for p in sources):
            raise ValueError("CSV, MEF and SIT inputs must be files")
        target = Path(out_met).resolve()
        if target.suffix.lower() != ".met" or target.exists():
            raise ValueError("MET output must be a new .met file")
        target.parent.mkdir(parents=True, exist_ok=True)
        result = self._run("convert-meteo", f"-icf:{sources[0]}", f"-imf:{sources[1]}",
                           f"-isf:{sources[2]}", f"-omf:{target}", f"-imt:{timeshift}")
        text = self._output(result)
        if result.returncode != 0 or "Conversion ended successfully" not in text or not target.is_file():
            raise PVsystError(f"Conversion did not produce the expected MET: {text.strip()}")
        return target


def parse_result_csv(path: str | os.PathLike) -> tuple[list[str], list[list]]:
    """Parse an hourly PVsyst SFI CSV; return headers and [datetime, float...] rows."""
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as stream:
        for line in stream:
            if line.lstrip().lower().startswith("date;"):
                headers = next(csv.reader([line], delimiter=";"))
                next(stream, None)  # units row
                next(stream, None)  # blank separator
                break
        else:
            raise ValueError("Missing SFI result header (date;...)")
        if len(headers) < 2:
            raise ValueError("SFI result has no numeric columns")
        rows: list[list] = []
        for parts in csv.reader(stream, delimiter=";"):
            if len(parts) < len(headers):
                continue
            try:
                stamp = datetime.strptime(parts[0].strip(), "%d/%m/%y %H:%M")
            except ValueError:
                continue
            try:
                row = [stamp] + [float(v.strip()) for v in parts[1:len(headers)]]
            except ValueError:
                continue
            rows.append(row)
    if not rows:
        raise ValueError("Result CSV contains no simulation rows")
    return [h.strip() for h in headers], rows


def summarize(path: str | os.PathLike, column: str = "E_Grid") -> dict:
    """Summarize one numeric column. `sum` is a sample sum, not energy units."""
    headers, rows = parse_result_csv(path)
    if column not in headers[1:]:
        raise ValueError(f"Column {column!r} not present; available: {headers[1:]}")
    values = [row[headers.index(column)] for row in rows]
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        raise ValueError(f"Column {column!r} contains no finite values")
    return {"column": column, "samples": len(finite), "sum": sum(finite),
            "mean": sum(finite) / len(finite), "max": max(finite)}

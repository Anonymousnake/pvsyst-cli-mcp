"""Compare independent native CSVs, or execute one budgeted PVsyst acceptance case."""
from __future__ import annotations

import argparse
import csv
import ctypes
from contextlib import closing
from datetime import date, datetime, time, timedelta
from itertools import zip_longest
import json
import math
import os
from pathlib import Path
import shutil
import sys
from zipfile import ZipFile, ZIP_DEFLATED

if os.environ.get("PVSYST_TEST_INSTALLED") != "1" and not os.environ.get("PVSYST_PACKAGE_INSTALLED_PYTHON"):
    _src = str(Path(__file__).resolve().parents[1] / "src")
    if _src not in sys.path:
        sys.path.insert(0, _src)

from pvsyst_auto.native.cli import PVsystCLI, iter_result_csv, result_units
from pvsyst_auto.project.paths import workspace_file


SUPPORTED_VERSIONS = {"8.1.6.49278", "8.1.7.49382"}
CONTEXT = {"Project", "Simulation variant", "Geographical Site", "Weather data"}


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(required) - value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError("Missing or unknown fields: " + ", ".join(required))
    return value


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Require a nonempty string")
    return value


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Tolerances must be finite nonnegative numbers")
    return value


def document(path, data=None):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field: " + key)
            result[key] = value
        return result
    data = path.read_bytes() if data is None else data
    return json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique)


def resolve_file(parent, name):
    path = (parent / text(name)).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Require a regular file: " + str(path))
    return path


def load_case(path):
    raw = path.read_bytes()
    case = fields(document(path, raw), ("schema_version", "case_id", "feature", "native_version",
                                  "workspace", "fixture_provenance", "input_equivalence",
                                  "baseline", "period", "columns", "request", "context"))
    if type(case["schema_version"]) is not int or case["schema_version"] != 1:
        raise ValueError("Unsupported acceptance schema version")
    for name in ("case_id", "feature", "fixture_provenance"):
        text(case[name])
    baseline = fields(case["baseline"], ("csv", "producer", "version", "provenance", "context"))
    if case["native_version"] not in SUPPORTED_VERSIONS or baseline["version"] not in SUPPORTED_VERSIONS:
        raise ValueError("Unknown native version; no acceptance claim is inherited")
    if baseline["producer"] not in ("native_gui", "native_cli"):
        raise ValueError("Reference must be an independently produced native artifact")
    text(baseline["provenance"])
    evidence = fields(case["input_equivalence"], ("basis", "evidence"))
    text(evidence["basis"])
    case["evidence_path"] = resolve_file(path.parent, evidence["evidence"])
    case["baseline_path"] = resolve_file(path.parent, baseline["csv"])
    workspace = (path.parent / text(case["workspace"])).resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("Fixture workspace is not a directory")
    case["workspace_path"] = workspace
    period = fields(case["period"], ("start", "end"))
    start, end = (date.fromisoformat(text(period[key])) for key in ("start", "end"))
    if (start.isoformat() != period["start"] or end.isoformat() != period["end"]
            or not date(1969, 1, 1) <= start < end <= date(2069, 1, 1)):
        raise ValueError("Require a valid ISO date period in the native two-digit-year window")
    case["start"], case["end"] = start, end
    columns = case["columns"]
    if not isinstance(columns, dict) or not 2 <= len(columns) <= 64:
        raise ValueError("Declare energy and loss columns explicitly")
    for name, options in columns.items():
        text(name)
        fields(options, ("unit", "role", "abs_tol", "rel_tol", "total_abs_tol", "total_rel_tol"))
        text(options["unit"])
        if options["role"] not in ("energy", "loss", "auxiliary"):
            raise ValueError("Column role must be energy, loss or auxiliary")
        if options["role"] == "energy" and options["unit"] not in ("W", "kW", "Wh", "kWh"):
            raise ValueError("Energy columns require explicit W, kW, Wh or kWh units")
        for key in ("abs_tol", "rel_tol", "total_abs_tol", "total_rel_tol"):
            finite(options[key])
    if any(not any(v["role"] == role for v in columns.values()) for role in ("energy", "loss")):
        raise ValueError("Declare at least one energy role and one loss role")
    request = fields(case["request"], ("project", "variant", "sfi_name"), ("met_name",))
    for value in request.values():
        text(value)
    for context in (case["context"], baseline["context"]):
        fields(context, CONTEXT)
        for value in context.values():
            text(value)
    if (case["context"]["Project"] != request["project"]
            or case["context"]["Simulation variant"] != Path(request["project"]).stem + "." + request["variant"]):
        raise ValueError("Candidate CSV context must identify the requested project and variant")
    case["case_path"], case["case_bytes"] = path, raw
    case["protected_bytes"] = {p: p.read_bytes() for p in (case["baseline_path"], case["evidence_path"])}
    case["protected_bytes"][path] = raw
    return case


def check_context(path, expected):
    def parse_lines(encoding):
        found = {}
        with path.open(encoding=encoding, errors="strict", newline="") as stream:
            for line in stream:
                if line.lstrip().lower().startswith(("date;", "date,")):
                    break
                separator = ";" if ";" in line else ","
                cells = next(csv.reader([line], delimiter=separator))
                if cells and cells[0].strip() in CONTEXT:
                    key = cells[0].strip()
                    if key in found or len(cells) < 2:
                        raise ValueError("Ambiguous CSV context: " + key)
                    found[key] = cells[1].strip()
        return found

    with path.open("rb") as probe:
        has_bom = probe.read(3) == b"\xef\xbb\xbf"
    try:
        actual = parse_lines("utf-8-sig")
    except UnicodeDecodeError:
        if has_bom:
            raise
        actual = parse_lines("cp1252")
    if actual != expected:
        raise ValueError("CSV context differs from the declared native run: " + str(path))


def result_rows(case, path, context):
    check_context(path, context)
    columns = case["columns"]
    units = result_units(path, strict=True)
    if any(units.get(name) != options["unit"] for name, options in columns.items()):
        raise ValueError("Selected CSV units differ from the explicit declaration")
    begin = datetime.combine(case["start"], time())
    expected_rows = (case["end"] - case["start"]).days * 24
    count, totals = 0, dict.fromkeys(columns, 0.0)
    with closing(iter_result_csv(path, strict=True)) as source:
        for headers, row in source:
            if count >= expected_rows or row[0] != begin + timedelta(hours=count):
                raise ValueError("Native CSV coverage must match every declared hourly timestamp")
            if len(headers) != len(set(headers)) or any(name not in headers[1:] for name in columns):
                raise ValueError("Native CSV requires unique selected numeric columns")
            values = {name: row[headers.index(name)] for name in columns}
            for name, value in values.items():
                totals[name] += value
                if not math.isfinite(value) or not math.isfinite(totals[name]):
                    raise ValueError("Missing/nonfinite or overflowing selected samples: " + name)
            count += 1
            yield values
    if count != expected_rows:
        raise ValueError("Native CSV coverage is incomplete")


def compare(case, candidate):
    baseline = case["baseline_path"]
    if candidate.samefile(baseline):
        raise ValueError("Candidate and independent reference must be different files")
    originals = dict(case["protected_bytes"])
    originals[candidate] = candidate.read_bytes()
    columns = case["columns"]
    states = {name: {"reference_sum": 0.0, "candidate_sum": 0.0,
                     "max_abs_delta": 0.0, "outside_tolerance": 0} for name in columns}
    count = 0
    left = result_rows(case, baseline, case["baseline"]["context"])
    right = result_rows(case, candidate, case["context"])
    try:
        for pair in zip_longest(left, right):
            if None in pair:
                raise ValueError("Native CSV row counts differ")
            a, b = pair
            for name, limits in columns.items():
                va, vb = a[name], b[name]
                if not math.isfinite(va) or not math.isfinite(vb) or not math.isfinite(vb - va):
                    raise ValueError("Missing/nonfinite selected sample: " + name)
                state = states[name]
                state["reference_sum"] += va
                state["candidate_sum"] += vb
                state["max_abs_delta"] = max(state["max_abs_delta"], abs(vb - va))
                state["outside_tolerance"] += not math.isclose(
                    va, vb, abs_tol=limits["abs_tol"], rel_tol=limits["rel_tol"])
            count += 1
    finally:
        left.close()
        right.close()
    for name, state in states.items():
        limits = columns[name]
        a, b = state["reference_sum"], state["candidate_sum"]
        if not math.isfinite(a) or not math.isfinite(b) or not math.isfinite(b - a):
            raise ValueError("Native CSV aggregate exceeds finite numeric range")
        state.update(unit=limits["unit"], role=limits["role"], delta_sum=b - a,
                     total_within_tolerance=math.isclose(a, b, abs_tol=limits["total_abs_tol"],
                                                        rel_tol=limits["total_rel_tol"]))
        scale = {"W": 0.001, "Wh": 0.001, "kW": 1.0, "kWh": 1.0}.get(limits["unit"])
        if scale is not None:
            state.update(reference_energy_kwh=a * scale, candidate_energy_kwh=b * scale)
    if any(path.read_bytes() != before for path, before in originals.items()):
        raise ValueError("Reference, candidate or equivalence evidence changed during comparison")
    return {"passed": all(v["outside_tolerance"] == 0 and v["total_within_tolerance"] for v in states.values()),
            "rows": count, "columns": states, "reference_provenance": "caller_declared_not_attested"}


def save_bytes(path, value):
    with path.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return True
    st = path.lstat()
    return getattr(st, "st_reparse_tag", 0) in (0xA0000003, 0xA000000C)


def workspace_bytes(root):
    # Fixture-sized byte snapshots; stream/archive if measured size warrants it.
    root_path = Path(root)
    resolved_root = root_path.resolve(strict=True)
    if _is_link_or_junction(root_path):
        raise ValueError("Fixture links and junctions must be resolved into a standalone copy")
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if _is_link_or_junction(path) or not path.resolve(strict=True).is_relative_to(resolved_root):
                raise ValueError("Fixture links and junctions must be resolved into a standalone copy")
        for name in files:
            path = Path(directory) / name
            result[path.relative_to(root)] = path.read_bytes()
    return result


def file_version(path):
    if os.name != "nt":
        raise ValueError("Native execution requires Windows")
    from ctypes import wintypes
    library = ctypes.WinDLL("version", use_last_error=True)
    library.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    library.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    library.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    library.GetFileVersionInfoW.restype = wintypes.BOOL
    library.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    library.VerQueryValueW.restype = wintypes.BOOL
    handle = wintypes.DWORD()
    size = library.GetFileVersionInfoSizeW(str(path), ctypes.byref(handle))
    if not size:
        raise ValueError("Native executable has no readable file-version resource")
    data = ctypes.create_string_buffer(size)
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if (not library.GetFileVersionInfoW(str(path), 0, size, data)
            or not library.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length))
            or length.value < 13 * ctypes.sizeof(wintypes.DWORD)):
        raise ValueError("Native executable file-version resource is invalid")
    values = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    if values[0] != 0xFEEF04BD:
        raise ValueError("Native executable file-version signature is invalid")
    return ".".join(str(v) for v in (values[2] >> 16, values[2] & 65535,
                                    values[3] >> 16, values[3] & 65535))


class BudgetedCLI(PVsystCLI):
    """Observe the unchanged launcher; spend command slots before every launch attempt."""

    def __init__(self, executable, workspace, attempt, report, maximum):
        super().__init__(executable, workspace)
        self.attempt, self.report, self.maximum = attempt, report, maximum

    def _launch(self, args, *, allow_window_creation):
        if allow_window_creation is not False:
            raise ValueError("Acceptance execution requires an explicit no-window request")
        if self.report["cli_commands_spent"] >= self.maximum:
            raise ValueError("Native command budget exhausted; no automatic retry")
        self.report["cli_commands_spent"] += 1
        number = self.report["cli_commands_spent"]
        save(self.attempt / f"native-{number:02d}-intent.json", {"args": list(args), "spent": True})
        try:
            result = super()._launch(args, allow_window_creation=False)
        except BaseException as exc:
            save(self.attempt / f"native-{number:02d}-error.json",
                 {"type": type(exc).__name__, "message": self._redact(str(exc)),
                  "stdout": self._redact(self._captured_text(getattr(exc, "stdout", None))),
                  "stderr": self._redact(self._captured_text(getattr(exc, "stderr", None)))})
            raise
        save(self.attempt / f"native-{number:02d}-result.json",
             {"returncode": result.returncode, "stdout": self._redact(result.stdout or ""),
              "stderr": self._redact(result.stderr or ""), "streams_redacted": True})
        return result


def execute(case, args, attempt, report):
    executable = Path(text(args.cli)).resolve(strict=True)
    executable_bytes = executable.read_bytes()
    observed = file_version(executable)
    report["observed_native_version"] = observed
    if observed not in SUPPORTED_VERSIONS or observed != case["native_version"]:
        raise ValueError("Native executable version differs from the declared supported version")
    if type(args.max_cli_commands) is not int or not 1 <= args.max_cli_commands <= 3:
        raise ValueError("Explicit native command budget must be in 1..3")
    source = case["workspace_path"]
    if attempt.is_relative_to(source) or source.is_relative_to(attempt):
        raise ValueError("Attempt output must be separate from the source workspace")
    protected = dict(case["protected_bytes"])
    protected[executable] = executable_bytes
    before = workspace_bytes(source)
    working = attempt / "PVsyst8.1_Data"
    try:
        save_bytes(attempt / "reference.csv", protected[case["baseline_path"]])
        save_bytes(attempt / "input-equivalence.bin", protected[case["evidence_path"]])
        with ZipFile(attempt / "source-before.zip", "x", ZIP_DEFLATED) as archive:
            for relative, data in before.items():
                archive.writestr(relative.as_posix(), data)
        shutil.copytree(source, working, symlinks=True)
        if workspace_bytes(working) != before:
            raise ValueError("Isolated workspace copy differs from its source")
        request = case["request"]
        cli = BudgetedCLI(executable, working, attempt, report, args.max_cli_commands)
        target = working / "Results" / "native-acceptance.csv"
        options = {"sfi": workspace_file(working, "Models", request["sfi_name"], ".sfi"),
                   "out_csv": target, "time_step": "hour", "recompute_shading": True,
                   "start_date": case["start"].strftime("%Y.%m.%d"),
                   "end_date": (case["end"] - timedelta(days=1)).strftime("%Y.%m.%d"),
                   "allow_window_creation": False}
        if "met_name" in request:
            options["met"] = workspace_file(working, "Meteo", request["met_name"], ".met")
        cli.preflight_run_simulation(request["project"], request["variant"], **options)
        if any(path.read_bytes() != data for path, data in protected.items()):
            raise ValueError("Protected input changed before native execution")
        licence = cli.license_info(allow_window_creation=False)
        if licence.get("status") == "TRIAL" and licence.get("remaining_executions") == 0:
            raise ValueError("Native trial reports zero remaining executions")
        native = cli.run_simulation(request["project"], request["variant"], **options)
        report["native_completed"] = True
        save(attempt / "native-return.json", native)
        report["comparison"] = compare(case, target)
        report["native_consumption_verified"] = report["comparison"]["passed"]
        report["status"] = ("passed_native" if report["comparison"]["passed"] else "failed")
        if native["warnings"] and report["status"] == "passed_native":
            report["status"] = "needs_review"
    finally:
        unwinding = sys.exc_info()[0] is not None
        for key, check in (("source_preserved", lambda: workspace_bytes(source) == before),
                           ("protected_files_preserved", lambda: all(
                               path.read_bytes() == data for path, data in protected.items()))):
            try:
                report[key] = check()
            except Exception as exc:
                report[key] = False
                report.setdefault("integrity_errors", []).append(str(exc))
        if not report["source_preserved"] or not report["protected_files_preserved"]:
            report.update(status="failed", native_consumption_verified=False)
            if not unwinding:
                raise ValueError("Protected source, reference, evidence, case or executable changed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("compare", "run"))
    parser.add_argument("case", type=Path)
    parser.add_argument("--candidate", type=Path, help="Existing candidate CSV for comparison only")
    parser.add_argument("--cli", help="Installed PVsystCLI.exe; used only in explicit run mode")
    parser.add_argument("--attempt", type=Path, help="NEW evidence directory, required for run mode")
    parser.add_argument("--attempt-id", help="Pre-registered request identifier, required for run mode")
    parser.add_argument("--max-cli-commands", type=int, help="Explicit 1..3 command slots, required for run mode")
    args = parser.parse_args()
    report = {"schema_version": 1, "status": "not_run", "mode": args.mode,
              "native_completed": False, "native_consumption_verified": False,
              "request_budget_spent": 0, "cli_commands_spent": 0,
              "persistence_acceptance": "not_run", "reference_provenance": "caller_declared_not_attested"}
    attempt = None
    if args.mode == "run":
        report.update(attempt_id=args.attempt_id, request_budget_spent=1)
    try:
        case = load_case(args.case.resolve(strict=True))
        if args.mode == "run":
            if args.attempt is None or not args.attempt_id or not args.attempt_id.strip():
                raise ValueError("Run mode requires a fresh attempt directory and pre-registered attempt ID")
            if args.candidate is not None:
                raise ValueError("Run mode accepts only its fresh native-produced candidate")
            target = args.attempt.resolve()
            source = case["workspace_path"]
            if target.is_relative_to(source) or source.is_relative_to(target):
                raise ValueError("Attempt output must be separate from the source workspace")
            if type(args.max_cli_commands) is not int or not 1 <= args.max_cli_commands <= 3:
                raise ValueError("Explicit native command budget must be in 1..3")
            executable = Path(text(args.cli)).resolve(strict=True)
            if not executable.is_file():
                raise ValueError("Require a regular file: " + str(executable))
            target.mkdir(parents=True, exist_ok=False)
            attempt = target
            save(attempt / "budget-spent.json", dict(report, max_cli_commands=args.max_cli_commands))
        report.update(case_id=case["case_id"], feature=case["feature"],
                      case_source=str(case["case_path"]), fixture_source=str(case["workspace_path"]),
                      reference_source=str(case["baseline_path"]),
                      declared_native_version=case["native_version"])
        if args.mode == "compare":
            if args.candidate is None or any(value is not None for value in
                    (args.cli, args.attempt, args.attempt_id, args.max_cli_commands)):
                raise ValueError("Compare mode needs a candidate CSV and accepts no execution options")
            report["comparison"] = compare(case, args.candidate.resolve(strict=True))
            report["status"] = "compared" if report["comparison"]["passed"] else "failed"
        else:
            save_bytes(attempt / "case.json", case["case_bytes"])
            for _ in result_rows(case, case["baseline_path"], case["baseline"]["context"]):
                pass
            execute(case, args, attempt, report)
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="failed", error={"code": "ACCEPTANCE_FAILED", "type": type(exc).__name__,
                                              "message": str(exc)}, native_consumption_verified=False)
    finally:
        if attempt is not None and report["request_budget_spent"]:
            save(attempt / "report.json", report)
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report["status"] in ("compared", "passed_native") else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the portable public contracts against an installed package, without PVsyst."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import sysconfig
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Write the actual unittest outcome as JSON")
    args = parser.parse_args()
    for name in tuple(os.environ):
        if name.startswith("PVSYST_"):
            del os.environ[name]
    os.environ.pop("PYTHONPATH", None)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    console = Path(sysconfig.get_path("scripts")) / (
        "pvsyst-mcp.exe" if os.name == "nt" else "pvsyst-mcp")
    if not console.is_file():
        parser.error("Install the built wheel in this Python environment before running contracts")
    os.environ["PVSYST_PACKAGE_INSTALLED_PYTHON"] = sys.executable
    os.environ["PVSYST_PACKAGE_INSTALLED_CONSOLE"] = str(console)
    contracts = Path(__file__).resolve().parents[1] / "tests" / "contracts"
    offline = contracts / "offline"
    sys.path.insert(0, str(contracts))
    suite = unittest.defaultTestLoader.discover(str(offline))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.wasSuccessful() and result.testsRun > 0 and not result.skipped
    report = {"schema_version": 1, "status": "passed" if passed else "failed",
              "suite": "portable_public_contracts", "python": sys.version,
              "tests_run": result.testsRun, "failures": len(result.failures),
              "errors": len(result.errors), "skipped": len(result.skipped),
              "native_acceptance": "not_run",
              "selected_contracts": [path.relative_to(contracts).as_posix()
                                     for path in sorted(offline.glob("test_*.py"))]}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

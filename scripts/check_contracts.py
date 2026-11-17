"""Run the portable public contracts against an installed package, without PVsyst."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import sysconfig
import unittest


CONTRACTS = (
    "tests.contracts.test_mcp_entrypoint_and_groups.PackageEntrypointBoundary",
    "tests.contracts.test_mcp_entrypoint_and_groups.ToolGroupsBoundary",
    "tests.contracts.test_mcp_entrypoint_and_groups.RunSimulationPreflightNoLaunchBoundary",
    "tests.contracts.test_mcp_core_boundaries.McpCoreBoundaries",
    "tests.contracts.test_variant_contracts.VariantContracts",
    "tests.contracts.test_scene_and_component_contracts.SceneAndComponentContracts",
    "tests.contracts.test_native_acceptance_cli.NativeAcceptanceCLIContract",
)


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
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    suite = unittest.defaultTestLoader.loadTestsFromNames(CONTRACTS)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.wasSuccessful() and result.testsRun > 0 and not result.skipped
    report = {"schema_version": 1, "status": "passed" if passed else "failed",
              "suite": "portable_public_contracts", "python": sys.version,
              "tests_run": result.testsRun, "failures": len(result.failures),
              "errors": len(result.errors), "skipped": len(result.skipped),
              "native_acceptance": "not_run", "selected_contracts": CONTRACTS}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the offline integration suite against the installed package, without PVsyst."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import sysconfig
import unittest


INTEGRATION_SUITES = (
    "tests.integration.test_mcp_entrypoint.PackageEntrypointTests",
    "tests.integration.test_mcp_entrypoint.ToolGroupsTests",
    "tests.integration.test_mcp_entrypoint.SimulationPreflightNoLaunchTests",
    "tests.integration.test_mcp_tools.McpToolIntegrationTests",
    "tests.integration.test_variant_io.VariantIOTests",
    "tests.integration.test_scene_components.SceneComponentTests",
    "tests.integration.test_acceptance_cli.AcceptanceCLITests",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Write the unittest summary as JSON")
    args = parser.parse_args()
    for name in tuple(os.environ):
        if name.startswith("PVSYST_"):
            del os.environ[name]
    os.environ.pop("PYTHONPATH", None)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    console = Path(sysconfig.get_path("scripts")) / (
        "pvsyst-mcp.exe" if os.name == "nt" else "pvsyst-mcp")
    if not console.is_file():
        parser.error("Install the built wheel in this Python environment before running CI checks")
    os.environ["PVSYST_PACKAGE_INSTALLED_PYTHON"] = sys.executable
    os.environ["PVSYST_PACKAGE_INSTALLED_CONSOLE"] = str(console)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    suite = unittest.defaultTestLoader.loadTestsFromNames(INTEGRATION_SUITES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.wasSuccessful() and result.testsRun > 0 and not result.skipped
    report = {
        "schema_version": 1,
        "status": "passed" if passed else "failed",
        "suite": "integration",
        "python": sys.version,
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "selected_suites": INTEGRATION_SUITES,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

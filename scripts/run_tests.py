"""Split test runner so changes only execute the relevant slice."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]

SUITES = {
    "core": ["tests/unit/core"],
    "energy": ["tests/unit/energy"],
    "weather": ["tests/unit/weather"],
    "workflows": ["tests/unit/workflows"],
    "unit": ["tests/unit"],
    "mcp": [
        "tests/integration/test_mcp_tools.py",
        "tests/integration/test_variant_io.py",
        "tests/integration/test_scene_components.py",
    ],
    "integration": ["tests/integration"],
    "e2e": ["tests/e2e"],
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=(*SUITES, "all"),
        default="mcp",
        help="Test slice to execute (default: mcp fast in-process slice)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose unittest output")
    args = parser.parse_args(argv)

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    targets = ["tests/unit", "tests/integration"] if args.suite == "all" else SUITES[args.suite]
    loader = unittest.TestLoader()
    combined = unittest.TestSuite()
    for target in targets:
        path = REPO / target
        if path.is_file():
            module = ".".join(path.with_suffix("").relative_to(REPO).parts)
            combined.addTests(loader.loadTestsFromName(module))
        else:
            combined.addTests(loader.discover(str(path), top_level_dir=str(REPO)))
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(combined)
    return 0 if result.wasSuccessful() and result.testsRun > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Tests for transient retry behavior on PVsystCLI read-only query commands."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pvsyst_auto.native import _winlaunch
from pvsyst_auto.native.cli import PVsystCLI, PVsystError


class CliQueryRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.workspace = root / "workspace"
        self.workspace.mkdir(parents=True)
        self.fake_cli = root / ("PVsystCLI.exe" if os.name == "nt" else "PVsystCLI")
        self.fake_cli.write_text("", encoding="utf-8")

    @contextlib.contextmanager
    def _process_boundary(self, handler):
        launches: list[tuple[str, ...]] = []

        def dispatch(argv, *args, **kwargs):
            cli_args = tuple(str(x) for x in argv[1:])
            launches.append(cli_args)
            rc, stdout, stderr = handler(cli_args, len(launches))
            return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("pvsyst_auto.native.cli.time.sleep"))
            if os.name == "nt":
                stack.enter_context(
                    patch.object(_winlaunch, "preserve_workspace_settings", return_value=contextlib.nullcontext())
                )
                stack.enter_context(patch.object(_winlaunch, "run", side_effect=dispatch))
            else:
                stack.enter_context(patch("pvsyst_auto.native.cli.subprocess.run", side_effect=dispatch))
            yield launches

    def test_license_info_retries_transient_empty_and_startup_abort_but_not_deterministic_errors(self) -> None:
        cli = PVsystCLI(self.fake_cli, self.workspace, max_transient_retries=1)

        with self._process_boundary(
            lambda args, attempt: (0, "", "") if attempt == 1 else (0, "Status: LICENSED\nRemaining days: 120\n", "")
        ) as launches:
            info = cli.license_info()
        self.assertEqual(launches, [("lic-info",), ("lic-info",)])
        self.assertEqual(info, {"status": "LICENSED", "remaining_days": 120})

        with self._process_boundary(
            lambda args, attempt: (1, "Checking license...\n", "")
            if attempt == 1
            else (0, "Status: LICENSED\nRemaining days: 90\n", "")
        ) as launches:
            info = cli.license_info()
        self.assertEqual(launches, [("lic-info",), ("lic-info",)])
        self.assertEqual(info, {"status": "LICENSED", "remaining_days": 90})

        with self._process_boundary(lambda args, attempt: (0, "No license or license expired\n", "")) as launches:
            with self.assertRaisesRegex(PVsystError, "no recognized fields"):
                cli.license_info()
        self.assertEqual(launches, [("lic-info",)])

    def test_version_retries_whitespace_and_respects_zero_retry_budget(self) -> None:
        cli = PVsystCLI(self.fake_cli, self.workspace, max_transient_retries=1)
        with self._process_boundary(
            lambda args, attempt: (0, "   \n", "") if attempt == 1 else (0, "PVsystCLI 8.1.7\n", "")
        ) as launches:
            self.assertEqual(cli.version(), "PVsystCLI 8.1.7")
        self.assertEqual(launches, [("version",), ("version",)])

        cli_zero = PVsystCLI(self.fake_cli, self.workspace, max_transient_retries=0)
        with self._process_boundary(lambda args, attempt: (0, "", "")) as launches:
            with self.assertRaisesRegex(PVsystError, "no output"):
                cli_zero.version()
        self.assertEqual(launches, [("version",)])

    def test_help_and_capabilities_retry_cache_only_success_and_recover_after_failure(self) -> None:
        cli = PVsystCLI(self.fake_cli, self.workspace, max_transient_retries=1)
        responses = {
            ("help", "run-simulation"): [
                (0, "", ""),
                (0, "  --output-csv-file|-ocf: CSV output\n", ""),
            ],
            ("help", "bogus"): [
                (0, "Unknown command: bogus", ""),
                (0, "  --flag|-f: Desc\n", ""),
            ],
            ("help", "list-projects"): [
                (0, "", ""),
                (0, "", ""),
                (0, "  --workspace|-w: Workspace\n", ""),
            ],
        }
        cursors: dict[tuple[str, ...], int] = {}

        def handler(args: tuple[str, ...], _attempt: int) -> tuple[int, str, str]:
            idx = cursors.get(args, 0)
            cursors[args] = idx + 1
            return responses[args][idx]

        with self._process_boundary(handler) as launches:
            caps = cli.capabilities("run-simulation")
            self.assertEqual(caps, {"run-simulation": ["output-csv-file"]})
            self.assertEqual(launches, [("help", "run-simulation"), ("help", "run-simulation")])

            # Repeated call returns the cached capabilities without another process launch.
            self.assertEqual(cli.capabilities("run-simulation"), {"run-simulation": ["output-csv-file"]})
            self.assertEqual(len(launches), 2)

            # Deterministic unknown-command failure does not retry and is not cached.
            with self.assertRaisesRegex(PVsystError, "CLI does not support bogus"):
                cli.command_help("bogus")
            self.assertEqual(len(launches), 3)
            self.assertIn("--flag", cli.command_help("bogus"))
            self.assertEqual(len(launches), 4)

            # Exhausted transient failure raises after 1 retry, then recovers cleanly later.
            with self.assertRaisesRegex(PVsystError, "CLI does not support list-projects"):
                cli.command_help("list-projects")
            self.assertEqual(len(launches), 6)
            self.assertIn("--workspace", cli.command_help("list-projects"))
            self.assertEqual(len(launches), 7)

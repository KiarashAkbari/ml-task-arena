"""Tests for CLI exit-code mapping (AGENTS.md section 4.1)."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from data_factory import cli
from data_factory.errors import EXIT_OK, EXIT_UNEXPECTED
from data_factory.fetch import FetchResult


class CliExitCodeTests(unittest.TestCase):
    def test_missing_config_exits_with_config_code(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cli.main(["--config", "does-not-exist.json"])
        self.assertEqual(code, 3)
        self.assertIn("cannot read config file", stderr.getvalue())

    def test_usage_error_exits_with_reserved_code_two(self) -> None:
        """argparse owns code 2 for unknown arguments; main must not swallow it."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            cli.main(["--definitely-not-a-flag"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_version_flag_exits_zero(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--version"]), EXIT_OK)

    def test_show_config_prints_resolved_settings(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["--show-config"])
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["settings"]["start_date"], "2023-01-01")
        self.assertEqual(payload["settings"]["raw_dir"], "data/raw")

    def test_unexpected_bug_exits_with_code_one(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            cli, "load_config", side_effect=RuntimeError("boom")
        ), contextlib.redirect_stderr(stderr):
            code = cli.main(["--config", "any.json"])
        self.assertEqual(code, EXIT_UNEXPECTED)
        self.assertIn("boom", stderr.getvalue())


class CliStageTests(unittest.TestCase):
    """One command = whole pipeline; a parameter = one part (AGENTS 4.1)."""

    @staticmethod
    def _result(**overrides: object) -> FetchResult:
        base: dict = {
            "records": 10,
            "batches": 2,
            "http_requests": 2,
            "resumed_records": 0,
            "peak_in_memory_records": 5,
            "already_complete": False,
        }
        base.update(overrides)
        return FetchResult(**base)  # type: ignore[arg-type]

    def test_default_command_runs_the_fetch_stage(self) -> None:
        with mock.patch("data_factory.cli.fetch_stage.run_fetch") as run:
            run.return_value = self._result()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main([]), EXIT_OK)
        run.assert_called_once()

    def test_fetch_subcommand_runs_fetch_stage_only(self) -> None:
        with mock.patch("data_factory.cli.fetch_stage.run_fetch") as run:
            run.return_value = self._result()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["fetch"]), EXIT_OK)
        run.assert_called_once()
        self.assertIn("10 records", stdout.getvalue())

    def test_fetch_summary_prints_real_measured_counts(self) -> None:
        with mock.patch("data_factory.cli.fetch_stage.run_fetch") as run:
            run.return_value = self._result(
                records=859910, batches=860, http_requests=860, resumed_records=1000
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                cli.main(["fetch"])
        summary = stdout.getvalue()
        self.assertIn("859,910 records in 860 batch(es)", summary)
        self.assertIn("resumed 1,000", summary)
        self.assertIn("860 HTTP request(s)", summary)

    def test_already_complete_summary_reports_zero_requests(self) -> None:
        with mock.patch("data_factory.cli.fetch_stage.run_fetch") as run:
            run.return_value = self._result(
                records=859910, batches=0, http_requests=0, already_complete=True
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                cli.main(["fetch"])
        self.assertIn("already complete", stdout.getvalue())
        self.assertIn("0 requests", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

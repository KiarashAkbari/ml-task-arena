"""Tests for CLI exit-code mapping (AGENTS.md section 4.1)."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from data_factory import cli
from data_factory.clean import CleanResult
from data_factory.curate import CurateResult
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

    def test_default_command_runs_fetch_then_curate_then_clean(self) -> None:
        with mock.patch("data_factory.cli.fetch_stage.run_fetch") as fetch, \
                mock.patch("data_factory.cli.curate_stage.run_curate") as curate, \
                mock.patch("data_factory.cli.clean_stage.run_clean") as clean:
            fetch.return_value = self._result()
            curate.return_value = _curate_result()
            clean.return_value = _clean_result()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main([]), EXIT_OK)
        fetch.assert_called_once()
        curate.assert_called_once()
        clean.assert_called_once()

    def test_fetch_subcommand_runs_fetch_stage_only(self) -> None:
        with mock.patch("data_factory.cli.fetch_stage.run_fetch") as fetch, \
                mock.patch("data_factory.cli.curate_stage.run_curate") as curate, \
                mock.patch("data_factory.cli.clean_stage.run_clean") as clean:
            fetch.return_value = self._result()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["fetch"]), EXIT_OK)
        fetch.assert_called_once()
        curate.assert_not_called()
        clean.assert_not_called()
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


def _curate_result(**overrides: object) -> CurateResult:
    base: dict = {
        "total": 100,
        "passed": 99,
        "quarantined": 1,
        "counts": {"INVALID_ZIP_CODE": 1},
        "ratio": 0.01,
        "threshold": 0.05,
    }
    base.update(overrides)
    return CurateResult(**base)  # type: ignore[arg-type]


class CurateCliTests(unittest.TestCase):
    def test_curate_subcommand_runs_curate_stage_only(self) -> None:
        with mock.patch("data_factory.cli.fetch_stage.run_fetch") as fetch, \
                mock.patch("data_factory.cli.curate_stage.run_curate") as curate, \
                mock.patch("data_factory.cli.clean_stage.run_clean") as clean:
            curate.return_value = _curate_result()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["curate"]), EXIT_OK)
        fetch.assert_not_called()
        clean.assert_not_called()
        curate.assert_called_once()
        summary = stdout.getvalue()
        self.assertIn("scanned 100", summary)
        self.assertIn("INVALID_ZIP_CODE: 1", summary)

    def test_curate_summary_printed_before_threshold_failure(self) -> None:
        """4.3: counts are reported, THEN the run fails with exit 5."""
        with mock.patch(
            "data_factory.cli.curate_stage.run_curate"
        ) as curate:
            curate.return_value = _curate_result(
                total=100, passed=90, quarantined=10, ratio=0.10,
                counts={"INVALID_ZIP_CODE": 10},
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(["curate"])
        self.assertEqual(code, 5)
        self.assertIn("quarantined 10 (10.00%; threshold 5.00%)", stdout.getvalue())
        self.assertIn("exceeds threshold", stderr.getvalue())


def _clean_result(**overrides: object) -> CleanResult:
    base: dict = {
        "rows_scanned": 100,
        "rows_written": 99,
        "rows_skipped": 1,
        "zip_matched": 90,
        "zip_missing": 6,
        "zip_unknown": 3,
        "borough_mismatches": 2,
        "duration_nulls": 4,
    }
    base.update(overrides)
    return CleanResult(**base)  # type: ignore[arg-type]


class CleanCliTests(unittest.TestCase):
    def test_clean_subcommand_runs_clean_stage_only(self) -> None:
        with mock.patch("data_factory.cli.fetch_stage.run_fetch") as fetch, \
                mock.patch("data_factory.cli.curate_stage.run_curate") as curate, \
                mock.patch("data_factory.cli.clean_stage.run_clean") as clean:
            clean.return_value = _clean_result()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["clean"]), EXIT_OK)
        fetch.assert_not_called()
        curate.assert_not_called()
        clean.assert_called_once()
        summary = stdout.getvalue()
        self.assertIn("wrote 99 rows", summary)
        self.assertIn("borough mismatches 2", summary)
        self.assertIn("quarantined 1", summary)


if __name__ == "__main__":
    unittest.main()

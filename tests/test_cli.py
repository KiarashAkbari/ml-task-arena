"""Tests for CLI exit-code mapping (AGENTS.md section 4.1)."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from data_factory import cli
from data_factory.aggregate import AggregationResult
from data_factory.clean import CleanResult
from data_factory.config import load_config
from data_factory.curate import CurateResult
from data_factory.errors import EXIT_OK, EXIT_UNEXPECTED
from data_factory.fetch import WINDOW_MODE_SINCE_LAST_RUN, FetchResult, run_fetch
from data_factory.moving_window import today_utc
from data_factory.outputs import OutputsResult
from data_factory.quality import QualityReport
from tests.test_config import VALID_CONFIG
from tests.test_curate import ScriptedClient, healthy_record


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

    def test_default_command_runs_all_four_stages(self) -> None:
        with mock.patch("data_factory.cli.fetch_stage.run_fetch") as fetch, \
                mock.patch("data_factory.cli.curate_stage.run_curate") as curate, \
                mock.patch("data_factory.cli.clean_stage.run_clean") as clean, \
                mock.patch("data_factory.cli.outputs_stage.run_outputs") as outputs:
            fetch.return_value = self._result()
            curate.return_value = _curate_result()
            clean.return_value = _clean_result()
            outputs.return_value = _outputs_result()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main([]), EXIT_OK)
        fetch.assert_called_once()
        curate.assert_called_once()
        clean.assert_called_once()
        outputs.assert_called_once()

    def test_fetch_subcommand_runs_fetch_stage_only(self) -> None:
        with mock.patch("data_factory.cli.fetch_stage.run_fetch") as fetch, \
                mock.patch("data_factory.cli.curate_stage.run_curate") as curate, \
                mock.patch("data_factory.cli.clean_stage.run_clean") as clean, \
                mock.patch("data_factory.cli.outputs_stage.run_outputs") as outputs:
            fetch.return_value = self._result()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["fetch"]), EXIT_OK)
        fetch.assert_called_once()
        curate.assert_not_called()
        clean.assert_not_called()
        outputs.assert_not_called()
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
                mock.patch("data_factory.cli.clean_stage.run_clean") as clean, \
                mock.patch("data_factory.cli.outputs_stage.run_outputs") as outputs:
            clean.return_value = _clean_result()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["clean"]), EXIT_OK)
        fetch.assert_not_called()
        curate.assert_not_called()
        clean.assert_called_once()
        outputs.assert_not_called()
        summary = stdout.getvalue()
        self.assertIn("wrote 99 rows", summary)
        self.assertIn("borough mismatches 2", summary)
        self.assertIn("quarantined 1", summary)


def _quality_report(**overrides: object) -> QualityReport:
    base: dict = {
        "path": Path("data/curated/data_quality.csv"),
        "rows": 99,
        "columns": 11,
        "emptiest_column": "duration_minutes",
        "emptiest_pct": 41.41,
    }
    base.update(overrides)
    return QualityReport(**base)  # type: ignore[arg-type]


def _outputs_result(**overrides: object) -> OutputsResult:
    base: dict = {
        "aggregate": AggregationResult(
            rows_written=30, months=("2023-01",), districts=("Queens",)
        ),
        "quality": _quality_report(),
        "manifest_path": "data/curated/run_manifest.json",
        "output_hashes": {
            "cases.csv": "a" * 64,
            "aggregate.csv": "b" * 64,
            "quarantine.jsonl": "c" * 64,
            "data_quality.csv": "d" * 64,
        },
    }
    base.update(overrides)
    return OutputsResult(**base)  # type: ignore[arg-type]


class OutputsCliTests(unittest.TestCase):
    def test_outputs_subcommand_runs_outputs_stage_only(self) -> None:
        with mock.patch("data_factory.cli.fetch_stage.run_fetch") as fetch, \
                mock.patch("data_factory.cli.clean_stage.run_clean") as clean, \
                mock.patch("data_factory.cli.outputs_stage.run_outputs") as outputs:
            outputs.return_value = _outputs_result()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(["outputs"]), EXIT_OK)
        fetch.assert_not_called()
        clean.assert_not_called()
        outputs.assert_called_once()
        summary = stdout.getvalue()
        self.assertIn("30 rows x 1 months", summary)
        self.assertIn("hashed 4 file(s)", summary)
        self.assertIn("cases.csv: sha256", summary)
        self.assertIn("data_quality.csv profiled 11 column(s) over 99 row(s)", summary)
        self.assertIn("emptiest column duration_minutes (41.41% empty)", summary)


class SinceLastRunCliTests(unittest.TestCase):
    """``--since-last-run`` wiring: flag validation + real window resolution."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.config_path = self.tmp_path / "config.json"

    def write_config(self, *, end: str = "2024-06-30") -> None:
        config = json.loads(json.dumps(VALID_CONFIG))
        config["date_range"] = {"start": "2023-01-01", "end": end}
        config["paths"] = {
            "raw_dir": str(self.tmp_path / "raw"),
            "curated_dir": str(self.tmp_path / "curated"),
            "raw_archive_dir": str(self.tmp_path / "raw_archived"),
        }
        config["population"]["path"] = str(self.tmp_path / "pop.csv")
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

    def seed_previous_run(self) -> None:
        settings = load_config(self.config_path)
        run_fetch(settings, ScriptedClient([healthy_record(i) for i in range(3)]))

    def test_flag_is_rejected_for_read_only_stages(self) -> None:
        self.write_config()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            cli.main(["outputs", "--since-last-run", "--config", str(self.config_path)])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("applies to the 'run' and 'fetch' commands", stderr.getvalue())

    def test_fetch_resolves_the_window_and_archives_the_previous_raw_data(self) -> None:
        self.write_config()
        self.seed_previous_run()
        with mock.patch("data_factory.cli.fetch_stage.run_fetch") as fetch:
            fetch.return_value = CliStageTests._result()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli.main(
                    ["fetch", "--since-last-run", "--config", str(self.config_path)]
                )

        self.assertEqual(code, EXIT_OK)
        settings, _client = fetch.call_args.args
        self.assertEqual(settings.start_date, date(2024, 7, 1))
        self.assertEqual(settings.end_date, today_utc())
        self.assertEqual(fetch.call_args.kwargs["window_mode"], WINDOW_MODE_SINCE_LAST_RUN)

        archive = self.tmp_path / "raw_archived" / "2023-01-01__2024-06-30"
        self.assertTrue(archive.is_dir())
        self.assertFalse((self.tmp_path / "raw").exists())
        summary = stdout.getvalue()
        self.assertIn(f"window: since last run — 2024-07-01..{today_utc().isoformat()}", summary)
        self.assertIn(f"archived the previous raw data to {archive}", summary)

    def test_flag_with_nothing_new_prints_a_noop_and_keeps_the_raw_data(self) -> None:
        self.write_config(end=today_utc().isoformat())
        self.seed_previous_run()
        with mock.patch("data_factory.cli.fetch_stage.run_fetch") as fetch:
            fetch.return_value = CliStageTests._result(already_complete=True, batches=0)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli.main(
                    ["fetch", "--since-last-run", "--config", str(self.config_path)]
                )

        self.assertEqual(code, EXIT_OK)
        self.assertTrue((self.tmp_path / "raw").is_dir())
        self.assertFalse((self.tmp_path / "raw_archived").exists())
        self.assertIn("nothing new to fetch", stdout.getvalue())

    def test_flag_without_a_previous_run_exits_with_the_validation_code(self) -> None:
        self.write_config()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["fetch", "--since-last-run", "--config", str(self.config_path)])
        self.assertEqual(code, 5)
        self.assertIn("needs a previous run", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

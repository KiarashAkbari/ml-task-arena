"""Tests for the moving fetch window ``--since-last-run`` (AGENTS.md 4.8.1).

The feature must fetch only "from the last run until now" while never
destroying the previous window's evidence, and it must fail loudly when there
is no previous run to continue from.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from data_factory.clean import run_clean
from data_factory.config import Settings, load_config
from data_factory.curate import run_curate
from data_factory.errors import ValidationError
from data_factory.fetch import (
    BATCH_NAME_TEMPLATE,
    CHECKPOINT_NAME,
    WINDOW_MODE_CONFIGURED,
    WINDOW_MODE_SINCE_LAST_RUN,
    run_fetch,
)
from data_factory.manifest import MANIFEST_NAME
from data_factory.moving_window import (
    apply_moving_window,
    read_previous_window,
    resolve_moving_window,
    today_utc,
)
from data_factory.outputs import run_outputs
from data_factory.quality import DATA_QUALITY_NAME
from tests.test_curate import ScriptedClient, healthy_record

CONFIGURED_WINDOW = (date(2023, 1, 1), date(2024, 6, 30))
ARCHIVE_NAME = "2023-01-01__2024-06-30"
POPULATION_CSV = "zip,population,borough\n11101,30000,Queens\n"


class MovingWindowTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.population_path = self.tmp_path / "pop.csv"
        self.population_path.write_text(POPULATION_CSV, encoding="utf-8")

    def settings(self, *, batch_size: int = 10) -> Settings:
        return dataclasses.replace(
            load_config(),
            batch_size=batch_size,
            raw_dir=self.tmp_path / "raw",
            curated_dir=self.tmp_path / "curated",
            raw_archive_dir=self.tmp_path / "raw_archived",
            population_path=self.population_path,
        )

    def fetch(self, settings: Settings, count: int = 3) -> ScriptedClient:
        client = ScriptedClient([healthy_record(i) for i in range(count)])
        run_fetch(settings, client)
        return client

    def full_pipeline(self, settings: Settings, count: int = 3) -> None:
        self.fetch(settings, count)
        run_curate(settings)
        run_clean(settings)
        run_outputs(settings, started_at="s", finished_at="f")

    def archive_dir(self, settings: Settings) -> Path:
        return Path(settings.raw_archive_dir) / ARCHIVE_NAME


class ResolutionTests(MovingWindowTestBase):
    def test_today_utc_returns_a_date(self) -> None:
        self.assertIsInstance(today_utc(), date)

    def test_no_previous_run_fails_loudly(self) -> None:
        settings = self.settings()
        with self.assertRaises(ValidationError) as ctx:
            resolve_moving_window(settings, today=date(2024, 7, 10))
        message = str(ctx.exception)
        self.assertIn("--since-last-run needs a previous run", message)
        self.assertIn("run the factory once without the flag first", message)

    def test_window_continues_the_day_after_the_checkpoint_window(self) -> None:
        settings = self.settings()
        self.fetch(settings)
        resolution = resolve_moving_window(settings, today=date(2024, 7, 10))
        self.assertEqual((resolution.start, resolution.end), (date(2024, 7, 1), date(2024, 7, 10)))
        self.assertEqual(resolution.previous, CONFIGURED_WINDOW)
        self.assertEqual(resolution.previous_source, "checkpoint")
        self.assertFalse(resolution.already_current)
        self.assertTrue(resolution.rollover)

    def test_previous_window_falls_back_to_the_last_manifest(self) -> None:
        settings = self.settings()
        self.full_pipeline(settings)
        # The raw directory no longer exists: only the manifest records the run.
        for path in sorted(Path(settings.raw_dir).glob("batch_*.json")):
            path.unlink()
        (Path(settings.raw_dir) / CHECKPOINT_NAME).unlink()

        self.assertEqual(read_previous_window(settings), (CONFIGURED_WINDOW, "manifest"))
        resolution = resolve_moving_window(settings, today=date(2024, 7, 1))
        self.assertEqual((resolution.start, resolution.end), (date(2024, 7, 1), date(2024, 7, 1)))
        self.assertEqual(resolution.previous_source, "manifest")
        self.assertFalse(resolution.rollover, "nothing on disk to archive")

    def test_already_current_is_a_noop_not_an_error(self) -> None:
        settings = self.settings()
        self.fetch(settings)
        resolution = resolve_moving_window(settings, today=date(2024, 6, 30))
        self.assertTrue(resolution.already_current)
        self.assertEqual((resolution.start, resolution.end), CONFIGURED_WINDOW)
        self.assertFalse(resolution.rollover)

        moving = apply_moving_window(settings, today=date(2024, 6, 30))
        self.assertIsNone(moving.archived_to)
        self.assertTrue(Path(settings.raw_dir).is_dir(), "raw data must stay in place")
        client = ScriptedClient([healthy_record(0)])
        result = run_fetch(
            moving.settings, client, window_mode=WINDOW_MODE_SINCE_LAST_RUN
        )
        self.assertTrue(result.already_complete)
        self.assertEqual(client.request_count, 0)

    def test_unreadable_manifest_is_refused(self) -> None:
        settings = self.settings()
        curated = Path(settings.curated_dir)
        curated.mkdir(parents=True)
        (curated / MANIFEST_NAME).write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValidationError) as ctx:
            resolve_moving_window(settings, today=date(2024, 7, 10))
        self.assertIn("unreadable", str(ctx.exception))

    def test_manifest_without_a_window_is_refused(self) -> None:
        settings = self.settings()
        curated = Path(settings.curated_dir)
        curated.mkdir(parents=True)
        (curated / MANIFEST_NAME).write_text('{"window": {"start": "x"}}', encoding="utf-8")
        with self.assertRaises(ValidationError) as ctx:
            resolve_moving_window(settings, today=date(2024, 7, 10))
        self.assertIn("ISO date", str(ctx.exception))


class RolloverTests(MovingWindowTestBase):
    def test_previous_raw_window_is_archived_not_deleted(self) -> None:
        settings = self.settings()
        self.fetch(settings, count=3)
        raw_dir = Path(settings.raw_dir)
        old_batches = {
            path.name: path.read_bytes() for path in raw_dir.glob("batch_*.json")
        }

        moving = apply_moving_window(settings, today=date(2024, 7, 5))

        self.assertEqual(moving.archived_to, self.archive_dir(settings))
        self.assertTrue(self.archive_dir(settings).is_dir())
        self.assertFalse(raw_dir.exists(), "the raw directory must be renamed, not reused")
        for name, content in old_batches.items():
            self.assertEqual((self.archive_dir(settings) / name).read_bytes(), content)
        self.assertTrue((self.archive_dir(settings) / CHECKPOINT_NAME).is_file())

    def test_manifest_travels_with_the_archived_window(self) -> None:
        settings = self.settings()
        self.full_pipeline(settings)
        original_manifest = json.loads(
            (Path(settings.curated_dir) / MANIFEST_NAME).read_text(encoding="utf-8")
        )

        moving = apply_moving_window(settings, today=date(2024, 7, 5))
        archived_manifest = json.loads(
            (moving.archived_to / MANIFEST_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(archived_manifest["window"], original_manifest["window"])

    def test_new_window_is_fetched_into_a_fresh_raw_directory(self) -> None:
        settings = self.settings()
        self.fetch(settings, count=2)
        moving = apply_moving_window(settings, today=date(2024, 7, 5))

        client = ScriptedClient([healthy_record(10), healthy_record(11)])
        result = run_fetch(
            moving.settings, client, window_mode=WINDOW_MODE_SINCE_LAST_RUN
        )
        self.assertEqual(result.records, 2)
        self.assertEqual(result.resumed_records, 0)

        checkpoint = json.loads(
            (Path(moving.settings.raw_dir) / CHECKPOINT_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(checkpoint["window_start"], "2024-07-01")
        self.assertEqual(checkpoint["window_end"], "2024-07-05")
        self.assertEqual(checkpoint["window_mode"], WINDOW_MODE_SINCE_LAST_RUN)
        self.assertEqual(
            sorted(p.name for p in Path(moving.settings.raw_dir).glob("batch_*.json")),
            [BATCH_NAME_TEMPLATE.format(index=0)],
        )

    def test_archive_name_collision_is_refused_and_raw_data_stays(self) -> None:
        settings = self.settings()
        self.fetch(settings)
        self.archive_dir(settings).mkdir(parents=True)

        with self.assertRaises(ValidationError) as ctx:
            apply_moving_window(settings, today=date(2024, 7, 5))
        self.assertIn("already exists", str(ctx.exception))
        self.assertTrue(
            (Path(settings.raw_dir) / CHECKPOINT_NAME).is_file(),
            "a refused rollover must not move the raw data",
        )

    def test_effective_settings_carry_the_new_window(self) -> None:
        settings = self.settings()
        self.fetch(settings)
        moving = apply_moving_window(settings, today=date(2024, 7, 5))
        self.assertEqual(moving.settings.start_date, date(2024, 7, 1))
        self.assertEqual(moving.settings.end_date, date(2024, 7, 5))
        # Everything else is untouched.
        self.assertEqual(moving.settings.scope_agency, settings.scope_agency)
        self.assertEqual(moving.settings.batch_size, settings.batch_size)


class MovingWindowPipelineTests(MovingWindowTestBase):
    def test_manifest_reports_the_moving_window_and_the_archive(self) -> None:
        settings = self.settings()
        self.full_pipeline(settings)

        moving = apply_moving_window(settings, today=date(2024, 7, 5))
        client = ScriptedClient([healthy_record(20), healthy_record(21)])
        run_fetch(moving.settings, client, window_mode=WINDOW_MODE_SINCE_LAST_RUN)
        run_curate(moving.settings)
        run_clean(moving.settings)
        run_outputs(moving.settings, started_at="s2", finished_at="f2")

        manifest = json.loads(
            (Path(moving.settings.curated_dir) / MANIFEST_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["window"],
            {"start": "2024-07-01", "end": "2024-07-05", "mode": WINDOW_MODE_SINCE_LAST_RUN},
        )
        self.assertEqual(
            manifest["archived_windows"],
            [
                {
                    "start": "2023-01-01",
                    "end": "2024-06-30",
                    "path": self.archive_dir(settings).as_posix(),
                }
            ],
        )
        self.assertEqual(manifest["fetch"]["records_raw"], 2)
        self.assertIn(DATA_QUALITY_NAME, manifest["outputs"])

    def test_outputs_alone_still_report_the_true_window(self) -> None:
        """The manifest reads the window from the checkpoint, not the config."""
        settings = self.settings()
        self.full_pipeline(settings)
        moving = apply_moving_window(settings, today=date(2024, 7, 5))
        run_fetch(
            moving.settings,
            ScriptedClient([healthy_record(30)]),
            window_mode=WINDOW_MODE_SINCE_LAST_RUN,
        )
        run_curate(moving.settings)
        run_clean(moving.settings)

        # A later, flag-less `outputs` call still has the configured dates.
        run_outputs(settings, started_at="s3", finished_at="f3")
        manifest = json.loads(
            (Path(settings.curated_dir) / MANIFEST_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["window"]["start"], "2024-07-01")
        self.assertEqual(manifest["window"]["end"], "2024-07-05")
        self.assertEqual(manifest["window"]["mode"], WINDOW_MODE_SINCE_LAST_RUN)


class CheckpointWindowRecordTests(MovingWindowTestBase):
    def test_checkpoint_records_the_configured_window_by_default(self) -> None:
        settings = self.settings()
        self.fetch(settings)
        checkpoint = json.loads(
            (Path(settings.raw_dir) / CHECKPOINT_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(checkpoint["window_start"], "2023-01-01")
        self.assertEqual(checkpoint["window_end"], "2024-06-30")
        self.assertEqual(checkpoint["window_mode"], WINDOW_MODE_CONFIGURED)

    def test_invalid_window_mode_is_refused(self) -> None:
        settings = self.settings()
        with self.assertRaises(ValidationError) as ctx:
            run_fetch(settings, ScriptedClient([healthy_record(0)]), window_mode="yesterday")
        self.assertIn("window_mode", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

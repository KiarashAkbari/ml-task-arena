"""Tests for the batched, resumable, idempotent fetch stage (section 4.2)."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from data_factory.config import Settings, load_config
from data_factory.errors import NetworkError, ValidationError
from data_factory.fetch import (
    BATCH_NAME_TEMPLATE,
    CHECKPOINT_NAME,
    WINDOW_MODE_SINCE_LAST_RUN,
    FetchResult,
    compute_fingerprint,
    run_fetch,
)


def _record(index: int) -> dict:
    return {
        "unique_key": str(index),
        "complaint_type": "Noise - Park",
        "agency": "NYPD",
        "closed_date": f"2023-05-0{(index % 9) + 1}T00:00:00.000",
    }


class FakeFetchClient:
    """Offline stand-in for HttpClient serving deterministic pages."""

    def __init__(
        self,
        total: int,
        *,
        fail_at_offset: int | None = None,
        columns_per_batch: list[set[str]] | None = None,
    ) -> None:
        self.total = total
        self.fail_at_offset = fail_at_offset
        self.columns_per_batch = columns_per_batch
        self.request_count = 0
        self.offsets: list[int] = []

    def get(self, url: str, *, params: dict | None = None):  # noqa: ANN001
        assert params is not None
        offset = int(params["$offset"])
        self.offsets.append(offset)
        self.request_count += 1
        if self.fail_at_offset is not None and offset >= self.fail_at_offset:
            raise NetworkError("simulated outage")
        batch_index = offset // int(params["$limit"])
        if self.columns_per_batch is not None:
            if batch_index >= len(self.columns_per_batch):
                return []
            columns = sorted(self.columns_per_batch[batch_index])
            return [
                {key: (str(index) if key == "unique_key" else "x") for key in columns}
                for index in range(offset, min(offset + int(params["$limit"]), self.total))
            ]
        limit = int(params["$limit"])
        return [_record(i) for i in range(offset, min(offset + limit, self.total))]


class FetchTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def settings(self, *, batch_size: int = 10, raw_dir: Path | None = None) -> Settings:
        return dataclasses.replace(
            load_config(),
            batch_size=batch_size,
            raw_dir=raw_dir or (self.tmp_path / "raw"),
        )

    @staticmethod
    def read_batch(raw_dir: Path, index: int) -> list[dict]:
        path = raw_dir / BATCH_NAME_TEMPLATE.format(index=index)
        return json.loads(path.read_text(encoding="utf-8"))


class FreshFetchTests(FetchTestBase):
    def test_covers_all_records_exactly_once(self) -> None:
        settings = self.settings(batch_size=10)
        result = run_fetch(settings, FakeFetchClient(25))
        self.assertEqual(result.records, 25)
        self.assertEqual(result.batches, 3)  # 10 + 10 + 5
        self.assertEqual(result.http_requests, 3)
        self.assertEqual(result.resumed_records, 0)
        self.assertFalse(result.already_complete)

        keys = [
            row["unique_key"]
            for index in range(3)
            for row in self.read_batch(settings.raw_dir, index)
        ]
        self.assertEqual(len(keys), 25)
        self.assertEqual(len(set(keys)), 25)  # no duplicates
        self.assertEqual(sorted(keys, key=int), [str(i) for i in range(25)])

    def test_exact_multiple_of_batch_size_ends_on_empty_page(self) -> None:
        settings = self.settings(batch_size=10)
        result = run_fetch(settings, FakeFetchClient(20))
        self.assertEqual(result.records, 20)
        self.assertEqual(result.batches, 2)
        self.assertEqual(result.http_requests, 3)  # final probe returns []
        self.assertFalse(
            (settings.raw_dir / BATCH_NAME_TEMPLATE.format(index=2)).exists(),
            "empty pages must not create empty batch files",
        )

    def test_peak_memory_stays_one_batch(self) -> None:
        """AGENTS 4.2: the whole raw dataset never sits in memory at once."""
        settings = self.settings(batch_size=10)
        result = run_fetch(settings, FakeFetchClient(50))
        self.assertEqual(result.peak_in_memory_records, 10)
        self.assertLess(result.peak_in_memory_records, result.records)

    def test_batch_bytes_are_deterministic_across_fresh_runs(self) -> None:
        dir_a = self.tmp_path / "a"
        dir_b = self.tmp_path / "b"
        run_fetch(self.settings(raw_dir=dir_a), FakeFetchClient(25))
        run_fetch(self.settings(raw_dir=dir_b), FakeFetchClient(25))
        for index in range(3):
            self.assertEqual(
                (dir_a / BATCH_NAME_TEMPLATE.format(index=index)).read_bytes(),
                (dir_b / BATCH_NAME_TEMPLATE.format(index=index)).read_bytes(),
            )

    def test_no_temp_files_left_behind(self) -> None:
        settings = self.settings()
        run_fetch(settings, FakeFetchClient(25))
        self.assertEqual(list(settings.raw_dir.glob("*.tmp")), [])


class ResumeTests(FetchTestBase):
    def test_interrupted_run_resumes_from_checkpoint(self) -> None:
        settings = self.settings(batch_size=10)
        with self.assertRaises(NetworkError):
            run_fetch(settings, FakeFetchClient(25, fail_at_offset=20))
        checkpoint = json.loads(
            (settings.raw_dir / CHECKPOINT_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(checkpoint["batches_written"], 2)
        self.assertEqual(checkpoint["next_offset"], 20)
        self.assertFalse(checkpoint["complete"])

        client = FakeFetchClient(25)
        result = run_fetch(settings, client)
        self.assertEqual(client.offsets[0], 20)  # continues, not from zero
        self.assertEqual(result.resumed_records, 20)
        self.assertEqual(result.records, 25)
        self.assertEqual(result.batches, 1)

        keys = [
            row["unique_key"]
            for index in range(3)
            for row in self.read_batch(settings.raw_dir, index)
        ]
        self.assertEqual(sorted(keys, key=int), [str(i) for i in range(25)])

    def test_cumulative_request_count_survives_resume(self) -> None:
        settings = self.settings(batch_size=10)
        first = FakeFetchClient(25, fail_at_offset=20)
        with self.assertRaises(NetworkError):
            run_fetch(settings, first)
        second = FakeFetchClient(25)
        run_fetch(settings, second)
        checkpoint = json.loads(
            (settings.raw_dir / CHECKPOINT_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(
            checkpoint["http_requests"], first.request_count + second.request_count
        )

    def test_completed_rerun_is_a_zero_request_noop(self) -> None:
        settings = self.settings(batch_size=10)
        run_fetch(settings, FakeFetchClient(25))
        before = {
            path.name: path.read_bytes()
            for path in settings.raw_dir.glob("batch_*.json")
        }
        client = FakeFetchClient(25)
        result = run_fetch(settings, client)
        self.assertTrue(result.already_complete)
        self.assertEqual(client.request_count, 0)
        self.assertEqual(result.http_requests, 0)
        after = {
            path.name: path.read_bytes()
            for path in settings.raw_dir.glob("batch_*.json")
        }
        self.assertEqual(before, after)


class GuardTests(FetchTestBase):
    def test_config_change_with_existing_raw_data_rejected(self) -> None:
        settings = self.settings(batch_size=10)
        run_fetch(settings, FakeFetchClient(25))
        changed = dataclasses.replace(settings, batch_size=5)
        with self.assertRaises(ValidationError) as ctx:
            run_fetch(changed, FakeFetchClient(25))
        self.assertIn("different scope", str(ctx.exception))

    def test_stray_batch_without_checkpoint_rejected(self) -> None:
        settings = self.settings()
        settings.raw_dir.mkdir(parents=True)
        (settings.raw_dir / BATCH_NAME_TEMPLATE.format(index=0)).write_text("[]")
        with self.assertRaises(ValidationError):
            run_fetch(settings, FakeFetchClient(5))

    def test_missing_batch_file_rejected(self) -> None:
        settings = self.settings(batch_size=10)
        run_fetch(settings, FakeFetchClient(25))
        (settings.raw_dir / BATCH_NAME_TEMPLATE.format(index=1)).unlink()
        with self.assertRaises(ValidationError):
            run_fetch(settings, FakeFetchClient(25))

    def test_corrupt_checkpoint_rejected(self) -> None:
        settings = self.settings(batch_size=10)
        run_fetch(settings, FakeFetchClient(25))
        (settings.raw_dir / CHECKPOINT_NAME).write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValidationError):
            run_fetch(settings, FakeFetchClient(25))

    def test_changed_column_set_detected_mid_run(self) -> None:
        """AGENTS 4.8.2: source shape changes stop the run loudly."""
        settings = self.settings(batch_size=2)
        client = FakeFetchClient(
            4,
            columns_per_batch=[{"unique_key", "a"}, {"unique_key", "a", "new_col"}],
        )
        with self.assertRaises(ValidationError) as ctx:
            run_fetch(settings, client)
        self.assertIn("source shape changed", str(ctx.exception))

    def test_missing_column_detected_mid_run(self) -> None:
        settings = self.settings(batch_size=2)
        client = FakeFetchClient(
            4,
            columns_per_batch=[{"unique_key", "a", "b"}, {"unique_key", "a"}],
        )
        with self.assertRaises(ValidationError) as ctx:
            run_fetch(settings, client)
        self.assertIn("missing=['b']", str(ctx.exception))

    def test_non_array_payload_rejected(self) -> None:
        settings = self.settings()

        class DictClient:
            request_count = 0

            def get(self, url: str, *, params: dict | None = None):  # noqa: ANN001
                self.request_count += 1
                return {"error": "unexpected dict"}

        with self.assertRaises(ValidationError):
            run_fetch(settings, DictClient())


class FingerprintTests(FetchTestBase):
    def test_fingerprint_changes_with_scope_and_batch_size(self) -> None:
        base = self.settings()
        self.assertEqual(compute_fingerprint(base), compute_fingerprint(self.settings()))
        self.assertNotEqual(
            compute_fingerprint(base),
            compute_fingerprint(dataclasses.replace(base, batch_size=99)),
        )
        self.assertNotEqual(
            compute_fingerprint(base),
            compute_fingerprint(
                dataclasses.replace(base, scope_agency="DSNY")
            ),
        )

    def test_fingerprint_changes_with_the_window(self) -> None:
        """A moving window (4.8.1) must not silently reuse another window's raw dir."""
        base = self.settings()
        shifted = dataclasses.replace(base, start_date=base.start_date.replace(day=2))
        self.assertNotEqual(compute_fingerprint(base), compute_fingerprint(shifted))


class WindowRecordTests(FetchTestBase):
    """The checkpoint is the window of record for the manifest (4.5 + 4.8.1)."""

    def checkpoint(self, settings) -> dict:  # noqa: ANN001
        path = settings.raw_dir / CHECKPOINT_NAME
        return json.loads(path.read_text(encoding="utf-8"))

    def test_window_and_mode_are_recorded(self) -> None:
        settings = self.settings()
        run_fetch(settings, FakeFetchClient(5), window_mode=WINDOW_MODE_SINCE_LAST_RUN)
        state = self.checkpoint(settings)
        self.assertEqual(state["window_start"], settings.start_date.isoformat())
        self.assertEqual(state["window_end"], settings.end_date.isoformat())
        self.assertEqual(state["window_mode"], WINDOW_MODE_SINCE_LAST_RUN)

    def test_resume_preserves_the_window_record(self) -> None:
        settings = self.settings(batch_size=10)
        with self.assertRaises(NetworkError):
            run_fetch(settings, FakeFetchClient(25, fail_at_offset=20))
        before = self.checkpoint(settings)
        run_fetch(settings, FakeFetchClient(25))
        after = self.checkpoint(settings)
        for key in ("window_start", "window_end", "window_mode"):
            self.assertEqual(before[key], after[key])

    def test_checkpoint_from_an_older_version_is_refused(self) -> None:
        settings = self.settings()
        run_fetch(settings, FakeFetchClient(5))
        state = self.checkpoint(settings)
        for key in ("window_start", "window_end", "window_mode"):
            del state[key]
        (settings.raw_dir / CHECKPOINT_NAME).write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(ValidationError) as ctx:
            run_fetch(settings, FakeFetchClient(5))
        message = str(ctx.exception)
        self.assertIn("incomplete", message)
        self.assertIn("window_start", message)

    def test_invalid_window_values_are_refused(self) -> None:
        settings = self.settings()
        run_fetch(settings, FakeFetchClient(5))
        good = self.checkpoint(settings)
        cases = [
            ({**good, "window_start": "not-a-date"}, "ISO date"),
            ({**good, "window_start": "2024-07-01", "window_end": "2024-06-30"}, "after"),
            ({**good, "window_mode": "yesterday"}, "window_mode"),
        ]
        for state, expected in cases:
            with self.subTest(expected=expected):
                (settings.raw_dir / CHECKPOINT_NAME).write_text(
                    json.dumps(state), encoding="utf-8"
                )
                with self.assertRaises(ValidationError) as ctx:
                    run_fetch(settings, FakeFetchClient(5))
                self.assertIn(expected, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

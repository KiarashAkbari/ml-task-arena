"""Integration tests for the curate stage (AGENTS.md section 4.3).

Raw batches are produced by the real fetch stage (scripted offline client),
then curate must: quarantine violators with reason codes, keep healthy
records out of quarantine, report per-reason counts, and fail the run when
the configured ratio is exceeded.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from data_factory.config import Settings, load_config
from data_factory.curate import QUARANTINE_NAME, ensure_within_threshold, run_curate
from data_factory.errors import QuarantineThresholdExceeded, ValidationError
from data_factory.fetch import run_fetch


def healthy_record(index: int) -> dict:
    return {
        "unique_key": str(1000 + index),
        "created_date": "2023-05-01T08:30:00.000",
        "closed_date": "2023-05-03T14:00:00.000",
        "agency": "NYPD",
        "complaint_type": "Noise - Residential",
        "borough": "Queens",
        "incident_zip": "11101",
        "status": "Closed",
        "incident_address": f"{index} MAIN ST",
    }


class ScriptedClient:
    """Serves a fixed list of records in pages (offline)."""

    def __init__(self, records: list[dict]) -> None:
        self.records = records
        self.request_count = 0

    def get(self, url: str, *, params: dict | None = None):  # noqa: ANN001
        assert params is not None
        self.request_count += 1
        offset = int(params["$offset"])
        limit = int(params["$limit"])
        return self.records[offset : offset + limit]


class CurateTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def settings(self, *, batch_size: int = 10, threshold: float = 0.05) -> Settings:
        return dataclasses.replace(
            load_config(),
            batch_size=batch_size,
            raw_dir=self.tmp_path / "raw",
            curated_dir=self.tmp_path / "curated",
            quarantine_max_ratio=threshold,
        )

    def fetch_records(self, settings: Settings, records: list[dict]) -> None:
        run_fetch(settings, ScriptedClient(records))

    @staticmethod
    def quarantine_lines(settings: Settings) -> list[dict]:
        path = Path(settings.curated_dir) / QUARANTINE_NAME
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]


class CuratePartitionTests(CurateTestBase):
    def test_all_healthy_writes_empty_quarantine_and_passes(self) -> None:
        settings = self.settings()
        records = [healthy_record(i) for i in range(25)]
        self.fetch_records(settings, records)
        result = run_curate(settings)
        self.assertEqual(result.total, 25)
        self.assertEqual(result.passed, 25)
        self.assertEqual(result.quarantined, 0)
        self.assertEqual(result.counts, {})
        self.assertEqual(result.ratio, 0.0)
        self.assertFalse(result.exceeded)
        self.assertEqual(self.quarantine_lines(settings), [])
        ensure_within_threshold(result)  # must not raise

    def test_violators_are_quarantined_with_record_and_reasons(self) -> None:
        settings = self.settings(threshold=0.5)
        bad_zip = {**healthy_record(0), "unique_key": "2000", "incident_zip": "00000"}
        bad_date = {**healthy_record(1), "unique_key": "2001", "created_date": "garbage"}
        records = [healthy_record(i) for i in range(8)] + [bad_zip, bad_date]
        self.fetch_records(settings, records)
        result = run_curate(settings)

        self.assertEqual(result.total, 10)
        self.assertEqual(result.passed, 8)
        self.assertEqual(result.quarantined, 2)
        self.assertEqual(result.ratio, 0.2)
        self.assertEqual(
            result.counts,
            {"INVALID_CREATED_DATE": 1, "INVALID_ZIP_CODE": 1},
        )
        lines = self.quarantine_lines(settings)
        self.assertEqual(len(lines), 2)
        by_code = {line["reasons"][0]: line for line in lines}
        self.assertEqual(by_code["INVALID_ZIP_CODE"]["record"]["unique_key"], "2000")
        self.assertEqual(by_code["INVALID_CREATED_DATE"]["record"]["unique_key"], "2001")

    def test_counts_count_records_per_reason(self) -> None:
        settings = self.settings(threshold=1.0)
        records = [
            {**healthy_record(i), "unique_key": None, "incident_zip": "00000"}
            for i in range(3)
        ] + [healthy_record(99)]
        self.fetch_records(settings, records)
        result = run_curate(settings)
        self.assertEqual(result.counts, {"INVALID_ZIP_CODE": 3, "MISSING_UNIQUE_KEY": 3})
        self.assertEqual(result.quarantined, 3)
        self.assertEqual(result.total, 4)

    def test_duplicate_records_quarantined_with_duplicate_reasons(self) -> None:
        settings = self.settings(threshold=1.0)
        records = [
            healthy_record(0),
            {**healthy_record(1), "unique_key": "1000"},  # same key again
        ]
        self.fetch_records(settings, records)
        result = run_curate(settings)
        self.assertEqual(result.quarantined, 1)
        self.assertEqual(result.counts, {"DUPLICATE_KEY": 1})
        line = self.quarantine_lines(settings)[0]
        self.assertEqual(line["reasons"], ["DUPLICATE_KEY"])

    def test_threshold_exceeded_fails_the_run_with_measured_numbers(self) -> None:
        settings = self.settings(threshold=0.05)
        records = [healthy_record(i) for i in range(19)] + [
            {**healthy_record(99), "incident_zip": "00000"}
        ]  # 1/20 = 5% exactly -> NOT above threshold (strictly greater fails)
        self.fetch_records(settings, records)
        result = run_curate(settings)
        self.assertFalse(result.exceeded)
        ensure_within_threshold(result)

        records_over = records + [{**healthy_record(100), "agency": None}]  # 2/21 > 5%
        settings_over = dataclasses.replace(
            settings, raw_dir=self.tmp_path / "raw2", curated_dir=self.tmp_path / "curated2"
        )
        self.fetch_records(settings_over, records_over)
        result_over = run_curate(settings_over)
        self.assertTrue(result_over.exceeded)
        with self.assertRaises(QuarantineThresholdExceeded) as ctx:
            ensure_within_threshold(result_over)
        message = str(ctx.exception)
        self.assertIn("exceeds threshold", message)
        self.assertIn("2 of 21", message)
        self.assertIn("INVALID_ZIP_CODE", message)
        self.assertEqual(QuarantineThresholdExceeded.exit_code, 5)


class CurateGuardTests(CurateTestBase):
    def test_curate_without_fetch_is_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            run_curate(self.settings())
        self.assertIn("fetch stage first", str(ctx.exception))

    def test_curate_with_incomplete_fetch_is_rejected(self) -> None:
        from tests.test_fetch import FakeFetchClient

        settings = self.settings(batch_size=10)
        with self.assertRaises(Exception):  # simulated network failure mid-fetch
            run_fetch(settings, FakeFetchClient(30, fail_at_offset=20))
        with self.assertRaises(ValidationError) as ctx:
            run_curate(settings)
        self.assertIn("incomplete", str(ctx.exception))

    def test_quarantine_output_is_byte_deterministic(self) -> None:
        records = [
            healthy_record(0),
            {**healthy_record(1), "incident_zip": "00000"},
            {**healthy_record(2), "complaint_type": "Pothole"},
        ]
        settings_a = self.settings(threshold=1.0)
        self.fetch_records(settings_a, records)
        run_curate(settings_a)
        bytes_a = (Path(settings_a.curated_dir) / QUARANTINE_NAME).read_bytes()

        settings_b = dataclasses.replace(
            settings_a,
            raw_dir=self.tmp_path / "raw_b",
            curated_dir=self.tmp_path / "curated_b",
        )
        self.fetch_records(settings_b, records)
        run_curate(settings_b)
        bytes_b = (Path(settings_b.curated_dir) / QUARANTINE_NAME).read_bytes()
        self.assertEqual(bytes_a, bytes_b)

    def test_no_temp_files_left_behind(self) -> None:
        settings = self.settings(threshold=1.0)
        self.fetch_records(
            settings, [{**healthy_record(1), "incident_zip": "00000"}]
        )
        run_curate(settings)
        self.assertEqual(list(Path(settings.curated_dir).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()

"""Tests for the batched, resumable, idempotent fetch stage (section 4.2)."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from data_factory.config import Settings, load_config
from data_factory.errors import NetworkError, ValidationError
from tests.test_curate import FAKE_SOURCE_FIELDS

from data_factory import fetch
from data_factory.fetch import (
    BATCH_NAME_TEMPLATE,
    CHECKPOINT_NAME,
    WINDOW_MODE_SINCE_LAST_RUN,
    FetchResult,
    compute_fingerprint,
    metadata_url,
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
        schema: dict[str, str] | None = None,
        fail_metadata: Exception | None = None,
    ) -> None:
        self.total = total
        self.fail_at_offset = fail_at_offset
        self.columns_per_batch = columns_per_batch
        self.schema = schema or {name: "text" for name in FAKE_SOURCE_FIELDS}
        self.fail_metadata = fail_metadata
        self.request_count = 0
        self.offsets: list[int] = []

    def get(self, url: str, *, params: dict | None = None):  # noqa: ANN001
        self.request_count += 1
        if params is None:  # metadata request (schema audit)
            if self.fail_metadata is not None:
                raise self.fail_metadata
            return {
                "columns": [
                    {"fieldName": name, "dataTypeName": dtype}
                    for name, dtype in sorted(self.schema.items())
                ]
            }
        offset = int(params["$offset"])
        self.offsets.append(offset)
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
        # 3 data pages + 1 schema-audit request (section 4.8.2)
        self.assertEqual(result.http_requests, 4)
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
        # 2 data pages + the probe + the schema audit
        self.assertEqual(result.http_requests, 4)
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

    def test_sparse_column_appearing_mid_run_is_recorded_not_fatal(self) -> None:
        """Regression: a live run died at page 36 when ``vehicle_type`` appeared.

        Socrata omits a field when every value in the page is null, so a new
        key mid-run is normal sparsity, not a shape change (AGENTS 4.8.2).
        """
        settings = self.settings(batch_size=2)
        client = FakeFetchClient(
            4,
            columns_per_batch=[{"unique_key", "a"}, {"unique_key", "a", "vehicle_type"}],
        )
        result = run_fetch(settings, client)
        self.assertEqual(result.records, 4)
        self.assertEqual(result.new_columns, ("vehicle_type",))
        state = json.loads(
            (settings.raw_dir / CHECKPOINT_NAME).read_text(encoding="utf-8")
        )
        self.assertIn("vehicle_type", state["columns"])

    def test_column_disappearing_from_a_page_is_not_fatal(self) -> None:
        """The mirror case: a sparse field absent from a later page is fine."""
        settings = self.settings(batch_size=2)
        client = FakeFetchClient(
            4,
            columns_per_batch=[{"unique_key", "a", "b"}, {"unique_key", "a"}],
        )
        result = run_fetch(settings, client)
        self.assertEqual(result.records, 4)

    def test_field_turning_into_an_object_is_fatal(self) -> None:
        """A real type change at the value level stops the run loudly."""
        settings = self.settings(batch_size=2)

        class ObjectClient(FakeFetchClient):
            def get(self, url, *, params=None):  # noqa: ANN001
                if params is None:
                    return super().get(url, params=params)
                rows = super().get(url, params=params)
                if rows:
                    rows[0] = {**rows[0], "created_date": {"$date": "2023-01-01"}}
                return rows

        with self.assertRaises(ValidationError) as ctx:
            run_fetch(settings, ObjectClient(4),)
        self.assertIn("source type changed", str(ctx.exception))
        self.assertIn("created_date", str(ctx.exception))


class SchemaAuditTests(FetchTestBase):
    """Layer 1 of the 4.8.2 guard: the dataset's declared schema."""

    def test_metadata_url_is_derived_from_a_socrata_resource_url(self) -> None:
        self.assertEqual(
            metadata_url("https://data.cityofnewyork.us/resource/erm2-nwe9.json"),
            "https://data.cityofnewyork.us/api/views/erm2-nwe9.json",
        )
        self.assertIsNone(metadata_url("https://example.test/v1/records.json"))

    def test_audit_records_the_declared_schema_in_the_checkpoint(self) -> None:
        settings = self.settings()
        result = run_fetch(settings, FakeFetchClient(5))
        self.assertEqual(result.schema_audit, "verified")
        state = json.loads(
            (settings.raw_dir / CHECKPOINT_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(state["schema_audit"], "verified")
        self.assertIn("incident_zip", state["source_schema"])

    def test_removed_mapped_field_fails_the_audit(self) -> None:
        settings = self.settings()
        schema = {name: "text" for name in FAKE_SOURCE_FIELDS if name != "incident_zip"}
        with self.assertRaises(ValidationError) as ctx:
            run_fetch(settings, FakeFetchClient(5, schema=schema))
        message = str(ctx.exception)
        self.assertIn("no longer declares mapped field", message)
        self.assertIn("incident_zip", message)

    def test_declared_type_change_between_invocations_fails_the_audit(self) -> None:
        settings = self.settings(batch_size=10)
        with self.assertRaises(NetworkError):  # interrupt so the checkpoint stays open
            run_fetch(settings, FakeFetchClient(25, fail_at_offset=20))

        changed = {name: "text" for name in FAKE_SOURCE_FIELDS}
        changed["created_date"] = "number"
        with self.assertRaises(ValidationError) as ctx:
            run_fetch(settings, FakeFetchClient(25, schema=changed))
        message = str(ctx.exception)
        self.assertIn("changed shape", message)
        self.assertIn("created_date: text -> number", message)

    def test_removed_column_between_invocations_fails_the_audit(self) -> None:
        """An unmapped column disappearing is still a shape change (4.8.2)."""
        settings = self.settings(batch_size=10)
        wide = {name: "text" for name in FAKE_SOURCE_FIELDS}
        wide["vehicle_type"] = "text"
        with self.assertRaises(NetworkError):
            run_fetch(settings, FakeFetchClient(25, fail_at_offset=20, schema=wide))

        shrunk = {name: "text" for name in FAKE_SOURCE_FIELDS}
        with self.assertRaises(ValidationError) as ctx:
            run_fetch(settings, FakeFetchClient(25, schema=shrunk))
        message = str(ctx.exception)
        self.assertIn("changed shape", message)
        self.assertIn("removed=['vehicle_type']", message)

    def test_removing_a_mapped_field_is_caught_even_on_the_first_page(self) -> None:
        settings = self.settings()
        shrunk = {name: "text" for name in FAKE_SOURCE_FIELDS if name != "descriptor"}
        with self.assertRaises(ValidationError) as ctx:
            run_fetch(settings, FakeFetchClient(5, schema=shrunk))
        self.assertIn("no longer declares mapped field(s) ['descriptor']", str(ctx.exception))

    def test_unreachable_metadata_degrades_instead_of_breaking_the_fetch(self) -> None:
        settings = self.settings()
        client = FakeFetchClient(5, fail_metadata=NetworkError("metadata down"))
        result = run_fetch(settings, client)
        self.assertEqual(result.records, 5)
        self.assertTrue(result.schema_audit.startswith("unavailable:"))
        state = json.loads(
            (settings.raw_dir / CHECKPOINT_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(state["schema_audit"], result.schema_audit)
        self.assertNotIn("source_schema", state)

    def test_non_socrata_source_skips_the_audit(self) -> None:
        settings = dataclasses.replace(
            self.settings(), source_url="https://example.test/v1/records"
        )
        result = run_fetch(settings, FakeFetchClient(5))
        self.assertEqual(result.schema_audit, "skipped: source URL is not a Socrata resource")

    def test_completed_checkpoint_stays_a_zero_request_noop(self) -> None:
        """The audit must not cost a request when there is nothing to fetch."""
        settings = self.settings()
        run_fetch(settings, FakeFetchClient(5))
        client = FakeFetchClient(5)
        result = run_fetch(settings, client)
        self.assertTrue(result.already_complete)
        self.assertEqual(client.request_count, 0)

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


class ProgressTests(FetchTestBase):
    """A long fetch must show life; reporting must never break it (4.2 UX)."""

    def test_progress_is_reported_every_ten_thousand_records(self) -> None:
        settings = self.settings(batch_size=10)
        seen: list[tuple[int, int, int]] = []
        result = run_fetch(
            settings,
            FakeFetchClient(25),
            on_progress=lambda records, batches, requests: seen.append(
                (records, batches, requests)
            ),
        )
        self.assertEqual(result.records, 25)
        self.assertEqual(seen, [], "25 records is below one progress interval")

    def test_progress_fires_on_the_interval_boundary(self) -> None:
        settings = self.settings(batch_size=10)
        seen: list[tuple[int, int, int]] = []
        original = fetch.PROGRESS_EVERY_RECORDS
        fetch.PROGRESS_EVERY_RECORDS = 20
        try:
            run_fetch(
                settings,
                FakeFetchClient(50),
                on_progress=lambda records, batches, requests: seen.append(
                    (records, batches, requests)
                ),
            )
        finally:
            fetch.PROGRESS_EVERY_RECORDS = original
        self.assertEqual([item[0] for item in seen], [20, 40])

    def test_a_failing_progress_hook_never_breaks_the_fetch(self) -> None:
        """A closed terminal raises BrokenPipeError; the fetch must finish."""
        settings = self.settings(batch_size=10)
        original = fetch.PROGRESS_EVERY_RECORDS
        fetch.PROGRESS_EVERY_RECORDS = 10
        calls: list[int] = []

        def explode(records: int, batches: int, requests: int) -> None:
            calls.append(records)
            raise BrokenPipeError("terminal closed")

        try:
            result = run_fetch(settings, FakeFetchClient(50), on_progress=explode)
        finally:
            fetch.PROGRESS_EVERY_RECORDS = original
        self.assertEqual(result.records, 50)
        self.assertEqual(calls, [10], "reporting stops after the first failure")

    def test_progress_does_not_change_the_written_data(self) -> None:
        plain = self.tmp_path / "plain"
        with_progress = self.tmp_path / "progress"
        run_fetch(self.settings(raw_dir=plain), FakeFetchClient(30))
        run_fetch(
            self.settings(raw_dir=with_progress),
            FakeFetchClient(30),
            on_progress=lambda *args: None,
        )
        self.assertEqual(
            sorted(p.name for p in plain.glob("*.json")),
            sorted(p.name for p in with_progress.glob("*.json")),
        )
        for path in plain.glob("batch_*.json"):
            self.assertEqual(
                path.read_bytes(), (with_progress / path.name).read_bytes()
            )


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

"""Integration tests: clean stage — types, missing policies, join (4.4, 3)."""

from __future__ import annotations

import csv
import dataclasses
import tempfile
import unittest
from pathlib import Path

from data_factory.clean import CASES_NAME, CURATED_COLUMNS, run_clean
from data_factory.config import Settings, load_config
from data_factory.curate import QUARANTINE_NAME, run_curate
from data_factory.errors import ValidationError
from data_factory.fetch import run_fetch
from tests.test_curate import ScriptedClient, healthy_record

POPULATION_CSV = (
    "zip,population,borough\n"
    "11101,30000,Queens\n"
    "10001,25000,New York\n"  # county alias -> Manhattan
    "11201,20000,Kings\n"     # county alias -> Brooklyn
)


class CleanTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.population_path = self.tmp_path / "pop.csv"
        self.population_path.write_text(POPULATION_CSV, encoding="utf-8")

    def settings(self, *, batch_size: int = 10, threshold: float = 0.5) -> Settings:
        return dataclasses.replace(
            load_config(),
            batch_size=batch_size,
            raw_dir=self.tmp_path / "raw",
            curated_dir=self.tmp_path / "curated",
            quarantine_max_ratio=threshold,
            population_path=self.population_path,
        )

    def pipeline(self, records: list[dict], settings: Settings) -> None:
        run_fetch(settings, ScriptedClient(records))
        run_curate(settings)

    def read_cases(self, settings: Settings) -> tuple[list[str], list[dict]]:
        path = Path(settings.curated_dir) / CASES_NAME
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            header = next(reader)
            rows = [dict(zip(header, row)) for row in reader]
        return header, rows


class CleanOutputTests(CleanTestBase):
    def test_header_types_and_duration_computation(self) -> None:
        """4.6 derived columns: duration from dates, including edge cases."""
        settings = self.settings()
        records = [
            healthy_record(0),  # 2023-05-01T08:30 -> 2023-05-03T14:00 = 3210 min
            {**healthy_record(1), "closed_date": "2023-05-01T08:30:00.000"},  # equal -> 0
            {**healthy_record(2), "closed_date": None},  # open case -> null
        ]
        self.pipeline(records, settings)
        result = run_clean(settings)

        header, rows = self.read_cases(settings)
        self.assertEqual(header, list(CURATED_COLUMNS))
        self.assertEqual(result.rows_written, 3)
        self.assertEqual(result.duration_nulls, 1)
        by_key = {row["unique_key"]: row for row in rows}
        self.assertEqual(by_key["1000"]["duration_minutes"], "3210")
        self.assertEqual(by_key["1001"]["duration_minutes"], "0")
        self.assertEqual(by_key["1002"]["duration_minutes"], "")

    def test_zip_missing_policy_and_population_join(self) -> None:
        settings = self.settings()
        records = [
            {**healthy_record(0), "incident_zip": "11101", "borough": "Queens"},
            {**healthy_record(1), "incident_zip": "N/A"},           # marker -> null
            {**healthy_record(2), "incident_zip": "99999"},          # unknown to reference
            {**healthy_record(3), "incident_zip": None},             # absent
        ]
        self.pipeline(records, settings)
        result = run_clean(settings)
        _, rows = self.read_cases(settings)
        by_key = {row["unique_key"]: row for row in rows}

        self.assertEqual(result.zip_matched, 1)
        self.assertEqual(result.zip_missing, 2)   # N/A + absent
        self.assertEqual(result.zip_unknown, 1)   # 99999
        self.assertEqual(by_key["1000"]["population"], "30000")
        self.assertEqual(by_key["1001"]["population"], "")
        self.assertEqual(by_key["1002"]["population"], "")
        self.assertEqual(by_key["1003"]["population"], "")

    def test_district_label_is_canonicalised_from_the_311_spelling(self) -> None:
        """311 publishes BROOKLYN; the reference publishes Brooklyn.

        Regression: the district join matched nothing on real data, so every
        rate cell in the aggregate and the EDA came out blank.
        """
        settings = self.settings()
        records = [
            {**healthy_record(0), "borough": "BROOKLYN", "incident_zip": "11201"},
            {**healthy_record(1), "borough": "BROOKLYN", "incident_zip": "11101"},
            {**healthy_record(2), "borough": "MANHATTAN", "incident_zip": "10001"},
            {**healthy_record(3), "borough": "Unspecified", "incident_zip": "11101"},
            {**healthy_record(4), "borough": None, "incident_zip": "11101"},
        ]
        self.pipeline(records, settings)
        run_clean(settings)
        _, rows = self.read_cases(settings)
        by_key = {row["unique_key"]: row for row in rows}

        self.assertEqual(by_key["1000"]["borough"], "Brooklyn")
        self.assertEqual(by_key["1001"]["borough"], "Brooklyn")
        self.assertEqual(by_key["1002"]["borough"], "Manhattan")
        self.assertEqual(by_key["1003"]["borough"], "Unspecified", "never invented")
        self.assertEqual(by_key["1004"]["borough"], "", "absent stays null")

    def test_canonical_district_still_flags_a_real_mismatch(self) -> None:
        """Normalising the label must not hide the cross-check (AGENTS 3)."""
        settings = self.settings()
        records = [
            # ZIP belongs to Queens in the reference, 311 says Brooklyn
            {**healthy_record(0), "borough": "BROOKLYN", "incident_zip": "11101"},
            {**healthy_record(1), "borough": "BRONX", "incident_zip": "11101"},
        ]
        self.pipeline(records, settings)
        result = run_clean(settings)
        self.assertEqual(result.borough_mismatches, 2)
        _, rows = self.read_cases(settings)
        self.assertEqual(rows[0]["borough"], "Brooklyn", "the 311 claim is kept")

    def test_join_never_changes_row_count(self) -> None:
        """4.4: rows before/after the join must be explainable — here: equal."""
        settings = self.settings()
        records = [healthy_record(i) for i in range(7)]
        self.pipeline(records, settings)
        result = run_clean(settings)
        self.assertEqual(result.rows_written, result.rows_scanned - result.rows_skipped)
        self.assertEqual(
            result.rows_written,
            result.zip_matched + result.zip_missing + result.zip_unknown,
        )

    def test_borough_cross_check_counts_mismatches_without_dropping(self) -> None:
        """3: compare the reference mapping with 311's own borough column."""
        settings = self.settings()
        records = [
            {**healthy_record(0), "incident_zip": "11101", "borough": "Brooklyn"},  # ref Queens
            {**healthy_record(1), "incident_zip": "11101", "borough": "Unspecified"},  # no claim
            {**healthy_record(2), "incident_zip": "10001", "borough": "Manhattan"},  # New York -> match
            {**healthy_record(3), "incident_zip": "11201", "borough": "Brooklyn"},   # Kings -> match
        ]
        self.pipeline(records, settings)
        result = run_clean(settings)
        self.assertEqual(result.borough_mismatches, 1)
        self.assertEqual(result.rows_written, 4)  # mismatch kept, never dropped

    def test_string_values_are_stripped(self) -> None:
        settings = self.settings()
        records = [{**healthy_record(0), "borough": "  Queens ", "status": "Closed  "}]
        self.pipeline(records, settings)
        run_clean(settings)
        _, rows = self.read_cases(settings)
        self.assertEqual(rows[0]["borough"], "Queens")
        self.assertEqual(rows[0]["status"], "Closed")

    def test_quarantined_records_absent_and_skip_count_cross_checked(self) -> None:
        settings = self.settings(threshold=1.0)
        records = [healthy_record(i) for i in range(5)] + [
            {**healthy_record(6), "incident_zip": "00000"},
            {**healthy_record(7), "created_date": "garbage"},
        ]
        self.pipeline(records, settings)
        result = run_clean(settings)
        _, rows = self.read_cases(settings)
        self.assertEqual(result.rows_written, 5)
        self.assertEqual(result.rows_skipped, 2)
        self.assertEqual(len(rows), 5)
        self.assertNotIn("1006", {row["unique_key"] for row in rows})


class CleanGuardTests(CleanTestBase):
    def test_clean_without_curate_is_rejected(self) -> None:
        settings = self.settings()
        run_fetch(settings, ScriptedClient([healthy_record(0)]))
        with self.assertRaises(ValidationError) as ctx:
            run_clean(settings)
        self.assertIn("curate stage", str(ctx.exception))

    def test_tampered_quarantine_output_detected(self) -> None:
        settings = self.settings()
        self.pipeline([healthy_record(0)], settings)
        qpath = Path(settings.curated_dir) / QUARANTINE_NAME
        qpath.write_text('{"reasons":["X"],"record":{}}\n', encoding="utf-8")
        with self.assertRaises(ValidationError) as ctx:
            run_clean(settings)
        self.assertIn("gate inconsistency", str(ctx.exception))

    def test_missing_population_reference_is_a_clear_error(self) -> None:
        settings = dataclasses.replace(
            self.settings(), population_path=self.tmp_path / "missing.csv"
        )
        self.pipeline([healthy_record(0)], settings)
        with self.assertRaises(ValidationError) as ctx:
            run_clean(settings)
        self.assertIn("not found", str(ctx.exception))
        self.assertIn("README", str(ctx.exception))

    def test_output_is_byte_deterministic_across_runs(self) -> None:
        records = [healthy_record(i) for i in range(6)]
        settings_a = self.settings()
        self.pipeline(records, settings_a)
        run_clean(settings_a)
        bytes_a = (Path(settings_a.curated_dir) / CASES_NAME).read_bytes()

        settings_b = dataclasses.replace(
            settings_a,
            raw_dir=self.tmp_path / "raw_b",
            curated_dir=self.tmp_path / "curated_b",
        )
        self.pipeline(records, settings_b)
        run_clean(settings_b)
        bytes_b = (Path(settings_b.curated_dir) / CASES_NAME).read_bytes()
        self.assertEqual(bytes_a, bytes_b)

    def test_no_temp_files_left_behind(self) -> None:
        settings = self.settings()
        self.pipeline([healthy_record(0)], settings)
        run_clean(settings)
        self.assertEqual(list(Path(settings.curated_dir).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()

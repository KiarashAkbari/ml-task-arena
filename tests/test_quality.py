"""Tests for the generated data-quality report (AGENTS.md 4.8.4).

The report must be produced by the factory from the delivered dataset — per
column: percent empty, unique count, range — and must be deterministic and
hashed into the run manifest.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from data_factory.clean import CASES_NAME, CURATED_COLUMNS
from data_factory.config import Settings, load_config
from data_factory.errors import ValidationError
from data_factory.manifest import MANIFEST_NAME
from data_factory.quality import (
    COLUMN_KINDS,
    DATA_QUALITY_NAME,
    DATE,
    INTEGER,
    REPORT_HEADER,
    TEXT,
    run_quality_report,
)

CASES_CSV = (
    "unique_key,created_date,closed_date,agency,complaint_type,descriptor,"
    "borough,zip_code,status,population,duration_minutes\n"
    "1,2023-01-01T00:00:00.000,2023-01-02T00:00:00.000,NYPD,Noise - Residential,"
    "Loud Music/Party,Brooklyn,11201,Closed,20000,1440\n"
    "2,2023-01-15T00:00:00.000,,NYPD,Noise - Park,,Queens,11101,Open,,\n"
    "3,2023-02-01T00:00:00.000,2023-02-01T00:10:00.000,NYPD,Noise - Residential,"
    "Loud Music/Party,Queens,11101,Closed,30000,9\n"
)


class QualityTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.curated_dir = self.tmp_path / "curated"
        self.curated_dir.mkdir()

    def settings(self) -> Settings:
        return dataclasses.replace(load_config(), curated_dir=self.curated_dir)

    def write_cases(self, text: str) -> None:
        (self.curated_dir / CASES_NAME).write_text(text, encoding="utf-8")

    def read_report(self) -> list[dict[str, str]]:
        with (self.curated_dir / DATA_QUALITY_NAME).open(
            newline="", encoding="utf-8"
        ) as handle:
            return list(csv.DictReader(handle))


class SchemaTests(QualityTestBase):
    def test_kinds_cover_every_curated_column(self) -> None:
        self.assertEqual(sorted(COLUMN_KINDS), sorted(CURATED_COLUMNS))
        self.assertTrue(set(COLUMN_KINDS.values()) <= {TEXT, INTEGER, DATE})

    def test_report_has_one_row_per_column_in_dataset_order(self) -> None:
        self.write_cases(CASES_CSV)
        result = run_quality_report(self.settings())
        report = self.read_report()

        self.assertEqual(result.rows, 3)
        self.assertEqual(result.columns, len(CURATED_COLUMNS))
        self.assertEqual([row["column"] for row in report], list(CURATED_COLUMNS))
        with (self.curated_dir / DATA_QUALITY_NAME).open(encoding="utf-8") as handle:
            self.assertEqual(handle.readline().strip().split(","), list(REPORT_HEADER))


class StatisticsTests(QualityTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.write_cases(CASES_CSV)
        run_quality_report(self.settings())
        self.by_column = {row["column"]: row for row in self.read_report()}

    def test_empty_share_per_column(self) -> None:
        self.assertEqual(self.by_column["unique_key"]["empty_count"], "0")
        self.assertEqual(self.by_column["unique_key"]["empty_pct"], "0.00")
        self.assertEqual(self.by_column["closed_date"]["empty_count"], "1")
        self.assertEqual(self.by_column["closed_date"]["empty_pct"], "33.33")
        self.assertEqual(self.by_column["population"]["empty_count"], "1")
        self.assertEqual(self.by_column["population"]["empty_pct"], "33.33")

    def test_unique_counts_are_measured(self) -> None:
        self.assertEqual(self.by_column["unique_key"]["unique_count"], "3")
        self.assertEqual(self.by_column["borough"]["unique_count"], "2")
        self.assertEqual(self.by_column["complaint_type"]["unique_count"], "2")

    def test_ranges_are_kind_aware(self) -> None:
        # Text: lexicographic, and the range is still reported.
        self.assertEqual(self.by_column["borough"]["min"], "Brooklyn")
        self.assertEqual(self.by_column["borough"]["max"], "Queens")
        # Dates: ISO strings are chronological.
        self.assertEqual(self.by_column["closed_date"]["min"], "2023-01-02T00:00:00.000")
        self.assertEqual(self.by_column["closed_date"]["max"], "2023-02-01T00:10:00.000")
        # Integers: numeric, not lexicographic ("9" < "1440" numerically).
        self.assertEqual(self.by_column["duration_minutes"]["kind"], INTEGER)
        self.assertEqual(self.by_column["duration_minutes"]["min"], "9")
        self.assertEqual(self.by_column["duration_minutes"]["max"], "1440")
        self.assertEqual(self.by_column["population"]["min"], "20000")
        self.assertEqual(self.by_column["population"]["max"], "30000")

    def test_top_values_are_counted_and_ranked(self) -> None:
        self.assertEqual(self.by_column["borough"]["top_values"], "Queens=2|Brooklyn=1")
        # Identifier columns get no frequency profile (memory + meaning).
        self.assertEqual(self.by_column["unique_key"]["top_values"], "")

    def test_result_summary_names_the_emptiest_column(self) -> None:
        result = run_quality_report(self.settings())
        # closed_date / population / duration_minutes tie at 1 empty cell;
        # ties break on dataset column order -> closed_date comes first.
        self.assertEqual(result.emptiest_column, "closed_date")
        self.assertAlmostEqual(result.emptiest_pct, 33.333333, places=5)


class EdgeCaseTests(QualityTestBase):
    def test_fully_empty_column_reports_100_percent_and_no_range(self) -> None:
        self.write_cases(CASES_CSV)
        run_quality_report(self.settings())
        by_column = {row["column"]: row for row in self.read_report()}
        # descriptor is empty in exactly one of three rows here...
        self.assertEqual(by_column["descriptor"]["empty_pct"], "33.33")

        only_empties = CASES_CSV.splitlines()[0] + "\n" + "\n".join(
            line.replace("Loud Music/Party", "") for line in CASES_CSV.splitlines()[1:]
        )
        self.write_cases(only_empties + "\n")
        run_quality_report(self.settings())
        by_column = {row["column"]: row for row in self.read_report()}
        self.assertEqual(by_column["descriptor"]["empty_pct"], "100.00")
        self.assertEqual(by_column["descriptor"]["unique_count"], "0")
        self.assertEqual(by_column["descriptor"]["min"], "")
        self.assertEqual(by_column["descriptor"]["max"], "")

    def test_header_only_dataset_reports_zero_rows(self) -> None:
        self.write_cases(CASES_CSV.splitlines()[0] + "\n")
        result = run_quality_report(self.settings())
        self.assertEqual(result.rows, 0)
        self.assertEqual(result.emptiest_column, "")
        for row in self.read_report():
            self.assertEqual(row["empty_pct"], "0.00")

    def test_missing_case_dataset_is_refused(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            run_quality_report(self.settings())
        self.assertIn("clean stage", str(ctx.exception))

    def test_changed_shape_is_refused_loudly(self) -> None:
        """AGENTS 4.8.2: a silent shape change must stop the run."""
        self.write_cases(
            "unique_key,created_date\n" "1,2023-01-01T00:00:00.000\n"
        )
        with self.assertRaises(ValidationError) as ctx:
            run_quality_report(self.settings())
        self.assertIn("changed shape", str(ctx.exception))

    def test_non_integer_in_integer_column_is_refused(self) -> None:
        self.write_cases(CASES_CSV.replace(",Closed,30000,9", ",Closed,30000,nine"))
        with self.assertRaises(ValidationError) as ctx:
            run_quality_report(self.settings())
        self.assertIn("integer column", str(ctx.exception))


class DeterminismTests(QualityTestBase):
    def test_two_runs_over_the_same_dataset_are_byte_identical(self) -> None:
        self.write_cases(CASES_CSV)
        run_quality_report(self.settings())
        first = (self.curated_dir / DATA_QUALITY_NAME).read_bytes()
        run_quality_report(self.settings())
        second = (self.curated_dir / DATA_QUALITY_NAME).read_bytes()
        self.assertEqual(first, second)
        self.assertEqual(list(self.curated_dir.glob("*.tmp")), [])


class ManifestIntegrationTests(QualityTestBase):
    def test_manifest_hashes_the_generated_report(self) -> None:
        from data_factory.clean import run_clean
        from data_factory.curate import run_curate
        from data_factory.fetch import run_fetch
        from data_factory.outputs import run_outputs
        from data_factory.manifest import sha256_file
        from tests.test_curate import ScriptedClient, healthy_record

        population_path = self.tmp_path / "pop.csv"
        population_path.write_text(
            "zip,population,borough\n11101,30000,Queens\n", encoding="utf-8"
        )
        settings = dataclasses.replace(
            load_config(),
            raw_dir=self.tmp_path / "raw",
            curated_dir=self.curated_dir,
            raw_archive_dir=self.tmp_path / "raw_archived",
            population_path=population_path,
        )
        run_fetch(settings, ScriptedClient([healthy_record(0), healthy_record(1)]))
        run_curate(settings)
        run_clean(settings)
        result = run_outputs(settings, started_at="s", finished_at="f")

        report_path = self.curated_dir / DATA_QUALITY_NAME
        self.assertTrue(report_path.is_file())
        manifest = json.loads(
            (self.curated_dir / MANIFEST_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["outputs"][DATA_QUALITY_NAME]["sha256"], sha256_file(report_path))
        self.assertEqual(
            result.output_hashes[DATA_QUALITY_NAME],
            manifest["outputs"][DATA_QUALITY_NAME]["sha256"],
        )
        self.assertEqual(result.quality.rows, 2)


if __name__ == "__main__":
    unittest.main()

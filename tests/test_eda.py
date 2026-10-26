"""Tests for the EDA helpers the notebook narrates (AGENTS.md section 4.9).

The notebook must not compute anything itself (section 5.6), so the analysis
functions are ordinary code and are tested here on a tiny dataset whose
expected values are worked out by hand.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from data_factory.clean import CURATED_COLUMNS
from data_factory.eda import (
    DURATION_BUCKETS,
    borough_rates,
    borough_type_rates,
    closure_speed_by_hour,
    duration_buckets,
    duration_by_type,
    duration_summary,
    finding_metrics,
    load_cases,
    monthly_volume,
    numeric_relationships,
    open_share_by_type,
    outlier_summary,
    type_mix,
    zip_volume_vs_population,
)
from data_factory.errors import ValidationError
from data_factory.population import load_population

# (unique_key, created, closed, type, zip) — durations are exact by design:
# 30m, 180m, 2880m (2d), open, 527040m (366d), 70m, 20m, open
FIXTURE_ROWS = (
    ("1", "2023-01-01T00:00:00.000", "2023-01-01T00:30:00.000", "Noise - Residential", "11101"),
    ("2", "2023-01-05T10:00:00.000", "2023-01-05T13:00:00.000", "Noise - Residential", "11101"),
    ("3", "2023-02-01T10:00:00.000", "2023-02-03T10:00:00.000", "Noise - Residential", "11101"),
    ("4", "2023-02-10T10:00:00.000", "", "Noise - Residential", "11101"),
    ("5", "2023-03-01T00:00:00.000", "2024-03-01T00:00:00.000", "Noise - Park", "11102"),
    ("6", "2023-03-05T23:00:00.000", "2023-03-06T00:10:00.000", "Noise - Park", "11102"),
    ("7", "2023-03-06T23:00:00.000", "2023-03-06T23:20:00.000", "Noise - Park", "11102"),
    ("8", "2023-03-07T12:00:00.000", "", "Noise - Park", "11103"),
)

POPULATION_CSV = (
    "zip,population,borough\n"
    "11101,30000,Queens\n"
    "11102,15000,Queens\n"
    "11103,5000,Queens\n"
)


def cases_csv(rows=FIXTURE_ROWS) -> str:
    lines = [",".join(CURATED_COLUMNS)]
    for key, created, closed, complaint_type, zip_code in rows:
        population = {"11101": "30000", "11102": "15000", "11103": "5000"}[zip_code]
        duration = "" if not closed else str(
            int(
                (
                    pd.Timestamp(closed) - pd.Timestamp(created)
                ).total_seconds()
                // 60
            )
        )
        lines.append(
            ",".join(
                [
                    key, created, closed, "NYPD", complaint_type, "", "Queens",
                    zip_code, "Closed" if closed else "Open", population, duration,
                ]
            )
        )
    return "\n".join(lines) + "\n"


class EdaTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.cases_path = self.tmp_path / "cases.csv"
        self.cases_path.write_text(cases_csv(), encoding="utf-8")
        self.population_path = self.tmp_path / "pop.csv"
        self.population_path.write_text(POPULATION_CSV, encoding="utf-8")
        self.frame = load_cases(self.cases_path)
        self.population = load_population(self.population_path)


class LoadTests(EdaTestBase):
    def test_derived_columns_and_types(self) -> None:
        self.assertEqual(len(self.frame), 8)
        self.assertEqual(str(self.frame["duration_minutes"].dtype), "Int64")
        self.assertEqual(self.frame["is_open"].sum(), 2)
        self.assertEqual(sorted(self.frame["month"].unique()), ["2023-01", "2023-02", "2023-03"])
        self.assertEqual(self.frame["created_hour"].tolist()[:2], [0, 10])

    def test_missing_file_is_refused(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            load_cases(self.tmp_path / "absent.csv")
        self.assertIn("run the factory first", str(ctx.exception))

    def test_wrong_shape_is_refused(self) -> None:
        broken = self.tmp_path / "broken.csv"
        broken.write_text("unique_key,created_date\n1,2023-01-01T00:00:00.000\n", encoding="utf-8")
        with self.assertRaises(ValidationError) as ctx:
            load_cases(broken)
        self.assertIn("missing column", str(ctx.exception))

        extra = self.tmp_path / "extra.csv"
        extra.write_text(cases_csv().replace("\n", ",bonus\n", 1), encoding="utf-8")
        with self.assertRaises(ValidationError) as ctx:
            load_cases(extra)
        self.assertIn("extra columns", str(ctx.exception))


class DurationTests(EdaTestBase):
    def test_summary_over_closed_cases_only(self) -> None:
        summary = duration_summary(self.frame)
        self.assertEqual(summary["cases"], 8)
        self.assertEqual(summary["closed_cases"], 6)
        self.assertEqual(summary["open_cases"], 2)
        self.assertAlmostEqual(summary["open_share"], 0.25)
        self.assertAlmostEqual(summary["median_days"], (70 / 1440 + 180 / 1440) / 2, places=6)
        self.assertAlmostEqual(summary["max_days"], 366.0)

    def test_buckets_cover_every_closed_case_exactly_once(self) -> None:
        table = duration_buckets(self.frame)
        self.assertEqual(list(table["bucket"]), [label for label, _, _ in DURATION_BUCKETS])
        self.assertEqual(int(table["cases"].sum()), 6)
        by_bucket = dict(zip(table["bucket"], table["cases"]))
        self.assertEqual(by_bucket["<1h"], 2)      # 30m, 20m
        self.assertEqual(by_bucket["1-6h"], 2)     # 180m, 70m
        self.assertEqual(by_bucket["1-3d"], 1)     # 2 days
        self.assertEqual(by_bucket[">365d"], 1)    # 366 days
        self.assertAlmostEqual(float(table["share"].sum()), 1.0)

    def test_outlier_summary_reports_and_keeps_outliers(self) -> None:
        summary = outlier_summary(self.frame)
        self.assertEqual(summary["beyond_cap_cases"], 1.0)
        self.assertAlmostEqual(summary["beyond_cap_share"], 1 / 6)
        self.assertEqual(summary["max_days"], 366.0)
        self.assertLess(summary["mean_days_within_cap"], summary["mean_days_all"])

    def test_duration_by_type_orders_by_median(self) -> None:
        table = duration_by_type(self.frame)
        # Residential closed: 30m, 180m, 2d -> median 3h; Park: 366d, 70m, 20m -> 70m.
        self.assertEqual(list(table.index), ["Noise - Residential", "Noise - Park"])
        self.assertAlmostEqual(
            float(table.loc["Noise - Residential", "median_days"]), 180 / 1440
        )
        self.assertAlmostEqual(float(table.loc["Noise - Park", "median_days"]), 70 / 1440)


class CategoryTests(EdaTestBase):
    def test_open_share_by_type(self) -> None:
        table = open_share_by_type(self.frame)
        self.assertEqual(int(table.loc["Noise - Residential", "open_cases"]), 1)
        self.assertAlmostEqual(float(table.loc["Noise - Residential", "open_share"]), 0.25)
        self.assertAlmostEqual(float(table.loc["Noise - Park", "open_share"]), 0.25)

    def test_type_mix(self) -> None:
        table = type_mix(self.frame)
        self.assertEqual(int(table["cases"].sum()), 8)
        self.assertAlmostEqual(float(table["share"].sum()), 1.0)

    def test_monthly_volume_is_complete_and_sorted(self) -> None:
        table = monthly_volume(self.frame)
        self.assertEqual(list(table.index), ["2023-01", "2023-02", "2023-03"])
        self.assertEqual(list(table.columns), ["Noise - Park", "Noise - Residential"])
        self.assertEqual(int(table.to_numpy().sum()), 8)
        self.assertEqual(int(table.loc["2023-01", "Noise - Residential"]), 2)
        self.assertEqual(int(table.loc["2023-01", "Noise - Park"]), 0)

    def test_closure_speed_by_hour(self) -> None:
        table = closure_speed_by_hour(self.frame, within_minutes=60)
        self.assertEqual(int(table.loc[0, "closed_cases"]), 2)   # 30m, 366d
        self.assertAlmostEqual(float(table.loc[0, "within_share"]), 0.5)
        self.assertAlmostEqual(float(table.loc[10, "within_share"]), 0.0)
        self.assertAlmostEqual(float(table.loc[23, "within_share"]), 0.5)


class RateTests(EdaTestBase):
    def test_borough_rates_use_the_reference_denominator(self) -> None:
        table = borough_rates(self.frame, self.population)
        self.assertEqual(int(table.loc["Queens", "cases"]), 8)
        self.assertEqual(int(table.loc["Queens", "population"]), 50000)
        self.assertAlmostEqual(
            float(table.loc["Queens", "rate_per_100k"]), 8 / 50000 * 100_000
        )

    def test_unknown_district_keeps_a_null_rate(self) -> None:
        frame = self.frame.copy()
        frame.loc[0, "borough"] = "Unspecified"
        table = borough_rates(frame, self.population)
        self.assertTrue(pd.isna(table.loc["Unspecified", "rate_per_100k"]))

    def test_borough_type_rates_index(self) -> None:
        table = borough_type_rates(self.frame, self.population)
        self.assertEqual(int(table.loc[("Queens", "Noise - Park"), "cases"]), 4)

    def test_zip_fit_and_residuals(self) -> None:
        table = zip_volume_vs_population(self.frame, self.population)
        self.assertEqual(sorted(table.index), ["11101", "11102", "11103"])
        self.assertEqual(int(table.loc["11101", "cases"]), 4)
        self.assertTrue(table["residual"].notna().all())
        # residual == cases - fitted, and the share is relative to the fitted value.
        for zip_code, row in table.iterrows():
            with self.subTest(zip_code=zip_code):
                self.assertAlmostEqual(float(row["residual"]), float(row["cases"] - row["fitted"]))
                self.assertAlmostEqual(
                    float(row["residual_share"]), float(row["residual"] / row["fitted"])
                )


class LegacyLabelTests(EdaTestBase):
    """Analysis stays correct for a dataset curated before the label fix."""

    def test_uppercase_311_boroughs_still_get_a_rate(self) -> None:
        frame = self.frame.copy()
        frame["borough"] = frame["borough"].str.upper()
        self.assertEqual(set(frame["borough"]), {"QUEENS"})

        table = borough_rates(frame, load_population(self.population_path))
        row = table.loc["Queens"]
        self.assertEqual(row["cases"], len(frame))
        self.assertEqual(row["population"], 50_000)
        self.assertAlmostEqual(float(row["rate_per_100k"]), len(frame) / 50_000 * 100_000)

    def test_uppercase_boroughs_group_into_one_district(self) -> None:
        frame = self.frame.copy()
        frame["borough"] = frame["borough"].str.upper()
        table = borough_type_rates(frame, load_population(self.population_path))
        self.assertEqual(sorted({district for district, _ in table.index}), ["Queens"])

    def test_unknown_label_is_left_alone(self) -> None:
        table = borough_rates(
            pd.DataFrame({"borough": ["Atlantis"], "complaint_type": ["Noise - Park"]}),
            load_population(self.population_path),
        )
        self.assertIn("Atlantis", table.index)
        self.assertTrue(pd.isna(table.loc["Atlantis", "rate_per_100k"]))


class RelationshipTests(EdaTestBase):
    def test_correlation_matrix_shape(self) -> None:
        table = numeric_relationships(self.frame)
        self.assertEqual(list(table.columns), ["duration_days", "population", "created_hour"])
        self.assertTrue(all(abs(table.loc[c, c] - 1.0) < 1e-12 for c in table.columns))

    def test_finding_metrics_carry_the_headline_numbers(self) -> None:
        metrics = finding_metrics(self.frame, self.population)
        for key in (
            "cases", "median_days", "mean_to_median", "open_share",
            "most_open_type", "top_type", "top_count_borough", "top_rate_borough",
            "worst_residual_zip", "peak_fast_hour", "quiet_fast_hour",
        ):
            with self.subTest(key=key):
                self.assertIn(key, metrics)
        self.assertEqual(metrics["cases"], 8)
        self.assertEqual(metrics["top_type"], "Noise - Residential")
        self.assertEqual(metrics["top_count_borough"], "Queens")


if __name__ == "__main__":
    unittest.main()

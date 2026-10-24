"""Tests for the ZIP -> population reference builder (AGENTS.md section 3).

The builder is part of the answer ("finding and validating the second source
is part of the challenge"), so its rule and its parsing are tested offline:
no test may touch the network.
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from data_factory.config import load_config
from data_factory.population import load_population
from scripts.build_population_reference import (
    borough_for_zip,
    build_reference,
    parse_census_rows,
    parse_modzcta_rows,
    parse_population_csv,
    summarise,
    write_config_metadata,
    write_reference,
)


class BoroughRuleTests(unittest.TestCase):
    def test_prefixes_map_to_boroughs(self) -> None:
        cases = {
            "10001": "Manhattan",
            "10128": "Manhattan",
            "10282": "Manhattan",
            "10301": "Staten Island",
            "10312": "Staten Island",
            "10451": "Bronx",
            "10467": "Bronx",
            "11201": "Brooklyn",
            "11238": "Brooklyn",
            "11101": "Queens",
            "11375": "Queens",
            "11434": "Queens",
            "11694": "Queens",
        }
        for zip_code, expected in cases.items():
            with self.subTest(zip_code=zip_code):
                self.assertEqual(borough_for_zip(zip_code), expected)

    def test_non_nyc_and_malformed_inputs_are_dropped(self) -> None:
        # 110xx deliberately excluded: it mixes Queens border ZIPs with Nassau.
        for zip_code in ("11001", "11021", "11040", "11050",
                         "07102", "90210", "00000",
                         "1001", "100011", "abcde", ""):
            with self.subTest(zip_code=zip_code):
                self.assertIsNone(borough_for_zip(zip_code))

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        self.assertEqual(borough_for_zip(" 10001 "), "Manhattan")


class CensusParsingTests(unittest.TestCase):
    def test_parses_the_census_list_of_lists_shape(self) -> None:
        payload = [
            ["NAME", "P1_001N", "zip code tabulation area"],
            ["ZCTA5 10001", "21142", "10001"],
            ["ZCTA5 11201", "58732", "11201"],
        ]
        self.assertEqual(
            parse_census_rows(payload), {"10001": 21142, "11201": 58732}
        )

    def test_falls_back_to_the_name_column_for_the_zip(self) -> None:
        payload = [
            ["NAME", "P001001"],
            ["ZCTA5 10002", "81117"],
        ]
        self.assertEqual(parse_census_rows(payload), {"10002": 81117})

    def test_rejects_payloads_without_a_population_column(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_census_rows([["NAME", "BOGUS"], ["ZCTA5 10001", "1"]])
        self.assertIn("no total-population column", str(ctx.exception))

    def test_rejects_error_objects_and_empty_payloads(self) -> None:
        for payload in ({"error": "unknown variable"}, [], [["NAME"]]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_census_rows(payload)


class LocalCsvTests(unittest.TestCase):
    def test_parses_a_zcta_population_file(self) -> None:
        text = "zcta,population\n10001,21142\n11201,\"58,732\"\n"
        self.assertEqual(
            parse_population_csv(text), {"10001": 21142, "11201": 58732}
        )

    def test_parses_a_file_with_extra_columns(self) -> None:
        text = "GEOID,name,total_pop\n10001,Manhattan,21142\n"
        self.assertEqual(parse_population_csv(text), {"10001": 21142})

    def test_rejects_files_without_recognisable_columns(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_population_csv("a,b\n1,2\n")
        self.assertIn("could not identify", str(ctx.exception))


class ModzctaParsingTests(unittest.TestCase):
    """Fallback source: NYC Open Data Modified ZCTAs (one row per MODZCTA)."""

    def test_single_zip_modzcta_is_taken_as_is(self) -> None:
        payload = [
            {"modzcta": "10001", "pop_est": "21142", "zcta": "10001"},
            {"modzcta": "11201", "pop_est": "58732", "zcta": "11201"},
        ]
        self.assertEqual(
            parse_modzcta_rows(payload), {"10001": 21142, "11201": 58732}
        )

    def test_multi_zip_modzcta_is_split_without_changing_the_total(self) -> None:
        """Summing ZIPs must never double-count one MODZCTA (rates depend on it)."""
        payload = [{"modzcta": "10001", "pop_est": "100", "zcta": "10001,10011,10018"}]
        parsed = parse_modzcta_rows(payload)
        self.assertEqual(sorted(parsed), ["10001", "10011", "10018"])
        self.assertEqual(sum(parsed.values()), 100)          # total preserved
        self.assertEqual(sorted(parsed.values()), [33, 33, 34])  # remainder to first

    def test_geo_json_feature_collection_is_accepted(self) -> None:
        payload = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {"modzcta": "10301", "pop_est": 47000,
                                                   "zcta": "10301"}, "geometry": None},
            ],
        }
        self.assertEqual(parse_modzcta_rows(payload), {"10301": 47000})

    def test_rows_without_population_or_zips_are_skipped(self) -> None:
        payload = [
            {"modzcta": "10001", "pop_est": "", "zcta": "10001"},
            {"modzcta": "10002", "pop_est": "0", "zcta": "10002"},
            {"modzcta": "10003", "pop_est": "5000", "zcta": ""},  # falls back to modzcta
        ]
        self.assertEqual(parse_modzcta_rows(payload), {"10003": 5000})

    def test_empty_payload_is_refused(self) -> None:
        for payload in ([], {"features": []}, {"error": "unknown dataset"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_modzcta_rows(payload)

    def test_fallback_output_passes_the_factory_loader(self) -> None:
        payload = [
            {"modzcta": "10001", "pop_est": "21142", "zcta": "10001,10011"},
            {"modzcta": "11201", "pop_est": "58732", "zcta": "11201"},
            {"modzcta": "11021", "pop_est": "9000", "zcta": "11021"},   # Nassau: dropped
        ]
        rows = build_reference(parse_modzcta_rows(payload))
        self.assertEqual([row[0] for row in rows], ["10001", "10011", "11201"])
        with tempfile.TemporaryDirectory() as tmp:
            path = write_reference(rows, Path(tmp) / "zip_population.csv")
            loaded = load_population(path)
        self.assertEqual(loaded["11201"].borough, "Brooklyn")


class BuildTests(unittest.TestCase):
    def test_keeps_only_nyc_zip_codes_and_positive_populations(self) -> None:
        rows = build_reference(
            {"10001": 21142, "11201": 58732, "11021": 9000, "10002": 0,
             "90210": 21000}
        )
        self.assertEqual(
            rows, [("10001", 21142, "Manhattan"), ("11201", 58732, "Brooklyn")]
        )

    def test_output_is_sorted_and_round_trips_through_the_factory_loader(self) -> None:
        rows = build_reference(
            {"11201": 58732, "10001": 21142, "10301": 47000, "10451": 30000,
             "11101": 40000}
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = write_reference(rows, Path(tmp) / "ref" / "zip_population.csv")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
            loaded = load_population(path)
        self.assertEqual(len(loaded), 5)
        self.assertEqual(loaded["10001"].borough, "Manhattan")
        self.assertEqual(
            [row[0] for row in rows], sorted(row[0] for row in rows)
        )

    def test_summary_reports_counts_and_warns_outside_plausible_bands(self) -> None:
        rows = build_reference({"10001": 21142, "11201": 58732})
        summary = summarise(rows)
        self.assertIn("2 NYC ZIPs", summary)
        self.assertIn("Manhattan", summary)
        # A two-ZIP extract is far below the real borough totals -> warns.
        self.assertIn("outside the plausible band", summary)


class ConfigMetadataTests(unittest.TestCase):
    def test_writes_source_and_date_and_keeps_the_config_valid(self) -> None:
        config = json.loads(
            (Path(__file__).resolve().parent.parent / "config" / "default.json")
            .read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "default.json"
            path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            write_config_metadata(
                path,
                source_url="https://api.census.gov/data/2020/dec/dhc?get=NAME,P1_001N",
                fetched="2026-10-24",
            )
            settings = load_config(path)
            self.assertEqual(
                settings.population_source_url,
                "https://api.census.gov/data/2020/dec/dhc?get=NAME,P1_001N",
            )
            self.assertEqual(settings.population_fetched_date.isoformat(), "2026-10-24")

    def test_invalid_values_are_rolled_back(self) -> None:
        config = json.loads(
            (Path(__file__).resolve().parent.parent / "config" / "default.json")
            .read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "default.json"
            path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            before = path.read_text(encoding="utf-8")
            with self.assertRaises(Exception):
                write_config_metadata(path, source_url="not-a-url", fetched="2026-10-24")
            self.assertEqual(path.read_text(encoding="utf-8"), before)


class CliBehaviourTests(unittest.TestCase):
    """The CLI must never be silent about whether it wrote anything."""

    def test_offline_failure_prints_a_hint_and_writes_nothing(self) -> None:
        import contextlib
        import io
        import sys
        import types

        from scripts import build_population_reference as builder

        def boom(url, timeout=0):  # noqa: ANN001
            raise OSError("blocked network")

        module = types.SimpleNamespace(get=boom)
        original = sys.modules.get("requests")
        sys.modules["requests"] = module
        target = Path(tempfile.mkdtemp()) / "never.csv"
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = builder.main(["--output", str(target)])
        finally:
            if original is not None:
                sys.modules["requests"] = original
            else:
                sys.modules.pop("requests", None)

        self.assertEqual(code, 1)
        self.assertFalse(target.exists(), "a failed run must not create a partial file")
        message = stderr.getvalue()
        self.assertIn("every population source failed", message)
        self.assertIn("NYC MODZCTA", message)
        self.assertIn("hint:", message)
        self.assertIn("--csv PATH", message)
        self.assertIn("contacting:", stdout.getvalue())

    def test_url_option_downloads_and_writes_a_csv(self) -> None:
        import contextlib
        import io
        import sys
        import types

        from scripts import build_population_reference as builder

        class Response:
            text = "zcta,population\n10001,21142\n11201,58732\n11021,9000\n"

            @staticmethod
            def raise_for_status() -> None:
                return None

        module = types.SimpleNamespace(get=lambda url, timeout=0: Response())
        original = sys.modules.get("requests")
        sys.modules["requests"] = module
        target = Path(tempfile.mkdtemp()) / "ref.csv"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = builder.main(["--url", "https://example.test/pop.csv",
                                     "--output", str(target)])
        finally:
            if original is not None:
                sys.modules["requests"] = original
            else:
                sys.modules.pop("requests", None)

        self.assertEqual(code, 0)
        lines = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "zip,population,borough")
        self.assertEqual(lines[1:], ["10001,21142,Manhattan", "11201,58732,Brooklyn"])


if __name__ == "__main__":
    unittest.main()

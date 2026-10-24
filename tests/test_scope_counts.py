"""Offline tests for the scope-count evidence script (AGENTS.md section 3)."""

from __future__ import annotations

import unittest

from data_factory.config import load_config
from data_factory.errors import ValidationError
from scripts import scope_counts


class _FakeClient:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.params: dict | None = None

    def get(self, url: str, *, params: dict | None = None):  # noqa: ANN001
        self.params = params
        return self._payload


class FetchCountTests(unittest.TestCase):
    def test_parses_socrata_count_column_variants(self) -> None:
        """Observed live: count(1) comes back under the key 'count_1'."""
        for key in ("count_1", "count"):
            with self.subTest(key=key):
                client = _FakeClient([{key: "123456"}])
                self.assertEqual(scope_counts.fetch_count(client, "agency='NYPD'"), 123456)
                self.assertEqual(client.params["$select"], "count(1)")
                self.assertIn("agency='NYPD'", client.params["$where"])

    def test_rejects_unexpected_payload_shapes(self) -> None:
        bad_payloads = [
            {"count_1": "1"},                      # not a list
            [],                                    # no rows
            [{"count_1": "1"}, {"count_1": "2"}],  # more than one row
            [{"count_1": "1", "x": "2"}],          # unexpected extra column
            [{"count_1": "not-a-number"}],         # unreadable count
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                scope_counts.fetch_count(_FakeClient(payload), "x=1")

    def test_row_count_parses_grouped_rows_only(self) -> None:
        self.assertEqual(
            scope_counts._row_count({"status": "Closed", "count_1": "9"}, "status"), 9
        )
        with self.assertRaises(ValidationError):
            scope_counts._row_count({"count_1": "9"}, "status")  # group field missing
        with self.assertRaises(ValidationError):
            scope_counts._row_count(
                {"status": "Closed", "count_1": "9", "x": "1"}, "status"
            )
        with self.assertRaises(ValidationError):
            scope_counts._row_count({"status": "Closed", "count_1": "?"}, "status")


class ScopeClauseTests(unittest.TestCase):
    def test_window_clause_uses_configured_dates_with_exclusive_end(self) -> None:
        settings = load_config()
        clause = scope_counts.window_where(settings)
        self.assertIn(f">= '{settings.start_date.isoformat()}T00:00:00'", clause)
        end_plus_one = settings.end_date.toordinal() + 1
        from datetime import date

        self.assertIn(f"< '{date.fromordinal(end_plus_one).isoformat()}T00:00:00'", clause)

    def test_candidates_encode_both_agency_scopes(self) -> None:
        settings = load_config()
        pairs = dict(scope_counts.candidates(settings))
        self.assertIn("agency='NYPD'", pairs["NYPD noise"])
        self.assertIn("LIKE 'Noise%'", pairs["NYPD noise"])
        self.assertIn("agency='DSNY'", pairs["DSNY"])
        for clause in pairs.values():
            self.assertIn(str(settings.start_date), clause)


if __name__ == "__main__":
    unittest.main()

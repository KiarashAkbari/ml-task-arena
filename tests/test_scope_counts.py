"""Offline tests for the scope-count evidence script (AGENTS.md section 3)."""

from __future__ import annotations

import unittest

from data_factory.errors import ValidationError
from scripts import scope_counts

URL = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"


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
                self.assertEqual(
                    scope_counts.fetch_count(client, URL, "agency='NYPD'"), 123456
                )
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
                scope_counts.fetch_count(_FakeClient(payload), URL, "x=1")

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


if __name__ == "__main__":
    unittest.main()

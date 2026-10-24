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
    def test_parses_socrata_string_count(self) -> None:
        client = _FakeClient([{"count": "123456"}])
        self.assertEqual(scope_counts.fetch_count(client, "agency='NYPD'"), 123456)
        self.assertEqual(client.params["$select"], "count(1)")
        self.assertIn("agency='NYPD'", client.params["$where"])

    def test_rejects_unexpected_payload_shapes(self) -> None:
        bad_payloads = [
            {"count": "1"},              # not a list
            [],                          # no rows
            [{"count": "1"}, {"count": "2"}],  # more than one row
            [{"nonsense": "1"}],         # missing count key
            [{"count": "not-a-number"}],  # unreadable count
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                scope_counts.fetch_count(_FakeClient(payload), "x=1")


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

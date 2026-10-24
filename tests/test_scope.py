"""Tests for the SoQL $where clause built from the locked scope (section 3)."""

from __future__ import annotations

import dataclasses
import unittest

from data_factory.config import load_config
from data_factory.scope import build_where, window_clause


class BuildWhereTests(unittest.TestCase):
    def test_default_config_builds_locked_scope_clause(self) -> None:
        settings = load_config()
        clause = build_where(settings)
        self.assertIn("agency = 'NYPD'", clause)
        self.assertIn("complaint_type IN (", clause)
        for name in settings.scope_complaint_types:
            self.assertIn(f"'{name}'", clause)
        # Window: configured end date is inclusive -> exclusive bound next day.
        self.assertIn("created_date >= '2023-01-01T00:00:00'", clause)
        self.assertIn("created_date < '2024-07-01T00:00:00'", clause)

    def test_complaint_types_come_from_config_not_hardcoded(self) -> None:
        settings = dataclasses.replace(
            load_config(),
            scope_agency="DSNY",
            scope_complaint_types=("Dirty Condition",),
        )
        clause = build_where(settings)
        self.assertIn("agency = 'DSNY'", clause)
        self.assertIn("'Dirty Condition'", clause)
        self.assertNotIn("NYPD", clause)
        self.assertNotIn("Noise - Residential", clause)

    def test_single_quotes_in_scope_values_are_escaped(self) -> None:
        settings = dataclasses.replace(
            load_config(),
            scope_complaint_types=("O'Brien's Noise",),
        )
        clause = build_where(settings)
        self.assertIn("'O''Brien''s Noise'", clause)

    def test_window_clause_isolated(self) -> None:
        settings = load_config()
        clause = window_clause(settings)
        self.assertNotIn("agency", clause)
        self.assertLess(settings.start_date, settings.end_date)


if __name__ == "__main__":
    unittest.main()

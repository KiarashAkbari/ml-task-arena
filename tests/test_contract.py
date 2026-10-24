"""Tests for the explicit schema contract (AGENTS.md section 4.3)."""

from __future__ import annotations

import unittest

from data_factory import contract
from data_factory.validation import REASONS


class ContractTests(unittest.TestCase):
    def test_every_column_declares_the_four_required_aspects(self) -> None:
        """4.3: type, nullability, domain, uniqueness — all written down."""
        names = [column.name for column in contract.COLUMNS]
        self.assertEqual(len(names), len(set(names)), "duplicate column in contract")
        for column in contract.COLUMNS:
            with self.subTest(column=column.name):
                self.assertTrue(column.name)
                self.assertTrue(column.type)
                self.assertIsInstance(column.nullable, bool)
                self.assertTrue(column.domain)

    def test_uniqueness_key_is_declared_and_in_the_contract(self) -> None:
        self.assertEqual(contract.UNIQUENESS, ("unique_key",))
        unique_spec = contract.spec("unique_key")
        self.assertFalse(unique_spec.nullable)

    def test_every_reason_code_is_covered_by_the_contract(self) -> None:
        """Registry <-> contract consistency: no orphan reason codes."""
        gates = ", ".join(column.raw_gate for column in contract.COLUMNS)
        for code in REASONS:
            with self.subTest(code=code):
                self.assertIn(code, gates)

    def test_spec_lookup_and_unknown_name(self) -> None:
        self.assertEqual(contract.spec("borough").nullable, True)
        with self.assertRaises(KeyError):
            contract.spec("no_such_column")


if __name__ == "__main__":
    unittest.main()

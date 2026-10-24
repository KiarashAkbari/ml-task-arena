"""Tests for the exception hierarchy and its exit-code contract."""

from __future__ import annotations

import unittest

from data_factory.errors import (
    EXIT_UNEXPECTED,
    ConfigError,
    DataFactoryError,
    NetworkError,
    ValidationError,
)


class ErrorHierarchyTests(unittest.TestCase):
    def test_all_errors_share_the_base_class(self) -> None:
        for exc_class in (ConfigError, NetworkError, ValidationError):
            with self.subTest(exc_class=exc_class.__name__):
                self.assertTrue(issubclass(exc_class, DataFactoryError))
                self.assertTrue(issubclass(exc_class, Exception))

    def test_exit_codes_are_distinct_per_error_kind(self) -> None:
        """AGENTS.md 4.1: network, data-validation, and config failures
        must be distinguishable via exit codes."""
        codes = [ConfigError.exit_code, NetworkError.exit_code, ValidationError.exit_code]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(ConfigError.exit_code, 3)
        self.assertEqual(NetworkError.exit_code, 4)
        self.assertEqual(ValidationError.exit_code, 5)

    def test_base_and_unexpected_share_code_one(self) -> None:
        self.assertEqual(DataFactoryError.exit_code, EXIT_UNEXPECTED)


if __name__ == "__main__":
    unittest.main()

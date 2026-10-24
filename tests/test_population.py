"""Tests for the population reference loader (AGENTS.md section 3/4.4)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from data_factory.errors import ValidationError
from data_factory.population import load_population, normalize_borough

VALID_CSV = (
    "zip,population,borough\n"
    "11101,30000,Queens\n"
    "10001,25000,New York\n"      # county name -> Manhattan
    "11201,20000,Kings\n"         # county name -> Brooklyn
    "10002,18000,Bronx\n"
)


class LoadPopulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def _write(self, content: str, name: str = "pop.csv") -> Path:
        path = self.tmp_path / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_valid_reference_loads_with_normalized_boroughs(self) -> None:
        rows = load_population(self._write(VALID_CSV))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows["11101"].population, 30000)
        self.assertEqual(rows["11101"].borough, "Queens")
        self.assertEqual(rows["10001"].borough, "Manhattan")  # New York county
        self.assertEqual(rows["11201"].borough, "Brooklyn")   # Kings county
        self.assertEqual(rows["10002"].borough, "Bronx")

    def test_missing_file_points_to_readme(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            load_population(self.tmp_path / "nope.csv")
        self.assertIn("not found", str(ctx.exception))
        self.assertIn("README", str(ctx.exception))

    def test_wrong_headers_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            load_population(self._write("zipcode,pop\n10001,100\n"))
        with self.assertRaises(ValidationError):
            load_population(self._write("zip,population,extra\n10001,100,x\n"))

    def test_bad_zip_values_rejected(self) -> None:
        for bad_zip in ("1001", "abcde", "10001.0", "  "):
            content = f"zip,population,borough\n{bad_zip},100,Queens\n"
            with self.subTest(zip=bad_zip), self.assertRaises(ValidationError):
                load_population(self._write(content))

    def test_bad_population_values_rejected(self) -> None:
        for bad_pop in ("0", "-5", "25,000", "many"):
            # Quoted so the CSV parser keeps the comma as part of the value.
            content = f'zip,population,borough\n10001,"{bad_pop}",Queens\n'
            with self.subTest(population=bad_pop), self.assertRaises(ValidationError):
                load_population(self._write(content))

    def test_duplicate_zip_rejected_to_protect_join_cardinality(self) -> None:
        content = "zip,population,borough\n10001,100,Queens\n10001,200,Queens\n"
        with self.assertRaises(ValidationError) as ctx:
            load_population(self._write(content))
        self.assertIn("duplicate zip", str(ctx.exception))

    def test_header_only_file_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            load_population(self._write("zip,population,borough\n"))

    def test_unclassified_zip_rejected(self) -> None:
        """Every ZIP must map to a district or it vanishes from rates silently."""
        content = "zip,population,borough\n10001,100,\n"
        with self.assertRaises(ValidationError) as ctx:
            load_population(self._write(content))
        self.assertIn("borough", str(ctx.exception))

    def test_utf8_bom_header_accepted(self) -> None:
        path = self._write("\ufeffzip,population,borough\n10001,100,Queens\n")
        rows = load_population(path)
        self.assertEqual(rows["10001"].population, 100)


class NormalizeBoroughTests(unittest.TestCase):
    def test_aliases_and_passthrough(self) -> None:
        self.assertEqual(normalize_borough("kings"), "Brooklyn")
        self.assertEqual(normalize_borough(" New York "), "Manhattan")
        self.assertEqual(normalize_borough("Brooklyn"), "Brooklyn")
        self.assertEqual(normalize_borough("Staten Island"), "Staten Island")

    def test_empty_stays_none(self) -> None:
        self.assertIsNone(normalize_borough(None))
        self.assertIsNone(normalize_borough(""))
        self.assertIsNone(normalize_borough("   "))


if __name__ == "__main__":
    unittest.main()

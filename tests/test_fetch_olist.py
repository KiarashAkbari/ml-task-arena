"""Tests for the Olist bootstrap fetcher (offline).

The mirror used to be read with ``member.startswith("dataset/")``, but the
archive wraps everything in ``<repo>-main/dataset/``, so a real download
extracted zero of nine files and the failure only surfaced on the network.
These tests pin the rule: the directory may sit at any depth, the file name is
still checked against the nine expected names, and a file outside the dataset
directory is ignored.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import fetch_olist

ORDERS = "olist_orders_dataset.csv"


def build_zip(path: Path, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as bundle:
        for name, body in members.items():
            bundle.writestr(name, body)


class ExtractMembersTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.dest = self.tmp / "raw"

    def test_mirror_layout_is_extracted_from_nested_dataset_dir(self) -> None:
        archive = self.tmp / "mirror.zip"
        build_zip(archive, {
            f"ecommerce-data-modeling-main/{fetch_olist.MIRROR_DIR}{ORDERS}": "a\n1\n",
        })
        with zipfile.ZipFile(archive) as bundle:
            written = fetch_olist.extract_members(
                bundle, self.dest, require_dir=fetch_olist.MIRROR_DIR.rstrip("/"))
        self.assertEqual(written, 1)
        self.assertEqual((self.dest / ORDERS).read_text(encoding="utf-8"), "a\n1\n")

    def test_dataset_dir_is_found_at_any_depth(self) -> None:
        archive = self.tmp / "deep.zip"
        build_zip(archive, {"a/b/dataset/" + ORDERS: "a\n1\n"})
        with zipfile.ZipFile(archive) as bundle:
            written = fetch_olist.extract_members(bundle, self.dest, require_dir="dataset")
        self.assertEqual(written, 1)

    def test_files_outside_the_dataset_dir_are_ignored(self) -> None:
        archive = self.tmp / "stray.zip"
        build_zip(archive, {"backup/" + ORDERS: "a\n1\n"})
        with zipfile.ZipFile(archive) as bundle:
            written = fetch_olist.extract_members(bundle, self.dest, require_dir="dataset")
        self.assertEqual(written, 0)
        self.assertFalse((self.dest / ORDERS).exists())

    def test_kaggle_zip_needs_no_dataset_dir(self) -> None:
        archive = self.tmp / "kaggle.zip"
        build_zip(archive, {ORDERS: "a\n1\n", "notes.txt": "ignore me"})
        with zipfile.ZipFile(archive) as bundle:
            written = fetch_olist.extract_members(bundle, self.dest)
        self.assertEqual(written, 1)
        self.assertFalse((self.dest / "notes.txt").exists())

    def test_unknown_csv_names_are_never_written(self) -> None:
        archive = self.tmp / "unknown.zip"
        build_zip(archive, {"dataset/other_dataset.csv": "a\n1\n"})
        with zipfile.ZipFile(archive) as bundle:
            written = fetch_olist.extract_members(bundle, self.dest, require_dir="dataset")
        self.assertEqual(written, 0)


if __name__ == "__main__":
    unittest.main()

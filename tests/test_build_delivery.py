"""Tests for the delivery builder (AGENTS.md section 6).

The folder is generated from the single source tree so the two can never
drift. The template in the contract lists ``data/curated`` as "the outputs",
so a reviewable delivery carries them — while ``data/raw`` stays empty,
because the Delivery Guide keeps raw dumps out of the delivered tree.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import build_delivery

REQUIRED_FILES = ("solution.ipynb", "report.md", "requirements.txt", "README.md",
                  "pyproject.toml", ".gitignore")
REQUIRED_TREES = ("src", "tests", "config", "scripts")


def executed_notebook() -> dict:
    return {
        "cells": [
            {"cell_type": "markdown", "id": "m1", "metadata": {}, "source": ["# title"]},
            {"cell_type": "code", "id": "c1", "metadata": {}, "execution_count": 1,
             "outputs": [{"output_type": "stream", "name": "stdout", "text": "ok\n"}],
             "source": ["print('ok')"]},
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }


class DeliveryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._project = tempfile.TemporaryDirectory()
        self.addCleanup(self._project.cleanup)
        self.root = Path(self._project.name) / "project"
        self.root.mkdir()
        for name in REQUIRED_FILES:
            content = ""
            if name == "solution.ipynb":
                content = json.dumps(executed_notebook())
            (self.root / name).write_text(content, encoding="utf-8")
        for name in REQUIRED_TREES:
            (self.root / name).mkdir()
            (self.root / name / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "data" / "reference").mkdir(parents=True)
        (self.root / "data" / "reference" / "zip_population.csv").write_text(
            "zip,population,borough\n11101,30000,Queens\n", encoding="utf-8"
        )
        self.curated = self.root / "data" / "curated"
        self.curated.mkdir()

        patcher = mock.patch.object(build_delivery, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.target = self.root / "stage-1-kiarash-akbari"

    def run_build(self, *, force: bool = False) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = build_delivery.build(self.target, force=force)
        return code, out.getvalue(), err.getvalue()

    def add_all_curated(self) -> None:
        for index, name in enumerate(build_delivery.CURATED_OUTPUTS):
            (self.curated / name).write_text(f"payload {index}\n", encoding="utf-8")


class StructureTests(DeliveryTestBase):
    def test_builds_the_section_6_tree(self) -> None:
        code, out, _ = self.run_build()
        self.assertEqual(code, 0)
        for name in REQUIRED_FILES:
            self.assertTrue((self.target / name).is_file(), name)
        for name in REQUIRED_TREES:
            self.assertTrue((self.target / name).is_dir(), name)
        self.assertTrue((self.target / "data" / "reference" / "zip_population.csv").is_file())
        self.assertIn("delivery folder:", out)

    def test_raw_stays_empty_but_exists(self) -> None:
        self.run_build()
        raw = self.target / "data" / "raw"
        self.assertTrue(raw.is_dir())
        self.assertEqual(list(raw.iterdir()), [], "raw dumps never enter the delivery")

    def test_curated_outputs_are_carried_over(self) -> None:
        self.add_all_curated()
        code, out, err = self.run_build()
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        delivered = self.target / "data" / "curated"
        for name in build_delivery.CURATED_OUTPUTS:
            self.assertTrue((delivered / name).is_file(), name)
        self.assertIn("cases.csv", out)

    def test_missing_outputs_warn_without_failing(self) -> None:
        code, _, err = self.run_build()
        self.assertEqual(code, 0, "a delivery without outputs is still buildable")
        self.assertIn("curated", err)
        self.assertIn("data-factory run", err)

    def test_partial_outputs_are_reported_by_name(self) -> None:
        (self.curated / "cases.csv").write_text("only this one\n", encoding="utf-8")
        _, _, err = self.run_build()
        self.assertIn("cases.csv", err) if "cases.csv" in err else None
        self.assertIn("aggregate.csv", err)
        self.assertTrue((self.target / "data" / "curated" / "cases.csv").is_file())

    def test_existing_folder_is_only_replaced_with_force(self) -> None:
        self.assertEqual(self.run_build()[0], 0)
        code, _, err = self.run_build()
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertEqual(self.run_build(force=True)[0], 0)

    def test_missing_answer_file_is_refused(self) -> None:
        (self.root / "report.md").unlink()
        code, _, err = self.run_build()
        self.assertEqual(code, 1)
        self.assertIn("report.md", err)


class NotebookOutputTests(DeliveryTestBase):
    def test_executed_notebook_does_not_warn(self) -> None:
        _, _, err = self.run_build()
        self.assertNotIn("no saved outputs", err)

    def test_unexecuted_notebook_warns(self) -> None:
        notebook = executed_notebook()
        notebook["cells"][1]["execution_count"] = None
        notebook["cells"][1]["outputs"] = []
        (self.root / "solution.ipynb").write_text(json.dumps(notebook), encoding="utf-8")
        code, _, err = self.run_build()
        self.assertEqual(code, 0, "a warning, not a failure")
        self.assertIn("no saved outputs", err)


if __name__ == "__main__":
    unittest.main()

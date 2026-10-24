"""Tests for the Jupyter-free notebook executor (AGENTS.md section 5.2/5.3).

The executor exists so a blocked PyPI cannot stop the delivery, but it must
produce the same shape of outputs Jupyter would: sequential execution counts,
stream output, figures as display data, and the last-expression repr.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.execute_notebook import execute_cell, execute_notebook


def notebook(*cells: tuple[str, str]) -> dict:
    return {
        "cells": [
            {
                "cell_type": kind,
                "id": f"cell-{index:02d}",
                "metadata": {},
                "source": text.splitlines(keepends=True),
                **({"execution_count": None, "outputs": []} if kind == "code" else {}),
            }
            for index, (kind, text) in enumerate(cells, start=1)
        ],
        "metadata": {"kernelspec": {"name": "python3", "language": "python",
                                    "display_name": "Python 3"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }


class CellExecutionTests(unittest.TestCase):
    def test_print_becomes_a_stream_output_in_order(self) -> None:
        outputs = execute_cell("print('hello')\nprint('world')", {})
        self.assertEqual(len(outputs), 1, "consecutive prints merge into one stream")
        self.assertEqual(outputs[0]["output_type"], "stream")
        self.assertEqual(outputs[0]["name"], "stdout")
        self.assertEqual(outputs[0]["text"], "hello\nworld\n")

    def test_last_expression_becomes_execute_result(self) -> None:
        namespace: dict = {}
        execute_cell("x = 21 * 2", namespace)
        outputs = execute_cell("x", namespace)
        kinds = [output["output_type"] for output in outputs]
        self.assertEqual(kinds, ["execute_result"])
        self.assertEqual(outputs[0]["data"]["text/plain"], "42")

    def test_trailing_semicolon_style_assignment_has_no_result(self) -> None:
        outputs = execute_cell("value = 5", {})
        self.assertEqual(outputs, [])

    def test_state_persists_between_cells_like_a_kernel(self) -> None:
        namespace: dict = {}
        execute_cell("counter = 1", namespace)
        execute_cell("counter += 1", namespace)
        self.assertEqual(namespace["counter"], 2)

    def test_figure_becomes_display_data_with_png(self) -> None:
        source = (
            "import matplotlib.pyplot as plt\n"
            "fig, ax = plt.subplots()\n"
            "ax.plot([1, 2, 3], [1, 4, 9])\n"
            "plt.show()"
        )
        outputs = execute_cell(source, {})
        images = [
            output for output in outputs
            if output["output_type"] == "display_data" and "image/png" in output["data"]
        ]
        self.assertEqual(len(images), 1)
        self.assertGreater(len(images[0]["data"]["image/png"]), 100)

    def test_stdout_and_figure_keep_their_order(self) -> None:
        source = (
            "import matplotlib.pyplot as plt\n"
            "print('before')\n"
            "plt.plot([1, 2])\n"
            "plt.show()\n"
            "print('after')"
        )
        kinds = [output["output_type"] for output in execute_cell(source, {})]
        self.assertEqual(kinds, ["stream", "display_data", "stream"])

    def test_an_exception_becomes_an_error_output(self) -> None:
        outputs = execute_cell("raise ValueError('boom')", {})
        self.assertEqual(outputs[-1]["output_type"], "error")
        self.assertEqual(outputs[-1]["ename"], "ValueError")
        self.assertEqual(outputs[-1]["evalue"], "boom")

    def test_figures_do_not_leak_into_the_next_cell(self) -> None:
        import matplotlib.pyplot as plt

        execute_cell("import matplotlib.pyplot as plt\nplt.plot([1, 2])\nplt.show()", {})
        self.assertEqual(plt.get_fignums(), [])


class NotebookExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.notebook_path = self.tmp_path / "solution.ipynb"

    def write(self, document: dict) -> None:
        self.notebook_path.write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )

    def test_execution_counts_are_sequential_across_code_cells(self) -> None:
        self.write(notebook(
            ("markdown", "# title"),
            ("code", "a = 1"),
            ("markdown", "## section"),
            ("code", "a + 1"),
            ("code", "print(a)"),
        ))
        summary = execute_notebook(self.notebook_path, working_directory=self.tmp_path)
        executed = json.loads(self.notebook_path.read_text(encoding="utf-8"))
        counts = [
            cell["execution_count"]
            for cell in executed["cells"] if cell["cell_type"] == "code"
        ]
        self.assertEqual(counts, [1, 2, 3])
        self.assertEqual(summary["code_cells"], 3)
        self.assertEqual(summary["executed"], 3)
        self.assertIsNone(summary["error_cell"])
        # the second code cell's result carries its own count
        second = [c for c in executed["cells"] if c["cell_type"] == "code"][1]
        self.assertEqual(second["outputs"][0]["execution_count"], 2)

    def test_markdown_cells_are_untouched(self) -> None:
        self.write(notebook(("markdown", "# keep me"), ("code", "1 + 1")))
        execute_notebook(self.notebook_path, working_directory=self.tmp_path)
        executed = json.loads(self.notebook_path.read_text(encoding="utf-8"))
        self.assertEqual(executed["cells"][0]["source"], ["# keep me"])
        self.assertNotIn("outputs", executed["cells"][0])

    def test_relative_paths_resolve_against_the_working_directory(self) -> None:
        (self.tmp_path / "data").mkdir()
        (self.tmp_path / "data" / "value.txt").write_text("42\n", encoding="utf-8")
        self.write(notebook(("code", "from pathlib import Path\n"
                                     "Path('data/value.txt').read_text().strip()")))
        execute_notebook(self.notebook_path, working_directory=self.tmp_path)
        executed = json.loads(self.notebook_path.read_text(encoding="utf-8"))
        self.assertEqual(
            executed["cells"][0]["outputs"][0]["data"]["text/plain"], "'42'"
        )

    def test_a_failing_cell_stops_execution_and_is_reported(self) -> None:
        self.write(notebook(("code", "x = 1"), ("code", "raise RuntimeError('stop')"),
                            ("code", "x = 999")))
        with self.assertRaises(RuntimeError) as ctx:
            execute_notebook(self.notebook_path, working_directory=self.tmp_path)
        self.assertIn("cell 2 raised", str(ctx.exception))
        executed = json.loads(self.notebook_path.read_text(encoding="utf-8"))
        counts = [c["execution_count"] for c in executed["cells"]]
        self.assertEqual(counts, [1, 2, None], "the third cell never ran")
        self.assertEqual(executed["cells"][1]["outputs"][-1]["ename"], "RuntimeError")

    def test_a_leaked_handle_does_not_pollute_the_next_cell(self) -> None:
        """ResourceWarning must surface in the cell that caused it (gc timing)."""
        self.write(notebook(
            ("code", "open('data/value.txt').read()"),   # deliberately unclosed
            ("code", "len('abc')"),
        ))
        (self.tmp_path / "data").mkdir()
        (self.tmp_path / "data" / "value.txt").write_text("42\n", encoding="utf-8")
        execute_notebook(self.notebook_path, working_directory=self.tmp_path)
        executed = json.loads(self.notebook_path.read_text(encoding="utf-8"))
        second = executed["cells"][1]["outputs"]
        self.assertEqual(
            [output["output_type"] for output in second], ["execute_result"],
            f"the next cell's output was polluted: {second}",
        )

    def test_two_runs_capture_identical_text_outputs(self) -> None:
        self.write(notebook(("code", "import sys\nprint(sys.version_info.major)"),
                            ("code", "sorted({'b', 'a'})")))
        execute_notebook(self.notebook_path, working_directory=self.tmp_path)
        first = json.loads(self.notebook_path.read_text(encoding="utf-8"))
        execute_notebook(self.notebook_path, working_directory=self.tmp_path)
        second = json.loads(self.notebook_path.read_text(encoding="utf-8"))

        def text_outputs(document: dict) -> list[str]:
            return [
                json.dumps(cell["outputs"], sort_keys=True)
                for cell in document["cells"] if cell["cell_type"] == "code"
            ]

        self.assertEqual(text_outputs(first), text_outputs(second))


if __name__ == "__main__":
    unittest.main()

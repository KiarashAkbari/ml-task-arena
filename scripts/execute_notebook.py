"""Execute ``solution.ipynb`` without Jupyter, saving real cell outputs.

AGENTS.md section 5 requires the notebook to be runnable top-to-bottom with
"Restart Kernel & Run All" **and** delivered with its outputs saved (5.3). The
normal way to do that is Jupyter::

    jupyter nbconvert --to notebook --execute --inplace solution.ipynb

That needs Jupyter installed, which needs PyPI. This script exists for
environments where PyPI is unreachable: it executes the notebook's code cells
in order in a *fresh* interpreter — the same semantics as "Restart Kernel & Run
All" — and writes genuine outputs back into the file:

* ``execution_count`` numbered ``1..N`` across code cells (section 5.2),
* ``stream`` outputs for everything printed, in the order it was printed,
* ``display_data`` with base64 ``image/png`` for every figure a cell shows,
* ``execute_result`` with the ``text/plain`` repr of a cell's last expression,
* ``error`` outputs (and a non-zero exit) if a cell raises.

Nothing is fabricated: the values are whatever the cells actually print and
plot against the dataset on disk. The preferred path remains
``jupyter nbconvert``; this is the fallback, and the notebook it produces opens
and re-runs identically in Jupyter.

Usage::

    python scripts/execute_notebook.py                # solution.ipynb, in place
    python scripts/execute_notebook.py --output /tmp/out.ipynb
    python scripts/execute_notebook.py --cwd /path/to/project
"""

from __future__ import annotations

import argparse
import ast
import base64
import builtins
import contextlib
import gc
import io
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NOTEBOOK = PROJECT_ROOT / "solution.ipynb"
PNG_DPI = 110  # matches the notebook's rcParams so figures look the same as in Jupyter


class _Collector(io.TextIOBase):
    """File-like object that turns writes into ordered notebook stream outputs."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def write(self, text: str) -> int:  # noqa: D102 - file protocol
        if not text:
            return 0
        if self.events and self.events[-1]["output_type"] == "stream":
            self.events[-1]["text"] += text
        else:
            self.events.append(
                {"output_type": "stream", "name": "stdout", "text": text}
            )
        return len(text)

    def flush(self) -> None:  # noqa: D102 - file protocol
        return None

    def isatty(self) -> bool:  # noqa: D102 - file protocol
        return False


def _split_last_expression(source: str) -> tuple[ast.Module, ast.Expression | None]:
    """Split a cell into statements plus its trailing expression (IPython semantics).

    Raises:
        SyntaxError: The cell does not parse.
    """
    tree = ast.parse(source)
    last: ast.Expression | None = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        node = tree.body.pop()
        assert isinstance(node, ast.Expr)
        last = ast.Expression(node.value)
    return tree, last


def _capture_figures(module: Any, collector: _Collector, figures: list[Any]) -> Any:
    """Replace ``matplotlib.pyplot.show`` so figures become display outputs."""

    original_show = module.show

    def show(*args: Any, **kwargs: Any) -> None:
        for number in module.get_fignums():
            figure = module.figure(number)
            buffer = io.BytesIO()
            figure.savefig(
                buffer, format="png", bbox_inches="tight", dpi=PNG_DPI
            )
            collector.events.append(
                {
                    "output_type": "display_data",
                    "data": {"image/png": base64.b64encode(buffer.getvalue()).decode("ascii")},
                    "metadata": {},
                }
            )
            module.close(figure)
        figures.append(True)  # marker: at least one figure was produced

    module.show = show
    return original_show


def execute_cell(source: str, namespace: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one code cell and return its notebook ``outputs`` list.

    Raises:
        SyntaxError: The cell does not parse (reported, not swallowed).
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    collector = _Collector()
    original_show = _capture_figures(plt, collector, [])
    result_value: Any = None
    have_result = False
    error: dict[str, Any] | None = None

    tree, last_expression = _split_last_expression(source)
    try:
        with contextlib.redirect_stdout(collector), contextlib.redirect_stderr(collector):
            exec(compile(tree, "<cell>", "exec"), namespace)  # noqa: S102 - executing the user's own cells
            if last_expression is not None:
                result_value = eval(  # noqa: S307 - notebook cell, by design
                    compile(last_expression, "<cell>", "eval"), namespace
                )
                have_result = True
            # Collect while this cell still owns the output stream: otherwise a
            # ResourceWarning for a file handle this cell forgot to close is
            # emitted (and attributed) during the NEXT cell, which silently
            # corrupts that cell's saved output.
            gc.collect()
    except BaseException as exc:  # noqa: BLE001 - reported as a notebook error output
        error = {
            "output_type": "error",
            "ename": type(exc).__name__,
            "evalue": str(exc),
            "traceback": traceback.format_exc().splitlines(),
        }
    finally:
        plt.show = original_show
        # Close anything the cell left open so figures never leak between cells.
        for number in list(plt.get_fignums()):
            plt.close(plt.figure(number))

    if error is not None:
        collector.events.append(error)
        return collector.events

    if have_result and result_value is not None:
        text = repr(result_value)
        if text and text != "None":
            collector.events.append(
                {
                    "output_type": "execute_result",
                    "execution_count": 0,  # filled in by the caller
                    "data": {"text/plain": text},
                    "metadata": {},
                }
            )
    return collector.events


def execute_notebook(
    notebook_path: Path,
    *,
    working_directory: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Execute every code cell in order and write the notebook with its outputs.

    Returns:
        A small summary: ``{"code_cells", "executed", "figures", "error_cell"}``.

    Raises:
        RuntimeError: A cell raised; the notebook is written up to that point so
            the failure can be inspected in the file itself.
    """
    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
    namespace: dict[str, Any] = {"__name__": "__main__", "__builtins__": builtins}
    count = 0
    figures = 0
    error_cell: int | None = None
    summary: dict[str, Any] = {"code_cells": 0, "executed": 0, "figures": 0}

    previous_cwd = Path.cwd()
    os.chdir(working_directory)
    try:
        for index, cell in enumerate(notebook["cells"], start=1):
            if cell.get("cell_type") != "code":
                continue
            summary["code_cells"] += 1
            count += 1
            source = "".join(cell.get("source", []))
            outputs = execute_cell(source, namespace)
            for output in outputs:
                if output["output_type"] == "execute_result":
                    output["execution_count"] = count
                if output["output_type"] == "display_data" and "image/png" in output.get("data", {}):
                    figures += 1
            cell["execution_count"] = count
            cell["outputs"] = outputs
            summary["executed"] += 1
            if outputs and outputs[-1]["output_type"] == "error":
                error_cell = index
                break
    finally:
        os.chdir(previous_cwd)
        target = output_path or notebook_path
        target.write_text(json.dumps(notebook, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    summary["figures"] = figures
    summary["error_cell"] = error_cell
    if error_cell is not None:
        raise RuntimeError(
            f"cell {error_cell} raised; outputs up to it were written to "
            f"{output_path or notebook_path}"
        )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("notebook", nargs="?", type=Path, default=DEFAULT_NOTEBOOK,
                        help=f"notebook to execute (default: {DEFAULT_NOTEBOOK.name})")
    parser.add_argument("--output", type=Path, default=None,
                        help="write the executed notebook here (default: in place)")
    parser.add_argument("--cwd", type=Path, default=None,
                        help="working directory for the cells (default: the project root)")
    args = parser.parse_args(None if argv is None else argv)

    working_directory = (args.cwd or args.notebook.resolve().parent).resolve()
    if not (working_directory / "data" / "curated" / "cases.csv").is_file():
        print(
            f"error: no curated dataset under '{working_directory}' — run "
            "`data-factory run` first",
            file=sys.stderr,
        )
        return 1

    try:
        summary = execute_notebook(
            args.notebook, working_directory=working_directory, output_path=args.output
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"executed {summary['executed']}/{summary['code_cells']} code cell(s), "
        f"{summary['figures']} figure(s) captured"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

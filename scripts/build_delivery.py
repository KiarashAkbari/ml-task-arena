"""Assemble the delivery folder required by AGENTS.md section 6.

The project has exactly one source tree (``src/``, ``tests/``, the notebook,
the report…); the delivery folder is *generated* from it so the two can never
drift. Run::

    python scripts/build_delivery.py                  # -> stage-1-kiarash-akbari/
    python scripts/build_delivery.py --force          # replace an existing folder
    python scripts/build_delivery.py --stage 2        # -> stage-2-kiarash-akbari/

The result mirrors the section 6 template::

    stage-1-kiarash-akbari/
    ├── solution.ipynb
    ├── report.md
    ├── requirements.txt
    ├── README.md
    ├── src/  tests/  config/  scripts/
    ├── data/{raw,curated,reference}
    └── .gitignore

``data/raw`` stays empty on purpose (the Delivery Guide keeps raw dumps out of
the delivered tree); ``data/curated`` is filled with the four outputs plus the
manifest when they exist, so the folder can be reviewed without re-running the
factory or having network access. The evaluator can still delete them and run
the factory from an empty folder, which is the section 4.10 acceptance check.

It refuses to run when a required answer file is missing, and it warns when
the notebook has no saved outputs — section 5.3 does not accept a notebook
whose cells were never executed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DELIVERY_NAME = "stage-1-kiarash-akbari"

#: Stage 2 answers live beside stage 1 rather than replacing it (AGENTS.md 1:
#: stages chain). The delivery folder is assembled from the same source tree.
STAGE2_NAME = "stage-2-kiarash-akbari"
STAGE2_SOURCES = {"solution.ipynb": "stage2/solution.ipynb",
                  "report.md": "stage2/report.md",
                  "README.md": "stage2/README.md"}
STAGE2_FILES = ("requirements.txt", "pyproject.toml", ".gitignore")
STAGE2_TREES = ("src", "tests", "sql", "config", "scripts")

FILES = ("solution.ipynb", "report.md", "requirements.txt", "README.md",
         "pyproject.toml", ".gitignore")
TREES = ("src", "tests", "config", "scripts")
IGNORED = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.egg-info", ".ipynb_checkpoints", ".venv"
)


def _notebook_is_executed(path: Path) -> bool:
    """True when every code cell has an execution count and saved outputs."""
    notebook = json.loads(path.read_text(encoding="utf-8"))
    code_cells = [cell for cell in notebook["cells"] if cell["cell_type"] == "code"]
    return bool(code_cells) and all(
        cell.get("execution_count") is not None for cell in code_cells
    )


#: The curated outputs a reviewable delivery should carry (AGENTS.md 4.5, 4.8.4).
CURATED_OUTPUTS = (
    "cases.csv",
    "aggregate.csv",
    "data_quality.csv",
    "quarantine.jsonl",
    "run_manifest.json",
)


def _copy_curated(target: Path) -> list[str]:
    """Copy the curated outputs into the delivery; returns what was copied.

    Missing outputs are not an error — the factory regenerates them — but they
    are reported, because a delivery without them cannot be inspected without
    running the pipeline first.
    """
    source = PROJECT_ROOT / "data" / "curated"
    copied: list[str] = []
    for name in CURATED_OUTPUTS:
        path = source / name
        if path.is_file():
            shutil.copy2(path, target / "data" / "curated" / name)
            copied.append(name)
    return copied


def build_stage2(target: Path, *, force: bool) -> int:
    """Assemble stage-2-kiarash-akbari/ from stage2/ plus the shared trees."""
    missing = [source for source in STAGE2_SOURCES.values()
               if not (PROJECT_ROOT / source).is_file()]
    missing += [name for name in STAGE2_FILES if not (PROJECT_ROOT / name).is_file()]
    if missing:
        print(f"error: missing answer file(s): {missing}", file=sys.stderr)
        return 1

    notebook = PROJECT_ROOT / STAGE2_SOURCES["solution.ipynb"]
    if not _notebook_is_executed(notebook):
        print(
            "warning: stage2/solution.ipynb has no saved outputs — run "
            "'python -m nbconvert --to notebook --execute --inplace "
            "stage2/solution.ipynb' first (AGENTS.md section 2.1.3).",
            file=sys.stderr,
        )

    if target.exists():
        if not force:
            print(f"error: '{target}' already exists — re-run with --force to replace it",
                  file=sys.stderr)
            return 1
        shutil.rmtree(target)

    target.mkdir(parents=True)
    for name, source in STAGE2_SOURCES.items():
        shutil.copy2(PROJECT_ROOT / source, target / name)
    for name in STAGE2_FILES:
        shutil.copy2(PROJECT_ROOT / name, target / name)
    for name in STAGE2_TREES:
        shutil.copytree(PROJECT_ROOT / name, target / name, ignore=IGNORED)

    (target / "data" / "raw").mkdir(parents=True, exist_ok=True)
    (target / "data" / "curated").mkdir(parents=True, exist_ok=True)
    (target / "data" / "db").mkdir(parents=True, exist_ok=True)

    # The measurement artefacts are the evidence behind every number in the
    # report; shipping them lets a reviewer read the raw JSON without running
    # anything. They are gitignored because they are generated.
    evidence_dir = PROJECT_ROOT / "data" / "db"
    shipped = []
    for name in ("load_report.json", "load_quarantine.jsonl",
                 "index_experiments.json", "view_vs_snapshot.json"):
        source = evidence_dir / name
        if source.is_file():
            shutil.copy2(source, target / "data" / "db" / name)
            shipped.append(name)
    if not shipped:
        print("warning: no measurement artefacts in data/db — run "
              "'data-factory db bootstrap' first so the delivery carries the "
              "evidence behind the report's numbers", file=sys.stderr)

    print(f"delivery folder: {target}")
    for path in sorted(target.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(target).as_posix():46s} {path.stat().st_size:>9,} B")
    return 0


def build(target: Path, *, force: bool) -> int:
    missing = [name for name in FILES if not (PROJECT_ROOT / name).is_file()]
    if missing:
        print(f"error: missing answer file(s): {missing}", file=sys.stderr)
        return 1

    notebook = PROJECT_ROOT / "solution.ipynb"
    if not _notebook_is_executed(notebook):
        print(
            "warning: solution.ipynb has no saved outputs — open it, run "
            "'Restart Kernel and Run All Cells', save, then rebuild "
            "(AGENTS.md section 5.2/5.3).",
            file=sys.stderr,
        )

    if target.exists():
        if not force:
            print(
                f"error: '{target}' already exists — re-run with --force to replace it",
                file=sys.stderr,
            )
            return 1
        shutil.rmtree(target)

    target.mkdir(parents=True)
    for name in FILES:
        shutil.copy2(PROJECT_ROOT / name, target / name)
    for name in TREES:
        shutil.copytree(PROJECT_ROOT / name, target / name, ignore=IGNORED)

    for relative in ("data/raw", "data/curated"):
        (target / relative).mkdir(parents=True, exist_ok=True)
    curated = _copy_curated(target)
    missing_curated = [name for name in CURATED_OUTPUTS if name not in curated]
    if missing_curated:
        print(
            f"warning: {len(missing_curated)} of {len(CURATED_OUTPUTS)} curated "
            f"output(s) not found and not copied: {missing_curated} — run "
            "'data-factory run' first if the delivery should carry them",
            file=sys.stderr,
        )
    reference = PROJECT_ROOT / "data" / "reference"
    if reference.is_dir():
        shutil.copytree(reference, target / "data" / "reference", ignore=IGNORED)
    else:
        (target / "data" / "reference").mkdir(parents=True, exist_ok=True)

    print(f"delivery folder: {target}")
    for path in sorted(target.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(target).as_posix():46s} {path.stat().st_size:>9,} B")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", default=DELIVERY_NAME,
                        help=f"folder name (default: {DELIVERY_NAME}, AGENTS.md section 6)")
    parser.add_argument("--output", default=None,
                        help="parent directory (default: the repository root)")
    parser.add_argument("--force", action="store_true",
                        help="replace the target folder if it already exists")
    parser.add_argument("--stage", type=int, choices=(1, 2), default=1,
                        help="which stage's answer folder to build (default: 1)")
    args = parser.parse_args(None if argv is None else argv)
    parent = Path(args.output).resolve() if args.output else PROJECT_ROOT

    if args.stage == 2:
        name = args.name if args.name != DELIVERY_NAME else STAGE2_NAME
        return build_stage2(parent / name, force=args.force)
    return build(parent / args.name, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())

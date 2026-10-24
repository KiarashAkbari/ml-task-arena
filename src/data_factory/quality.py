"""Data-quality report: per-column profile of the curated dataset (AGENTS 4.8.4).

Generated, never hand-written. One streaming pass over ``cases.csv`` writes
``<curated_dir>/data_quality.csv`` with, for **every** column of the delivered
dataset:

* ``empty_count`` / ``empty_pct`` — cells that are empty after cleaning (an
  empty cell is this project's null: see the per-column missing policies in
  :mod:`data_factory.clean`),
* ``unique_count`` — distinct non-empty values (measured, not assumed: the
  contract already forbids duplicate keys, and the report proves it),
* ``min`` / ``max`` — the observed range: numeric for integer columns, ISO
  (hence chronological) for date columns, lexicographic for text,
* ``top_values`` — the three most frequent values with their counts, for every
  column except identifier columns (``unique_key`` is unique by contract, so a
  frequency profile of it is noise) — the exemption is the only per-column
  choice in this module and it is also what keeps memory bounded. A flat
  profile on a high-cardinality column is itself information: it says the
  column behaves like an identifier.

The file is hashed into ``run_manifest.json`` like every other output, so any
number quoted in the report or notebook traces back to the exact dataset it
describes. Rows are streamed one at a time; the structures that grow with the
data are the distinct-value sets and the small frequency counters.

Determinism: percentages use fixed 2-decimal formatting and tie-breaks are
explicit (``-count`` then value), so two runs over the same ``cases.csv``
produce byte-identical reports.
"""

from __future__ import annotations

import csv
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from data_factory.clean import CASES_NAME, CURATED_COLUMNS
from data_factory.config import Settings
from data_factory.errors import ValidationError

DATA_QUALITY_NAME = "data_quality.csv"

TEXT = "text"
INTEGER = "integer"
DATE = "date"

# Column kind per curated column: dates and integers get numeric/chronological
# ranges, everything else is text. Checked against CURATED_COLUMNS by the suite
# so a new curated column cannot slip through unprofiled.
COLUMN_KINDS: dict[str, str] = {
    "unique_key": TEXT,
    "created_date": DATE,
    "closed_date": DATE,
    "agency": TEXT,
    "complaint_type": TEXT,
    "descriptor": TEXT,
    "borough": TEXT,
    "zip_code": TEXT,
    "status": TEXT,
    "population": INTEGER,
    "duration_minutes": INTEGER,
}

# Columns whose value is a per-row identifier: no frequency profile (see above).
IDENTIFIER_COLUMNS: frozenset[str] = frozenset({"unique_key"})

TOP_VALUES = 3
REPORT_HEADER: tuple[str, ...] = (
    "column",
    "kind",
    "rows",
    "empty_count",
    "empty_pct",
    "unique_count",
    "min",
    "max",
    "top_values",
)


@dataclass(frozen=True)
class QualityReport:
    """Measured outcome of one report generation."""

    path: Path
    rows: int
    columns: int
    emptiest_column: str   # "" when the dataset has no rows
    emptiest_pct: float


@dataclass
class _ColumnProfile:
    kind: str
    empty: int = 0
    values: set[Any] = field(default_factory=set)
    counts: Counter[str] | None = None
    minimum: Any = None
    maximum: Any = None

    def observe(self, raw: str, *, row_number: int, column: str) -> None:
        if raw == "":
            self.empty += 1
            return
        value: Any = raw
        if self.kind == INTEGER:
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValidationError(
                    f"data quality: column '{column}' is an integer column but "
                    f"row {row_number} holds {raw!r} — the cleaned dataset "
                    "violates its own contract"
                ) from exc
        self.values.add(value)
        if self.counts is not None:
            self.counts[raw] += 1
        if self.minimum is None or value < self.minimum:
            self.minimum = value
        if self.maximum is None or value > self.maximum:
            self.maximum = value

    def row(self, rows: int, column: str) -> list[str]:
        pct = (100.0 * self.empty / rows) if rows else 0.0
        top = ""
        if self.counts:
            ranked = sorted(self.counts.items(), key=lambda item: (-item[1], item[0]))
            top = "|".join(f"{value}={count}" for value, count in ranked[:TOP_VALUES])
        return [
            column,
            self.kind,
            str(rows),
            str(self.empty),
            f"{pct:.2f}",
            str(len(self.values)),
            "" if self.minimum is None else str(self.minimum),
            "" if self.maximum is None else str(self.maximum),
            top,
        ]


def run_quality_report(settings: Settings) -> QualityReport:
    """Profile ``cases.csv`` into ``<curated_dir>/data_quality.csv``.

    Raises:
        ValidationError: Cleaning has not run, or the case dataset's columns do
            not match :data:`~data_factory.clean.CURATED_COLUMNS` (a shape
            change must stop the run loudly, AGENTS 4.8.2), or an integer
            column holds a non-integer.
    """
    curated_dir = Path(settings.curated_dir)
    cases_path = curated_dir / CASES_NAME
    if not cases_path.is_file():
        raise ValidationError(
            f"no case dataset at '{cases_path}' — run the clean stage first"
        )

    columns = list(CURATED_COLUMNS)
    profiles = {
        name: _ColumnProfile(
            kind=COLUMN_KINDS[name],
            counts=None if name in IDENTIFIER_COLUMNS else Counter(),
        )
        for name in columns
    }

    rows = 0
    with cases_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            raise ValidationError(
                f"case dataset '{cases_path}' is empty (not even a header) — "
                "re-run the clean stage"
            ) from None
        if header != columns:
            raise ValidationError(
                f"case dataset '{cases_path}' has columns {header}, expected "
                f"{columns} — the cleaned dataset changed shape"
            )
        for values in reader:
            rows += 1
            if len(values) != len(columns):
                raise ValidationError(
                    f"case dataset '{cases_path}' row {rows} has {len(values)} "
                    f"field(s), expected {len(columns)}"
                )
            for name, raw in zip(columns, values):
                profiles[name].observe(raw, row_number=rows, column=name)

    report_path = curated_dir / DATA_QUALITY_NAME
    tmp_path = report_path.with_name(report_path.name + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as tmp:
            writer = csv.writer(tmp, lineterminator="\n")
            writer.writerow(list(REPORT_HEADER))
            for name in columns:
                writer.writerow(profiles[name].row(rows, name))
        os.replace(tmp_path, report_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    # Emptiest column for the CLI summary; ties break on dataset column order
    # (earliest column wins) so the choice is deterministic.
    emptiest_index = min(
        range(len(columns)), key=lambda index: (-profiles[columns[index]].empty, index)
    ) if rows else None
    emptiest = columns[emptiest_index] if emptiest_index is not None else ""
    return QualityReport(
        path=report_path,
        rows=rows,
        columns=len(columns),
        emptiest_column=emptiest,
        emptiest_pct=(100.0 * profiles[emptiest].empty / rows) if emptiest else 0.0,
    )

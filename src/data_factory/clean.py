"""Cleaning + second-source join over contract-passing records (AGENTS 4.4).

Runs after curate: re-applies the same :class:`RecordGate` stream (identical
order => identical partition, no second quarantine file) and cross-checks the
skip count against the quarantine output, then transforms each passing record
into one curated CSV row:

* **types** — dates validated as ISO at the gate and consumed as datetimes;
  ``duration_minutes`` computed from them (never negative: the gate already
  quarantined closed-before-created); ``population`` is an integer.
* **per-column missing policies** (each needs its own justification in the
  report): zip markers (``N/A``/blank) -> null; absent ``closed_date`` ->
  null duration; absent ``descriptor``/``population`` -> null; ``borough``
  kept as-is (including ``Unspecified`` — an explicit unknown beats a null);
  columns with a ``MISSING_*`` gate rule never reach cleaning (missing =
  quarantined, not silent null).
* **join** — LEFT join against the population reference by ZIP: row counts
  are asserted equal before/after (the reference loader forbids duplicate
  ZIPs, so the join cannot fan out); unmatched/missing ZIPs keep the row with
  null population; the reference borough is cross-checked against 311's
  ``borough`` and mismatches are counted (never dropped).
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from data_factory.config import Settings
from data_factory.errors import ValidationError
from data_factory.fetch import BATCH_NAME_TEMPLATE, CHECKPOINT_NAME, _load_checkpoint
from data_factory.population import PopulationRow, load_population, normalize_borough
from data_factory.validation import ZIP_MISSING_MARKERS, RecordGate

CURATED_COLUMNS: tuple[str, ...] = (
    "unique_key",
    "created_date",
    "closed_date",
    "agency",
    "complaint_type",
    "descriptor",
    "borough",
    "zip_code",
    "status",
    "population",
    "duration_minutes",
)
CASES_NAME = "cases.csv"
UNSPECIFIED_BOROUGH = "Unspecified"


@dataclass(frozen=True)
class CleanResult:
    """Measured outcome of one clean invocation (all counts are real)."""

    rows_scanned: int
    rows_written: int
    rows_skipped: int          # == quarantine file lines (cross-checked)
    zip_matched: int           # ZIP found in reference -> population attached
    zip_missing: int           # no/ marker ZIP -> null population
    zip_unknown: int           # ZIP present but absent from reference
    borough_mismatches: int    # reference borough vs 311 borough (cross-check)
    duration_nulls: int


def _naive(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _clean_row(
    record: dict[str, Any],
    settings: Settings,
    population: dict[str, PopulationRow],
) -> tuple[list[str], int, int, int, int]:
    fields = settings.source_field_map

    def text(canonical: str) -> str:
        value = record.get(fields[canonical])
        return value.strip() if isinstance(value, str) else ""

    def optional_text(canonical: str) -> str | None:
        value = record.get(fields[canonical])
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None

    created_raw = text("created_date")
    closed_raw = optional_text("closed_date")

    # duration_minutes: computed type-fixing; null iff closed_date absent.
    duration = ""
    if closed_raw is not None:
        duration = str(int((_naive(closed_raw) - _naive(created_raw)).total_seconds() // 60))

    zip_code = text("zip_code")
    if zip_code.upper() in ZIP_MISSING_MARKERS:
        zip_code = ""  # per-column missing policy: markers (N/A, blank) -> null
    pop_row = population.get(zip_code) if zip_code else None
    if not zip_code:
        matched, missing, unknown = 0, 1, 0
        population_value: int | str = ""
    elif pop_row is None:
        matched, missing, unknown = 0, 0, 1
        population_value = ""
    else:
        matched, missing, unknown = 1, 0, 0
        population_value = pop_row.population

    mismatch = 0
    ref_borough = pop_row.borough if pop_row else None
    borough = text("borough")
    if (
        ref_borough
        and borough
        and borough != UNSPECIFIED_BOROUGH
        and normalize_borough(borough) != ref_borough
    ):
        mismatch = 1

    # The district label is canonicalised here, once, so that every consumer
    # agrees: 311 publishes BROOKLYN, the reference publishes Brooklyn, and
    # both the aggregate table and the EDA join on the district name. Without
    # this the join matched nothing and every rate cell came out blank —
    # found by a real run, not by the tests (their fixtures were title-case).
    district = normalize_borough(borough) or ""

    row_values = [
        text("unique_key"),
        created_raw,
        closed_raw or "",
        text("agency"),
        text("complaint_type"),
        optional_text("descriptor") or "",
        district,
        zip_code,
        text("status"),
        str(population_value) if population_value != "" else "",
        duration,
    ]
    return row_values, matched, missing, unknown, mismatch


def run_clean(settings: Settings) -> CleanResult:
    """Build ``<curated_dir>/cases.csv`` from gate-passing raw records.

    Raises:
        ValidationError: Curate stage not run (or its output tampered with),
            or the population reference missing/invalid.
    """
    raw_dir = Path(settings.raw_dir)
    curated_dir = Path(settings.curated_dir)
    checkpoint_path = raw_dir / CHECKPOINT_NAME
    if not checkpoint_path.is_file():
        raise ValidationError(
            f"no fetch checkpoint in '{raw_dir}' — run the fetch stage first"
        )
    state = _load_checkpoint(checkpoint_path, settings.batch_size)
    if not state["complete"]:
        raise ValidationError(
            f"fetch is incomplete ({state['records_written']} records so far) — "
            "resume the fetch stage first"
        )

    quarantine_path = curated_dir / "quarantine.jsonl"
    if not quarantine_path.is_file():
        raise ValidationError("no quarantine output — run the curate stage first")
    quarantine_lines = sum(
        1 for line in quarantine_path.read_text(encoding="utf-8").splitlines() if line
    )

    population = load_population(settings.population_path)

    cases_path = curated_dir / CASES_NAME
    tmp_path = cases_path.with_name(cases_path.name + ".tmp")

    gate = RecordGate(settings)  # fresh, same stream order => same partition
    rows_scanned = 0
    rows_written = 0
    rows_skipped = 0
    zip_matched = zip_missing = zip_unknown = 0
    borough_mismatches = 0
    duration_nulls = 0

    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as tmp:
            writer = csv.writer(tmp, lineterminator="\n")
            writer.writerow(CURATED_COLUMNS)
            for index in range(state["batches_written"]):
                batch_path = raw_dir / BATCH_NAME_TEMPLATE.format(index=index)
                batch = json.loads(batch_path.read_text(encoding="utf-8"))
                for record in batch:
                    rows_scanned += 1
                    if gate.check(record):
                        rows_skipped += 1
                        continue
                    values, matched, missing, unknown, mismatch = _clean_row(
                        record, settings, population
                    )
                    writer.writerow(values)
                    rows_written += 1
                    zip_matched += matched
                    zip_missing += missing
                    zip_unknown += unknown
                    borough_mismatches += mismatch
                    if values[-1] == "":
                        duration_nulls += 1
        os.replace(tmp_path, cases_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    if rows_skipped != quarantine_lines:
        raise ValidationError(
            f"gate inconsistency: curate quarantined {quarantine_lines} record(s) "
            f"but clean skipped {rows_skipped} — quarantine output and raw data "
            "are out of sync; re-run curate"
        )
    if rows_written != rows_scanned - rows_skipped:
        raise ValidationError(
            f"join invariant broken: wrote {rows_written} of "
            f"{rows_scanned - rows_skipped} passing rows"
        )

    return CleanResult(
        rows_scanned=rows_scanned,
        rows_written=rows_written,
        rows_skipped=rows_skipped,
        zip_matched=zip_matched,
        zip_missing=zip_missing,
        zip_unknown=zip_unknown,
        borough_mismatches=borough_mismatches,
        duration_nulls=duration_nulls,
    )

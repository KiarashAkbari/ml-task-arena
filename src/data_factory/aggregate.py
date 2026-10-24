"""Wide aggregate: district x complaint_type x month with population rates.

AGENTS.md section 4.5 output 2 — a table a human can read across the screen:
one row per (district, complaint type), one column per month (counts), then
the same months as population-normalized rates per 100,000 residents, plus
row totals. Not a long three-column list.

Decisions (report material):

* **District = the 311 ``borough`` column** (the analysis question is about
  the city's own districts; the reference table only supplies denominators
  and the section 3 cross-check).
* **Rates per 100,000** (named constant), monthly: ``count / district_pop *
  100000``, formatted to 2 decimals (IEEE-stable -> deterministic).
* Districts where the reference gives no population (e.g. ``Unspecified``)
  get blank rate cells — never a fabricated denominator.
* The grid is complete: every district present in the data x every LOCKED
  scope type, zero-filled, so shapes match across runs.
* Memory: rows stream from ``cases.csv``; only the small cell grid accumulates.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path

from data_factory.clean import CASES_NAME
from data_factory.config import Settings
from data_factory.errors import ValidationError
from data_factory.population import load_population

RATE_PER = 100_000  # rate units: complaints per 100,000 residents
AGGREGATE_NAME = "aggregate.csv"


@dataclass(frozen=True)
class AggregationResult:
    rows_written: int
    months: tuple[str, ...]
    districts: tuple[str, ...]


def _month_range(first: str, last: str) -> list[str]:
    """Inclusive list of YYYY-MM strings between two month keys."""
    year, month = int(first[:4]), int(first[5:7])
    end_year, end_month = int(last[:4]), int(last[5:7])
    months: list[str] = []
    while (year, month) <= (end_year, end_month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return months


def run_aggregate(settings: Settings) -> AggregationResult:
    """Build ``<curated_dir>/aggregate.csv`` from the curated case dataset.

    Raises:
        ValidationError: If cleaning has not run (no case dataset).
    """
    curated_dir = Path(settings.curated_dir)
    cases_path = curated_dir / CASES_NAME
    if not cases_path.is_file():
        raise ValidationError(
            f"no case dataset at '{cases_path}' — run the clean stage first"
        )

    population = load_population(settings.population_path)
    district_population: dict[str, int] = {}
    for row in population.values():
        if row.borough is not None:
            district_population[row.borough] = (
                district_population.get(row.borough, 0) + row.population
            )

    # Stream cases: cells only, never the rows themselves.
    cells: dict[tuple[str, str], dict[str, int]] = {}
    districts_seen: set[str] = set()
    min_month: str | None = None
    max_month: str | None = None
    with cases_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            district = row["borough"]
            complaint_type = row["complaint_type"]
            month = row["created_date"][:7]
            districts_seen.add(district)
            min_month = month if min_month is None or month < min_month else min_month
            max_month = month if max_month is None or month > max_month else max_month
            key = (district, complaint_type)
            cells.setdefault(key, {})
            cells[key][month] = cells[key].get(month, 0) + 1

    if min_month is None or max_month is None:
        raise ValidationError(
            "case dataset is empty — nothing to aggregate (was clean run on real data?)"
        )

    months = _month_range(min_month, max_month)
    complaint_types = sorted(settings.scope_complaint_types)
    districts = sorted(districts_seen)

    aggregate_path = curated_dir / AGGREGATE_NAME
    tmp_path = aggregate_path.with_name(aggregate_path.name + ".tmp")
    header = (
        ["borough", "complaint_type", *months, "total"]
        + [f"rate_{month}" for month in months]
        + ["total_rate"]
    )

    def rate(count: int, district: str) -> str:
        population_size = district_population.get(district)
        if not population_size:
            return ""  # no fabricated denominator
        return f"{count / population_size * RATE_PER:.2f}"

    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as tmp:
            writer = csv.writer(tmp, lineterminator="\n")
            writer.writerow(header)
            for district in districts:
                for complaint_type in complaint_types:
                    counts = cells.get((district, complaint_type), {})
                    monthly = [counts.get(month, 0) for month in months]
                    total = sum(monthly)
                    writer.writerow(
                        [district, complaint_type, *monthly, total]
                        + [rate(count, district) for count in monthly]
                        + [rate(total, district)]
                    )
        os.replace(tmp_path, aggregate_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return AggregationResult(
        rows_written=len(districts) * len(complaint_types),
        months=tuple(months),
        districts=tuple(districts),
    )

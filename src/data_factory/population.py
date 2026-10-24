"""Population reference source: ZIP -> population -> district.

AGENTS.md section 3: an independent public reference table enters the factory
as its own source and provides the population denominator. Finding and
validating that source is part of the challenge — the student downloads it to
``population.path`` (committed as small reference data, unlike raw 311 dumps)
and records its URL and fetch date in config for the report and manifest.

Required CSV format (README documents it):

* columns ``zip,population,borough`` — ``borough`` feeds BOTH the section 3
  cross-check against 311's own borough column AND the population-normalized
  rates of the aggregate table (section 4.5); county names are normalized
  (KINGS -> Brooklyn etc.)
* ZIPs unique (the loader rejects duplicates so the join can never fan out
  and change row counts), population a positive integer, every row classified
  into a district (an unclassified ZIP would silently vanish from denominators)
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from data_factory.errors import ValidationError

REQUIRED_COLUMNS = ("zip", "population", "borough")
OPTIONAL_COLUMNS: tuple[str, ...] = ()
ALLOWED_COLUMNS = frozenset(REQUIRED_COLUMNS + OPTIONAL_COLUMNS)

# County-name -> NYC borough, for reference sources published per county.
BOROUGH_ALIASES = {
    "KINGS": "Brooklyn",
    "QUEENS": "Queens",
    "BRONX": "Bronx",
    "RICHMOND": "Staten Island",
    "NEW YORK": "Manhattan",
    "BROOKLYN": "Brooklyn",
    "MANHATTAN": "Manhattan",
    "STATEN ISLAND": "Staten Island",
}


@dataclass(frozen=True)
class PopulationRow:
    zip_code: str
    population: int
    borough: str | None  # normalized NYC borough, or None when not provided


def normalize_borough(value: str | None) -> str | None:
    """Map county/borough spellings to canonical NYC borough names."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return BOROUGH_ALIASES.get(cleaned.upper(), cleaned)


def load_population(path: str | Path) -> dict[str, PopulationRow]:
    """Load and validate the ZIP population reference table.

    Raises:
        ValidationError: File missing, wrong columns, bad ZIP/population
            values, or duplicate ZIPs (which would fan out the join).
    """
    csv_path = Path(path)
    if not csv_path.is_file():
        raise ValidationError(
            f"population reference '{csv_path}' not found — download the ZIP "
            "population source into that path (see README; choose and validate "
            "the source per AGENTS.md section 3)"
        )

    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        unknown = sorted(set(header) - ALLOWED_COLUMNS)
        missing = [column for column in REQUIRED_COLUMNS if column not in header]
        if missing or unknown:
            raise ValidationError(
                f"population reference '{csv_path}' header must be "
                f"{list(REQUIRED_COLUMNS)} + optional {list(OPTIONAL_COLUMNS)}; "
                f"missing={missing}, unknown={unknown}"
            )

        rows: dict[str, PopulationRow] = {}
        for raw in reader:
            line = reader.line_num
            zip_value = (raw.get("zip") or "").strip()
            if len(zip_value) != 5 or not zip_value.isdigit():
                raise ValidationError(
                    f"population reference line {line}: 'zip' must be 5 digits, "
                    f"got {zip_value!r}"
                )
            if zip_value in rows:
                raise ValidationError(
                    f"population reference line {line}: duplicate zip {zip_value} "
                    "(join would multiply rows)"
                )
            population_value = (raw.get("population") or "").strip()
            if not population_value.isdigit() or int(population_value) < 1:
                raise ValidationError(
                    f"population reference line {line}: 'population' must be a "
                    f"positive integer, got {population_value!r}"
                )
            borough_raw = (raw.get("borough") or "").strip()
            if not borough_raw:
                raise ValidationError(
                    f"population reference line {line}: 'borough' is required "
                    "(rates and the district cross-check need every ZIP classified)"
                )
            borough = normalize_borough(borough_raw)
            rows[zip_value] = PopulationRow(
                zip_code=zip_value,
                population=int(population_value),
                borough=borough,
            )

    if not rows:
        raise ValidationError(f"population reference '{csv_path}' contains no data rows")
    return rows

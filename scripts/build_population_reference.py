"""Build the ZIP -> population reference table that acts as the second source.

The factory needs a public table shaped ``zip,population,borough``
(AGENTS.md section 3): ``population`` is the denominator of every per-capita
rate, and ``borough`` lets ``clean`` cross-check its own district attribution.

This script builds that file from sources that are independent of the 311
complaint data:

1. **Population** — U.S. Census Bureau, 2020 Census Demographic and Housing
   Characteristics (DHC), table P1, at ZIP Code Tabulation Area (ZCTA) level,
   read from the Census API (the ``dec/*`` endpoints need no key).
2. **District** — the NYC ZIP-plan rule (100-102 Manhattan, 103 Staten Island,
   104 Bronx, 112 Brooklyn, 111/113/114/116 Queens). A published rule rather
   than a lookup table, so ``clean``'s ``borough_mismatches`` counter compares
   two genuinely independent attributions instead of 311 against itself.

Deliberate omissions (each one is a decision, not an oversight):

* The **110xx block is excluded**: it mixes Queens border ZIPs with Nassau
  County (Great Neck, Manhasset, Port Washington). Guessing would silently add
  Nassau residents to Queens' denominator; those cases instead keep a null
  population and are counted as ``zip unknown`` by the clean stage.
* ZIPs with blank or zero population are dropped (a zero denominator is not a
  measurement).
* Non-NYC ZIPs are dropped: a ZIP that 311 mentions but that is not in the rule
  is left out rather than forced into a district.

The output is small reference data and is meant to be committed (unlike raw
311 dumps). Run ``--write-config`` once to record the source URL and fetch date
in ``config/default.json``; both then appear in ``run_manifest.json``.

Examples::

    python scripts/build_population_reference.py --write-config
    python scripts/build_population_reference.py --csv ~/Downloads/zcta.csv
    python scripts/build_population_reference.py --check
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "reference" / "zip_population.csv"
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "default.json"

#: Census API endpoints tried in order, with the total-population column each
#: one exposes. ``dec/*`` endpoints are usable without an API key; the ACS
#: fallback usually needs one and is listed last on purpose.
CENSUS_SOURCES: tuple[tuple[str, str], ...] = (
    ("2020 Census DHC (P1)",
     "https://api.census.gov/data/2020/dec/dhc"
     "?get=NAME,P1_001N&for=zip%20code%20tabulation%20area:*"),
    ("2020 Census PL (P1)",
     "https://api.census.gov/data/2020/dec/pl"
     "?get=NAME,P1_001N&for=zip%20code%20tabulation%20area:*"),
    ("2010 Census SF1 (P001)",
     "https://api.census.gov/data/2010/dec/sf1"
     "?get=NAME,P001001&for=zip%20code%20tabulation%20area:*"),
)

POPULATION_COLUMNS: tuple[str, ...] = (
    "P1_001N", "P001001", "P0010001", "B01003_001E", "POP100", "population",
)
ZIP_COLUMN_HINTS: tuple[str, ...] = ("zcta", "zip", "geoid")
FIVE_DIGITS = re.compile(r"^\d{5}$")

#: ZIP-plan rule for the five boroughs. Prefixes outside this map are not NYC.
BOROUGH_PREFIXES: dict[tuple[int, ...], str] = {
    (100, 101, 102): "Manhattan",
    (103,): "Staten Island",
    (104,): "Bronx",
    (112,): "Brooklyn",
    (111, 113, 114, 116): "Queens",
}

#: Plausibility band per borough, only used to warn (never to reject).
POPULATION_BANDS: dict[str, tuple[int, int]] = {
    "Manhattan": (1_200_000, 2_100_000),
    "Staten Island": (350_000, 650_000),
    "Bronx": (1_100_000, 1_800_000),
    "Brooklyn": (2_200_000, 3_000_000),
    "Queens": (1_900_000, 2_800_000),
}


def borough_for_zip(zip_code: str) -> str | None:
    """Map a 5-digit NYC ZIP to its borough, or ``None`` when it is not NYC.

    Non-NYC input (wrong length, non-digits, prefix outside the plan) returns
    ``None``; callers drop those rows instead of guessing a district.
    """
    value = str(zip_code).strip()
    if not FIVE_DIGITS.match(value):
        return None
    prefix = int(value[:3])
    for prefixes, borough in BOROUGH_PREFIXES.items():
        if prefix in prefixes:
            return borough
    return None


def parse_census_rows(payload: Any) -> dict[str, int]:
    """Turn a Census API JSON payload into ``{zcta: population}``.

    Accepts the API's list-of-lists shape (header row first) and finds the
    total-population and ZCTA columns by name, raising :class:`ValueError`
    with a readable message when the payload has an unexpected shape.
    """
    if not isinstance(payload, list) or len(payload) < 2:
        raise ValueError(
            "Census payload is not a non-empty JSON array of rows — the "
            "endpoint may have returned an error object instead of data"
        )
    header = payload[0]
    if not all(isinstance(name, str) for name in header):
        raise ValueError(f"Census header row is not a list of column names: {header!r}")

    population_index = next(
        (header.index(name) for name in POPULATION_COLUMNS if name in header), None
    )
    if population_index is None:
        raise ValueError(
            f"Census payload has no total-population column; got {header}. "
            "Known names: " + ", ".join(POPULATION_COLUMNS)
        )
    zip_index = next(
        (
            index
            for index, name in enumerate(header)
            if any(hint in name.lower() for hint in ZIP_COLUMN_HINTS)
        ),
        None,
    )

    populations: dict[str, int] = {}
    for row in payload[1:]:
        if not isinstance(row, list) or len(row) <= population_index:
            continue
        zip_value = ""
        if zip_index is not None and len(row) > zip_index:
            zip_value = str(row[zip_index]).strip()
        if not FIVE_DIGITS.match(zip_value):
            # NAME is like "ZCTA5 10001"; a 5-digit token is the ZCTA code.
            match = re.search(r"(\d{5})(?!\d)", str(row[0]))
            if match is None:
                continue
            zip_value = match.group(1)
        raw_population = str(row[population_index]).strip().replace(",", "")
        if not raw_population.isdigit():
            continue
        populations[zip_value] = int(raw_population)
    if not populations:
        raise ValueError("Census payload parsed to zero rows")
    return populations


def parse_population_csv(text: str) -> dict[str, int]:
    """Parse a locally saved population table (``zcta,population`` or similar).

    Used when the Census API is unreachable from the user's network: any CSV
    whose header names a ZIP/ZCTA column and a population column works.
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("CSV is empty") from None
    lowered = [name.strip().lower() for name in header]

    zip_index = next(
        (index for index, name in enumerate(lowered)
         if any(hint in name for hint in ZIP_COLUMN_HINTS)),
        None,
    )
    population_index = next(
        (header.index(name) for name in POPULATION_COLUMNS if name in header), None
    )
    if population_index is None:
        population_index = next(
            (index for index, name in enumerate(lowered)
             if any(hint in name for hint in ("pop", "total"))),
            None,
        )
    if zip_index is None or population_index is None:
        raise ValueError(
            f"could not identify the ZIP and population columns in {header}; "
            "rename them to 'zcta' and 'population' (or pass --csv with a "
            "file that has those columns)"
        )

    populations: dict[str, int] = {}
    for row in reader:
        if len(row) <= max(zip_index, population_index):
            continue
        zip_value = row[zip_index].strip()
        if not FIVE_DIGITS.match(zip_value):
            continue
        raw_population = row[population_index].strip().replace(",", "")
        if not raw_population.isdigit():
            continue
        populations[zip_value] = int(raw_population)
    if not populations:
        raise ValueError("CSV parsed to zero ZIP rows")
    return populations


def build_reference(populations: dict[str, int]) -> list[tuple[str, int, str]]:
    """Filter to NYC ZIPs with a usable denominator; sorted by ZIP."""
    rows: list[tuple[str, int, str]] = []
    for zip_code in sorted(populations):
        borough = borough_for_zip(zip_code)
        population = populations[zip_code]
        if borough is None or population <= 0:
            continue
        rows.append((zip_code, population, borough))
    if not rows:
        raise ValueError(
            "no NYC ZIPs survived filtering — the source probably does not use "
            "5-digit ZCTAs (or every population was blank)"
        )
    return rows


def write_reference(rows: Iterable[tuple[str, int, str]], path: Path) -> Path:
    """Write ``zip,population,borough`` atomically (temp file + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(("zip", "population", "borough"))
            writer.writerows(rows)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return path


def fetch_census_populations() -> tuple[str, dict[str, int]]:
    """Try the Census endpoints in order; return ``(label, populations)``."""
    import requests  # imported here so --csv/--check work without the network

    failures: list[str] = []
    for label, url in CENSUS_SOURCES:
        try:
            response = requests.get(url, timeout=60)
            if response.status_code != 200:
                failures.append(f"{label}: HTTP {response.status_code}")
                continue
            populations = parse_census_rows(response.json())
        except Exception as exc:  # noqa: BLE001 - try the next endpoint
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
            continue
        return label, populations
    raise RuntimeError(
        "every Census endpoint failed:\n  " + "\n  ".join(failures) +
        "\n\nIf your network blocks api.census.gov, download the 2020 ZCTA "
        "population table manually and re-run with --csv PATH."
    )


def summarise(rows: list[tuple[str, int, str]]) -> str:
    """Human-readable sanity check: ZIP counts and population per borough."""
    totals: dict[str, int] = {}
    counts: dict[str, int] = {}
    for _, population, borough in rows:
        totals[borough] = totals.get(borough, 0) + population
        counts[borough] = counts.get(borough, 0) + 1
    lines = [f"{len(rows)} NYC ZIPs"]
    for borough in sorted(totals):
        lines.append(
            f"  {borough:14s} {counts[borough]:>4} ZIPs  {totals[borough]:>12,} residents"
        )
    warnings = []
    for borough, total in totals.items():
        low, high = POPULATION_BANDS[borough]
        if not low <= total <= high:
            warnings.append(
                f"{borough}: {total:,} residents is outside the plausible band "
                f"[{low:,}, {high:,}] — check the source"
            )
    if warnings:
        lines.append("warnings:")
        lines.extend(f"  ! {warning}" for warning in warnings)
    return "\n".join(lines)


def write_config_metadata(config_path: Path, *, source_url: str, fetched: str) -> None:
    """Record the source URL and fetch date in the config's population section."""
    data = json.loads(config_path.read_text(encoding="utf-8"))
    population = data.setdefault("population", {})
    population["source_url"] = source_url
    population["fetched_date"] = fetched
    original = config_path.read_text(encoding="utf-8")
    tmp_path = config_path.with_name(config_path.name + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, config_path)

    from data_factory.config import load_config  # validates the written file

    try:
        load_config(config_path)
    except Exception:
        config_path.write_text(original, encoding="utf-8")
        raise


def check_reference(path: Path) -> int:
    """Load an existing reference through the factory's own validator."""
    from data_factory.population import load_population

    rows = load_population(path)
    totals: dict[str, int] = {}
    for row in rows.values():
        totals[row.borough] = totals.get(row.borough, 0) + row.population
    print(f"{path}: {len(rows)} ZIPs load cleanly")
    for borough in sorted(totals):
        print(f"  {borough:14s} {totals[borough]:>12,} residents")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=None,
                        help="use a locally saved population CSV instead of the Census API")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"reference file to write (default: {DEFAULT_OUTPUT.relative_to(PROJECT_ROOT)})")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="config file to update with --write-config")
    parser.add_argument("--write-config", action="store_true",
                        help="record the source URL and fetch date in the config")
    parser.add_argument("--fetched-date", default=None,
                        help="override the recorded fetch date (default: today, UTC)")
    parser.add_argument("--check", action="store_true",
                        help="only validate the current reference file and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the summary without writing anything")
    args = parser.parse_args(None if argv is None else argv)

    if args.check:
        return check_reference(args.output)

    try:
        if args.csv is not None:
            label = f"local file {args.csv}"
            source_url = ""
            populations = parse_population_csv(
                args.csv.read_text(encoding="utf-8-sig")
            )
        else:
            label, populations = fetch_census_populations()
            source_url = CENSUS_SOURCES[
                [name for name, _ in CENSUS_SOURCES].index(label)
            ][1]
        rows = build_reference(populations)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"source      : {label}")
    print(f"rows parsed : {len(populations):,}")
    print(summarise(rows))

    if args.dry_run:
        print("dry run: nothing written")
        return 0

    path = write_reference(rows, args.output)
    print(f"wrote       : {path}")

    if args.write_config:
        fetched = args.fetched_date or date.today().isoformat()
        write_config_metadata(args.config, source_url=source_url, fetched=fetched)
        print(f"config      : population.source_url + fetched_date -> {fetched}")
        if not source_url:
            print("note        : source_url left empty (built from a local file); "
                  "fill it in by hand for the report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

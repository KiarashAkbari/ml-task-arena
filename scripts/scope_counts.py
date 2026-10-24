#!/usr/bin/env python
"""Measure scope-candidate evidence for AGENTS.md section 3.

Run on a machine WITH internet access, from the repository root:

    python scripts/scope_counts.py [--config PATH]

For each scope candidate — NYPD noise complaints and DSNY — over the date
window from the project config, it reports:

  * total record count vs the 300,000-record floor (section 3),
  * status breakdown (input for the missing-duration policy, section 4.4),
  * the top complaint types (evidence for narrowing the scope),
  * how many HTTP requests it took (seed for the request budget, 4.8.3).

The printed numbers are the evidence the report must cite when justifying the
scope criterion and the date window.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from typing import Any

from data_factory.config import DEFAULT_CONFIG_PATH, Settings, load_config
from data_factory.errors import DataFactoryError, ValidationError
from data_factory.http_client import HttpClient, RetryPolicy

# Moves into project config when the fetch stage lands (step 4).
SOURCE_URL = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"
MIN_RECORDS = 300_000  # floor from AGENTS.md section 3
TOP_TYPES_LIMIT = 15


def window_where(settings: Settings) -> str:
    """Build the date-window clause; the configured end date is inclusive."""
    end_exclusive = settings.end_date + timedelta(days=1)
    return (
        f"created_date >= '{settings.start_date.isoformat()}T00:00:00' "
        f"AND created_date < '{end_exclusive.isoformat()}T00:00:00'"
    )


def candidates(settings: Settings) -> list[tuple[str, str]]:
    """Scope candidates as (label, $where clause) pairs."""
    window = window_where(settings)
    return [
        ("NYPD noise", f"agency='NYPD' AND complaint_type LIKE 'Noise%' AND {window}"),
        ("DSNY", f"agency='DSNY' AND {window}"),
    ]


def _fetch(client: HttpClient, params: dict[str, Any]) -> list[dict[str, Any]]:
    payload = client.get(SOURCE_URL, params=params)
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ValidationError(f"unexpected response shape for {params}: {payload!r}")
    return payload


def fetch_count(client: HttpClient, where: str) -> int:
    """Return record count for a $where clause via a single count query."""
    rows = _fetch(client, {"$select": "count(1)", "$where": where})
    if len(rows) != 1:
        raise ValidationError(f"count query returned {len(rows)} row(s): {rows!r}")
    try:
        return int(rows[0]["count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(f"count query returned unreadable payload: {rows!r}") from exc


def fetch_group(
    client: HttpClient, *, group_field: str, where: str
) -> list[dict[str, Any]]:
    """Return (value, count) rows for a field, most frequent first."""
    return _fetch(
        client,
        {
            "$select": f"{group_field},count(1)",
            "$group": group_field,
            "$where": where,
            "$order": "count(1) DESC",
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scope_counts",
        description="Measure scope-candidate counts against the 300k floor.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        metavar="PATH",
        help=f"path to the JSON config file (default: {DEFAULT_CONFIG_PATH})",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_config(args.config)
        client = HttpClient(RetryPolicy.from_settings(settings))

        print(f"window: {settings.start_date} .. {settings.end_date} (inclusive)")
        print(f"floor:  {MIN_RECORDS:,} records")
        for label, where in candidates(settings):
            total = fetch_count(client, where)
            verdict = "PASS" if total >= MIN_RECORDS else "FAIL"
            print(f"\n=== {label}: {total:,} records — {verdict} "
                  f"(margin {total - MIN_RECORDS:+,})")

            statuses = fetch_group(client, group_field="status", where=where)
            for row in statuses:
                count = int(row["count"])
                share = 100 * count / total if total else 0.0
                print(f"  status {row['status']!r}: {count:,} ({share:.1f}%)")

            types = fetch_group(client, group_field="complaint_type", where=where)
            print(f"  top complaint types (of {len(types)}):")
            for row in types[:TOP_TYPES_LIMIT]:
                print(f"    {int(row['count']):>10,}  {row['complaint_type']}")

        print(f"\nHTTP requests made: {client.request_count}")
        return 0
    except DataFactoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""Measure evidence for the LOCKED fetch scope (AGENTS.md section 3).

Run on a machine WITH internet access, from the repository root:

    python scripts/scope_counts.py [--config PATH]

For the scope configured in the project config (agency + explicit complaint
types + date window), it reports:

  * total record count vs the 300,000-record floor (section 3),
  * status breakdown (input for the missing-duration policy, section 4.4),
  * the complaint types present (scope-integrity check),
  * how many HTTP requests it took (seed for the request budget, 4.8.3).

The printed numbers are the evidence the report cites when justifying the
scope criterion and the date window. Historically this script compared the
two candidate scopes (NYPD noise vs DSNY); the candidate counts that decided
the scope were measured on 2026-10-24 (859,910 vs 445,384).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from data_factory.config import DEFAULT_CONFIG_PATH, Settings, load_config
from data_factory.errors import DataFactoryError, ValidationError
from data_factory.http_client import HttpClient, RetryPolicy
from data_factory.scope import build_where

MIN_RECORDS = 300_000  # floor from AGENTS.md section 3
TOP_TYPES_LIMIT = 15


def _fetch(client: HttpClient, url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    payload = client.get(url, params=params)
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ValidationError(f"unexpected response shape for {params}: {payload!r}")
    return payload


def fetch_count(client: HttpClient, url: str, where: str) -> int:
    """Return record count for a $where clause via a single count query.

    Socrata names the ``count(1)`` column variably (observed live: ``count_1``,
    also ``count``), so the single returned value is parsed positionally.
    """
    rows = _fetch(client, url, {"$select": "count(1)", "$where": where})
    if len(rows) != 1:
        raise ValidationError(f"count query returned {len(rows)} row(s): {rows!r}")
    row = rows[0]
    if len(row) != 1:
        raise ValidationError(
            f"count query returned unexpected columns {sorted(row)}: {row!r}"
        )
    value = next(iter(row.values()))
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"count query returned unreadable payload: {row!r}") from exc


def _row_count(row: dict[str, Any], group_field: str) -> int:
    """Parse the count out of a grouped row: ``{group_field, count_*}``."""
    if group_field not in row:
        raise ValidationError(f"group query row is missing '{group_field}': {row!r}")
    count_keys = [key for key in row if key != group_field]
    if len(count_keys) != 1:
        raise ValidationError(
            f"group query row has unexpected columns {sorted(row)}: {row!r}"
        )
    try:
        return int(row[count_keys[0]])
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"group query row is unreadable: {row!r}") from exc


def fetch_group(
    client: HttpClient, url: str, *, group_field: str, where: str
) -> list[dict[str, Any]]:
    """Return (value, count) rows for a field, most frequent first."""
    return _fetch(
        client,
        url,
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
        description="Measure the locked scope's counts against the 300k floor.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        metavar="PATH",
        help=f"path to the JSON config file (default: {DEFAULT_CONFIG_PATH})",
    )
    args = parser.parse_args(argv)

    try:
        settings: Settings = load_config(args.config)
        client = HttpClient(RetryPolicy.from_settings(settings))
        where = build_where(settings)
        label = (
            f"{settings.scope_agency} / {len(settings.scope_complaint_types)} "
            "locked complaint types"
        )

        print(f"window: {settings.start_date} .. {settings.end_date} (inclusive)")
        print(f"floor:  {MIN_RECORDS:,} records")

        total = fetch_count(client, settings.source_url, where)
        verdict = "PASS" if total >= MIN_RECORDS else "FAIL"
        print(f"\n=== {label}: {total:,} records — {verdict} "
              f"(margin {total - MIN_RECORDS:+,})")

        statuses = fetch_group(
            client, settings.source_url, group_field="status", where=where
        )
        for row in statuses:
            count = _row_count(row, "status")
            share = 100 * count / total if total else 0.0
            print(f"  status {row['status']!r}: {count:,} ({share:.1f}%)")

        types = fetch_group(
            client, settings.source_url, group_field="complaint_type", where=where
        )
        print(f"  complaint types present (of {len(settings.scope_complaint_types)} locked):")
        for row in types[:TOP_TYPES_LIMIT]:
            print(f"  {_row_count(row, 'complaint_type'):>10,}  {row['complaint_type']}")
        if len(types) > TOP_TYPES_LIMIT:
            print(f"  ... and {len(types) - TOP_TYPES_LIMIT} more")

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

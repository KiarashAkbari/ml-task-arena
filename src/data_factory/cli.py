"""Command-line entry point: arguments, config loading, exit-code mapping.

One command runs the whole factory (``run``, the default); a parameter runs a
single stage (``fetch`` / ``curate`` / ``clean`` / ``outputs``). Two flags
change *what* is fetched rather than which stage runs: ``--config`` (settings
live outside the code) and ``--since-last-run`` (the moving window of AGENTS
4.8.1). Every failure raised as a :class:`DataFactoryError` is mapped to its
documented exit code here (AGENTS.md section 4.1).
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data_factory import __version__
from data_factory import curate as curate_stage
from data_factory import fetch as fetch_stage
from data_factory import clean as clean_stage
from data_factory import moving_window as moving_window_stage
from data_factory import outputs as outputs_stage
from data_factory.config import DEFAULT_CONFIG_PATH, Settings, load_config
from data_factory.curate import CurateResult
from data_factory.clean import CleanResult
from data_factory.errors import (
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_UNEXPECTED,
    ConfigError,
    DataFactoryError,
)
from data_factory.http_client import HttpClient, RetryPolicy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="data-factory",
        description="Reproducible NYC 311 dataset factory (Stage 1).",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "fetch", "curate", "clean", "outputs", "db"),
        default="run",
        help="run = the whole pipeline (default); "
             "fetch / curate / clean / outputs = one stage each; "
             "db = the Stage 2 PostgreSQL destination",
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=("bootstrap", "status"),
        default=None,
        help="db sub-command: bootstrap = rebuild the database from sql/ and the "
             "CSV sources; status = report what is currently loaded",
    )
    parser.add_argument(
        "--url",
        default=None,
        metavar="URL",
        help="PostgreSQL connection URL (default: the DATABASE_URL environment "
             "variable), e.g. postgresql+psycopg://user:secret@localhost:5432/olist",
    )
    parser.add_argument(
        "--source-dir",
        default="data/raw/olist",
        metavar="PATH",
        help="directory holding the nine Olist CSV files (default: data/raw/olist)",
    )
    parser.add_argument(
        "--curated",
        default="data/curated/cases.csv",
        metavar="PATH",
        help="Stage 1 curated dataset loaded into the factory schema "
             "(default: data/curated/cases.csv)",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the version and exit",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        metavar="PATH",
        help=f"path to the JSON config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="print the resolved settings as JSON and exit",
    )
    parser.add_argument(
        "--since-last-run",
        action="store_true",
        help="moving window (AGENTS 4.8.1): fetch only [last window end + 1 day "
             ".. today] instead of the configured date_range; the previous raw "
             "window is archived under paths.raw_archive_dir, never deleted. "
             "Valid with 'run' and 'fetch' only.",
    )
    return parser


def _progress_printer():
    """Live progress on stderr, so stdout stays the machine-readable summary."""

    def report(records: int, batches: int, http_requests: int) -> None:
        print(
            f"fetch: {records:,} records — {batches:,} batch(es), "
            f"{http_requests:,} HTTP request(s)",
            file=sys.stderr,
            flush=True,
        )

    return report


def _run_fetch(settings: Settings, *, window_mode: str) -> None:
    """Execute the fetch stage and print its measured summary."""
    client = HttpClient(RetryPolicy.from_settings(settings))
    result = fetch_stage.run_fetch(
        settings, client, window_mode=window_mode, on_progress=_progress_printer()
    )
    if result.schema_audit:
        print(f"source schema: {result.schema_audit}")
    if result.new_columns:
        print(
            "source schema: column(s) first seen mid-run (sparse, not a shape "
            f"change): {', '.join(result.new_columns)}"
        )
    if result.already_complete:
        print(
            f"fetch: already complete — {result.records:,} records in "
            f"{result.batches or 'existing'} batches on disk (0 requests)"
        )
        return
    resumed = f", resumed {result.resumed_records:,}" if result.resumed_records else ""
    print(
        f"fetch: {result.records:,} records in {result.batches} batch(es){resumed} "
        f"— {result.http_requests} HTTP request(s); "
        f"peak in-memory batch: {result.peak_in_memory_records:,}"
    )


def _apply_moving_window(settings: Settings) -> tuple[Settings, str]:
    """Resolve ``--since-last-run`` and report what it decided (AGENTS 4.8.1).

    Returns:
        The settings to run with (dates = the resolved window) and the fetch
        window mode to record in the checkpoint.
    """
    moving = moving_window_stage.apply_moving_window(
        settings, today=moving_window_stage.today_utc()
    )
    resolution = moving.resolution
    previous = resolution.previous
    previous_text = (
        f"{previous[0].isoformat()}..{previous[1].isoformat()}" if previous else "?"
    )
    if resolution.already_current:
        print(
            "window: the last run already covers up to "
            f"{resolution.end.isoformat()} — nothing new to fetch; re-running "
            f"window {resolution.start.isoformat()}..{resolution.end.isoformat()}"
        )
    else:
        print(
            "window: since last run — "
            f"{resolution.start.isoformat()}..{resolution.end.isoformat()} "
            f"(previous {previous_text}, from {resolution.previous_source})"
        )
    if moving.archived_to is not None:
        print(f"window: archived the previous raw data to {moving.archived_to}")
    return moving.settings, fetch_stage.WINDOW_MODE_SINCE_LAST_RUN


def _print_curate_summary(result: CurateResult) -> None:
    print(
        f"curate: scanned {result.total:,} — passed {result.passed:,}, "
        f"quarantined {result.quarantined:,} ({result.ratio:.2%}; "
        f"threshold {result.threshold:.2%})"
    )
    for code, count in result.counts.items():
        print(f"  {code}: {count:,}")


def _run_curate(settings: Settings) -> None:
    """Execute the curate stage, report per-reason counts, enforce threshold."""
    result = curate_stage.run_curate(settings)
    _print_curate_summary(result)
    curate_stage.ensure_within_threshold(result)


def _run_clean(settings: Settings) -> None:
    """Execute the clean + join stage and print its measured summary."""
    result = clean_stage.run_clean(settings)
    print(
        f"clean: wrote {result.rows_written:,} rows "
        f"(scanned {result.rows_scanned:,}, quarantined {result.rows_skipped:,}); "
        f"zip matched {result.zip_matched:,}, missing {result.zip_missing:,}, "
        f"unknown {result.zip_unknown:,}; borough mismatches "
        f"{result.borough_mismatches:,}; null durations {result.duration_nulls:,}"
    )


def _run_outputs(settings: Settings, *, started_at: str) -> None:
    """Execute the outputs stage (aggregate + quality report + manifest)."""
    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = outputs_stage.run_outputs(
        settings, started_at=started_at, finished_at=finished_at
    )
    agg = result.aggregate
    quality = result.quality
    emptiest = (
        f"; emptiest column {quality.emptiest_column} "
        f"({quality.emptiest_pct:.2f}% empty)"
        if quality.rows
        else ""
    )
    print(
        f"outputs: {agg.rows_written} rows x {len(agg.months)} months "
        f"across {len(agg.districts)} district(s) -> aggregate.csv; "
        f"data_quality.csv profiled {quality.columns} column(s) over "
        f"{quality.rows:,} row(s){emptiest}; "
        f"run_manifest.json hashed {len(result.output_hashes)} file(s)"
    )
    for name, digest in sorted(result.output_hashes.items()):
        print(f"  {name}: sha256 {digest[:12]}…")


def _settings_as_json(settings: Settings) -> dict[str, Any]:
    return {
        "source_url": settings.source_url,
        "source_field_map": dict(settings.source_field_map),
        "population": {
            "path": settings.population_path.as_posix(),
            "source_url": settings.population_source_url,
            "fetched_date": (
                settings.population_fetched_date.isoformat()
                if settings.population_fetched_date
                else None
            ),
        },
        "scope": {
            "agency": settings.scope_agency,
            "complaint_types": list(settings.scope_complaint_types),
        },
        "start_date": settings.start_date.isoformat(),
        "end_date": settings.end_date.isoformat(),
        "batch_size": settings.batch_size,
        "max_retries": settings.max_retries,
        "retry_backoff_seconds": settings.retry_backoff_seconds,
        "timeout_seconds": settings.timeout_seconds,
        "raw_dir": settings.raw_dir.as_posix(),
        "curated_dir": settings.curated_dir.as_posix(),
        "raw_archive_dir": settings.raw_archive_dir.as_posix(),
        "quarantine_max_ratio": settings.quarantine_max_ratio,
    }


def _resolve_database_url(args: argparse.Namespace) -> str:
    """CLI flag first, environment second; a clear error if neither is set."""
    import os

    url = args.url or os.environ.get("DATABASE_URL")
    if not url:
        raise ConfigError(
            "no database URL: pass --url postgresql+psycopg://user:secret@host:5432/olist "
            "or export DATABASE_URL"
        )
    return url


def _run_db(args: argparse.Namespace) -> None:
    """Stage 2 destination: build or inspect the PostgreSQL database."""
    from data_factory.db.bootstrap import bootstrap
    from data_factory.db.connection import engine_scope
    from data_factory.db.loader import OLIST_LOAD_ORDER, count_rows

    url = _resolve_database_url(args)
    action = args.action or "bootstrap"

    if action == "status":
        with engine_scope(url) as engine:
            with engine.connect() as conn:
                for table in OLIST_LOAD_ORDER:
                    print(f"olist.{table}: {count_rows(conn, table):,} rows")
                print(f"factory.case_file: {count_rows(conn, 'case_file', schema='factory'):,} rows")
        return

    report = bootstrap(
        url,
        source_dir=Path(args.source_dir),
        curated_path=Path(args.curated),
    )
    print(f"db: {report.url}")
    print(f"db: database created: {report.created_database}")
    print(f"db: applied {len(report.ddl)} DDL script(s) from sql/")
    for table, rows in sorted(report.tables.items()):
        print(f"  {table}: {rows:,} rows")
    quarantine = report.checks.get("quarantine", {})
    print(
        f"db: rejected {quarantine.get('rejected_rows', 0):,} row(s) in "
        f"{quarantine.get('rejection_reasons', 0)} reason(s); "
        f"repaired {quarantine.get('repaired_rows', 0):,} value(s)"
    )
    for rejection in report.rejections:
        print(f"  {rejection['reason']}: {rejection['row_count']:,} ({rejection['source_file']})")
    checks = report.checks
    print(f"db: foreign-key orphans all zero: {checks.get('fk_all_zero')}")
    print(f"db: CHECK constraints refused every probe: {checks.get('constraints_all_enforced')}")
    print(f"db: total {report.timings['total']:.1f}s")


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return a process exit code.

    argparse usage errors are not caught here: argparse exits with code 2 on
    its own, which the exit-code contract reserves for usage mistakes.
    """
    parser = build_parser()
    args = parser.parse_args(None if argv is None else argv)

    if args.version:
        print(__version__)
        return EXIT_OK

    if args.action is not None and args.command != "db":
        parser.error(f"'{args.action}' is a sub-command of 'db', not '{args.command}'")

    if args.command == "db":
        try:
            _run_db(args)
            return EXIT_OK
        except DataFactoryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return exc.exit_code
        except Exception:
            traceback.print_exc()
            return EXIT_UNEXPECTED

    if args.since_last_run and args.command not in ("run", "fetch"):
        # Usage error (exit 2, argparse's own code): the flag chooses a fetch
        # window, so it is meaningless for the stages that only read raw data.
        parser.error(
            "--since-last-run applies to the 'run' and 'fetch' commands, "
            f"not '{args.command}'"
        )

    try:
        settings = load_config(args.config)
        if args.show_config:
            payload = {"config_path": args.config, "settings": _settings_as_json(settings)}
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            # One command = whole factory (run), a parameter = one part;
            # later stages append to "run" (AGENTS 4.1).
            started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            window_mode = fetch_stage.WINDOW_MODE_CONFIGURED
            if args.since_last_run:
                settings, window_mode = _apply_moving_window(settings)
            if args.command in ("run", "fetch"):
                _run_fetch(settings, window_mode=window_mode)
            if args.command in ("run", "curate"):
                _run_curate(settings)
            if args.command in ("run", "clean"):
                _run_clean(settings)
            if args.command in ("run", "outputs"):
                _run_outputs(settings, started_at=started_at)
        return EXIT_OK
    except DataFactoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception:
        traceback.print_exc()
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())

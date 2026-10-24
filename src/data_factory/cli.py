"""Command-line entry point: arguments, config loading, exit-code mapping.

Pipeline stages (fetch, quarantine, outputs…) are added in later steps; every
failure raised as a :class:`DataFactoryError` is mapped to its documented
exit code here (AGENTS.md section 4.1).
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any

from data_factory import __version__
from data_factory import fetch as fetch_stage
from data_factory.config import DEFAULT_CONFIG_PATH, Settings, load_config
from data_factory.errors import (
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_UNEXPECTED,
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
        choices=("run", "fetch"),
        default="run",
        help="run = the whole pipeline (default); fetch = only the fetch stage",
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
    return parser


def _run_fetch(settings: Settings) -> None:
    """Execute the fetch stage and print its measured summary."""
    client = HttpClient(RetryPolicy.from_settings(settings))
    result = fetch_stage.run_fetch(settings, client)
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


def _settings_as_json(settings: Settings) -> dict[str, Any]:
    return {
        "source_url": settings.source_url,
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
        "quarantine_max_ratio": settings.quarantine_max_ratio,
    }


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return a process exit code.

    argparse usage errors are not caught here: argparse exits with code 2 on
    its own, which the exit-code contract reserves for usage mistakes.
    """
    args = build_parser().parse_args(None if argv is None else argv)

    if args.version:
        print(__version__)
        return EXIT_OK

    try:
        settings = load_config(args.config)
        if args.show_config:
            payload = {"config_path": args.config, "settings": _settings_as_json(settings)}
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            # Both commands currently run the fetch stage; later stages of the
            # pipeline are appended to the default "run" command (AGENTS 4.1:
            # one command = whole factory, a parameter = one part).
            _run_fetch(settings)
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

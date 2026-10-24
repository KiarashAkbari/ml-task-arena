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
from data_factory.config import DEFAULT_CONFIG_PATH, Settings, load_config
from data_factory.errors import (
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_UNEXPECTED,
    DataFactoryError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="data-factory",
        description="Reproducible NYC 311 dataset factory (Stage 1).",
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


def _settings_as_json(settings: Settings) -> dict[str, Any]:
    return {
        "start_date": settings.start_date.isoformat(),
        "end_date": settings.end_date.isoformat(),
        "batch_size": settings.batch_size,
        "max_retries": settings.max_retries,
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
        # Pipeline stages will run here in later steps.
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

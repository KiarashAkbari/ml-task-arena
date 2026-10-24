"""Command-line entry point for the data factory.

The full pipeline (fetch -> validate/quarantine -> clean -> outputs) is added
step by step; this module owns argument parsing and maps failures to the
project exit codes (see AGENTS.md section 4.1).
"""

from __future__ import annotations

import sys

from data_factory import __version__


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return a process exit code.

    Args:
        argv: Argument list without the program name. Defaults to sys.argv[1:].
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["--version"], ["-V"]):
        print(__version__)
        return 0
    print(f"data-factory {__version__} (pipeline stages are added in later steps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

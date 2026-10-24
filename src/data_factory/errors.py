"""Project-specific exception hierarchy and process exit codes.

Exit-code contract (also documented in README):

======= ==========================================================
code    meaning
======= ==========================================================
0       success
1       unspecified failure (base DataFactoryError or unexpected bug)
2       command-line usage error (reserved: argparse's own default)
3       configuration error           (:class:`ConfigError`)
4       network / HTTP error          (:class:`NetworkError`)
5       data validation error         (:class:`ValidationError`)
130     interrupted by SIGINT (conventional 128 + 2)
======= ==========================================================
"""

from __future__ import annotations


class DataFactoryError(Exception):
    """Base class for every expected data-factory failure."""

    exit_code = 1


class ConfigError(DataFactoryError):
    """The configuration file is missing, unreadable, or invalid."""

    exit_code = 3


class NetworkError(DataFactoryError):
    """A data source could not be reached or answered unusably."""

    exit_code = 4


class ValidationError(DataFactoryError):
    """Data violated the schema contract / validation rules."""

    exit_code = 5


class QuarantineThresholdExceeded(ValidationError):
    """Quarantine ratio is above the configured threshold (AGENTS.md 4.3):
    the run must fail instead of continuing with suspect data."""

    exit_code = 5  # still a data-validation failure (exit-code contract: 4.1)


EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

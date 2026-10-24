"""Locked fetch scope: builds the SoQL ``$where`` clause from config.

AGENTS.md section 3 requires the scope criterion (agency, complaint types,
window) to be part of the contract: configurable, never hard-coded, and
recorded in ``run_manifest.json``. This module is the single place where the
configured scope becomes a query clause — the fetch stage (step 4) and the
evidence script both call it, so they can never drift apart.
"""

from __future__ import annotations

from datetime import timedelta

from data_factory.config import Settings


def _squote(value: str) -> str:
    """Return a SoQL single-quoted string literal with escaping."""
    return "'" + value.replace("'", "''") + "'"


def window_clause(settings: Settings) -> str:
    """Date window with the configured end date treated as inclusive."""
    end_exclusive = settings.end_date + timedelta(days=1)
    return (
        f"created_date >= '{settings.start_date.isoformat()}T00:00:00' "
        f"AND created_date < '{end_exclusive.isoformat()}T00:00:00'"
    )


def build_where(settings: Settings) -> str:
    """Compose the full ``$where`` clause for the configured scope.

    The complaint-type list is explicit (closed set) rather than a prefix
    pattern so that a brand-new type appearing upstream cannot silently change
    the scope — and therefore the output hashes — between two runs.
    """
    types = ", ".join(_squote(name) for name in settings.scope_complaint_types)
    return (
        f"agency = {_squote(settings.scope_agency)} "
        f"AND complaint_type IN ({types}) "
        f"AND {window_clause(settings)}"
    )

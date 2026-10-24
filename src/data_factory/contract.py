"""Explicit schema contract for the final dataset (AGENTS.md section 4.3).

The challenge requires four aspects per column — type, nullability, value
domain, and what makes a record unique. This module is that contract, written
down once and enforced in two places:

* ``validation.validate_record`` gates RAW records before cleaning (the
  pre-clean rules each column relies on are listed in ``ColumnSpec.raw_gate``),
* cleaning (step 6) is what guarantees the ``type`` shown here for the final
  curated dataset.

A record that violates the contract is never dropped silently: it goes to the
quarantine output with reason codes (see ``curate``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ColumnSpec:
    """One column of the final dataset."""

    name: str
    type: str      # final-dataset type after cleaning
    nullable: bool
    domain: str    # allowed values / pattern
    raw_gate: str  # reason codes enforced pre-clean ("" = enforced at cleaning/join)


COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        name="unique_key",
        type="string",
        nullable=False,
        domain="digits only; unique across the dataset",
        raw_gate="MISSING_UNIQUE_KEY, INVALID_UNIQUE_KEY",
    ),
    ColumnSpec(
        name="created_date",
        type="datetime",
        nullable=False,
        domain="ISO-8601 timestamp inside the configured window",
        raw_gate="MISSING_CREATED_DATE, INVALID_CREATED_DATE",
    ),
    ColumnSpec(
        name="closed_date",
        type="datetime",
        nullable=True,
        domain="ISO-8601 timestamp; null when the case is still open",
        raw_gate="INVALID_CLOSED_DATE",
    ),
    ColumnSpec(
        name="agency",
        type="string",
        nullable=False,
        domain="exactly the locked scope agency from config",
        raw_gate="MISSING_AGENCY, OUT_OF_SCOPE_AGENCY",
    ),
    ColumnSpec(
        name="complaint_type",
        type="string",
        nullable=False,
        domain="exactly one of the locked scope complaint types from config",
        raw_gate="MISSING_COMPLAINT_TYPE, OUT_OF_SCOPE_COMPLAINT_TYPE",
    ),
    ColumnSpec(
        name="borough",
        type="string",
        nullable=True,
        domain="Brooklyn | Bronx | Manhattan | Queens | Staten Island | Unspecified",
        raw_gate="",
    ),
    ColumnSpec(
        name="zip_code",
        type="string",
        nullable=True,
        domain="^[0-9]{5}$ and never the sentinel 00000; null if absent or N/A",
        raw_gate="INVALID_ZIP_CODE",
    ),
    ColumnSpec(
        name="status",
        type="string",
        nullable=True,
        domain="Closed | In Progress | Open | Assigned | Pending | Unspecified",
        raw_gate="",
    ),
)

# AGENTS.md 4.3: "چه چیزی آن را یکتا می‌کند" — the uniqueness key.
UNIQUENESS: tuple[str, ...] = ("unique_key",)


def spec(name: str) -> ColumnSpec:
    """Return the contract spec for a column name."""
    for column in COLUMNS:
        if column.name == name:
            return column
    raise KeyError(name)

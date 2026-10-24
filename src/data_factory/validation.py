"""Record-level contract, logical-validity, and duplicate rules (AGENTS 4.3/4.4).

Two layers:

* :func:`validate_record` — pure per-record checks of the pre-clean contract
  (all fields resolved through ``settings.source_field_map``, so the raw field
  names live in config, not here — AGENTS.md section 1) plus the logical rule
  *closed_date must not precede created_date*.
* :class:`RecordGate` — adds the stateful rules that need stream context:
  duplicates. Every record is checked in fetch order (unique_key order), which
  is deterministic across runs.

Decisions worth defending in the report:

* ``N/A`` and blank zip codes are MISSINGNESS MARKERS, not violations — they
  become null in cleaning (per-column missing policy). The sentinel ``00000``
  IS a violation (``INVALID_ZIP_CODE``): the value exists and is outside the
  domain; converting garbage to null would hide it from the discard report.
* **Duplicate definition** — a duplicate is a record that represents a case
  the factory has already seen: either the same ``unique_key`` again
  (``DUPLICATE_KEY``), or a different key with the identical business content
  ``(created_date, complaint_type, incident_address, borough, zip_code)``
  (``DUPLICATE_CONTENT``) — a source double-entry. First occurrence wins;
  later ones are quarantined. Fuzzy/near-duplicate matching is deliberately
  NOT used: it would discard genuinely distinct reports from different
  citizens.
* ``closed_date`` may be absent (nullable); present-but-unparseable or
  logically-before-created is quarantined with its own reason code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from data_factory.config import Settings

ZIP_MISSING_MARKERS = frozenset({"", "N/A"})
ZIP_SENTINEL_INVALID = "00000"
FIVE_DIGITS = 5

# Content fingerprint of the duplicate rule (canonical field names).
CONTENT_FINGERPRINT_FIELDS: tuple[str, ...] = (
    "created_date",
    "complaint_type",
    "incident_address",
    "borough",
    "zip_code",
)


class Reason:
    """Reason codes for quarantined records (stable, English, contract keys)."""

    MISSING_UNIQUE_KEY = "MISSING_UNIQUE_KEY"
    INVALID_UNIQUE_KEY = "INVALID_UNIQUE_KEY"
    MISSING_CREATED_DATE = "MISSING_CREATED_DATE"
    INVALID_CREATED_DATE = "INVALID_CREATED_DATE"
    INVALID_CLOSED_DATE = "INVALID_CLOSED_DATE"
    CLOSED_BEFORE_CREATED = "CLOSED_BEFORE_CREATED"
    MISSING_AGENCY = "MISSING_AGENCY"
    OUT_OF_SCOPE_AGENCY = "OUT_OF_SCOPE_AGENCY"
    MISSING_COMPLAINT_TYPE = "MISSING_COMPLAINT_TYPE"
    OUT_OF_SCOPE_COMPLAINT_TYPE = "OUT_OF_SCOPE_COMPLAINT_TYPE"
    INVALID_ZIP_CODE = "INVALID_ZIP_CODE"
    DUPLICATE_KEY = "DUPLICATE_KEY"
    DUPLICATE_CONTENT = "DUPLICATE_CONTENT"


REASONS: tuple[str, ...] = (
    Reason.MISSING_UNIQUE_KEY,
    Reason.INVALID_UNIQUE_KEY,
    Reason.MISSING_CREATED_DATE,
    Reason.INVALID_CREATED_DATE,
    Reason.INVALID_CLOSED_DATE,
    Reason.CLOSED_BEFORE_CREATED,
    Reason.MISSING_AGENCY,
    Reason.OUT_OF_SCOPE_AGENCY,
    Reason.MISSING_COMPLAINT_TYPE,
    Reason.OUT_OF_SCOPE_COMPLAINT_TYPE,
    Reason.INVALID_ZIP_CODE,
    Reason.DUPLICATE_KEY,
    Reason.DUPLICATE_CONTENT,
)


def _is_iso_timestamp(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _naive(value: str) -> datetime:
    """Parse and normalize to naive wall-clock for cross-tz comparisons."""
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def validate_record(record: Mapping[str, Any], settings: Settings) -> list[str]:
    """Return every reason code this raw record violates (``[]`` = pass).

    Field names are resolved through ``settings.source_field_map``; reason
    order is fixed (not input-dependent) so outputs stay deterministic.
    """
    fields = settings.source_field_map
    reasons: list[str] = []

    unique_key = record.get(fields["unique_key"])
    if unique_key is None or unique_key == "":
        reasons.append(Reason.MISSING_UNIQUE_KEY)
    elif not (isinstance(unique_key, str) and unique_key.isdigit()):
        reasons.append(Reason.INVALID_UNIQUE_KEY)

    created = record.get(fields["created_date"])
    created_ok = False
    if created is None or created == "":
        reasons.append(Reason.MISSING_CREATED_DATE)
    elif not isinstance(created, str) or not _is_iso_timestamp(created):
        reasons.append(Reason.INVALID_CREATED_DATE)
    else:
        created_ok = True

    closed = record.get(fields["closed_date"])
    closed_ok = False
    if closed is not None and closed != "":
        if not isinstance(closed, str) or not _is_iso_timestamp(closed):
            reasons.append(Reason.INVALID_CLOSED_DATE)
        else:
            closed_ok = True
    # Logical validity (AGENTS 4.4): a case cannot close before it is opened.
    if created_ok and closed_ok and _naive(closed) < _naive(created):
        reasons.append(Reason.CLOSED_BEFORE_CREATED)

    agency = record.get(fields["agency"])
    if agency is None or agency == "":
        reasons.append(Reason.MISSING_AGENCY)
    elif agency != settings.scope_agency:
        reasons.append(Reason.OUT_OF_SCOPE_AGENCY)

    complaint_type = record.get(fields["complaint_type"])
    if complaint_type is None or complaint_type == "":
        reasons.append(Reason.MISSING_COMPLAINT_TYPE)
    elif complaint_type not in settings.scope_complaint_types:
        reasons.append(Reason.OUT_OF_SCOPE_COMPLAINT_TYPE)

    zip_code = record.get(fields["zip_code"])
    if zip_code is not None:
        if not isinstance(zip_code, str):
            reasons.append(Reason.INVALID_ZIP_CODE)
        else:
            normalized = zip_code.strip()
            if normalized.upper() not in ZIP_MISSING_MARKERS:
                valid_format = len(normalized) == FIVE_DIGITS and normalized.isdigit()
                if not valid_format or normalized == ZIP_SENTINEL_INVALID:
                    reasons.append(Reason.INVALID_ZIP_CODE)

    return reasons


def _fingerprint(record: Mapping[str, Any], settings: Settings) -> tuple[Any, ...]:
    fields = settings.source_field_map
    values = []
    for canonical in CONTENT_FINGERPRINT_FIELDS:
        value = record.get(fields[canonical])
        values.append(value.strip() if isinstance(value, str) else value)
    return tuple(values)


class RecordGate:
    """Contract + logical + duplicate rules over one deterministic stream.

    State advances for EVERY record seen (even ones quarantined for other
    reasons), so verdicts depend only on stream position — identical across
    runs (fetch order is unique_key order).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._seen_keys: set[Any] = set()
        self._seen_content: set[tuple[Any, ...]] = set()

    def check(self, record: Mapping[str, Any]) -> list[str]:
        reasons = validate_record(record, self._settings)

        unique_key = record.get(self._settings.source_field_map["unique_key"])
        if unique_key is not None and unique_key != "":
            if unique_key in self._seen_keys:
                reasons.append(Reason.DUPLICATE_KEY)
            self._seen_keys.add(unique_key)

        content = _fingerprint(record, self._settings)
        if content in self._seen_content:
            reasons.append(Reason.DUPLICATE_CONTENT)
        self._seen_content.add(content)

        return reasons

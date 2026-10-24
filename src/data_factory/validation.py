"""Record-level contract gate (AGENTS.md section 4.3).

``validate_record`` checks one RAW record against the pre-clean rules of
``contract.COLUMNS`` and returns every reason code it violates — all of them,
not just the first, so one quarantined record tells the whole story.

Decisions worth defending in the report (flagged in AGENTS terms):

* ``N/A`` and blank zip codes are MISSINGNESS MARKERS, not violations — they
  become null in cleaning (step 6, per-column missing policy).
* The sentinel ``00000`` zip IS a violation (``INVALID_ZIP_CODE``): the value
  exists and is outside the domain; converting garbage to null would hide it
  from the factory's discard report.
* ``closed_date`` may be absent (nullable); a present-but-unparseable value is
  a violation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from data_factory.config import Settings

ZIP_MISSING_MARKERS = frozenset({"", "N/A"})
ZIP_SENTINEL_INVALID = "00000"
FIVE_DIGITS = 5


class Reason:
    """Reason codes for quarantined records (stable, English, contract keys)."""

    MISSING_UNIQUE_KEY = "MISSING_UNIQUE_KEY"
    INVALID_UNIQUE_KEY = "INVALID_UNIQUE_KEY"
    MISSING_CREATED_DATE = "MISSING_CREATED_DATE"
    INVALID_CREATED_DATE = "INVALID_CREATED_DATE"
    INVALID_CLOSED_DATE = "INVALID_CLOSED_DATE"
    MISSING_AGENCY = "MISSING_AGENCY"
    OUT_OF_SCOPE_AGENCY = "OUT_OF_SCOPE_AGENCY"
    MISSING_COMPLAINT_TYPE = "MISSING_COMPLAINT_TYPE"
    OUT_OF_SCOPE_COMPLAINT_TYPE = "OUT_OF_SCOPE_COMPLAINT_TYPE"
    INVALID_ZIP_CODE = "INVALID_ZIP_CODE"


REASONS: tuple[str, ...] = (
    Reason.MISSING_UNIQUE_KEY,
    Reason.INVALID_UNIQUE_KEY,
    Reason.MISSING_CREATED_DATE,
    Reason.INVALID_CREATED_DATE,
    Reason.INVALID_CLOSED_DATE,
    Reason.MISSING_AGENCY,
    Reason.OUT_OF_SCOPE_AGENCY,
    Reason.MISSING_COMPLAINT_TYPE,
    Reason.OUT_OF_SCOPE_COMPLAINT_TYPE,
    Reason.INVALID_ZIP_CODE,
)


def _is_iso_timestamp(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_record(record: Mapping[str, Any], settings: Settings) -> list[str]:
    """Return every contract-reason code this raw record violates (``[]`` = pass).

    Reason order is fixed (not input-dependent) so outputs stay deterministic.
    """
    reasons: list[str] = []

    unique_key = record.get("unique_key")
    if unique_key is None or unique_key == "":
        reasons.append(Reason.MISSING_UNIQUE_KEY)
    elif not (isinstance(unique_key, str) and unique_key.isdigit()):
        reasons.append(Reason.INVALID_UNIQUE_KEY)

    created = record.get("created_date")
    if created is None or created == "":
        reasons.append(Reason.MISSING_CREATED_DATE)
    elif not isinstance(created, str) or not _is_iso_timestamp(created):
        reasons.append(Reason.INVALID_CREATED_DATE)

    closed = record.get("closed_date")
    if closed is not None and closed != "":
        if not isinstance(closed, str) or not _is_iso_timestamp(closed):
            reasons.append(Reason.INVALID_CLOSED_DATE)

    agency = record.get("agency")
    if agency is None or agency == "":
        reasons.append(Reason.MISSING_AGENCY)
    elif agency != settings.scope_agency:
        reasons.append(Reason.OUT_OF_SCOPE_AGENCY)

    complaint_type = record.get("complaint_type")
    if complaint_type is None or complaint_type == "":
        reasons.append(Reason.MISSING_COMPLAINT_TYPE)
    elif complaint_type not in settings.scope_complaint_types:
        reasons.append(Reason.OUT_OF_SCOPE_COMPLAINT_TYPE)

    zip_code = record.get("zip_code")
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

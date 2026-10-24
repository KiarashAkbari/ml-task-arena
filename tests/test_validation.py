"""Tests for the record-level contract gate (AGENTS.md section 4.3).

The challenge's acceptance test for this logic: a violating record must be
quarantined, a healthy one must NOT.
"""

from __future__ import annotations

import unittest

from data_factory.config import load_config
from data_factory.validation import REASONS, Reason, validate_record


def healthy_record() -> dict:
    return {
        "unique_key": "62024444",
        "created_date": "2023-05-01T08:30:00.000",
        "closed_date": "2023-05-03T14:00:00.000",
        "agency": "NYPD",
        "complaint_type": "Noise - Residential",
        "borough": "Queens",
        "zip_code": "11101",
        "status": "Closed",
    }


class ValidateRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_config()

    def test_healthy_record_passes_with_no_reasons(self) -> None:
        """4.6: the healthy record must NOT be quarantined."""
        self.assertEqual(validate_record(healthy_record(), self.settings), [])

    def test_each_violation_yields_exactly_its_reason(self) -> None:
        cases: list[tuple[str, dict, str]] = [
            ("missing unique_key", {"unique_key": None}, Reason.MISSING_UNIQUE_KEY),
            ("empty unique_key", {"unique_key": ""}, Reason.MISSING_UNIQUE_KEY),
            ("non-string unique_key", {"unique_key": 42}, Reason.INVALID_UNIQUE_KEY),
            ("non-digit unique_key", {"unique_key": "abc-1"}, Reason.INVALID_UNIQUE_KEY),
            ("missing created_date", {"created_date": None}, Reason.MISSING_CREATED_DATE),
            ("unparseable created_date", {"created_date": "not a date"}, Reason.INVALID_CREATED_DATE),
            ("non-string created_date", {"created_date": 123}, Reason.INVALID_CREATED_DATE),
            ("unparseable closed_date", {"closed_date": "01/05/2023"}, Reason.INVALID_CLOSED_DATE),
            ("missing agency", {"agency": None}, Reason.MISSING_AGENCY),
            ("foreign agency", {"agency": "FDNY"}, Reason.OUT_OF_SCOPE_AGENCY),
            ("missing complaint_type", {"complaint_type": ""}, Reason.MISSING_COMPLAINT_TYPE),
            ("out-of-scope complaint_type", {"complaint_type": "Pothole"}, Reason.OUT_OF_SCOPE_COMPLAINT_TYPE),
            ("sentinel zip 00000", {"zip_code": "00000"}, Reason.INVALID_ZIP_CODE),
            ("short zip", {"zip_code": "1001"}, Reason.INVALID_ZIP_CODE),
            ("alpha zip", {"zip_code": "ABCDE"}, Reason.INVALID_ZIP_CODE),
            ("non-string zip", {"zip_code": 10001}, Reason.INVALID_ZIP_CODE),
        ]
        for label, override, expected in cases:
            record = {**healthy_record(), **override}
            with self.subTest(label=label):
                self.assertEqual(validate_record(record, self.settings), [expected])

    def test_absent_or_marker_zips_are_missing_not_violations(self) -> None:
        """N/A and blank = missingness markers -> cleaning policy (step 6)."""
        for zip_value in (None, "", "N/A", "  N/A  ", "   "):
            record = {**healthy_record(), "zip_code": zip_value}
            with self.subTest(zip_value=zip_value):
                self.assertEqual(validate_record(record, self.settings), [])

    def test_padded_valid_zip_is_normalized_and_passes(self) -> None:
        record = {**healthy_record(), "zip_code": " 10001 "}
        self.assertEqual(validate_record(record, self.settings), [])

    def test_absent_closed_date_is_allowed_by_contract(self) -> None:
        record = {**healthy_record(), "closed_date": None}
        self.assertEqual(validate_record(record, self.settings), [])

    def test_multiple_violations_are_all_reported_in_stable_order(self) -> None:
        record = {
            "unique_key": None,
            "created_date": "garbage",
            "agency": "FDNY",
            "complaint_type": "Pothole",
            "zip_code": "00000",
        }
        reasons = validate_record(record, self.settings)
        self.assertEqual(
            reasons,
            [
                Reason.MISSING_UNIQUE_KEY,
                Reason.INVALID_CREATED_DATE,
                Reason.OUT_OF_SCOPE_AGENCY,
                Reason.OUT_OF_SCOPE_COMPLAINT_TYPE,
                Reason.INVALID_ZIP_CODE,
            ],
        )
        # Reported reason codes are all in the published registry.
        for code in reasons:
            self.assertIn(code, REASONS)


if __name__ == "__main__":
    unittest.main()

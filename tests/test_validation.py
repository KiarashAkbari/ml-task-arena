"""Tests for the record-level gate: contract, logical validity, duplicates."""

from __future__ import annotations

import dataclasses
import unittest

from data_factory.config import Settings, load_config
from data_factory.validation import (
    REASONS,
    Reason,
    RecordGate,
    validate_record,
)


def healthy_record() -> dict:
    """Raw-shaped record (zip lives under 'incident_zip' via source.field_map)."""
    return {
        "unique_key": "62024444",
        "created_date": "2023-05-01T08:30:00.000",
        "closed_date": "2023-05-03T14:00:00.000",
        "agency": "NYPD",
        "complaint_type": "Noise - Residential",
        "borough": "Queens",
        "incident_zip": "11101",
        "status": "Closed",
        "incident_address": "123 MAIN ST",
        "descriptor": "Loud Music/Party",
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
            ("sentinel zip 00000", {"incident_zip": "00000"}, Reason.INVALID_ZIP_CODE),
            ("short zip", {"incident_zip": "1001"}, Reason.INVALID_ZIP_CODE),
            ("alpha zip", {"incident_zip": "ABCDE"}, Reason.INVALID_ZIP_CODE),
            ("non-string zip", {"incident_zip": 10001}, Reason.INVALID_ZIP_CODE),
        ]
        for label, override, expected in cases:
            record = {**healthy_record(), **override}
            with self.subTest(label=label):
                self.assertEqual(validate_record(record, self.settings), [expected])

    def test_closed_before_created_quarantined_with_own_reason(self) -> None:
        """4.4: logically invalid data (closed before opened) is identified."""
        record = {
            **healthy_record(),
            "created_date": "2023-05-03T14:00:00.000",
            "closed_date": "2023-05-01T08:30:00.000",
        }
        self.assertEqual(
            validate_record(record, self.settings), [Reason.CLOSED_BEFORE_CREATED]
        )

    def test_equal_timestamps_are_valid(self) -> None:
        record = {**healthy_record(), "closed_date": "2023-05-01T08:30:00.000"}
        self.assertEqual(validate_record(record, self.settings), [])

    def test_logical_rule_only_runs_when_both_dates_parse(self) -> None:
        record = {
            **healthy_record(),
            "closed_date": "garbage",  # invalid -> only INVALID, no logical claim
        }
        self.assertEqual(
            validate_record(record, self.settings), [Reason.INVALID_CLOSED_DATE]
        )

    def test_tz_aware_comparison_uses_wall_clock(self) -> None:
        """Flagged decision: timestamps are compared as naive wall-clock."""
        record = {
            **healthy_record(),
            "created_date": "2023-05-01T10:00:00+02:00",
            "closed_date": "2023-05-01T09:00:00",
        }
        self.assertEqual(
            validate_record(record, self.settings), [Reason.CLOSED_BEFORE_CREATED]
        )

    def test_absent_or_marker_zips_are_missing_not_violations(self) -> None:
        """N/A and blank = missingness markers -> cleaning policy (step 6)."""
        for zip_value in (None, "", "N/A", "  N/A  ", "   "):
            record = {**healthy_record(), "incident_zip": zip_value}
            with self.subTest(zip_value=zip_value):
                self.assertEqual(validate_record(record, self.settings), [])

    def test_padded_valid_zip_is_normalized_and_passes(self) -> None:
        record = {**healthy_record(), "incident_zip": " 10001 "}
        self.assertEqual(validate_record(record, self.settings), [])

    def test_absent_closed_date_is_allowed_by_contract(self) -> None:
        record = {**healthy_record(), "closed_date": None}
        self.assertEqual(validate_record(record, self.settings), [])

    def test_raw_field_names_come_from_configured_field_map(self) -> None:
        """A source rename is a config edit, not a code change (section 1)."""
        remapped = dataclasses.replace(
            self.settings,
            source_field_map={**self.settings.source_field_map, "zip_code": "postal"},
        )
        record = {**healthy_record(), "postal": "00000"}
        del record["incident_zip"]
        self.assertEqual(validate_record(record, remapped), [Reason.INVALID_ZIP_CODE])
        # Under the original map, 'postal' is not consulted at all.
        self.assertEqual(validate_record(record, self.settings), [])

    def test_multiple_violations_are_all_reported_in_stable_order(self) -> None:
        record = {
            "unique_key": None,
            "created_date": "garbage",
            "agency": "FDNY",
            "complaint_type": "Pothole",
            "incident_zip": "00000",
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
        for code in reasons:
            self.assertIn(code, REASONS)


class RecordGateTests(unittest.TestCase):
    """Duplicate rules (4.4): definitions written down -> pinned by tests."""

    def setUp(self) -> None:
        self.settings = load_config()
        self.gate = RecordGate(self.settings)

    def test_same_unique_key_twice_quarantines_only_the_later(self) -> None:
        first = healthy_record()
        second = {**healthy_record(), "incident_address": "456 OTHER ST"}
        self.assertEqual(self.gate.check(first), [])
        self.assertEqual(self.gate.check(second), [Reason.DUPLICATE_KEY])

    def test_same_key_and_same_content_reports_both_reasons(self) -> None:
        self.gate.check(healthy_record())
        clone = healthy_record()
        self.assertEqual(
            self.gate.check(clone),
            [Reason.DUPLICATE_KEY, Reason.DUPLICATE_CONTENT],
        )

    def test_identical_content_under_new_key_is_a_duplicate(self) -> None:
        first = healthy_record()
        second = {**healthy_record(), "unique_key": "62024445"}
        self.assertEqual(self.gate.check(first), [])
        self.assertEqual(self.gate.check(second), [Reason.DUPLICATE_CONTENT])

    def test_one_differing_fingerprint_field_makes_records_distinct(self) -> None:
        first = healthy_record()
        second = {
            **healthy_record(),
            "unique_key": "62024445",
            "created_date": "2023-05-01T08:31:00.000",  # one second later = new case
        }
        self.assertEqual(self.gate.check(first), [])
        self.assertEqual(self.gate.check(second), [])

    def test_state_includes_records_quarantined_for_other_reasons(self) -> None:
        first = healthy_record()
        quarantined = {**healthy_record(), "unique_key": "62024445", "agency": None}
        third = {**healthy_record(), "unique_key": "62024446"}
        self.assertEqual(self.gate.check(first), [])
        self.assertEqual(
            self.gate.check(quarantined),
            [Reason.MISSING_AGENCY, Reason.DUPLICATE_CONTENT],
        )
        self.assertEqual(self.gate.check(third), [Reason.DUPLICATE_CONTENT])

    def test_contract_reasons_come_before_duplicate_reasons(self) -> None:
        self.gate.check(healthy_record())
        offender = {**healthy_record(), "incident_zip": "00000"}  # same key + bad zip
        self.assertEqual(
            self.gate.check(offender),
            [Reason.INVALID_ZIP_CODE, Reason.DUPLICATE_KEY],
        )

    def test_gate_decisions_are_order_deterministic(self) -> None:
        records = [
            healthy_record(),
            {**healthy_record(), "unique_key": "62024445"},
            healthy_record(),
        ]
        gate_a, gate_b = RecordGate(self.settings), RecordGate(self.settings)
        run_a = [gate_a.check(record) for record in records]
        run_b = [gate_b.check(record) for record in records]
        self.assertEqual(run_a, run_b)
        self.assertEqual(run_b[2], [Reason.DUPLICATE_KEY, Reason.DUPLICATE_CONTENT])


if __name__ == "__main__":
    unittest.main()

"""Tests for the JSON configuration layer (AGENTS.md section 4.1)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from data_factory.config import DEFAULT_CONFIG_PATH, load_config
from data_factory.errors import ConfigError

VALID_CONFIG = {
    "source": {"url": "https://data.cityofnewyork.us/resource/erm2-nwe9.json"},
    "scope": {"agency": "NYPD", "complaint_types": ["Noise - Residential"]},
    "date_range": {"start": "2023-01-01", "end": "2024-06-30"},
    "fetch": {
        "batch_size": 1000,
        "max_retries": 5,
        "retry_backoff_seconds": 1.0,
        "timeout_seconds": 30.0,
    },
    "paths": {"raw_dir": "data/raw", "curated_dir": "data/curated"},
    "quarantine": {"max_ratio": 0.05},
}


class LoadConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def _write(self, data: object | str) -> Path:
        path = self.tmp_path / "config.json"
        text = data if isinstance(data, str) else json.dumps(data)
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_file_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.tmp_path / "does-not-exist.json")

    def test_invalid_json_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self._write("{not json"))

    def test_non_object_json_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self._write("[1, 2, 3]"))

    def test_missing_or_malformed_sections_rejected(self) -> None:
        cases: list[dict] = []
        no_fetch = dict(VALID_CONFIG)
        del no_fetch["fetch"]
        cases.append(no_fetch)
        wrong_type = dict(VALID_CONFIG)
        wrong_type["fetch"] = [1, 2]
        cases.append(wrong_type)
        for case in cases:
            with self.subTest(config=case), self.assertRaises(ConfigError):
                load_config(self._write(case))

    def test_invalid_values_rejected(self) -> None:
        invalid: list[tuple[str, object]] = [
            ("batch_size", "1000"),      # wrong type
            ("batch_size", True),        # bool is not an int here
            ("batch_size", 0),           # below minimum
            ("batch_size", -5),
            ("max_retries", -1),
            ("max_retries", "5"),
            ("retry_backoff_seconds", -0.5),
            ("retry_backoff_seconds", "1"),
            ("timeout_seconds", 0),      # must be strictly positive
            ("timeout_seconds", -1.0),
            ("start", "01/01/2023"),     # not ISO
            ("start", 20230101),         # not a string
            ("max_ratio", 1.5),          # outside [0, 1]
            ("max_ratio", -0.1),
            ("raw_dir", ""),             # empty path
            ("raw_dir", 42),
        ]
        for key, value in invalid:
            config = json.loads(json.dumps(VALID_CONFIG))
            if key in config.get("fetch", {}):
                config["fetch"][key] = value
            elif key in ("start", "end"):
                config["date_range"][key] = value
            elif key == "max_ratio":
                config["quarantine"][key] = value
            else:
                config["paths"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                load_config(self._write(config))

    def test_end_before_start_rejected(self) -> None:
        config = json.loads(json.dumps(VALID_CONFIG))
        config["date_range"] = {"start": "2024-06-30", "end": "2023-01-01"}
        with self.assertRaises(ConfigError):
            load_config(self._write(config))

    def test_unknown_keys_rejected(self) -> None:
        config = json.loads(json.dumps(VALID_CONFIG))
        config["qurantine"] = {"max_ratio": 0.05}  # deliberate typo
        with self.assertRaises(ConfigError):
            load_config(self._write(config))

    def test_invalid_source_and_scope_rejected(self) -> None:
        invalid = [
            {"source": None},                                  # section missing
            {"source": {"url": "example.com"}},                # no scheme
            {"source": {"url": "ftp://example.test/x"}},       # wrong scheme
            {"source": {"url": ""}},                           # empty
            {"scope": None},                                   # section missing
            {"scope": {"agency": "", "complaint_types": ["x"]}},  # empty agency
            {"scope": {"agency": 42, "complaint_types": ["x"]}},  # wrong type
            {"scope": {"agency": "NYPD", "complaint_types": []}},  # empty list
            {"scope": {"agency": "NYPD", "complaint_types": [42]}},  # non-string
            {"scope": {"agency": "NYPD", "complaint_types": ["x", "x"]}},  # duplicate
            {"scope": {"agency": "NYPD", "complaint_types": ["ok", "  "]}},  # blank
        ]
        for change in invalid:
            section, replacement = next(iter(change.items()))
            config = json.loads(json.dumps(VALID_CONFIG))
            if replacement is None:
                del config[section]
            else:
                config[section] = replacement
            with self.subTest(change=change), self.assertRaises(ConfigError):
                load_config(self._write(config))

    def test_scope_values_come_from_file(self) -> None:
        config = json.loads(json.dumps(VALID_CONFIG))
        config["scope"] = {
            "agency": "DSNY",
            "complaint_types": ["Dirty Condition", "Missed Collection"],
        }
        settings = load_config(self._write(config))
        self.assertEqual(settings.scope_agency, "DSNY")
        self.assertEqual(
            settings.scope_complaint_types, ("Dirty Condition", "Missed Collection")
        )

    def test_values_come_from_file_not_hardcoded(self) -> None:
        config = json.loads(json.dumps(VALID_CONFIG))
        config["fetch"]["batch_size"] = 123
        settings = load_config(self._write(config))
        self.assertEqual(settings.batch_size, 123)
        self.assertEqual(settings.start_date.isoformat(), "2023-01-01")

    def test_committed_default_config_is_valid(self) -> None:
        """The config shipped in the repo must always pass validation."""
        settings = load_config(DEFAULT_CONFIG_PATH)
        self.assertLessEqual(settings.start_date, settings.end_date)
        self.assertGreaterEqual(settings.batch_size, 1)


if __name__ == "__main__":
    unittest.main()

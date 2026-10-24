"""Tests for the HTTP layer's defined failure behavior (AGENTS.md section 4.2).

Everything runs against mocked sessions — no network, no sleeping.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from data_factory.config import load_config
from data_factory.errors import NetworkError
from data_factory.http_client import HttpClient, RetryPolicy

POLICY = RetryPolicy(timeout_seconds=30.0, max_retries=2, retry_backoff_seconds=1.0)


def _response(status_code: int, payload: object | None = None, *, invalid_json: bool = False):
    response = mock.Mock()
    response.status_code = status_code
    if invalid_json:
        response.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
    else:
        response.json.return_value = payload
    return response


class HttpClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = mock.Mock()
        self.sleep = mock.Mock()
        self.client = HttpClient(POLICY, session=self.session, sleep=self.sleep)

    # --- success paths -----------------------------------------------------

    def test_get_success_returns_parsed_json(self) -> None:
        self.session.request.return_value = _response(200, [{"unique_key": "1"}])
        payload = self.client.get("https://example.test/resource", params={"$limit": 2})
        self.assertEqual(payload, [{"unique_key": "1"}])
        method, url = self.session.request.call_args.args
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://example.test/resource")
        self.sleep.assert_not_called()

    def test_post_is_proven_by_test_sends_post_with_body(self) -> None:
        """AGENTS.md 4.2: POST support must be proven by a test even though
        the current source does not need it."""
        self.session.request.return_value = _response(200, {"ok": True})
        body = {"query": "select 1"}
        payload = self.client.post("https://example.test/query", json=body)
        self.assertEqual(payload, {"ok": True})
        call = self.session.request.call_args
        self.assertEqual(call.args[0], "POST")
        self.assertEqual(call.kwargs["json"], body)

    def test_request_timeout_comes_from_policy(self) -> None:
        self.session.request.return_value = _response(200, [])
        self.client.get("https://example.test/resource")
        self.assertEqual(self.session.request.call_args.kwargs["timeout"], 30.0)

    # --- retry paths -------------------------------------------------------

    def test_retryable_status_is_retried_then_succeeds(self) -> None:
        self.session.request.side_effect = [
            _response(503),
            _response(200, ["data"]),
        ]
        payload = self.client.get("https://example.test/resource")
        self.assertEqual(payload, ["data"])
        self.assertEqual(self.session.request.call_count, 2)
        self.sleep.assert_called_once_with(1.0)  # first backoff delay

    def test_connection_error_is_retried_then_succeeds(self) -> None:
        self.session.request.side_effect = [
            requests.ConnectionError("boom"),
            _response(200, {"rows": []}),
        ]
        payload = self.client.get("https://example.test/resource")
        self.assertEqual(payload, {"rows": []})
        self.assertEqual(self.session.request.call_count, 2)
        # Every real HTTP attempt (including failed retries) is counted for
        # the request-budget report (AGENTS.md section 4.8.3).
        self.assertEqual(self.client.request_count, 2)

    def test_timeout_is_retried_then_fails_with_network_error(self) -> None:
        self.session.request.side_effect = requests.Timeout("too slow")
        with self.assertRaises(NetworkError) as ctx:
            self.client.get("https://example.test/resource")
        self.assertIn("Timeout", str(ctx.exception))
        # 1 initial attempt + max_retries(2) = 3 total, 2 sleeps with exponential backoff
        self.assertEqual(self.session.request.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in self.sleep.call_args_list],
            [1.0, 2.0],
        )

    def test_non_json_body_is_retried_then_fails_with_network_error(self) -> None:
        self.session.request.return_value = _response(200, invalid_json=True)
        with self.assertRaises(NetworkError) as ctx:
            self.client.get("https://example.test/resource")
        self.assertIn("not valid JSON", str(ctx.exception))
        self.assertEqual(self.session.request.call_count, 3)

    def test_exhausted_retries_report_attempt_count_and_last_status(self) -> None:
        self.session.request.return_value = _response(500)
        with self.assertRaises(NetworkError) as ctx:
            self.client.get("https://example.test/resource")
        message = str(ctx.exception)
        self.assertIn("3 attempt(s)", message)
        self.assertIn("HTTP 500", message)

    # --- non-retryable paths -----------------------------------------------

    def test_client_error_4xx_fails_immediately_without_retry(self) -> None:
        self.session.request.return_value = _response(404)
        with self.assertRaises(NetworkError) as ctx:
            self.client.get("https://example.test/resource")
        self.assertIn("HTTP 404", str(ctx.exception))
        self.assertIn("not retryable", str(ctx.exception))
        self.assertEqual(self.session.request.call_count, 1)
        self.sleep.assert_not_called()

    def test_scalar_json_body_is_rejected_as_invalid(self) -> None:
        """A 200 whose body cannot carry records is defined as invalid."""
        self.session.request.return_value = _response(200, "just-a-string")
        with self.assertRaises(NetworkError):
            self.client.get("https://example.test/resource")
        self.assertEqual(self.session.request.call_count, 3)


class RetryPolicyTests(unittest.TestCase):
    def test_policy_is_resolved_from_config_file_values(self) -> None:
        """Backoff and timeout must come from config, never from code."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "source": {"url": "https://data.cityofnewyork.us/resource/erm2-nwe9.json"},
                        "scope": {"agency": "NYPD", "complaint_types": ["Noise - Park"]},
                        "date_range": {"start": "2023-01-01", "end": "2024-06-30"},
                        "fetch": {
                            "batch_size": 100,
                            "max_retries": 7,
                            "retry_backoff_seconds": 2.5,
                            "timeout_seconds": 12.5,
                        },
                        "paths": {"raw_dir": "data/raw", "curated_dir": "data/curated"},
                        "quarantine": {"max_ratio": 0.05},
                    }
                ),
                encoding="utf-8",
            )
            policy = RetryPolicy.from_settings(load_config(config_path))
        self.assertEqual(policy.max_retries, 7)
        self.assertEqual(policy.retry_backoff_seconds, 2.5)
        self.assertEqual(policy.timeout_seconds, 12.5)

    def test_backoff_delay_scales_with_configured_base(self) -> None:
        session = mock.Mock()
        session.request.side_effect = [
            _response(503),
            _response(503),
            _response(200, [1]),
        ]
        sleep = mock.Mock()
        policy = RetryPolicy(timeout_seconds=1.0, max_retries=2, retry_backoff_seconds=3.0)
        client = HttpClient(policy, session=session, sleep=sleep)
        self.assertEqual(client.get("https://example.test/resource"), [1])
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [3.0, 6.0],
        )


if __name__ == "__main__":
    unittest.main()

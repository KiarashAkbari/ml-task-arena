"""HTTP layer with defined behavior for every failure mode (AGENTS.md 4.2).

Supports GET and POST with bounded retries and exponential backoff. The
behavior matrix is explicit, never accidental:

======================================= ===================================
server/client outcome                   result
======================================= ===================================
2xx with a valid JSON body              parsed JSON payload
429, 500, 502, 503, 504                 retry, then :class:`NetworkError`
connection error / timeout              retry, then :class:`NetworkError`
2xx with a non-JSON body                retry, then :class:`NetworkError`
any other non-2xx                       :class:`NetworkError` immediately
                                        (no retry: repeating a client-side
                                        error cannot help)
======================================= ===================================

Total attempts = ``1 + max_retries``. Backoff delay before retry *i* (0-based)
is ``retry_backoff_seconds * 2**i``; both values come from config.

Validating the *shape* of the payload (expected keys, record structure) is
the job of the schema-contract layer, not the transport layer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from data_factory.config import Settings
from data_factory.errors import NetworkError

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
BACKOFF_FACTOR = 2
JSON_BODY_TYPES = (list, dict)


@dataclass(frozen=True)
class RetryPolicy:
    """Retry/timeout behavior, resolved from project configuration."""

    timeout_seconds: float
    max_retries: int
    retry_backoff_seconds: float

    @classmethod
    def from_settings(cls, settings: Settings) -> "RetryPolicy":
        return cls(
            timeout_seconds=settings.timeout_seconds,
            max_retries=settings.max_retries,
            retry_backoff_seconds=settings.retry_backoff_seconds,
        )


class HttpClient:
    """Thin GET/POST client around :mod:`requests` with a fixed failure policy."""

    def __init__(
        self,
        policy: RetryPolicy,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._policy = policy
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        # Total HTTP requests issued, including retries: the seed for the
        # request-budget report of AGENTS.md section 4.8 (item 3).
        self.request_count = 0

    def get(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        """Perform a GET request and return the parsed JSON body."""
        return self._request("GET", url, params=params)

    def post(self, url: str, *, json: Any = None) -> Any:
        """Perform a POST request and return the parsed JSON body."""
        return self._request("POST", url, json_body=json)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        attempts = self._policy.max_retries + 1
        last_failure = "no attempt was made"
        for attempt in range(attempts):
            if attempt:
                delay = self._policy.retry_backoff_seconds * BACKOFF_FACTOR ** (attempt - 1)
                self._sleep(delay)
            self.request_count += 1
            try:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    timeout=self._policy.timeout_seconds,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_failure = f"{type(exc).__name__}: {exc}"
                continue

            status = response.status_code
            if status in RETRYABLE_STATUS_CODES:
                last_failure = f"HTTP {status}"
                continue
            if not 200 <= status < 300:
                # Non-retryable non-2xx: fail immediately with a defined error.
                raise NetworkError(
                    f"{method} {url} failed with HTTP {status} (not retryable)"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                last_failure = f"response body is not valid JSON: {exc}"
                continue
            if not isinstance(payload, JSON_BODY_TYPES):
                last_failure = (
                    f"response JSON must be an object or array, got {type(payload).__name__}"
                )
                continue
            return payload

        raise NetworkError(
            f"{method} {url} failed after {attempts} attempt(s): {last_failure}"
        )

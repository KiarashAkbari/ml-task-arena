"""Batched, resumable, idempotent raw fetch (AGENTS.md section 4.2).

Behavior contract:

* **Batched** — pages of ``batch_size`` records ordered by ``unique_key`` so
  offset windows are stable (no row-shifting between requests; the configured
  window is historical, so no inserts can appear mid-run).
* **Resumable** — ``fetch_checkpoint.json`` is saved atomically after every
  completed batch; an interrupted run continues at ``next_offset``, never from
  zero. A failed invocation leaves no partial batch file (temp + replace).
* **Idempotent** — a completed checkpoint makes the next invocation a no-op
  (zero HTTP requests); batch files are canonical JSON (sorted keys, compact
  separators) so identical upstream data yields byte-identical files.
* **Bounded memory** — exactly one batch lives in memory at a time; the peak
  is measured into ``FetchResult.peak_in_memory_records`` (proof for the
  section 4.2 memory-cap requirement).
* **Window of record** — the checkpoint stores the exact date window the raw
  data on disk was fetched with (``window_start`` / ``window_end``) and how it
  was chosen (``window_mode``). The moving-window feature (section 4.8.1) and
  the run manifest (section 4.5: "the window received") both read it from
  here, so the manifest can never claim a window the data does not have.
* **Defined failures** — non-array payload / non-object rows / changed column
  set -> :class:`ValidationError`; HTTP failures propagate as
  :class:`NetworkError` with the checkpoint intact.

Assumption (flagged): Socrata emits every dataset column on every row
(including nulls), so the column set of any batch must equal the first
batch's. If a real run ever shows a spurious mismatch, the check should relax
to detecting added columns only.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from data_factory.config import Settings
from data_factory.errors import ValidationError
from data_factory.scope import build_where

ORDER_FIELD = "unique_key"
CHECKPOINT_NAME = "fetch_checkpoint.json"
BATCH_NAME_TEMPLATE = "batch_{index:06d}.json"

# How the window recorded in a checkpoint was chosen (section 4.8.1).
WINDOW_MODE_CONFIGURED = "configured"          # date_range from the config file
WINDOW_MODE_SINCE_LAST_RUN = "since_last_run"  # --since-last-run: last end + 1d .. today
WINDOW_MODES = (WINDOW_MODE_CONFIGURED, WINDOW_MODE_SINCE_LAST_RUN)

_CHECKPOINT_KEYS = (
    "fingerprint",
    "batches_written",
    "next_offset",
    "records_written",
    "http_requests",
    "columns",
    "complete",
    "window_start",
    "window_end",
    "window_mode",
)


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one fetch invocation (all counters are real, from the run)."""

    records: int          # total records now on disk (including resumed ones)
    batches: int          # batches written by THIS invocation
    http_requests: int    # HTTP requests made by THIS invocation
    resumed_records: int  # records already present when this invocation started
    peak_in_memory_records: int
    already_complete: bool


def compute_fingerprint(settings: Settings) -> str:
    """Digest of everything that determines raw-batch content and layout."""
    payload = json.dumps(
        {
            "source_url": settings.source_url,
            "where": build_where(settings),
            "batch_size": settings.batch_size,
            "order_field": ORDER_FIELD,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _dump_batch(records: list[dict[str, Any]]) -> str:
    return (
        json.dumps(records, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    )


def _save_checkpoint(path: Path, state: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(state, sort_keys=True, indent=1) + "\n")


def _load_checkpoint(path: Path, batch_size: int) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(
            f"fetch checkpoint '{path}' is unreadable ({exc}); "
            "remove the raw directory and re-run to start over"
        ) from exc
    if not isinstance(state, dict) or any(key not in state for key in _CHECKPOINT_KEYS):
        missing = sorted(
            set(_CHECKPOINT_KEYS) - set(state) if isinstance(state, dict) else _CHECKPOINT_KEYS
        )
        raise ValidationError(
            f"fetch checkpoint '{path}' is incomplete (missing {missing}); a "
            "checkpoint written by an older version of the factory cannot be "
            "resumed — remove the raw directory and re-run to start over"
        )
    integers = ("batches_written", "next_offset", "records_written", "http_requests",
                "peak_in_memory_records")
    for key in integers:
        value = state[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError(f"fetch checkpoint '{path}': '{key}' must be a non-negative integer")
    if not isinstance(state["fingerprint"], str) or not state["fingerprint"]:
        raise ValidationError(f"fetch checkpoint '{path}': 'fingerprint' must be a non-empty string")
    if not isinstance(state["columns"], list) or not all(
        isinstance(column, str) for column in state["columns"]
    ):
        raise ValidationError(f"fetch checkpoint '{path}': 'columns' must be a list of strings")
    if not isinstance(state["complete"], bool):
        raise ValidationError(f"fetch checkpoint '{path}': 'complete' must be a boolean")
    window: list[date] = []
    for key in ("window_start", "window_end"):
        value = state[key]
        if not isinstance(value, str):
            raise ValidationError(
                f"fetch checkpoint '{path}': '{key}' must be an ISO date string"
            )
        try:
            window.append(date.fromisoformat(value))
        except ValueError as exc:
            raise ValidationError(
                f"fetch checkpoint '{path}': '{key}' must be an ISO date string "
                f"(YYYY-MM-DD), got {value!r}"
            ) from exc
    if window[0] > window[1]:
        raise ValidationError(
            f"fetch checkpoint '{path}': window_start ({window[0]}) is after "
            f"window_end ({window[1]})"
        )
    if state["window_mode"] not in WINDOW_MODES:
        raise ValidationError(
            f"fetch checkpoint '{path}': 'window_mode' must be one of "
            f"{list(WINDOW_MODES)}, got {state['window_mode']!r}"
        )
    # Invariants that catch tampering / partial manual edits.
    if state["next_offset"] != state["records_written"]:
        raise ValidationError(
            f"fetch checkpoint '{path}': next_offset must equal records_written"
        )
    if not state["complete"] and state["next_offset"] % batch_size != 0:
        raise ValidationError(
            f"fetch checkpoint '{path}': partial state must sit on a batch boundary"
        )
    return state


def _verify_batch_files(raw_dir: Path, batches_written: int) -> None:
    for index in range(batches_written):
        path = raw_dir / BATCH_NAME_TEMPLATE.format(index=index)
        if not path.is_file():
            raise ValidationError(
                f"raw batch file '{path}' is missing although the checkpoint "
                f"claims {batches_written} batch(es); remove the raw directory "
                "and re-run to start over"
            )


def _validate_payload(payload: Any, offset: int) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValidationError(
            f"fetch expected a JSON array of records at offset {offset}, "
            f"got {type(payload).__name__}"
        )
    if not all(isinstance(row, dict) for row in payload):
        raise ValidationError(
            f"fetch expected object rows at offset {offset}"
        )
    return payload


def run_fetch(
    settings: Settings,
    client: Any,
    *,
    window_mode: str = WINDOW_MODE_CONFIGURED,
) -> FetchResult:
    """Fetch the configured scope into ``settings.raw_dir``, batch by batch.

    Args:
        settings: Resolved project configuration. Its ``start_date`` /
            ``end_date`` are the window of record for this fetch — the
            moving-window feature (section 4.8.1) hands ``run_fetch`` a
            settings object whose dates are already the resolved window.
        client: Any object with ``get(url, params=...)`` and ``request_count``
            (the real :class:`HttpClient` or a test double).
        window_mode: How the window was chosen — ``WINDOW_MODE_CONFIGURED``
            (default) or ``WINDOW_MODE_SINCE_LAST_RUN``. Recorded in the
            checkpoint when it is created and preserved across resumes, so the
            manifest reports the truth even when a later stage runs alone.

    Raises:
        NetworkError: HTTP failures (checkpoint stays resumable).
        ValidationError: Unusable payloads, changed source shape, or raw state
            that does not match the current configuration.
    """
    if window_mode not in WINDOW_MODES:
        raise ValidationError(
            f"window_mode must be one of {list(WINDOW_MODES)}, got {window_mode!r}"
        )
    where = build_where(settings)
    fingerprint = compute_fingerprint(settings)
    raw_dir = Path(settings.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = raw_dir / CHECKPOINT_NAME

    if checkpoint_path.exists():
        state = _load_checkpoint(checkpoint_path, settings.batch_size)
        if state["fingerprint"] != fingerprint:
            raise ValidationError(
                f"raw data in '{raw_dir}' was fetched with a different scope, "
                "source, or batch size than the current config; restore the old "
                "config or remove the raw directory to start over"
            )
        # Resuming keeps the window of record (and how it was chosen) from the
        # checkpoint: the raw data on disk belongs to that window, whatever the
        # current invocation asked for. A genuinely different window cannot
        # reach this point — its fingerprint differs and is rejected above.
        _verify_batch_files(raw_dir, state["batches_written"])
        if state["complete"]:
            return FetchResult(
                records=state["records_written"],
                batches=0,
                http_requests=0,
                resumed_records=state["records_written"],
                peak_in_memory_records=state["peak_in_memory_records"],
                already_complete=True,
            )
        resumed_records = state["records_written"]
    else:
        stray = sorted(raw_dir.glob("batch_*.json"))
        if stray:
            raise ValidationError(
                f"raw batch file '{stray[0]}' exists without a checkpoint; "
                "remove the raw directory and re-run to start over"
            )
        state = {
            "fingerprint": fingerprint,
            "batches_written": 0,
            "next_offset": 0,
            "records_written": 0,
            "http_requests": 0,
            "peak_in_memory_records": 0,
            "columns": [],
            "complete": False,
            "window_start": settings.start_date.isoformat(),
            "window_end": settings.end_date.isoformat(),
            "window_mode": window_mode,
        }
        resumed_records = 0

    initial_batches = state["batches_written"]
    prior_http_requests = state["http_requests"]
    peak_records = 0
    baseline_columns: set[str] | None = set(state["columns"]) or None

    while True:
        offset = state["next_offset"]
        try:
            payload = client.get(
                settings.source_url,
                params={
                    "$where": where,
                    "$order": ORDER_FIELD,
                    "$limit": settings.batch_size,
                    "$offset": offset,
                },
            )
        except BaseException:
            # Persist request accounting even when the attempt fails or is
            # interrupted, so the section 4.8.3 budget stays truthful.
            state["http_requests"] = prior_http_requests + client.request_count
            state["peak_in_memory_records"] = max(
                state["peak_in_memory_records"], peak_records
            )
            _save_checkpoint(checkpoint_path, state)
            raise
        rows = _validate_payload(payload, offset)
        if not rows:
            state["complete"] = True
            state["http_requests"] = prior_http_requests + client.request_count
            state["peak_in_memory_records"] = max(
                state["peak_in_memory_records"], peak_records
            )
            _save_checkpoint(checkpoint_path, state)
            break

        peak_records = max(peak_records, len(rows))

        columns = sorted({key for row in rows for key in row})
        if baseline_columns is None:
            baseline_columns = set(columns)
            state["columns"] = columns
        elif set(columns) != baseline_columns:
            missing = sorted(baseline_columns - set(columns))
            added = sorted(set(columns) - baseline_columns)
            raise ValidationError(
                "source shape changed during fetch "
                f"(offset {offset}): missing={missing}, added={added}"
            )

        batch_path = raw_dir / BATCH_NAME_TEMPLATE.format(index=state["batches_written"])
        _atomic_write_text(batch_path, _dump_batch(rows))

        state["batches_written"] += 1
        state["next_offset"] += len(rows)
        state["records_written"] += len(rows)
        state["http_requests"] = prior_http_requests + client.request_count
        state["peak_in_memory_records"] = max(
            state["peak_in_memory_records"], peak_records
        )
        if len(rows) < settings.batch_size:
            state["complete"] = True
        _save_checkpoint(checkpoint_path, state)
        if state["complete"]:
            break

    return FetchResult(
        records=state["records_written"],
        batches=state["batches_written"] - initial_batches,
        http_requests=client.request_count,
        resumed_records=resumed_records,
        peak_in_memory_records=peak_records,
        already_complete=False,
    )

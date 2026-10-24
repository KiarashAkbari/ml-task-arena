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
* **Defined failures** — non-array payload / non-object rows / a mapped field
  turning into a nested value -> :class:`ValidationError`; HTTP failures
  propagate as :class:`NetworkError` with the checkpoint intact.
* **Shape guard (section 4.8.2)** — two layers, because a page's key set is
  NOT a reliable schema signal: Socrata omits a field entirely when every row
  in that page has it null (a live run hit this at page 36: ``vehicle_type``
  appeared for the first time). So:

  1. *Authoritative, once per invocation*: fetch the dataset metadata
     (``/api/views/<id>.json``), require every mapped field to be declared,
     and compare the declared column types with the ones stored in the
     checkpoint. A removed column or a changed type fails the run loudly.
     The observed schema is stored in the checkpoint and reported in the
     manifest. When the metadata endpoint is unreachable — or the source is
     not a Socrata resource — the audit degrades to a recorded warning
     instead of breaking a resumable run.
  2. *Cheap, per page*: a mapped field must hold a scalar (or be absent); a
     dict/list means the field changed type and stops the run.

  Columns that merely APPEAR mid-run are normal (sparse fields); they are
  recorded in the checkpoint and reported, never treated as a failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from data_factory.config import Settings
from data_factory.errors import ValidationError
from data_factory.scope import build_where

ORDER_FIELD = "unique_key"

#: A long fetch prints nothing until it finishes, which makes "working",
#: "throttled" and "dead" indistinguishable from outside (a real run was
#: mistaken for stalled at 505,000 records). Progress is reported every
#: ``PROGRESS_EVERY_RECORDS`` records written — coarse enough to stay out of
#: the way, fine enough to show life within a minute or two.
PROGRESS_EVERY_RECORDS = 10_000
CHECKPOINT_NAME = "fetch_checkpoint.json"
BATCH_NAME_TEMPLATE = "batch_{index:06d}.json"

# Socrata resource URLs look like https://host/resource/<dataset-id>.json;
# the metadata (declared schema) lives at /api/views/<dataset-id>.json.
_SOCRATA_RESOURCE = re.compile(r"/resource/([A-Za-z0-9]{4}-[A-Za-z0-9]{4})(?:\.json)?/?$")

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
    schema_audit: str = ""            # "verified" / "unavailable: …" / "skipped: …"
    new_columns: tuple[str, ...] = ()  # columns that first appeared mid-run


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


def metadata_url(source_url: str) -> str | None:
    """Socrata metadata URL for a resource URL (``None`` when not Socrata)."""
    parsed = urlparse(source_url)
    match = _SOCRATA_RESOURCE.search(parsed.path)
    if match is None:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/api/views/{match.group(1)}.json"


def parse_source_schema(payload: Any) -> dict[str, str]:
    """``{field name: declared type}`` from a Socrata view-metadata payload.

    Uses ``fieldName`` (the API field, e.g. ``incident_zip``) over ``name``
    (the display label, e.g. ``Incident Zip``) — the factory maps raw API
    field names, not labels.

    Raises:
        ValueError: The payload is not a metadata document with columns.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"expected a metadata object, got {type(payload).__name__}")
    columns = payload.get("columns")
    if not isinstance(columns, list) or not columns:
        raise ValueError("metadata document declares no columns")
    schema: dict[str, str] = {}
    for entry in columns:
        if not isinstance(entry, dict):
            continue
        name = entry.get("fieldName") or entry.get("name")
        if isinstance(name, str) and name:
            schema[name] = str(entry.get("dataTypeName") or "unknown")
    if not schema:
        raise ValueError("metadata document declares no named columns")
    return schema


def audit_source_shape(
    settings: Settings,
    client: Any,
    *,
    previous: dict[str, str] | None,
) -> tuple[dict[str, str], str]:
    """Verify the source's declared schema (AGENTS.md section 4.8.2).

    Returns:
        ``(schema, status)`` where status is ``"verified"``, or a
        ``"skipped: …"`` / ``"unavailable: …"`` note when the audit could not
        run. A degraded audit never breaks a resumable fetch: the data-level
        guards still apply and the status is recorded for the manifest.

    Raises:
        ValidationError: The declared schema no longer contains a mapped
            field, or a declared type changed compared with the checkpoint —
            the two failures section 4.8.2 requires to stop the run.
    """
    url = metadata_url(settings.source_url)
    if url is None:
        return {}, "skipped: source URL is not a Socrata resource"
    try:
        payload = client.get(url)
        schema = parse_source_schema(payload)
    except Exception as exc:  # noqa: BLE001 - audit must not break the fetch
        return {}, f"unavailable: {type(exc).__name__}: {exc}"[:200]

    mapped = set(settings.source_field_map.values())
    missing = sorted(mapped - set(schema))
    if missing:
        raise ValidationError(
            "source schema audit: the dataset no longer declares mapped "
            f"field(s) {missing} ({len(schema)} columns declared); the source "
            "shape changed — fix source.field_map and the scope, or restore "
            "the old source"
        )

    if previous:
        removed = sorted(set(previous) - set(schema))
        retyped = sorted(
            name for name in set(previous) & set(schema)
            if previous[name] != schema[name]
        )
        if removed or retyped:
            details = []
            if removed:
                details.append(f"removed={removed}")
            if retyped:
                details.append(
                    "type changed="
                    + str([f"{n}: {previous[n]} -> {schema[n]}" for n in retyped])
                )
            raise ValidationError(
                "source schema audit: the dataset changed shape since this "
                f"window was fetched ({'; '.join(details)}); remove the raw "
                "directory to refetch, or restore the previous source"
            )
    return schema, "verified"


def _check_field_types(
    rows: list[dict[str, Any]], offset: int, mapped_fields: set[str]
) -> None:
    """Fail when a mapped field holds a nested value (a real type change).

    Absent and null values are fine: a page where every record has, say, no
    ``vehicle_type`` legitimately omits the field. Only the JSON shape of a
    present value is a signal.
    """
    for row in rows:
        for field in mapped_fields:
            if field not in row:
                continue
            value = row[field]
            if value is None or isinstance(value, str):
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                continue  # numbers instead of strings stay readable by the gate
            raise ValidationError(
                "source type changed during fetch "
                f"(offset {offset}): field '{field}' holds a "
                f"{type(value).__name__}, expected a scalar"
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
    on_progress: Any = None,
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
        on_progress: Optional ``callable(records, batches, http_requests)``
            invoked every ``PROGRESS_EVERY_RECORDS`` records. Reporting must
            never break a multi-hour fetch, so an exception from it (a closed
            terminal raises ``BrokenPipeError``) disables further reporting and
            the fetch continues.

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
                schema_audit=str(state.get("schema_audit", "")),
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
    new_columns: list[str] = []
    last_progress_mark = state["records_written"] // PROGRESS_EVERY_RECORDS

    # Shape guard layer 1 (section 4.8.2): the dataset's declared schema.
    # Runs only when there is work to do — a completed checkpoint stays a
    # zero-request no-op (section 4.2 idempotency).
    schema, audit_status = audit_source_shape(
        settings, client, previous=dict(state.get("source_schema") or {}) or None
    )
    if schema:
        state["source_schema"] = schema
    state["schema_audit"] = audit_status
    _save_checkpoint(checkpoint_path, state)

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
        try:
            rows = _validate_payload(payload, offset)
            if rows:
                _check_field_types(
                    rows, offset, set(settings.source_field_map.values())
                )
        except ValidationError:
            # The request succeeded, so count it before failing the run.
            state["http_requests"] = prior_http_requests + client.request_count
            state["peak_in_memory_records"] = max(
                state["peak_in_memory_records"], peak_records
            )
            _save_checkpoint(checkpoint_path, state)
            raise
        if not rows:
            state["complete"] = True
            state["http_requests"] = prior_http_requests + client.request_count
            state["peak_in_memory_records"] = max(
                state["peak_in_memory_records"], peak_records
            )
            _save_checkpoint(checkpoint_path, state)
            break

        peak_records = max(peak_records, len(rows))

        # Columns that appear mid-run are sparse fields (Socrata omits a
        # field when every value in that page is null), not a shape change:
        # record them, report them, never fail on them.
        observed = {key for row in rows for key in row}
        if state["columns"]:  # baseline already established by an earlier page
            added = sorted(observed - set(state["columns"]))
            if added:
                new_columns.extend(added)
            state["columns"] = sorted(set(state["columns"]) | observed)
        else:
            state["columns"] = sorted(observed)

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

        mark = state["records_written"] // PROGRESS_EVERY_RECORDS
        if on_progress is not None and mark > last_progress_mark:
            last_progress_mark = mark
            try:
                on_progress(
                    state["records_written"],
                    state["batches_written"],
                    state["http_requests"],
                )
            except Exception:  # noqa: BLE001 - never break the fetch for a log line
                on_progress = None
        if state["complete"]:
            break

    return FetchResult(
        records=state["records_written"],
        batches=state["batches_written"] - initial_batches,
        http_requests=client.request_count,
        resumed_records=resumed_records,
        peak_in_memory_records=peak_records,
        already_complete=False,
        schema_audit=audit_status,
        new_columns=tuple(sorted(set(new_columns))),
    )

"""Moving fetch window: "from the last run until now" (AGENTS.md 4.8.1).

One parameter — ``data-factory run --since-last-run`` — makes the factory
fetch only ``[previous window end + 1 day .. today]`` instead of always
re-fetching the whole configured range. Rules, all derived from artifacts on
disk (never guessed):

* **Previous window of record** — read from the fetch checkpoint first (that
  is the raw data actually present), else from the last ``run_manifest.json``.
  If neither exists the flag fails loudly: there is no "last run" to continue
  from, and inventing a window would silently change the dataset.
* **Rollover preserves evidence** — when raw data for a different window is
  on disk it is *renamed* into ``paths.raw_archive_dir`` as
  ``<start>__<end>/`` (batches + checkpoint), together with a copy of the
  manifest that described it. Nothing is deleted: the previous window stays
  reproducible, which is why the archive name is derived from that window.
* **Nothing new is a no-op, not an error** — if the previous window already
  ends today, the effective window *is* the previous one, so the run fetches
  zero requests and re-derives byte-identical outputs (idempotency, 4.2)
  instead of querying an empty range or failing.
* ``today`` is an injectable UTC date, so behavior never depends on the local
  clock or timezone and tests can pin it.

The effective window is returned as a :class:`~data_factory.config.Settings`
copy, which means every downstream stage — and the manifest's "window
received" — sees the same window the fetch actually used.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from data_factory.config import Settings
from data_factory.errors import ValidationError
from data_factory.fetch import CHECKPOINT_NAME, _load_checkpoint
from data_factory.manifest import MANIFEST_NAME

ARCHIVE_NAME_TEMPLATE = "{start}__{end}"  # ISO dates: sortable, filesystem-safe


def today_utc() -> date:
    """Current UTC date — the "until now" end of a moving window."""
    return datetime.now(timezone.utc).date()


@dataclass(frozen=True)
class WindowResolution:
    """What the moving window resolved to, and why."""

    start: date
    end: date
    previous: tuple[date, date] | None    # window of the last run, if known
    previous_source: str | None           # "checkpoint" | "manifest" | None
    already_current: bool                 # previous window already ends today
    rollover: bool                        # raw data on disk must be archived first


@dataclass(frozen=True)
class MovingWindowRun:
    """Result of applying the moving window: settings to run with + evidence."""

    settings: Settings            # effective settings (dates = resolved window)
    resolution: WindowResolution
    archived_to: Path | None      # where the previous raw window was renamed to


def _parse_window(value: object, context: str) -> tuple[date, date]:
    """Validate a ``{"start": ..., "end": ...}`` mapping into two dates."""
    if not isinstance(value, dict):
        raise ValidationError(
            f"{context}: expected an object with 'start' and 'end' dates, "
            f"got {type(value).__name__}"
        )
    parsed: list[date] = []
    for key in ("start", "end"):
        raw = value.get(key)
        if not isinstance(raw, str):
            raise ValidationError(f"{context}: '{key}' must be an ISO date string")
        try:
            parsed.append(date.fromisoformat(raw))
        except ValueError as exc:
            raise ValidationError(
                f"{context}: '{key}' must be an ISO date string (YYYY-MM-DD), got {raw!r}"
            ) from exc
    if parsed[0] > parsed[1]:
        raise ValidationError(
            f"{context}: start ({parsed[0]}) is after end ({parsed[1]})"
        )
    return parsed[0], parsed[1]


def read_previous_window(settings: Settings) -> tuple[tuple[date, date] | None, str | None]:
    """Locate the window of the last run: checkpoint first, manifest second.

    Returns:
        ``((start, end), source)`` where source is ``"checkpoint"`` (raw data
        on disk) or ``"manifest"`` (last completed run), or ``(None, None)``
        when the project has never run.

    Raises:
        ValidationError: A checkpoint or manifest exists but its window record
            is unreadable — a loud failure beats guessing a window.
    """
    checkpoint_path = Path(settings.raw_dir) / CHECKPOINT_NAME
    if checkpoint_path.is_file():
        state = _load_checkpoint(checkpoint_path, settings.batch_size)
        return (
            date.fromisoformat(state["window_start"]),
            date.fromisoformat(state["window_end"]),
        ), "checkpoint"

    manifest_path = Path(settings.curated_dir) / MANIFEST_NAME
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(
                f"run manifest '{manifest_path}' is unreadable ({exc}); cannot "
                "determine the last window — fix or remove the manifest"
            ) from exc
        if not isinstance(payload, dict) or "window" not in payload:
            raise ValidationError(
                f"run manifest '{manifest_path}' records no window; cannot "
                "continue from the last run"
            )
        return (
            _parse_window(payload["window"], f"run manifest '{manifest_path}' window"),
            "manifest",
        )

    return None, None


def resolve_moving_window(settings: Settings, *, today: date) -> WindowResolution:
    """Resolve the ``--since-last-run`` window without touching the filesystem.

    Args:
        settings: Configured settings (its dates are the fallback scope only).
        today: The UTC date to fetch up to (inclusive).

    Raises:
        ValidationError: No previous run to continue from, or an unreadable
            window record.
    """
    previous, source = read_previous_window(settings)
    if previous is None:
        raise ValidationError(
            "--since-last-run needs a previous run to continue from, but neither "
            f"'{Path(settings.raw_dir) / CHECKPOINT_NAME}' nor "
            f"'{Path(settings.curated_dir) / MANIFEST_NAME}' records a window; "
            "run the factory once without the flag first"
        )
    previous_start, previous_end = previous
    start = previous_end + timedelta(days=1)
    if start > today:
        # Nothing new since the last run: keep the previous window so the run
        # stays a zero-request, byte-identical no-op instead of an empty fetch.
        return WindowResolution(
            start=previous_start,
            end=previous_end,
            previous=previous,
            previous_source=source,
            already_current=True,
            rollover=False,
        )
    return WindowResolution(
        start=start,
        end=today,
        previous=previous,
        previous_source=source,
        already_current=False,
        # Raw data on disk belongs to the previous window -> archive it first.
        rollover=source == "checkpoint",
    )


def _archive_raw_dir(settings: Settings, resolution: WindowResolution) -> Path:
    """Rename the raw directory into the archive; copy its manifest alongside.

    Raises:
        ValidationError: The archive name is already taken — the factory never
            overwrites or merges two windows of evidence.
    """
    raw_dir = Path(settings.raw_dir)
    if resolution.previous is None:  # unreachable: rollover implies a known window
        raise ValidationError(f"cannot archive '{raw_dir}': the previous window is unknown")
    previous_start, previous_end = resolution.previous
    target = Path(settings.raw_archive_dir) / ARCHIVE_NAME_TEMPLATE.format(
        start=previous_start.isoformat(), end=previous_end.isoformat()
    )
    if target.exists():
        raise ValidationError(
            f"archive '{target}' already exists; move or remove it before rolling "
            "the fetch window over (raw data is never overwritten)"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    raw_dir.rename(target)
    manifest_path = Path(settings.curated_dir) / MANIFEST_NAME
    if manifest_path.is_file():
        # Evidence travels with the data it describes.
        shutil.copy2(manifest_path, target / MANIFEST_NAME)
    return target


def apply_moving_window(settings: Settings, *, today: date) -> MovingWindowRun:
    """Resolve the moving window, roll the raw directory over, return settings.

    This is the only place the ``--since-last-run`` flag changes state: after
    it returns, ``settings`` carries the resolved window and the raw directory
    is either the previous one (nothing new) or empty and ready for a fresh
    fetch.

    Raises:
        ValidationError: No previous run, unreadable window record, or an
            archive name already taken.
    """
    resolution = resolve_moving_window(settings, today=today)
    archived_to = _archive_raw_dir(settings, resolution) if resolution.rollover else None
    effective = dataclasses.replace(
        settings, start_date=resolution.start, end_date=resolution.end
    )
    return MovingWindowRun(
        settings=effective, resolution=resolution, archived_to=archived_to
    )

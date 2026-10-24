"""Project configuration: loaded from JSON files outside the code.

Every tunable named in AGENTS.md section 4.1 lives here — date range, batch
size, paths, retry cap — plus the quarantine-ratio threshold (section 4.3).
Loading failures raise :class:`ConfigError` so the CLI can exit with code 3.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from data_factory.errors import ConfigError

DEFAULT_CONFIG_PATH = Path("config/default.json")


@dataclass(frozen=True)
class Settings:
    """Resolved and validated project settings."""

    start_date: date
    end_date: date
    batch_size: int
    max_retries: int
    raw_dir: Path
    curated_dir: Path
    quarantine_max_ratio: float


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    if name not in data:
        raise ConfigError(f"config: missing required section '{name}'")
    value = data[name]
    if not isinstance(value, dict):
        raise ConfigError(f"config: section '{name}' must be a JSON object")
    return value


def _reject_unknown_keys(data: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(
            f"config: unknown key(s) in {context}: {', '.join(unknown)}"
        )


def _require(section: dict[str, Any], key: str, context: str) -> Any:
    if key not in section:
        raise ConfigError(f"config: missing required key '{context}.{key}'")
    return section[key]


def _parse_date(value: Any, context: str) -> date:
    if not isinstance(value, str):
        raise ConfigError(f"config: '{context}' must be an ISO date string (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(
            f"config: '{context}' must be an ISO date string (YYYY-MM-DD), got {value!r}"
        ) from exc


def _parse_int(value: Any, context: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"config: '{context}' must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"config: '{context}' must be >= {minimum}, got {value}")
    return value


def _parse_ratio(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"config: '{context}' must be a number, got {value!r}")
    ratio = float(value)
    if not 0.0 <= ratio <= 1.0:
        raise ConfigError(f"config: '{context}' must be within [0, 1], got {value}")
    return ratio


def _parse_path(value: Any, context: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"config: '{context}' must be a non-empty relative path string")
    return Path(value)


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Settings:
    """Read, validate, and resolve the project configuration.

    Args:
        path: Path to a JSON config file.

    Raises:
        ConfigError: If the file is missing, unreadable, or fails validation.
    """
    config_path = Path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file '{config_path}': {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file '{config_path}' is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file '{config_path}' must contain a JSON object")

    _reject_unknown_keys(
        data,
        {"date_range", "fetch", "paths", "quarantine"},
        "top level",
    )

    date_range = _section(data, "date_range")
    _reject_unknown_keys(date_range, {"start", "end"}, "date_range")
    start_date = _parse_date(_require(date_range, "start", "date_range"), "date_range.start")
    end_date = _parse_date(_require(date_range, "end", "date_range"), "date_range.end")
    # Duration policy is NOT enforced here: the challenge's 18-month window is a
    # scope/manifest concern (AGENTS.md section 3), and the moving-range feature
    # (section 4.8) legitimately produces shorter windows.
    if end_date < start_date:
        raise ConfigError(
            f"config: date_range.end ({end_date}) is before date_range.start ({start_date})"
        )

    fetch = _section(data, "fetch")
    _reject_unknown_keys(fetch, {"batch_size", "max_retries"}, "fetch")
    batch_size = _parse_int(_require(fetch, "batch_size", "fetch"), "fetch.batch_size", minimum=1)
    max_retries = _parse_int(_require(fetch, "max_retries", "fetch"), "fetch.max_retries", minimum=0)

    paths = _section(data, "paths")
    _reject_unknown_keys(paths, {"raw_dir", "curated_dir"}, "paths")
    raw_dir = _parse_path(_require(paths, "raw_dir", "paths"), "paths.raw_dir")
    curated_dir = _parse_path(_require(paths, "curated_dir", "paths"), "paths.curated_dir")

    quarantine = _section(data, "quarantine")
    _reject_unknown_keys(quarantine, {"max_ratio"}, "quarantine")
    quarantine_max_ratio = _parse_ratio(
        _require(quarantine, "max_ratio", "quarantine"), "quarantine.max_ratio"
    )

    return Settings(
        start_date=start_date,
        end_date=end_date,
        batch_size=batch_size,
        max_retries=max_retries,
        raw_dir=raw_dir,
        curated_dir=curated_dir,
        quarantine_max_ratio=quarantine_max_ratio,
    )

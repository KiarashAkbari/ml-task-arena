"""Project configuration: loaded from JSON files outside the code.

Every tunable named in AGENTS.md section 4.1 lives here — date range, batch
size, paths (including the raw-archive directory used by the moving-window
feature of section 4.8.1), retry cap — plus the quarantine-ratio threshold
(section 4.3) and the HTTP retry/timeout behavior of the fetch layer
(section 4.2). Loading failures raise :class:`ConfigError` so the CLI can
exit with code 3.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from data_factory.errors import ConfigError

DEFAULT_CONFIG_PATH = Path("config/default.json")

# Canonical fields the pipeline consumes -> must be present in source.field_map.
# The raw field names they map to are config, not code (AGENTS.md section 1:
# a new source must plug in without rewriting validation).
REQUIRED_FIELD_MAP_KEYS: tuple[str, ...] = (
    "unique_key",
    "created_date",
    "closed_date",
    "agency",
    "complaint_type",
    "borough",
    "zip_code",
    "status",
    "incident_address",
    "descriptor",
)


@dataclass(frozen=True)
class Settings:
    """Resolved and validated project settings."""

    source_url: str
    source_field_map: dict[str, str]
    population_path: Path
    population_source_url: str  # "" until the student fills in the chosen source
    population_fetched_date: date | None  # None until filled in
    scope_agency: str
    scope_complaint_types: tuple[str, ...]
    start_date: date
    end_date: date
    batch_size: int
    max_retries: int
    retry_backoff_seconds: float
    timeout_seconds: float
    raw_dir: Path
    curated_dir: Path
    raw_archive_dir: Path  # where a rolled-over raw window is renamed to (4.8.1)
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


def _parse_float(value: Any, context: str, *, minimum: float, strict: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"config: '{context}' must be a number, got {value!r}")
    number = float(value)
    invalid = number <= minimum if strict else number < minimum
    if invalid:
        operator = ">" if strict else ">="
        raise ConfigError(f"config: '{context}' must be {operator} {minimum}, got {value}")
    return number


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
        {"source", "population", "scope", "date_range", "fetch", "paths", "quarantine"},
        "top level",
    )

    source = _section(data, "source")
    _reject_unknown_keys(source, {"url", "field_map"}, "source")
    source_url = _require(source, "url", "source")
    if not isinstance(source_url, str) or not source_url.startswith(("http://", "https://")):
        raise ConfigError(f"config: 'source.url' must be an http(s) URL, got {source_url!r}")
    raw_field_map = _require(source, "field_map", "source")
    if not isinstance(raw_field_map, dict):
        raise ConfigError("config: 'source.field_map' must be a JSON object")
    field_map_keys = set(raw_field_map)
    required_keys = set(REQUIRED_FIELD_MAP_KEYS)
    if field_map_keys != required_keys:
        missing = sorted(required_keys - field_map_keys)
        unknown = sorted(field_map_keys - required_keys)
        details = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise ConfigError(f"config: 'source.field_map' must define exactly the required keys ({'; '.join(details)})")
    source_field_map: dict[str, str] = {}
    for canonical, raw_name in raw_field_map.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ConfigError(
                f"config: 'source.field_map.{canonical}' must be a non-empty string, got {raw_name!r}"
            )
        source_field_map[canonical] = raw_name

    population = _section(data, "population")
    _reject_unknown_keys(population, {"path", "source_url", "fetched_date"}, "population")
    population_path_raw = _require(population, "path", "population")
    if not isinstance(population_path_raw, str) or not population_path_raw.strip():
        raise ConfigError(
            f"config: 'population.path' must be a non-empty relative path, got {population_path_raw!r}"
        )
    population_source_url = _require(population, "source_url", "population")
    if not isinstance(population_source_url, str):
        raise ConfigError(f"config: 'population.source_url' must be a string, got {population_source_url!r}")
    if population_source_url and not population_source_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"config: 'population.source_url' must be empty or an http(s) URL, got {population_source_url!r}"
        )
    population_fetched_raw = _require(population, "fetched_date", "population")
    if not isinstance(population_fetched_raw, str):
        raise ConfigError(f"config: 'population.fetched_date' must be a string, got {population_fetched_raw!r}")
    population_fetched_date: date | None = None
    if population_fetched_raw:
        try:
            population_fetched_date = date.fromisoformat(population_fetched_raw)
        except ValueError as exc:
            raise ConfigError(
                f"config: 'population.fetched_date' must be empty or ISO YYYY-MM-DD, "
                f"got {population_fetched_raw!r}"
            ) from exc

    scope = _section(data, "scope")
    _reject_unknown_keys(scope, {"agency", "complaint_types"}, "scope")
    scope_agency = _require(scope, "agency", "scope")
    if not isinstance(scope_agency, str) or not scope_agency.strip():
        raise ConfigError(f"config: 'scope.agency' must be a non-empty string, got {scope_agency!r}")
    raw_types = _require(scope, "complaint_types", "scope")
    if not isinstance(raw_types, list) or not raw_types:
        raise ConfigError("config: 'scope.complaint_types' must be a non-empty array")
    complaint_types: list[str] = []
    for item in raw_types:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(
                f"config: 'scope.complaint_types' entries must be non-empty strings, got {item!r}"
            )
        if item in complaint_types:
            raise ConfigError(f"config: 'scope.complaint_types' contains duplicate {item!r}")
        complaint_types.append(item)

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
    _reject_unknown_keys(fetch, {"batch_size", "max_retries", "retry_backoff_seconds", "timeout_seconds"}, "fetch")
    batch_size = _parse_int(_require(fetch, "batch_size", "fetch"), "fetch.batch_size", minimum=1)
    max_retries = _parse_int(_require(fetch, "max_retries", "fetch"), "fetch.max_retries", minimum=0)
    retry_backoff_seconds = _parse_float(
        _require(fetch, "retry_backoff_seconds", "fetch"),
        "fetch.retry_backoff_seconds",
        minimum=0.0,
    )
    timeout_seconds = _parse_float(
        _require(fetch, "timeout_seconds", "fetch"),
        "fetch.timeout_seconds",
        minimum=0.0,
        strict=True,
    )

    paths = _section(data, "paths")
    _reject_unknown_keys(paths, {"raw_dir", "curated_dir", "raw_archive_dir"}, "paths")
    raw_dir = _parse_path(_require(paths, "raw_dir", "paths"), "paths.raw_dir")
    curated_dir = _parse_path(_require(paths, "curated_dir", "paths"), "paths.curated_dir")
    # Optional: the moving-window feature (section 4.8.1) RENAMES the raw
    # directory into the archive instead of deleting it. Absent key -> the raw
    # directory name plus "_archived" (data/raw -> data/raw_archived), so the
    # archive always sits beside the data it preserves.
    if "raw_archive_dir" in paths:
        raw_archive_dir = _parse_path(paths["raw_archive_dir"], "paths.raw_archive_dir")
    else:
        raw_archive_dir = raw_dir.parent / f"{raw_dir.name}_archived"
    if raw_archive_dir == raw_dir or raw_dir.is_relative_to(raw_archive_dir) or (
        raw_archive_dir.is_relative_to(raw_dir)
    ):
        raise ConfigError(
            "config: 'paths.raw_archive_dir' must be a directory separate from "
            f"'paths.raw_dir' (got '{raw_archive_dir}' vs '{raw_dir}'): the moving "
            "window renames the whole raw directory into it"
        )

    quarantine = _section(data, "quarantine")
    _reject_unknown_keys(quarantine, {"max_ratio"}, "quarantine")
    quarantine_max_ratio = _parse_ratio(
        _require(quarantine, "max_ratio", "quarantine"), "quarantine.max_ratio"
    )

    return Settings(
        source_url=source_url,
        source_field_map=source_field_map,
        population_path=Path(population_path_raw),
        population_source_url=population_source_url,
        population_fetched_date=population_fetched_date,
        scope_agency=scope_agency,
        scope_complaint_types=tuple(complaint_types),
        start_date=start_date,
        end_date=end_date,
        batch_size=batch_size,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
        timeout_seconds=timeout_seconds,
        raw_dir=raw_dir,
        curated_dir=curated_dir,
        raw_archive_dir=raw_archive_dir,
        quarantine_max_ratio=quarantine_max_ratio,
    )

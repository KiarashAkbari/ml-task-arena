"""Run manifest: the evidence record of one run (AGENTS.md 4.5 output 3 + section 3).

``run_manifest.json`` records, all from artifacts on disk (never invented):

* the received time window and the scope criterion (source, agency, locked
  complaint-type list) — section 3 requires the criterion in the manifest,
* raw record count (fetch checkpoint), passed count, quarantine count with
  per-reason breakdown (quarantine output),
* fetch budget seeds: total HTTP requests, batches, peak in-memory batch,
* run start / finish timestamps and the code version,
* sha256 + size of every curated output file (cases, aggregate, quarantine).
  The manifest cannot hash itself; its timestamps are metadata — the
  determinism claim covers the data outputs it lists (documented in README).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from data_factory import __version__
from data_factory.clean import CASES_NAME
from data_factory.config import Settings
from data_factory.curate import QUARANTINE_NAME
from data_factory.aggregate import AGGREGATE_NAME
from data_factory.errors import ValidationError
from data_factory.fetch import CHECKPOINT_NAME, _load_checkpoint

MANIFEST_NAME = "run_manifest.json"
_HASH_CHUNK_BYTES = 1 << 20  # 1 MiB chunks: never load a whole output in memory


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _output_entry(path: Path) -> dict[str, Any]:
    return {"sha256": sha256_file(path), "bytes": path.stat().st_size}


def collect_manifest(
    settings: Settings,
    *,
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    """Assemble the manifest dictionary from stage artifacts on disk."""
    raw_dir = Path(settings.raw_dir)
    curated_dir = Path(settings.curated_dir)

    checkpoint_path = raw_dir / CHECKPOINT_NAME
    if not checkpoint_path.is_file():
        raise ValidationError(
            f"no fetch checkpoint in '{raw_dir}' — run the fetch stage first"
        )
    state = _load_checkpoint(checkpoint_path, settings.batch_size)
    if not state["complete"]:
        raise ValidationError(
            f"fetch is incomplete ({state['records_written']} records so far) — "
            "resume the fetch stage first"
        )

    quarantine_path = curated_dir / QUARANTINE_NAME
    cases_path = curated_dir / CASES_NAME
    aggregate_path = curated_dir / AGGREGATE_NAME
    missing = [
        str(path)
        for path in (quarantine_path, cases_path, aggregate_path)
        if not path.is_file()
    ]
    if missing:
        raise ValidationError(
            f"missing output(s): {missing} — run curate, clean, and aggregate first"
        )

    quarantine_records = 0
    by_reason: Counter[str] = Counter()
    with quarantine_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            entry = json.loads(line)
            quarantine_records += 1
            by_reason.update(entry["reasons"])

    with cases_path.open(newline="", encoding="utf-8") as handle:
        rows_written = sum(1 for _ in handle) - 1  # minus header

    raw_records = state["records_written"]
    passed = raw_records - quarantine_records
    if rows_written != passed:
        raise ValidationError(
            f"manifest cross-check failed: {passed} gate-passing records but "
            f"{rows_written} curated rows — outputs are out of sync"
        )

    return {
        "code_version": __version__,
        "window": {
            "start": settings.start_date.isoformat(),
            "end": settings.end_date.isoformat(),
        },
        "scope": {
            "source_url": settings.source_url,
            "agency": settings.scope_agency,
            "complaint_types": list(settings.scope_complaint_types),
        },
        "fetch": {
            "records_raw": raw_records,
            "batches": state["batches_written"],
            "http_requests": state["http_requests"],
            "peak_in_memory_records": state["peak_in_memory_records"],
        },
        "passed_records": passed,
        "quarantine": {
            "records": quarantine_records,
            "by_reason": dict(sorted(by_reason.items())),
        },
        "population_source": {
            "path": settings.population_path.as_posix(),
            "source_url": settings.population_source_url or None,
            "fetched_date": (
                settings.population_fetched_date.isoformat()
                if settings.population_fetched_date
                else None
            ),
        },
        "started_at": started_at,
        "finished_at": finished_at,
        "outputs": {
            CASES_NAME: _output_entry(cases_path),
            AGGREGATE_NAME: _output_entry(aggregate_path),
            QUARANTINE_NAME: _output_entry(quarantine_path),
        },
    }


def write_manifest(settings: Settings, manifest: dict[str, Any]) -> Path:
    """Atomically write the manifest; returns its path."""
    curated_dir = Path(settings.curated_dir)
    curated_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = curated_dir / MANIFEST_NAME
    tmp_path = manifest_path.with_name(manifest_path.name + ".tmp")
    tmp_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(tmp_path, manifest_path)
    return manifest_path

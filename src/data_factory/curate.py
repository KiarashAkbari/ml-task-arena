"""Curate stage: contract gate -> quarantine output -> counts -> threshold.

Implements AGENTS.md section 4.3 — the heart of the challenge:

* records that violate ``contract.COLUMNS`` are NEVER dropped: they are
  written to ``<curated_dir>/quarantine.jsonl`` with every reason code and
  the original record (so the factory knows what it discarded and why),
* per-reason counts are reported at the end of every run,
* if the quarantine ratio exceeds ``quarantine.max_ratio`` from config, the
  run fails (:class:`QuarantineThresholdExceeded`) instead of continuing with
  suspect data,
* one batch file is read at a time — the whole raw dataset never sits in
  memory (section 4.2), and the quarantine file is canonical JSONL in fetch
  order, so two runs produce byte-identical outputs.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from data_factory.config import Settings
from data_factory.errors import QuarantineThresholdExceeded, ValidationError
from data_factory.fetch import BATCH_NAME_TEMPLATE, CHECKPOINT_NAME, _load_checkpoint
from data_factory.validation import RecordGate

QUARANTINE_NAME = "quarantine.jsonl"


@dataclass(frozen=True)
class CurateResult:
    """Measured outcome of one curate invocation (all counts are real)."""

    total: int
    passed: int
    quarantined: int
    counts: dict[str, int]   # per reason code: records containing that code
    ratio: float
    threshold: float

    @property
    def exceeded(self) -> bool:
        return self.ratio > self.threshold


def run_curate(settings: Settings) -> CurateResult:
    """Gate all fetched raw records; write quarantine output and counts.

    Raises:
        ValidationError: If fetching has not completed yet (stage ordering).
    """
    raw_dir = Path(settings.raw_dir)
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

    curated_dir = Path(settings.curated_dir)
    curated_dir.mkdir(parents=True, exist_ok=True)
    quarantine_path = curated_dir / QUARANTINE_NAME
    tmp_path = quarantine_path.with_name(quarantine_path.name + ".tmp")

    counts: Counter[str] = Counter()
    total = 0
    passed = 0
    gate = RecordGate(settings)  # contract + logical + duplicate rules, stream-ordered
    try:
        with tmp_path.open("w", encoding="utf-8") as tmp:
            for index in range(state["batches_written"]):
                batch_path = raw_dir / BATCH_NAME_TEMPLATE.format(index=index)
                batch = json.loads(batch_path.read_text(encoding="utf-8"))
                for record in batch:
                    total += 1
                    reasons = sorted(set(gate.check(record)))
                    if reasons:
                        for code in reasons:
                            counts[code] += 1
                        line = json.dumps(
                            {"reasons": reasons, "record": record},
                            sort_keys=True,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        tmp.write(line + "\n")
                    else:
                        passed += 1
        os.replace(tmp_path, quarantine_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    quarantined = total - passed
    ratio = quarantined / total if total else 0.0
    return CurateResult(
        total=total,
        passed=passed,
        quarantined=quarantined,
        counts=dict(sorted(counts.items())),
        ratio=ratio,
        threshold=settings.quarantine_max_ratio,
    )


def ensure_within_threshold(result: CurateResult) -> None:
    """Fail the run when quarantine is above the configured threshold (4.3)."""
    if result.exceeded:
        raise QuarantineThresholdExceeded(
            f"quarantine ratio {result.ratio:.2%} exceeds threshold "
            f"{result.threshold:.2%}: {result.quarantined} of {result.total} "
            f"records quarantined (reason counts: {result.counts}) — "
            "refusing to continue with suspect data"
        )

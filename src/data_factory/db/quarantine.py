"""Row-level load quarantine: what was rejected, why, and what was repaired.

Two different things are recorded, and conflating them would hide a decision:

* **rejection** — the row cannot satisfy a NOT NULL / CHECK / FK constraint,
  so it is not loaded.  Every rejection carries a reason code and a count.
* **repair** — the row is loadable after a value is replaced by a faithful
  encoding of "unknown" (e.g. a weight of 0 g becomes NULL).  A repair never
  removes a constraint; it removes a value that cannot be true.

Each rejection also quotes up to a few key values so a reader can find the
offending rows in the source file without re-running the pipeline.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Rejection:
    source_file: str
    reason: str
    row_count: int
    detail: str
    sample_keys: tuple[str, ...] = field(default=())

    def as_row(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Repair:
    source_file: str
    reason: str
    row_count: int
    detail: str

    def as_row(self) -> dict:
        return asdict(self)


def write_jsonl(path: Path, rejections: Iterable[Rejection]) -> Path:
    """One JSON object per line, stable order, so the file is diff-able."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rejection in sorted(rejections, key=lambda r: (r.source_file, r.reason)):
            fh.write(json.dumps(rejection.as_row(), ensure_ascii=False) + "\n")
    return path


def summarise(rejections: Iterable[Rejection], repairs: Iterable[Repair]) -> dict[str, int]:
    """Totals used by the load report and the notebook."""
    rej = list(rejections)
    rep = list(repairs)
    return {
        "rejected_rows": sum(r.row_count for r in rej),
        "rejection_reasons": len(rej),
        "repaired_rows": sum(r.row_count for r in rep),
        "repair_reasons": len(rep),
    }

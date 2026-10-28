"""Repeatable query timing and execution-plan capture.

"One run and a number" is not a measurement (AGENTS.md 4.3.2).  Every number
this module reports is the **median of N runs after warm-up**, with the p95
reported next to it so a reader can see the spread instead of a single lucky
sample.  Cache state is stated, not implied: PostgreSQL's shared buffers and
the OS page cache are warm after the warm-up runs, so the numbers describe the
"dashboard already in use" case, which is the case the 200 ms budget is about.

Plans are captured with ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` so the
notebook can show estimated vs actual rows and buffer reads, not just cost.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, text

DEFAULT_RUNS = 7
DEFAULT_WARMUP = 2


@dataclass(frozen=True)
class Timing:
    key: str
    runs: int
    warmup: int
    min_ms: float
    median_ms: float
    p95_ms: float
    max_ms: float
    rows: int

    @property
    def within_budget(self) -> bool:
        return self.median_ms < 200.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "runs": self.runs,
            "warmup": self.warmup,
            "min_ms": round(self.min_ms, 2),
            "median_ms": round(self.median_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
            "max_ms": round(self.max_ms, 2),
            "rows": self.rows,
            "within_200ms": self.within_budget,
        }


@dataclass(frozen=True)
class PlanCapture:
    key: str
    plan: dict[str, Any]
    text_plan: str
    seq_scans: tuple[str, ...] = ()
    index_scans: tuple[str, ...] = ()
    execution_ms: float = 0.0
    shared_read_blocks: int = 0
    rows_estimated: float = 0.0
    rows_actual: float = 0.0

    @property
    def worst_estimate_error(self) -> float:
        """Actual rows / estimated rows: the classic sign a plan is being fooled."""
        if not self.rows_estimated:
            return 0.0
        return self.rows_actual / self.rows_estimated

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "execution_ms": round(self.execution_ms, 2),
            "seq_scans": list(self.seq_scans),
            "index_scans": list(self.index_scans),
            "shared_read_blocks": self.shared_read_blocks,
            "rows_estimated": round(self.rows_estimated, 1),
            "rows_actual": round(self.rows_actual, 1),
            "estimate_error_ratio": round(self.worst_estimate_error, 2),
        }


def measure(
    conn: Connection,
    sql: str,
    *,
    key: str = "",
    runs: int = DEFAULT_RUNS,
    warmup: int = DEFAULT_WARMUP,
    params: dict[str, Any] | None = None,
) -> Timing:
    """Time a query N times after warm-up and report median / p95.

    Returns the row count from the last run so a caller can prove the query
    still produced a result while being timed.
    """
    statement = text(sql)
    rows = 0
    for _ in range(warmup):
        rows = len(conn.execute(statement, params or {}).fetchall())

    samples: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        rows = len(conn.execute(statement, params or {}).fetchall())
        samples.append((time.perf_counter() - start) * 1000.0)

    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return Timing(
        key=key,
        runs=runs,
        warmup=warmup,
        min_ms=ordered[0],
        median_ms=statistics.median(ordered),
        p95_ms=ordered[p95_index],
        max_ms=ordered[-1],
        rows=rows,
    )


def _walk(node: dict[str, Any]):
    yield node
    for child in node.get("Plans", []):
        yield from _walk(child)


def explain(conn: Connection, sql: str, *, key: str = "",
            params: dict[str, Any] | None = None) -> PlanCapture:
    """EXPLAIN ANALYZE the query and summarise which scans it chose."""
    raw = conn.execute(
        text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}"), params or {}
    ).scalar_one()
    plan_root = (raw[0] if isinstance(raw, list) else raw)["Plan"]
    execution_ms = float((raw[0] if isinstance(raw, list) else raw)["Execution Time"])
    text_plan = conn.execute(
        text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params or {}
    )
    lines = [row[0] for row in text_plan]

    seq, idx = [], []
    for node in _walk(plan_root):
        relation = node.get("Relation Name") or node.get("CTE Name") or node.get("Node Type")
        if node.get("Node Type") == "Seq Scan":
            seq.append(str(relation))
        elif "Index" in str(node.get("Node Type", "")) or node.get("Node Type") == "Bitmap Heap Scan":
            idx.append(str(relation))
    return PlanCapture(
        key=key,
        plan=plan_root,
        text_plan="\n".join(lines),
        seq_scans=tuple(seq),
        index_scans=tuple(idx),
        execution_ms=execution_ms,
        shared_read_blocks=int(plan_root.get("Shared Hit Blocks", 0)),
        rows_estimated=float(plan_root.get("Plan Rows", 0)),
        rows_actual=float(plan_root.get("Actual Rows", 0)),
    )


@dataclass
class Evidence:
    """Everything the notebook and report need about one query at one moment."""

    key: str
    timing: Timing
    plan: PlanCapture
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = {"key": self.key, "timing": self.timing.as_dict(),
                   "plan": self.plan.as_dict(), "note": self.note}
        payload.update(self.extra)
        return payload


def to_json(evidence: list[Evidence]) -> str:
    return json.dumps([item.as_dict() for item in evidence], indent=2)

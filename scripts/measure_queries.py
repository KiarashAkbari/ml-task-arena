"""Measure every dashboard query and write the evidence to data/db/.

Usage:
    python scripts/measure_queries.py --url ... --label baseline
    python scripts/measure_queries.py --url ... --label optimized

The label only names the output file (``data/db/query_evidence_<label>.json``);
the numbers always come from a real run, so nothing here can manufacture a
before/after pair that did not happen.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_factory.db.connection import engine_scope  # noqa: E402
from data_factory.db.queries import QUERIES  # noqa: E402
from data_factory.db.timing import Evidence, explain, measure, to_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--label", default="run")
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--out-dir", default="data/db")
    args = parser.parse_args()

    evidence: list[Evidence] = []
    with engine_scope(args.url) as engine:
        with engine.connect() as conn:
            for query in QUERIES:
                # The served form is what the 200 ms budget applies to; the
                # live form is the before-side of the comparison.
                timing = measure(conn, query.served_sql, key=query.key,
                                 runs=args.runs, warmup=args.warmup)
                plan = explain(conn, query.served_sql, key=query.key)
                live = measure(conn, query.live_sql, key=query.key + "-live",
                               runs=args.runs, warmup=args.warmup)
                evidence.append(Evidence(
                    key=query.key, timing=timing, plan=plan, note=query.title,
                    extra={"served_from": query.served_from,
                           "live_median_ms": round(live.median_ms, 2),
                           "live_p95_ms": round(live.p95_ms, 2)}))
                status = "OK  " if timing.within_budget else "OVER"
                print(
                    f"{status} {query.key} [{query.served_from:8}] served {timing.median_ms:7.2f} ms"
                    f"  live {live.median_ms:8.2f} ms  rows {timing.rows:>5}"
                )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"query_evidence_{args.label}.json"
    path.write_text(to_json(evidence), encoding="utf-8")
    print(f"\nwrote {path}")
    over = [e.key for e in evidence if not e.timing.within_budget]
    print(f"over budget: {over if over else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Measure what an index costs, so the ones we did not keep have a price tag.

Thin CLI over ``data_factory.db.evidence``: candidate indexes are built,
measured against a freshly re-measured baseline, and dropped; then insert
throughput is measured with 0, 1, 2 and 4 secondary indexes.

    python scripts/measure_indexes.py --url "$DATABASE_URL"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_factory.db.connection import engine_scope  # noqa: E402
from data_factory.db.evidence import index_experiments, insert_cost_experiment  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--out-dir", default="data/db")
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()

    with engine_scope(args.url) as engine:
        candidates = index_experiments(engine, runs=args.runs)
        for item in candidates:
            print(f"  {item['index']:38} {item['size']:>8}  best delta "
                  f"{item['best_delta_ms']:+.0f} ms")
        insert_cost = insert_cost_experiment(engine)
        for item in insert_cost:
            print(f"  {item['setup']:20} {item['rows_per_second']:>8,} rows/s  "
                  f"{item['table_size']}")

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "index_experiments.json"
    path.write_text(json.dumps({"candidates": candidates, "insert_cost": insert_cost},
                               indent=2), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

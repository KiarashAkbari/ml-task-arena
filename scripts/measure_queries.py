"""Time every dashboard query and write the evidence to data/db/.

Thin CLI over ``data_factory.db.evidence.query_evidence`` so the notebook,
this script and the report all measure through the same code.

    python scripts/measure_queries.py --url "$DATABASE_URL" --label optimized
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_factory.db.connection import engine_scope  # noqa: E402
from data_factory.db.evidence import query_evidence  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--label", default="run")
    parser.add_argument("--runs", type=int, default=15)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--out-dir", default="data/db")
    args = parser.parse_args()

    with engine_scope(args.url) as engine:
        evidence = query_evidence(engine, runs=args.runs, warmup=args.warmup)

    payload = [item.as_dict() for item in evidence]
    for item in payload:
        status = "OK  " if item["within_budget"] else "OVER"
        print(f"{status} {item['key']} [{item['served_from']:8}] "
              f"served {item['timing']['median_ms']:7.2f} ms "
              f"p95 {item['timing']['p95_ms']:7.2f} "
              f"| live {item['live_median_ms']:8.2f} ms")

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"query_evidence_{args.label}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwrote {path}")
    over = [item["key"] for item in payload if not item["within_budget"]]
    print(f"over budget: {over if over else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

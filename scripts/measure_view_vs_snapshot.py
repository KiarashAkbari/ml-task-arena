"""View or snapshot? Measure both and let the numbers decide.

Thin CLI over ``data_factory.db.evidence.view_vs_snapshot``.

    python scripts/measure_view_vs_snapshot.py --url "$DATABASE_URL"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_factory.db.connection import engine_scope  # noqa: E402
from data_factory.db.evidence import view_vs_snapshot  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--out-dir", default="data/db")
    args = parser.parse_args()

    with engine_scope(args.url) as engine:
        payload = view_vs_snapshot(engine, runs=args.runs)

    for key in ("q1", "q4"):
        item = payload[key]
        print(f"{key}: live {item['live_median_ms']:7.2f} ms -> snapshot "
              f"{item['snapshot_median_ms']:6.2f} ms ({item['speedup']}x)")
    print(f"full refresh: {payload['refresh_full_seconds']} s")
    print(f"one-month refresh: {payload['refresh_one_month_seconds']} s")

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "view_vs_snapshot.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

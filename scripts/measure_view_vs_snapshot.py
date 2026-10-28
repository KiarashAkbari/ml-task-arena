"""View or snapshot? Measure both and let the numbers decide.

AGENTS.md 4.3.6 asks for the heaviest analysis built both ways - a view that
recomputes on every call, and a precomputed table - timed, with the trade-off
stated.  This script produces that comparison for the two panels where it
applies (Q1 and Q4), plus the refresh cost and the staleness the snapshot
introduces, so "fresher" and "cheaper" can be compared as quantities rather
than adjectives.

Writes data/db/view_vs_snapshot.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text  # noqa: E402

from data_factory.db.connection import engine_scope  # noqa: E402
from data_factory.db.queries import BY_KEY  # noqa: E402
from data_factory.db.timing import measure  # noqa: E402

PANELS = {
    "q1": {
        "live_object": "olist.v_revenue_by_month_category (view)",
        "snapshot_table": "olist.revenue_snapshot",
        "source_tables": ("olist.order_item", "olist.orders", "olist.product",
                          "olist.product_category"),
        "refresh_call": "CALL olist.refresh_dashboard_snapshot(NULL)",
    },
    "q4": {
        "live_object": "inline three-CTE query over order_item/orders/order_review",
        "snapshot_table": "olist.seller_leaderboard",
        "source_tables": ("olist.order_item", "olist.orders", "olist.order_review"),
        "refresh_call": "CALL olist.refresh_dashboard_snapshot(NULL)",
    },
}


def table_sizes(conn, names) -> dict[str, str]:
    return {
        name: conn.execute(
            text("SELECT pg_size_pretty(pg_total_relation_size(:t))"), {"t": name}
        ).scalar_one()
        for name in names
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--out-dir", default="data/db")
    args = parser.parse_args()

    results = {}
    with engine_scope(args.url) as engine:
        with engine.connect() as conn:
            for key, panel in PANELS.items():
                query = BY_KEY[key]
                live = measure(conn, query.live_sql, runs=args.runs, warmup=2)
                snapshot = measure(conn, query.served_sql, runs=args.runs, warmup=2)
                sizes = table_sizes(
                    conn, (panel["snapshot_table"],) + panel["source_tables"])
                staleness = conn.execute(text(
                    f"SELECT max(computed_at) FROM {panel['snapshot_table']}"
                )).scalar()
                results[key] = {
                    "live_object": panel["live_object"],
                    "live_median_ms": round(live.median_ms, 2),
                    "live_p95_ms": round(live.p95_ms, 2),
                    "snapshot_median_ms": round(snapshot.median_ms, 2),
                    "snapshot_p95_ms": round(snapshot.p95_ms, 2),
                    "speedup": round(live.median_ms / max(snapshot.median_ms, 0.01), 1),
                    "rows": snapshot.rows,
                    "sizes": sizes,
                    "snapshot_last_computed": staleness.isoformat() if staleness else None,
                    "refresh_call": panel["refresh_call"],
                }
                print(f"{key}: live {live.median_ms:7.2f} ms -> snapshot "
                      f"{snapshot.median_ms:6.2f} ms  ({results[key]['speedup']}x)")

            # What the snapshot costs to maintain: a full refresh, timed.
            started = time.perf_counter()
            with engine.begin() as writer:
                writer.execute(text("CALL olist.refresh_dashboard_snapshot(NULL)"))
            refresh_seconds = time.perf_counter() - started
            results["refresh_full_seconds"] = round(refresh_seconds, 2)
            print(f"full refresh: {refresh_seconds:.2f} s")

            # And what a single-month refresh costs, which is what a nightly
            # job would actually pay.
            started = time.perf_counter()
            with engine.begin() as writer:
                writer.execute(
                    text("CALL olist.refresh_dashboard_snapshot(:m)"),
                    {"m": "2017-05-01"})
            one_month = time.perf_counter() - started
            results["refresh_one_month_seconds"] = round(one_month, 2)
            print(f"one-month refresh: {one_month:.2f} s")

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "view_vs_snapshot.json"
    path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Measure what an index costs, so the ones we did not keep have a price tag.

Two experiments, both on the real data:

1. For each candidate index: build it, time the query it was meant to help,
   record its size, then drop it.  A candidate that does not pay is reported
   with its cost anyway - that is the evidence AGENTS.md 4.3.4 asks for.

2. Insert cost: load a 50k-row copy of order_item with zero indexes, then with
   two, then with four, and report rows/second.  This is the price of an index
   that a "make it faster" instinct never shows you.

Writes data/db/index_experiments.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from data_factory.db.connection import engine_scope  # noqa: E402
from data_factory.db.queries import BY_KEY  # noqa: E402
from data_factory.db.timing import measure  # noqa: E402


CANDIDATES = [
    ("item_seller", "CREATE INDEX {name} ON olist.order_item (seller_id)", ["q4"]),
    ("item_product", "CREATE INDEX {name} ON olist.order_item (product_id)", ["q1"]),
    ("order_status_ts", "CREATE INDEX {name} ON olist.orders (status, purchase_ts)", ["q2"]),
    ("order_delivered", "CREATE INDEX {name} ON olist.orders (status) WHERE status = 'delivered'", ["q6"]),
    ("customer_state", "CREATE INDEX {name} ON olist.customer (state, customer_id)", ["q6", "q2"]),
    ("review_order", "CREATE INDEX {name} ON olist.order_review (order_id) INCLUDE (score)", ["q3"]),
]

INSERT_ROWS = 50_000


def relation_size(conn, name: str) -> str:
    return conn.execute(
        text(
            "SELECT pg_size_pretty(pg_relation_size(c.oid)) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'olist' AND c.relname = :n"
        ),
        {"n": name},
    ).scalar()


def candidate_experiment(conn, runs: int) -> list[dict]:
    results = []
    # Statistics first: an unanalyzed table made a useless index look like a
    # 43 ms win in an earlier run of this exact experiment.
    for table in ("order_item", "orders", "customer", "order_review"):
        conn.execute(text(f"ANALYZE olist.{table}"))
    for name, ddl, targets in CANDIDATES:
        full = f"olist.{name}"
        # baseline measured here, with no secondary index present
        baseline = {}
        for key in targets:
            query = BY_KEY[key]
            # Measure the form an index could plausibly help: for a snapshot
            # panel that is the live query behind it, not the 0.2 ms read.
            sql = query.live_sql if query.served_from == "snapshot" else query.served_sql
            baseline[key] = measure(conn, sql, runs=runs, warmup=1).median_ms
        conn.execute(text(ddl.format(name=name)))
        conn.execute(text("ANALYZE olist.order_item"))
        conn.execute(text("ANALYZE olist.orders"))
        conn.execute(text("ANALYZE olist.customer"))
        conn.execute(text("ANALYZE olist.order_review"))
        size = relation_size(conn, name)
        effects = {}
        for key in targets:
            query = BY_KEY[key]
            sql = query.live_sql if query.served_from == "snapshot" else query.served_sql
            timing = measure(conn, sql, runs=runs, warmup=1)
            effects[key] = {
                "median_ms": round(timing.median_ms, 2),
                "baseline_ms": round(baseline[key], 2),
                "delta_ms": round(timing.median_ms - baseline[key], 2),
            }
        conn.execute(text(f"DROP INDEX {full}"))
        results.append({"index": name, "size": size, "ddl": ddl.format(name=name),
                        "kept": False, "effects": effects})
        print(f"  {name:18} {size:>8}  " +
              "  ".join(f"{k} {v['median_ms']:.0f}ms ({v['delta_ms']:+.0f})"
                        for k, v in effects.items()))
    return results


def insert_experiment(conn) -> list[dict]:
    """Insert the same 50k rows with 0, 2 and 4 indexes on the target table."""
    source = pd.read_csv(
        "data/raw/olist/olist_order_items_dataset.csv",
        nrows=INSERT_ROWS, dtype=str,
    )
    frame = pd.DataFrame({
        "order_id": source["order_id"],
        "item_seq": pd.to_numeric(source["order_item_id"]).astype("int16"),
        "product_id": source["product_id"],
        "seller_id": source["seller_id"],
        "shipping_limit_ts": pd.to_datetime(source["shipping_limit_date"]),
        "price": pd.to_numeric(source["price"]).round(2),
        "freight_value": pd.to_numeric(source["freight_value"]).round(2),
    })

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS olist.bench_item (
            order_id varchar(32) NOT NULL, item_seq smallint NOT NULL,
            product_id varchar(32) NOT NULL, seller_id varchar(32) NOT NULL,
            shipping_limit_ts timestamp NOT NULL, price numeric(10,2) NOT NULL,
            freight_value numeric(10,2) NOT NULL,
            PRIMARY KEY (order_id, item_seq))
    """))

    setups = [
        ("no secondary index", []),
        ("+ seller_id", ["CREATE INDEX bench_s ON olist.bench_item (seller_id)"]),
        ("+ seller_id, product_id", [
            "CREATE INDEX bench_s ON olist.bench_item (seller_id)",
            "CREATE INDEX bench_p ON olist.bench_item (product_id)"]),
        ("+ 4 indexes", [
            "CREATE INDEX bench_s ON olist.bench_item (seller_id)",
            "CREATE INDEX bench_p ON olist.bench_item (product_id)",
            "CREATE INDEX bench_o ON olist.bench_item (order_id)",
            "CREATE INDEX bench_price ON olist.bench_item (price)"]),
    ]

    results = []
    # The same rows twice: the second copy re-inserts identical keys only after
    # the table is emptied, so primary-key collisions never enter the timing.
    for label, ddls in setups:
        conn.execute(text("TRUNCATE olist.bench_item"))
        for statement in ddls:
            conn.execute(text(statement))
        conn.execute(text("ANALYZE olist.bench_item"))
        start = time.perf_counter()
        frame.to_sql("bench_item", conn, schema="olist", if_exists="append",
                     index=False, chunksize=5000, method=None)
        seconds = time.perf_counter() - start
        total_size = conn.execute(text(
            "SELECT pg_size_pretty(pg_total_relation_size('olist.bench_item'))")).scalar()
        for statement in ddls:
            index = statement.split()[2]
            conn.execute(text(f"DROP INDEX olist.{index}"))
        rows_per_second = len(frame) / seconds
        results.append({"setup": label, "indexes": len(ddls), "seconds": round(seconds, 3),
                        "rows_per_second": round(rows_per_second), "total_size": total_size})
        print(f"  {label:24} {seconds:6.2f}s  {rows_per_second:8,.0f} rows/s  {total_size}")
    conn.execute(text("DROP TABLE olist.bench_item"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--out-dir", default="data/db")
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()

    payload = {}
    with engine_scope(args.url) as engine:
        with engine.begin() as conn:
            print("candidate indexes (build, measure, drop):")
            payload["candidates"] = candidate_experiment(conn, args.runs)
            print("\ninsert cost on a 50,000-row table:")
            payload["insert_cost"] = insert_experiment(conn)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "index_experiments.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

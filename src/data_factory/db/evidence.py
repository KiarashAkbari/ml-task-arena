"""Every experiment the notebook reports, as callable functions.

The notebook narrates; this module measures.  Keeping the experiments here
rather than in notebook cells means the same code produces the numbers in the
notebook, in `scripts/measure_*.py`, and in the report - so a number cannot
drift between the three.

Each function takes an ``Engine`` and returns plain dictionaries, which is
what makes them easy to display, dump to JSON, and re-run.
"""

from __future__ import annotations

import time
from typing import Any

import pandas as pd
from sqlalchemy import Engine, text

from data_factory.db import sqltext
from data_factory.db.queries import QUERIES
from data_factory.db.timing import Evidence, explain, measure

# ---------------------------------------------------------------------------
# 1. the dashboard queries: correctness rows, timing, plans
# ---------------------------------------------------------------------------


def query_evidence(
    engine: Engine, *, runs: int = 15, warmup: int = 3, live_runs: int = 5
) -> list[Evidence]:
    """Time both forms of every query and capture the served plan.

    ``runs`` applies to the served form - the one the 200 ms budget is about -
    and is deliberately larger than ``live_runs`` because the live form takes
    100-330 ms per execution and is only needed for the comparison.
    """
    evidence: list[Evidence] = []
    with engine.connect() as conn:
        for query in QUERIES:
            served = measure(conn, query.served_sql, key=query.key,
                             runs=runs, warmup=warmup)
            plan = explain(conn, query.served_sql, key=query.key)
            live = measure(conn, query.live_sql, key=query.key + "-live",
                           runs=live_runs, warmup=1)
            evidence.append(
                Evidence(
                    key=query.key,
                    timing=served,
                    plan=plan,
                    note=query.title,
                    extra={
                        "served_from": query.served_from,
                        "live_median_ms": round(live.median_ms, 2),
                        "live_p95_ms": round(live.p95_ms, 2),
                        "within_budget": served.within_budget,
                    },
                )
            )
    return evidence


def result_frame(engine: Engine, sql: str, *, limit: int | None = None) -> pd.DataFrame:
    """Read a query into a DataFrame in batches (AGENTS.md 4.3.8).

    A dashboard panel is small, but the batched read is the same code path a
    large export would take, and the notebook shows it rather than a
    one-shot ``fetchall``.
    """
    chunks: list[pd.DataFrame] = []
    with engine.connect() as conn:
        with conn.execution_options(stream_results=True).execute(text(sql)) as result:
            while True:
                batch = result.fetchmany(2_000)
                if not batch:
                    break
                chunks.append(pd.DataFrame(batch, columns=list(result.keys())))
                if limit is not None and sum(len(c) for c in chunks) >= limit:
                    break
    if not chunks:
        return pd.DataFrame()
    frame = pd.concat(chunks, ignore_index=True)
    return frame.head(limit) if limit is not None else frame


# ---------------------------------------------------------------------------
# 2. what an index costs, and whether it pays
# ---------------------------------------------------------------------------

#: (label, index name, target) - the target is validated by sqltext.create_index.
CANDIDATE_INDEXES = (
    ("order_item(seller_id)", "probe_order_item_seller",
     "olist.order_item (seller_id)", ("q4",)),
    ("order_item(product_id)", "probe_order_item_product",
     "olist.order_item (product_id)", ("q1",)),
    ("orders(status, purchase_ts)", "probe_orders_status_ts",
     "olist.orders (status, purchase_ts)", ("q2",)),
    ("orders(status) WHERE delivered", "probe_orders_delivered",
     "olist.orders (status)", ("q6",)),
    ("customer(state, customer_id)", "probe_customer_state",
     "olist.customer (state, customer_id)", ("q6", "q2")),
    ("order_review(order_id) INCLUDE(score)", "probe_review_order",
     "olist.order_review (order_id) INCLUDE (score)", ("q3",)),
)

INSERT_ROWS = 50_000


def _relation_size(conn, name: str) -> str:
    return conn.execute(
        text(
            "SELECT pg_size_pretty(pg_relation_size(c.oid)) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'olist' AND c.relname = :n"
        ),
        {"n": name},
    ).scalar()


def index_experiments(engine: Engine, *, runs: int = 5) -> list[dict[str, Any]]:
    """Build each candidate, measure the query it targets, then drop it.

    The baseline is re-measured before every candidate on the same connection.
    That detail matters: an earlier version of this experiment compared against
    a baseline taken before ``ANALYZE`` and credited a partial index with a
    43 ms win that turned out to be fresh statistics.
    """
    results: list[dict[str, Any]] = []
    by_key = {query.key: query for query in QUERIES}

    with engine.begin() as conn:
        for table in ("order_item", "orders", "customer", "order_review"):
            conn.execute(sqltext.analyze(table))

    for label, name, target, targets in CANDIDATE_INDEXES:
        with engine.begin() as conn:
            baseline = {}
            for key in targets:
                query = by_key[key]
                sql = query.live_sql if query.served_from == "snapshot" else query.served_sql
                baseline[key] = round(measure(conn, sql, runs=runs, warmup=1).median_ms, 2)

            conn.execute(sqltext.create_index(name, target))
            for table in ("order_item", "orders", "customer", "order_review"):
                conn.execute(sqltext.analyze(table))

            size = _relation_size(conn, name)
            effects = {}
            for key in targets:
                query = by_key[key]
                sql = query.live_sql if query.served_from == "snapshot" else query.served_sql
                after = measure(conn, sql, runs=runs, warmup=1).median_ms
                effects[key] = {
                    "baseline_ms": baseline[key],
                    "with_index_ms": round(after, 2),
                    "delta_ms": round(after - baseline[key], 2),
                }
            conn.execute(sqltext.drop_index(name))

        results.append({
            "index": label,
            "size": size,
            "kept": False,
            "effects": effects,
            "best_delta_ms": min(e["delta_ms"] for e in effects.values()),
        })
    return results


def insert_cost_experiment(engine: Engine) -> list[dict[str, Any]]:
    """Insert the same 50,000 rows with 0, 1, 2 and 4 secondary indexes.

    This is the cost an index never shows you when you are only looking at
    SELECT timings.
    """
    source = pd.read_csv("data/raw/olist/olist_order_items_dataset.csv",
                         nrows=INSERT_ROWS, dtype=str)
    frame = pd.DataFrame({
        "order_id": source["order_id"],
        "item_seq": pd.to_numeric(source["order_item_id"]).astype("int16"),
        "product_id": source["product_id"],
        "seller_id": source["seller_id"],
        "shipping_limit_ts": pd.to_datetime(source["shipping_limit_date"]),
        "price": pd.to_numeric(source["price"]).round(2),
        "freight_value": pd.to_numeric(source["freight_value"]).round(2),
    })

    setups = (
        ("no secondary index", ()),
        ("1 index", (("bench_s", "olist.bench_item (seller_id)"),)),
        ("2 indexes", (("bench_s", "olist.bench_item (seller_id)"),
                       ("bench_p", "olist.bench_item (product_id)"))),
        ("4 indexes", (("bench_s", "olist.bench_item (seller_id)"),
                       ("bench_p", "olist.bench_item (product_id)"),
                       ("bench_o", "olist.bench_item (order_id)"),
                       ("bench_px", "olist.bench_item (price)"))),
    )

    results: list[dict[str, Any]] = []
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS olist.bench_item (
                order_id varchar(32) NOT NULL, item_seq smallint NOT NULL,
                product_id varchar(32) NOT NULL, seller_id varchar(32) NOT NULL,
                shipping_limit_ts timestamp NOT NULL, price numeric(10,2) NOT NULL,
                freight_value numeric(10,2) NOT NULL,
                PRIMARY KEY (order_id, item_seq))
        """))

    for label, ddls in setups:
        with engine.begin() as conn:
            conn.execute(sqltext.truncate("bench_item"))
            for index_name, target in ddls:
                conn.execute(sqltext.create_index(index_name, target))
            conn.execute(text("ANALYZE olist.bench_item"))
            started = time.perf_counter()
            frame.to_sql("bench_item", conn, schema="olist", if_exists="append",
                         index=False, chunksize=5_000, method=None)
            seconds = time.perf_counter() - started
            total_size = conn.execute(text(
                "SELECT pg_size_pretty(pg_total_relation_size('olist.bench_item'))"
            )).scalar()
            for index_name, _target in reversed(ddls):
                conn.execute(sqltext.drop_index(index_name))
        results.append({
            "setup": label,
            "indexes": len(ddls),
            "seconds": round(seconds, 3),
            "rows_per_second": round(len(frame) / seconds),
            "table_size": total_size,
        })

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE olist.bench_item"))
    return results


# ---------------------------------------------------------------------------
# 3. view or snapshot
# ---------------------------------------------------------------------------

VIEW_VS_SNAPSHOT = {
    "q1": ("olist.v_revenue_by_month_category", "olist.revenue_snapshot"),
    "q4": ("live three-CTE query", "olist.seller_leaderboard"),
}


def view_vs_snapshot(engine: Engine, *, runs: int = 7) -> dict[str, Any]:
    """Time the two halves, then time what keeping the snapshot costs."""
    by_key = {query.key: query for query in QUERIES}
    payload: dict[str, Any] = {}

    with engine.connect() as conn:
        for key, (live_object, snapshot_table) in VIEW_VS_SNAPSHOT.items():
            query = by_key[key]
            live = measure(conn, query.live_sql, runs=runs, warmup=2)
            snapshot = measure(conn, query.served_sql, runs=runs, warmup=2)
            size = conn.execute(
                text("SELECT pg_size_pretty(pg_total_relation_size(:t))"),
                {"t": snapshot_table},
            ).scalar_one()
            snapshot_schema, _, snapshot_name = snapshot_table.partition(".")
            computed_at = conn.execute(
                sqltext.latest_timestamp(snapshot_name, schema=snapshot_schema or "olist")
            ).scalar_one()
            payload[key] = {
                "live_object": live_object,
                "live_median_ms": round(live.median_ms, 2),
                "snapshot_median_ms": round(snapshot.median_ms, 2),
                "speedup": round(live.median_ms / max(snapshot.median_ms, 0.01), 1),
                "snapshot_size": size,
                "snapshot_rows": snapshot.rows,
                "last_computed": computed_at.isoformat() if computed_at else None,
            }

    started = time.perf_counter()
    with engine.begin() as conn:
        conn.execute(text("CALL olist.refresh_dashboard_snapshot(NULL)"))
    payload["refresh_full_seconds"] = round(time.perf_counter() - started, 2)

    started = time.perf_counter()
    with engine.begin() as conn:
        conn.execute(text("CALL olist.refresh_dashboard_snapshot(:m)"),
                     {"m": "2017-05-01"})
    payload["refresh_one_month_seconds"] = round(time.perf_counter() - started, 2)
    return payload


# ---------------------------------------------------------------------------
# 4. the load itself
# ---------------------------------------------------------------------------


def load_summary(path: str = "data/db/load_report.json") -> dict[str, Any]:
    """Read the report the bootstrap wrote, so the notebook shows its numbers."""
    import json
    from pathlib import Path

    report = Path(path)
    if not report.is_file():
        return {}
    return json.loads(report.read_text(encoding="utf-8"))

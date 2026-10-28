"""Every experiment the notebook reports, as callable functions.

The notebook narrates; this module measures.  Keeping the experiments here
rather than in notebook cells means the same code produces the numbers in the
notebook, in `scripts/measure_*.py`, and in the report - so a number cannot
drift between the three.

Each function takes an ``Engine`` and returns plain dictionaries, which is
what makes them easy to display, dump to JSON, and re-run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
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


# ---------------------------------------------------------------------------
# 5. what the rejection policy costs, and how repeatable a measurement is
# ---------------------------------------------------------------------------


def rejection_impact(source_dir, engine=None) -> dict[str, Any]:
    """Revenue removed by the quarantine policy, computed from the raw CSVs.

    The rule is applied here exactly as ``olist.prepare`` applies it.  When an
    engine is supplied the result is cross-checked against what actually got
    loaded, so the cost is verified by two independent code paths instead of
    being taken on trust from one.

    The total record count is returned too, because every "share of raw" figure
    in the notebook divides by it - and a denominator that is typed in by hand
    is a number nobody measured.
    """
    import csv
    from pathlib import Path

    import pandas as pd

    source = Path(source_dir)
    files = sorted(source.glob("*.csv"))
    total_records = 0
    for path in files:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            next(reader, None)
            total_records += sum(1 for _ in reader)

    orders = pd.read_csv(source / "olist_orders_dataset.csv", dtype=str)
    items = pd.read_csv(source / "olist_order_items_dataset.csv", dtype=str)

    def parsed(column: str):
        return pd.to_datetime(orders[column], errors="coerce")

    delivered_without_date = (
        orders["order_status"].eq("delivered") & orders["order_delivered_customer_date"].isna()
    )
    carrier_before_purchase = (
        parsed("order_delivered_carrier_date") < parsed("order_purchase_timestamp")
    ).fillna(False)
    rejected_ids = set(
        orders.loc[delivered_without_date | carrier_before_purchase, "order_id"]
    )

    revenue = pd.to_numeric(items["price"]) + pd.to_numeric(items["freight_value"])
    total_revenue = float(revenue.sum())
    lost = items["order_id"].isin(rejected_ids)
    lost_revenue = float(revenue[lost].sum())

    payload: dict[str, Any] = {
        "total_raw_records": total_records,
        "files_counted": len(files),
        "rejected_orders": len(rejected_ids),
        "rejected_items": int(lost.sum()),
        "total_revenue": round(total_revenue, 2),
        "lost_revenue": round(lost_revenue, 2),
        "lost_share_pct": round(100.0 * lost_revenue / total_revenue, 4) if total_revenue else 0.0,
        "kept_revenue_expected": round(total_revenue - lost_revenue, 2),
    }

    if engine is not None:
        with engine.connect() as conn:
            loaded = conn.execute(
                text("SELECT round(sum(price + freight_value), 2) FROM olist.order_item")
            ).scalar_one()
        payload["kept_revenue_actual"] = float(loaded)
        payload["cross_check_matches"] = abs(float(loaded) - payload["kept_revenue_expected"]) < 0.01
    return payload


def noise_floor(engine: Engine, sql: str, *, runs: int = 7) -> dict[str, Any]:
    """Measure the same unchanged query twice, to bound what a delta can mean.

    Two consecutive measurements of an identical query differ by some amount
    that has nothing to do with any index.  That difference is the floor below
    which a "speed-up" is not a speed-up, and it is the honest yardstick for
    every index delta in this project.
    """
    with engine.connect() as conn:
        first = measure(conn, sql, runs=runs, warmup=2)
        second = measure(conn, sql, runs=runs, warmup=2)
    return {
        "runs_each": runs,
        "first_median_ms": round(first.median_ms, 2),
        "second_median_ms": round(second.median_ms, 2),
        "difference_ms": round(second.median_ms - first.median_ms, 2),
        "absolute_difference_ms": round(abs(second.median_ms - first.median_ms), 2),
        "spread_pct": round(
            100.0 * abs(second.median_ms - first.median_ms) / max(first.median_ms, 0.01), 1
        ),
        "first_p95_ms": round(first.p95_ms, 2),
        "second_p95_ms": round(second.p95_ms, 2),
    }


# ---------------------------------------------------------------------------
# 8. the textbook index that still does not rescue Q4
# ---------------------------------------------------------------------------

#: The covering index a textbook would prescribe for Q4: seek order_item by
#: seller, and read price/freight straight from the index leaf so the per-order
#: deduplication never touches the heap.
COVERING_INDEX = "ix_order_item_seller_order_cover"
COVERING_TARGET = (
    "olist.order_item (seller_id, order_id) INCLUDE (price, freight_value)"
)


def covering_index_probe(
    engine: Engine, query_sql: str, *, key: str = "q4"
) -> dict[str, Any]:
    """Build the textbook covering index, prove it is usable, and show the
    planner still refusing it.

    "Usable" is not a guess: after ``VACUUM ANALYZE`` the heap is fully
    all-visible, so an index-only scan really is *possible* here.  The plan is
    then captured with the index in place.  The index is dropped before the
    function returns, so the database ends in the no-index state the rest of
    the notebook reports.

    Returns a plain dict so the notebook can print it and the JSON dump can
    keep it.
    """
    result: dict[str, Any] = {"index": COVERING_INDEX, "target": COVERING_TARGET}

    # VACUUM cannot run inside a transaction block, so this connection is
    # autocommit.  CREATE INDEX is happy either way; it stays here so both
    # statements share one connection.
    with engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as conn:
        conn.execute(sqltext.drop_index(COVERING_INDEX, if_exists=True))
        conn.execute(sqltext.create_index(COVERING_INDEX, COVERING_TARGET))
        conn.execute(text("VACUUM ANALYZE olist.order_item"))

    try:
        with engine.connect() as conn:
            result["index_size"] = conn.execute(
                text("SELECT pg_size_pretty(pg_relation_size(CAST(:i AS regclass)))"),
                {"i": f"olist.{COVERING_INDEX}"},
            ).scalar_one()
            result["index_pages"] = conn.execute(
                text(
                    "SELECT relpages FROM pg_class WHERE relname = :n"
                ),
                {"n": COVERING_INDEX},
            ).scalar_one()
            heap_pages, heap_visible = conn.execute(
                text(
                    "SELECT relpages, relallvisible FROM pg_class "
                    "WHERE relname = 'order_item' AND relkind = 'r'"
                )
            ).one()
            result["heap_pages"] = int(heap_pages)
            result["heap_all_visible_pages"] = int(heap_visible)
            result["heap_all_visible_pct"] = round(
                100.0 * heap_visible / heap_pages, 1
            ) if heap_pages else None

            plan = explain(conn, query_sql, key=f"{key}-covering")
            result["execution_ms"] = round(plan.execution_ms, 2)
            result["seq_scans"] = sorted(set(plan.seq_scans))
            result["index_scans"] = sorted(set(plan.index_scans))
            result["used_the_index"] = any(
                "ix_order_item_seller_order_cover" in str(s)
                for s in plan.index_scans
            )
            result["plan_lines"] = [
                line for line in plan.text_plan.splitlines()
                if any(
                    token in line
                    for token in (
                        "Sort Method", "external merge", "Index Only Scan",
                        "Index Scan", "Seq Scan", "GroupAggregate",
                    )
                )
            ]
    finally:
        with engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as conn:
            conn.execute(sqltext.drop_index(COVERING_INDEX, if_exists=True))
    return result


# ---------------------------------------------------------------------------
# 9. reproducibility: what the same index measurement does when repeated
# ---------------------------------------------------------------------------

#: Every replication ever recorded, appended by the notebook and committed.
#: Committing the history is the point: a claim like "this delta does not
#: reproduce" can only be checked against the observations it is based on.
INDEX_HISTORY = "data/db/index_replications.json"


def index_replication_history(
    engine: Engine, *, runs: int = 5, path: str = INDEX_HISTORY,
    keep: int = 10
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run the index experiment twice, append both passes to a kept history,
    and summarise how far each (index, query) pair has moved over time.

    Returns ``(history, summary)``.  ``history`` is every observation currently
    kept; ``summary`` is one row per pair with the number of observations, the
    smallest and largest delta, and the span between them.  The span is the
    honest error bar of a single-run measurement.
    """
    from datetime import datetime, timezone

    observations: list[dict[str, Any]] = []
    if Path(path).exists():
        observations = json.loads(Path(path).read_text(encoding="utf-8"))

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for tag in ("pass-1", "pass-2"):
        for item in index_experiments(engine, runs=runs):
            for query, effect in item["effects"].items():
                observations.append({
                    "at": stamp,
                    "pass": tag,
                    "index": item["index"],
                    "query": query,
                    "delta_ms": round(float(effect["delta_ms"]), 2),
                })

    # Keep only the newest ``keep`` sessions so the file cannot grow forever.
    stamps = sorted({o["at"] for o in observations})[-keep:]
    observations = [o for o in observations if o["at"] in stamps]

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(observations, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    history = pd.DataFrame(observations)
    summary = (
        history.groupby(["index", "query"])["delta_ms"]
        .agg(observations="count", min_ms="min", max_ms="max")
        .assign(span_ms=lambda d: (d["max_ms"] - d["min_ms"]).round(2))
        .reset_index()
        .sort_values("span_ms", ascending=False)
    )
    return history, summary

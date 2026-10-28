"""One command: empty PostgreSQL -> complete, loaded, constrained database.

AGENTS.md 4.4 acceptance criterion 1.  Everything this needs is decided
elsewhere; this module only sequences it and measures it:

    ensure database -> apply sql/*.sql -> prepare rows -> load in FK order
                    -> record quarantine -> verify -> write report

The load runs inside a single transaction, so a failure at row 90,000 of
``order_item`` leaves the database exactly as empty as it was - the same
"no half-loaded state" rule the Stage 2 procedure has to satisfy.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Connection, text

from data_factory.db import ddl as ddl_module
from data_factory.db.connection import engine_scope, ensure_database, redact_url
from data_factory.db.factory_output import load_factory_output
from data_factory.db.loader import OLIST_LOAD_ORDER, count_rows, write_frame
from data_factory.db.olist import prepare
from data_factory.db.quarantine import Rejection, Repair, summarise, write_jsonl

DEFAULT_SOURCE_DIR = Path("data/raw/olist")
DEFAULT_CURATED = Path("data/curated/cases.csv")
DEFAULT_REPORT_DIR = Path("data/db")

# Foreign keys that must have zero orphans after the load.  Written out rather
# than discovered, so a missing check is visible in review.
FOREIGN_KEYS = (
    ("product", "category_pt", "product_category", "name_pt"),
    ("orders", "customer_id", "customer", "customer_id"),
    ("order_item", "order_id", "orders", "order_id"),
    ("order_item", "product_id", "product", "product_id"),
    ("order_item", "seller_id", "seller", "seller_id"),
    ("order_payment", "order_id", "orders", "order_id"),
    ("order_review", "order_id", "orders", "order_id"),
)


@dataclass
class LoadReport:
    url: str
    created_database: bool
    ddl: list[dict] = field(default_factory=list)
    tables: dict[str, int] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    rejections: list[dict] = field(default_factory=list)
    repairs: list[dict] = field(default_factory=list)
    checks: dict[str, object] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "created_database": self.created_database,
            "ddl": self.ddl,
            "tables": self.tables,
            "timings_seconds": {k: round(v, 3) for k, v in self.timings.items()},
            "rejections": self.rejections,
            "repairs": self.repairs,
            "checks": self.checks,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _verify(conn: Connection, report: LoadReport) -> None:
    """Prove the load is complete and connected, not just 'no exception'."""
    orphans: dict[str, int] = {}
    for child, column, parent, parent_column in FOREIGN_KEYS:
        query = text(
            f"SELECT count(*) FROM olist.{child} c "
            f"LEFT JOIN olist.{parent} p ON c.{column} = p.{parent_column} "
            f"WHERE c.{column} IS NOT NULL AND p.{parent_column} IS NULL"
        )
        orphans[f"{child}.{column} -> {parent}.{parent_column}"] = int(conn.execute(query).scalar_one())

    # Do the CHECK constraints actually bite?  Probe a few representative ones
    # inside savepoints and require each probe to be refused.
    probes = {
        "orders.delivered_without_date": (
            "INSERT INTO olist.orders (order_id, customer_id, status, purchase_ts, delivered_customer_ts) "
            "SELECT 'probe', customer_id, 'delivered', now()::timestamp, NULL FROM olist.customer LIMIT 1"
        ),
        "order_item.price_positive": (
            "INSERT INTO olist.order_item (order_id, item_seq, product_id, seller_id, shipping_limit_ts, price, freight_value) "
            "SELECT order_id, 9999, product_id, seller_id, now()::timestamp, -1, 0 FROM olist.order_item LIMIT 1"
        ),
        "order_review.score_range": (
            "INSERT INTO olist.order_review (review_id, order_id, score) "
            "SELECT 'probe', order_id, 6 FROM olist.orders LIMIT 1"
        ),
        "customer.state_uf": (
            "INSERT INTO olist.customer (customer_id, customer_unique_id, zip_code_prefix, city, state) "
            "VALUES ('probe', 'probe', '01037', 'x', 'ZZ')"
        ),
    }
    refused: dict[str, bool] = {}
    for name, statement in probes.items():
        nested = conn.begin_nested()
        try:
            conn.execute(text(statement))
            nested.rollback()
            refused[name] = False          # it was accepted: the constraint is missing
        except Exception:
            nested.rollback()
            refused[name] = True

    report.checks = {
        "fk_orphans": orphans,
        "fk_all_zero": all(value == 0 for value in orphans.values()),
        "constraints_refused_probe": refused,
        "constraints_all_enforced": all(refused.values()),
    }


def bootstrap(
    url: str,
    *,
    source_dir: Path = DEFAULT_SOURCE_DIR,
    curated_path: Path = DEFAULT_CURATED,
    report_dir: Path = DEFAULT_REPORT_DIR,
    load_olist: bool = True,
    write_report: bool = True,
) -> LoadReport:
    """Rebuild the whole Stage 2 database and return the evidence."""
    report = LoadReport(url=redact_url(url), created_database=False, started_at=_now())
    wall_start = time.perf_counter()

    report.created_database = ensure_database(url)

    files = ddl_module.load_sql_files()
    with engine_scope(url) as engine:
        # --- DDL -------------------------------------------------------
        start = time.perf_counter()
        with engine.begin() as conn:
            for sql_file in files:
                ddl_module.apply_file(conn, sql_file)
            # ddl_history is created by the first file, so record afterwards
            for sql_file in files:
                ddl_module.record_applied(conn, sql_file)
                report.ddl.append({"file": sql_file.name, "sha256": sql_file.sha256})
        report.timings["ddl"] = time.perf_counter() - start

        # --- row preparation (pandas, outside any transaction) ----------
        prepared = None
        if load_olist:
            start = time.perf_counter()
            prepared = prepare(source_dir)
            report.timings["prepare"] = time.perf_counter() - start

        # --- loading ---------------------------------------------------
        with engine.begin() as conn:
            if prepared is not None:
                for table in OLIST_LOAD_ORDER:
                    frame = getattr(prepared, table)
                    start = time.perf_counter()
                    rows = write_frame(conn, frame, table)
                    report.timings[f"load:{table}"] = time.perf_counter() - start
                    report.tables[f"olist.{table}"] = rows

                start = time.perf_counter()
                factory_result = load_factory_output(conn, curated_path)
                report.timings["load:factory.case_file"] = time.perf_counter() - start
                report.tables["factory.case_file"] = factory_result.rows

                # Fresh statistics before anything is measured against this
                # database: without this the planner works from the empty-table
                # estimates it had at CREATE time.  The difference was measured
                # at 167 ms -> 128 ms on Q6, which is what made a partial index
                # look useful in an earlier experiment.
                start = time.perf_counter()
                conn.execute(text("ANALYZE"))
                report.timings["analyze"] = time.perf_counter() - start

                # Build the precomputed dashboard panels in the same
                # transaction as the load: a half-loaded database never gets a
                # snapshot that describes it.
                start = time.perf_counter()
                conn.execute(text("CALL olist.refresh_dashboard_snapshot(NULL)"))
                report.timings["snapshot_refresh"] = time.perf_counter() - start

                for rejection in prepared.rejections:
                    conn.execute(
                        text(
                            "INSERT INTO olist.load_rejection "
                            "(source_file, reason, row_count, detail) "
                            "VALUES (:source_file, :reason, :row_count, :detail)"
                        ),
                        rejection.as_row(),
                    )

        # --- verification (read-only) ----------------------------------
        with engine.connect() as conn:
            _verify(conn, report)
            verified = {
                f"olist.{table}": count_rows(conn, table) for table in OLIST_LOAD_ORDER
            }
            verified["factory.case_file"] = count_rows(conn, "case_file", schema="factory")
            for snapshot in ("revenue_snapshot", "seller_leaderboard"):
                verified[f"olist.{snapshot}"] = count_rows(conn, snapshot)
        report.checks["row_counts"] = verified

    report.timings["total"] = time.perf_counter() - wall_start
    report.finished_at = _now()

    if prepared is not None:
        report.rejections = [r.as_row() for r in prepared.rejections]
        report.repairs = [r.as_row() for r in prepared.repairs]
        report.checks["quarantine"] = summarise(prepared.rejections, prepared.repairs)

    if write_report:
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "load_report.json").write_text(
            json.dumps(report.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if prepared is not None:
            write_jsonl(report_dir / "load_quarantine.jsonl", prepared.rejections)

    return report

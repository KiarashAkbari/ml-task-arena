"""Data-layer tests for the Stage 2 database.

These tests talk to a real PostgreSQL, because the things being tested are
PostgreSQL behaviours: what a function returns, whether a procedure is
idempotent, whether a failed refresh leaves a half-built snapshot.  A mock
would assert that we wrote the SQL we wrote.

They run offline and are skipped when ``DATABASE_URL`` is not set, so
``python -m unittest discover -s tests`` stays green on a machine with no
database (the Stage 1 suite is unaffected).

    DATABASE_URL=postgresql+psycopg://user:secret@localhost:5432/olist \
        python -m unittest tests.test_db -v

Every test that writes data does so inside a transaction it rolls back, or
targets rows it creates and removes itself, so running the suite does not
damage a loaded database.
"""

from __future__ import annotations

import os
import unittest
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

DATABASE_URL = os.environ.get("DATABASE_URL")

requires_db = unittest.skipUnless(
    DATABASE_URL, "DATABASE_URL is not set - database tests skipped"
)


@requires_db
class DatabaseTestCase(unittest.TestCase):
    """One engine per class; connections are always closed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        # Fail fast and loudly if the database has not been bootstrapped.
        with self.engine.connect() as conn:
            loaded = conn.execute(text("SELECT count(*) FROM olist.orders")).scalar_one()
        if not loaded:
            self.skipTest("olist schema is empty - run `data-factory db bootstrap` first")


class FunctionTests(DatabaseTestCase):
    """AGENTS.md 4.3.9: the function's returned value is correct."""

    def test_delay_bucket_boundaries(self) -> None:
        """Every boundary, on both sides, including the inclusive edges."""
        cases = [
            ("-5", "on_time_or_early"),
            ("0", "on_time_or_early"),
            ("1", "up_to_3_days_late"),
            ("3", "up_to_3_days_late"),
            ("4", "up_to_7_days_late"),
            ("7", "up_to_7_days_late"),
            ("8", "up_to_14_days_late"),
            ("14", "up_to_14_days_late"),
            ("15", "more_than_14_days_late"),
            ("400", "more_than_14_days_late"),
        ]
        with self.engine.connect() as conn:
            for delay, expected in cases:
                with self.subTest(delay=delay):
                    got = conn.execute(
                        text("SELECT olist.delay_bucket(CAST(:d AS numeric))"),
                        {"d": delay},
                    ).scalar_one()
                    self.assertEqual(got, expected)

    def test_delay_bucket_is_strict(self) -> None:
        """NULL in, NULL out - a missing delay must not become 'on time'."""
        with self.engine.connect() as conn:
            got = conn.execute(
                text("SELECT olist.delay_bucket(NULL::numeric)")
            ).scalar_one()
        self.assertIsNone(got)

    def test_order_delay_days_on_known_rows(self) -> None:
        """The scalar function agrees with the expression it wraps."""
        with self.engine.connect() as conn:
            mismatches = conn.execute(text("""
                SELECT count(*)
                FROM olist.orders o
                WHERE o.status = 'delivered'
                  AND o.delivered_customer_ts IS NOT NULL
                  AND olist.order_delay_days(o.order_id)
                      IS DISTINCT FROM
                      (o.delivered_customer_ts::date - o.estimated_delivery_ts::date)::numeric
            """)).scalar_one()
        self.assertEqual(mismatches, 0)

    def test_order_delay_days_returns_null_when_undelivered(self) -> None:
        with self.engine.connect() as conn:
            order_id = conn.execute(text("""
                SELECT order_id FROM olist.orders
                WHERE delivered_customer_ts IS NULL LIMIT 1
            """)).scalar_one_or_none()
        if order_id is None:
            self.skipTest("no undelivered order in this database")
        with self.engine.connect() as conn:
            got = conn.execute(
                text("SELECT olist.order_delay_days(:o)"), {"o": order_id}
            ).scalar_one()
        self.assertIsNone(got)


class ControllerTests(DatabaseTestCase):
    """AGENTS.md 4.3.9: the procedure is idempotent and never leaves half state."""

    def test_procedure_is_idempotent_for_one_month(self) -> None:
        """Calling it twice for the same month leaves exactly the same rows."""
        month = "2017-05-01"
        with self.engine.connect() as conn:
            before = conn.execute(text(
                "SELECT count(*) FROM olist.revenue_snapshot WHERE purchase_month = :m"
            ), {"m": month}).scalar_one()

        with self.engine.begin() as conn:
            conn.execute(text("CALL olist.refresh_dashboard_snapshot(:m)"), {"m": month})
        with self.engine.connect() as conn:
            after_first = conn.execute(text(
                "SELECT count(*) FROM olist.revenue_snapshot WHERE purchase_month = :m"
            ), {"m": month}).scalar_one()

        with self.engine.begin() as conn:
            conn.execute(text("CALL olist.refresh_dashboard_snapshot(:m)"), {"m": month})
        with self.engine.connect() as conn:
            after_second = conn.execute(text(
                "SELECT count(*) FROM olist.revenue_snapshot WHERE purchase_month = :m"
            ), {"m": month}).scalar_one()

        self.assertGreater(after_first, 0)
        self.assertEqual(after_first, before, "first refresh changed the row count")
        self.assertEqual(after_second, after_first, "second refresh duplicated rows")

    def test_procedure_rolls_back_completely_on_midway_failure(self) -> None:
        """A failure after the DELETE must not leave the month empty.

        Simulated by making the INSERT half fail: a BEFORE INSERT trigger
        rejects one row.  The procedure deletes the month, then the insert
        raises, so the caller's transaction aborts - and the delete must go
        with it.
        """
        month = "2017-06-01"
        with self.engine.connect() as conn:
            before = conn.execute(text(
                "SELECT category, revenue FROM olist.revenue_snapshot "
                "WHERE purchase_month = :m ORDER BY category"
            ), {"m": month}).fetchall()
        if not before:
            self.skipTest("no snapshot rows for the fixture month")

        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE OR REPLACE FUNCTION olist._fail_on_insert() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'simulated mid-refresh failure';
                END
                $$;
                CREATE TRIGGER _test_fail BEFORE INSERT ON olist.revenue_snapshot
                    FOR EACH ROW EXECUTE FUNCTION olist._fail_on_insert();
            """))

        try:
            with self.engine.connect() as conn:
                transaction = conn.begin()
                with self.assertRaises(DBAPIError):
                    conn.execute(
                        text("CALL olist.refresh_dashboard_snapshot(:m)"), {"m": month}
                    )
                transaction.rollback()
        finally:
            with self.engine.begin() as conn:
                conn.execute(text(
                    "DROP TRIGGER IF EXISTS _test_fail ON olist.revenue_snapshot"))
                conn.execute(text("DROP FUNCTION IF EXISTS olist._fail_on_insert()"))

        with self.engine.connect() as conn:
            after = conn.execute(text(
                "SELECT category, revenue FROM olist.revenue_snapshot "
                "WHERE purchase_month = :m ORDER BY category"
            ), {"m": month}).fetchall()

        self.assertEqual(before, after, "a failed refresh left the month without its rows")

    def test_snapshot_matches_the_live_view_for_one_month(self) -> None:
        """The precomputed answer must equal the computed one, row for row."""
        month = "2018-01-01"
        with self.engine.connect() as conn:
            live = conn.execute(text("""
                SELECT purchase_month, category, revenue, orders
                FROM olist.v_revenue_by_month_category
                WHERE purchase_month = :m
                ORDER BY category
            """), {"m": month}).fetchall()
            snapshot = conn.execute(text("""
                SELECT purchase_month, category, revenue, orders
                FROM olist.revenue_snapshot
                WHERE purchase_month = :m
                ORDER BY category
            """), {"m": month}).fetchall()
        self.assertGreater(len(live), 0)
        self.assertEqual(live, snapshot)


class QueryCorrectnessTests(DatabaseTestCase):
    """AGENTS.md 4.3.9: analytical results are right on a small, controlled set.

    The whole schema is emptied and refilled inside a transaction that is
    always rolled back, so the real data is untouched.  Hand-computed
    expectations, not values copied from a previous run.
    """

    FIXTURE = """
        INSERT INTO olist.customer
            (customer_id, customer_unique_id, zip_code_prefix, city, state) VALUES
            ('c1', 'u1', '01001', 'sao paulo', 'SP'),
            ('c2', 'u2', '20001', 'rio',       'RJ'),
            ('c3', 'u3', '30001', 'bh',        'MG');

        INSERT INTO olist.orders
            (order_id, customer_id, status, purchase_ts, delivered_customer_ts,
             estimated_delivery_ts) VALUES
            ('o1', 'c1', 'delivered', '2017-05-01 10:00', '2017-05-04 09:00', '2017-05-05 00:00'),
            ('o2', 'c2', 'delivered', '2017-05-02 10:00', '2017-05-10 09:00', '2017-05-05 00:00'),
            ('o3', 'c3', 'delivered', '2017-05-03 10:00', '2017-05-20 09:00', '2017-05-05 00:00'),
            ('o4', 'c1', 'canceled',  '2017-05-04 10:00', NULL,                  '2017-05-20 00:00');

        INSERT INTO olist.order_review
            (review_id, order_id, score, comment_title, comment_message,
             creation_ts, answer_ts) VALUES
            ('r1', 'o1', 5, NULL, NULL, '2017-05-04 10:00', '2017-05-04 11:00'),
            ('r2', 'o2', 3, NULL, NULL, '2017-05-10 10:00', '2017-05-10 11:00'),
            ('r3', 'o3', 1, NULL, NULL, '2017-05-20 10:00', '2017-05-20 11:00');
    """

    def setUp(self) -> None:
        super().setUp()
        self.conn = self.engine.connect()
        self.transaction = self.conn.begin()
        # TRUNCATE ... CASCADE is transactional in PostgreSQL, so the rollback
        # in tearDown restores every row.
        self.conn.execute(text("TRUNCATE olist.order_review, olist.order_item, "
                               "olist.order_payment, olist.orders, olist.customer CASCADE"))
        self.conn.execute(text(self.FIXTURE))

    def tearDown(self) -> None:
        self.transaction.rollback()
        self.conn.close()

    def test_delay_buckets_on_controlled_rows(self) -> None:
        """o1 is 1 day early, o2 is 5 days late, o3 is 15 days late."""
        rows = self.conn.execute(text("""
            SELECT o.order_id, olist.delay_bucket(
                       (o.delivered_customer_ts::date - o.estimated_delivery_ts::date)::numeric)
            FROM olist.orders o WHERE o.order_id LIKE 'o%' AND o.status = 'delivered'
            ORDER BY o.order_id
        """)).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("o1", "on_time_or_early"),
             ("o2", "up_to_7_days_late"),
             ("o3", "more_than_14_days_late")],
        )

    def test_late_share_is_computed_from_dates_not_instants(self) -> None:
        """One of three delivered orders is late; the promised day itself is not late."""
        late_pct = self.conn.execute(text("""
            SELECT round(100.0 * count(*) FILTER (WHERE delay_days > 0) / count(*), 2)
            FROM olist.v_delivered_order
        """)).scalar_one()
        self.assertEqual(Decimal(str(late_pct)), Decimal("66.67"))

    def test_canceled_orders_are_not_sales(self) -> None:
        """v_valid_order hides the canceled one; the raw table still has four."""
        self.assertEqual(
            self.conn.execute(text("SELECT count(*) FROM olist.orders")).scalar_one(), 4)
        self.assertEqual(
            self.conn.execute(text("SELECT count(*) FROM olist.v_valid_order")).scalar_one(), 3)

    def test_review_buckets_average_the_right_scores(self) -> None:
        """Scores 5 / 3 / 1 land in three different buckets, each averaging itself."""
        rows = self.conn.execute(text("""
            SELECT olist.delay_bucket(v.delay_days::numeric) AS bucket, avg(r.score)
            FROM olist.v_delivered_order v
            JOIN olist.order_review r ON r.order_id = v.order_id
            GROUP BY 1 ORDER BY 1
        """)).fetchall()
        self.assertEqual(
            [(row[0], Decimal(str(row[1]))) for row in rows],
            [("more_than_14_days_late", Decimal("1")),
             ("on_time_or_early", Decimal("5")),
             ("up_to_7_days_late", Decimal("3"))],
        )

    def test_payment_aggregation_on_controlled_rows(self) -> None:
        """Two payments on one order are two rows, and the share is of payments."""
        self.conn.execute(text("""
            INSERT INTO olist.order_payment
                (order_id, payment_seq, payment_type, installments, payment_value) VALUES
                ('o1', 1, 'credit_card', 2, 100.00),
                ('o1', 2, 'voucher',     1,  50.00)
        """))
        rows = self.conn.execute(text("""
            SELECT payment_type, count(*), round(avg(payment_value), 2)
            FROM olist.order_payment GROUP BY 1 ORDER BY 1
        """)).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("credit_card", 1, Decimal("100.00")), ("voucher", 1, Decimal("50.00"))],
        )


@requires_db
class ChainTests(unittest.TestCase):
    """The Stage 1 output really is in the same database, in its own schema."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(DATABASE_URL, future=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def test_factory_schema_is_loaded_and_separate(self) -> None:
        with self.engine.connect() as conn:
            rows = conn.execute(text("SELECT count(*) FROM factory.case_file")).scalar_one()
            schemas = {
                row[0] for row in conn.execute(text(
                    "SELECT table_schema FROM information_schema.tables "
                    "WHERE table_schema IN ('olist', 'factory')"
                )).fetchall()
            }
        self.assertGreater(rows, 0, "factory.case_file is empty")
        self.assertEqual(schemas, {"olist", "factory"})


if __name__ == "__main__":
    unittest.main()

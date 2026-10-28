-- =====================================================================
-- 06_procedure.sql - refreshing the precomputed panels
--
-- Contract this procedure keeps, and why each clause is there:
--
--   parameterised   p_month rebuilds exactly one month; NULL rebuilds all of
--                   them plus the global leaderboard.  The dashboard's
--                   nightly job calls it per month, the bootstrap calls it
--                   once with NULL.
--
--   idempotent      every target is DELETE-then-INSERT for the same key, so
--                   running it twice for 2017-05 leaves 2017-05 with the same
--                   rows, not twice the rows.  (Tested in tests/test_db.py by
--                   calling it twice and comparing counts.)
--
--   no half state   the procedure never COMMITs internally.  A failure at any
--                   statement aborts the caller's transaction, so the database
--                   is left exactly as it was - the "defined behaviour on a
--                   mid-way error" AGENTS.md 4.3.7 asks for, and the same rule
--                   the Stage 1 factory applies to a partial fetch.
--
-- Why a procedure and not a function: CALL manages its own execution and may
-- control transactions; a function returns a value and runs inside the
-- caller's statement.  The design note is in the report, next to the code.
-- =====================================================================

CREATE OR REPLACE PROCEDURE olist.refresh_dashboard_snapshot(p_month date DEFAULT NULL)
LANGUAGE plpgsql
AS $$
DECLARE
    v_started timestamp := clock_timestamp();
BEGIN
    IF p_month IS NULL THEN
        -- Full rebuild.  The leaderboard is global, so it is only recomputed
        -- in this mode: a single month cannot change the top-20 ordering
        -- without the other months' revenue.
        DELETE FROM olist.revenue_snapshot;
        DELETE FROM olist.seller_leaderboard;
    ELSE
        DELETE FROM olist.revenue_snapshot WHERE purchase_month = p_month;
    END IF;

    INSERT INTO olist.revenue_snapshot (purchase_month, category, revenue, orders)
    SELECT v.purchase_month, v.category, v.revenue, v.orders
    FROM olist.v_revenue_by_month_category v
    WHERE p_month IS NULL OR v.purchase_month = p_month;

    IF p_month IS NULL THEN
        -- Q4's leaderboard.  Preparation time is averaged over DISTINCT
        -- (seller, order) pairs so an order with three items from one seller
        -- does not count three times.
        WITH seller_revenue AS (
            SELECT i.seller_id,
                   sum(i.price + i.freight_value) AS revenue,
                   count(DISTINCT i.order_id)     AS orders
            FROM olist.order_item i
            JOIN olist.v_valid_order v ON v.order_id = i.order_id
            GROUP BY 1
        ),
        ranked AS (
            SELECT seller_id, revenue, orders,
                   row_number() OVER (ORDER BY revenue DESC) AS rank
            FROM seller_revenue
            ORDER BY revenue DESC
            LIMIT 20
        ),
        prep AS (
            SELECT seller_id, avg(hours) AS avg_prep_hours
            FROM (
                SELECT DISTINCT i.seller_id,
                       i.order_id,
                       EXTRACT(EPOCH FROM (v.delivered_carrier_ts - v.purchase_ts)) / 3600.0 AS hours
                FROM olist.order_item i
                JOIN olist.v_valid_order v ON v.order_id = i.order_id
                WHERE v.delivered_carrier_ts IS NOT NULL
                  AND i.seller_id IN (SELECT seller_id FROM ranked)
            ) per_order
            GROUP BY 1
        ),
        scores AS (
            SELECT i.seller_id, avg(r.score) AS avg_score
            FROM olist.order_item i
            JOIN olist.order_review r ON r.order_id = i.order_id
            WHERE i.seller_id IN (SELECT seller_id FROM ranked)
            GROUP BY 1
        )
        INSERT INTO olist.seller_leaderboard
            (rank, seller_id, revenue, orders, avg_prep_hours, avg_score)
        SELECT r.rank, r.seller_id, r.revenue, r.orders,
               round(p.avg_prep_hours, 2), round(s.avg_score, 3)
        FROM ranked r
        LEFT JOIN prep   p ON p.seller_id = r.seller_id
        LEFT JOIN scores s ON s.seller_id = r.seller_id;
    END IF;

    RAISE NOTICE 'refresh_dashboard_snapshot(%): done in % ms',
        COALESCE(p_month::text, 'ALL'), round(EXTRACT(EPOCH FROM (clock_timestamp() - v_started)) * 1000);
END
$$;

COMMENT ON PROCEDURE olist.refresh_dashboard_snapshot(date) IS
    'Rebuild the precomputed dashboard panels. p_month = one month, NULL = all '
    'months plus the seller leaderboard. Idempotent and atomic.';

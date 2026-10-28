-- =====================================================================
-- 04_functions.sql - a scalar function usable inside SELECT
--
-- delay_bucket() exists because the same five-way classification was needed
-- in Q3 and in the snapshot refresh.  Two copies of a CASE expression is two
-- places for the boundaries to drift apart.  As a function it is:
--
--   * callable in SELECT, WHERE and GROUP BY,
--   * IMMUTABLE, so the planner may inline or cache it and an expression
--     index on it would be legal if it ever paid for itself,
--   * STRICT, so a NULL delay yields NULL rather than silently becoming
--     "on time" - the failure mode a CASE with no NULL guard would have.
--
-- The Stage 2 report contrasts this with the procedure in 06: a function
-- returns a value and runs inside the caller's transaction; the procedure
-- controls its own and can therefore promise "all or nothing" across several
-- statements.
-- =====================================================================

CREATE OR REPLACE FUNCTION olist.delay_bucket(p_delay_days numeric)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    SELECT CASE
        WHEN p_delay_days <= 0  THEN 'on_time_or_early'
        WHEN p_delay_days <= 3  THEN 'up_to_3_days_late'
        WHEN p_delay_days <= 7  THEN 'up_to_7_days_late'
        WHEN p_delay_days <= 14 THEN 'up_to_14_days_late'
        ELSE                         'more_than_14_days_late'
    END
$$;

COMMENT ON FUNCTION olist.delay_bucket(numeric) IS
    'Classify a delivery delay (days, negative = early) into a report bucket. '
    'STRICT: NULL in, NULL out.';

-- A second function that answers a question the dashboard asks in prose:
-- "how late is late, in days, for this order?"  Returns NULL when the order
-- has no delivery date, which is why the dashboard never shows it as 0.
CREATE OR REPLACE FUNCTION olist.order_delay_days(p_order_id text)
RETURNS numeric
LANGUAGE sql
STABLE
AS $$
    SELECT (delivered_customer_ts::date - estimated_delivery_ts::date)::numeric
    FROM olist.orders
    WHERE order_id = p_order_id
$$;

COMMENT ON FUNCTION olist.order_delay_days(text) IS
    'Delay in whole days for one order; NULL when undelivered or unknown.';

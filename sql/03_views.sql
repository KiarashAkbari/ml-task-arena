-- =====================================================================
-- 03_views.sql - one definition per repeated decision
--
-- Three views, each earning its place differently:
--   v_valid_order              single table, hides the "is this a sale?" filter
--   v_delivered_order          multi-table, holds the join everyone repeats
--   v_revenue_by_month_category the heaviest analysis, as a LIVE view
--
-- The last one is deliberately the slowest object in the schema: it is the
-- "compute every time" half of the view-versus-snapshot comparison required
-- by AGENTS.md 4.3.6.  Keeping it expensive is the point.
-- =====================================================================

-- Simple view: one table, one recurring filter.
-- Cancelled and unavailable orders never became sales; before this view that
-- sentence lived in three different queries and could drift in one of them.
CREATE OR REPLACE VIEW olist.v_valid_order AS
SELECT order_id,
       customer_id,
       status,
       purchase_ts,
       date_trunc('month', purchase_ts)::date AS purchase_month,
       delivered_carrier_ts,
       delivered_customer_ts,
       estimated_delivery_ts
FROM olist.orders
WHERE status NOT IN ('canceled', 'unavailable');

COMMENT ON VIEW olist.v_valid_order IS
    'Orders that became sales: excludes canceled and unavailable. All revenue '
    'and seller queries read this view instead of repeating the filter.';

-- Multi-table view: the join that Q2 and Q6 both need, written once.
-- delay_days is positive when late, negative when early, and compared at
-- date level because the promise is a date rather than an instant.
CREATE OR REPLACE VIEW olist.v_delivered_order AS
SELECT o.order_id,
       c.customer_unique_id,
       c.state,
       o.purchase_ts,
       date_trunc('month', o.purchase_ts)::date AS purchase_month,
       o.delivered_customer_ts,
       o.estimated_delivery_ts,
       (o.delivered_customer_ts::date - o.estimated_delivery_ts::date) AS delay_days
FROM olist.orders o
JOIN olist.customer c ON c.customer_id = o.customer_id
WHERE o.status = 'delivered';

COMMENT ON VIEW olist.v_delivered_order IS
    'Delivered orders joined to their customer, with delay in whole days. '
    'Only delivered orders have an observable delivery date; the rest are not '
    '"on time", they are unmeasured.';

-- The heaviest analysis, kept as a live view on purpose (see 06_procedure).
CREATE OR REPLACE VIEW olist.v_revenue_by_month_category AS
SELECT o.purchase_month,
       COALESCE(pc.name_en, p.category_pt, 'unknown') AS category,
       sum(i.price + i.freight_value)                 AS revenue,
       count(DISTINCT o.order_id)                     AS orders
FROM olist.order_item i
JOIN olist.v_valid_order o ON o.order_id   = i.order_id
JOIN olist.product       p ON p.product_id = i.product_id
LEFT JOIN olist.product_category pc ON pc.name_pt = p.category_pt
GROUP BY 1, 2;

COMMENT ON VIEW olist.v_revenue_by_month_category IS
    'Q1 computed live on every call: 1,282 rows out of 112,455 item rows. '
    'Measured against olist.revenue_snapshot in the notebook - this view is the '
    'price of freshness, the snapshot is the price of staleness.';

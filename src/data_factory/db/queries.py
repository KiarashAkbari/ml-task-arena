"""The dashboard's analytical queries, as data - not as strings in a notebook.

Each entry carries two forms and the *decision* it encodes:

``live_sql``    computed from base tables or views on every call.  This is the
                before-side of the optimisation cycle.
``served_sql``  what the dashboard actually runs.  For three panels that is a
                precomputed snapshot (measured, not assumed); for the other
                three it is the same live query, because it already fits.

``served_from`` says which, so a reader never has to guess whether a number on
screen is fresh or as-of the last refresh.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Query:
    key: str
    title: str
    decision: str
    live_sql: str
    served_sql: str
    served_from: str  # "snapshot" | "view" | "live"


Q1_BASKET_REVENUE = Query(
    key="q1",
    title="Q1 - basket and revenue, per month and product category",
    decision=(
        "Revenue is sum(price + freight) at item level: payments are recorded per "
        "order and cannot be split by category. Cancelled and unavailable orders are "
        "excluded by reading v_valid_order. An order spanning two categories counts "
        "once in each category's order count, so those counts sum above the number "
        "of orders. Served from a snapshot because the live view costs 298 ms."
    ),
    live_sql="""
        SELECT purchase_month, category, revenue, orders,
               round(revenue / orders, 2) AS avg_order_value
        FROM olist.v_revenue_by_month_category
        ORDER BY 1, 2
    """,
    served_sql="""
        SELECT purchase_month, category, revenue, orders,
               round(revenue / orders, 2) AS avg_order_value
        FROM olist.revenue_snapshot
        ORDER BY 1, 2
    """,
    served_from="snapshot",
)

Q2_LATE_BY_STATE = Query(
    key="q2",
    title="Q2 - late-delivery share, per customer state and month",
    decision=(
        "Lateness is compared at date level (delivered day > promised day), because "
        "the promise is a date: an order delivered at 18:00 on the promised day is "
        "not late. Only delivered orders have an observable delivery date, so the "
        "rest are unmeasured rather than counted as on time. Reads "
        "v_delivered_order, which holds the join once."
    ),
    live_sql="""
        SELECT state,
               purchase_month                                        AS month,
               count(*)                                             AS delivered,
               count(*) FILTER (WHERE delay_days > 0)               AS late,
               round(100.0 * count(*) FILTER (WHERE delay_days > 0)
                     / count(*), 2)                                 AS late_pct
        FROM olist.v_delivered_order
        GROUP BY 1, 2
        ORDER BY 1, 2
    """,
    served_sql="""
        SELECT state,
               purchase_month                                        AS month,
               count(*)                                             AS delivered,
               count(*) FILTER (WHERE delay_days > 0)               AS late,
               round(100.0 * count(*) FILTER (WHERE delay_days > 0)
                     / count(*), 2)                                 AS late_pct
        FROM olist.v_delivered_order
        GROUP BY 1, 2
        ORDER BY 1, 2
    """,
    served_from="view",
)

Q3_DELAY_VS_SCORE = Query(
    key="q3",
    title="Q3 - delivery delay against review score",
    decision=(
        "Delay is (delivered - promised) in whole days, so negative means early. "
        "Reviews are counted per review row, not per order: 547 orders carry more than "
        "one review and each is a separate opinion. The bucket boundaries also exist "
        "as olist.delay_bucket(), but that function is not inlined by the planner "
        "here and made this query 2.6x slower (352 ms vs 136 ms), so the hot path "
        "writes the boundaries inline and a test pins the two together. This query "
        "reads orders, not the customer view, because it does not need the state."
    ),

    live_sql="""
        SELECT CASE
                   WHEN o.delivered_customer_ts::date - o.estimated_delivery_ts::date <= 0
                       THEN 'on_time_or_early'
                   WHEN o.delivered_customer_ts::date - o.estimated_delivery_ts::date <= 3
                       THEN 'up_to_3_days_late'
                   WHEN o.delivered_customer_ts::date - o.estimated_delivery_ts::date <= 7
                       THEN 'up_to_7_days_late'
                   WHEN o.delivered_customer_ts::date - o.estimated_delivery_ts::date <= 14
                       THEN 'up_to_14_days_late'
                   ELSE 'more_than_14_days_late'
               END                                                    AS bucket,
               count(*)                                               AS reviews,
               round(avg(r.score), 3)                                 AS avg_score,
               round(100.0 * count(*) FILTER (WHERE r.score <= 2)
                     / count(*), 2)                                   AS pct_score_1_2,
               min(o.delivered_customer_ts::date
                   - o.estimated_delivery_ts::date)                    AS sort_key
        FROM olist.orders o
        JOIN olist.order_review r ON r.order_id = o.order_id
        WHERE o.status = 'delivered'
          AND o.delivered_customer_ts IS NOT NULL
        GROUP BY 1
        ORDER BY sort_key
    """,
    served_sql="""
        SELECT CASE
                   WHEN o.delivered_customer_ts::date - o.estimated_delivery_ts::date <= 0
                       THEN 'on_time_or_early'
                   WHEN o.delivered_customer_ts::date - o.estimated_delivery_ts::date <= 3
                       THEN 'up_to_3_days_late'
                   WHEN o.delivered_customer_ts::date - o.estimated_delivery_ts::date <= 7
                       THEN 'up_to_7_days_late'
                   WHEN o.delivered_customer_ts::date - o.estimated_delivery_ts::date <= 14
                       THEN 'up_to_14_days_late'
                   ELSE 'more_than_14_days_late'
               END                                                    AS bucket,
               count(*)                                               AS reviews,
               round(avg(r.score), 3)                                 AS avg_score,
               round(100.0 * count(*) FILTER (WHERE r.score <= 2)
                     / count(*), 2)                                   AS pct_score_1_2
        FROM olist.orders o
        JOIN olist.order_review r ON r.order_id = o.order_id
        WHERE o.status = 'delivered'
          AND o.delivered_customer_ts IS NOT NULL
        GROUP BY 1
        ORDER BY min(o.delivered_customer_ts::date - o.estimated_delivery_ts::date)
    """,
    served_from="live",
)

Q4_TOP_SELLERS = Query(
    key="q4",
    title="Q4 - top 20 sellers by revenue, with preparation time and average score",
    decision=(
        "Preparation time is carrier handover minus purchase, averaged over DISTINCT "
        "(seller, order) pairs so a seller is not weighted by how many items an order "
        "contained. That deduplication forces a 99k-row sort, which no index removed "
        "on this data - the live form bottoms out at 211 ms, so the leaderboard is "
        "precomputed and the dashboard shows its computed_at."
    ),
    live_sql="""
        WITH per_seller_order AS (
            SELECT i.seller_id, i.order_id,
                   sum(i.price + i.freight_value) AS order_revenue,
                   min(v.delivered_carrier_ts - v.purchase_ts) AS prep_interval
            FROM olist.order_item i
            JOIN olist.v_valid_order v ON v.order_id = i.order_id
            GROUP BY 1, 2
        ),
        seller AS (
            SELECT seller_id,
                   sum(order_revenue) AS revenue,
                   count(*)           AS orders,
                   avg(EXTRACT(EPOCH FROM prep_interval) / 3600.0) AS avg_prep_hours
            FROM per_seller_order
            GROUP BY 1
        ),
        scores AS (
            SELECT i.seller_id, avg(r.score) AS avg_score
            FROM olist.order_item i
            JOIN olist.order_review r ON r.order_id = i.order_id
            GROUP BY 1
        )
        SELECT s.seller_id,
               round(s.revenue, 2)        AS revenue,
               s.orders,
               round(s.avg_prep_hours, 2) AS avg_prep_hours,
               round(sc.avg_score, 3)     AS avg_score
        FROM seller s
        LEFT JOIN scores sc ON sc.seller_id = s.seller_id
        ORDER BY revenue DESC
        LIMIT 20
    """,
    served_sql="""
        SELECT rank, seller_id, revenue, orders, avg_prep_hours, avg_score, computed_at
        FROM olist.seller_leaderboard
        ORDER BY rank
    """,
    served_from="snapshot",
)

Q5_PAYMENTS = Query(
    key="q5",
    title="Q5 - payment method and instalments against order value",
    decision=(
        "The unit of analysis is the payment row, not the order: one order can be "
        "paid with two vouchers plus a card. 'not_defined' stays its own category "
        "rather than being merged into credit_card, because inventing a method is "
        "worse than leaving three rows unexplained. Single table, already 23 ms, so "
        "no snapshot and no index."
    ),
    live_sql="""
        SELECT payment_type,
               count(*)                                            AS payments,
               round(100.0 * count(*) / sum(count(*)) OVER (), 2)   AS share_pct,
               round(avg(installments), 2)                          AS avg_installments,
               round(avg(payment_value), 2)                         AS avg_value,
               round(sum(payment_value), 2)                         AS total_value,
               round(corr(installments, payment_value)::numeric, 4)  AS corr_installments_value
        FROM olist.order_payment
        GROUP BY 1
        ORDER BY payments DESC
    """,
    served_sql="""
        SELECT payment_type,
               count(*)                                            AS payments,
               round(100.0 * count(*) / sum(count(*)) OVER (), 2)   AS share_pct,
               round(avg(installments), 2)                          AS avg_installments,
               round(avg(payment_value), 2)                         AS avg_value,
               round(sum(payment_value), 2)                         AS total_value,
               round(corr(installments, payment_value)::numeric, 4)  AS corr_installments_value
        FROM olist.order_payment
        GROUP BY 1
        ORDER BY payments DESC
    """,
    served_from="live",
)

Q6_PERCENTILES = Query(
    key="q6",
    title="Q6 - delivery-delay percentiles by state and month (no index can rescue this)",
    decision=(
        "percentile_cont needs every delivered order sorted inside its "
        "(state, month) group, so cost tracks rows read, not rows returned. Six "
        "index candidates were built and measured against this query; the best "
        "removed 23 of its 239 ms. It is answered by a maintained snapshot instead."
    ),
    live_sql="""
        SELECT state,
               purchase_month                                   AS month,
               count(*)                                         AS delivered,
               round(percentile_cont(0.5)  WITHIN GROUP (ORDER BY delay_days)::numeric, 2)
                                                                AS p50_delay_days,
               round(percentile_cont(0.9)  WITHIN GROUP (ORDER BY delay_days)::numeric, 2)
                                                                AS p90_delay_days,
               round(percentile_cont(0.99) WITHIN GROUP (ORDER BY delay_days)::numeric, 2)
                                                                AS p99_delay_days
        FROM olist.v_delivered_order
        GROUP BY 1, 2
        ORDER BY 1, 2
    """,
    served_sql="""
        SELECT state,
               purchase_month                                   AS month,
               count(*)                                         AS delivered,
               round(percentile_cont(0.5)  WITHIN GROUP (ORDER BY delay_days)::numeric, 2)
                                                                AS p50_delay_days,
               round(percentile_cont(0.9)  WITHIN GROUP (ORDER BY delay_days)::numeric, 2)
                                                                AS p90_delay_days,
               round(percentile_cont(0.99) WITHIN GROUP (ORDER BY delay_days)::numeric, 2)
                                                                AS p99_delay_days
        FROM olist.v_delivered_order
        GROUP BY 1, 2
        ORDER BY 1, 2
    """,
    served_from="live",
)

QUERIES: tuple[Query, ...] = (
    Q1_BASKET_REVENUE,
    Q2_LATE_BY_STATE,
    Q3_DELAY_VS_SCORE,
    Q4_TOP_SELLERS,
    Q5_PAYMENTS,
    Q6_PERCENTILES,
)

BY_KEY = {query.key: query for query in QUERIES}

-- =====================================================================
-- 05_snapshots.sql - the precomputed half of the view/snapshot comparison
--
-- Three panels are precomputed, each for a different measured reason:
--
--   revenue_snapshot        Q1, the heaviest analysis.  298 ms live -> 2.1 ms
--   seller_leaderboard      Q4.  Two rewrites and a covering index could not
--                           bring the live form under 200 ms (best 211 ms),
--                           because a top-20 leaderboard that is correct needs
--                           the order-weighted preparation average, which
--                           forces a 99k-row (seller, order) deduplication.
-- Q6 (delay percentiles by state and month) needs no snapshot: measured at
-- 136 ms it already fits, and adding one would only buy staleness.
--
-- Every row carries the moment it was computed.  The dashboard can therefore
-- show "as of" instead of pretending the number is live; that is the honesty
-- the comparison in AGENTS.md 4.3.6 is asking to be made explicit.
-- =====================================================================

CREATE TABLE olist.revenue_snapshot (
    purchase_month date          NOT NULL,
    category       varchar(60)   NOT NULL,
    revenue        numeric(12,2) NOT NULL,
    orders         integer       NOT NULL CHECK (orders > 0),
    computed_at    timestamp     NOT NULL DEFAULT now(),
    PRIMARY KEY (purchase_month, category)
);

CREATE TABLE olist.seller_leaderboard (
    rank           smallint      NOT NULL CHECK (rank >= 1),
    seller_id      varchar(32)   NOT NULL REFERENCES olist.seller (seller_id),
    revenue        numeric(12,2) NOT NULL,
    orders         integer       NOT NULL CHECK (orders > 0),
    avg_prep_hours numeric(8,2),
    avg_score      numeric(4,3),
    computed_at    timestamp     NOT NULL DEFAULT now(),
    PRIMARY KEY (rank)
);

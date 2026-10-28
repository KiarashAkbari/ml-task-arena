-- =====================================================================
-- Stage 2 - schema design (olist + factory)
--
-- Design notes that are NOT obvious from the CSV headers:
--
--  * Timestamps are `timestamp` WITHOUT time zone.  The source CSVs carry
--    no zone information at all; they are wall-clock readings in Brazil.
--    Storing them as timestamptz would require inventing a zone for data
--    that never recorded one.  Every comparison we make (delay, preparation
--    time) subtracts two of these columns from each other, so it is
--    unaffected by the choice.  Known limitation: for the DST window that
--    Brazil still had in 2017-2018 an hourly analysis could be off by one
--    hour.  No query in this stage buckets by hour, so we accept it.
--
--  * Money is `numeric(10,2)`, never float.  price/freight/payment are
--    exact decimal amounts in BRL and are summed across ~112k rows; float
--    would accumulate representation error into the revenue figures.
--
--  * Zip prefixes are `char(5)`, not integer.  They are identifiers, not
--    quantities: 01037 and 1037 would compare equal as integers.  A regex
--    CHECK keeps the fixed width honest.
--
--  * `customer_id` is order-scoped in this dataset (99,441 rows for 96,096
--    distinct people).  We keep it as the primary key because orders join
--    on it, and keep `customer_unique_id` as a separate column so that
--    "per person" analysis does not silently count one person many times.
--
--  * Geolocation is NOT a mandatory foreign key.  19,015 zip prefixes have
--    coordinates but customers/sellers reference 157 + 7 that do not.
--    Making it a hard FK would reject 285 real orders for a missing
--    enrichment, so the join is optional and left to the query.
-- =====================================================================

DROP SCHEMA IF EXISTS olist   CASCADE;
DROP SCHEMA IF EXISTS factory CASCADE;

CREATE SCHEMA olist;
CREATE SCHEMA factory;

-- ---------------------------------------------------------------------
-- Dimensions
-- ---------------------------------------------------------------------

CREATE TABLE olist.product_category (
    name_pt varchar(60) PRIMARY KEY,
    name_en varchar(60)
);

COMMENT ON COLUMN olist.product_category.name_en IS
    'NULL when the translation file has no entry for this Portuguese name.';

CREATE TABLE olist.customer (
    customer_id        varchar(32) PRIMARY KEY,
    customer_unique_id varchar(32) NOT NULL,
    zip_code_prefix    char(5)     NOT NULL CHECK (zip_code_prefix ~ '^[0-9]{5}$'),
    city               varchar(60) NOT NULL,
    state              char(2)     NOT NULL CHECK (state IN (
        'AC','AL','AM','AP','BA','CE','DF','ES','GO','MA','MG','MS','MT',
        'PA','PB','PE','PI','PR','RJ','RN','RO','RR','RS','SC','SE','SP','TO'))
);

CREATE TABLE olist.seller (
    seller_id       varchar(32) PRIMARY KEY,
    zip_code_prefix char(5)     NOT NULL CHECK (zip_code_prefix ~ '^[0-9]{5}$'),
    city            varchar(60) NOT NULL,
    state           char(2)     NOT NULL CHECK (state IN (
        'AC','AL','AM','AP','BA','CE','DF','ES','GO','MA','MG','MS','MT',
        'PA','PB','PE','PI','PR','RJ','RN','RO','RR','RS','SC','SE','SP','TO'))
);

CREATE TABLE olist.product (
    product_id         varchar(32) PRIMARY KEY,
    category_pt        varchar(60) REFERENCES olist.product_category (name_pt),
    name_length        smallint    CHECK (name_length        >= 0),
    description_length integer     CHECK (description_length >= 0),
    photos_qty         smallint    CHECK (photos_qty         >= 0),
    -- NULL means "unknown", not zero: 2 products have no measurements and
    -- 4 recorded weight 0, which is physically impossible for a shipped
    -- parcel and was repaired to NULL before load (see load report).
    weight_g           integer     CHECK (weight_g  > 0),
    length_cm          smallint    CHECK (length_cm > 0),
    height_cm          smallint    CHECK (height_cm > 0),
    width_cm           smallint    CHECK (width_cm  > 0)
);

-- ---------------------------------------------------------------------
-- Order fact tables
-- ---------------------------------------------------------------------

CREATE TABLE olist.orders (
    order_id              varchar(32) PRIMARY KEY,
    customer_id           varchar(32) NOT NULL REFERENCES olist.customer (customer_id),
    status                varchar(20) NOT NULL CHECK (status IN (
        'approved','canceled','created','delivered','invoiced',
        'processing','shipped','unavailable')),
    purchase_ts           timestamp   NOT NULL,
    approved_ts           timestamp,
    delivered_carrier_ts  timestamp,
    delivered_customer_ts timestamp,
    estimated_delivery_ts timestamp,
    -- Causality: a stage that happened cannot precede the purchase, and a
    -- delivered order must know when it was delivered.  Rows violating
    -- these were quarantined before load, not loaded with the checks off.
    CHECK (approved_ts           IS NULL OR approved_ts           >= purchase_ts),
    CHECK (delivered_carrier_ts  IS NULL OR delivered_carrier_ts  >= purchase_ts),
    CHECK (delivered_customer_ts IS NULL OR delivered_customer_ts >= purchase_ts),
    CHECK (estimated_delivery_ts IS NULL OR estimated_delivery_ts >= purchase_ts),
    CHECK (status <> 'delivered' OR delivered_customer_ts IS NOT NULL)
);

CREATE TABLE olist.order_item (
    order_id         varchar(32)   NOT NULL REFERENCES olist.orders  (order_id),
    item_seq         smallint      NOT NULL CHECK (item_seq >= 1),
    product_id       varchar(32)   NOT NULL REFERENCES olist.product (product_id),
    seller_id        varchar(32)   NOT NULL REFERENCES olist.seller  (seller_id),
    shipping_limit_ts timestamp    NOT NULL,
    price            numeric(10,2) NOT NULL CHECK (price         > 0),
    freight_value    numeric(10,2) NOT NULL CHECK (freight_value >= 0),
    PRIMARY KEY (order_id, item_seq)
);

CREATE TABLE olist.order_payment (
    order_id      varchar(32)   NOT NULL REFERENCES olist.orders (order_id),
    payment_seq   smallint      NOT NULL CHECK (payment_seq >= 1),
    payment_type  varchar(20)   NOT NULL CHECK (payment_type IN (
        'credit_card','boleto','voucher','debit_card','not_defined')),
    installments  smallint      NOT NULL CHECK (installments  >= 1),
    payment_value numeric(10,2) NOT NULL CHECK (payment_value > 0),
    PRIMARY KEY (order_id, payment_seq)
);

-- review_id is NOT unique in the source: 814 identifiers are shared by rows
-- for different orders, so the natural key is the (review_id, order_id) pair.
CREATE TABLE olist.order_review (
    review_id       varchar(32) NOT NULL,
    order_id        varchar(32) NOT NULL REFERENCES olist.orders (order_id),
    score           smallint    NOT NULL CHECK (score BETWEEN 1 AND 5),
    comment_title   text,
    comment_message text,
    creation_ts     timestamp,
    answer_ts       timestamp,
    PRIMARY KEY (review_id, order_id)
);

-- Aggregated from 1,000,163 raw points: one representative point per zip
-- prefix.  261,831 rows were exact duplicates and 31 points fell outside
-- Brazil.  `spread_km` records how well the median point represents the
-- points behind it, so a query can weigh trust in the coordinate.
CREATE TABLE olist.zip_geo (
    zip_code_prefix char(5)      PRIMARY KEY CHECK (zip_code_prefix ~ '^[0-9]{5}$'),
    latitude        numeric(9,6) NOT NULL CHECK (latitude  BETWEEN -33.75 AND   5.28),
    longitude       numeric(9,6) NOT NULL CHECK (longitude BETWEEN -73.99 AND -28.63),
    point_count     integer      NOT NULL CHECK (point_count > 0),
    spread_km       numeric(8,3) NOT NULL CHECK (spread_km  >= 0)
);

-- ---------------------------------------------------------------------
-- Row-level load quarantine (per-stage analogue of the Stage 1 quarantine)
-- ---------------------------------------------------------------------

CREATE TABLE olist.load_rejection (
    source_file  varchar(60) NOT NULL,
    reason       varchar(60) NOT NULL,
    row_count    integer     NOT NULL CHECK (row_count > 0),
    detail       text        NOT NULL,
    PRIMARY KEY (source_file, reason)
);

-- Audited DDL application: which file at which content hash, applied when.
CREATE TABLE olist.ddl_history (
    filename    varchar(80) PRIMARY KEY,
    sha256      char(64)    NOT NULL,
    applied_at  timestamp   NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Stage 1 output, same database, separate schema (chain requirement)
-- ---------------------------------------------------------------------

CREATE TABLE factory.case_file (
    unique_key       bigint      PRIMARY KEY,
    created_ts       timestamp   NOT NULL,
    closed_ts        timestamp,
    agency           varchar(20) NOT NULL,
    complaint_type   varchar(60) NOT NULL,
    descriptor       varchar(120),
    borough          varchar(20),
    zip_code         integer      CHECK (zip_code BETWEEN 0 AND 99999),
    status           varchar(20) NOT NULL,
    population       integer      CHECK (population >= 0),
    duration_minutes integer      CHECK (duration_minutes >= 0),
    CHECK (closed_ts IS NULL OR closed_ts >= created_ts)
);

COMMENT ON SCHEMA factory IS
    'Output of the Stage 1 data factory (data/curated/cases.csv), loaded by '
    'the same package through its database destination.';

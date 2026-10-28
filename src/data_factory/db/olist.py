"""Read the Olist CSV set and decide, before loading, what each bad row means.

This module contains *policy*, not SQL: which rows cannot be loaded, which
values are repaired, and how the 1,000,163-row geolocation file becomes a
usable reference table.  ``loader.py`` only writes what this module returns,
so every number in the load report traces back to a rule written here.

The rules are deliberately boring and explicit - the interesting part of the
stage is that each one is *declared and counted* rather than discovered later
inside a query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from data_factory.db.quarantine import Rejection, Repair

# Source file names, exactly as published on Kaggle.
FILES = {
    "orders": "olist_orders_dataset.csv",
    "items": "olist_order_items_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
    "reviews": "olist_order_reviews_dataset.csv",
    "customers": "olist_customers_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "products": "olist_products_dataset.csv",
    "geo": "olist_geolocation_dataset.csv",
    "translations": "product_category_name_translation.csv",
}

ORDER_STATUSES = (
    "approved", "canceled", "created", "delivered",
    "invoiced", "processing", "shipped", "unavailable",
)
PAYMENT_TYPES = ("credit_card", "boleto", "voucher", "debit_card", "not_defined")
BRAZIL_BOUNDS = {"lat_min": -33.75, "lat_max": 5.28, "lng_min": -73.99, "lng_max": -28.63}
GEO_CHUNK_ROWS = 200_000


@dataclass
class PreparedOlist:
    """Typed frames ready for ``loader.write_frame`` plus the audit trail."""

    product_category: pd.DataFrame
    customer: pd.DataFrame
    seller: pd.DataFrame
    product: pd.DataFrame
    orders: pd.DataFrame
    order_item: pd.DataFrame
    order_payment: pd.DataFrame
    order_review: pd.DataFrame
    zip_geo: pd.DataFrame
    rejections: list[Rejection] = field(default_factory=list)
    repairs: list[Repair] = field(default_factory=list)
    stats: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _money(series: pd.Series) -> pd.Series:
    """Exact decimals, so the numeric(10,2) columns never see a float."""
    return series.map(lambda v: Decimal(str(round(float(v), 2))).quantize(Decimal("0.01")))


def _numeric(series: pd.Series, decimals: str = "0.000001") -> pd.Series:
    quantum = Decimal(decimals)
    return series.map(lambda v: Decimal(str(v)).quantize(quantum))


def _ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce")


def _sample(values: pd.Series, limit: int = 5) -> tuple[str, ...]:
    return tuple(str(v) for v in values.drop_duplicates().head(limit))


def haversine_km(lat1: np.ndarray, lng1: np.ndarray,
                 lat2: np.ndarray, lng2: np.ndarray) -> np.ndarray:
    """Great-circle distance in kilometres between two coordinate arrays."""
    r = 6371.0088
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lng2 - lng1)
    a = np.sin(dp / 2.0) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2.0) ** 2
    return 2.0 * r * np.arcsin(np.sqrt(a))


# ---------------------------------------------------------------------------
# geolocation: 1,000,163 points -> one representative point per zip prefix
# ---------------------------------------------------------------------------

def build_zip_geo(path: Path) -> tuple[pd.DataFrame, list[Rejection], list[Repair], dict]:
    """Read the geolocation file in chunks and reduce it to a zip-level table.

    Read in chunks (AGENTS.md 4.3.8: large reads are batched) with compact
    dtypes, because 1M rows x 5 object columns would sit in memory as several
    hundred megabytes for no reason.
    """
    usecols = ["geolocation_zip_code_prefix", "geolocation_lat", "geolocation_lng"]
    dtypes = {"geolocation_zip_code_prefix": "string",
              "geolocation_lat": "float64",
              "geolocation_lng": "float64"}

    chunks: list[pd.DataFrame] = []
    rows_read = 0
    for chunk in pd.read_csv(path, usecols=usecols, dtype=dtypes,
                             chunksize=GEO_CHUNK_ROWS):
        rows_read += len(chunk)
        chunks.append(chunk)
    points = pd.concat(chunks, ignore_index=True)
    del chunks

    duplicate_count = int(points.duplicated().sum())
    points = points.drop_duplicates()

    out_of_bounds = (
        (points["geolocation_lat"] < BRAZIL_BOUNDS["lat_min"])
        | (points["geolocation_lat"] > BRAZIL_BOUNDS["lat_max"])
        | (points["geolocation_lng"] < BRAZIL_BOUNDS["lng_min"])
        | (points["geolocation_lng"] > BRAZIL_BOUNDS["lng_max"])
    )
    rejected = points.loc[out_of_bounds]
    points = points.loc[~out_of_bounds].copy()
    points["zip"] = points["geolocation_zip_code_prefix"].astype(str).str.zfill(5)

    # Median is the representative point: a single swapped latitude/longitude
    # in the source cannot drag the whole prefix across the map.
    grouped = points.groupby("zip", observed=True)
    representative = grouped[["geolocation_lat", "geolocation_lng"]].median()
    counts = grouped.size().rename("point_count")

    spread = pd.Series(
        haversine_km(
            points["geolocation_lat"].to_numpy(),
            points["geolocation_lng"].to_numpy(),
            points["zip"].map(representative["geolocation_lat"]).to_numpy(),
            points["zip"].map(representative["geolocation_lng"]).to_numpy(),
        ),
        index=points.index,
    )
    max_spread = spread.groupby(points["zip"]).max().rename("spread_km")

    frame = representative.join(counts).join(max_spread).reset_index()
    frame = frame.rename(columns={"zip": "zip_code_prefix",
                                  "geolocation_lat": "latitude",
                                  "geolocation_lng": "longitude"})
    frame["latitude"] = _numeric(frame["latitude"])
    frame["longitude"] = _numeric(frame["longitude"])
    frame["spread_km"] = _numeric(frame["spread_km"], "0.001")
    frame["point_count"] = frame["point_count"].astype("int32")

    rejections = [
        Rejection(
            source_file=FILES["geo"],
            reason="GEO_OUTSIDE_BRAZIL",
            row_count=int(len(rejected)),
            detail=(
                "coordinates outside the Brazil bounding box "
                f"(lat {BRAZIL_BOUNDS['lat_min']}..{BRAZIL_BOUNDS['lat_max']}, "
                f"lng {BRAZIL_BOUNDS['lng_min']}..{BRAZIL_BOUNDS['lng_max']}); "
                "excluded from the per-zip representative point"
            ),
            sample_keys=_sample(rejected["geolocation_zip_code_prefix"].astype(str)),
        )
    ]
    repairs = [
        Repair(
            source_file=FILES["geo"],
            reason="GEO_EXACT_DUPLICATE_POINT",
            row_count=duplicate_count,
            detail=(
                f"{rows_read:,} published rows collapse to {len(points) + len(rejected):,} "
                f"distinct points; duplicates add no information to a per-zip median"
            ),
        )
    ]
    stats = {"rows_read": rows_read, "distinct_points": int(len(points) + len(rejected)),
             "zip_prefixes": int(len(frame))}
    return frame, rejections, repairs, stats


# ---------------------------------------------------------------------------
# the whole set
# ---------------------------------------------------------------------------

def prepare(source_dir: Path) -> PreparedOlist:
    """Apply every load-time rule and return typed, loadable frames."""
    def read(key: str, **kwargs) -> pd.DataFrame:
        return pd.read_csv(source_dir / FILES[key], dtype=str, **kwargs)

    rejections: list[Rejection] = []
    repairs: list[Repair] = []
    stats: dict[str, object] = {}

    # ---- reference data ------------------------------------------------
    translations = read("translations")
    products_raw = read("products")
    categories = translations.rename(
        columns={"product_category_name": "name_pt",
                 "product_category_name_english": "name_en"}
    )[["name_pt", "name_en"]]

    known = set(categories["name_pt"])
    used = set(products_raw["product_category_name"].dropna())
    untranslated = sorted(used - known)
    if untranslated:
        # The product catalogue references two categories the translation
        # file never learned.  Keeping the catalogue whole and letting the
        # English name be NULL is a smaller lie than dropping 13 products
        # (and the 24 order items that point at them).
        categories = pd.concat(
            [categories, pd.DataFrame({"name_pt": untranslated, "name_en": [None] * len(untranslated)})],
            ignore_index=True,
        )
        repairs.append(
            Repair(
                source_file=FILES["translations"],
                reason="CATEGORY_WITHOUT_TRANSLATION",
                row_count=int(products_raw["product_category_name"].isin(untranslated).sum()),
                detail=(
                    f"{len(untranslated)} category name(s) referenced by products are absent "
                    f"from the translation file ({', '.join(untranslated)}); added to "
                    "product_category with name_en = NULL instead of dropping the products"
                ),
            )
        )
    stats["categories"] = int(len(categories))

    # ---- product -------------------------------------------------------
    zero_weight = pd.to_numeric(products_raw["product_weight_g"], errors="coerce").le(0)
    if zero_weight.any():
        repairs.append(
            Repair(
                source_file=FILES["products"],
                reason="PRODUCT_WEIGHT_NOT_POSITIVE",
                row_count=int(zero_weight.sum()),
                detail="weight 0 g cannot be true for a shipped parcel; stored as NULL (unknown)",
            )
        )

    product = pd.DataFrame({
        "product_id": products_raw["product_id"],
        "category_pt": products_raw["product_category_name"].where(
            products_raw["product_category_name"].notna()),
        "name_length": pd.to_numeric(products_raw["product_name_lenght"], errors="coerce").astype("Int16"),
        "description_length": pd.to_numeric(products_raw["product_description_lenght"], errors="coerce").astype("Int32"),
        "photos_qty": pd.to_numeric(products_raw["product_photos_qty"], errors="coerce").astype("Int16"),
        "weight_g": pd.to_numeric(products_raw["product_weight_g"], errors="coerce")
                      .mask(zero_weight).astype("Int32"),
        "length_cm": pd.to_numeric(products_raw["product_length_cm"], errors="coerce").astype("Int16"),
        "height_cm": pd.to_numeric(products_raw["product_height_cm"], errors="coerce").astype("Int16"),
        "width_cm": pd.to_numeric(products_raw["product_width_cm"], errors="coerce").astype("Int16"),
    })

    # ---- customer / seller ---------------------------------------------
    customers_raw = read("customers")
    customer = pd.DataFrame({
        "customer_id": customers_raw["customer_id"],
        "customer_unique_id": customers_raw["customer_unique_id"],
        "zip_code_prefix": customers_raw["customer_zip_code_prefix"].str.zfill(5),
        "city": customers_raw["customer_city"],
        "state": customers_raw["customer_state"],
    })

    sellers_raw = read("sellers")
    seller = pd.DataFrame({
        "seller_id": sellers_raw["seller_id"],
        "zip_code_prefix": sellers_raw["seller_zip_code_prefix"].str.zfill(5),
        "city": sellers_raw["seller_city"],
        "state": sellers_raw["seller_state"],
    })

    # ---- orders (with the two date-causality rules) ---------------------
    orders_raw = read("orders")
    orders_raw["purchase_ts"] = _ts(orders_raw["order_purchase_timestamp"])
    orders_raw["approved_ts"] = _ts(orders_raw["order_approved_at"])
    orders_raw["carrier_ts"] = _ts(orders_raw["order_delivered_carrier_date"])
    orders_raw["customer_ts"] = _ts(orders_raw["order_delivered_customer_date"])
    orders_raw["estimated_ts"] = _ts(orders_raw["order_estimated_delivery_date"])

    delivered_without_date = orders_raw["order_status"].eq("delivered") & orders_raw["customer_ts"].isna()
    carrier_before_purchase = (orders_raw["carrier_ts"] < orders_raw["purchase_ts"]).fillna(False)

    if delivered_without_date.any():
        rejections.append(
            Rejection(
                source_file=FILES["orders"],
                reason="DELIVERED_WITHOUT_DELIVERY_DATE",
                row_count=int(delivered_without_date.sum()),
                detail="status = delivered but order_delivered_customer_date is empty; "
                       "the order cannot support any delivery-time measure",
                sample_keys=_sample(orders_raw.loc[delivered_without_date, "order_id"]),
            )
        )
    if carrier_before_purchase.any():
        rejections.append(
            Rejection(
                source_file=FILES["orders"],
                reason="CARRIER_BEFORE_PURCHASE",
                row_count=int(carrier_before_purchase.sum()),
                detail="order_delivered_carrier_date precedes order_purchase_timestamp; "
                       "the carrier cannot receive a parcel before it is bought",
                sample_keys=_sample(orders_raw.loc[carrier_before_purchase, "order_id"]),
            )
        )

    rejected_order_ids = set(orders_raw.loc[delivered_without_date | carrier_before_purchase, "order_id"])
    orders = orders_raw.loc[~orders_raw["order_id"].isin(rejected_order_ids)]
    stats["orders_rejected"] = len(rejected_order_ids)

    orders = pd.DataFrame({
        "order_id": orders["order_id"],
        "customer_id": orders["customer_id"],
        "status": orders["order_status"],
        "purchase_ts": orders["purchase_ts"],
        "approved_ts": orders["approved_ts"],
        "delivered_carrier_ts": orders["carrier_ts"],
        "delivered_customer_ts": orders["customer_ts"],
        "estimated_delivery_ts": orders["estimated_ts"],
    })

    # ---- children cascade ----------------------------------------------
    items_raw = read("items")
    items = items_raw.loc[~items_raw["order_id"].isin(rejected_order_ids)].copy()
    cascaded_items = len(items_raw) - len(items)
    if cascaded_items:
        rejections.append(
            Rejection(
                source_file=FILES["items"],
                reason="CASCADE_PARENT_REJECTED",
                row_count=cascaded_items,
                detail="item belongs to an order rejected for a date-causality violation",
                sample_keys=_sample(items_raw.loc[items_raw["order_id"].isin(rejected_order_ids), "order_id"]),
            )
        )
    order_item = pd.DataFrame({
        "order_id": items["order_id"],
        "item_seq": pd.to_numeric(items["order_item_id"]).astype("int16"),
        "product_id": items["product_id"],
        "seller_id": items["seller_id"],
        "shipping_limit_ts": _ts(items["shipping_limit_date"]),
        "price": _money(items["price"]),
        "freight_value": _money(items["freight_value"]),
    })

    payments_raw = read("payments")
    survivors = payments_raw.loc[~payments_raw["order_id"].isin(rejected_order_ids)].copy()
    cascaded_payments = len(payments_raw) - len(survivors)
    if cascaded_payments:
        rejections.append(
            Rejection(
                source_file=FILES["payments"],
                reason="CASCADE_PARENT_REJECTED",
                row_count=cascaded_payments,
                detail="payment belongs to an order rejected for a date-causality violation",
                sample_keys=_sample(payments_raw.loc[payments_raw["order_id"].isin(rejected_order_ids), "order_id"]),
            )
        )

    value = pd.to_numeric(survivors["payment_value"], errors="coerce")
    installments = pd.to_numeric(survivors["payment_installments"], errors="coerce")
    bad_value = value.le(0).fillna(False)
    bad_installments = installments.lt(1).fillna(False)
    if bad_value.any():
        rejections.append(
            Rejection(
                source_file=FILES["payments"],
                reason="PAYMENT_VALUE_NOT_POSITIVE",
                row_count=int(bad_value.sum()),
                detail="a payment row moving zero (or negative) money is not a payment",
                sample_keys=_sample(survivors.loc[bad_value, "order_id"]),
            )
        )
    if bad_installments.any():
        rejections.append(
            Rejection(
                source_file=FILES["payments"],
                reason="PAYMENT_INSTALLMENTS_BELOW_ONE",
                row_count=int(bad_installments.sum()),
                detail="payment_installments = 0 cannot describe an instalment plan",
                sample_keys=_sample(survivors.loc[bad_installments, "order_id"]),
            )
        )

    payments = survivors.loc[~(bad_value | bad_installments)]
    order_payment = pd.DataFrame({
        "order_id": payments["order_id"],
        "payment_seq": pd.to_numeric(payments["payment_sequential"]).astype("int16"),
        "payment_type": payments["payment_type"],
        "installments": pd.to_numeric(payments["payment_installments"]).astype("int16"),
        "payment_value": _money(payments["payment_value"]),
    })

    reviews_raw = read("reviews")
    reviews = reviews_raw.loc[~reviews_raw["order_id"].isin(rejected_order_ids)].copy()
    cascaded_reviews = len(reviews_raw) - len(reviews)
    if cascaded_reviews:
        rejections.append(
            Rejection(
                source_file=FILES["reviews"],
                reason="CASCADE_PARENT_REJECTED",
                row_count=cascaded_reviews,
                detail="review belongs to an order rejected for a date-causality violation",
                sample_keys=_sample(reviews_raw.loc[reviews_raw["order_id"].isin(rejected_order_ids), "order_id"]),
            )
        )
    order_review = pd.DataFrame({
        "review_id": reviews["review_id"],
        "order_id": reviews["order_id"],
        "score": pd.to_numeric(reviews["review_score"]).astype("int16"),
        "comment_title": reviews["review_comment_title"],
        "comment_message": reviews["review_comment_message"],
        "creation_ts": _ts(reviews["review_creation_date"]),
        "answer_ts": _ts(reviews["review_answer_timestamp"]),
    })

    # ---- geolocation ----------------------------------------------------
    zip_geo, geo_rejections, geo_repairs, geo_stats = build_zip_geo(source_dir / FILES["geo"])
    rejections.extend(geo_rejections)
    repairs.extend(geo_repairs)
    stats["geo"] = geo_stats

    return PreparedOlist(
        product_category=categories,
        customer=customer,
        seller=seller,
        product=product,
        orders=orders,
        order_item=order_item,
        order_payment=order_payment,
        order_review=order_review,
        zip_geo=zip_geo,
        rejections=rejections,
        repairs=repairs,
        stats=stats,
    )

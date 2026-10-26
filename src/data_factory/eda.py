"""Exploratory analysis over the curated case dataset (AGENTS.md section 4.9).

The notebook imports this module instead of carrying its own data logic
(section 5.6): every function is pure — a DataFrame (plus the population
reference where a rate is needed) goes in, a DataFrame/dict comes out — so the
numbers the notebook prints are ordinary offline-testable code.

Design decisions worth knowing when reading the notebook:

* **Only the delivered dataset is read.** The analysis runs over
  ``data/curated/cases.csv`` (the published artifact, hashed in the manifest),
  never over raw batches: the notebook explains the product, not the plumbing.
* **Open cases are not dropped.** ``duration_minutes`` is null exactly when
  the case is still open (see :mod:`data_factory.clean`). Duration statistics
  therefore use closed cases only and report the open share next to them —
  dropping open cases silently would bias every duration claim.
* **Outliers are kept.** A closure that took two years is a process fact, not
  a data error; the analysis caps *descriptive means* at
  :data:`OUTLIER_CAP_DAYS` and reports p90/p99, which is stated wherever a
  number derived from durations appears.
* **No randomness.** Buckets, group orders and tie-breaks are explicit, so two
  runs of the notebook print identical numbers (section 5.5).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from data_factory.clean import CURATED_COLUMNS
from data_factory.errors import ValidationError
from data_factory.population import PopulationRow, normalize_borough

DATE_COLUMNS: tuple[str, ...] = ("created_date", "closed_date")
INTEGER_COLUMNS: tuple[str, ...] = ("population", "duration_minutes")
MINUTES_PER_HOUR = 60
MINUTES_PER_DAY = 24 * MINUTES_PER_HOUR

#: Durations above one year are reported, never silently dropped; they are
#: excluded from the descriptive mean only (see the module docstring).
OUTLIER_CAP_DAYS = 365.0

#: Fixed buckets, in minutes, for the duration histogram. Lower edge is
#: inclusive, upper exclusive; the last bucket is open-ended.
DURATION_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<1h", 0.0, 60.0),
    ("1-6h", 60.0, 360.0),
    ("6-24h", 360.0, 1440.0),
    ("1-3d", 1440.0, 4320.0),
    ("3-7d", 4320.0, 10080.0),
    ("7-30d", 10080.0, 43200.0),
    ("30-90d", 43200.0, 129600.0),
    ("90-365d", 129600.0, 525600.0),
    (">365d", 525600.0, float("inf")),
)


def load_cases(path: str | Path) -> pd.DataFrame:
    """Load the curated dataset and add the derived columns analysis needs.

    Derived columns: ``duration_days`` (float, null when open), ``is_open``,
    ``month`` (``YYYY-MM`` of creation) and ``created_hour``.

    Raises:
        ValidationError: The file is missing or its columns differ from
            :data:`~data_factory.clean.CURATED_COLUMNS`.
    """
    csv_path = Path(path)
    if not csv_path.is_file():
        raise ValidationError(
            f"curated dataset '{csv_path}' not found — run the factory first "
            "(`data-factory run`)"
        )
    frame = pd.read_csv(
        csv_path,
        dtype={"unique_key": "string", "zip_code": "string"},
    )
    missing = [column for column in CURATED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValidationError(
            f"curated dataset '{csv_path}' is missing column(s) {missing}"
        )
    if len(frame.columns) != len(CURATED_COLUMNS):
        raise ValidationError(
            f"curated dataset '{csv_path}' has unexpected extra columns "
            f"{sorted(set(frame.columns) - set(CURATED_COLUMNS))}"
        )
    for column in DATE_COLUMNS:
        frame[column] = pd.to_datetime(frame[column], format="ISO8601")
    for column in INTEGER_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype("Int64")
    frame["duration_days"] = frame["duration_minutes"].astype("Float64") / MINUTES_PER_DAY
    frame["is_open"] = frame["duration_minutes"].isna()
    frame["month"] = frame["created_date"].dt.strftime("%Y-%m")
    frame["created_hour"] = frame["created_date"].dt.hour
    return frame


def duration_summary(frame: pd.DataFrame) -> dict[str, float]:
    """Headline duration statistics over *closed* cases, in days."""
    durations = frame["duration_days"].dropna()
    closed = int(len(durations))
    total = int(len(frame))
    if closed == 0:
        raise ValidationError("no closed cases — duration statistics are undefined")
    return {
        "cases": total,
        "closed_cases": closed,
        "open_cases": total - closed,
        "open_share": (total - closed) / total,
        "mean_days": float(durations.mean()),
        "median_days": float(durations.median()),
        "p90_days": float(durations.quantile(0.90)),
        "p99_days": float(durations.quantile(0.99)),
        "max_days": float(durations.max()),
    }


def duration_buckets(frame: pd.DataFrame) -> pd.DataFrame:
    """Case counts per fixed duration bucket (closed cases only)."""
    durations = frame["duration_minutes"].dropna()
    labels = [label for label, _, _ in DURATION_BUCKETS]
    edges = [low for _, low, _ in DURATION_BUCKETS] + [float("inf")]
    bucketed = pd.cut(
        durations.astype("float64"), bins=edges, labels=labels, right=False
    )
    counts = bucketed.value_counts().reindex(labels, fill_value=0)
    total = int(counts.sum())
    if total == 0:
        raise ValidationError("no closed cases — the duration histogram is empty")
    return pd.DataFrame(
        {
            "bucket": labels,
            "cases": counts.to_numpy(dtype="int64"),
            "share": counts.to_numpy(dtype="float64") / total,
        }
    )


def outlier_summary(frame: pd.DataFrame, *, cap_days: float = OUTLIER_CAP_DAYS) -> dict[str, float]:
    """How much of the dataset sits beyond the outlier cap, and how far."""
    durations = frame["duration_days"].dropna()
    beyond = durations[durations > cap_days]
    within = durations[durations <= cap_days]
    return {
        "cap_days": cap_days,
        "closed_cases": float(len(durations)),
        "beyond_cap_cases": float(len(beyond)),
        "beyond_cap_share": float(len(beyond) / len(durations)),
        "mean_days_all": float(durations.mean()),
        "mean_days_within_cap": float(within.mean()),
        "median_days": float(durations.median()),
        "max_days": float(durations.max()),
    }


def open_share_by_type(frame: pd.DataFrame) -> pd.DataFrame:
    """Cases, still-open cases and open share per complaint type."""
    grouped = frame.groupby("complaint_type", observed=True)
    table = pd.DataFrame(
        {
            "cases": grouped.size(),
            "open_cases": grouped["is_open"].sum(),
        }
    )
    table["open_share"] = table["open_cases"] / table["cases"]
    return table.sort_values(["open_share", "cases"], ascending=[False, False])


def duration_by_type(frame: pd.DataFrame) -> pd.DataFrame:
    """Median / p90 closure time per complaint type (closed cases only)."""
    closed = frame.dropna(subset=["duration_days"])
    grouped = closed.groupby("complaint_type", observed=True)["duration_days"]
    table = pd.DataFrame(
        {
            "closed_cases": grouped.size(),
            "median_days": grouped.median(),
            "p90_days": grouped.quantile(0.90),
        }
    )
    return table.sort_values(["median_days", "closed_cases"], ascending=[False, False])


def type_mix(frame: pd.DataFrame) -> pd.DataFrame:
    """Share of each locked complaint type in the dataset."""
    counts = frame["complaint_type"].value_counts()
    return pd.DataFrame(
        {"cases": counts, "share": counts / int(counts.sum())}
    )


def monthly_volume(frame: pd.DataFrame) -> pd.DataFrame:
    """Cases per month and per complaint type (months sorted, types sorted)."""
    table = (
        frame.pivot_table(
            index="month",
            columns="complaint_type",
            values="unique_key",
            aggfunc="size",
            fill_value=0,
            observed=True,
        )
        .sort_index()
        .sort_index(axis=1)
    )
    return table.astype("int64")


def _district_labels(frame: pd.DataFrame) -> pd.Series:
    """District labels with 311 spellings mapped to the canonical ones.

    ``clean`` already canonicalises the column, so this is normally a no-op;
    it keeps the analysis correct for a dataset curated before that fix and
    can never change a label it does not recognise.
    """
    return frame["borough"].map(
        lambda value: normalize_borough(value) if isinstance(value, str) else value
    )


def borough_totals(population: dict[str, PopulationRow]) -> pd.Series:
    """Reference population per district (borough), summed over ZIPs."""
    totals: dict[str, int] = {}
    for row in population.values():
        if row.borough is not None:
            totals[row.borough] = totals.get(row.borough, 0) + row.population
    return pd.Series(totals, dtype="int64").sort_index()


def borough_rates(frame: pd.DataFrame, population: dict[str, PopulationRow]) -> pd.DataFrame:
    """Cases and cases per 100,000 residents per district.

    Districts without a reference population keep a null rate — the same rule
    the aggregate table follows, so the analysis never invents a denominator.
    """
    cases = _district_labels(frame).value_counts()
    totals = borough_totals(population)
    table = pd.DataFrame({"cases": cases})
    table["population"] = totals.reindex(table.index)
    table["rate_per_100k"] = table["cases"] / table["population"] * 100_000
    return table.sort_values("cases", ascending=False)


def borough_type_rates(frame: pd.DataFrame, population: dict[str, PopulationRow]) -> pd.DataFrame:
    """Cases and rate per 100,000 for every (district, complaint type) pair."""
    labelled = frame.assign(borough=_district_labels(frame))
    grouped = labelled.groupby(["borough", "complaint_type"], observed=True)
    table = pd.DataFrame({"cases": grouped.size()})
    totals = borough_totals(population)
    table["population"] = table.index.get_level_values(0).map(totals).astype("Int64")
    table["rate_per_100k"] = table["cases"] / table["population"] * 100_000
    return table


def zip_volume_vs_population(
    frame: pd.DataFrame, population: dict[str, PopulationRow]
) -> pd.DataFrame:
    """Per-ZIP cases vs. reference population, with a least-squares baseline.

    ``residual`` is ``cases - fitted``; ``residual_share`` divides it by the
    fitted value so ZIPs of different sizes are comparable. ZIPs without a
    reference population are excluded (no denominator, no residual).
    """
    cases = frame["zip_code"].dropna().value_counts()
    known = {zip_code: row.population for zip_code, row in population.items()}
    table = pd.DataFrame({"cases": cases})
    table["population"] = pd.Series(known).reindex(table.index).astype("Int64")
    table = table.dropna(subset=["population"]).copy()
    if len(table) < 2:
        raise ValidationError("not enough ZIPs with a reference population to fit")
    slope, intercept = np.polyfit(
        table["population"].astype("float64"), table["cases"].astype("float64"), 1
    )
    table["fitted"] = slope * table["population"].astype("float64") + intercept
    table["residual"] = table["cases"].astype("float64") - table["fitted"]
    table["residual_share"] = table["residual"] / table["fitted"]
    return table.sort_values("cases", ascending=False)


def numeric_relationships(frame: pd.DataFrame) -> pd.DataFrame:
    """Pearson correlation between the numeric columns the dataset carries."""
    columns = ["duration_days", "population", "created_hour"]
    numeric = frame[columns].astype("float64")
    return numeric.corr(method="pearson")


def closure_speed_by_hour(
    frame: pd.DataFrame, *, within_minutes: int = 60
) -> pd.DataFrame:
    """Share of closed cases resolved within ``within_minutes`` per creation hour.

    A spike at particular hours is the signature of batch/automatic closures;
    it explains the fast bucket in the duration histogram.
    """
    closed = frame.dropna(subset=["duration_minutes"])
    fast = closed["duration_minutes"].astype("float64") <= within_minutes
    grouped = closed.assign(fast=fast.astype("float64")).groupby(
        "created_hour", observed=True
    )
    table = pd.DataFrame(
        {
            "closed_cases": grouped.size(),
            "within_share": grouped["fast"].mean(),
        }
    )
    return table.sort_index()


def finding_metrics(
    frame: pd.DataFrame, population: dict[str, PopulationRow]
) -> dict[str, object]:
    """The few numbers behind the notebook's headline findings.

    Kept in code (not typed into markdown) so every sentence in the notebook
    and the report is generated from the same computation.
    """
    summary = duration_summary(frame)
    open_shares = open_share_by_type(frame)["open_share"]
    mixes = type_mix(frame)
    rates = borough_rates(frame, population).dropna(subset=["rate_per_100k"])
    zip_table = zip_volume_vs_population(frame, population)
    speed = closure_speed_by_hour(frame)
    return {
        "cases": summary["cases"],
        "median_days": summary["median_days"],
        "mean_days": summary["mean_days"],
        "p90_days": summary["p90_days"],
        "mean_to_median": summary["mean_days"] / summary["median_days"],
        "open_share": summary["open_share"],
        "most_open_type": str(open_shares.index[0]),
        "most_open_share": float(open_shares.iloc[0]),
        "least_open_type": str(open_shares.index[-1]),
        "least_open_share": float(open_shares.iloc[-1]),
        "top_type": str(mixes.index[0]),
        "top_type_share": float(mixes["share"].iloc[0]),
        "top_count_borough": str(rates["cases"].idxmax()),
        "top_rate_borough": str(rates["rate_per_100k"].idxmax()),
        "rate_leader_over_count_leader": float(
            rates["rate_per_100k"].max() / rates.loc[rates["cases"].idxmax(), "rate_per_100k"]
        ),
        "worst_residual_zip": str(zip_table["residual_share"].idxmax()),
        "worst_residual_share": float(zip_table["residual_share"].max()),
        "worst_residual_cases": float(zip_table.loc[zip_table["residual_share"].idxmax(), "cases"]),
        "peak_fast_hour": int(speed["within_share"].idxmax()),
        "peak_fast_hour_share": float(speed["within_share"].max()),
        "quiet_fast_hour": int(speed["within_share"].idxmin()),
        "quiet_fast_hour_share": float(speed["within_share"].min()),
    }

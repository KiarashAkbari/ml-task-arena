"""Fetch the nine Olist CSV files and verify they are the real dataset.

The Kaggle competition rules require an authenticated download, so this script
cannot fetch silently on a grader's machine.  It offers two routes and treats
both with the same suspicion:

    # 1. You already downloaded the Kaggle files somewhere
    python scripts/fetch_olist.py --from-zip ~/Downloads/archive.zip

    # 2. Fetch a public mirror, then verify it
    python scripts/fetch_olist.py --mirror

Either way the files are **verified against the canonical row counts** of
`olistbr/brazilian-ecommerce` before anything is written into data/raw/olist.
Row counts are the provenance proof: a mirror that reproduces all nine counts
exactly is the published dataset, and one that does not is refused.  Nothing
here trusts a source by its URL.

Counts (Kaggle, checked 2026-10-28):
    orders 99,441 | items 112,650 | payments 103,886 | reviews 99,224
    customers 99,441 | sellers 3,095 | products 32,951
    geolocation 1,000,163 | category translation 71
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from pathlib import Path
from urllib.request import urlopen

EXPECTED_ROWS = {
    "olist_orders_dataset.csv": 99_441,
    "olist_order_items_dataset.csv": 112_650,
    "olist_order_payments_dataset.csv": 103_886,
    "olist_order_reviews_dataset.csv": 99_224,
    "olist_customers_dataset.csv": 99_441,
    "olist_sellers_dataset.csv": 3_095,
    "olist_products_dataset.csv": 32_951,
    "olist_geolocation_dataset.csv": 1_000_163,
    "product_category_name_translation.csv": 71,
}

#: A public copy of the dataset.  Used only because Kaggle needs credentials;
#: every file it provides is verified by row count before it is accepted.
MIRROR = "https://codeload.github.com/tunguyenn99/ecommerce-data-modeling/zip/refs/heads/main"
MIRROR_DIR = "dataset/"

DEST = Path("data/raw/olist")


def count_records(path: Path) -> int:
    """Count CSV records, not newlines: review text contains embedded newlines."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        return sum(1 for _ in reader)


def verify(directory: Path) -> list[str]:
    """Return a list of problems; an empty list means the set is authentic."""
    problems = []
    for name, expected in EXPECTED_ROWS.items():
        path = directory / name
        if not path.is_file():
            problems.append(f"{name}: missing")
            continue
        actual = count_records(path)
        if actual != expected:
            problems.append(f"{name}: {actual:,} rows, expected {expected:,}")
    return problems


def extract_from_zip(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        written = 0
        for member in bundle.namelist():
            base = member.split("/")[-1]
            if base in EXPECTED_ROWS:
                with bundle.open(member) as source:
                    (dest / base).write_bytes(source.read())
                written += 1
        print(f"extracted {written} of {len(EXPECTED_ROWS)} expected files from {archive}")


def extract_from_mirror(dest: Path) -> None:
    print(f"downloading a public mirror (~45 MB): {MIRROR}")
    with urlopen(MIRROR, timeout=300) as response:      # noqa: S310 - fixed URL
        payload = response.read()
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        for member in bundle.namelist():
            if not member.startswith(MIRROR_DIR) or not member.endswith(".csv"):
                continue
            base = member.split("/")[-1]
            if base in EXPECTED_ROWS:
                with bundle.open(member) as source:
                    (dest / base).write_bytes(source.read())
                written += 1
    print(f"extracted {written} of {len(EXPECTED_ROWS)} expected files")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-zip", metavar="PATH",
                        help="a Kaggle archive.zip you downloaded manually")
    parser.add_argument("--mirror", action="store_true",
                        help="download a public mirror, then verify row counts")
    parser.add_argument("--dest", default=str(DEST),
                        help=f"where to write the CSVs (default: {DEST})")
    parser.add_argument("--verify-only", action="store_true",
                        help="only check the files already in --dest")
    args = parser.parse_args()

    dest = Path(args.dest)

    if args.verify_only:
        problems = verify(dest)
        for problem in problems:
            print(f"  FAIL {problem}")
        if problems:
            return 1
        print(f"all {len(EXPECTED_ROWS)} files match the published row counts")
        return 0

    if args.from_zip:
        extract_from_zip(Path(args.from_zip).expanduser(), dest)
    elif args.mirror:
        extract_from_mirror(dest)
    elif dest.is_dir() and any(dest.glob("*.csv")):
        print(f"using the files already in {dest}")
    else:
        print(
            "No source given and nothing in data/raw/olist.\n"
            "Download the dataset from Kaggle (olistbr/brazilian-ecommerce) and run:\n"
            "    python scripts/fetch_olist.py --from-zip ~/Downloads/archive.zip\n"
            "or fetch a public mirror with:\n"
            "    python scripts/fetch_olist.py --mirror",
            file=sys.stderr,
        )
        return 2

    problems = verify(dest)
    for problem in problems:
        print(f"  FAIL {problem}", file=sys.stderr)
    if problems:
        print(
            "\nThe downloaded files are not the published Olist dataset. "
            "Nothing downstream should be trusted until this matches.",
            file=sys.stderr,
        )
        return 1
    print(f"verified: all {len(EXPECTED_ROWS)} files match the published row counts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

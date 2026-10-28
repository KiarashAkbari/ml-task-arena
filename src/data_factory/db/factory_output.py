"""Load the Stage 1 curated dataset into its own schema (chain requirement).

AGENTS.md 4.2: the second source of Stage 2 is the clean output of Stage 1,
and it must live in the *same* database, in a separate schema, loaded *by the
same package*.  This module is that second source: it reads the artifact
``clean`` already produced (``data/curated/cases.csv``) and writes it to
``factory.case_file``.

Nothing here re-implements Stage 1 logic.  If the file is missing, the honest
answer is "run the factory first", not "fetch the data again here" - the
Stage 1 pipeline stays the only writer of that dataset.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from sqlalchemy import Connection

from data_factory.db.loader import write_frame

CHUNK_ROWS = 100_000


@dataclass(frozen=True)
class FactoryLoadResult:
    rows: int
    source: str


def read_curated(path: Path) -> pd.DataFrame:
    """Read the curated case file in batches; keep only declared columns."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found - run `python -m data_factory run` (Stage 1) first, "
            "or point --curated at an existing cases.csv"
        )

    chunks = []
    for chunk in pd.read_csv(
        path,
        chunksize=CHUNK_ROWS,
        dtype={
            "unique_key": "int64",
            "agency": "string",
            "complaint_type": "string",
            "descriptor": "string",
            "borough": "string",
            "status": "string",
        },
    ):
        chunks.append(
            pd.DataFrame(
                {
                    "unique_key": chunk["unique_key"].astype("int64"),
                    "created_ts": pd.to_datetime(chunk["created_date"], errors="coerce"),
                    "closed_ts": pd.to_datetime(chunk["closed_date"], errors="coerce"),
                    "agency": chunk["agency"],
                    "complaint_type": chunk["complaint_type"],
                    "descriptor": chunk["descriptor"],
                    "borough": chunk["borough"],
                    "zip_code": pd.to_numeric(chunk["zip_code"], errors="coerce").astype("Int32"),
                    "status": chunk["status"],
                    "population": pd.to_numeric(chunk["population"], errors="coerce").astype("Int32"),
                    "duration_minutes": pd.to_numeric(chunk["duration_minutes"], errors="coerce").astype("Int32"),
                }
            )
        )
    return pd.concat(chunks, ignore_index=True)


def load_factory_output(conn: Connection, path: Path) -> FactoryLoadResult:
    frame = read_curated(path)
    rows = write_frame(conn, frame, "case_file", schema="factory", chunk_rows=10_000)
    return FactoryLoadResult(rows=rows, source=str(path))

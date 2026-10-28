"""Apply versioned ``.sql`` files, with an audit trail.

AGENTS.md 4.3.9 requires every piece of DDL to live in a versioned ``.sql``
file.  This module is the only thing that executes them, and it records the
file name + content hash in ``olist.ddl_history`` so the database can be
compared against the repository instead of trusted.

Statements are split by a small tokenizer rather than ``text.split(";")``,
because a PL/pgSQL body contains semicolons inside ``$fn$ … $fn$`` and a
naive split would cut a function in half.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, text

# src/data_factory/db/ddl.py -> parents[3] is the repository root
SQL_DIR = Path(__file__).resolve().parents[3] / "sql"


@dataclass(frozen=True)
class SqlFile:
    path: Path
    sha256: str
    statements: tuple[str, ...]

    @property
    def name(self) -> str:
        return self.path.name


def split_statements(script: str) -> tuple[str, ...]:
    """Split a SQL script on top-level semicolons.

    Handles single-quoted strings (including '' escapes), double-quoted
    identifiers, line comments, block comments and dollar-quoted bodies.
    """
    statements: list[str] = []
    buffer: list[str] = []
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ""

        if ch == "-" and nxt == "-":                     # line comment
            end = script.find("\n", i)
            end = n if end == -1 else end
            buffer.append(script[i:end])
            i = end
            continue

        if ch == "/" and nxt == "*":                     # block comment
            end = script.find("*/", i + 2)
            end = n if end == -1 else end + 2
            buffer.append(script[i:end])
            i = end
            continue

        if ch == "'":                                    # string literal
            j = i + 1
            while j < n:
                if script[j] == "'" and (j + 1 >= n or script[j + 1] != "'"):
                    j += 1
                    break
                j += 2 if script[j] == "'" else 1
            buffer.append(script[i:j])
            i = j
            continue

        if ch == '"':                                    # quoted identifier
            j = script.find('"', i + 1)
            j = n if j == -1 else j + 1
            buffer.append(script[i:j])
            i = j
            continue

        if ch == "$":                                    # dollar-quoted body
            close = script.find("$", i + 1)
            tag = script[i : close + 1] if close != -1 else ""
            if tag and (len(tag) == 2 or tag[1].isalpha() or tag[1] == "_"):
                end = script.find(tag, close + 1)
                if end != -1:
                    buffer.append(script[i : end + len(tag)])
                    i = end + len(tag)
                    continue

        if ch == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            i += 1
            continue

        buffer.append(ch)
        i += 1

    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return tuple(statements)


def load_sql_files(directory: Path | None = None) -> tuple[SqlFile, ...]:
    """Return the numbered scripts in filename order (01_, 02_, …)."""
    root = directory or SQL_DIR
    if not root.is_dir():
        raise FileNotFoundError(f"SQL directory not found: {root}")
    files = []
    for path in sorted(root.glob("[0-9][0-9]_*.sql")):
        raw = path.read_bytes()
        files.append(
            SqlFile(
                path=path,
                sha256=hashlib.sha256(raw).hexdigest(),
                statements=split_statements(raw.decode("utf-8")),
            )
        )
    if not files:
        raise FileNotFoundError(f"no numbered .sql files in {root}")
    return tuple(files)


def apply_file(conn: Connection, sql_file: SqlFile) -> int:
    """Execute one script statement by statement inside the caller's transaction."""
    for statement in sql_file.statements:
        conn.execute(text(statement))
    return len(sql_file.statements)


def record_applied(conn: Connection, sql_file: SqlFile) -> None:
    """Upsert the audit row. Re-applying an identical file is not an error."""
    conn.execute(
        text(
            """
            INSERT INTO olist.ddl_history (filename, sha256, applied_at)
            VALUES (:filename, :sha256, now())
            ON CONFLICT (filename) DO UPDATE
               SET sha256 = EXCLUDED.sha256,
                   applied_at = EXCLUDED.applied_at
            """
        ),
        {"filename": sql_file.name, "sha256": sql_file.sha256},
    )

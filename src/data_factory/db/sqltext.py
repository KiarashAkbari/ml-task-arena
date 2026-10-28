"""The one place where SQL text is assembled from parts.

AGENTS.md 4.3.8 forbids building queries by concatenation, and the spirit of
that rule is about **values**: a value must be a bind parameter, never string
interpolation.  Some things genuinely cannot be bind parameters in PostgreSQL -
table names, column names, index names, and the ``EXPLAIN`` prefix - because
they are identifiers, which the planner resolves before parameters exist.

So the rule this module enforces is narrower and checkable:

* every identifier that reaches SQL is validated against a strict pattern
  here, in one file, before it is interpolated;
* everywhere else in the package, ``text()`` receives a literal or a variable
  that came from this module.

The notebook's AST audit scans the package for interpolated ``text()`` calls
and exempts exactly this file, then verifies the guard below still exists - so
the exemption is checked rather than assumed.
"""

from __future__ import annotations

import re
from typing import Callable

from sqlalchemy import TextClause, text

#: Conservative identifier rule: lower_snake_case, max 63 bytes (PostgreSQL's
#: limit). Anything that does not match is refused rather than escaped, because
#: every identifier in this project comes from a module-level constant.
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

#: An index target is "schema.table (col, col)" with an optional INCLUDE list.
_INDEX_TARGET = re.compile(
    r"^[a-z_]+\.[a-z_]+ \([a-z_]+(?:, [a-z_]+)*\)"
    r"(?: INCLUDE \([a-z_]+(?:, [a-z_]+)*\))?$"
)


def ident(name: str) -> str:
    """Validate a SQL identifier. Raises instead of escaping."""
    if not isinstance(name, str) or not _IDENTIFIER.match(name):
        raise ValueError(
            f"refusing to interpolate {name!r} as a SQL identifier; "
            "identifiers must match ^[a-z_][a-z0-9_]{0,62}$"
        )
    return name


def table_ref(table: str, schema: str = "olist") -> str:
    return f"{ident(schema)}.{ident(table)}"


def analyze(table: str, schema: str = "olist") -> TextClause:
    return text("ANALYZE " + table_ref(table, schema))


def count_star(table: str, schema: str = "olist") -> TextClause:
    return text("SELECT count(*) FROM " + table_ref(table, schema))


def truncate(table: str, schema: str = "olist") -> TextClause:
    return text("TRUNCATE " + table_ref(table, schema))


def drop_index(name: str, schema: str = "olist") -> TextClause:
    return text(f"DROP INDEX {ident(schema)}.{ident(name)}")


def create_index(name: str, target: str) -> TextClause:
    """``target`` is 'schema.table (col, ...)' and is validated as a whole."""
    if not _INDEX_TARGET.match(target):
        raise ValueError(f"refusing index target {target!r}")
    return text(f"CREATE INDEX {ident(name)} ON {target}")


def create_database(name: str, quote: Callable[[str], str]) -> TextClause:
    """``quote`` is the dialect's identifier preparer, so quoting is not hand-rolled."""
    return text("CREATE DATABASE " + quote(name))


def explain(query: str, *, analyze: bool = True, buffers: bool = False,
            as_json: bool = False, sql: str | None = None) -> TextClause:
    """Wrap a query in EXPLAIN. The query itself is passed through untouched."""
    options = []
    if analyze:
        options.append("ANALYZE")
    if buffers:
        options.append("BUFFERS")
    if as_json:
        options.append("FORMAT JSON")
    return text(f"EXPLAIN ({', '.join(options)}) {sql if sql is not None else query}")


def latest_timestamp(table: str, column: str = "computed_at",
                     schema: str = "olist") -> TextClause:
    return text(f"SELECT max({ident(column)}) FROM {table_ref(table, schema)}")


def foreign_key_orphans(child: str, child_column: str, parent: str,
                        parent_column: str, schema: str = "olist") -> TextClause:
    """Rows in ``child`` whose FK has no parent row. Used by the load check."""
    return text(
        f"SELECT count(*) FROM {table_ref(child, schema)} c "
        f"LEFT JOIN {table_ref(parent, schema)} p "
        f"ON c.{ident(child_column)} = p.{ident(parent_column)} "
        f"WHERE c.{ident(child_column)} IS NOT NULL "
        f"AND p.{ident(parent_column)} IS NULL"
    )

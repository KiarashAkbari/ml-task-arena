"""Database connection management for the PostgreSQL destination.

Every connection in this project is created and disposed through this module,
so "no leaked connection" (AGENTS.md 4.3.8) is a property of one file rather
than a promise repeated in every caller.  Passwords are never printed: any
URL that reaches a log line goes through :func:`redact_url` first.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

from data_factory.db import sqltext
from data_factory.errors import ConfigError

MAINTENANCE_DATABASE = "postgres"


def parse_url(url: str) -> URL:
    """Validate the connection string early, with a message that says why."""
    if not url or not url.strip():
        raise ConfigError(
            "database URL is empty: pass --url or set DATABASE_URL, e.g. "
            "postgresql+psycopg://user:secret@localhost:5432/olist"
        )
    try:
        parsed = make_url(url)
    except Exception as exc:  # sqlalchemy raises ArgumentError
        raise ConfigError(f"cannot parse database URL: {exc}") from exc
    if not parsed.database:
        raise ConfigError("database URL has no database name (…/dbname)")
    return parsed


def redact_url(url: str) -> str:
    """Return the URL with the password replaced, safe for logs and reports."""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<unparseable url>"


def make_engine(url: str) -> Engine:
    """Build an engine that never leaves a half-open connection behind."""
    parsed = parse_url(url)
    return create_engine(
        parsed,
        pool_pre_ping=True,   # a recycled server connection is detected, not reused
        pool_size=2,
        max_overflow=0,
        future=True,
    )


@contextmanager
def engine_scope(url: str) -> Iterator[Engine]:
    """Engine lifetime == scope. ``dispose()`` closes every pooled connection.

    The notebook and the CLI both go through this, so after the process ends
    PostgreSQL reports no client from this application (verified in the
    notebook with ``pg_stat_activity``).
    """
    engine = make_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


def ensure_database(url: str) -> bool:
    """Create the target database if it does not exist yet.

    Connects to the maintenance database first, because PostgreSQL cannot
    ``CREATE DATABASE`` from inside the database being created.  Returns True
    when the database had to be created.
    """
    parsed = parse_url(url)
    target = parsed.database
    admin = parsed.set(database=MAINTENANCE_DATABASE)
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target},
            ).scalar()
            if exists:
                return False
            # The identifier is an identifier, so it cannot be a bind
            # parameter; quote it with the driver's own quoting rather than
            # pasting it into the statement.
            conn.execute(
                sqltext.create_database(target, admin_engine.dialect.identifier_preparer.quote)
            )
            return True
    finally:
        admin_engine.dispose()

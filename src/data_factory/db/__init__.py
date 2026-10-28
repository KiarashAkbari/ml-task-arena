"""PostgreSQL destination for the data factory (Stage 2).

The Stage 1 package wrote its outputs to CSV.  Stage 2 needs a second
destination, so this subpackage adds one instead of growing a parallel
one-off loader: ``bootstrap`` builds the whole database through the same
``Settings``/``DataFactoryError`` contracts the rest of the package uses.
"""

from data_factory.db.bootstrap import LoadReport, bootstrap
from data_factory.db.connection import engine_scope, ensure_database, make_engine, redact_url

__all__ = [
    "LoadReport",
    "bootstrap",
    "engine_scope",
    "ensure_database",
    "make_engine",
    "redact_url",
]

"""PostgreSQL / PostGIS access.

Pipelines use short-lived synchronous connections (``connect``); the API uses an
asynchronous pool created in ``narsat.api.app``. Both go through psycopg 3 and plain,
parameterised SQL: the queries are spatial and read better as SQL than through an ORM.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from narsat.config import DbSettings

Connection = psycopg.Connection[dict[str, Any]]

#: Every session runs in UTC, whatever the time zone of the server or of the machine
#: the client runs on: timestamps come back with a +00:00 offset and expressions such
#: as ``acq_ts::date`` mean the UTC day everywhere.
SESSION_OPTIONS = "-c timezone=UTC"


@contextmanager
def connect(settings: DbSettings, autocommit: bool = False) -> Iterator[Connection]:
    """Open a connection that returns rows as dictionaries.

    The block commits on success and rolls back on error (psycopg's connection
    context manager semantics).
    """
    with psycopg.connect(
        settings.dsn.get_secret_value(),
        row_factory=dict_row,
        autocommit=autocommit,
        application_name="narsat",
        options=SESSION_OPTIONS,
    ) as conn:
        yield conn


def wait_until_ready(settings: DbSettings, timeout_s: float = 60.0) -> None:
    """Block until the database accepts connections (used by container start-up)."""
    import time

    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with connect(settings, autocommit=True) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1.0)
    raise TimeoutError(f"database not reachable after {timeout_s:.0f}s") from last_error

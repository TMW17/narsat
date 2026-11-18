"""Publish the dbt marts to PostgreSQL for the API.

dbt builds the marts over the lakehouse and writes each one as a Parquet file
(``external`` materialisation). This step loads those files into the ``mart`` schema
so the API answers analytics questions from one database, with no access to the
lake. Each table is replaced inside a transaction: readers see the old content or
the new one, never an empty table.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
from psycopg import sql

from narsat.db import Connection
from narsat.log import get_logger

log = get_logger(__name__)

MART_TABLES = ("fire_activity_daily", "burned_area_admin_year", "event_summary")


class MartSchemaError(RuntimeError):
    """The Parquet file produced by dbt does not have the columns the API expects."""


def _expected_columns(conn: Connection, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'mart' AND table_name = %(table)s ORDER BY ordinal_position",
        {"table": table},
    ).fetchall()
    return [row["column_name"] for row in rows]


def publish_mart(conn: Connection, table: str, parquet_path: Path) -> int:
    if table not in MART_TABLES:
        raise ValueError(f"unknown mart {table!r}")
    expected = _expected_columns(conn, table)
    data = pq.read_table(parquet_path)
    missing = [column for column in expected if column not in data.column_names]
    if missing:
        raise MartSchemaError(f"{parquet_path.name} lacks columns {missing}")
    rows = [
        row
        for row in data.select(expected).to_pylist()
        # dbt-duckdb cannot write an empty file: for a model without rows it writes one
        # row of NULLs (and hides it in its own view). It is not data.
        if any(value is not None for value in row.values())
    ]

    target = sql.Identifier("mart", table)
    conn.execute(sql.SQL("TRUNCATE {}").format(target))
    statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
        target, sql.SQL(", ").join(sql.Identifier(column) for column in expected)
    )
    with conn.cursor() as cursor, cursor.copy(statement) as copy:
        for row in rows:
            copy.write_row([row[column] for column in expected])
    log.info("mart_published", table=table, rows=len(rows))
    return len(rows)


def publish_all(conn: Connection, marts_dir: Path) -> dict[str, int]:
    """Load every mart found in ``marts_dir`` (``<table>.parquet``)."""
    published: dict[str, int] = {}
    for table in MART_TABLES:
        path = marts_dir / f"{table}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"dbt output not found: {path} (run 'dbt build' first)")
        published[table] = publish_mart(conn, table, path)
    return published

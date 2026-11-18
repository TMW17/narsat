"""Alembic environment.

The schema is written as explicit SQL in the revision files (there are no ORM models
to autogenerate from), so this file only has to open a connection and run them.
The URL comes from ``narsat db upgrade`` or, when Alembic is called directly, from
``NARSAT_DB_DSN``.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from narsat.config import DbSettings

config = context.config


def _url() -> str:
    configured = config.get_main_option("sqlalchemy.url")
    return configured or DbSettings().sqlalchemy_url()


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

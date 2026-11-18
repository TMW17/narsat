"""Run Alembic migrations from code (``narsat db upgrade``).

The migration scripts ship inside the package, so the same command works from a
source checkout, an installed wheel and a container image.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from narsat.config import DbSettings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(settings: DbSettings) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # '%' must be doubled: Alembic stores options in a ConfigParser.
    config.set_main_option("sqlalchemy.url", settings.sqlalchemy_url().replace("%", "%%"))
    return config


def upgrade(settings: DbSettings, revision: str = "head") -> None:
    command.upgrade(alembic_config(settings), revision)


def downgrade(settings: DbSettings, revision: str) -> None:
    command.downgrade(alembic_config(settings), revision)


def current_revision(settings: DbSettings) -> str | None:
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    engine = create_engine(settings.sqlalchemy_url())
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()

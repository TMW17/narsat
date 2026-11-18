"""dbt-duckdb plugin: expose the Iceberg tables of the lakehouse as dbt sources.

dbt-duckdb ships an ``iceberg`` plugin, but it takes the catalog properties from
``profiles.yml``; they would then exist twice (once for the pipeline, once for dbt)
and drift. This plugin builds the catalog from the same ``NARSAT_LAKE_*`` settings as
the rest of the platform, so ``dbt build`` always reads the lake the pipeline writes.

Declared in ``transform/profiles.yml``::

    plugins:
      - module: narsat.transform.iceberg_source
        alias: lakehouse

and used by a source with ``config: {plugin: lakehouse}``. The table is read with
PyIceberg (no DuckDB extension to download) and handed to DuckDB as an Arrow table.
A source may set ``selected_fields`` to read only the columns the models need.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dbt.adapters.duckdb.plugins import BasePlugin
from dbt.adapters.duckdb.utils import SourceConfig

from narsat.config import LakeSettings
from narsat.lake.catalog import load_catalog

MARTS_DIR_ENV = "NARSAT_MARTS_DIR"
DEFAULT_MARTS_DIR = "var/marts"


class Plugin(BasePlugin):  # type: ignore[misc]
    """Loads ``<schema>.<identifier>`` from the NarSat Iceberg catalog."""

    def initialize(self, plugin_config: dict[str, Any]) -> None:
        self._catalog = load_catalog(LakeSettings())
        # The marts are written as Parquet files ("external" materialisation) and DuckDB
        # does not create missing directories.
        Path(os.environ.get(MARTS_DIR_ENV, DEFAULT_MARTS_DIR)).mkdir(parents=True, exist_ok=True)

    def load(self, source_config: SourceConfig) -> Any:
        table = self._catalog.load_table(f"{source_config.schema}.{source_config.identifier}")
        fields = source_config.get("selected_fields")
        scan = table.scan(selected_fields=tuple(fields)) if fields else table.scan()
        return scan.to_arrow()

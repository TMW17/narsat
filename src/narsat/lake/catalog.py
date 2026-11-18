"""Apache Iceberg catalog and table definitions.

The catalog is PyIceberg's SQL catalog: its two bookkeeping tables live in SQLite
for local work and in PostgreSQL in the Compose stack, so the lakehouse needs no
catalog service of its own. Any engine that speaks Iceberg can read the tables;
moving to a REST catalog later only changes ``load_catalog``.

Partitioning uses an explicit ``acq_month`` column with the identity transform.
That keeps writes independent of the optional ``pyiceberg-core`` extension and lets
the silver build replace whole months atomically (dynamic partition overwrite).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pyiceberg.catalog import Catalog
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC, PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import Table
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import (
    BinaryType,
    BooleanType,
    DoubleType,
    IntegerType,
    LongType,
    NestedField,
    StringType,
    TimestamptzType,
)

from narsat.config import LakeSettings
from narsat.lake.storage import resolve_uri


@dataclass(frozen=True)
class TableDef:
    identifier: str
    schema: Schema
    partition_by: str | None = None

    def spec(self) -> PartitionSpec:
        if self.partition_by is None:
            return UNPARTITIONED_PARTITION_SPEC
        source = self.schema.find_field(self.partition_by)
        return PartitionSpec(
            PartitionField(
                source_id=source.field_id,
                field_id=1000,
                transform=IdentityTransform(),
                name=self.partition_by,
            )
        )


def _fields(*columns: tuple[str, object, bool]) -> Schema:
    return Schema(
        *[
            NestedField(field_id=index, name=name, field_type=kind, required=required)
            for index, (name, kind, required) in enumerate(columns, start=1)
        ]
    )


_HOTSPOT_MEASUREMENTS: tuple[tuple[str, object, bool], ...] = (
    ("hotspot_id", StringType(), True),
    ("source", StringType(), True),
    ("satellite", StringType(), True),
    ("instrument", StringType(), True),
    ("acq_ts", TimestamptzType(), True),
    ("latitude", DoubleType(), True),
    ("longitude", DoubleType(), True),
    ("brightness_k", DoubleType(), False),
    ("brightness_2_k", DoubleType(), False),
    ("scan_km", DoubleType(), False),
    ("track_km", DoubleType(), False),
    ("frp_mw", DoubleType(), False),
    ("confidence_raw", StringType(), True),
    ("confidence_level", StringType(), True),
    ("daynight", StringType(), False),
    ("version", StringType(), False),
    ("type_code", IntegerType(), False),
)

BRONZE_HOTSPOTS = TableDef(
    "bronze.firms_hotspots",
    _fields(
        *_HOTSPOT_MEASUREMENTS,
        ("fetched_at", TimestamptzType(), True),
        ("ingested_at", TimestamptzType(), True),
        ("kafka_topic", StringType(), True),
        ("kafka_partition", IntegerType(), True),
        ("kafka_offset", LongType(), True),
        ("acq_month", StringType(), True),
    ),
    partition_by="acq_month",
)

SILVER_HOTSPOTS = TableDef(
    "silver.hotspots",
    _fields(
        *_HOTSPOT_MEASUREMENTS,
        ("first_ingested_at", TimestamptzType(), True),
        ("is_static_source", BooleanType(), True),
        ("country_iso3", StringType(), False),
        ("admin1_name", StringType(), False),
        ("event_id", StringType(), False),
        ("acq_month", StringType(), True),
    ),
    partition_by="acq_month",
)

SILVER_EVENTS = TableDef(
    "silver.fire_events",
    _fields(
        ("event_id", StringType(), True),
        ("status", StringType(), True),
        ("mapping_status", StringType(), True),
        ("first_seen", TimestamptzType(), True),
        ("last_seen", TimestamptzType(), True),
        ("n_hotspots", IntegerType(), True),
        ("n_high_confidence", IntegerType(), True),
        ("frp_max_mw", DoubleType(), False),
        ("frp_sum_mw", DoubleType(), False),
        ("country_iso3", StringType(), False),
        ("admin1_name", StringType(), False),
        ("centroid_lon", DoubleType(), True),
        ("centroid_lat", DoubleType(), True),
        ("hull_wkb", BinaryType(), True),
        ("hull_area_ha", DoubleType(), True),
        ("merged_into", StringType(), False),
        ("updated_at", TimestamptzType(), True),
    ),
)

GOLD_BURNED_AREAS = TableDef(
    "gold.burned_areas",
    _fields(
        ("ba_id", StringType(), True),
        ("event_id", StringType(), True),
        ("method", StringType(), True),
        ("model_version", StringType(), True),
        ("pre_datetime", TimestamptzType(), False),
        ("post_datetime", TimestamptzType(), True),
        ("threshold", DoubleType(), False),
        ("clear_fraction", DoubleType(), True),
        ("area_ha", DoubleType(), True),
        ("n_polygons", IntegerType(), True),
        ("qa_status", StringType(), True),
        ("geom_wkb", BinaryType(), True),
        ("xmin", DoubleType(), False),
        ("ymin", DoubleType(), False),
        ("xmax", DoubleType(), False),
        ("ymax", DoubleType(), False),
        ("updated_at", TimestamptzType(), True),
    ),
)

GOLD_VALIDATION = TableDef(
    "gold.burned_area_validation",
    _fields(
        ("ba_id", StringType(), True),
        ("event_id", StringType(), True),
        ("method", StringType(), True),
        ("ref_source", StringType(), True),
        ("n_ref_perimeters", IntegerType(), True),
        ("mapped_ha", DoubleType(), True),
        ("ref_ha", DoubleType(), True),
        ("intersection_ha", DoubleType(), True),
        ("union_ha", DoubleType(), True),
        ("iou", DoubleType(), False),
        ("precision_ref", DoubleType(), False),
        ("recall_ref", DoubleType(), False),
        ("computed_at", TimestamptzType(), True),
    ),
)

ALL_TABLES = (BRONZE_HOTSPOTS, SILVER_HOTSPOTS, SILVER_EVENTS, GOLD_BURNED_AREAS, GOLD_VALIDATION)


def catalog_properties(settings: LakeSettings) -> dict[str, str]:
    """Properties understood by PyIceberg (also used to configure dbt's Iceberg plugin)."""
    uri = settings.catalog_uri.get_secret_value()
    if uri.startswith("sqlite:///"):
        # SQLite catalog file: anchor a relative path and make sure the directory exists
        # (SQLite creates the file, not its parents).
        path = Path(uri[len("sqlite:///") :]).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        uri = f"sqlite:///{path}"
    properties = {"uri": uri, "warehouse": resolve_uri(settings.warehouse_uri)}
    if settings.warehouse_uri.startswith("s3://"):
        properties["s3.region"] = settings.s3_region
        if settings.s3_endpoint:
            properties["s3.endpoint"] = settings.s3_endpoint
        if settings.s3_access_key and settings.s3_secret_key:
            properties["s3.access-key-id"] = settings.s3_access_key.get_secret_value()
            properties["s3.secret-access-key"] = settings.s3_secret_key.get_secret_value()
    return properties


def load_catalog(settings: LakeSettings) -> Catalog:
    return SqlCatalog(settings.catalog_name, **catalog_properties(settings))


def ensure_tables(catalog: Catalog) -> dict[str, Table]:
    """Create namespaces and tables that do not exist yet; returns all of them."""
    tables: dict[str, Table] = {}
    for definition in ALL_TABLES:
        namespace = definition.identifier.split(".", 1)[0]
        catalog.create_namespace_if_not_exists(namespace)
        tables[definition.identifier] = catalog.create_table_if_not_exists(
            definition.identifier,
            schema=definition.schema,
            partition_spec=definition.spec(),
            properties={"write.parquet.compression-codec": "zstd"},
        )
    return tables

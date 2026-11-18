"""Silver tables: validated, de-duplicated and enriched.

``silver.hotspots`` is derived from bronze (one row per detection, the copy that
arrived first) and enriched from the operational store with the attributes that only
exist there: the fire event, the administrative unit and the static-source flag.
It is rebuilt month by month with a dynamic partition overwrite, so a rebuild is
atomic and re-running it gives the same table.

``silver.fire_events`` is a full snapshot of the event table.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pyarrow as pa
from pyiceberg.catalog import Catalog
from pyiceberg.expressions import In

from narsat.db import Connection
from narsat.lake.catalog import BRONZE_HOTSPOTS, SILVER_EVENTS, SILVER_HOTSPOTS
from narsat.log import get_logger

log = get_logger(__name__)


def months_between(start: dt.date, end: dt.date) -> list[str]:
    """``YYYY-MM`` labels of every month touched by ``start..end`` (inclusive)."""
    if end < start:
        raise ValueError("end must not be before start")
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def available_months(catalog: Catalog) -> list[str]:
    """Months that currently have data in bronze."""
    table = catalog.load_table(BRONZE_HOTSPOTS.identifier)
    partitions = table.inspect.partitions().to_pylist()
    return sorted({p["partition"]["acq_month"] for p in partitions if p["record_count"] > 0})


def read_enrichment(conn: Connection, months: list[str]) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT h.hotspot_id::text AS hotspot_id, h.is_static_source,
               h.country_iso3::text AS country_iso3, a.name AS admin1_name,
               h.event_id::text AS event_id
        FROM hotspot h
        LEFT JOIN admin_unit a ON a.admin_id = h.admin1_id
        WHERE to_char(h.acq_ts AT TIME ZONE 'UTC', 'YYYY-MM') = ANY(%(months)s)
        """,
        {"months": months},
    ).fetchall()
    columns = ["hotspot_id", "is_static_source", "country_iso3", "admin1_name", "event_id"]
    return pd.DataFrame(rows, columns=columns)


def build_silver_hotspots_frame(bronze: pd.DataFrame, enrichment: pd.DataFrame) -> pd.DataFrame:
    """Pure transformation bronze -> silver (unit-tested without any storage)."""
    if bronze.empty:
        return pd.DataFrame(columns=SILVER_HOTSPOTS.schema.column_names)
    ordered = bronze.sort_values(["hotspot_id", "ingested_at", "kafka_offset"])
    first = ordered.drop_duplicates("hotspot_id", keep="first").copy()
    first = first.rename(columns={"ingested_at": "first_ingested_at"})
    merged = first.merge(enrichment, on="hotspot_id", how="left", validate="one_to_one")
    merged["is_static_source"] = (
        merged["is_static_source"].map(lambda v: bool(v) if pd.notna(v) else False).astype(bool)
    )
    for column in ("country_iso3", "admin1_name", "event_id"):
        merged[column] = merged[column].astype("object").where(merged[column].notna(), None)
    result: pd.DataFrame = merged[SILVER_HOTSPOTS.schema.column_names].reset_index(drop=True)
    return result


def read_bronze_months(catalog: Catalog, months: list[str]) -> pd.DataFrame:
    table = catalog.load_table(BRONZE_HOTSPOTS.identifier)
    frame: pd.DataFrame = table.scan(row_filter=In("acq_month", months)).to_pandas()
    return frame


def to_arrow(frame: pd.DataFrame, schema: pa.Schema) -> pa.Table:
    """Convert with the table's exact Arrow schema (types, nullability, field ids)."""
    return pa.Table.from_pandas(frame, schema=schema, preserve_index=False)


def write_silver_hotspots(catalog: Catalog, frame: pd.DataFrame) -> int:
    table = catalog.load_table(SILVER_HOTSPOTS.identifier)
    if frame.empty:
        return 0
    table.dynamic_partition_overwrite(to_arrow(frame, table.schema().as_arrow()))
    log.info(
        "silver_hotspots_written",
        rows=len(frame),
        months=sorted(frame["acq_month"].unique().tolist()),
    )
    return len(frame)


def read_events_frame(conn: Connection) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT e.event_id::text AS event_id, e.status, e.mapping_status, e.first_seen,
               e.last_seen, e.n_hotspots, e.n_high_confidence, e.frp_max_mw, e.frp_sum_mw,
               e.country_iso3::text AS country_iso3, a.name AS admin1_name,
               ST_X(e.centroid) AS centroid_lon, ST_Y(e.centroid) AS centroid_lat,
               ST_AsBinary(e.hull) AS hull_wkb, e.hull_area_ha,
               e.merged_into::text AS merged_into, e.updated_at
        FROM fire_event e
        LEFT JOIN admin_unit a ON a.admin_id = e.admin1_id
        ORDER BY e.event_id
        """
    ).fetchall()
    frame = pd.DataFrame(rows, columns=SILVER_EVENTS.schema.column_names)
    if not frame.empty:
        frame["hull_wkb"] = frame["hull_wkb"].map(bytes)
        for column in ("first_seen", "last_seen", "updated_at"):
            frame[column] = pd.to_datetime(frame[column], utc=True)
        for column in ("n_hotspots", "n_high_confidence"):
            frame[column] = frame[column].astype("int32")
    return frame


def write_silver_events(catalog: Catalog, frame: pd.DataFrame) -> int:
    table = catalog.load_table(SILVER_EVENTS.identifier)
    table.overwrite(to_arrow(frame, table.schema().as_arrow()))
    log.info("silver_events_written", rows=len(frame))
    return len(frame)

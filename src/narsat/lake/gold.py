"""Gold tables: burned-area products and their validation, snapshotted from PostGIS.

Geometries are stored as WKB with their bounding box in plain columns, the layout
GeoParquet uses, so the data files can be read by DuckDB spatial, GeoPandas or QGIS
without going through the catalog.
"""

from __future__ import annotations

import pandas as pd
from pyiceberg.catalog import Catalog

from narsat.db import Connection
from narsat.lake.catalog import GOLD_BURNED_AREAS, GOLD_VALIDATION
from narsat.lake.silver import to_arrow
from narsat.log import get_logger

log = get_logger(__name__)


def read_burned_areas_frame(conn: Connection) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT b.ba_id::text AS ba_id, b.event_id::text AS event_id, b.method, b.model_version,
               b.pre_datetime, b.post_datetime, b.threshold, b.clear_fraction, b.area_ha,
               b.n_polygons, b.qa_status, ST_AsBinary(b.geom) AS geom_wkb,
               ST_XMin(b.geom) AS xmin, ST_YMin(b.geom) AS ymin,
               ST_XMax(b.geom) AS xmax, ST_YMax(b.geom) AS ymax, b.updated_at
        FROM burned_area b
        JOIN fire_event e ON e.event_id = b.event_id
        WHERE e.status IN ('active', 'cooling', 'closed')
        ORDER BY b.ba_id
        """
    ).fetchall()
    frame = pd.DataFrame(rows, columns=GOLD_BURNED_AREAS.schema.column_names)
    if not frame.empty:
        frame["geom_wkb"] = frame["geom_wkb"].map(bytes)
        for column in ("pre_datetime", "post_datetime", "updated_at"):
            frame[column] = pd.to_datetime(frame[column], utc=True)
        frame["n_polygons"] = frame["n_polygons"].astype("int32")
    return frame


def read_validation_frame(conn: Connection) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT v.ba_id::text AS ba_id, b.event_id::text AS event_id, b.method, v.ref_source,
               v.n_ref_perimeters, v.mapped_ha, v.ref_ha, v.intersection_ha, v.union_ha,
               v.iou, v.precision_ref, v.recall_ref, v.computed_at
        FROM burned_area_validation v
        JOIN burned_area b ON b.ba_id = v.ba_id
        ORDER BY v.ba_id, v.ref_source
        """
    ).fetchall()
    frame = pd.DataFrame(rows, columns=GOLD_VALIDATION.schema.column_names)
    if not frame.empty:
        frame["computed_at"] = pd.to_datetime(frame["computed_at"], utc=True)
        frame["n_ref_perimeters"] = frame["n_ref_perimeters"].astype("int32")
    return frame


def write_gold(catalog: Catalog, burned: pd.DataFrame, validation: pd.DataFrame) -> dict[str, int]:
    """Replace both gold tables with the given snapshots."""
    written: dict[str, int] = {}
    for definition, frame in ((GOLD_BURNED_AREAS, burned), (GOLD_VALIDATION, validation)):
        table = catalog.load_table(definition.identifier)
        table.overwrite(to_arrow(frame, table.schema().as_arrow()))
        written[definition.identifier] = len(frame)
    log.info("gold_written", **{k.replace(".", "_"): v for k, v in written.items()})
    return written

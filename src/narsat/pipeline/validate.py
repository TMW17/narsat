"""Agreement between mapped burned areas and independent perimeters.

Two comparisons are stored in ``burned_area_validation``:

* against an external reference (for example EFFIS or NEALGEBA perimeters loaded
  with ``narsat reference load-perimeters``): the reference polygons that lie within
  ``match_m`` of one of the event's own detections, and whose date (if they have one)
  is compatible with the event. By default ``match_m`` is the mapping's seed distance
  (750 m), the same rule that decides which burned patches belong to the event in
  NarSat's own maps. A wider radius around the whole event would also take in
  neighbouring fires of the same complex, which then count as "missed" and push recall
  towards zero. For the same reason the matched reference is clipped to the area the
  mapper examined (the event outline buffered by ``aoi_m``, 1.5 km by default): one
  reference polygon can cover a whole fire complex, and the burn outside that area was
  never in this event's images;
* between the project's own methods: each model product is compared with the dNBR
  product of the same event (``ref_source = 'narsat:dnbr'``).

Metrics (areas are geodesic, computed by PostGIS on the geography type):

* ``iou``            intersection / union
* ``precision_ref``  intersection / mapped area  (how much of the map the reference confirms)
* ``recall_ref``     intersection / reference area (how much of the reference was mapped)

A reference perimeter is not ground truth (EFFIS maps fires of roughly 30 ha and
more from coarser imagery), so these numbers measure agreement, not accuracy.
"""

from __future__ import annotations

from narsat.db import Connection
from narsat.log import get_logger

log = get_logger(__name__)

_METRICS = """
    mapped_ha, ref_ha, intersection_ha,
    mapped_ha + ref_ha - intersection_ha AS union_ha,
    intersection_ha / NULLIF(mapped_ha + ref_ha - intersection_ha, 0) AS iou,
    intersection_ha / NULLIF(mapped_ha, 0) AS precision_ref,
    intersection_ha / NULLIF(ref_ha, 0) AS recall_ref
"""

_UPSERT = """
ON CONFLICT (ba_id, ref_source) DO UPDATE
SET n_ref_perimeters = EXCLUDED.n_ref_perimeters, mapped_ha = EXCLUDED.mapped_ha,
    ref_ha = EXCLUDED.ref_ha, intersection_ha = EXCLUDED.intersection_ha,
    union_ha = EXCLUDED.union_ha, iou = EXCLUDED.iou, precision_ref = EXCLUDED.precision_ref,
    recall_ref = EXCLUDED.recall_ref, computed_at = now()
"""

_AGAINST_REFERENCE = f"""
WITH matched AS (
    SELECT b.ba_id, b.geom AS mapped,
           ST_CollectionExtract(ST_Intersection(
               ST_Union(ST_MakeValid(r.geom)),
               ST_Buffer(e.hull::geography, %(aoi_m)s)::geometry), 3) AS reference,
           count(*) AS n
    FROM burned_area b
    JOIN fire_event e ON e.event_id = b.event_id
    JOIN reference_perimeter r
      ON r.source = %(source)s
     -- Index-friendly prefilter; 1 degree of longitude is at least 80 km below 44 N.
     AND r.geom && ST_Expand(e.hull, %(match_m)s / 80000.0)
     AND EXISTS (
         SELECT 1 FROM hotspot h
         WHERE h.event_id = e.event_id
           AND ST_DWithin(r.geom::geography, h.geom::geography, %(match_m)s))
     AND (r.fire_date IS NULL
          OR r.fire_date BETWEEN (e.first_seen AT TIME ZONE 'UTC')::date - %(days_before)s
                             AND (e.last_seen AT TIME ZONE 'UTC')::date + %(days_after)s)
    WHERE e.status IN ('active', 'cooling', 'closed')
    GROUP BY b.ba_id, b.geom, e.hull
), areas AS (
    SELECT ba_id, n,
           ST_Area(mapped::geography) / 10000.0 AS mapped_ha,
           ST_Area(reference::geography) / 10000.0 AS ref_ha,
           ST_Area(ST_Intersection(ST_MakeValid(mapped), ST_MakeValid(reference))::geography)
               / 10000.0 AS intersection_ha
    FROM matched
)
INSERT INTO burned_area_validation (
    ba_id, ref_source, n_ref_perimeters, mapped_ha, ref_ha, intersection_ha, union_ha, iou,
    precision_ref, recall_ref
)
SELECT ba_id, %(source)s, n, {_METRICS}
FROM areas
{_UPSERT}
"""

_BETWEEN_METHODS = f"""
WITH pairs AS (
    SELECT m.ba_id, m.geom AS mapped, d.geom AS reference
    FROM burned_area m
    JOIN burned_area d ON d.event_id = m.event_id AND d.method = 'dnbr'
    WHERE m.method = 'model'
), areas AS (
    SELECT ba_id, 1 AS n,
           ST_Area(mapped::geography) / 10000.0 AS mapped_ha,
           ST_Area(reference::geography) / 10000.0 AS ref_ha,
           ST_Area(ST_Intersection(ST_MakeValid(mapped), ST_MakeValid(reference))::geography)
               / 10000.0 AS intersection_ha
    FROM pairs
)
INSERT INTO burned_area_validation (
    ba_id, ref_source, n_ref_perimeters, mapped_ha, ref_ha, intersection_ha, union_ha, iou,
    precision_ref, recall_ref
)
SELECT ba_id, 'narsat:dnbr', n, {_METRICS}
FROM areas
{_UPSERT}
"""


def compute_agreement(
    conn: Connection,
    match_m: float = 750.0,
    aoi_m: float = 1500.0,
    days_before: int = 7,
    days_after: int = 45,
) -> dict[str, int]:
    """Recompute every comparison. Returns the number of rows written per reference source.

    Each source is recomputed from scratch in one transaction, so a product that no
    longer matches any perimeter loses its old row instead of keeping a stale one.
    """
    written: dict[str, int] = {}
    sources = [
        row["source"]
        for row in conn.execute("SELECT DISTINCT source FROM reference_perimeter").fetchall()
    ]
    with conn.transaction():
        conn.execute(
            "DELETE FROM burned_area_validation "
            "WHERE ref_source <> 'narsat:dnbr' AND NOT (ref_source = ANY(%(sources)s))",
            {"sources": sources},
        )
        for source in sources:
            conn.execute(
                "DELETE FROM burned_area_validation WHERE ref_source = %(source)s",
                {"source": source},
            )
            cursor = conn.execute(
                _AGAINST_REFERENCE,
                {
                    "source": source,
                    "match_m": match_m,
                    "aoi_m": aoi_m,
                    "days_before": days_before,
                    "days_after": days_after,
                },
            )
            written[source] = cursor.rowcount
        written["narsat:dnbr"] = conn.execute(_BETWEEN_METHODS).rowcount
    # Comparisons whose products no longer exist disappear through ON DELETE CASCADE.
    log.info("agreement_computed", **{k.replace(":", "_"): v for k, v in written.items()})
    return written

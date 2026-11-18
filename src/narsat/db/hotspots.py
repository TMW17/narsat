"""Hotspot persistence in PostGIS (the operational store behind the live map)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from narsat.db import Connection
from narsat.schemas import HotspotMessage

_INSERT = """
INSERT INTO hotspot (
    hotspot_id, source, satellite, instrument, acq_ts, latitude, longitude,
    brightness_k, brightness_2_k, scan_km, track_km, frp_mw,
    confidence_raw, confidence_level, daynight, version, type_code,
    is_static_source, fetched_at
)
SELECT u.hotspot_id, u.source, u.satellite, u.instrument, u.acq_ts, u.latitude, u.longitude,
       u.brightness_k, u.brightness_2_k, u.scan_km, u.track_km, u.frp_mw,
       u.confidence_raw, u.confidence_level, u.daynight, u.version, u.type_code,
       -- Standard-processing files label volcanoes (1), other static land sources (2)
       -- and offshore detections (3) explicitly.
       COALESCE(u.type_code IN (1, 2, 3), false),
       u.fetched_at
FROM unnest(
    %(hotspot_id)s::uuid[], %(source)s::text[], %(satellite)s::text[], %(instrument)s::text[],
    %(acq_ts)s::timestamptz[], %(latitude)s::float8[], %(longitude)s::float8[],
    %(brightness_k)s::float8[], %(brightness_2_k)s::float8[], %(scan_km)s::float8[],
    %(track_km)s::float8[], %(frp_mw)s::float8[], %(confidence_raw)s::text[],
    %(confidence_level)s::text[], %(daynight)s::text[], %(version)s::text[],
    %(type_code)s::int2[], %(fetched_at)s::timestamptz[]
) AS u (
    hotspot_id, source, satellite, instrument, acq_ts, latitude, longitude,
    brightness_k, brightness_2_k, scan_km, track_km, frp_mw,
    confidence_raw, confidence_level, daynight, version, type_code, fetched_at
)
ON CONFLICT (hotspot_id) DO NOTHING
RETURNING hotspot_id
"""

_FLAG_KNOWN_STATIC = """
UPDATE hotspot h
SET is_static_source = true
FROM static_cell s
WHERE h.hotspot_id = ANY(%(ids)s)
  AND NOT h.is_static_source
  AND floor(h.latitude / s.cell_deg)::int = s.cell_y
  AND floor(h.longitude / s.cell_deg)::int = s.cell_x
"""

_ATTRIBUTE_ADMIN = """
UPDATE hotspot h
SET country_iso3 = a.country_iso3,
    admin1_id = CASE WHEN a.level = 1 THEN a.admin_id ELSE h.admin1_id END
FROM admin_unit a
WHERE h.hotspot_id = ANY(%(ids)s)
  AND a.level = %(level)s
  AND ST_Intersects(a.geom, h.geom)
"""

_COLUMNS = (
    "hotspot_id", "source", "satellite", "instrument", "acq_ts", "latitude", "longitude",
    "brightness_k", "brightness_2_k", "scan_km", "track_km", "frp_mw", "confidence_raw",
    "confidence_level", "daynight", "version", "type_code", "fetched_at",
)  # fmt: skip


def insert_hotspots(conn: Connection, messages: Sequence[HotspotMessage]) -> list[uuid.UUID]:
    """Insert detections that are not stored yet; returns the identifiers actually added.

    ``ON CONFLICT DO NOTHING`` makes the call idempotent: replaying a Kafka batch
    inserts nothing the second time.
    """
    if not messages:
        return []
    params = {column: [getattr(m, column) for m in messages] for column in _COLUMNS}
    inserted = [row["hotspot_id"] for row in conn.execute(_INSERT, params).fetchall()]
    if inserted:
        conn.execute(_FLAG_KNOWN_STATIC, {"ids": inserted})
        attribute_admin(conn, inserted)
    return inserted


def attribute_admin(conn: Connection, hotspot_ids: Sequence[uuid.UUID]) -> None:
    """Attach country (level 0) and first-level admin unit by point-in-polygon.

    A no-op until boundaries have been loaded (``narsat reference load-admin``).
    """
    if not hotspot_ids:
        return
    ids = list(hotspot_ids)
    conn.execute(_ATTRIBUTE_ADMIN, {"ids": ids, "level": 0})
    conn.execute(_ATTRIBUTE_ADMIN, {"ids": ids, "level": 1})


def backfill_admin(conn: Connection) -> int:
    """Attribute every hotspot that has no country yet (after loading boundaries)."""
    total = 0
    for level in (0, 1):
        cursor = conn.execute(
            """
            UPDATE hotspot h
            SET country_iso3 = a.country_iso3,
                admin1_id = CASE WHEN a.level = 1 THEN a.admin_id ELSE h.admin1_id END
            FROM admin_unit a
            WHERE a.level = %(level)s
              AND (h.country_iso3 IS NULL OR (a.level = 1 AND h.admin1_id IS NULL))
              AND ST_Intersects(a.geom, h.geom)
            """,
            {"level": level},
        )
        total += cursor.rowcount
    return total

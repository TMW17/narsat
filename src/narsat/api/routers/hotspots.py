"""Raw detections as GeoJSON (for analysis; the map itself uses vector tiles)."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response

from narsat.api.deps import ConnDep, SettingsDep, ensure_utc, parse_bbox

router = APIRouter(tags=["hotspots"])

GEOJSON = "application/geo+json"

_QUERY = """
WITH selected AS (
    SELECT h.hotspot_id, h.geom, h.acq_ts, h.instrument, h.satellite, h.source, h.frp_mw,
           h.brightness_k, h.confidence_level, h.daynight, h.is_static_source,
           h.country_iso3::text AS country_iso3, h.event_id
    FROM hotspot h
    WHERE (%(include_static)s OR NOT h.is_static_source)
      AND (%(start)s::timestamptz IS NULL OR h.acq_ts >= %(start)s)
      AND (%(end)s::timestamptz IS NULL OR h.acq_ts <= %(end)s)
      AND (%(west)s::float8 IS NULL
           OR h.geom && ST_MakeEnvelope(%(west)s, %(south)s, %(east)s, %(north)s, 4326))
    ORDER BY h.acq_ts DESC, h.hotspot_id
    LIMIT %(limit)s
)
SELECT jsonb_build_object(
    'type', 'FeatureCollection',
    'numberReturned', count(*),
    'features', COALESCE(jsonb_agg(jsonb_build_object(
        'type', 'Feature',
        'id', s.hotspot_id,
        'geometry', ST_AsGeoJSON(s.geom, 5)::jsonb,
        'properties', to_jsonb(s) - 'geom' - 'hotspot_id')), '[]'::jsonb)
)::text AS body
FROM selected s
"""


@router.get("/hotspots", response_class=Response, responses={200: {"content": {GEOJSON: {}}}})
async def list_hotspots(
    conn: ConnDep,
    settings: SettingsDep,
    bbox: Annotated[str | None, Query(description="west,south,east,north")] = None,
    start: Annotated[dt.datetime | None, Query()] = None,
    end: Annotated[dt.datetime | None, Query()] = None,
    include_static: Annotated[
        bool, Query(description="Include persistent sources such as gas flares")
    ] = False,
    limit: Annotated[int, Query(ge=1)] = 1000,
) -> Response:
    """Most recent detections first, as a GeoJSON FeatureCollection."""
    box = parse_bbox(bbox)
    params: dict[str, Any] = {
        "include_static": include_static,
        "start": ensure_utc(start),
        "end": ensure_utc(end),
        "west": box.west if box else None,
        "south": box.south if box else None,
        "east": box.east if box else None,
        "north": box.north if box else None,
        "limit": min(limit, settings.api.max_page_size * 10),
    }
    row = await (await conn.execute(_QUERY, params)).fetchone()
    return Response(row["body"] if row else "{}", media_type=GEOJSON)

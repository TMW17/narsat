"""Fire events, their detections and their burned-area products.

GeoJSON is assembled by PostGIS and passed through as text: the API never
re-serialises geometries in Python.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from narsat.api import auth
from narsat.api.deps import ConnDep, SettingsDep, ensure_utc, parse_bbox, require_role
from narsat.config import Settings

router = APIRouter(tags=["events"])

GEOJSON = "application/geo+json"
LIVE = ("active", "cooling", "closed")

_EVENT_PROPERTIES = """
    e.event_id, e.status, e.mapping_status, e.mapping_note, e.first_seen, e.last_seen,
    e.n_hotspots, e.n_high_confidence, e.frp_max_mw, e.frp_sum_mw, e.sensors,
    round(e.hull_area_ha::numeric, 1)::float8 AS hull_area_ha,
    e.country_iso3::text AS country_iso3, a.name AS admin1_name,
    (SELECT jsonb_object_agg(b.method, round(b.area_ha::numeric, 2)::float8)
       FROM burned_area b WHERE b.event_id = e.event_id) AS burned_area_ha
"""

_LIST = f"""
WITH selected AS (
    SELECT {_EVENT_PROPERTIES},
           CASE WHEN %(geometry)s = 'centroid' THEN e.centroid ELSE e.hull END AS geom
    FROM fire_event e
    LEFT JOIN admin_unit a ON a.admin_id = e.admin1_id
    WHERE e.status = ANY(%(statuses)s)
      AND e.n_hotspots >= %(min_hotspots)s
      AND (%(start)s::timestamptz IS NULL OR e.last_seen >= %(start)s)
      AND (%(end)s::timestamptz IS NULL OR e.first_seen <= %(end)s)
      AND (%(country)s::text IS NULL OR e.country_iso3 = %(country)s)
      AND (%(mapping_status)s::text IS NULL OR e.mapping_status = %(mapping_status)s)
      AND (%(west)s::float8 IS NULL
           OR e.hull && ST_MakeEnvelope(%(west)s, %(south)s, %(east)s, %(north)s, 4326))
    ORDER BY e.last_seen DESC, e.event_id
    LIMIT %(limit)s OFFSET %(offset)s
)
SELECT jsonb_build_object(
    'type', 'FeatureCollection',
    'numberReturned', count(*),
    'features', COALESCE(jsonb_agg(jsonb_build_object(
        'type', 'Feature',
        'id', s.event_id,
        'geometry', ST_AsGeoJSON(s.geom, 5)::jsonb,
        'properties', to_jsonb(s) - 'geom')), '[]'::jsonb)
)::text AS body
FROM selected s
"""


@router.get("/events", response_class=Response, responses={200: {"content": {GEOJSON: {}}}})
async def list_events(
    conn: ConnDep,
    settings: SettingsDep,
    bbox: Annotated[str | None, Query(description="west,south,east,north")] = None,
    start: Annotated[dt.datetime | None, Query(description="Events seen on or after")] = None,
    end: Annotated[dt.datetime | None, Query(description="Events first seen on or before")] = None,
    country: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    mapping_status: str | None = None,
    min_hotspots: Annotated[int, Query(ge=1)] = 1,
    include_retired: Annotated[bool, Query(description="Include merged/dissolved events")] = False,
    geometry: Literal["hull", "centroid"] = "hull",
    limit: Annotated[int, Query(ge=1)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    """Fire events as a GeoJSON FeatureCollection, most recently active first."""
    box = parse_bbox(bbox)
    params: dict[str, Any] = {
        "geometry": geometry,
        "statuses": [*LIVE, "merged", "dissolved"] if include_retired else list(LIVE),
        "min_hotspots": 0 if include_retired else min_hotspots,
        "start": ensure_utc(start),
        "end": ensure_utc(end),
        "country": country.upper() if country else None,
        "mapping_status": mapping_status,
        "west": box.west if box else None,
        "south": box.south if box else None,
        "east": box.east if box else None,
        "north": box.north if box else None,
        "limit": min(limit, settings.api.max_page_size),
        "offset": offset,
    }
    row = await (await conn.execute(_LIST, params)).fetchone()
    return Response(row["body"] if row else "{}", media_type=GEOJSON)


def tile_urls(settings: Settings, uri: str, kind: str) -> dict[str, str]:
    """TiTiler URL templates for the renderings that make sense for a raster kind."""
    # TiTiler reads local files by path and objects by their s3:// URI.
    source = uri[len("file://") :] if uri.startswith("file://") else uri
    base = (
        f"{settings.api.titiler_public_url.rstrip('/')}/cog/tiles/WebMercatorQuad/"
        f"{{z}}/{{x}}/{{y}}.png?url={quote(source, safe='')}"
    )
    if kind in ("pre", "post"):
        return {
            # Bands: 1 blue, 2 green, 3 red, 4 narrow NIR, 5 SWIR1, 6 SWIR2 (reflectance x 10000)
            "true_color": f"{base}&bidx=3&bidx=2&bidx=1&rescale=0,3000",
            "swir_false_color": f"{base}&bidx=6&bidx=4&bidx=3&rescale=0,4500",
        }
    if kind == "dnbr":
        return {"dnbr": f"{base}&bidx=1&rescale=-0.3,1.0&colormap_name=rdylgn_r"}
    if kind == "probability":
        return {"probability": f"{base}&bidx=1&rescale=0,1&colormap_name=inferno"}
    return {"mask": f"{base}&bidx=1&rescale=0,1&colormap_name=reds"}


_DETAIL = f"""
SELECT {_EVENT_PROPERTIES},
       e.merged_into, e.mapped_at, e.mapping_attempts, e.seed_hotspot_id,
       ST_AsGeoJSON(e.hull, 6)::jsonb AS hull,
       ST_AsGeoJSON(e.centroid, 6)::jsonb AS centroid,
       jsonb_build_array(ST_XMin(e.hull), ST_YMin(e.hull), ST_XMax(e.hull), ST_YMax(e.hull))
           AS bbox
FROM fire_event e
LEFT JOIN admin_unit a ON a.admin_id = e.admin1_id
WHERE e.event_id = %(id)s
"""

_PRODUCTS = """
SELECT b.ba_id, b.method, b.model_version, b.pre_item_ids, b.post_item_ids, b.pre_datetime,
       b.post_datetime, b.threshold, b.mean_score, b.clear_fraction,
       round(b.area_ha::numeric, 2)::float8 AS area_ha, b.n_polygons, b.qa_status, b.qa_at,
       b.qa_note, b.params, b.updated_at,
       COALESCE((SELECT jsonb_agg(jsonb_build_object(
                    'ref_source', v.ref_source, 'iou', v.iou, 'precision', v.precision_ref,
                    'recall', v.recall_ref, 'ref_ha', v.ref_ha,
                    'n_ref_perimeters', v.n_ref_perimeters) ORDER BY v.ref_source)
                 FROM burned_area_validation v WHERE v.ba_id = b.ba_id), '[]'::jsonb) AS validation
FROM burned_area b
WHERE b.event_id = %(id)s
ORDER BY b.method
"""


@router.get("/events/{event_id}")
async def get_event(event_id: uuid.UUID, conn: ConnDep, settings: SettingsDep) -> dict[str, Any]:
    """One event with its products, validation metrics and raster layers."""
    event = await (await conn.execute(_DETAIL, {"id": event_id})).fetchone()
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")
    products = await (await conn.execute(_PRODUCTS, {"id": event_id})).fetchall()
    assets = await (
        await conn.execute(
            "SELECT kind, method, uri, epsg, width, height, bands, created_at, "
            "ARRAY[ST_XMin(footprint), ST_YMin(footprint), ST_XMax(footprint), "
            "ST_YMax(footprint)]::float8[] AS bounds "
            "FROM raster_asset WHERE event_id = %(id)s ORDER BY kind, method NULLS FIRST",
            {"id": event_id},
        )
    ).fetchall()
    for asset in assets:
        asset["tiles"] = tile_urls(settings, asset["uri"], asset["kind"])
    return {**event, "burned_areas": products, "rasters": assets}


@router.get(
    "/events/{event_id}/hotspots",
    response_class=Response,
    responses={200: {"content": {GEOJSON: {}}}},
)
async def event_hotspots(event_id: uuid.UUID, conn: ConnDep) -> Response:
    """The detections of an event, in acquisition order."""
    row = await (
        await conn.execute(
            """
            SELECT jsonb_build_object(
                'type', 'FeatureCollection',
                'features', COALESCE(jsonb_agg(jsonb_build_object(
                    'type', 'Feature', 'id', h.hotspot_id,
                    'geometry', ST_AsGeoJSON(h.geom, 5)::jsonb,
                    'properties', jsonb_build_object(
                        'acq_ts', h.acq_ts, 'instrument', h.instrument,
                        'satellite', h.satellite, 'frp_mw', h.frp_mw,
                        'confidence_level', h.confidence_level, 'daynight', h.daynight))
                    ORDER BY h.acq_ts), '[]'::jsonb))::text AS body
            FROM hotspot h WHERE h.event_id = %(id)s
            """,
            {"id": event_id},
        )
    ).fetchone()
    return Response(row["body"] if row else "{}", media_type=GEOJSON)


@router.get(
    "/events/{event_id}/burned-areas",
    response_class=Response,
    responses={200: {"content": {GEOJSON: {}}}},
)
async def event_burned_areas(event_id: uuid.UUID, conn: ConnDep) -> Response:
    """Burned-area polygons of an event, one feature per method."""
    row = await (
        await conn.execute(
            """
            SELECT jsonb_build_object(
                'type', 'FeatureCollection',
                'features', COALESCE(jsonb_agg(jsonb_build_object(
                    'type', 'Feature', 'id', b.ba_id,
                    'geometry', ST_AsGeoJSON(b.geom, 6)::jsonb,
                    'properties', jsonb_build_object(
                        'method', b.method, 'model_version', b.model_version,
                        'area_ha', round(b.area_ha::numeric, 2)::float8, 'qa_status', b.qa_status,
                        'post_datetime', b.post_datetime)) ORDER BY b.method), '[]'::jsonb)
            )::text AS body
            FROM burned_area b WHERE b.event_id = %(id)s
            """,
            {"id": event_id},
        )
    ).fetchone()
    return Response(row["body"] if row else "{}", media_type=GEOJSON)


class QaVerdict(BaseModel):
    status: Literal["accepted", "rejected", "auto"]
    note: str | None = Field(default=None, max_length=1000)


@router.post("/burned-areas/{ba_id}/qa")
async def review_burned_area(
    ba_id: uuid.UUID,
    verdict: QaVerdict,
    conn: ConnDep,
    user: Annotated[auth.User, Depends(require_role("analyst", "admin"))],
) -> dict[str, Any]:
    """Record an analyst's verdict on a burned-area polygon (``auto`` clears it)."""
    row = await (
        await conn.execute(
            """
            UPDATE burned_area
            SET qa_status = %(status)s,
                qa_by = CASE WHEN %(status)s = 'auto' THEN NULL ELSE %(user)s::uuid END,
                qa_at = CASE WHEN %(status)s = 'auto' THEN NULL ELSE now() END,
                qa_note = %(note)s, updated_at = now()
            WHERE ba_id = %(id)s
            RETURNING ba_id, event_id, method, qa_status, qa_at, qa_note
            """,
            {"status": verdict.status, "user": user.user_id, "note": verdict.note, "id": ba_id},
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "burned area not found")
    return row


@router.post("/events/{event_id}/remap", status_code=status.HTTP_202_ACCEPTED)
async def requeue_event(
    event_id: uuid.UUID,
    conn: ConnDep,
    _user: Annotated[auth.User, Depends(require_role("admin"))],
) -> dict[str, Any]:
    """Put an event back in the mapping queue (picked up by the next mapping run)."""
    row = await (
        await conn.execute(
            """
            UPDATE fire_event
            SET mapping_status = 'pending', mapping_attempts = 0, mapping_checked_at = NULL,
                mapped_n_hotspots = NULL, mapping_note = 're-queued by an administrator',
                updated_at = now()
            WHERE event_id = %(id)s AND status IN ('active', 'cooling', 'closed')
            RETURNING event_id, mapping_status
            """,
            {"id": event_id},
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "live event not found")
    return row

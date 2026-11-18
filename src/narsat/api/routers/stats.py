"""Statistics and data-quality endpoints.

``/stats/summary`` and ``/quality/freshness`` read the operational tables and are
always current. The other ``/stats`` endpoints read the ``mart`` schema, which is
produced by dbt over the lakehouse and published by the refresh pipeline; they are
as fresh as the last successful run.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Query

from narsat.api.deps import ConnDep

router = APIRouter(tags=["statistics"])


@router.get("/stats/summary")
async def summary(conn: ConnDep) -> dict[str, Any]:
    """Headline numbers for the dashboard."""
    row = await (
        await conn.execute(
            """
            SELECT
                (SELECT count(*) FROM hotspot WHERE NOT is_static_source) AS hotspots,
                (SELECT count(*) FROM hotspot WHERE is_static_source) AS static_hotspots,
                (SELECT count(*) FROM fire_event
                  WHERE status IN ('active', 'cooling', 'closed')) AS events,
                (SELECT count(*) FROM fire_event WHERE status = 'active') AS active_events,
                (SELECT count(*) FROM fire_event WHERE mapping_status = 'mapped') AS mapped_events,
                (SELECT COALESCE(jsonb_object_agg(method, area), '{}'::jsonb)
                   FROM (SELECT method, round(sum(area_ha)::numeric, 1)::float8 AS area
                         FROM burned_area WHERE qa_status <> 'rejected' GROUP BY method) m)
                    AS burned_area_ha,
                (SELECT min(acq_ts) FROM hotspot) AS first_acquisition,
                (SELECT max(acq_ts) FROM hotspot) AS last_acquisition
            """
        )
    ).fetchone()
    return dict(row or {})


@router.get("/stats/activity")
async def activity(
    conn: ConnDep,
    start: Annotated[dt.date | None, Query()] = None,
    end: Annotated[dt.date | None, Query()] = None,
    country: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
) -> list[dict[str, Any]]:
    """Daily detections and events (mart ``fire_activity_daily``), summed over admin units."""
    cursor = await conn.execute(
        """
        SELECT activity_date, country_iso3, sum(n_hotspots)::bigint AS n_hotspots,
               sum(n_events)::bigint AS n_events,
               round(sum(frp_sum_mw)::numeric, 1)::float8 AS frp_sum_mw
        FROM mart.fire_activity_daily
        WHERE (%(start)s::date IS NULL OR activity_date >= %(start)s)
          AND (%(end)s::date IS NULL OR activity_date <= %(end)s)
          AND (%(country)s::text IS NULL OR country_iso3 = %(country)s)
        GROUP BY activity_date, country_iso3
        ORDER BY activity_date, country_iso3
        """,
        {"start": start, "end": end, "country": country.upper() if country else None},
    )
    return await cursor.fetchall()


@router.get("/stats/burned-area")
async def burned_area_by_admin(
    conn: ConnDep,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
    country: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    method: Annotated[str | None, Query()] = None,
) -> list[dict[str, Any]]:
    """Burned area per year, country, first-level unit and method (mart)."""
    cursor = await conn.execute(
        """
        SELECT fire_year, country_iso3, admin1_name, method, n_events,
               round(area_ha::numeric, 1)::float8 AS area_ha
        FROM mart.burned_area_admin_year
        WHERE (%(year)s::int IS NULL OR fire_year = %(year)s)
          AND (%(country)s::text IS NULL OR country_iso3 = %(country)s)
          AND (%(method)s::text IS NULL OR method = %(method)s)
        ORDER BY fire_year DESC, area_ha DESC
        """,
        {"year": year, "country": country.upper() if country else None, "method": method},
    )
    return await cursor.fetchall()


@router.get("/stats/events")
async def event_summaries(
    conn: ConnDep, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> list[dict[str, Any]]:
    """Largest mapped events with both area estimates and their agreement (mart).

    Ranked by the dNBR area: against the NEALGEBA reference, dNBR is the more accurate of
    the two methods (docs/results.md, section 5), and the model's larger areas would put
    its false positives at the top.
    """
    cursor = await conn.execute(
        """
        SELECT event_id, country_iso3, admin1_name, first_seen, last_seen,
               round(duration_hours::numeric, 1)::float8 AS duration_hours, n_hotspots, frp_max_mw,
               round(area_dnbr_ha::numeric, 1)::float8 AS area_dnbr_ha,
               round(area_model_ha::numeric, 1)::float8 AS area_model_ha,
               round(method_iou::numeric, 3)::float8 AS method_iou
        FROM mart.event_summary
        ORDER BY COALESCE(area_dnbr_ha, 0) DESC, COALESCE(area_model_ha, 0) DESC,
                 n_hotspots DESC
        LIMIT %(limit)s
        """,
        {"limit": limit},
    )
    return await cursor.fetchall()


@router.get("/quality/checks", tags=["data quality"])
async def latest_checks(conn: ConnDep) -> list[dict[str, Any]]:
    """Result of every check in the most recent run of each quality suite."""
    cursor = await conn.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (suite) suite, run_id
            FROM dq_result ORDER BY suite, checked_at DESC
        )
        SELECT d.suite, d.check_name, d.status, d.severity, d.observed, d.detail, d.checked_at
        FROM dq_result d JOIN latest l ON l.suite = d.suite AND l.run_id = d.run_id
        ORDER BY d.suite, d.status DESC, d.check_name
        """
    )
    return await cursor.fetchall()


@router.get("/quality/freshness", tags=["data quality"])
async def freshness(conn: ConnDep) -> dict[str, Any]:
    """How current the operational data is, and how long the mapping queue is."""
    row = await (
        await conn.execute(
            """
            SELECT
                (SELECT max(acq_ts) FROM hotspot) AS last_acquisition,
                (SELECT max(ingested_at) FROM hotspot) AS last_ingestion,
                (SELECT count(*) FROM hotspot
                  WHERE ingested_at >= now() - interval '24 hours') AS ingested_last_24h,
                (SELECT count(*) FROM fire_event
                  WHERE mapping_status IN ('pending', 'imagery_pending')
                    AND status IN ('active', 'cooling', 'closed')) AS mapping_queue,
                (SELECT max(mapped_at) FROM fire_event) AS last_mapping,
                (SELECT max(checked_at) FROM dq_result) AS last_quality_run
            """
        )
    ).fetchone()
    return dict(row or {})


@router.get("/admin-units", tags=["reference"])
async def admin_units(
    conn: ConnDep,
    country: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    level: Annotated[int, Query(ge=0, le=3)] = 1,
) -> list[dict[str, Any]]:
    """Administrative units that have been loaded (names and codes, no geometry)."""
    cursor = await conn.execute(
        """
        SELECT admin_id, country_iso3::text AS country_iso3, level, code, name, source, licence
        FROM admin_unit
        WHERE level = %(level)s AND (%(country)s::text IS NULL OR country_iso3 = %(country)s)
        ORDER BY country_iso3, name
        """,
        {"level": level, "country": country.upper() if country else None},
    )
    return await cursor.fetchall()

"""Platform-health gauges exported by the API's ``/metrics`` endpoint.

The batch pipelines (mapping, lake refresh, quality gates) run as short-lived commands
under Airflow, so their own Prometheus counters disappear when the process exits. What
an operator needs to alert on is the state those jobs leave behind, and that state lives
in the database: how fresh the detections are, how many events wait to be mapped, how
many quality checks failed in the latest run. The API is always running, so it reads
these figures periodically and publishes them as gauges.

The refresh runs in the background on the API's async connection pool, never inside a
scrape: prometheus_client collects synchronously, and a slow query there would stall
the event loop.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from prometheus_client import Gauge
from psycopg_pool import AsyncConnectionPool

from narsat.log import get_logger

log = get_logger(__name__)

LAST_ACQUISITION = Gauge(
    "narsat_data_last_acquisition_timestamp_seconds",
    "Unix time of the most recent satellite fire detection stored",
)
LAST_INGESTION = Gauge(
    "narsat_data_last_ingestion_timestamp_seconds",
    "Unix time at which the most recent detection was stored",
)
MAPPING_QUEUE = Gauge("narsat_mapping_queue_events", "Fire events waiting for burned-area mapping")
MAPPING_FAILED = Gauge(
    "narsat_mapping_failed_events", "Fire events whose mapping failed after every attempt"
)
DQ_FAILED = Gauge(
    "narsat_dq_failed_checks",
    "Checks that did not pass in the latest run of each quality suite",
    ["severity"],
)
LAST_QUALITY_RUN = Gauge(
    "narsat_dq_last_run_timestamp_seconds", "Unix time of the latest data-quality run"
)
REFRESHED = Gauge(
    "narsat_platform_metrics_refresh_timestamp_seconds",
    "Unix time at which the platform gauges were last read from the database",
)

QUERY = """
WITH latest AS (
    SELECT DISTINCT ON (suite) suite, run_id
    FROM dq_result ORDER BY suite, checked_at DESC
)
SELECT
    extract(epoch FROM (SELECT max(acq_ts) FROM hotspot))::float8 AS last_acquisition,
    extract(epoch FROM (SELECT max(ingested_at) FROM hotspot))::float8 AS last_ingestion,
    (SELECT count(*) FROM fire_event
      WHERE mapping_status IN ('pending', 'imagery_pending')
        AND status IN ('active', 'cooling', 'closed')) AS mapping_queue,
    (SELECT count(*) FROM fire_event
      WHERE mapping_status = 'failed' AND status IN ('active', 'cooling', 'closed'))
        AS mapping_failed,
    (SELECT count(*) FROM dq_result d JOIN latest l USING (suite, run_id)
      WHERE d.status <> 'pass' AND d.severity = 'error') AS dq_failed_error,
    (SELECT count(*) FROM dq_result d JOIN latest l USING (suite, run_id)
      WHERE d.status <> 'pass' AND d.severity = 'warn') AS dq_failed_warn,
    extract(epoch FROM (SELECT max(checked_at) FROM dq_result))::float8 AS last_quality_run
"""


def _set_timestamp(gauge: Gauge, value: float | None) -> None:
    # An empty table has no "latest" time: 0 reads as "infinitely old", which is what an
    # alert on staleness should conclude for a platform that never received data.
    gauge.set(value if value is not None else 0.0)


def apply(row: dict[str, Any], now: float) -> None:
    """Publish one result of ``QUERY``."""
    _set_timestamp(LAST_ACQUISITION, row["last_acquisition"])
    _set_timestamp(LAST_INGESTION, row["last_ingestion"])
    _set_timestamp(LAST_QUALITY_RUN, row["last_quality_run"])
    MAPPING_QUEUE.set(row["mapping_queue"])
    MAPPING_FAILED.set(row["mapping_failed"])
    DQ_FAILED.labels("error").set(row["dq_failed_error"])
    DQ_FAILED.labels("warn").set(row["dq_failed_warn"])
    REFRESHED.set(now)


async def refresh_once(pool: AsyncConnectionPool[Any]) -> None:
    async with pool.connection() as conn:
        row = await (await conn.execute(QUERY)).fetchone()
    if row is not None:
        apply(row, time.time())


async def refresh_forever(pool: AsyncConnectionPool[Any], interval_s: float) -> None:
    """Keep the gauges current until cancelled. A failed read is logged, not raised:
    the refresh timestamp then stops moving, which is itself alertable."""
    while True:
        try:
            await refresh_once(pool)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # monitoring must never take the API down
            log.warning("platform_metrics_failed", error=type(exc).__name__)
        await asyncio.sleep(interval_s)

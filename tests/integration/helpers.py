"""Helpers shared by the integration tests."""

from __future__ import annotations

import datetime as dt
import uuid

from narsat.config import ClusteringSettings, DbSettings
from narsat.db import Connection, connect
from narsat.db.hotspots import insert_hotspots
from narsat.events.updater import EventUpdater, UpdateResult
from narsat.schemas import HotspotMessage
from tests import synthetic
from tests.conftest import make_hotspot

UTC = dt.UTC
FIRE_START = dt.datetime(2023, 7, 22, 12, 0, tzinfo=UTC)
AFTER_FIRE = dt.datetime(2023, 9, 1, tzinfo=UTC)
COUNTRIES = ["DZA", "MAR", "TUN"]


def fire_over_synthetic_burn(n: int = 8) -> list[HotspotMessage]:
    """Detections scattered inside the burned disc of the synthetic scene, over two days."""
    lon0, lat0 = synthetic.truth_20m().centre_lonlat
    offsets = [(0, 0), (3, 2), (-3, 2), (2, -3), (-2, -3), (5, 0), (0, 5), (-5, -1)][:n]
    return [
        make_hotspot(
            round(lat0 + dy * 0.001, 5),
            round(lon0 + dx * 0.001, 5),
            FIRE_START + dt.timedelta(hours=6 * i),
            confidence="h" if i % 2 == 0 else "n",
            frp=20.0 + i,
        )
        for i, (dx, dy) in enumerate(offsets)
    ]


def updater(**overrides: float) -> EventUpdater:
    return EventUpdater(ClusteringSettings(**overrides), COUNTRIES)  # type: ignore[arg-type]


def store_and_cluster(
    conn: Connection,
    hotspots: list[HotspotMessage],
    as_of: dt.datetime = AFTER_FIRE,
    **overrides: float,
) -> UpdateResult:
    insert_hotspots(conn, hotspots)
    stamps = [h.acq_ts for h in hotspots]
    return updater(**overrides).update(conn, as_of=as_of, touched=(min(stamps), max(stamps)))


def single_event(db: DbSettings, hotspots: list[HotspotMessage] | None = None) -> uuid.UUID:
    """Store one fire and return its event identifier (closed and queued for mapping)."""
    with connect(db) as conn:
        result = store_and_cluster(conn, hotspots or fire_over_synthetic_burn())
    assert len(result.events_created) == 1
    return result.events_created[0]


def square(lon: float, lat: float, half_deg: float) -> str:
    """WKT of a square MultiPolygon centred on a point."""
    w, e, s, n = lon - half_deg, lon + half_deg, lat - half_deg, lat + half_deg
    return f"MULTIPOLYGON((({w} {s}, {e} {s}, {e} {n}, {w} {n}, {w} {s})))"

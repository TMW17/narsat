"""Maintain the ``fire_event`` table from the hotspots in PostGIS.

One call to ``EventUpdater.update`` performs, inside the caller's transaction:

1. refresh of the static-source grid and flagging of hotspots that fall in it;
2. loading of the hotspots that the new detections can affect (a time window around
   them, plus every member of every event touching that window);
3. spatio-temporal clustering and reconciliation into stable event identifiers;
4. recomputation of the aggregates and hull of each event, written as upserts;
5. status transitions, including hand-over to burned-area mapping.

The work is a deterministic function of the stored hotspots, so running it twice
changes nothing (rows are only rewritten when a value actually differs).

Exactness: with ``min_samples = 1`` (the default) the windowed update is identical to
re-clustering the whole table, because a cluster is a connected component and every
component touching the window is loaded in full. With larger values, detections at
the edge of the window can lose neighbours that were not loaded; ``full=True``
(``narsat events recluster --full``) removes that approximation.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import dataclass, field

import pandas as pd
import shapely

from narsat import metrics
from narsat.config import ClusteringSettings
from narsat.db import Connection
from narsat.events.clustering import st_dbscan
from narsat.events.reconcile import reconcile
from narsat.events.static_sources import persistent_cells
from narsat.geo import geodesic_area_ha, hull_of_points
from narsat.log import get_logger

log = get_logger(__name__)

_HOTSPOT_COLUMNS = (
    "hotspot_id", "latitude", "longitude", "acq_ts", "frp_mw", "confidence_level",
    "instrument", "satellite", "event_id",
)  # fmt: skip
_EVENT_COLUMNS = (
    "event_id", "seed_hotspot_id", "status", "first_seen", "last_seen", "n_hotspots",
    "n_high_confidence", "frp_max_mw", "frp_sum_mw", "sensors", "centroid_lon",
    "centroid_lat", "hull_wkb", "hull_area_ha",
)  # fmt: skip


@dataclass
class UpdateResult:
    hotspots_considered: int = 0
    static_cells_added: int = 0
    hotspots_flagged_static: int = 0
    events_upserted: list[uuid.UUID] = field(default_factory=list)
    events_created: list[uuid.UUID] = field(default_factory=list)
    events_merged: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    events_dissolved: list[uuid.UUID] = field(default_factory=list)
    queued_for_mapping: int = 0

    @property
    def changed(self) -> list[uuid.UUID]:
        """Every event whose stored state changed in this cycle."""
        touched = set(self.events_upserted) | set(self.events_merged) | set(self.events_dissolved)
        return sorted(touched, key=str)


_UPSERT_EVENTS = """
INSERT INTO fire_event AS e (
    event_id, seed_hotspot_id, status, first_seen, last_seen, n_hotspots, n_high_confidence,
    frp_max_mw, frp_sum_mw, sensors, centroid, hull, hull_area_ha, merged_into
)
SELECT u.event_id, u.seed_hotspot_id, u.status, u.first_seen, u.last_seen, u.n_hotspots,
       u.n_high_confidence, u.frp_max_mw, u.frp_sum_mw,
       string_to_array(u.sensors, ','),
       ST_SetSRID(ST_MakePoint(u.centroid_lon, u.centroid_lat), 4326),
       ST_GeomFromWKB(u.hull_wkb, 4326), u.hull_area_ha, NULL
FROM unnest(
    %(event_id)s::uuid[], %(seed_hotspot_id)s::uuid[], %(status)s::text[],
    %(first_seen)s::timestamptz[], %(last_seen)s::timestamptz[], %(n_hotspots)s::int[],
    %(n_high_confidence)s::int[], %(frp_max_mw)s::float8[], %(frp_sum_mw)s::float8[],
    %(sensors)s::text[], %(centroid_lon)s::float8[], %(centroid_lat)s::float8[],
    %(hull_wkb)s::bytea[], %(hull_area_ha)s::float8[]
) AS u (
    event_id, seed_hotspot_id, status, first_seen, last_seen, n_hotspots, n_high_confidence,
    frp_max_mw, frp_sum_mw, sensors, centroid_lon, centroid_lat, hull_wkb, hull_area_ha
)
ON CONFLICT (event_id) DO UPDATE
SET seed_hotspot_id = EXCLUDED.seed_hotspot_id,
    status = EXCLUDED.status,
    first_seen = EXCLUDED.first_seen,
    last_seen = EXCLUDED.last_seen,
    n_hotspots = EXCLUDED.n_hotspots,
    n_high_confidence = EXCLUDED.n_high_confidence,
    frp_max_mw = EXCLUDED.frp_max_mw,
    frp_sum_mw = EXCLUDED.frp_sum_mw,
    sensors = EXCLUDED.sensors,
    centroid = EXCLUDED.centroid,
    hull = EXCLUDED.hull,
    hull_area_ha = EXCLUDED.hull_area_ha,
    merged_into = NULL,
    updated_at = now()
WHERE (e.status, e.first_seen, e.last_seen, e.n_hotspots, e.n_high_confidence,
       e.frp_max_mw, e.frp_sum_mw, e.merged_into)
      IS DISTINCT FROM
      (EXCLUDED.status, EXCLUDED.first_seen, EXCLUDED.last_seen, EXCLUDED.n_hotspots,
       EXCLUDED.n_high_confidence, EXCLUDED.frp_max_mw, EXCLUDED.frp_sum_mw, NULL::uuid)
RETURNING e.event_id, (e.xmax = 0) AS inserted
"""

_ASSIGN_HOTSPOTS = """
UPDATE hotspot h
SET event_id = u.event_id
FROM unnest(%(hotspot_id)s::uuid[], %(event_id)s::uuid[]) AS u (hotspot_id, event_id)
WHERE h.hotspot_id = u.hotspot_id
  AND h.event_id IS DISTINCT FROM u.event_id
"""

#: An event belongs to the unit that contains its centroid or, for a fire on the coast
#: whose centroid falls just offshore of the boundary polygons, to the nearest unit within
#: this distance (degrees; about 5 km at these latitudes).
COAST_TOLERANCE_DEG = 0.05

_ATTRIBUTE_EVENTS = """
WITH nearest AS (
    SELECT DISTINCT ON (e.event_id) e.event_id, a.admin_id, a.country_iso3
    FROM fire_event e
    JOIN admin_unit a
      ON a.level = %(level)s AND ST_DWithin(a.geom, e.centroid, %(tolerance)s)
    WHERE e.event_id = ANY(%(ids)s)
    ORDER BY e.event_id, ST_Distance(a.geom, e.centroid), a.admin_id
)
UPDATE fire_event e
SET country_iso3 = n.country_iso3,
    admin1_id = CASE WHEN %(level)s = 1 THEN n.admin_id ELSE e.admin1_id END
FROM nearest n
WHERE e.event_id = n.event_id
  AND (e.country_iso3 IS DISTINCT FROM n.country_iso3
       OR (%(level)s = 1 AND e.admin1_id IS DISTINCT FROM n.admin_id))
"""

#: Live events still missing a country or a first-level unit, for instance because they
#: were created before the boundaries were loaded. They are retried on every update.
_UNATTRIBUTED_EVENTS = """
SELECT event_id FROM fire_event
WHERE status IN ('active', 'cooling', 'closed')
  AND (country_iso3 IS NULL OR admin1_id IS NULL)
"""

_LIVE_STATUS = """CASE
    WHEN %(as_of)s - last_seen <= %(active)s THEN 'active'
    WHEN %(as_of)s - last_seen <= %(settle)s THEN 'cooling'
    ELSE 'closed' END"""


class EventUpdater:
    def __init__(self, settings: ClusteringSettings, aoi_countries: list[str]) -> None:
        self._s = settings
        self._countries = [c.upper() for c in aoi_countries]

    # ------------------------------------------------------------------ static sources
    def _refresh_static(
        self, conn: Connection, anchor: dt.datetime, result: UpdateResult
    ) -> set[uuid.UUID]:
        """Update the static grid; returns the events that just lost detections to it."""
        since = anchor - dt.timedelta(days=self._s.static_lookback_days)
        rows = conn.execute(
            "SELECT latitude, longitude, (acq_ts AT TIME ZONE 'UTC')::date AS day "
            "FROM hotspot WHERE acq_ts >= %(since)s AND acq_ts <= %(anchor)s",
            {"since": since, "anchor": anchor},
        ).fetchall()
        if rows:
            frame = pd.DataFrame(rows)
            cells = persistent_cells(
                frame["latitude"], frame["longitude"], frame["day"],
                self._s.static_cell_deg, self._s.static_min_days,
            )  # fmt: skip
            if not cells.empty:
                cursor = conn.execute(
                    """
                    INSERT INTO static_cell (cell_y, cell_x, cell_deg, n_days)
                    SELECT u.cell_y, u.cell_x, %(cell_deg)s, u.n_days
                    FROM unnest(%(cell_y)s::int[], %(cell_x)s::int[], %(n_days)s::int[])
                         AS u (cell_y, cell_x, n_days)
                    ON CONFLICT (cell_y, cell_x) DO UPDATE
                    SET n_days = EXCLUDED.n_days, updated_at = now()
                    WHERE static_cell.n_days < EXCLUDED.n_days
                    RETURNING (xmax = 0) AS inserted
                    """,
                    {
                        "cell_deg": self._s.static_cell_deg,
                        "cell_y": cells["cell_y"].tolist(),
                        "cell_x": cells["cell_x"].tolist(),
                        "n_days": cells["n_days"].tolist(),
                    },
                )
                result.static_cells_added = sum(1 for r in cursor.fetchall() if r["inserted"])

        flagged = conn.execute(
            """
            WITH hit AS (
                SELECT h.hotspot_id, h.event_id
                FROM hotspot h
                JOIN static_cell s
                  ON floor(h.latitude / s.cell_deg)::int = s.cell_y
                 AND floor(h.longitude / s.cell_deg)::int = s.cell_x
                WHERE NOT h.is_static_source
            ), upd AS (
                UPDATE hotspot h
                SET is_static_source = true, event_id = NULL
                FROM hit WHERE h.hotspot_id = hit.hotspot_id
            )
            SELECT event_id FROM hit
            """
        ).fetchall()
        result.hotspots_flagged_static = len(flagged)
        return {row["event_id"] for row in flagged if row["event_id"] is not None}

    # ------------------------------------------------------------------ loading
    def _load(
        self,
        conn: Connection,
        window: tuple[dt.datetime, dt.datetime] | None,
        extra_events: set[uuid.UUID],
        everything: bool,
    ) -> pd.DataFrame:
        columns = ", ".join(_HOTSPOT_COLUMNS)
        if everything:
            rows = conn.execute(
                f"SELECT {columns} FROM hotspot WHERE NOT is_static_source"
            ).fetchall()
        else:
            pad = dt.timedelta(days=self._s.window_days)
            start = window[0] - pad if window else None
            end = window[1] + pad if window else None
            rows = conn.execute(
                f"""
                WITH in_window AS (
                    SELECT hotspot_id, event_id FROM hotspot
                    WHERE NOT is_static_source
                      AND %(start)s::timestamptz IS NOT NULL
                      AND acq_ts BETWEEN %(start)s AND %(end)s
                )
                SELECT {columns} FROM hotspot
                WHERE NOT is_static_source
                  AND (hotspot_id IN (SELECT hotspot_id FROM in_window)
                       OR event_id IN (SELECT event_id FROM in_window WHERE event_id IS NOT NULL)
                       OR event_id = ANY(%(extra)s::uuid[]))
                """,
                {"start": start, "end": end, "extra": list(extra_events)},
            ).fetchall()
        frame = pd.DataFrame(rows, columns=list(_HOTSPOT_COLUMNS))
        if not frame.empty:
            frame["acq_ts"] = pd.to_datetime(frame["acq_ts"], utc=True)
            frame = frame.sort_values(["acq_ts", "hotspot_id"]).reset_index(drop=True)
        return frame

    # ------------------------------------------------------------------ aggregation
    def _status(self, last_seen: dt.datetime, as_of: dt.datetime) -> str:
        quiet_hours = (as_of - last_seen).total_seconds() / 3600.0
        if quiet_hours <= self._s.active_hours:
            return "active"
        return "cooling" if quiet_hours <= self._s.settle_days * 24.0 else "closed"

    def _aggregate(
        self, frame: pd.DataFrame, seeds: dict[uuid.UUID, uuid.UUID], as_of: dt.datetime
    ) -> dict[str, list[object]]:
        out: dict[str, list[object]] = {key: [] for key in _EVENT_COLUMNS}
        members = frame.loc[frame["new_event_id"].notna()]
        for key, group in members.groupby("new_event_id", sort=True):
            event = uuid.UUID(str(key))
            first_seen = group["acq_ts"].min().to_pydatetime()
            last_seen = group["acq_ts"].max().to_pydatetime()
            frp = group["frp_mw"].astype("float64")
            hull = hull_of_points(
                group["longitude"].to_numpy(), group["latitude"].to_numpy(),
                self._s.footprint_buffer_m,
            )  # fmt: skip
            sensors = sorted(
                {f"{i}/{s}" for i, s in zip(group["instrument"], group["satellite"], strict=True)}
            )
            values: dict[str, object] = {
                "event_id": event,
                "seed_hotspot_id": seeds[event],
                "status": self._status(last_seen, as_of),
                "first_seen": first_seen,
                "last_seen": last_seen,
                "n_hotspots": len(group),
                "n_high_confidence": int((group["confidence_level"] == "high").sum()),
                "frp_max_mw": None if frp.isna().all() else float(frp.max()),
                "frp_sum_mw": None if frp.isna().all() else float(frp.sum()),
                "sensors": ",".join(sensors),
                "centroid_lon": float(hull.centroid.x),
                "centroid_lat": float(hull.centroid.y),
                "hull_wkb": shapely.to_wkb(hull),
                "hull_area_ha": geodesic_area_ha(hull),
            }
            for key in _EVENT_COLUMNS:
                out[key].append(values[key])
        return out

    # ------------------------------------------------------------------ status rules
    def _transition(self, conn: Connection, as_of: dt.datetime, result: UpdateResult) -> None:
        params = {
            "as_of": as_of,
            "active": dt.timedelta(hours=self._s.active_hours),
            "settle": dt.timedelta(days=self._s.settle_days),
            "min_hotspots": self._s.min_hotspots_to_map,
            "countries": self._countries,
        }
        # Time passes even when no detection arrives: age the live events.
        conn.execute(
            f"""
            UPDATE fire_event
            SET status = {_LIVE_STATUS}, updated_at = now()
            WHERE status IN ('active', 'cooling', 'closed') AND status <> {_LIVE_STATUS}
            """,
            params,
        )
        # An event that changed after it was mapped (or skipped) is looked at again.
        conn.execute(
            """
            UPDATE fire_event
            SET mapping_status = 'not_ready', mapping_note = 'event changed after mapping',
                mapping_attempts = 0, mapped_n_hotspots = NULL, updated_at = now()
            WHERE status IN ('active', 'cooling', 'closed')
              AND mapped_n_hotspots IS NOT NULL
              AND mapped_n_hotspots <> n_hotspots
            """
        )
        conn.execute(
            """
            UPDATE fire_event
            SET mapping_status = 'skipped', mapped_n_hotspots = n_hotspots, updated_at = now(),
                mapping_note = CASE
                    WHEN n_hotspots < %(min_hotspots)s THEN 'fewer detections than the minimum'
                    ELSE 'outside the configured countries' END
            WHERE status = 'closed' AND mapping_status = 'not_ready'
              AND (n_hotspots < %(min_hotspots)s
                   OR (cardinality(%(countries)s::text[]) > 0
                       AND (
                           -- in another country
                           (country_iso3 IS NOT NULL
                            AND NOT (country_iso3::text = ANY(%(countries)s::text[])))
                           -- or in no loaded country, although the boundaries of the
                           -- configured countries are loaded (e.g. a fire in Spain)
                           OR (country_iso3 IS NULL
                               AND EXISTS (SELECT 1 FROM admin_unit
                                           WHERE level = 0
                                             AND country_iso3::text
                                                 = ANY(%(countries)s::text[]))))))
            """,
            params,
        )
        cursor = conn.execute(
            """
            UPDATE fire_event
            SET mapping_status = 'pending', mapping_note = NULL, updated_at = now()
            WHERE status = 'closed' AND mapping_status = 'not_ready'
            """
        )
        result.queued_for_mapping = cursor.rowcount

    def _attribute(self, conn: Connection, changed: list[uuid.UUID]) -> None:
        """Country and first-level unit of the changed events and of every live event
        still without them (boundaries loaded after the event was created)."""
        unattributed = [row["event_id"] for row in conn.execute(_UNATTRIBUTED_EVENTS).fetchall()]
        ids = list(dict.fromkeys([*changed, *unattributed]))
        if not ids:
            return
        for level in (0, 1):
            conn.execute(
                _ATTRIBUTE_EVENTS, {"ids": ids, "level": level, "tolerance": COAST_TOLERANCE_DEG}
            )

    # ------------------------------------------------------------------ entry point
    def update(
        self,
        conn: Connection,
        as_of: dt.datetime | None = None,
        touched: tuple[dt.datetime, dt.datetime] | None = None,
        full: bool = False,
    ) -> UpdateResult:
        """Re-cluster and persist.

        ``touched`` is the acquisition-time range of the detections added since the
        last call; ``full=True`` ignores it and recomputes every event from scratch.
        With neither, only the time-driven status transitions are applied.
        """
        started = time.perf_counter()
        as_of = as_of or dt.datetime.now(dt.UTC)
        result = UpdateResult()

        newest_row = conn.execute("SELECT max(acq_ts) AS newest FROM hotspot").fetchone()
        newest = newest_row["newest"] if newest_row else None
        if newest is None:
            return result
        lost_members = self._refresh_static(conn, touched[1] if touched else newest, result)

        if not (full or touched or lost_members):
            self._attribute(conn, [])
            self._transition(conn, as_of, result)
            return result

        frame = self._load(conn, touched, lost_members, everything=full)
        result.hotspots_considered = len(frame)
        previous = [e if isinstance(e, uuid.UUID) else None for e in frame["event_id"]]
        previous_events = {e for e in previous if e is not None} | lost_members
        if full:
            live = conn.execute(
                "SELECT event_id FROM fire_event WHERE status IN ('active', 'cooling', 'closed')"
            ).fetchall()
            previous_events |= {row["event_id"] for row in live}
        seed_rows = conn.execute(
            "SELECT event_id, seed_hotspot_id FROM fire_event WHERE event_id = ANY(%(ids)s)",
            {"ids": list(previous_events)},
        ).fetchall()
        previous_seeds = {row["event_id"]: row["seed_hotspot_id"] for row in seed_rows}

        if frame.empty:
            outcome = reconcile([], [], [], [], previous_seeds)
        else:
            hours = (frame["acq_ts"] - frame["acq_ts"].min()).dt.total_seconds().to_numpy() / 3600
            labels = st_dbscan(
                frame["latitude"].to_numpy(), frame["longitude"].to_numpy(), hours,
                self._s.eps_km, self._s.eps_hours, self._s.min_samples,
            )  # fmt: skip
            outcome = reconcile(
                frame["hotspot_id"].tolist(),
                labels.tolist(),
                [ts.to_pydatetime() for ts in frame["acq_ts"]],
                previous,
                previous_seeds,
            )
            frame["new_event_id"] = pd.Series(outcome.event_ids, dtype="object")
            payload = self._aggregate(frame, outcome.seeds, as_of)
            if payload["event_id"]:
                rows = conn.execute(_UPSERT_EVENTS, payload).fetchall()
                result.events_upserted = [row["event_id"] for row in rows]
                result.events_created = [row["event_id"] for row in rows if row["inserted"]]
            conn.execute(
                _ASSIGN_HOTSPOTS,
                {"hotspot_id": frame["hotspot_id"].tolist(), "event_id": outcome.event_ids},
            )

        if outcome.merged:
            conn.execute(
                """
                UPDATE fire_event e
                SET status = 'merged', merged_into = u.target, n_hotspots = 0,
                    mapping_status = 'skipped', mapping_note = 'merged into another event',
                    mapped_n_hotspots = NULL, updated_at = now()
                FROM unnest(%(source)s::uuid[], %(target)s::uuid[]) AS u (source, target)
                WHERE e.event_id = u.source
                """,
                {"source": list(outcome.merged), "target": list(outcome.merged.values())},
            )
            result.events_merged = dict(outcome.merged)
        if outcome.dissolved:
            conn.execute(
                """
                UPDATE fire_event
                SET status = 'dissolved', n_hotspots = 0, mapping_status = 'skipped',
                    mapping_note = 'no detections left (reclassified as static sources)',
                    mapped_n_hotspots = NULL, updated_at = now()
                WHERE event_id = ANY(%(ids)s) AND status <> 'dissolved'
                """,
                {"ids": list(outcome.dissolved)},
            )
            result.events_dissolved = sorted(outcome.dissolved, key=str)
        retired = [*outcome.merged, *outcome.dissolved]
        if retired:
            # Products of a retired event would be double-counted next to its successor.
            conn.execute("DELETE FROM burned_area WHERE event_id = ANY(%(ids)s)", {"ids": retired})
            conn.execute("DELETE FROM raster_asset WHERE event_id = ANY(%(ids)s)", {"ids": retired})

        self._attribute(conn, result.events_upserted)
        self._transition(conn, as_of, result)

        metrics.EVENTS_CHANGED.labels("created").inc(len(result.events_created))
        metrics.EVENTS_CHANGED.labels("updated").inc(
            len(result.events_upserted) - len(result.events_created)
        )
        metrics.EVENTS_CHANGED.labels("merged").inc(len(result.events_merged))
        metrics.CLUSTERING_SECONDS.observe(time.perf_counter() - started)
        log.info(
            "events_updated",
            hotspots=result.hotspots_considered,
            upserted=len(result.events_upserted),
            created=len(result.events_created),
            merged=len(result.events_merged),
            dissolved=len(result.events_dissolved),
            flagged_static=result.hotspots_flagged_static,
            queued_for_mapping=result.queued_for_mapping,
        )
        return result

"""Change feed of fire events on the compacted ``narsat.fire-events`` topic.

Every time the event table changes, the new state of each affected event is published
keyed by its identifier. Because the topic is compacted, a consumer that starts from
the beginning receives the latest state of every event: downstream systems (alerting,
another team's pipeline) can follow fire events without access to the database.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Sequence
from typing import Protocol

from narsat.db import Connection
from narsat.events.updater import UpdateResult
from narsat.schemas import FireEventMessage


class EventProducer(Protocol):
    def produce(self, topic: str, value: bytes | None = ..., key: bytes | None = ...) -> None: ...

    def flush(self, timeout: float = ...) -> int: ...


_SELECT = """
SELECT event_id, status, mapping_status, first_seen, last_seen, n_hotspots, frp_max_mw,
       ST_X(centroid) AS centroid_lon, ST_Y(centroid) AS centroid_lat,
       ST_XMin(hull) AS xmin, ST_YMin(hull) AS ymin, ST_XMax(hull) AS xmax, ST_YMax(hull) AS ymax,
       country_iso3::text AS country_iso3, merged_into
FROM fire_event
WHERE event_id = ANY(%(ids)s)
"""


def build_messages(
    conn: Connection,
    result: UpdateResult,
    now: dt.datetime,
) -> list[FireEventMessage]:
    changed: Sequence[uuid.UUID] = result.changed
    if not changed:
        return []
    created = set(result.events_created)
    merged = set(result.events_merged)
    dissolved = set(result.events_dissolved)
    messages: list[FireEventMessage] = []
    for row in conn.execute(_SELECT, {"ids": list(changed)}).fetchall():
        event = row["event_id"]
        if event in merged:
            change = "merged"
        elif event in dissolved:
            change = "dissolved"
        else:
            change = "created" if event in created else "updated"
        messages.append(
            FireEventMessage(
                event_id=event,
                change=change,
                status=row["status"],
                mapping_status=row["mapping_status"],
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
                n_hotspots=row["n_hotspots"],
                frp_max_mw=row["frp_max_mw"],
                centroid_lon=row["centroid_lon"],
                centroid_lat=row["centroid_lat"],
                bbox=(row["xmin"], row["ymin"], row["xmax"], row["ymax"]),
                country_iso3=row["country_iso3"],
                merged_into=row["merged_into"],
                emitted_at=now,
            )
        )
    return messages


def publish(
    producer: EventProducer,
    topic: str,
    messages: Sequence[FireEventMessage],
    on_error: Callable[[str], None] | None = None,
) -> int:
    """Publish and flush. Returns the number of messages not acknowledged in time."""
    for message in messages:
        producer.produce(
            topic,
            value=message.model_dump_json().encode("utf-8"),
            key=str(message.event_id).encode("ascii"),
        )
    pending = producer.flush(30) if messages else 0
    if pending and on_error is not None:
        on_error(f"{pending} fire-event messages were not acknowledged")
    return pending

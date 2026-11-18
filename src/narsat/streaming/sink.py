"""The hotspot stream sink: validate, land in bronze, upsert into PostGIS.

This module contains the processing logic of the stream consumer and nothing about
Kafka itself, so it can be exercised with plain Python objects in unit tests.

For one micro-batch:

1. every message is checked against the contract (schema, identifier, area of
   interest, plausible acquisition time); failures become dead letters with a reason;
2. valid messages are appended to the bronze Iceberg table - exactly once, using the
   Kafka offsets stored in the Iceberg snapshot (see ``narsat.lake.bronze``);
3. detections are inserted into PostGIS with ``ON CONFLICT DO NOTHING``.

Only when all three steps have succeeded does the caller commit the Kafka offsets.
A crash at any point therefore leads to a replay that changes nothing twice.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import ValidationError

from narsat import ids, metrics
from narsat.config import BoundingBox, DbSettings
from narsat.db import connect
from narsat.db.hotspots import insert_hotspots
from narsat.lake.bronze import BronzeRecord
from narsat.schemas import HotspotMessage


@dataclass(frozen=True)
class Incoming:
    """A Kafka record reduced to what the sink needs."""

    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes | None


@dataclass(frozen=True)
class DeadLetter:
    incoming: Incoming
    reason: str


@dataclass
class SinkResult:
    received: int = 0
    valid: int = 0
    duplicates_in_batch: int = 0
    bronze_written: int = 0
    inserted: list[uuid.UUID] = field(default_factory=list)
    dead_letters: list[DeadLetter] = field(default_factory=list)
    #: Acquisition-time range of the detections stored for the first time.
    touched: tuple[dt.datetime, dt.datetime] | None = None


class BronzeWriter(Protocol):
    def append(self, records: list[BronzeRecord], now: dt.datetime | None = None) -> int: ...


def validate_message(
    incoming: Incoming,
    bbox: BoundingBox,
    now: dt.datetime,
    max_future: dt.timedelta = dt.timedelta(hours=2),
) -> HotspotMessage | str:
    """Return the parsed message, or the reason it is rejected."""
    if not incoming.value:
        return "empty payload"
    try:
        message = HotspotMessage.model_validate_json(incoming.value)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "payload"
        return f"schema: {location}: {first['msg']}"
    expected = ids.hotspot_id(
        message.instrument, message.satellite, message.acq_ts, message.latitude, message.longitude
    )
    if message.hotspot_id != expected:
        return "hotspot_id does not match the natural key"
    if incoming.key is not None and incoming.key != str(message.hotspot_id).encode("ascii"):
        return "record key differs from hotspot_id"
    if not bbox.contains(message.longitude, message.latitude):
        return "outside the area of interest"
    if message.acq_ts > now + max_future:
        return "acquisition time is in the future"
    return message


class HotspotSink:
    def __init__(
        self,
        bbox: BoundingBox,
        bronze: BronzeWriter,
        db: DbSettings,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._bbox = bbox
        self._bronze = bronze
        self._db = db
        self._clock = clock

    def process(self, batch: Sequence[Incoming]) -> SinkResult:
        now = self._clock()
        result = SinkResult(received=len(batch))
        records: list[BronzeRecord] = []
        unique: dict[uuid.UUID, HotspotMessage] = {}

        for incoming in batch:
            outcome = validate_message(incoming, self._bbox, now)
            if isinstance(outcome, str):
                result.dead_letters.append(DeadLetter(incoming, outcome))
                continue
            result.valid += 1
            # Bronze keeps every valid record; duplicates are resolved in silver.
            records.append(
                BronzeRecord(outcome, incoming.topic, incoming.partition, incoming.offset)
            )
            if outcome.hotspot_id in unique:
                result.duplicates_in_batch += 1
            else:
                unique[outcome.hotspot_id] = outcome

        if records:
            result.bronze_written = self._bronze.append(records, now)
        if unique:
            with connect(self._db) as conn:
                result.inserted = insert_hotspots(conn, list(unique.values()))
            if result.inserted:
                stored = [unique[i].acq_ts for i in result.inserted]
                result.touched = (min(stored), max(stored))

        metrics.MESSAGES_CONSUMED.labels("valid").inc(result.valid)
        metrics.MESSAGES_CONSUMED.labels("dead_letter").inc(len(result.dead_letters))
        metrics.HOTSPOTS_STORED.labels("bronze").inc(result.bronze_written)
        metrics.HOTSPOTS_STORED.labels("postgis").inc(len(result.inserted))
        return result

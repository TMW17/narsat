"""Exactly-once landing of Kafka hotspot messages in the bronze Iceberg table.

Kafka delivers at least once: if the consumer dies after writing a batch but before
committing its offsets, the batch is delivered again. An Iceberg append is not
idempotent, so replaying it would duplicate rows.

The fix used here is the one Flink's and Kafka Connect's Iceberg sinks use: **the
Kafka offsets are stored in the summary of the Iceberg snapshot that contains the
rows**. Data and offsets become durable in the same atomic commit, and after a
restart the writer simply ignores every message at or below the recorded offset of
its partition.

Offsets are only meaningful for one incarnation of a topic. If Kafka's data is lost and
the topic re-created, its offsets start again at 0 and every new message would look
"already written". Kafka gives each topic a unique identifier at creation, so the
identifier is stored next to the offsets; when the writer is told a different one, it
discards that topic's recorded offsets instead of discarding the new messages.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from dataclasses import dataclass

import pyarrow as pa
from pyiceberg.catalog import Catalog
from pyiceberg.exceptions import CommitFailedException
from pyiceberg.table import Table
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random

from narsat.lake.catalog import BRONZE_HOTSPOTS
from narsat.log import get_logger
from narsat.schemas import HotspotMessage

log = get_logger(__name__)

OFFSETS_PROPERTY = "narsat.kafka.offsets"
TOPIC_IDS_PROPERTY = "narsat.kafka.topic-ids"
TopicPartition = tuple[str, int]


@dataclass(frozen=True)
class BronzeRecord:
    message: HotspotMessage
    topic: str
    partition: int
    offset: int


def encode_offsets(offsets: dict[TopicPartition, int]) -> str:
    nested: dict[str, dict[str, int]] = {}
    for (topic, partition), offset in sorted(offsets.items()):
        nested.setdefault(topic, {})[str(partition)] = offset
    return json.dumps(nested, sort_keys=True)


def decode_offsets(text: str) -> dict[TopicPartition, int]:
    nested = json.loads(text)
    return {
        (topic, int(partition)): int(offset)
        for topic, partitions in nested.items()
        for partition, offset in partitions.items()
    }


@dataclass(frozen=True)
class OffsetState:
    """What the newest bronze snapshot records about the Kafka input."""

    offsets: dict[TopicPartition, int]
    topic_ids: dict[str, str]

    def snapshot_properties(self) -> dict[str, str]:
        properties = {OFFSETS_PROPERTY: encode_offsets(self.offsets)} if self.offsets else {}
        if self.topic_ids:
            properties[TOPIC_IDS_PROPERTY] = json.dumps(self.topic_ids, sort_keys=True)
        return properties


def read_offset_state(table: Table) -> OffsetState:
    """Offsets and topic identifiers of the newest snapshot that records them.

    Walks the snapshot log from the newest entry: maintenance operations carry the
    properties forward, but an unrelated writer might not.
    """
    for snapshot in sorted(table.snapshots(), key=lambda s: s.timestamp_ms, reverse=True):
        if snapshot.summary is None:
            continue
        properties = snapshot.summary.additional_properties
        encoded = properties.get(OFFSETS_PROPERTY)
        if encoded:
            ids = properties.get(TOPIC_IDS_PROPERTY)
            return OffsetState(decode_offsets(encoded), json.loads(ids) if ids else {})
    return OffsetState({}, {})


def read_committed_offsets(table: Table) -> dict[TopicPartition, int]:
    """Highest offset per partition already contained in the table."""
    return read_offset_state(table).offsets


def reconcile_topic_ids(state: OffsetState, current: Mapping[str, str]) -> OffsetState:
    """Drop the recorded offsets of every topic whose identifier changed.

    ``current`` holds the identifiers reported by the broker now. A topic it does not
    mention (the broker could not be asked) keeps its offsets: without evidence that
    the topic was re-created, skipping replayed messages is the safe default.
    """
    recreated = {
        topic
        for topic, topic_id in current.items()
        if state.topic_ids.get(topic) not in (None, topic_id)
    }
    for topic in sorted(recreated):
        log.warning(
            "kafka_topic_recreated",
            topic=topic,
            recorded_id=state.topic_ids[topic],
            current_id=current[topic],
            discarded_offsets={
                partition: offset for (t, partition), offset in state.offsets.items() if t == topic
            },
        )
    offsets = {key: offset for key, offset in state.offsets.items() if key[0] not in recreated}
    return OffsetState(offsets, {**state.topic_ids, **current})


class BronzeHotspotWriter:
    def __init__(self, catalog: Catalog, topic_ids: Mapping[str, str] | None = None) -> None:
        """``topic_ids``: Kafka's identifier of each input topic, when the broker could
        report it (see ``narsat.streaming.kafka.topic_ids``)."""
        self._catalog = catalog
        self._topic_ids = dict(topic_ids or {})
        self._table = catalog.load_table(BRONZE_HOTSPOTS.identifier)
        self._arrow_schema = self._table.schema().as_arrow()
        self._committed = self._state().offsets

    def _state(self) -> OffsetState:
        return reconcile_topic_ids(read_offset_state(self._table), self._topic_ids)

    @property
    def committed_offsets(self) -> dict[TopicPartition, int]:
        return dict(self._committed)

    def _rows(self, records: list[BronzeRecord], now: dt.datetime) -> pa.Table:
        rows = []
        for record in records:
            row = record.message.model_dump(exclude={"schema_version", "hotspot_id"})
            row.update(
                hotspot_id=str(record.message.hotspot_id),
                ingested_at=now,
                kafka_topic=record.topic,
                kafka_partition=record.partition,
                kafka_offset=record.offset,
                acq_month=record.message.acq_ts.strftime("%Y-%m"),
            )
            rows.append(row)
        return pa.Table.from_pylist(rows, schema=self._arrow_schema)

    @retry(
        retry=retry_if_exception_type(CommitFailedException),
        wait=wait_random(0.1, 1.0),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    def _commit(self, records: list[BronzeRecord], now: dt.datetime) -> int:
        # Each attempt starts from the table's current state. Iceberg commits are
        # compare-and-swap on the table metadata, so if another writer (compaction, a
        # consumer that took over the partition during a rebalance) commits between
        # this read and our append, the append fails and the whole step is redone
        # against the new offsets. That closes the read-then-write race.
        self._table = self._catalog.load_table(BRONZE_HOTSPOTS.identifier)
        state = self._state()
        committed = dict(state.offsets)
        fresh = [r for r in records if r.offset > committed.get((r.topic, r.partition), -1)]
        if fresh:
            for record in fresh:
                key = (record.topic, record.partition)
                committed[key] = max(committed.get(key, -1), record.offset)
            self._table.append(
                self._rows(fresh, now),
                snapshot_properties=OffsetState(committed, state.topic_ids).snapshot_properties(),
            )
        self._committed = committed
        return len(fresh)

    def append(self, records: list[BronzeRecord], now: dt.datetime | None = None) -> int:
        """Append the records that are not in the table yet. Returns how many were written."""
        # Cheap pre-check against the offsets seen at the last commit; the authoritative
        # check happens inside ``_commit`` against the freshly loaded table.
        candidates = [
            r for r in records if r.offset > self._committed.get((r.topic, r.partition), -1)
        ]
        if not candidates:
            return 0
        written = self._commit(candidates, now or dt.datetime.now(dt.UTC))
        log.debug("bronze_appended", rows=written, skipped=len(records) - written)
        return written

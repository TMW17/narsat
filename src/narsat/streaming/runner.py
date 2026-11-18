"""The stream consumer service: Kafka in, bronze + PostGIS + fire events out.

The loop is a classic micro-batch consumer with manual offset management:

    poll -> HotspotSink.process -> dead letters flushed -> offsets committed
         -> fire events re-clustered -> event changes published

Offsets are committed only after the batch is durable in both sinks, and both sinks
are idempotent, which together give effectively-once processing.

Clustering runs after the commit and in its own transaction: a clustering failure
must not stop ingestion. The time range it still has to look at is remembered and
retried on the next batch; at start-up the service also picks up any detection that
has no event yet.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any, Protocol

from confluent_kafka import KafkaError, KafkaException, TopicPartition

from narsat import metrics
from narsat.config import DbSettings, KafkaSettings
from narsat.db import connect
from narsat.events.updater import EventUpdater, UpdateResult
from narsat.log import get_logger
from narsat.streaming import events_feed
from narsat.streaming.sink import DeadLetter, HotspotSink, Incoming, SinkResult

log = get_logger(__name__)

TimeRange = tuple[dt.datetime, dt.datetime]


class DeadLetterError(RuntimeError):
    """Rejected messages could not be stored on the dead-letter topic."""


class ConsumerLike(Protocol):
    def consume(self, num_messages: int = ..., timeout: float = ...) -> list[Any]: ...

    def commit(self, *, offsets: list[TopicPartition], asynchronous: bool) -> Any: ...

    def assignment(self) -> list[TopicPartition]: ...

    def position(self, partitions: list[TopicPartition]) -> list[TopicPartition]: ...

    def get_watermark_offsets(
        self, partition: TopicPartition, timeout: float = ..., cached: bool = ...
    ) -> tuple[int, int] | None: ...


class ProducerLike(Protocol):
    def produce(
        self,
        topic: str,
        value: bytes | None = ...,
        key: bytes | None = ...,
        headers: list[tuple[str, bytes]] | None = ...,
    ) -> None: ...

    def flush(self, timeout: float = ...) -> int: ...


def merge_ranges(a: TimeRange | None, b: TimeRange | None) -> TimeRange | None:
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), max(a[1], b[1]))


class StreamRunner:
    def __init__(
        self,
        consumer: ConsumerLike,
        producer: ProducerLike,
        sink: HotspotSink,
        updater: EventUpdater,
        kafka: KafkaSettings,
        db: DbSettings,
    ) -> None:
        self._consumer = consumer
        self._producer = producer
        self._sink = sink
        self._updater = updater
        self._kafka = kafka
        self._db = db
        self._pending_range: TimeRange | None = None

    # ------------------------------------------------------------------ Kafka I/O
    def _read_batch(self, stop: threading.Event) -> list[Incoming]:
        """Collect messages until the batch is full or its time budget is spent."""
        batch: list[Incoming] = []
        deadline = time.monotonic() + self._kafka.batch_max_seconds
        while not stop.is_set() and len(batch) < self._kafka.batch_max_messages:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            messages = self._consumer.consume(
                num_messages=self._kafka.batch_max_messages - len(batch),
                timeout=min(1.0, remaining),
            )
            for message in messages:
                error = message.error()
                if error is not None:
                    if error.code() == KafkaError._PARTITION_EOF:
                        continue
                    if error.fatal():
                        raise KafkaException(error)
                    log.warning("kafka_consume_error", error=str(error))
                    continue
                batch.append(
                    Incoming(
                        topic=message.topic(),
                        partition=message.partition(),
                        offset=message.offset(),
                        key=message.key(),
                        value=message.value(),
                    )
                )
            if not batch and not messages:
                # Idle topic: return so that time-driven work can run.
                break
        return batch

    def _send_dead_letters(self, dead: list[DeadLetter]) -> None:
        for letter in dead:
            source = letter.incoming
            self._producer.produce(
                self._kafka.topic_dlq,
                value=source.value,
                key=source.key,
                headers=[
                    ("narsat-reason", letter.reason.encode("utf-8")[:500]),
                    ("narsat-source-topic", source.topic.encode("ascii")),
                    ("narsat-source-partition", str(source.partition).encode("ascii")),
                    ("narsat-source-offset", str(source.offset).encode("ascii")),
                ],
            )
        if dead and self._producer.flush(30):
            # Do not commit past records whose dead letter is not safely stored.
            raise DeadLetterError("dead letters were not acknowledged by the broker")

    def _commit(self, batch: list[Incoming]) -> None:
        highest: dict[tuple[str, int], int] = {}
        for item in batch:
            key = (item.topic, item.partition)
            highest[key] = max(highest.get(key, -1), item.offset)
        offsets = [
            TopicPartition(topic, part, offset + 1) for (topic, part), offset in highest.items()
        ]
        self._consumer.commit(offsets=offsets, asynchronous=False)

    def _report_lag(self) -> None:
        try:
            assignment = self._consumer.assignment()
            for position in self._consumer.position(assignment):
                marks = self._consumer.get_watermark_offsets(position, timeout=2.0, cached=False)
                if marks is None or position.offset < 0:
                    continue
                metrics.CONSUMER_LAG.labels(position.topic, str(position.partition)).set(
                    max(0, marks[1] - position.offset)
                )
        except KafkaException as exc:  # lag is observability, never a reason to stop
            log.debug("lag_probe_failed", error=str(exc))

    # ------------------------------------------------------------------ fire events
    def _unclustered_range(self) -> TimeRange | None:
        with connect(self._db) as conn:
            row = conn.execute(
                "SELECT min(acq_ts) AS lo, max(acq_ts) AS hi FROM hotspot "
                "WHERE event_id IS NULL AND NOT is_static_source"
            ).fetchone()
        if row is None or row["lo"] is None:
            return None
        return (row["lo"], row["hi"])

    def update_events(self, now: dt.datetime | None = None) -> UpdateResult | None:
        """Re-cluster the pending time range and publish what changed."""
        now = now or dt.datetime.now(dt.UTC)
        try:
            with connect(self._db) as conn:
                result = self._updater.update(conn, as_of=now, touched=self._pending_range)
                messages = events_feed.build_messages(conn, result, now)
        except Exception:
            log.exception("event_update_failed", pending_range=str(self._pending_range))
            return None
        self._pending_range = None
        events_feed.publish(
            self._producer,
            self._kafka.topic_events,
            messages,
            on_error=lambda text: log.warning("event_feed_incomplete", detail=text),
        )
        return result

    # ------------------------------------------------------------------ main loop
    def process_once(self, stop: threading.Event) -> SinkResult | None:
        batch = self._read_batch(stop)
        if not batch:
            return None
        with metrics.SINK_BATCH_SECONDS.time():
            result = self._sink.process(batch)
            self._send_dead_letters(result.dead_letters)
            self._commit(batch)
        self._pending_range = merge_ranges(self._pending_range, result.touched)
        log.info(
            "stream_batch_done",
            received=result.received,
            valid=result.valid,
            dead=len(result.dead_letters),
            bronze=result.bronze_written,
            inserted=len(result.inserted),
        )
        return result

    def run(self, stop: threading.Event, idle_update_s: float = 300.0) -> None:
        log.info(
            "stream_consumer_started", topic=self._kafka.topic_hotspots, group=self._kafka.group_id
        )
        self._pending_range = self._unclustered_range()
        last_update = 0.0
        while not stop.is_set():
            result = self.process_once(stop)
            due = time.monotonic() - last_update >= idle_update_s
            if self._pending_range is not None or (result is None and due):
                # New detections, or enough idle time for statuses to have aged.
                self.update_events()
                last_update = time.monotonic()
            self._report_lag()
        log.info("stream_consumer_stopped")

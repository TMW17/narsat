"""The streaming path end to end.

    FIRMS (HTTP mocked) -> poller -> Kafka -> stream runner -> bronze + PostGIS -> events feed

Kafka is librdkafka's in-process mock cluster: the real producer and consumer clients
talk the real wire protocol to it. PostGIS is real. The lakehouse is on local disk.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
import uuid
from collections.abc import Iterator

import httpx2 as httpx
import pytest
from confluent_kafka import Consumer, Producer

from narsat.config import BoundingBox, ClusteringSettings, DbSettings, KafkaSettings, LakeSettings
from narsat.db import connect
from narsat.events.updater import EventUpdater
from narsat.ingestion.firms import FirmsClient
from narsat.ingestion.poller import FirmsPoller
from narsat.ingestion.state import SeenCache
from narsat.lake.bronze import BronzeHotspotWriter, read_committed_offsets
from narsat.lake.catalog import BRONZE_HOTSPOTS, ensure_tables, load_catalog
from narsat.schemas import FireEventMessage
from narsat.streaming import kafka
from narsat.streaming.runner import StreamRunner
from narsat.streaming.sink import HotspotSink
from tests.conftest import FIXTURES, make_hotspot

pytestmark = pytest.mark.integration
UTC = dt.UTC
BOX = BoundingBox(west=-13.5, south=27.0, east=11.8, north=37.6)
POLL_TIME = dt.datetime(2023, 7, 24, 6, 0, tzinfo=UTC)
MOCK_CLIENT = {"session.timeout.ms": 6000, "partition.assignment.strategy": "range"}


@pytest.fixture
def kafka_settings(kafka_bootstrap: str) -> KafkaSettings:
    return KafkaSettings(
        bootstrap_servers=kafka_bootstrap,
        group_id=f"sink-{uuid.uuid4().hex[:8]}",
        batch_max_messages=500,
        batch_max_seconds=3.0,
    )


@pytest.fixture
def producer(kafka_settings: KafkaSettings) -> Iterator[Producer]:
    client = kafka.make_producer(kafka_settings)
    yield client
    client.flush(5)


def _runner(
    kafka_settings: KafkaSettings, db: DbSettings, lake: LakeSettings, producer: Producer
) -> tuple[StreamRunner, Consumer]:
    catalog = load_catalog(lake)
    ensure_tables(catalog)
    consumer = kafka.make_consumer(kafka_settings, MOCK_CLIENT)
    consumer.subscribe([kafka_settings.topic_hotspots])
    runner = StreamRunner(
        consumer=consumer,
        producer=producer,
        sink=HotspotSink(BOX, BronzeHotspotWriter(catalog), db, clock=lambda: POLL_TIME),
        updater=EventUpdater(ClusteringSettings(), ["DZA", "MAR", "TUN"]),
        kafka=kafka_settings,
        db=db,
    )
    return runner, consumer


def _drain(runner: StreamRunner, expected: int, timeout_s: float = 40.0) -> int:
    """Run micro-batches until ``expected`` messages have been received."""
    stop = threading.Event()
    received = 0
    deadline = time.monotonic() + timeout_s
    while received < expected and time.monotonic() < deadline:
        result = runner.process_once(stop)
        if result is not None:
            received += result.received
    return received


def _read_topic(bootstrap: str, topic: str, expected: int, timeout_s: float = 20.0) -> list:
    reader = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": f"probe-{uuid.uuid4().hex}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            **MOCK_CLIENT,
        }
    )
    reader.subscribe([topic])
    messages: list = []
    deadline = time.monotonic() + timeout_s
    try:
        while len(messages) < expected and time.monotonic() < deadline:
            messages += [m for m in reader.consume(100, 1.0) if m.error() is None]
    finally:
        reader.close()
    return messages


def _poller(kafka_settings: KafkaSettings, producer: Producer, body: str) -> FirmsPoller:
    client = FirmsClient(
        "k" * 32, transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=body))
    )
    return FirmsPoller(
        client,
        producer,
        kafka_settings.topic_hotspots,
        BOX,
        ["VIIRS_NOAA20_NRT"],
        2,
        SeenCache(None),
        clock=lambda: POLL_TIME,
    )


def _counts(db: DbSettings, lake: LakeSettings) -> tuple[int, int]:
    with connect(db) as conn:
        row = conn.execute("SELECT count(*) AS n FROM hotspot").fetchone()
    assert row is not None
    bronze = load_catalog(lake).load_table(BRONZE_HOTSPOTS.identifier)
    return row["n"], bronze.scan().count()


def test_firms_to_postgis_bronze_and_event_feed(
    kafka_settings: KafkaSettings, db_settings: DbSettings, lake_settings: LakeSettings,
    producer: Producer,
) -> None:  # fmt: skip
    body = (FIXTURES / "firms_viirs_nrt.csv").read_text()
    stats = _poller(kafka_settings, producer, body).poll_once()[0]
    assert stats.published == 4

    runner, consumer = _runner(kafka_settings, db_settings, lake_settings, producer)
    try:
        assert _drain(runner, 4) == 4
        assert _counts(db_settings, lake_settings) == (4, 4)

        # Offsets in the Iceberg snapshot agree with what was consumed.
        bronze = load_catalog(lake_settings).load_table(BRONZE_HOTSPOTS.identifier)
        committed = read_committed_offsets(bronze)
        assert sum(offset + 1 for offset in committed.values()) == 4

        # Clustering: three detections near Béjaïa form one event, the fourth another.
        update = runner.update_events(now=dt.datetime(2023, 9, 1, tzinfo=UTC))
        assert update is not None
        assert len(update.events_created) == 2
        with connect(db_settings) as conn:
            sizes = conn.execute(
                "SELECT n_hotspots, mapping_status FROM fire_event ORDER BY n_hotspots DESC"
            ).fetchall()
        assert [(s["n_hotspots"], s["mapping_status"]) for s in sizes] == [
            (3, "pending"), (1, "skipped"),
        ]  # fmt: skip
    finally:
        consumer.close()

    # The change feed carries the state of both events, keyed by event id.
    feed = _read_topic(kafka_settings.bootstrap_servers, kafka_settings.topic_events, 2)
    events = [FireEventMessage.model_validate_json(m.value()) for m in feed]
    assert sorted(e.n_hotspots for e in events) == [1, 3]
    assert {m.key().decode() for m in feed} == {str(e.event_id) for e in events}
    assert {e.change for e in events} == {"created"}


def test_rejected_messages_go_to_the_dead_letter_topic_with_a_reason(
    kafka_settings: KafkaSettings, db_settings: DbSettings, lake_settings: LakeSettings,
    producer: Producer,
) -> None:  # fmt: skip
    good = make_hotspot(36.70012, 4.60234, dt.datetime(2023, 7, 23, 1, 34, tzinfo=UTC))
    sicily = make_hotspot(37.5, 14.0, dt.datetime(2023, 7, 23, 1, 34, tzinfo=UTC))
    topic = kafka_settings.topic_hotspots
    producer.produce(topic, good.model_dump_json().encode(), str(good.hotspot_id).encode())
    producer.produce(topic, b"{broken json", b"k1")
    producer.produce(topic, sicily.model_dump_json().encode(), str(sicily.hotspot_id).encode())
    tampered = json.loads(good.model_dump_json()) | {"latitude": 36.9}
    producer.produce(topic, json.dumps(tampered).encode(), str(good.hotspot_id).encode())
    assert producer.flush(10) == 0

    runner, consumer = _runner(kafka_settings, db_settings, lake_settings, producer)
    try:
        assert _drain(runner, 4) == 4
    finally:
        consumer.close()
    assert _counts(db_settings, lake_settings) == (1, 1)

    dead = _read_topic(kafka_settings.bootstrap_servers, kafka_settings.topic_dlq, 3)
    reasons = sorted(dict(m.headers())["narsat-reason"].decode() for m in dead)
    assert reasons[0].startswith("hotspot_id does not match")
    assert reasons[1] == "outside the area of interest"
    assert reasons[2].startswith("schema:")
    headers = dict(dead[0].headers())
    assert headers["narsat-source-topic"] == topic.encode()
    assert int(headers["narsat-source-offset"]) >= 0


def test_replay_after_a_crash_changes_nothing(
    kafka_settings: KafkaSettings, db_settings: DbSettings, lake_settings: LakeSettings,
    producer: Producer,
) -> None:  # fmt: skip
    body = (FIXTURES / "firms_viirs_nrt.csv").read_text()
    _poller(kafka_settings, producer, body).poll_once()

    # First consumer writes both sinks, then "crashes" before committing its offsets.
    crashing, consumer = _runner(kafka_settings, db_settings, lake_settings, producer)
    try:
        batch = []
        deadline = time.monotonic() + 40
        while len(batch) < 4 and time.monotonic() < deadline:
            batch += crashing._read_batch(threading.Event())
        assert len(batch) == 4
        result = crashing._sink.process(batch)
        assert (result.bronze_written, len(result.inserted)) == (4, 4)
    finally:
        consumer.close()  # no commit happened
    assert _counts(db_settings, lake_settings) == (4, 4)

    # A new consumer in the same group starts again from the beginning of the log.
    restarted, consumer = _runner(kafka_settings, db_settings, lake_settings, producer)
    try:
        assert _drain(restarted, 4) == 4
    finally:
        consumer.close()
    assert _counts(db_settings, lake_settings) == (4, 4)

    # After the commit, a third consumer has nothing left to read.
    third, consumer = _runner(kafka_settings, db_settings, lake_settings, producer)
    try:
        assert _drain(third, 1, timeout_s=8.0) == 0
    finally:
        consumer.close()


def test_duplicate_publication_is_absorbed(
    kafka_settings: KafkaSettings, db_settings: DbSettings, lake_settings: LakeSettings,
    producer: Producer,
) -> None:  # fmt: skip
    body = (FIXTURES / "firms_viirs_nrt.csv").read_text()
    # Two pollers without shared memory (e.g. a restart that lost its state file).
    _poller(kafka_settings, producer, body).poll_once()
    _poller(kafka_settings, producer, body).poll_once()

    runner, consumer = _runner(kafka_settings, db_settings, lake_settings, producer)
    try:
        assert _drain(runner, 8) == 8
    finally:
        consumer.close()
    hotspots, bronze_rows = _counts(db_settings, lake_settings)
    assert hotspots == 4  # the operational store holds each detection once
    assert bronze_rows == 8  # bronze is the raw log; silver removes the duplicates


def test_topic_id_lookup_falls_back_when_the_broker_cannot_answer(kafka_bootstrap: str) -> None:
    """librdkafka's mock cluster does not implement DescribeTopics, like some brokers."""
    import time

    from narsat.config import KafkaSettings
    from narsat.streaming import kafka

    started = time.monotonic()
    ids = kafka.topic_ids(
        KafkaSettings(bootstrap_servers=kafka_bootstrap), ["firms.hotspots.raw"], timeout_s=2.0
    )
    assert ids == {}
    assert time.monotonic() - started < 10

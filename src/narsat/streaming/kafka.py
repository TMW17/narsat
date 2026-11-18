"""Kafka client construction and topic administration.

All librdkafka settings live here so that delivery guarantees are decided in one place:

* producers are idempotent with ``acks=all`` (no duplicates from retries, no loss on
  leader change);
* the sink consumer never auto-commits: offsets are committed only after a batch has
  been written to both sinks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from confluent_kafka import Consumer, KafkaException, Producer, TopicCollection
from confluent_kafka.admin import AdminClient, NewTopic

from narsat.config import KafkaSettings
from narsat.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class TopicSpec:
    name: str
    partitions: int
    config: dict[str, str]


def topic_specs(settings: KafkaSettings) -> list[TopicSpec]:
    week_ms = str(7 * 24 * 3600 * 1000)
    return [
        # Raw detections: a replayable log, kept one week.
        TopicSpec(
            settings.topic_hotspots,
            settings.hotspots_partitions,
            {"cleanup.policy": "delete", "retention.ms": week_ms},
        ),
        # Messages that failed the contract, with the reason in the headers.
        TopicSpec(
            settings.topic_dlq,
            1,
            {"cleanup.policy": "delete", "retention.ms": str(30 * 24 * 3600 * 1000)},
        ),
        # Latest state per event: compaction keeps one record per key.
        TopicSpec(
            settings.topic_events,
            1,
            {"cleanup.policy": "compact", "min.cleanable.dirty.ratio": "0.1"},
        ),
    ]


def producer_config(settings: KafkaSettings, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {
        "bootstrap.servers": settings.bootstrap_servers,
        "client.id": f"{settings.client_id}-producer",
        "enable.idempotence": True,
        "acks": "all",
        "compression.type": "zstd",
        "linger.ms": 50,
        "delivery.timeout.ms": 120_000,
    }
    config.update(extra or {})
    return config


def consumer_config(settings: KafkaSettings, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {
        "bootstrap.servers": settings.bootstrap_servers,
        "client.id": f"{settings.client_id}-sink",
        "group.id": settings.group_id,
        "enable.auto.commit": False,
        "enable.auto.offset.store": False,
        "auto.offset.reset": "earliest",
        "partition.assignment.strategy": "cooperative-sticky",
        "max.poll.interval.ms": 600_000,
    }
    config.update(extra or {})
    return config


def make_producer(settings: KafkaSettings, extra: dict[str, Any] | None = None) -> Producer:
    return Producer(producer_config(settings, extra))


def make_consumer(settings: KafkaSettings, extra: dict[str, Any] | None = None) -> Consumer:
    return Consumer(consumer_config(settings, extra))


def ensure_topics(settings: KafkaSettings, timeout_s: float = 30.0) -> dict[str, str]:
    """Create the project's topics if they are missing. Safe to run repeatedly.

    Returns ``{topic: "created" | "exists"}``.
    """
    admin = AdminClient({"bootstrap.servers": settings.bootstrap_servers})
    existing = set(admin.list_topics(timeout=timeout_s).topics)
    outcome: dict[str, str] = {}
    wanted = [spec for spec in topic_specs(settings) if spec.name not in existing]
    for spec in topic_specs(settings):
        if spec.name in existing:
            outcome[spec.name] = "exists"
    if not wanted:
        return outcome
    futures = admin.create_topics(
        [
            NewTopic(
                spec.name, num_partitions=spec.partitions, replication_factor=1, config=spec.config
            )
            for spec in wanted
        ],
        operation_timeout=timeout_s,
        request_timeout=timeout_s,
    )
    for name, future in futures.items():
        try:
            future.result(timeout_s)
            outcome[name] = "created"
        except KafkaException as exc:
            # Another process may have created it between the listing and the request.
            if "TOPIC_ALREADY_EXISTS" in str(exc):
                outcome[name] = "exists"
            else:
                raise
    log.info("kafka_topics_ready", topics=outcome)
    return outcome


def topic_ids(
    settings: KafkaSettings, topics: list[str], timeout_s: float = 10.0
) -> dict[str, str]:
    """Kafka's unique identifier of each topic (a new one whenever a topic is re-created).

    Best effort: a broker that cannot describe topics (old versions, test doubles) yields
    an empty mapping, and the bronze writer then keeps its recorded offsets as before.
    """
    admin = AdminClient({"bootstrap.servers": settings.bootstrap_servers})
    try:
        futures = admin.describe_topics(TopicCollection(topics), request_timeout=timeout_s)
        return {
            name: str(future.result(timeout=timeout_s).topic_id) for name, future in futures.items()
        }
    except Exception as exc:  # the stream must start even if this lookup fails
        log.warning("kafka_topic_ids_unavailable", topics=topics, error=type(exc).__name__)
        return {}

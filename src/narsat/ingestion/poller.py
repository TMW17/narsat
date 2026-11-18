"""The FIRMS poller: turns a polling API into an event stream.

Each cycle asks FIRMS for the last ``day_range`` days of every configured source,
keeps the detections that were not delivered before and publishes them to Kafka,
keyed by hotspot identifier. A detection is remembered as "seen" only after the
broker has acknowledged it, so a failed delivery is retried on the next cycle.

The same code path serves historical backfills (``backfill``): past detections are
replayed onto the topic in five-day windows and flow through the identical stream
sink, which is how the pipeline is validated on documented fires.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from narsat import metrics
from narsat.config import BoundingBox
from narsat.ingestion.firms import FirmsClient, FirmsError, parse_area_csv
from narsat.ingestion.state import SeenCache
from narsat.log import get_logger
from narsat.schemas import HotspotMessage

log = get_logger(__name__)


class ProducerLike(Protocol):
    """The part of ``confluent_kafka.Producer`` the poller relies on."""

    def produce(
        self,
        topic: str,
        value: bytes | None = ...,
        key: bytes | None = ...,
        *,
        on_delivery: Callable[..., None] | None = ...,
    ) -> None: ...

    def poll(self, timeout: float = ...) -> int: ...

    def flush(self, timeout: float = ...) -> int: ...


@dataclass
class PollStats:
    source: str
    fetched: int = 0
    rejected: int = 0
    outside_aoi: int = 0
    already_seen: int = 0
    published: int = 0
    failed: int = 0
    error: str | None = None


class FirmsPoller:
    def __init__(
        self,
        client: FirmsClient,
        producer: ProducerLike,
        topic: str,
        bbox: BoundingBox,
        sources: Iterable[str],
        day_range: int,
        cache: SeenCache,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._client = client
        self._producer = producer
        self._topic = topic
        self._bbox = bbox
        self._sources = list(sources)
        self._day_range = day_range
        self._cache = cache
        self._clock = clock

    # ------------------------------------------------------------------ publishing
    def _publish(self, source: str, messages: list[HotspotMessage], stats: PollStats) -> None:
        delivered: list[HotspotMessage] = []

        def on_delivery(message: HotspotMessage) -> Callable[..., None]:
            def callback(err: object, _msg: object) -> None:
                if err is None:
                    delivered.append(message)
                else:
                    stats.failed += 1
                    log.warning("hotspot_delivery_failed", source=source, error=str(err))

            return callback

        for message in messages:
            self._producer.produce(
                self._topic,
                value=message.model_dump_json().encode("utf-8"),
                key=str(message.hotspot_id).encode("ascii"),
                on_delivery=on_delivery(message),
            )
            self._producer.poll(0)
        remaining = self._producer.flush(60)
        if remaining:
            # Not acknowledged in time: leave them unseen so the next cycle retries.
            stats.failed += remaining
        for message in delivered:
            self._cache.add(message.hotspot_id, message.acq_ts.date())
        stats.published = len(delivered)
        metrics.HOTSPOTS_PUBLISHED.labels(source).inc(len(delivered))
        if stats.failed:
            metrics.PUBLISH_FAILURES.labels(source).inc(stats.failed)

    def _ingest_csv(self, source: str, text: str, stats: PollStats) -> None:
        parsed = parse_area_csv(text, source, self._clock())
        stats.fetched = len(parsed.hotspots) + len(parsed.rejected)
        stats.rejected = len(parsed.rejected)
        for bad in parsed.rejected[:5]:
            log.warning(
                "firms_row_rejected", source=source, line=bad.line_number, reason=bad.reason
            )

        fresh: list[HotspotMessage] = []
        batch_ids: set[str] = set()
        for message in parsed.hotspots:
            key = str(message.hotspot_id)
            if not self._bbox.contains(message.longitude, message.latitude):
                stats.outside_aoi += 1
            elif key in self._cache or key in batch_ids:
                stats.already_seen += 1
            else:
                batch_ids.add(key)
                fresh.append(message)
        if fresh:
            self._publish(source, fresh, stats)

        metrics.FIRMS_ROWS.labels(source, "accepted").inc(len(parsed.hotspots))
        metrics.FIRMS_ROWS.labels(source, "rejected").inc(stats.rejected)

    # ------------------------------------------------------------------ operations
    def poll_source(
        self, source: str, date: dt.date | None = None, day_range: int | None = None
    ) -> PollStats:
        stats = PollStats(source=source)
        try:
            text = self._client.area_csv(source, self._bbox, day_range or self._day_range, date)
        except FirmsError as exc:
            stats.error = str(exc)
            metrics.FIRMS_REQUESTS.labels(source, "error").inc()
            log.error("firms_poll_failed", source=source, error=stats.error)
            return stats
        metrics.FIRMS_REQUESTS.labels(source, "ok").inc()
        self._ingest_csv(source, text, stats)
        if date is None:
            metrics.FIRMS_LAST_SUCCESS.labels(source).set(self._clock().timestamp())
        log.info("firms_poll_done", **stats.__dict__)
        return stats

    def poll_once(self) -> list[PollStats]:
        """One cycle over every source. A failing source never blocks the others."""
        results = [self.poll_source(source) for source in self._sources]
        self._cache.prune(self._clock().date())
        self._cache.save()
        return results

    def backfill(self, source: str, start: dt.date, end: dt.date) -> list[PollStats]:
        """Replay ``start..end`` (inclusive) for one source in five-day windows."""
        if end < start:
            raise ValueError("end must not be before start")
        results: list[PollStats] = []
        cursor = start
        while cursor <= end:
            span = min(5, (end - cursor).days + 1)
            results.append(self.poll_source(source, date=cursor, day_range=span))
            cursor += dt.timedelta(days=span)
        self._cache.save()
        return results

    def run_forever(self, interval_s: float, stop: threading.Event) -> None:
        log.info(
            "firms_poller_started",
            sources=self._sources,
            interval_s=interval_s,
            bbox=self._bbox.as_firms(),
        )
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception:  # keep the service alive; the error is logged with its trace
                log.exception("firms_poll_cycle_crashed")
            stop.wait(interval_s)
        log.info("firms_poller_stopped")

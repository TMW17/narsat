"""Composition root: build the long-running services from the settings.

Everything that needs to know *which* concrete client, catalog or broker is used is
assembled here, so the modules holding the logic stay free of configuration lookups
and can be constructed with test doubles.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from narsat.config import Settings
from narsat.errors import ConfigurationError
from narsat.events.updater import EventUpdater
from narsat.ingestion.firms import FirmsClient
from narsat.ingestion.poller import FirmsPoller
from narsat.ingestion.state import SeenCache
from narsat.lake.bronze import BronzeHotspotWriter
from narsat.lake.catalog import ensure_tables, load_catalog
from narsat.lake.storage import LakeStorage
from narsat.streaming import kafka
from narsat.streaming.runner import StreamRunner
from narsat.streaming.sink import HotspotSink

if TYPE_CHECKING:
    # The imaging stack (rasterio, GDAL, STAC) is imported only by the commands that
    # map events, so that the other commands start without it.
    from narsat.pipeline.map_event import EventMapper


def stop_on_signals() -> threading.Event:
    """An event that is set on SIGINT/SIGTERM, for graceful shutdown of service loops."""
    stop = threading.Event()

    def handler(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    return stop


def build_firms_client(settings: Settings) -> FirmsClient:
    if settings.firms.map_key is None:
        raise ConfigurationError(
            "NARSAT_FIRMS_MAP_KEY is not set. Request a free key at "
            "https://firms.modaps.eosdis.nasa.gov/api/map_key/ and put it in .env"
        )
    return FirmsClient(
        settings.firms.map_key.get_secret_value(),
        base_url=settings.firms.base_url,
        timeout_s=settings.firms.request_timeout_s,
    )


def build_poller(settings: Settings, persistent_state: bool = True) -> FirmsPoller:
    cache = SeenCache(
        settings.firms.state_path if persistent_state else None,
        retention_days=settings.firms.day_range + 3,
    )
    return FirmsPoller(
        client=build_firms_client(settings),
        producer=kafka.make_producer(settings.kafka),
        topic=settings.kafka.topic_hotspots,
        bbox=settings.aoi.bbox,
        sources=settings.firms.sources,
        day_range=settings.firms.day_range,
        cache=cache,
    )


def build_event_updater(settings: Settings) -> EventUpdater:
    return EventUpdater(settings.cluster, settings.aoi.countries)


def build_stream_runner(settings: Settings) -> tuple[StreamRunner, Callable[[], None]]:
    """Returns the runner and a function that closes its Kafka clients."""
    catalog = load_catalog(settings.lake)
    ensure_tables(catalog)
    consumer = kafka.make_consumer(settings.kafka)
    consumer.subscribe([settings.kafka.topic_hotspots])
    producer = kafka.make_producer(settings.kafka)
    runner = StreamRunner(
        consumer=consumer,
        producer=producer,
        sink=HotspotSink(
            settings.aoi.bbox,
            BronzeHotspotWriter(
                catalog, kafka.topic_ids(settings.kafka, [settings.kafka.topic_hotspots])
            ),
            settings.db,
        ),
        updater=build_event_updater(settings),
        kafka=settings.kafka,
        db=settings.db,
    )

    def close() -> None:
        producer.flush(10)
        consumer.close()

    return runner, close


def build_event_mapper(settings: Settings) -> EventMapper:
    from narsat.imagery.stac import earth_search
    from narsat.pipeline.map_event import EventMapper
    from narsat.segmentation.client import InferenceClient

    inference = (
        InferenceClient(settings.seg.inference_url, settings.seg.inference_timeout_s)
        if settings.seg.inference_url
        else None
    )
    storage = LakeStorage.assets(settings.lake)
    storage.ensure_ready()
    return EventMapper(
        imagery=settings.imagery,
        seg=settings.seg,
        storage=storage,
        search=earth_search(
            settings.imagery.stac_url,
            settings.imagery.collection,
            settings.imagery.max_scene_cloud_pct,
        ),
        inference=inference,
    )

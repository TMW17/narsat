"""Prometheus metrics shared by the long-running services.

Metric names follow the Prometheus conventions (unit suffixes, ``_total`` for counters).
The poller and the stream consumer expose them with ``start_metrics_server``; the API
mounts the same registry under ``/metrics``.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server

# --- FIRMS poller -----------------------------------------------------------------
FIRMS_REQUESTS = Counter("narsat_firms_requests_total", "FIRMS API requests", ["source", "outcome"])
FIRMS_ROWS = Counter("narsat_firms_rows_total", "Rows returned by FIRMS", ["source", "outcome"])
FIRMS_LAST_SUCCESS = Gauge(
    "narsat_firms_last_success_timestamp_seconds",
    "Unix time of the last successful FIRMS poll",
    ["source"],
)
HOTSPOTS_PUBLISHED = Counter(
    "narsat_hotspots_published_total", "Hotspot messages delivered to Kafka", ["source"]
)
PUBLISH_FAILURES = Counter(
    "narsat_hotspot_publish_failures_total", "Hotspot messages Kafka refused", ["source"]
)

# --- Stream consumer ---------------------------------------------------------------
MESSAGES_CONSUMED = Counter(
    "narsat_stream_messages_total", "Messages read from the hotspot topic", ["outcome"]
)
SINK_BATCH_SECONDS = Histogram(
    "narsat_stream_batch_seconds",
    "Time to validate and persist one micro-batch",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
CONSUMER_LAG = Gauge(
    "narsat_stream_consumer_lag_messages",
    "High watermark minus committed position",
    ["topic", "partition"],
)
HOTSPOTS_STORED = Counter(
    "narsat_hotspots_stored_total", "Hotspots written by the stream sink", ["target"]
)
EVENTS_CHANGED = Counter(
    "narsat_events_changed_total", "Fire events created or updated", ["change"]
)
CLUSTERING_SECONDS = Histogram(
    "narsat_clustering_seconds",
    "Duration of one event-clustering cycle",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

# --- Batch mapping -----------------------------------------------------------------
EVENTS_MAPPED = Counter(
    "narsat_events_mapped_total", "Events processed by the mapping pipeline", ["outcome"]
)
MAPPING_SECONDS = Histogram(
    "narsat_event_mapping_seconds",
    "Duration of burned-area mapping for one event",
    buckets=(5, 10, 30, 60, 120, 300, 600, 1200),
)
DQ_CHECKS = Counter("narsat_dq_checks_total", "Data-quality checks", ["suite", "status"])


def start_metrics_server(port: int) -> None:
    """Expose the default registry on ``port`` (no-op when the port is 0)."""
    if port > 0:
        start_http_server(port)

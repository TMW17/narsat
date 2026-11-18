"""Shared fixtures.

Unit tests need nothing but the Python environment. Integration tests are skipped
unless the matching variable points at a disposable service:

* ``NARSAT_TEST_DB_DSN``       PostgreSQL + PostGIS database whose name contains ``test``
  (tables are truncated between tests, so it must not be a database you care about)
* ``NARSAT_TEST_S3_ENDPOINT``  S3-compatible endpoint (with ``NARSAT_TEST_S3_ACCESS_KEY``
  and ``NARSAT_TEST_S3_SECRET_KEY``)

The Kafka tests use the mock cluster built into librdkafka, so they run everywhere.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlparse

import pytest
from pydantic import SecretStr

from narsat import ids
from narsat.config import DbSettings, LakeSettings, reset_settings_cache
from narsat.schemas import HotspotMessage

FIXTURES = Path(__file__).parent / "fixtures"
UTC = dt.UTC

TABLES = (
    "burned_area_validation, burned_area, raster_asset, hotspot, fire_event, static_cell, "
    "reference_perimeter, dq_result, admin_unit, app_user, mart.fire_activity_daily, "
    "mart.burned_area_admin_year, mart.event_summary"
)


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test may read the developer's ``.env`` or NARSAT_* variables, nor write to the repo."""
    for name in list(os.environ):
        if name.startswith("NARSAT_") and not name.startswith("NARSAT_TEST_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    reset_settings_cache()
    yield
    reset_settings_cache()


def make_hotspot(
    latitude: float,
    longitude: float,
    acq_ts: dt.datetime,
    *,
    confidence: str = "n",
    frp: float | None = 10.0,
    satellite: str = "N20",
    instrument: str = "VIIRS",
    source: str = "VIIRS_NOAA20_NRT",
    type_code: int | None = None,
) -> HotspotMessage:
    levels = {"l": "low", "n": "nominal", "h": "high"}
    return HotspotMessage(
        hotspot_id=ids.hotspot_id(instrument, satellite, acq_ts, latitude, longitude),
        source=source,
        satellite=satellite,
        instrument=instrument,  # type: ignore[arg-type]
        acq_ts=acq_ts,
        latitude=latitude,
        longitude=longitude,
        brightness_k=335.0,
        brightness_2_k=295.0,
        scan_km=0.4,
        track_km=0.4,
        frp_mw=frp,
        confidence_raw=confidence,
        confidence_level=levels[confidence],  # type: ignore[arg-type]
        daynight="N",
        version="2.0NRT",
        type_code=type_code,
        fetched_at=acq_ts + dt.timedelta(hours=3),
    )


@pytest.fixture
def hotspot_factory() -> Callable[..., HotspotMessage]:
    return make_hotspot


# ------------------------------------------------------------------------------- lake
@pytest.fixture
def lake_settings(tmp_path: Path) -> LakeSettings:
    """A lakehouse on the local filesystem with a SQLite catalog."""
    root = tmp_path / "lake"
    return LakeSettings(
        catalog_uri=SecretStr(f"sqlite:///{root}/catalog.db"),
        warehouse_uri=f"file://{root}/warehouse",
        assets_uri=f"file://{root}/assets",
    )


# ------------------------------------------------------------------------------- PostGIS
def _test_dsn() -> str | None:
    dsn = os.environ.get("NARSAT_TEST_DB_DSN")
    if not dsn:
        return None
    database = urlparse(dsn).path.lstrip("/")
    if "test" not in database:
        raise pytest.UsageError(
            f"NARSAT_TEST_DB_DSN points at database {database!r}; refusing to truncate a "
            "database whose name does not contain 'test'"
        )
    return dsn


@pytest.fixture(scope="session")
def _migrated_dsn() -> str:
    dsn = _test_dsn()
    if dsn is None:
        pytest.skip("set NARSAT_TEST_DB_DSN to run the PostGIS integration tests")
    from narsat.db import migrate

    settings = DbSettings(dsn=SecretStr(dsn))
    migrate.downgrade(settings, "base")
    migrate.upgrade(settings)
    return dsn


@pytest.fixture
def db_settings(_migrated_dsn: str) -> DbSettings:
    """A migrated, empty database."""
    from narsat.db import connect

    settings = DbSettings(dsn=SecretStr(_migrated_dsn))
    with connect(settings) as conn:
        conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    return settings


# ------------------------------------------------------------------------------- S3
@pytest.fixture
def s3_lake_settings() -> Iterator[LakeSettings]:
    endpoint = os.environ.get("NARSAT_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("set NARSAT_TEST_S3_ENDPOINT to run the object-storage integration tests")
    # One bucket, one throw-away prefix per test: SeaweedFS allocates a set of volumes for
    # every bucket, so a bucket per test would exhaust a small development server.
    bucket = os.environ.get("NARSAT_TEST_S3_BUCKET", "narsat-test")
    prefix = f"{bucket}/run-{uuid.uuid4().hex[:10]}"
    settings = LakeSettings(
        catalog_uri=SecretStr("sqlite:///s3_catalog.db"),
        warehouse_uri=f"s3://{prefix}/warehouse",
        assets_uri=f"s3://{prefix}/assets",
        s3_endpoint=endpoint,
        s3_access_key=SecretStr(os.environ.get("NARSAT_TEST_S3_ACCESS_KEY", "")),
        s3_secret_key=SecretStr(os.environ.get("NARSAT_TEST_S3_SECRET_KEY", "")),
    )
    yield settings
    from narsat.lake.storage import s3_filesystem

    with contextlib.suppress(OSError):  # best-effort clean-up of the throw-away prefix
        s3_filesystem(settings).delete_dir(prefix)


# ------------------------------------------------------------------------------- Kafka
@pytest.fixture
def kafka_bootstrap() -> Iterator[str]:
    """Bootstrap address of an in-process Kafka mock cluster (librdkafka)."""
    from confluent_kafka import Producer

    owner = Producer({"test.mock.num.brokers": 1})
    metadata = owner.list_topics(timeout=10)
    yield ",".join(f"{broker.host}:{broker.port}" for broker in metadata.brokers.values())
    owner.flush(1)

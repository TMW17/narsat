"""Typed runtime configuration.

Every value comes from an environment variable (or the ``.env`` file in the working
directory). Each section has its own prefix so the variable names stay readable:
``NARSAT_FIRMS_MAP_KEY``, ``NARSAT_KAFKA_BOOTSTRAP_SERVERS``, ``NARSAT_DB_DSN``...

Secrets are ``SecretStr`` so they never appear in ``repr`` output or structured logs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# A value that is fine on a laptop but must never reach a deployed environment.
DEV_JWT_SECRET = "dev-only-change-me-dev-only-change-me"  # noqa: S105 - documented dev default


def _split_csv(value: object) -> object:
    """Accept ``a,b,c`` as well as real lists for list-typed settings."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CsvList = Annotated[list[str], NoDecode]


class BoundingBox(BaseModel):
    """Geographic bounding box in EPSG:4326, in FIRMS order (west, south, east, north)."""

    west: float = Field(ge=-180, le=180)
    south: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)

    @model_validator(mode="after")
    def _ordered(self) -> BoundingBox:
        if self.west >= self.east or self.south >= self.north:
            raise ValueError("bounding box must satisfy west < east and south < north")
        return self

    @classmethod
    def parse(cls, text: str) -> BoundingBox:
        parts = [float(p) for p in text.split(",")]
        if len(parts) != 4:
            raise ValueError("expected 'west,south,east,north'")
        return cls(west=parts[0], south=parts[1], east=parts[2], north=parts[3])

    def contains(self, lon: float, lat: float) -> bool:
        return self.west <= lon <= self.east and self.south <= lat <= self.north

    def as_firms(self) -> str:
        return f"{self.west:g},{self.south:g},{self.east:g},{self.north:g}"

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.west, self.south, self.east, self.north)


class _Section(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


class AoiSettings(_Section):
    """Area of interest. The default box covers Morocco, northern Algeria and Tunisia."""

    model_config = SettingsConfigDict(
        env_prefix="NARSAT_AOI_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # NoDecode: the value is the FIRMS-style string "west,south,east,north", not JSON.
    bbox: Annotated[BoundingBox, NoDecode] = BoundingBox(
        west=-13.5, south=27.0, east=11.8, north=37.6
    )
    # ISO-3166 alpha-3 codes. Events attributed to another country are kept but never mapped.
    countries: CsvList = ["DZA", "MAR", "TUN"]

    @field_validator("bbox", mode="before")
    @classmethod
    def _parse_bbox(cls, value: object) -> object:
        return BoundingBox.parse(value) if isinstance(value, str) else value

    _csv = field_validator("countries", mode="before")(_split_csv)


class FirmsSettings(_Section):
    model_config = SettingsConfigDict(
        env_prefix="NARSAT_FIRMS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    map_key: SecretStr | None = None
    base_url: str = "https://firms.modaps.eosdis.nasa.gov"
    sources: CsvList = ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "MODIS_NRT"]
    poll_interval_s: int = Field(default=600, ge=60)
    # FIRMS accepts 1..5 days per request. Two days covers late-arriving detections.
    day_range: int = Field(default=2, ge=1, le=5)
    request_timeout_s: float = Field(default=60.0, gt=0)
    state_path: Path = Path("var/poller_state.json")

    _csv = field_validator("sources", mode="before")(_split_csv)

    @field_validator("map_key", mode="before")
    @classmethod
    def _blank_key_is_no_key(cls, value: object) -> object:
        # Compose passes `NARSAT_FIRMS_MAP_KEY=` when .env leaves it empty.
        return None if isinstance(value, str) and not value.strip() else value


class KafkaSettings(_Section):
    model_config = SettingsConfigDict(
        env_prefix="NARSAT_KAFKA_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bootstrap_servers: str = "localhost:9092"
    client_id: str = "narsat"
    group_id: str = "narsat-hotspot-sink"
    topic_hotspots: str = "firms.hotspots.raw"
    topic_dlq: str = "firms.hotspots.dlq"
    topic_events: str = "narsat.fire-events"
    hotspots_partitions: int = Field(default=3, ge=1)
    batch_max_messages: int = Field(default=2000, ge=1)
    batch_max_seconds: float = Field(default=15.0, gt=0)


class DbSettings(_Section):
    model_config = SettingsConfigDict(
        env_prefix="NARSAT_DB_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    dsn: SecretStr = SecretStr("postgresql://narsat:narsat@localhost:5432/narsat")
    pool_min_size: int = Field(default=1, ge=0)
    pool_max_size: int = Field(default=8, ge=1)

    def sqlalchemy_url(self) -> str:
        """Same database, expressed for SQLAlchemy with the psycopg 3 driver."""
        dsn = self.dsn.get_secret_value()
        for prefix in ("postgresql://", "postgres://"):
            if dsn.startswith(prefix):
                return "postgresql+psycopg://" + dsn[len(prefix) :]
        return dsn


class LakeSettings(_Section):
    """Lakehouse storage: Iceberg warehouse, raster/JSON assets and the Iceberg SQL catalog."""

    model_config = SettingsConfigDict(
        env_prefix="NARSAT_LAKE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    catalog_name: str = "narsat"
    # SQLAlchemy URL of the Iceberg SQL catalog (SQLite for local work, PostgreSQL in Compose).
    catalog_uri: SecretStr = SecretStr("sqlite:///var/lake/catalog.db")
    warehouse_uri: str = "file://var/lake/warehouse"
    assets_uri: str = "file://var/lake/assets"
    # S3-compatible endpoint, only used when the URIs above start with s3://
    s3_endpoint: str | None = None
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_region: str = "us-east-1"
    snapshot_retention_days: int = Field(default=14, ge=1)

    @field_validator("warehouse_uri", "assets_uri")
    @classmethod
    def _known_scheme(cls, value: str) -> str:
        if not value.startswith(("file://", "s3://")):
            raise ValueError("lake URIs must start with file:// or s3://")
        return value.rstrip("/")


class ClusteringSettings(_Section):
    """Spatio-temporal clustering of hotspots into fire events (see docs/architecture.md)."""

    model_config = SettingsConfigDict(
        env_prefix="NARSAT_CLUSTER_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    eps_km: float = Field(default=2.0, gt=0)
    eps_hours: float = Field(default=72.0, gt=0)
    min_samples: int = Field(default=1, ge=1)
    # Hotspots newer than this (relative to the newest detection) are re-clustered every cycle.
    window_days: int = Field(default=10, ge=1)
    active_hours: float = Field(default=48.0, gt=0)
    # An event is handed to burned-area mapping once it has been quiet for this long.
    settle_days: float = Field(default=3.0, ge=0)
    min_hotspots_to_map: int = Field(default=2, ge=1)
    # Persistent thermal anomalies (gas flares, industry): grid cells that light up on many days.
    static_cell_deg: float = Field(default=0.02, gt=0)
    static_min_days: int = Field(default=10, ge=2)
    static_lookback_days: int = Field(default=60, ge=7)
    # Nominal half-size of a detection footprint, used to build the event hull.
    footprint_buffer_m: float = Field(default=500.0, gt=0)


class ImagerySettings(_Section):
    model_config = SettingsConfigDict(
        env_prefix="NARSAT_IMAGERY_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    stac_url: str = "https://earth-search.aws.element84.com/v1"
    collection: str = "sentinel-2-c1-l2a"
    pre_window_days: int = Field(default=60, ge=5)
    post_window_days: int = Field(default=45, ge=5)
    post_min_delay_days: int = Field(default=1, ge=0)
    # Scene-level pre-filter only; the real test is the clear fraction inside the event AOI.
    max_scene_cloud_pct: float = Field(default=70.0, ge=0, le=100)
    min_clear_fraction: float = Field(default=0.7, ge=0, le=1)
    max_candidates: int = Field(default=6, ge=1)
    resolution_m: float = Field(default=20.0, gt=0)
    aoi_buffer_m: float = Field(default=1500.0, ge=0)
    max_pixels: int = Field(default=16_000_000, ge=10_000)
    read_threads: int = Field(default=6, ge=1)


class SegmentationSettings(_Section):
    model_config = SettingsConfigDict(
        env_prefix="NARSAT_SEG_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # "dnbr" needs nothing but the imagery. "model" calls the inference service.
    methods: CsvList = ["dnbr"]
    dnbr_threshold_mode: Literal["fixed", "otsu"] = "otsu"
    dnbr_fixed_threshold: float = Field(default=0.27, gt=0)
    dnbr_otsu_min: float = Field(default=0.10, gt=0)
    dnbr_otsu_max: float = Field(default=0.44, gt=0)
    min_pre_ndvi: float = Field(default=0.15, ge=-1, le=1)
    min_patch_ha: float = Field(default=1.0, ge=0)
    # Burned patches must lie within this distance of a thermal detection.
    seed_distance_m: float = Field(default=750.0, ge=0)
    inference_url: str | None = None
    inference_timeout_s: float = Field(default=600.0, gt=0)
    model_threshold: float = Field(default=0.5, gt=0, lt=1)
    model_resolution_m: float = Field(default=30.0, gt=0)

    _csv = field_validator("methods", mode="before")(_split_csv)

    @field_validator("methods")
    @classmethod
    def _known_methods(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - {"dnbr", "model"})
        if unknown:
            raise ValueError(f"unknown segmentation methods: {unknown}")
        return value


class ApiSettings(_Section):
    model_config = SettingsConfigDict(
        env_prefix="NARSAT_API_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    jwt_secret: SecretStr = SecretStr(DEV_JWT_SECRET)
    jwt_ttl_minutes: int = Field(default=60, ge=1)
    cors_origins: CsvList = ["http://localhost:5173", "http://localhost:8080"]
    # Public URL of the TiTiler service as seen from the browser.
    titiler_public_url: str = "http://localhost:8001"
    max_page_size: int = Field(default=1000, ge=1)

    _csv = field_validator("cors_origins", mode="before")(_split_csv)


class Settings(_Section):
    model_config = SettingsConfigDict(
        env_prefix="NARSAT_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = False
    metrics_port: int = Field(default=0, ge=0, le=65535)  # 0 disables the metrics endpoint

    aoi: AoiSettings = Field(default_factory=AoiSettings)
    firms: FirmsSettings = Field(default_factory=FirmsSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    db: DbSettings = Field(default_factory=DbSettings)
    lake: LakeSettings = Field(default_factory=LakeSettings)
    cluster: ClusteringSettings = Field(default_factory=ClusteringSettings)
    imagery: ImagerySettings = Field(default_factory=ImagerySettings)
    seg: SegmentationSettings = Field(default_factory=SegmentationSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)

    @model_validator(mode="after")
    def _no_dev_secret_in_prod(self) -> Settings:
        if self.env == "prod" and self.api.jwt_secret.get_secret_value() == DEV_JWT_SECRET:
            raise ValueError("NARSAT_API_JWT_SECRET must be set to a private value when ENV=prod")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read once."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings (tests change the environment between cases)."""
    get_settings.cache_clear()

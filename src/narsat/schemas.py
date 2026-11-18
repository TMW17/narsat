"""Message contracts for the Kafka topics.

These pydantic models are the single definition of what a hotspot or fire-event
message looks like. The poller serialises them, the stream consumer validates against
them, and ``narsat schemas export`` writes the matching JSON Schema files so that the
contract is reviewable in pull requests.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

ConfidenceLevel = Literal["low", "nominal", "high"]
Instrument = Literal["VIIRS", "MODIS"]

#: Bump the Literal in the model together with these when a contract changes.
HOTSPOT_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1


class HotspotMessage(BaseModel):
    """One satellite thermal detection, as published on ``firms.hotspots.raw``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    hotspot_id: uuid.UUID
    source: str = Field(min_length=3, max_length=40, description="FIRMS source name")
    satellite: str = Field(min_length=1, max_length=20)
    instrument: Instrument
    acq_ts: AwareDatetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    brightness_k: float | None = Field(default=None, ge=0, description="I-4 / band 21-22 [K]")
    brightness_2_k: float | None = Field(default=None, ge=0, description="I-5 / band 31 [K]")
    scan_km: float | None = Field(default=None, gt=0)
    track_km: float | None = Field(default=None, gt=0)
    frp_mw: float | None = Field(default=None, ge=0, description="Fire radiative power [MW]")
    confidence_raw: str = Field(max_length=10)
    confidence_level: ConfidenceLevel
    daynight: Literal["D", "N"] | None = None
    version: str | None = Field(default=None, max_length=20)
    # Only standard-processing files carry it: 0 vegetation fire, 1 volcano,
    # 2 other static land source, 3 offshore.
    type_code: int | None = Field(default=None, ge=0, le=3)
    fetched_at: AwareDatetime

    @field_validator("acq_ts", "fetched_at")
    @classmethod
    def _to_utc(cls, value: dt.datetime) -> dt.datetime:
        return value.astimezone(dt.UTC)


class FireEventMessage(BaseModel):
    """State of a fire event, published on the compacted ``narsat.fire-events`` topic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    event_id: uuid.UUID
    change: Literal["created", "updated", "merged", "dissolved"]
    status: str
    mapping_status: str
    first_seen: AwareDatetime
    last_seen: AwareDatetime
    n_hotspots: int = Field(ge=0)
    frp_max_mw: float | None = None
    centroid_lon: float | None = None
    centroid_lat: float | None = None
    bbox: tuple[float, float, float, float] | None = None
    country_iso3: str | None = None
    merged_into: uuid.UUID | None = None
    emitted_at: AwareDatetime

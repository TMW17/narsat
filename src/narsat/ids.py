"""Deterministic identifiers.

Every entity gets a UUIDv5 derived from its natural key. Re-ingesting the same FIRMS
row, re-clustering the same hotspots or re-mapping the same event therefore produces
the same identifiers, which is what makes every write in the pipeline an idempotent
upsert.
"""

from __future__ import annotations

import datetime as dt
import uuid

# Fixed namespaces. Changing one of these changes every identifier of that kind.
_NS_HOTSPOT = uuid.UUID("6b1f3c1e-5d0b-5c8e-9f0e-2a4a8c1d7e10")
_NS_EVENT = uuid.UUID("c2a0f6a4-91c5-5b6f-8a53-0e7b6f2d4c21")
_NS_BURNED_AREA = uuid.UUID("f04c7d2b-3a6e-5d19-b1c8-9e5a2b7c6d32")
_NS_ASSET = uuid.UUID("0d9e8f7a-6b5c-5d4e-8f3a-2b1c0d9e8f43")


def hotspot_id(
    instrument: str,
    satellite: str,
    acq_ts: dt.datetime,
    latitude: float,
    longitude: float,
) -> uuid.UUID:
    """Identity of one thermal detection.

    FIRMS publishes coordinates with five decimals and acquisition time to the minute.
    The processing level (NRT, RT, SP) is deliberately left out so that the same
    detection delivered twice at different levels collapses to one row.
    """
    if acq_ts.tzinfo is None:
        raise ValueError("acq_ts must be timezone-aware")
    stamp = acq_ts.astimezone(dt.UTC).strftime("%Y%m%dT%H%M")
    key = f"{instrument.upper()}|{satellite.upper()}|{stamp}|{latitude:.5f}|{longitude:.5f}"
    return uuid.uuid5(_NS_HOTSPOT, key)


def event_id(seed_hotspot_id: uuid.UUID) -> uuid.UUID:
    """Identity of a fire event: a function of the hotspot that founded it."""
    return uuid.uuid5(_NS_EVENT, str(seed_hotspot_id))


def burned_area_id(event: uuid.UUID, method: str) -> uuid.UUID:
    return uuid.uuid5(_NS_BURNED_AREA, f"{event}|{method}")


def asset_id(event: uuid.UUID, kind: str, method: str | None = None) -> uuid.UUID:
    return uuid.uuid5(_NS_ASSET, f"{event}|{kind}|{method or ''}")

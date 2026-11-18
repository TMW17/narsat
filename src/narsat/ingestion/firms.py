"""NASA FIRMS client and CSV parser.

API reference: https://firms.modaps.eosdis.nasa.gov/api/area/

* ``/api/area/csv/[MAP_KEY]/[SOURCE]/[west,south,east,north]/[DAY_RANGE]/[DATE]``
* ``DAY_RANGE`` is 1..5; ``DATE`` (YYYY-MM-DD) is the first day of the range and is
  optional (omitted means "the most recent days").
* The free MAP_KEY allows 5,000 transactions per 10 minutes.

The key travels in the URL path, so no URL built here is ever logged or put in an
exception message.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx
from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from narsat import ids
from narsat.config import BoundingBox
from narsat.log import redact
from narsat.schemas import ConfidenceLevel, HotspotMessage, Instrument

REQUIRED_COLUMNS = {"latitude", "longitude", "acq_date", "acq_time", "confidence"}


class FirmsError(RuntimeError):
    """FIRMS refused the request (bad key, bad parameters, quota) or sent garbage."""


class FirmsTransientError(FirmsError):
    """Worth retrying: timeouts, connection resets, 429 and 5xx responses."""


@dataclass(frozen=True)
class RejectedRow:
    line_number: int
    reason: str
    row: dict[str, str]


@dataclass
class ParseResult:
    hotspots: list[HotspotMessage] = field(default_factory=list)
    rejected: list[RejectedRow] = field(default_factory=list)


class FirmsClient:
    """Thin, retrying wrapper around the three FIRMS endpoints the project uses."""

    def __init__(
        self,
        map_key: str,
        base_url: str = "https://firms.modaps.eosdis.nasa.gov",
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not map_key:
            raise FirmsError("a FIRMS MAP_KEY is required (set NARSAT_FIRMS_MAP_KEY)")
        self._key = map_key
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
            headers={"User-Agent": "narsat-firms-poller/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FirmsClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception_type(FirmsTransientError),
        wait=wait_exponential_jitter(multiplier=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            response = self._client.get(path, params=params)
        except httpx.TransportError as exc:
            # str(exc) may quote the URL, which contains the key.
            raise FirmsTransientError(f"network error: {redact(str(exc))}") from None
        if response.status_code == 429 or response.status_code >= 500:
            raise FirmsTransientError(f"FIRMS answered HTTP {response.status_code}")
        if response.status_code != 200:
            raise FirmsError(
                f"FIRMS answered HTTP {response.status_code}: {redact(response.text[:200])}"
            )
        return response

    def area_csv(
        self,
        source: str,
        bbox: BoundingBox,
        day_range: int = 1,
        date: dt.date | None = None,
    ) -> str:
        """Return the raw CSV for one source over ``bbox``.

        FIRMS reports most errors with HTTP 200 and a one-line text body
        ("Invalid MAP_KEY.", "Invalid day range..."), so the body is checked too.
        """
        if not 1 <= day_range <= 5:
            raise FirmsError("day_range must be between 1 and 5")
        path = f"/api/area/csv/{self._key}/{source}/{bbox.as_firms()}/{day_range}"
        if date is not None:
            path += f"/{date.isoformat()}"
        text = self._get(path).text
        first_line = text.lstrip().split("\n", 1)[0].lower()
        if text.strip() and "latitude" not in first_line:
            raise FirmsError(f"FIRMS error for {source}: {redact(first_line[:200])}")
        return text

    def key_status(self) -> dict[str, Any]:
        """Quota information for the configured key."""
        response = self._get("/mapserver/mapkey_status/", params={"MAP_KEY": self._key})
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            raise FirmsError(
                f"unexpected key-status reply: {redact(response.text[:200])}"
            ) from None
        return payload

    def data_availability(self, sensor: str = "ALL") -> list[dict[str, str]]:
        """Date range currently served for each source (``data_id,min_date,max_date``)."""
        text = self._get(f"/api/data_availability/csv/{self._key}/{sensor}").text
        if "data_id" not in text.split("\n", 1)[0]:
            raise FirmsError(f"unexpected data-availability reply: {redact(text[:200])}")
        return list(csv.DictReader(io.StringIO(text)))


def _confidence(instrument: Instrument, raw: str) -> ConfidenceLevel:
    """Map both confidence encodings onto low / nominal / high.

    VIIRS uses the letters l, n, h. MODIS uses 0-100; the FIRMS documentation
    describes values under 30 as low, 30-80 as nominal and above 80 as high.
    """
    value = raw.strip().lower()
    if instrument == "VIIRS":
        mapping: dict[str, ConfidenceLevel] = {
            "l": "low",
            "low": "low",
            "n": "nominal",
            "nominal": "nominal",
            "h": "high",
            "high": "high",
        }
        if value not in mapping:
            raise ValueError(f"unknown VIIRS confidence {raw!r}")
        return mapping[value]
    score = float(value)
    if not 0 <= score <= 100:
        raise ValueError(f"MODIS confidence out of range: {raw!r}")
    if score < 30:
        return "low"
    return "nominal" if score <= 80 else "high"


def _optional_float(row: dict[str, str], *names: str) -> float | None:
    for name in names:
        value = (row.get(name) or "").strip()
        if value:
            return float(value)
    return None


def _acquisition_time(acq_date: str, acq_time: str) -> dt.datetime:
    """FIRMS gives the time as HHMM without leading zeros (``46`` means 00:46 UTC)."""
    hhmm = acq_time.strip().replace(":", "").zfill(4)
    if len(hhmm) != 4 or not hhmm.isdigit():
        raise ValueError(f"bad acq_time {acq_time!r}")
    day = dt.date.fromisoformat(acq_date.strip())
    return dt.datetime(day.year, day.month, day.day, int(hhmm[:2]), int(hhmm[2:]), tzinfo=dt.UTC)


def _instrument(row: dict[str, str], source: str) -> Instrument:
    declared = (row.get("instrument") or "").strip().upper()
    if declared in ("VIIRS", "MODIS"):
        return declared  # type: ignore[return-value]
    if source.upper().startswith("VIIRS"):
        return "VIIRS"
    if source.upper().startswith("MODIS"):
        return "MODIS"
    raise ValueError(f"cannot tell the instrument for source {source!r}")


def parse_area_csv(text: str, source: str, fetched_at: dt.datetime) -> ParseResult:
    """Turn a FIRMS CSV body into validated hotspot messages.

    VIIRS and MODIS files differ in three columns (``bright_ti4``/``bright_ti5``
    versus ``brightness``/``bright_t31``, and the confidence encoding); both are
    handled here. Rows that cannot be parsed are returned, not dropped silently.
    """
    result = ParseResult()
    if not text.strip():
        return result
    reader = csv.DictReader(io.StringIO(text.lstrip()))
    columns = {name.strip().lower() for name in (reader.fieldnames or [])}
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise FirmsError(f"FIRMS CSV for {source} lacks columns {sorted(missing)}")

    for line_number, raw_row in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "") for k, v in raw_row.items()}
        try:
            instrument = _instrument(row, source)
            acq_ts = _acquisition_time(row["acq_date"], row["acq_time"])
            latitude = float(row["latitude"])
            longitude = float(row["longitude"])
            satellite = row.get("satellite", "").strip() or "unknown"
            type_text = row.get("type", "").strip()
            daynight = row.get("daynight", "").strip().upper() or None
            message = HotspotMessage(
                hotspot_id=ids.hotspot_id(instrument, satellite, acq_ts, latitude, longitude),
                source=source,
                satellite=satellite,
                instrument=instrument,
                acq_ts=acq_ts,
                latitude=latitude,
                longitude=longitude,
                brightness_k=_optional_float(row, "bright_ti4", "brightness"),
                brightness_2_k=_optional_float(row, "bright_ti5", "bright_t31"),
                scan_km=_optional_float(row, "scan"),
                track_km=_optional_float(row, "track"),
                frp_mw=_optional_float(row, "frp"),
                confidence_raw=row["confidence"].strip(),
                confidence_level=_confidence(instrument, row["confidence"]),
                daynight=daynight,
                version=row.get("version", "").strip() or None,
                type_code=int(type_text) if type_text else None,
                fetched_at=fetched_at,
            )
        except (ValueError, KeyError, ValidationError) as exc:
            reason = str(exc).splitlines()[0][:200]
            result.rejected.append(RejectedRow(line_number, reason, row))
            continue
        result.hotspots.append(message)
    return result

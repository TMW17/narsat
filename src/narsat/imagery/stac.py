"""STAC search and scene grouping for Sentinel-2 L2A.

Catalogue: Element 84 Earth Search (``sentinel-2-c1-l2a``), which indexes the public
Sentinel-2 cloud-optimised GeoTIFFs on AWS. No account or key is needed.

A *scene* here is everything one satellite acquired over the area on one overpass:
usually one STAC item, sometimes two or more adjacent MGRS tiles that have to be
mosaicked. Items are grouped by platform, acquisition day and relative orbit.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from narsat.log import get_logger

log = get_logger(__name__)

StacItem = dict[str, Any]
SearchFunction = Callable[[dict[str, Any], dt.datetime, dt.datetime], list[StacItem]]

#: Earth Search asset keys, in the band order the burn-scar model expects
#: (Blue, Green, Red, Narrow NIR, SWIR 1, SWIR 2 = B02, B03, B04, B8A, B11, B12).
REFLECTANCE_ASSETS = ("blue", "green", "red", "nir08", "swir16", "swir22")
SCL_ASSET = "scl"

_RELATIVE_ORBIT = re.compile(r"_R(\d{3})_")


def parse_datetime(item: StacItem) -> dt.datetime:
    text = str(item["properties"]["datetime"]).replace("Z", "+00:00")
    stamp = dt.datetime.fromisoformat(text)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.UTC)


def relative_orbit(item: StacItem) -> int | None:
    """Relative orbit number, read from the product name (``..._R067_...``)."""
    properties = item.get("properties", {})
    if isinstance(properties.get("sat:relative_orbit"), int):
        return int(properties["sat:relative_orbit"])
    match = _RELATIVE_ORBIT.search(str(properties.get("s2:product_uri", "")))
    return int(match.group(1)) if match else None


def reflectance_scaling(item: StacItem, asset_key: str) -> tuple[float, float]:
    """``(scale, offset)`` such that reflectance = DN * scale + offset.

    Since processing baseline 04.00 (January 2022) Sentinel-2 L2A digital numbers
    carry an offset of 1000. The Collection-1 items publish the conversion in the
    STAC raster extension; that is used when present. For items without it, the
    processing baseline and Earth Search's ``boa_offset_applied`` flag decide.
    """
    asset = item["assets"][asset_key]
    for bands_key in ("raster:bands", "bands"):
        bands = asset.get(bands_key) or []
        if bands and isinstance(bands[0], dict):
            band = bands[0]
            scale = band.get("scale", band.get("raster:scale"))
            if scale is not None:
                offset = band.get("offset", band.get("raster:offset"))
                return float(scale), float(offset or 0.0)
    properties = item.get("properties", {})
    try:
        baseline = float(properties.get("s2:processing_baseline") or 0.0)
    except ValueError:
        baseline = 0.0
    applied = bool(properties.get("earthsearch:boa_offset_applied", False))
    return 1e-4, (-0.1 if baseline >= 4.0 and not applied else 0.0)


@dataclass(frozen=True)
class Scene:
    key: str
    datetime: dt.datetime
    platform: str
    relative_orbit: int | None
    cloud_cover: float
    items: tuple[StacItem, ...]

    @property
    def item_ids(self) -> list[str]:
        return [str(item["id"]) for item in self.items]


def group_scenes(items: list[StacItem]) -> list[Scene]:
    """Group items into scenes, newest first, dropping duplicate deliveries of a tile."""
    groups: dict[tuple[str, dt.date, int | None], dict[str, StacItem]] = defaultdict(dict)
    for item in items:
        properties = item.get("properties", {})
        when = parse_datetime(item)
        key = (str(properties.get("platform", "sentinel-2")), when.date(), relative_orbit(item))
        tile = str(properties.get("grid:code") or properties.get("s2:tile_id") or item["id"])
        current = groups[key].get(tile)
        # The same tile can be delivered more than once; keep the most recently generated.
        stamp = str(properties.get("s2:generation_time") or properties.get("created") or "")
        if current is None or stamp > str(
            current["properties"].get("s2:generation_time")
            or current["properties"].get("created")
            or ""
        ):
            groups[key][tile] = item

    scenes: list[Scene] = []
    for (platform, day, orbit), tiles in groups.items():
        members = tuple(sorted(tiles.values(), key=lambda it: str(it["id"])))
        clouds = [float(m["properties"].get("eo:cloud_cover", 100.0)) for m in members]
        scenes.append(
            Scene(
                key=f"{platform}_{day:%Y%m%d}_R{orbit if orbit is not None else 'NA'}",
                datetime=min(parse_datetime(m) for m in members),
                platform=platform,
                relative_orbit=orbit,
                cloud_cover=sum(clouds) / len(clouds),
                items=members,
            )
        )
    return sorted(scenes, key=lambda s: s.datetime, reverse=True)


#: Connect and read timeouts (seconds) for catalogue requests; without them a stalled
#: connection would hang the mapper forever.
STAC_TIMEOUT = (10.0, 60.0)


def stac_io() -> Any:
    """HTTP layer for pystac-client with patient retries.

    pystac-client retries five times by default but immediately, so a DNS lookup that
    fails for a few seconds fails every attempt. Here the five attempts back off
    (1, 2, 4, 8 s), status 429/5xx answers are retried too, and the POST search is
    allowed to repeat because it does not change anything on the server.
    """
    from pystac_client.stac_api_io import StacApiIO
    from urllib3.util.retry import Retry

    retry = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=None,
        raise_on_status=False,
    )
    return StacApiIO(timeout=STAC_TIMEOUT, max_retries=retry)


def earth_search(stac_url: str, collection: str, max_cloud_pct: float) -> SearchFunction:
    """Build the search function used in production (imports pystac-client lazily)."""

    def search(geometry: dict[str, Any], start: dt.datetime, end: dt.datetime) -> list[StacItem]:
        from pystac_client import Client

        client = Client.open(stac_url, stac_io=stac_io())
        result = client.search(
            collections=[collection],
            intersects=geometry,
            datetime=f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
            query={"eo:cloud_cover": {"lt": max_cloud_pct}},
            max_items=200,
        )
        items = [item.to_dict() for item in result.items()]
        log.info(
            "stac_search",
            collection=collection,
            start=start.date().isoformat(),
            end=end.date().isoformat(),
            items=len(items),
        )
        return items

    return search

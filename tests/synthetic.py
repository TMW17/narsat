"""Synthetic Sentinel-2-like scenes for tests.

These are *test fixtures*, not data: small GeoTIFFs written to a temporary directory
with a known burned patch, so that the imagery, segmentation and mapping code can be
checked against an answer that is known exactly. Nothing here is ever loaded by the
application.

Layout mimics Earth Search ``sentinel-2-c1-l2a`` items: one single-band file per
asset, digital numbers with scale 0.0001 and offset -0.1 declared in ``raster:bands``,
10 m visible bands and 20 m NIR/SWIR/SCL bands, nodata 0.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from affine import Affine

from narsat.imagery import indices
from narsat.imagery.stac import REFLECTANCE_ASSETS

EPSG = 32631  # UTM 31N: covers 0-6 degrees east (Kabylie)
ORIGIN_X, ORIGIN_Y = 640_000.0, 4_070_000.0  # roughly 4.57 E, 36.76 N
SIZE_M = 6_000.0

# Typical surface reflectances (blue, green, red, nir08, swir16, swir22).
VEGETATION = (0.03, 0.06, 0.05, 0.35, 0.18, 0.08)
BURNED = (0.04, 0.05, 0.06, 0.10, 0.22, 0.20)
BARE = (0.12, 0.16, 0.22, 0.26, 0.34, 0.30)  # NDVI about 0.08
RESOLUTION = {
    "blue": 10.0,
    "green": 10.0,
    "red": 10.0,
    "nir08": 20.0,
    "swir16": 20.0,
    "swir22": 20.0,
    "scl": 20.0,
}


@dataclass(frozen=True)
class Truth:
    """What the synthetic scene contains, on the 20 m grid."""

    burned: np.ndarray  # bool
    cloud: np.ndarray  # bool (post-fire scene only)
    bare: np.ndarray  # bool
    centre_lonlat: tuple[float, float]


def _disc(shape: tuple[int, int], row: float, col: float, radius: float) -> np.ndarray:
    rows, cols = np.indices(shape)
    return (rows - row) ** 2 + (cols - col) ** 2 <= radius**2


def truth_20m() -> Truth:
    from pyproj import Transformer

    n = int(SIZE_M / 20.0)
    burned = _disc((n, n), 150, 150, 45)  # radius 45 px = 900 m, about 254 ha
    cloud = _disc((n, n), 40, 250, 25)
    bare = np.zeros((n, n), dtype=bool)
    bare[230:290, 20:120] = True
    x = ORIGIN_X + 150 * 20.0 + 10.0
    y = ORIGIN_Y - 150 * 20.0 - 10.0
    lon, lat = Transformer.from_crs(EPSG, 4326, always_xy=True).transform(x, y)
    return Truth(burned, cloud, bare, (float(lon), float(lat)))


def _upsample(mask: np.ndarray, factor: int) -> np.ndarray:
    return np.repeat(np.repeat(mask, factor, axis=0), factor, axis=1)


def _write(path: Path, data: np.ndarray, resolution: float, dtype: str) -> None:
    profile = {
        "driver": "GTiff",
        "dtype": dtype,
        "count": 1,
        "height": data.shape[0],
        "width": data.shape[1],
        "crs": f"EPSG:{EPSG}",
        "transform": Affine(resolution, 0, ORIGIN_X, 0, -resolution, ORIGIN_Y),
        "nodata": 0,
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
        "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dataset:
        dataset.write(data.astype(dtype), 1)


def write_scene(
    directory: Path,
    item_id: str,
    when: dt.datetime,
    *,
    burned: bool,
    cloudy: bool = False,
    cloud_cover: float = 5.0,
    relative_orbit: int = 108,
    noise_seed: int = 0,
) -> dict[str, Any]:
    """Write one scene and return its STAC item (hrefs are local paths)."""
    truth = truth_20m()
    rng = np.random.default_rng(noise_seed)
    directory.mkdir(parents=True, exist_ok=True)
    assets: dict[str, Any] = {}
    for index, key in enumerate(REFLECTANCE_ASSETS):
        factor = int(20.0 / RESOLUTION[key])
        shape = (truth.burned.shape[0] * factor, truth.burned.shape[1] * factor)
        reflectance = np.full(shape, VEGETATION[index], dtype="float64")
        reflectance[_upsample(truth.bare, factor)] = BARE[index]
        if burned:
            reflectance[_upsample(truth.burned, factor)] = BURNED[index]
        reflectance += rng.normal(0.0, 0.004, shape)
        if cloudy:
            reflectance[_upsample(truth.cloud, factor)] = 0.6
        # Collection-1 encoding: DN = (reflectance + 0.1) / 0.0001, 0 reserved for nodata.
        digital = np.clip(np.round((reflectance + 0.1) / 1e-4), 1, 65535)
        path = directory / f"{item_id}_{key}.tif"
        _write(path, digital, RESOLUTION[key], "uint16")
        assets[key] = {
            "href": str(path),
            "type": "image/tiff; application=geotiff; profile=cloud-optimized",
            "raster:bands": [
                {
                    "nodata": 0,
                    "data_type": "uint16",
                    "scale": 0.0001,
                    "offset": -0.1,
                    "spatial_resolution": RESOLUTION[key],
                }
            ],
        }
    scl = np.full(truth.burned.shape, indices.SCL_VEGETATION, dtype="uint8")
    scl[truth.bare] = indices.SCL_BARE
    if burned:
        # Sen2Cor frequently labels fresh burn scars as "dark area".
        scl[truth.burned] = indices.SCL_DARK
    if cloudy:
        scl[truth.cloud] = indices.SCL_CLOUD_HIGH
    scl_path = directory / f"{item_id}_scl.tif"
    _write(scl_path, scl, 20.0, "uint8")
    assets["scl"] = {"href": str(scl_path), "type": "image/tiff"}
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": item_id,
        "properties": {
            "datetime": when.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
            "platform": "sentinel-2a",
            "eo:cloud_cover": cloud_cover,
            "grid:code": "MGRS-31SEA",
            "s2:processing_baseline": "05.09",
            "s2:product_uri": f"S2A_MSIL2A_{when:%Y%m%dT%H%M%S}_N0509_R{relative_orbit:03d}"
            f"_T31SEA_{when:%Y%m%dT%H%M%S}.SAFE",
            "s2:generation_time": when.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
        },
        "assets": assets,
    }


def fake_search(items: list[dict[str, Any]]) -> Any:
    """A search function over a fixed list of items (filters on the date range only)."""
    from narsat.imagery.stac import parse_datetime

    def search(_geometry: dict[str, Any], start: dt.datetime, end: dt.datetime) -> list[Any]:
        return [item for item in items if start <= parse_datetime(item) <= end]

    return search

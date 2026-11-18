"""Windowed reads of Sentinel-2 COGs onto a common analysis grid.

Only the pixels over the fire are transferred: GDAL issues HTTP range requests for
the blocks that intersect the target grid, so mapping a 20 km fire costs a few
megabytes instead of the ~800 MB of a full granule.

The analysis grid is in the UTM zone of the area of interest, at 20 m (the native
resolution of the red-edge and SWIR bands used for burn mapping). Every band of
every tile is warped onto that grid, then tiles are mosaicked by "first valid pixel".
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from math import ceil, floor

import numpy as np
import rasterio
import shapely
from affine import Affine
from rasterio.enums import Resampling
from rasterio.errors import RasterioIOError
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject
from shapely.geometry.base import BaseGeometry
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from narsat.geo import reproject as reproject_geometry
from narsat.geo import utm_epsg_for
from narsat.imagery.stac import (
    REFLECTANCE_ASSETS,
    SCL_ASSET,
    Scene,
    StacItem,
    reflectance_scaling,
)
from narsat.log import get_logger

#: GDAL settings for efficient remote COG access (range requests, no directory listing).
log = get_logger(__name__)

GDAL_REMOTE_OPTIONS = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF,.tiff",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "VSI_CACHE": "TRUE",
    "AWS_NO_SIGN_REQUEST": "YES",
}


class GridTooLargeError(ValueError):
    """The area of interest would need more pixels than the configured maximum."""


@dataclass(frozen=True)
class Grid:
    """A north-up raster grid."""

    epsg: int
    transform: Affine
    width: int
    height: int

    @property
    def resolution(self) -> float:
        return float(self.transform.a)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        left, top = self.transform.c, self.transform.f
        return (left, top - self.height * self.resolution, left + self.width * self.resolution, top)

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)

    def footprint_4326(self) -> BaseGeometry:
        """The grid's rectangle in longitude/latitude.

        The edges are densified before reprojection: a straight edge in UTM is slightly
        curved in geographic coordinates, and a four-corner polygon would cut it short.
        """
        left, bottom, right, top = self.bounds
        rectangle = shapely.segmentize(shapely.box(left, bottom, right, top), self.resolution * 16)
        return reproject_geometry(rectangle, self.epsg, 4326)

    @classmethod
    def for_geometry(
        cls, geometry_4326: BaseGeometry, resolution_m: float, max_pixels: int
    ) -> Grid:
        """Smallest grid, aligned to multiples of the resolution, covering the geometry."""
        centroid = geometry_4326.centroid
        epsg = utm_epsg_for(centroid.x, centroid.y)
        minx, miny, maxx, maxy = reproject_geometry(geometry_4326, 4326, epsg).bounds
        left = floor(minx / resolution_m) * resolution_m
        top = ceil(maxy / resolution_m) * resolution_m
        width = max(1, ceil((maxx - left) / resolution_m))
        height = max(1, ceil((top - miny) / resolution_m))
        if width * height > max_pixels:
            raise GridTooLargeError(
                f"area needs {width}x{height} pixels at {resolution_m:g} m, above the limit of "
                f"{max_pixels}"
            )
        return cls(epsg, Affine(resolution_m, 0.0, left, 0.0, -resolution_m, top), width, height)

    def with_resolution(self, resolution_m: float) -> Grid:
        """Same footprint at another pixel size (used to feed the 30 m model)."""
        left, bottom, right, top = self.bounds
        width = max(1, ceil((right - left) / resolution_m))
        height = max(1, ceil((top - bottom) / resolution_m))
        return Grid(
            self.epsg, Affine(resolution_m, 0.0, left, 0.0, -resolution_m, top), width, height
        )


@dataclass(frozen=True)
class SceneArrays:
    """Surface reflectance (0-1, NaN where missing) and the scene classification layer."""

    reflectance: np.ndarray  # float32, shape (6, height, width), order of REFLECTANCE_ASSETS
    scl: np.ndarray  # uint8, shape (height, width), 0 = no data


#: Network failures worth another attempt. GDAL already retries HTTP 429 and 5xx answers
#: itself (GDAL_HTTP_MAX_RETRY), but not failures before any answer: a DNS lookup that
#: fails, a refused or reset connection, a timeout. A missing file (HTTP 404) or a
#: corrupt one is not retried.
_TRANSIENT = re.compile(
    r"could(?: not|n't) resolve|timed out|timeout was reached|connection (?:refused|reset)"
    r"|failed to connect|recv failure|send failure|ssl|http response code: (?:429|5\d\d)",
    re.IGNORECASE,
)
READ_ATTEMPTS = 4


def is_transient(exc: BaseException) -> bool:
    """A network failure worth another attempt.

    rasterio reports a failed block read as "Read failed. See previous exception for
    details." and chains GDAL's own error, which names the cause, so the chain is searched.
    """
    if not isinstance(exc, RasterioIOError):
        return False
    seen: BaseException | None = exc
    for _ in range(5):
        if seen is None:
            return False
        if _TRANSIENT.search(str(seen)):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _log_retry(state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome else None
    log.warning(
        "imagery_read_retry",
        attempt=state.attempt_number,
        error=str(exc)[:200] if exc else None,
    )


@retry(
    retry=retry_if_exception(is_transient),
    stop=stop_after_attempt(READ_ATTEMPTS),
    wait=wait_exponential_jitter(multiplier=2, max=30),
    before_sleep=_log_retry,
    reraise=True,
)
def _read_asset(href: str, grid: Grid, categorical: bool) -> np.ndarray:
    """Warp one single-band asset onto the grid; nodata (0) is preserved as 0.

    Transient network failures are retried (four attempts, exponential back-off).
    """
    with rasterio.Env(**GDAL_REMOTE_OPTIONS), rasterio.open(href) as source:
        if categorical:
            resampling = Resampling.nearest
        else:
            # Aggregating 10 m bands to 20 m must average, not pick a neighbour.
            finer = abs(source.transform.a) < grid.resolution * 0.99
            resampling = Resampling.average if finer else Resampling.bilinear
        with WarpedVRT(
            source,
            crs=f"EPSG:{grid.epsg}",
            transform=grid.transform,
            width=grid.width,
            height=grid.height,
            resampling=resampling,
            src_nodata=0,
            nodata=0,
        ) as vrt:
            data: np.ndarray = vrt.read(1)
    return data


AssetReader = Callable[[str, Grid, bool], np.ndarray]


def read_scl(scene: Scene, grid: Grid, read_asset: AssetReader = _read_asset) -> np.ndarray:
    """Scene classification over the grid (cheap: one 20 m 8-bit band per tile)."""
    scl = np.zeros(grid.shape, dtype="uint8")
    for item in scene.items:
        tile = read_asset(item["assets"][SCL_ASSET]["href"], grid, True).astype("uint8")
        scl = np.where(scl == 0, tile, scl)
    return scl


def _read_item_band(
    item: StacItem, asset_key: str, grid: Grid, read_asset: AssetReader
) -> np.ndarray:
    raw = read_asset(item["assets"][asset_key]["href"], grid, False)
    scale, offset = reflectance_scaling(item, asset_key)
    reflectance = raw.astype("float32") * np.float32(scale) + np.float32(offset)
    reflectance[raw == 0] = np.nan
    return reflectance


def read_scene(
    scene: Scene,
    grid: Grid,
    threads: int = 6,
    read_asset: AssetReader = _read_asset,
) -> SceneArrays:
    """Read the six reflectance bands and SCL of a scene onto ``grid``."""
    jobs = [(item, key) for item in scene.items for key in REFLECTANCE_ASSETS]
    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        bands = list(pool.map(lambda job: _read_item_band(job[0], job[1], grid, read_asset), jobs))

    reflectance = np.full((len(REFLECTANCE_ASSETS), *grid.shape), np.nan, dtype="float32")
    for (_item, key), band in zip(jobs, bands, strict=True):
        index = REFLECTANCE_ASSETS.index(key)
        target = reflectance[index]
        missing = np.isnan(target)
        target[missing] = band[missing]
    return SceneArrays(reflectance=reflectance, scl=read_scl(scene, grid, read_asset))


def resample(data: np.ndarray, source: Grid, target: Grid, resampling: Resampling) -> np.ndarray:
    """Resample a (bands, h, w) or (h, w) float array between grids of the same CRS."""
    squeeze = data.ndim == 2
    stack = data[np.newaxis] if squeeze else data
    out = np.full((stack.shape[0], *target.shape), np.nan, dtype="float32")
    reproject(
        source=stack.astype("float32"),
        destination=out,
        src_transform=source.transform,
        src_crs=f"EPSG:{source.epsg}",
        dst_transform=target.transform,
        dst_crs=f"EPSG:{target.epsg}",
        src_nodata=np.nan,
        dst_nodata=np.nan,
        resampling=resampling,
    )
    return out[0] if squeeze else out

"""Cloud-optimised GeoTIFF output.

Rasters are written locally with GDAL's COG driver (internal tiling, overviews,
DEFLATE) and then uploaded to the lake, so they can be served tile by tile through
HTTP range requests (TiTiler, QGIS, ``rasterio``) without a raster server holding state.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio

from narsat.imagery.reader import Grid

REFLECTANCE_NODATA = -32768
REFLECTANCE_SCALE = 10_000.0


def write_cog(
    path: Path,
    data: np.ndarray,
    grid: Grid,
    nodata: float | int | None,
    band_names: list[str],
    overview_resampling: str = "average",
    scale: float | None = None,
) -> Path:
    """Write a ``(bands, height, width)`` array as a COG."""
    if data.ndim == 2:
        data = data[np.newaxis]
    if data.shape[1:] != grid.shape or data.shape[0] != len(band_names):
        raise ValueError(
            f"array shape {data.shape} does not match grid {grid.shape} / {len(band_names)} bands"
        )
    profile = {
        "driver": "COG",
        "dtype": data.dtype.name,
        "count": data.shape[0],
        "height": grid.height,
        "width": grid.width,
        "crs": f"EPSG:{grid.epsg}",
        "transform": grid.transform,
        "nodata": nodata,
        "compress": "DEFLATE",
        "predictor": 3 if data.dtype.kind == "f" else 2,
        "blocksize": 512,
        "overview_resampling": overview_resampling,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dataset:
        dataset.write(data)
        for index, name in enumerate(band_names, start=1):
            dataset.set_band_description(index, name)
        if scale is not None:
            dataset.scales = tuple([scale] * data.shape[0])
    return path


def encode_reflectance(reflectance: np.ndarray) -> np.ndarray:
    """Reflectance (float, NaN = missing) to int16 scaled by 10,000."""
    scaled = np.round(reflectance * REFLECTANCE_SCALE)
    scaled = np.clip(scaled, REFLECTANCE_NODATA + 1, 32767)
    return np.where(np.isfinite(reflectance), scaled, REFLECTANCE_NODATA).astype("int16")

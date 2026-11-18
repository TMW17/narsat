"""Raster masks to polygons."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import shapely
from rasterio import features
from shapely.geometry import MultiPolygon, shape
from shapely.geometry.base import BaseGeometry

from narsat.geo import as_multipolygon, geodesic_area_ha, project_points, reproject
from narsat.imagery.reader import Grid


@dataclass(frozen=True)
class Polygonised:
    geometry: MultiPolygon  # EPSG:4326
    area_ha: float
    n_polygons: int


def polygonise(mask: np.ndarray, grid: Grid, simplify: bool = True) -> Polygonised:
    """Outline the ``True`` pixels of a mask as a MultiPolygon in EPSG:4326.

    Pixels touching only by a corner belong to different polygons (4-connectivity),
    which keeps every polygon valid. Simplification removes the staircase of pixel
    edges with a tolerance of half a pixel, so the area changes by a negligible amount;
    the reported area is geodesic and computed on the final geometry.
    """
    if mask.shape != grid.shape:
        raise ValueError(f"mask shape {mask.shape} does not match grid {grid.shape}")
    binary = np.ascontiguousarray(mask, dtype="uint8")
    if not binary.any():
        return Polygonised(MultiPolygon(), 0.0, 0)
    parts: list[BaseGeometry] = [
        shape(geometry)
        for geometry, value in features.shapes(
            binary, mask=binary.astype(bool), connectivity=4, transform=grid.transform
        )
        if value == 1
    ]
    merged: BaseGeometry = shapely.union_all(parts)
    if simplify:
        merged = merged.simplify(grid.resolution / 2.0, preserve_topology=True)
    result = as_multipolygon(reproject(merged, grid.epsg, 4326))
    return Polygonised(result, geodesic_area_ha(result), len(result.geoms))


def rasterise(geometry_4326: BaseGeometry, grid: Grid) -> np.ndarray:
    """Boolean mask of the pixels whose centre falls inside the geometry."""
    projected = reproject(geometry_4326, 4326, grid.epsg)
    if projected.is_empty:
        return np.zeros(grid.shape, dtype=bool)
    burned: np.ndarray = features.rasterize(
        [(projected, 1)], out_shape=grid.shape, transform=grid.transform, fill=0, dtype="uint8"
    )
    return burned.astype(bool)


def points_to_pixels(
    lons: np.ndarray, lats: np.ndarray, grid: Grid
) -> tuple[np.ndarray, np.ndarray]:
    """Row/column indices of the points that fall inside the grid."""
    xs, ys = project_points(lons, lats, grid.epsg)
    cols = np.floor((xs - grid.transform.c) / grid.resolution).astype("int64")
    rows = np.floor((grid.transform.f - ys) / grid.resolution).astype("int64")
    inside = (rows >= 0) & (rows < grid.height) & (cols >= 0) & (cols < grid.width)
    return rows[inside], cols[inside]

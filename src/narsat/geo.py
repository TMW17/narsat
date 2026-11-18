"""Small geodesy helpers shared by clustering, imagery and vectorisation.

Distances and buffers are computed in the local UTM zone of the geometry; areas are
geodesic (WGS84 ellipsoid), so they do not depend on the choice of projection.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache

import numpy as np
import shapely
from pyproj import CRS, Geod, Transformer
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

WGS84 = CRS.from_epsg(4326)
_GEOD = Geod(ellps="WGS84")
EARTH_RADIUS_KM = 6371.0088


def utm_epsg_for(lon: float, lat: float) -> int:
    """EPSG code of the WGS84 / UTM zone that contains the point."""
    zone = int((lon + 180.0) // 6.0) % 60 + 1
    return (32600 if lat >= 0 else 32700) + zone


@lru_cache(maxsize=128)
def _transformer(src_epsg: int, dst_epsg: int) -> Transformer:
    return Transformer.from_crs(src_epsg, dst_epsg, always_xy=True)


def reproject(geom: BaseGeometry, src_epsg: int, dst_epsg: int) -> BaseGeometry:
    if src_epsg == dst_epsg:
        return geom
    # interleaved=False calls the function with separate x and y arrays, which is
    # pyproj's signature; the type stubs only describe the interleaved form.
    return shapely.transform(
        geom,
        _transformer(src_epsg, dst_epsg).transform,  # type: ignore[arg-type]
        interleaved=False,
    )


def project_points(
    lons: Sequence[float] | np.ndarray, lats: Sequence[float] | np.ndarray, dst_epsg: int
) -> tuple[np.ndarray, np.ndarray]:
    """Project EPSG:4326 coordinates to ``dst_epsg``; returns ``(x, y)`` arrays."""
    xs, ys = _transformer(4326, dst_epsg).transform(
        np.asarray(lons, dtype="float64"), np.asarray(lats, dtype="float64")
    )
    return np.asarray(xs), np.asarray(ys)


def hull_of_points(
    lons: Sequence[float] | np.ndarray,
    lats: Sequence[float] | np.ndarray,
    buffer_m: float,
) -> Polygon:
    """Convex hull, in EPSG:4326, of detections buffered by their nominal footprint.

    A single detection yields a (roughly circular) polygon, never a degenerate point,
    so the result is always usable as an imagery area of interest.
    """
    lon_arr = np.asarray(lons, dtype="float64")
    lat_arr = np.asarray(lats, dtype="float64")
    if lon_arr.size == 0:
        raise ValueError("at least one point is required")
    epsg = utm_epsg_for(float(lon_arr.mean()), float(lat_arr.mean()))
    xs, ys = project_points(lon_arr, lat_arr, epsg)
    points = shapely.points(np.column_stack([xs, ys]))
    hull = shapely.convex_hull(shapely.union_all(shapely.buffer(points, buffer_m, quad_segs=6)))
    result = reproject(hull, epsg, 4326)
    if not isinstance(result, Polygon):  # pragma: no cover - buffered hulls are polygons
        raise TypeError(f"unexpected hull geometry type {result.geom_type}")
    return result


def buffer_metres(geom: BaseGeometry, distance_m: float) -> BaseGeometry:
    """Buffer an EPSG:4326 geometry by a distance in metres."""
    centroid = geom.centroid
    epsg = utm_epsg_for(centroid.x, centroid.y)
    return reproject(reproject(geom, 4326, epsg).buffer(distance_m), epsg, 4326)


def geodesic_area_ha(geom: BaseGeometry) -> float:
    """Area of an EPSG:4326 polygonal geometry in hectares."""
    if geom.is_empty:
        return 0.0
    area_m2, _perimeter = _GEOD.geometry_area_perimeter(geom)
    return abs(area_m2) / 10_000.0


def as_multipolygon(geom: BaseGeometry) -> MultiPolygon:
    """Normalise polygonal output to a valid MultiPolygon (possibly empty)."""
    if geom.is_empty:
        return MultiPolygon()
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    if isinstance(geom, MultiPolygon):
        return geom
    polygons = [part for part in getattr(geom, "geoms", []) if isinstance(part, Polygon)]
    multi = [
        p
        for part in getattr(geom, "geoms", [])
        if isinstance(part, MultiPolygon)
        for p in part.geoms
    ]
    return MultiPolygon(polygons + multi)

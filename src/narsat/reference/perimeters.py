"""Independent burnt-area perimeters used to assess the maps (not to produce them).

The intended source is the EFFIS burnt-area database published by the Copernicus
Emergency Management Service. It is downloaded by hand, because the download page
asks the user to acknowledge the EFFIS data licence:

    https://forest-fire.emergency.copernicus.eu/applications/data-and-services

The loader is generic: it takes any polygon file (GeoJSON natively; Shapefile,
zipped Shapefile or GeoPackage through the optional ``pyogrio`` dependency) and a
mapping from the file's attribute names to the four fields the project needs.

The default attribute names (``id``, ``FIREDATE``, ``AREA_HA``, ``COUNTRY``) are an
assumption about the EFFIS layer that has not been checked against a current
download. Run ``narsat reference describe-perimeters <file>`` first: it prints the
attributes actually present, and the names can then be passed explicitly.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import shapely
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from narsat.config import BoundingBox
from narsat.db import Connection
from narsat.geo import as_multipolygon
from narsat.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class FieldMap:
    id_field: str = "id"
    #: None: ignore dates and match perimeters to events by place only.
    date_field: str | None = "FIREDATE"
    area_field: str = "AREA_HA"
    country_field: str = "COUNTRY"
    #: strptime pattern for text dates, e.g. "%m/%d/%Y". Without it, ISO dates and a few
    #: unambiguous patterns are tried, and a day-first reading is assumed for "a/b/YYYY".
    date_format: str | None = None


@dataclass(frozen=True)
class Perimeter:
    ref_id: str
    fire_date: dt.date | None
    area_ha: float | None
    country: str | None
    attrs: dict[str, Any]
    geometry: BaseGeometry


def _parse_date(value: Any, date_format: str | None = None) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if date_format is not None:
        try:
            return dt.datetime.strptime(text, date_format).date()  # noqa: DTZ007 - date only
        except ValueError:
            # A file that says "MM/DD/YYYY" may still hold ISO dates (pyogrio converts
            # date-typed attributes), so fall through to the unambiguous readings.
            pass
        for candidate in (text, text[:10]):
            try:
                return dt.date.fromisoformat(candidate)
            except ValueError:
                continue
        return None
    for candidate in (text, text[:10]):
        try:
            return dt.date.fromisoformat(candidate)
        except ValueError:
            continue
    for pattern in ("%Y/%m/%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(text[:10], pattern).date()  # noqa: DTZ007 - date only
        except ValueError:
            continue
    return None


def _to_float(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _read_features(
    path: Path, bbox: BoundingBox | None
) -> list[tuple[dict[str, Any], BaseGeometry]]:
    """Read ``(attributes, geometry)`` pairs in EPSG:4326."""
    if path.suffix.lower() in (".geojson", ".json"):
        collection = json.loads(path.read_text(encoding="utf-8"))
        pairs = [
            (dict(feature.get("properties") or {}), shape(feature["geometry"]))
            for feature in collection["features"]
            if feature.get("geometry")
        ]
        if bbox is not None:
            area = shapely.box(*bbox.as_tuple())
            pairs = [(attrs, geom) for attrs, geom in pairs if geom.intersects(area)]
        return pairs

    try:
        from pyogrio.raw import read
    except ImportError as exc:  # pragma: no cover - depends on the installed extras
        raise RuntimeError(
            "reading this format needs pyogrio: install with 'pip install narsat[vector]'"
        ) from exc
    import pyproj

    source = f"/vsizip/{path}" if path.suffix.lower() == ".zip" else str(path)
    meta, _fids, geometries, columns = read(source)
    crs = pyproj.CRS.from_user_input(meta["crs"]) if meta.get("crs") else pyproj.CRS.from_epsg(4326)
    to_wgs84 = None
    if not crs.equals(pyproj.CRS.from_epsg(4326)):
        to_wgs84 = pyproj.Transformer.from_crs(crs, 4326, always_xy=True).transform
    window = shapely.box(*bbox.as_tuple()) if bbox is not None else None
    pairs = []
    names = list(meta["fields"])
    for row, wkb in enumerate(geometries):
        if wkb is None:
            continue
        geometry = shapely.from_wkb(wkb)
        if to_wgs84 is not None:
            geometry = shapely.transform(geometry, to_wgs84, interleaved=False)  # type: ignore[arg-type]
        if window is not None and not geometry.intersects(window):
            continue
        attrs = {name: _plain(columns[index][row]) for index, name in enumerate(names)}
        pairs.append((attrs, geometry))
    return pairs


def _plain(value: Any) -> Any:
    """NumPy scalars and timestamps to JSON-friendly Python values."""
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, float) and value != value:  # NaN
        return None
    return value


def describe(path: Path) -> dict[str, Any]:
    """Attribute names and feature count of a perimeter file (for checking the field map)."""
    pairs = _read_features(path, None)
    return {
        "features": len(pairs),
        "fields": sorted(pairs[0][0]) if pairs else [],
        "example": pairs[0][0] if pairs else {},
    }


def read_perimeters(
    path: Path, source: str, fields: FieldMap, bbox: BoundingBox | None = None
) -> list[Perimeter]:
    perimeters: list[Perimeter] = []
    for index, (attrs, geometry) in enumerate(_read_features(path, bbox)):
        polygonal = as_multipolygon(geometry)
        if polygonal.is_empty:
            continue
        identifier = attrs.get(fields.id_field)
        perimeters.append(
            Perimeter(
                ref_id=f"{source}:{identifier if identifier not in (None, '') else index}",
                fire_date=_parse_date(attrs.get(fields.date_field), fields.date_format)
                if fields.date_field
                else None,
                area_ha=_to_float(attrs.get(fields.area_field)),
                country=(str(attrs[fields.country_field]).strip() or None)
                if attrs.get(fields.country_field) is not None
                else None,
                attrs=attrs,
                geometry=polygonal,
            )
        )
    return perimeters


def store_perimeters(conn: Connection, source: str, perimeters: list[Perimeter]) -> int:
    """Replace everything previously loaded from ``source``."""
    conn.execute("DELETE FROM reference_perimeter WHERE source = %(source)s", {"source": source})
    for perimeter in perimeters:
        conn.execute(
            """
            INSERT INTO reference_perimeter (ref_id, source, fire_date, country_iso3, area_ha,
                                             attrs, geom)
            VALUES (%(ref_id)s, %(source)s, %(fire_date)s,
                    (SELECT a.country_iso3 FROM admin_unit a
                     WHERE a.level = 0
                       AND ST_Intersects(a.geom, ST_PointOnSurface(
                               ST_MakeValid(ST_GeomFromWKB(%(geom)s, 4326))))
                     LIMIT 1),
                    %(area_ha)s, %(attrs)s::jsonb,
                    ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_GeomFromWKB(%(geom)s, 4326)), 3)))
            ON CONFLICT (ref_id) DO NOTHING
            """,
            {
                "ref_id": perimeter.ref_id,
                "source": source,
                "fire_date": perimeter.fire_date,
                "area_ha": perimeter.area_ha,
                "attrs": json.dumps(perimeter.attrs, default=str),
                "geom": shapely.to_wkb(perimeter.geometry),
            },
        )
    log.info("reference_perimeters_loaded", source=source, perimeters=len(perimeters))
    return len(perimeters)

"""Administrative boundaries (countries and first-level units such as wilayas).

Default source: geoBoundaries (https://www.geoboundaries.org), ``gbOpen`` release,
whose files are openly licensed; the licence of each layer is read from the API
response and stored with the rows so that attribution stays attached to the data.
GADM is *not* used: its licence forbids commercial use.

Any GeoJSON file can be loaded instead (``load_geojson_file``), for example an
official boundary file that reflects a recent administrative reform.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2 as httpx
import shapely
from shapely.geometry import shape

from narsat.db import Connection
from narsat.geo import as_multipolygon
from narsat.log import get_logger

log = get_logger(__name__)

GEOBOUNDARIES_API = "https://www.geoboundaries.org/api/current/gbOpen"


@dataclass(frozen=True)
class BoundaryLayer:
    country_iso3: str
    level: int
    source: str
    licence: str | None
    features: list[dict[str, Any]]


def fetch_geoboundaries(
    iso3: str, level: int, transport: httpx.BaseTransport | None = None
) -> BoundaryLayer:
    """Download one layer (``level`` 0 = country, 1 = first-level units)."""
    with httpx.Client(timeout=120.0, follow_redirects=True, transport=transport) as client:
        meta_response = client.get(f"{GEOBOUNDARIES_API}/{iso3.upper()}/ADM{level}/")
        meta_response.raise_for_status()
        meta = meta_response.json()
        if isinstance(meta, list):  # the API returns a list when queried for several layers
            meta = meta[0]
        # The simplified geometry is ample for attributing points to a wilaya.
        url = meta.get("simplifiedGeometryGeoJSON") or meta["gjDownloadURL"]
        data_response = client.get(url)
        data_response.raise_for_status()
        collection = data_response.json()
    release = meta.get("boundaryYearRepresented") or meta.get("buildDate") or "current"
    return BoundaryLayer(
        country_iso3=iso3.upper(),
        level=level,
        source=f"geoBoundaries gbOpen {iso3.upper()} ADM{level} ({release}); "
        f"{meta.get('boundarySource', 'source not stated')}",
        licence=meta.get("boundaryLicense"),
        features=list(collection["features"]),
    )


def load_geojson_file(
    path: Path, iso3: str, level: int, source: str, licence: str | None
) -> BoundaryLayer:
    collection = json.loads(path.read_text(encoding="utf-8"))
    return BoundaryLayer(iso3.upper(), level, source, licence, list(collection["features"]))


def store_layer(
    conn: Connection,
    layer: BoundaryLayer,
    name_field: str = "shapeName",
    code_field: str = "shapeISO",
    fallback_code_field: str = "shapeID",
) -> int:
    """Replace the layer for this country and level; returns the number of units stored."""
    conn.execute(
        "DELETE FROM admin_unit WHERE country_iso3 = %(iso3)s AND level = %(level)s",
        {"iso3": layer.country_iso3, "level": layer.level},
    )
    stored = 0
    seen_codes: set[str] = set()
    for index, feature in enumerate(layer.features):
        properties = feature.get("properties") or {}
        geometry = as_multipolygon(shape(feature["geometry"]))
        if geometry.is_empty:
            continue
        name = str(properties.get(name_field) or layer.country_iso3)
        code = str(
            properties.get(code_field)
            or properties.get(fallback_code_field)
            or f"{layer.country_iso3}-{layer.level}-{index}"
        )
        if code in seen_codes:  # some sources reuse a code for multi-part units
            code = f"{code}-{index}"
        seen_codes.add(code)
        conn.execute(
            """
            INSERT INTO admin_unit (country_iso3, level, code, name, source, licence, geom)
            VALUES (%(iso3)s, %(level)s, %(code)s, %(name)s, %(source)s, %(licence)s,
                    ST_Multi(ST_MakeValid(ST_GeomFromWKB(%(geom)s, 4326))))
            """,
            {
                "iso3": layer.country_iso3,
                "level": layer.level,
                "code": code,
                "name": name,
                "source": layer.source,
                "licence": layer.licence,
                "geom": shapely.to_wkb(geometry),
            },
        )
        stored += 1
    log.info(
        "admin_units_loaded",
        country=layer.country_iso3,
        level=layer.level,
        units=stored,
        licence=layer.licence,
    )
    return stored

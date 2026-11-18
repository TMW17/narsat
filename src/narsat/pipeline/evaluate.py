"""Pixel-level accuracy of the burned-area maps against independent reference perimeters.

The reference polygons (for example NEALGEBA, a Landsat burned-area product for
north-east Algeria) are burned into each mapped event's own 20 m grid and compared with
four maps, fixed on 2026-11-18 before any reference had been loaded:

* ``dnbr``: the dNBR product as mapped.
* ``model``: the model product as mapped.
* ``model_gated``: model pixels that dNBR could analyse (vegetated before the fire, clear
  in both images) and where dNBR is at least 0.10, the lower limit of the USGS low
  severity class. It removes the two errors ``narsat map compare --all`` found: ground
  that was bare before the fire, and vegetation that did not change.
* ``dnbr_or_model_gated``: the union of the two above.

Only pixels inside the coverage zone of the reference are counted (``admin1``, a pattern
on first-level unit names). Event grids can overlap (neighbouring events of one fire
complex). Grids are aligned to multiples of the pixel size in their UTM zone, so every
pixel has a key on a common lattice. A pixel seen by several events is counted once,
and it is burned in a map if any of the events burns it.

The reference is counted only inside the event grids, so recall here means "of the
reference burned area around the fires NarSat detected". Fires NarSat never detected
are not part of it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import numpy as np

from narsat.db import Connection
from narsat.lake.storage import LakeStorage
from narsat.pipeline.compare import MASK_BURNED, EventRasters, load_event_rasters

GATE_MIN_DNBR = 0.10

FLAG_REFERENCE = 1
VARIANTS: dict[str, int] = {
    "dnbr": 2,
    "model": 4,
    "model_gated": 8,
    "dnbr_or_model_gated": 16,
}


def variant_masks(rasters: EventRasters) -> dict[str, np.ndarray]:
    """The four maps of one event (see the module docstring)."""
    dnbr_burned = rasters.dnbr_mask == MASK_BURNED
    model_burned = rasters.model_mask == MASK_BURNED
    changed = np.isfinite(rasters.dnbr) & (np.nan_to_num(rasters.dnbr, nan=-1.0) >= GATE_MIN_DNBR)
    gated = model_burned & changed
    return {
        "dnbr": dnbr_burned,
        "model": model_burned,
        "model_gated": gated,
        "dnbr_or_model_gated": dnbr_burned | gated,
    }


def pixel_keys(epsg: int, transform: Any, rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
    """A key per pixel that is the same in every grid aligned to the same lattice."""
    resolution = float(transform.a)
    x0 = round(transform.c / resolution)
    y0 = round(transform.f / resolution)
    xi = (x0 + cols).astype(np.int64)
    yi = (y0 - rows).astype(np.int64)
    if (xi < 0).any() or (yi < 0).any() or (xi >= 1 << 21).any() or (yi >= 1 << 21).any():
        raise ValueError("pixel index outside the key range")
    keys: np.ndarray = (np.int64(epsg % 1000) << 42) | (xi << 21) | yi
    return keys


def event_flags(
    rasters: EventRasters, reference: np.ndarray, zone: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Keys and flags of the zone pixels that are burned in the reference or in any map."""
    flags = np.where(reference, FLAG_REFERENCE, 0).astype(np.uint8)
    for name, mask in variant_masks(rasters).items():
        flags |= np.where(mask, VARIANTS[name], 0).astype(np.uint8)
    rows, cols = np.nonzero((flags > 0) & zone)
    keys = pixel_keys(rasters.epsg, rasters.transform, rows, cols)
    return keys, flags[rows, cols]


def merge(keys: list[np.ndarray], flags: list[np.ndarray]) -> np.ndarray:
    """One flag set per distinct pixel: the union over the events that see it."""
    if not keys:
        return np.zeros(0, dtype=np.uint8)
    all_keys = np.concatenate(keys)
    all_flags = np.concatenate(flags)
    order = np.argsort(all_keys, kind="stable")
    all_keys, all_flags = all_keys[order], all_flags[order]
    starts = np.flatnonzero(np.r_[True, all_keys[1:] != all_keys[:-1]])
    return np.bitwise_or.reduceat(all_flags, starts)


def scores(flags: np.ndarray, pixel_ha: float) -> dict[str, Any]:
    """Areas and accuracy of each map against the reference."""
    reference = (flags & FLAG_REFERENCE) > 0
    result: dict[str, Any] = {"reference_ha": round(float(reference.sum()) * pixel_ha, 1)}
    for name, bit in VARIANTS.items():
        mapped = (flags & bit) > 0
        tp = int((mapped & reference).sum())
        fp = int((mapped & ~reference).sum())
        fn = int((~mapped & reference).sum())
        result[name] = {
            "mapped_ha": round((tp + fp) * pixel_ha, 1),
            "true_positive_ha": round(tp * pixel_ha, 1),
            "false_positive_ha": round(fp * pixel_ha, 1),
            "false_negative_ha": round(fn * pixel_ha, 1),
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / (tp + fn), 3) if tp + fn else None,
            "iou": round(tp / (tp + fp + fn), 3) if tp + fp + fn else None,
            "dice": round(2 * tp / (2 * tp + fp + fn), 3) if tp + fp + fn else None,
        }
    return result


# ----------------------------------------------------------------------------- database
def _burn(geometries: list[bytes], rasters: EventRasters) -> np.ndarray:
    from rasterio.features import rasterize
    from shapely import from_wkb

    shape = rasters.dnbr_mask.shape
    shapes = [(geom, 1) for geom in (from_wkb(wkb) for wkb in geometries) if not geom.is_empty]
    if not shapes:
        return np.zeros(shape, dtype=bool)
    burned = rasterize(shapes, out_shape=shape, transform=rasters.transform, fill=0, dtype="uint8")
    return np.asarray(burned, dtype=bool)


def _footprint_wkb(rasters: EventRasters) -> bytes:
    from shapely import to_wkb

    from narsat.imagery.reader import Grid

    height, width = rasters.dnbr_mask.shape
    grid = Grid(rasters.epsg, rasters.transform, width, height)
    return bytes(to_wkb(grid.footprint_4326()))


_REFERENCE_IN_GRID = """
SELECT ST_AsBinary(ST_Transform(
           ST_CollectionExtract(ST_MakeValid(ST_Intersection(r.geom, f.geom)), 3), %(epsg)s)) AS wkb
FROM reference_perimeter r,
     (SELECT ST_GeomFromWKB(%(footprint)s, 4326) AS geom) f
WHERE r.source = %(source)s AND ST_Intersects(r.geom, f.geom)
"""

_ZONE_IN_GRID = """
SELECT ST_AsBinary(ST_Transform(
           ST_CollectionExtract(ST_MakeValid(ST_Intersection(a.geom, f.geom)), 3), %(epsg)s)) AS wkb
FROM admin_unit a,
     (SELECT ST_GeomFromWKB(%(footprint)s, 4326) AS geom) f
WHERE a.level = 1 AND a.name ~* %(pattern)s AND ST_Intersects(a.geom, f.geom)
"""

_EVENTS = """
SELECT e.event_id, coalesce(a.name, '-') AS region, e.first_seen
FROM fire_event e
JOIN burned_area d ON d.event_id = e.event_id AND d.method = 'dnbr'
JOIN burned_area m ON m.event_id = e.event_id AND m.method = 'model'
LEFT JOIN admin_unit a ON a.admin_id = e.admin1_id
WHERE e.mapping_status = 'mapped'
  AND (%(since)s::date IS NULL OR e.first_seen >= %(since)s::date)
  AND (%(until)s::date IS NULL OR e.first_seen < %(until)s::date)
  AND (%(pattern)s::text IS NULL OR EXISTS (
        SELECT 1 FROM admin_unit z
        WHERE z.level = 1 AND z.name ~* %(pattern)s AND ST_Intersects(z.geom, e.hull)))
ORDER BY e.first_seen
"""


@dataclass(frozen=True)
class EvaluationScope:
    source: str
    admin1_pattern: str | None = None
    since: dt.date | None = None
    until: dt.date | None = None


def evaluate(conn: Connection, storage: LakeStorage, scope: EvaluationScope) -> dict[str, Any]:
    """Accuracy of the four maps over every mapped event in the scope."""
    from narsat.log import get_logger

    log = get_logger(__name__)
    params = {
        "source": scope.source,
        "pattern": scope.admin1_pattern,
        "since": scope.since,
        "until": scope.until,
    }
    zone_names = (
        [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM admin_unit WHERE level = 1 AND name ~* %(pattern)s ORDER BY name",
                params,
            ).fetchall()
        ]
        if scope.admin1_pattern
        else []
    )
    perimeters = conn.execute(
        "SELECT count(*) AS n FROM reference_perimeter WHERE source = %(source)s", params
    ).fetchone()
    if not perimeters or perimeters["n"] == 0:
        raise ValueError(f"no reference perimeters loaded for source {scope.source!r}")

    events = conn.execute(_EVENTS, params).fetchall()
    keys: list[np.ndarray] = []
    flags: list[np.ndarray] = []
    per_event = []
    skipped = []
    pixel_sizes: set[float] = set()
    for number, event in enumerate(events, start=1):
        try:
            rasters, _product = load_event_rasters(conn, storage, event["event_id"])
        except ValueError as exc:
            skipped.append({"event_id": str(event["event_id"]), "reason": str(exc)})
            continue
        grid = {"epsg": rasters.epsg, "footprint": _footprint_wkb(rasters), **params}
        reference = _burn(
            [row["wkb"] for row in conn.execute(_REFERENCE_IN_GRID, grid).fetchall()], rasters
        )
        zone = (
            _burn([row["wkb"] for row in conn.execute(_ZONE_IN_GRID, grid).fetchall()], rasters)
            if scope.admin1_pattern
            else np.ones_like(reference)
        )
        event_keys, event_flags_ = event_flags(rasters, reference, zone)
        keys.append(event_keys)
        flags.append(event_flags_)
        pixel_sizes.add(rasters.pixel_size_m)
        pixel_ha = rasters.pixel_size_m**2 / 10_000.0
        own = scores(event_flags_, pixel_ha)
        per_event.append(
            {
                "event_id": str(event["event_id"]),
                "region": event["region"],
                "first_day": event["first_seen"].date().isoformat(),
                "reference_ha": own["reference_ha"],
                **{f"{name}_iou": own[name]["iou"] for name in VARIANTS},
            }
        )
        log.info("event_evaluated", event_id=str(event["event_id"]), n=number, of=len(events))

    if len(pixel_sizes) > 1:
        raise ValueError(f"events mapped at different pixel sizes: {sorted(pixel_sizes)}")
    pixel_size = pixel_sizes.pop() if pixel_sizes else 20.0
    merged = merge(keys, flags)
    per_event.sort(key=lambda row: row["reference_ha"], reverse=True)
    return {
        "source": scope.source,
        "zone": zone_names or "whole event grids",
        "events_evaluated": len(per_event),
        "pixel_size_m": pixel_size,
        **scores(merged, pixel_size**2 / 10_000.0),
        "largest_reference_events": per_event[:15],
        "skipped": skipped,
    }

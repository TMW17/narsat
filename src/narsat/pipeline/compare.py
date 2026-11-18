"""Where the two burned-area methods of an event disagree, and why.

The dNBR map and the model map are compared pixel by pixel on the event's 20 m grid,
using the rasters the mapper stored (both burn masks, the dNBR raster and the pre- and
post-fire reflectance). Every pixel that only the model calls burned is put into one
class that explains why dNBR left it out:

* ``bare_or_water_before``: dNBR did not analyse it because the pre-fire NDVI was below
  the vegetation floor (bare ground, sparse cover or water). By design: bare ground
  cannot burn and its NBR changes for other reasons.
* ``no_clear_view``: dNBR did not analyse it for another reason, in practice cloud or
  shadow in one of the two images.
* ``unchanged``: dNBR below 0.10, inside the USGS "unburned" range (Key and Benson, 2006).
* ``low_severity``: dNBR from 0.10 to 0.27, the USGS "low severity" range, but below the
  event's threshold.
* ``below_threshold``: dNBR of at least 0.27 but still below the event's threshold.
* ``removed_by_cleaning``: dNBR above the threshold, but the patch was smaller than the
  minimum patch size or too far from any fire detection.

Pixels only dNBR calls burned are split into those the model could not see and those it
scored below its own threshold.

Areas are pixel counts times the pixel area, so they differ slightly from the
geodesic polygon areas stored with the products.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import numpy as np

from narsat.db import Connection
from narsat.imagery import indices
from narsat.lake.storage import LakeStorage

#: USGS burn-severity class limits for dNBR (Key and Benson, 2006).
DNBR_UNBURNED_MAX = 0.10
DNBR_LOW_SEVERITY_MAX = 0.27

MASK_BURNED = 1
MASK_NODATA = 255

MODEL_ONLY_CLASSES: dict[str, str] = {
    "bare_or_water_before": "dNBR skipped it: bare, sparse or water before the fire",
    "no_clear_view": "dNBR skipped it: no clear view in one of the two images",
    "unchanged": "dNBR below 0.10 (USGS unburned range)",
    "low_severity": "dNBR 0.10 to 0.27 (USGS low severity), below the event's threshold",
    "below_threshold": "dNBR of at least 0.27 but below the event's threshold",
    "removed_by_cleaning": "dNBR above the threshold, dropped as a small or distant patch",
}


@dataclass(frozen=True)
class EventRasters:
    """The stored rasters of one mapped event, all on the same grid."""

    dnbr_mask: np.ndarray  # uint8: 1 burned, 0 not, 255 not analysed
    model_mask: np.ndarray  # uint8: 1 burned, 0 not, 255 not visible
    dnbr: np.ndarray  # float32, NaN where not analysed
    pre: np.ndarray  # float32 reflectance (6, h, w), NaN where missing
    post: np.ndarray  # float32 reflectance (6, h, w), NaN where missing
    pixel_size_m: float
    epsg: int = 0  # the grid's projection and placement, for comparing with vectors
    transform: Any = None  # affine.Affine of the grid


def _median(values: np.ndarray) -> float | None:
    finite = values[np.isfinite(values)]
    return round(float(np.median(finite)), 3) if finite.size else None


@dataclass(frozen=True)
class Disagreement:
    """Pixel counts behind one comparison, before any rounding."""

    pixel_ha: float
    both: int
    dnbr_only: int
    model_only: int
    classes: dict[str, int]  # model-only pixels per class of MODEL_ONLY_CLASSES
    model_could_not_see: int  # dNBR-only pixels the model had no view of
    model_scored_below_threshold: int  # dNBR-only pixels the model saw and rejected
    medians: dict[str, dict[str, float | None]]  # per class: dnbr, pre_ndvi, post_ndvi


def measure(rasters: EventRasters, *, dnbr_threshold: float, min_pre_ndvi: float) -> Disagreement:
    """Classify every pixel on which the two methods disagree (see the module docstring)."""
    shapes = {
        rasters.dnbr_mask.shape,
        rasters.model_mask.shape,
        rasters.dnbr.shape,
        rasters.pre.shape[1:],
        rasters.post.shape[1:],
    }
    if len(shapes) != 1:
        raise ValueError(f"rasters are not on one grid: {sorted(shapes)}")

    dnbr_burned = rasters.dnbr_mask == MASK_BURNED
    model_burned = rasters.model_mask == MASK_BURNED
    model_only = model_burned & ~dnbr_burned
    dnbr_only = dnbr_burned & ~model_burned

    dnbr = rasters.dnbr
    pre_ndvi = indices.ndvi(rasters.pre)
    post_ndvi = indices.ndvi(rasters.post)
    analysed = np.isfinite(dnbr)
    value = np.nan_to_num(dnbr, nan=-np.inf)

    masks: dict[str, np.ndarray] = {}
    bare_before = ~analysed & (np.nan_to_num(pre_ndvi, nan=np.inf) < min_pre_ndvi)
    masks["bare_or_water_before"] = bare_before
    masks["no_clear_view"] = ~analysed & ~bare_before
    above = analysed & (value >= dnbr_threshold)
    masks["removed_by_cleaning"] = above
    below = analysed & ~above
    masks["unchanged"] = below & (value < DNBR_UNBURNED_MAX)
    masks["low_severity"] = below & (value >= DNBR_UNBURNED_MAX) & (value < DNBR_LOW_SEVERITY_MAX)
    masks["below_threshold"] = below & (value >= DNBR_LOW_SEVERITY_MAX)

    classes: dict[str, int] = {}
    medians: dict[str, dict[str, float | None]] = {}
    for name in MODEL_ONLY_CLASSES:
        selected = model_only & masks[name]
        classes[name] = int(selected.sum())
        medians[name] = {
            "dnbr": _median(dnbr[selected]),
            "pre_ndvi": _median(pre_ndvi[selected]),
            "post_ndvi": _median(post_ndvi[selected]),
        }
    return Disagreement(
        pixel_ha=rasters.pixel_size_m**2 / 10_000.0,
        both=int((dnbr_burned & model_burned).sum()),
        dnbr_only=int(dnbr_only.sum()),
        model_only=int(model_only.sum()),
        classes=classes,
        model_could_not_see=int((dnbr_only & (rasters.model_mask == MASK_NODATA)).sum()),
        model_scored_below_threshold=int((dnbr_only & (rasters.model_mask == 0)).sum()),
        medians=medians,
    )


def explain_disagreement(
    rasters: EventRasters, *, dnbr_threshold: float, min_pre_ndvi: float
) -> dict[str, Any]:
    """The report of one event: areas per class, with the medians of each class."""
    result = measure(rasters, dnbr_threshold=dnbr_threshold, min_pre_ndvi=min_pre_ndvi)
    ha = result.pixel_ha
    breakdown = [
        {
            "class": name,
            "meaning": description,
            "ha": round(result.classes[name] * ha, 2),
            "share": round(result.classes[name] / result.model_only, 3)
            if result.model_only
            else 0.0,
            "median_dnbr": result.medians[name]["dnbr"],
            "median_pre_ndvi": result.medians[name]["pre_ndvi"],
            "median_post_ndvi": result.medians[name]["post_ndvi"],
        }
        for name, description in MODEL_ONLY_CLASSES.items()
    ]
    return {
        "pixel_size_m": rasters.pixel_size_m,
        "dnbr_threshold": round(dnbr_threshold, 3),
        "min_pre_ndvi": min_pre_ndvi,
        "burned_ha": {
            "both": round(result.both * ha, 2),
            "dnbr_only": round(result.dnbr_only * ha, 2),
            "model_only": round(result.model_only * ha, 2),
        },
        "model_only": breakdown,
        "dnbr_only": {
            "model_could_not_see": round(result.model_could_not_see * ha, 2),
            "model_scored_below_threshold": round(result.model_scored_below_threshold * ha, 2),
        },
    }


def _hectares(members: list[Disagreement], field: str, name: str | None = None) -> float:
    """Sum of one pixel count over events, in hectares (``name`` picks a model-only class)."""
    return float(
        sum((r.classes[name] if name else getattr(r, field)) * r.pixel_ha for r in members)
    )


def summarise(results: list[tuple[str, Disagreement]]) -> list[dict[str, Any]]:
    """Totals per outcome group (``both found burn`` and so on), then over all events.

    Areas are summed before rounding. Medians are not aggregated: they belong to one event.
    """
    groups: dict[str, list[Disagreement]] = {}
    for outcome, result in results:
        groups.setdefault(outcome, []).append(result)
    if results:
        groups["all events"] = [result for _, result in results]
    rows = []
    for outcome, members in groups.items():
        model_only = _hectares(members, "model_only")
        rows.append(
            {
                "outcome": outcome,
                "events": len(members),
                "burned_ha": {
                    field: round(_hectares(members, field), 1)
                    for field in ("both", "dnbr_only", "model_only")
                },
                "model_only_ha": {
                    name: round(_hectares(members, "classes", name), 1)
                    for name in MODEL_ONLY_CLASSES
                },
                "model_only_share": {
                    name: round(_hectares(members, "classes", name) / model_only, 3)
                    if model_only
                    else 0.0
                    for name in MODEL_ONLY_CLASSES
                },
                "dnbr_only": {
                    field: round(_hectares(members, field), 1)
                    for field in ("model_could_not_see", "model_scored_below_threshold")
                },
            }
        )
    return rows


# ------------------------------------------------------------------------------ loading
@dataclass(frozen=True)
class _Raster:
    data: np.ndarray
    pixel_size_m: float
    epsg: int
    transform: Any


def _read(storage: LakeStorage, uri: str) -> _Raster:
    """Decoded bands of a stored COG (scale and nodata applied), with its grid."""
    from rasterio.io import MemoryFile

    prefix = storage.base_uri + "/"
    if not uri.startswith(prefix):
        raise ValueError(f"raster {uri} is not in the asset store {storage.base_uri}")
    with MemoryFile(storage.read_bytes(uri[len(prefix) :])) as memory, memory.open() as source:
        data = source.read()
        grid = (float(abs(source.transform.a)), int(source.crs.to_epsg()), source.transform)
        if source.dtypes[0] == "uint8":
            return _Raster(data, *grid)
        decoded = data.astype("float32")
        if source.nodata is not None and not np.isnan(source.nodata):
            decoded[data == source.nodata] = np.nan
        scales = np.asarray(source.scales, dtype="float32").reshape(-1, 1, 1)
        return _Raster(decoded * scales, *grid)


def load_event_rasters(
    conn: Connection, storage: LakeStorage, event_id: uuid.UUID
) -> tuple[EventRasters, dict[str, Any]]:
    """The rasters of a mapped event and the dNBR product's settings (threshold, NDVI floor)."""
    assets = {
        (row["kind"], row["method"]): row["uri"]
        for row in conn.execute(
            "SELECT kind, method, uri FROM raster_asset WHERE event_id = %(id)s", {"id": event_id}
        ).fetchall()
    }
    needed = {
        "dnbr_mask": ("mask", "dnbr"),
        "model_mask": ("mask", "model"),
        "dnbr": ("dnbr", None),
        "pre": ("pre", None),
        "post": ("post", None),
    }
    missing = [name for name, key in needed.items() if key not in assets]
    if missing:
        raise ValueError(
            f"event {event_id} has no {', '.join(missing)} raster: map it with both methods first"
        )
    product = conn.execute(
        "SELECT threshold, params FROM burned_area WHERE event_id = %(id)s AND method = 'dnbr'",
        {"id": event_id},
    ).fetchone()
    if product is None or product["threshold"] is None:
        raise ValueError(f"event {event_id} has no dNBR product")

    arrays = {name: _read(storage, assets[key]) for name, key in needed.items()}
    rasters = EventRasters(
        dnbr_mask=arrays["dnbr_mask"].data[0],
        model_mask=arrays["model_mask"].data[0],
        dnbr=arrays["dnbr"].data[0],
        pre=arrays["pre"].data,
        post=arrays["post"].data,
        pixel_size_m=arrays["dnbr_mask"].pixel_size_m,
        epsg=arrays["dnbr_mask"].epsg,
        transform=arrays["dnbr_mask"].transform,
    )
    settings = {
        "dnbr_threshold": float(product["threshold"]),
        "min_pre_ndvi": float(product["params"].get("min_pre_ndvi", 0.15)),
    }
    return rasters, settings


_COMPARABLE_EVENTS = """
SELECT d.event_id,
       CASE WHEN d.area_ha > 0 AND m.area_ha > 0 THEN 'both found burn'
            WHEN d.area_ha > 0 THEN 'only dNBR found burn'
            ELSE 'only model found burn' END AS outcome
FROM burned_area d
JOIN burned_area m ON m.event_id = d.event_id AND m.method = 'model'
WHERE d.method = 'dnbr' AND (d.area_ha > 0 OR m.area_ha > 0)
ORDER BY d.area_ha + m.area_ha DESC
"""


def compare_all(conn: Connection, storage: LakeStorage) -> dict[str, Any]:
    """Run the comparison on every event where at least one method found a burn."""
    from narsat.log import get_logger

    log = get_logger(__name__)
    events = conn.execute(_COMPARABLE_EVENTS).fetchall()
    results: list[tuple[str, Disagreement]] = []
    skipped = []
    for number, row in enumerate(events, start=1):
        try:
            rasters, product = load_event_rasters(conn, storage, row["event_id"])
        except ValueError as exc:
            skipped.append({"event_id": str(row["event_id"]), "reason": str(exc)})
            continue
        results.append((row["outcome"], measure(rasters, **product)))
        log.info("event_compared", event_id=str(row["event_id"]), n=number, of=len(events))
    return {"events_compared": len(results), "groups": summarise(results), "skipped": skipped}

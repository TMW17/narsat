"""Burned-area mapping of one fire event.

    event (PostGIS) -> area of interest -> STAC search -> pre/post scene choice
        -> windowed COG reads -> dNBR baseline and/or model inference
        -> patch post-processing -> polygons -> COGs in the lake + rows in PostGIS

The step is idempotent: products are keyed by ``(event, method)`` and rasters by a
fixed lake key, so mapping an event again replaces its previous products.

Scene choice
------------
* Post-fire: the *earliest* scene after the last detection whose area of interest is
  clear enough. Early is better than cloud-free-but-late, because regrowth and
  autumn rain erase the signal quickly in Mediterranean shrubland.
* Pre-fire: the *latest* clear scene before the first detection, preferring the same
  relative orbit as the post-fire scene (same viewing geometry, smaller BRDF and
  co-registration differences).
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from rasterio.enums import Resampling
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from narsat import ids, metrics
from narsat.config import ImagerySettings, SegmentationSettings
from narsat.db import Connection
from narsat.geo import buffer_metres
from narsat.imagery import indices, reader
from narsat.imagery.cog import encode_reflectance, write_cog
from narsat.imagery.reader import Grid, GridTooLargeError, SceneArrays
from narsat.imagery.stac import REFLECTANCE_ASSETS, Scene, SearchFunction, group_scenes
from narsat.imagery.vectorize import Polygonised, points_to_pixels, polygonise, rasterise
from narsat.lake.storage import LakeStorage
from narsat.log import get_logger
from narsat.segmentation.client import InferenceClient
from narsat.segmentation.dnbr import DNBR_VERSION, segment_dnbr
from narsat.segmentation.postprocess import clean_mask

log = get_logger(__name__)

#: Below this clear fraction a scene is never used, even when nothing better exists.
MIN_USABLE_CLEAR = 0.3
MAX_ATTEMPTS = 5
MASK_NODATA = 255

SceneReader = Callable[[Scene, Grid, int], SceneArrays]
SclReader = Callable[[Scene, Grid], np.ndarray]


@dataclass(frozen=True)
class EventRecord:
    event_id: uuid.UUID
    first_seen: dt.datetime
    last_seen: dt.datetime
    n_hotspots: int
    hull: BaseGeometry
    hotspot_lons: np.ndarray
    hotspot_lats: np.ndarray


@dataclass(frozen=True)
class ChosenScene:
    scene: Scene
    scl: np.ndarray
    clear_fraction: float


@dataclass
class Product:
    method: str
    model_version: str
    polygons: Polygonised
    threshold: float | None
    mean_score: float | None
    clear_fraction: float
    params: dict[str, Any]


@dataclass
class MappingOutcome:
    event_id: uuid.UUID
    status: str  # mapped | imagery_pending | failed
    note: str | None = None
    pre_scene: str | None = None
    post_scene: str | None = None
    products: list[Product] = field(default_factory=list)
    seconds: float = 0.0


def load_event(conn: Connection, event_id: uuid.UUID) -> EventRecord | None:
    row = conn.execute(
        "SELECT event_id, first_seen, last_seen, n_hotspots, ST_AsBinary(hull) AS hull "
        "FROM fire_event WHERE event_id = %(id)s AND status IN ('active', 'cooling', 'closed')",
        {"id": event_id},
    ).fetchone()
    if row is None:
        return None
    points = conn.execute(
        "SELECT longitude, latitude FROM hotspot WHERE event_id = %(id)s", {"id": event_id}
    ).fetchall()
    return EventRecord(
        event_id=row["event_id"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        n_hotspots=row["n_hotspots"],
        hull=shapely.from_wkb(bytes(row["hull"])),
        hotspot_lons=np.array([p["longitude"] for p in points], dtype="float64"),
        hotspot_lats=np.array([p["latitude"] for p in points], dtype="float64"),
    )


def clear_fraction(scl: np.ndarray, aoi_mask: np.ndarray) -> float:
    """Share of the area of interest whose surface is visible in a scene."""
    inside = int(aoi_mask.sum())
    if inside == 0:
        return 0.0
    return float((~np.isin(scl, indices.SCL_OBSCURED) & aoi_mask).sum() / inside)


def choose_scene(
    candidates: list[Scene],
    grid: Grid,
    aoi_mask: np.ndarray,
    min_clear: float,
    max_candidates: int,
    read_scl: SclReader,
) -> tuple[ChosenScene | None, ChosenScene | None]:
    """Walk the candidates in order; return ``(first acceptable, clearest seen)``."""
    best: ChosenScene | None = None
    for scene in candidates[:max_candidates]:
        scl = read_scl(scene, grid)
        chosen = ChosenScene(scene, scl, clear_fraction(scl, aoi_mask))
        if best is None or chosen.clear_fraction > best.clear_fraction:
            best = chosen
        if chosen.clear_fraction >= min_clear:
            return chosen, best
    return None, best


class EventMapper:
    def __init__(
        self,
        imagery: ImagerySettings,
        seg: SegmentationSettings,
        storage: LakeStorage,
        search: SearchFunction,
        inference: InferenceClient | None = None,
        read_scene: SceneReader = reader.read_scene,
        read_scl: SclReader = reader.read_scl,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._imagery = imagery
        self._seg = seg
        self._storage = storage
        self._search = search
        self._inference = inference
        self._read_scene = read_scene
        self._read_scl = read_scl
        self._clock = clock
        if "model" in seg.methods and inference is None:
            raise ValueError("segmentation method 'model' needs NARSAT_SEG_INFERENCE_URL")

    # ------------------------------------------------------------------ scenes
    def _candidates(self, aoi: BaseGeometry, start: dt.datetime, end: dt.datetime) -> list[Scene]:
        if end <= start:
            return []
        return group_scenes(self._search(mapping(aoi), start, end))

    def _pick_post(
        self, event: EventRecord, aoi: BaseGeometry, grid: Grid, aoi_mask: np.ndarray
    ) -> tuple[ChosenScene | None, str | None, bool]:
        """Returns ``(scene, note, window_still_open)``."""
        now = self._clock()
        start = event.last_seen + dt.timedelta(days=self._imagery.post_min_delay_days)
        window_end = event.last_seen + dt.timedelta(days=self._imagery.post_window_days)
        still_open = now < window_end
        candidates = sorted(
            self._candidates(aoi, start, min(now, window_end)), key=lambda s: s.datetime
        )
        accepted, best = choose_scene(
            candidates, grid, aoi_mask, self._imagery.min_clear_fraction,
            self._imagery.max_candidates, self._read_scl,
        )  # fmt: skip
        if accepted is not None:
            return accepted, None, still_open
        if best is None:
            return None, "no post-fire Sentinel-2 scene found yet", still_open
        note = f"clearest post-fire scene is {best.clear_fraction:.0%} clear"
        if not still_open and best.clear_fraction >= MIN_USABLE_CLEAR:
            return best, note + " (used: the post-fire window has closed)", still_open
        return None, note, still_open

    def _pick_pre(
        self, event: EventRecord, aoi: BaseGeometry, grid: Grid, aoi_mask: np.ndarray,
        post_orbit: int | None,
    ) -> ChosenScene | None:  # fmt: skip
        end = event.first_seen - dt.timedelta(days=1)
        start = event.first_seen - dt.timedelta(days=self._imagery.pre_window_days)
        candidates = self._candidates(aoi, start, end)
        # Same orbit first, then most recent first (``sorted`` is stable).
        ordered = sorted(candidates, key=lambda s: s.datetime, reverse=True)
        ordered = sorted(ordered, key=lambda s: s.relative_orbit != post_orbit)
        accepted, _best = choose_scene(
            ordered, grid, aoi_mask, self._imagery.min_clear_fraction,
            self._imagery.max_candidates, self._read_scl,
        )  # fmt: skip
        return accepted

    # ------------------------------------------------------------------ outputs
    def _store_raster(
        self, conn: Connection, event: EventRecord, workdir: Path, kind: str, method: str | None,
        data: np.ndarray, grid: Grid, nodata: float | int | None, bands: list[str],
        overview_resampling: str = "average", scale: float | None = None,
    ) -> str:  # fmt: skip
        name = f"{method}_{kind}" if method else kind
        local = write_cog(
            workdir / f"{name}.tif", data, grid, nodata, bands, overview_resampling, scale
        )
        uri = self._storage.put_file(local, f"events/{event.event_id}/{name}.tif")
        conn.execute(
            """
            INSERT INTO raster_asset (asset_id, event_id, kind, method, uri, epsg, width, height,
                                      bands, footprint)
            VALUES (%(asset_id)s, %(event_id)s, %(kind)s, %(method)s, %(uri)s, %(epsg)s,
                    %(width)s, %(height)s, %(bands)s, ST_GeomFromText(%(footprint)s, 4326))
            ON CONFLICT (asset_id) DO UPDATE
            SET uri = EXCLUDED.uri, epsg = EXCLUDED.epsg, width = EXCLUDED.width,
                height = EXCLUDED.height, bands = EXCLUDED.bands,
                footprint = EXCLUDED.footprint, created_at = now()
            """,
            {
                "asset_id": ids.asset_id(event.event_id, kind, method),
                "event_id": event.event_id,
                "kind": kind,
                "method": method,
                "uri": uri,
                "epsg": grid.epsg,
                "width": grid.width,
                "height": grid.height,
                "bands": bands,
                "footprint": grid.footprint_4326().wkt,
            },
        )
        return uri

    def _store_product(
        self, conn: Connection, event: EventRecord, product: Product,
        pre: ChosenScene | None, post: ChosenScene,
    ) -> None:  # fmt: skip
        conn.execute(
            """
            INSERT INTO burned_area AS b (
                ba_id, event_id, method, model_version, pre_item_ids, post_item_ids,
                pre_datetime, post_datetime, threshold, mean_score, clear_fraction, area_ha,
                n_polygons, geom, params
            )
            VALUES (
                %(ba_id)s, %(event_id)s, %(method)s, %(model_version)s, %(pre_items)s,
                %(post_items)s, %(pre_datetime)s, %(post_datetime)s, %(threshold)s,
                %(mean_score)s, %(clear_fraction)s, %(area_ha)s, %(n_polygons)s,
                ST_Multi(ST_GeomFromWKB(%(geom)s, 4326)), %(params)s::jsonb
            )
            ON CONFLICT (event_id, method) DO UPDATE
            SET model_version = EXCLUDED.model_version,
                pre_item_ids = EXCLUDED.pre_item_ids,
                post_item_ids = EXCLUDED.post_item_ids,
                pre_datetime = EXCLUDED.pre_datetime,
                post_datetime = EXCLUDED.post_datetime,
                threshold = EXCLUDED.threshold,
                mean_score = EXCLUDED.mean_score,
                clear_fraction = EXCLUDED.clear_fraction,
                area_ha = EXCLUDED.area_ha,
                n_polygons = EXCLUDED.n_polygons,
                geom = EXCLUDED.geom,
                params = EXCLUDED.params,
                -- A human verdict applies to the geometry it was given for.
                qa_status = CASE WHEN ST_Equals(b.geom, EXCLUDED.geom) THEN b.qa_status
                                 ELSE 'auto' END,
                qa_by = CASE WHEN ST_Equals(b.geom, EXCLUDED.geom) THEN b.qa_by END,
                qa_at = CASE WHEN ST_Equals(b.geom, EXCLUDED.geom) THEN b.qa_at END,
                qa_note = CASE WHEN ST_Equals(b.geom, EXCLUDED.geom) THEN b.qa_note END,
                updated_at = now()
            """,
            {
                "ba_id": ids.burned_area_id(event.event_id, product.method),
                "event_id": event.event_id,
                "method": product.method,
                "model_version": product.model_version,
                "pre_items": pre.scene.item_ids if pre else [],
                "post_items": post.scene.item_ids,
                "pre_datetime": pre.scene.datetime if pre else None,
                "post_datetime": post.scene.datetime,
                "threshold": product.threshold,
                "mean_score": product.mean_score,
                "clear_fraction": product.clear_fraction,
                "area_ha": product.polygons.area_ha,
                "n_polygons": product.polygons.n_polygons,
                "geom": shapely.to_wkb(product.polygons.geometry),
                "params": json.dumps(product.params),
            },
        )

    def _store_provenance(self, event: EventRecord, role: str, chosen: ChosenScene) -> None:
        for item in chosen.scene.items:
            self._storage.put_bytes(
                json.dumps(item, sort_keys=True).encode("utf-8"),
                f"events/{event.event_id}/stac/{role}/{item['id']}.json",
            )

    # ------------------------------------------------------------------ methods
    def _dnbr_product(
        self, conn: Connection, event: EventRecord, workdir: Path, grid: Grid,
        aoi_mask: np.ndarray, seeds: tuple[np.ndarray, np.ndarray],
        pre: SceneArrays, post: SceneArrays,
    ) -> Product:  # fmt: skip
        result = segment_dnbr(pre.reflectance, post.reflectance, pre.scl, post.scl, self._seg)
        analysable = result.analysable & aoi_mask
        cleaned = clean_mask(
            result.mask & aoi_mask, grid.resolution, self._seg.min_patch_ha, seeds[0], seeds[1],
            self._seg.seed_distance_m,
        )  # fmt: skip
        self._store_raster(
            conn, event, workdir, "dnbr", None, result.dnbr, grid, float("nan"), ["dnbr"]
        )
        mask_raster = np.where(analysable, cleaned.astype("uint8"), MASK_NODATA).astype("uint8")
        self._store_raster(
            conn,
            event,
            workdir,
            "mask",
            "dnbr",
            mask_raster,
            grid,
            MASK_NODATA,
            ["burned"],
            overview_resampling="nearest",
        )
        burned_values = result.dnbr[cleaned]
        return Product(
            method="dnbr",
            model_version=DNBR_VERSION,
            polygons=polygonise(cleaned, grid),
            threshold=result.threshold,
            mean_score=float(np.nanmean(burned_values)) if burned_values.size else None,
            clear_fraction=float(analysable.sum() / max(1, int(aoi_mask.sum()))),
            params={
                "threshold_source": result.threshold_source,
                "min_pre_ndvi": self._seg.min_pre_ndvi,
                "min_patch_ha": self._seg.min_patch_ha,
                "seed_distance_m": self._seg.seed_distance_m,
                "resolution_m": grid.resolution,
            },
        )

    def _model_product(
        self, conn: Connection, event: EventRecord, workdir: Path, grid: Grid,
        aoi_mask: np.ndarray, seeds: tuple[np.ndarray, np.ndarray], post: SceneArrays,
    ) -> Product:  # fmt: skip
        if self._inference is None:  # guarded in __init__
            raise RuntimeError("inference client missing")
        info = self._inference.model_info()
        model_grid = grid.with_resolution(self._seg.model_resolution_m)
        cube = reader.resample(post.reflectance, grid, model_grid, Resampling.average)
        coarse = self._inference.segment(cube)
        probability = reader.resample(coarse, model_grid, grid, Resampling.bilinear)
        visible = indices.observed(post.scl, post.reflectance) & aoi_mask
        probability = np.where(visible, probability, np.nan).astype("float32")
        raw = visible & (np.nan_to_num(probability, nan=0.0) >= self._seg.model_threshold)
        cleaned = clean_mask(
            raw,
            grid.resolution,
            self._seg.min_patch_ha,
            seeds[0],
            seeds[1],
            self._seg.seed_distance_m,
        )
        self._store_raster(
            conn,
            event,
            workdir,
            "probability",
            "model",
            probability,
            grid,
            float("nan"),
            ["burn_probability"],
        )
        mask_raster = np.where(visible, cleaned.astype("uint8"), MASK_NODATA).astype("uint8")
        self._store_raster(
            conn,
            event,
            workdir,
            "mask",
            "model",
            mask_raster,
            grid,
            MASK_NODATA,
            ["burned"],
            overview_resampling="nearest",
        )
        scores = probability[cleaned]
        return Product(
            method="model",
            model_version=f"{info.name}@{info.version}",
            polygons=polygonise(cleaned, grid),
            threshold=self._seg.model_threshold,
            mean_score=float(np.nanmean(scores)) if scores.size else None,
            clear_fraction=float(visible.sum() / max(1, int(aoi_mask.sum()))),
            params={
                "engine": info.engine,
                "device": info.device,
                "model_resolution_m": self._seg.model_resolution_m,
                "min_patch_ha": self._seg.min_patch_ha,
                "seed_distance_m": self._seg.seed_distance_m,
            },
        )

    # ------------------------------------------------------------------ entry point
    def map_event(self, conn: Connection, event_id: uuid.UUID) -> MappingOutcome:
        started = time.perf_counter()
        event = load_event(conn, event_id)
        if event is None:
            return MappingOutcome(event_id, "failed", "event not found or no longer live")
        outcome = self._map(conn, event)
        outcome.seconds = time.perf_counter() - started
        _record_outcome(conn, event, outcome, self._clock())
        metrics.EVENTS_MAPPED.labels(outcome.status).inc()
        metrics.MAPPING_SECONDS.observe(outcome.seconds)
        log.info(
            "event_mapped",
            event_id=str(event_id),
            status=outcome.status,
            note=outcome.note,
            pre=outcome.pre_scene,
            post=outcome.post_scene,
            products={p.method: round(p.polygons.area_ha, 1) for p in outcome.products},
            seconds=round(outcome.seconds, 1),
        )
        return outcome

    def _map(self, conn: Connection, event: EventRecord) -> MappingOutcome:
        aoi = buffer_metres(event.hull, self._imagery.aoi_buffer_m)
        try:
            grid = Grid.for_geometry(aoi, self._imagery.resolution_m, self._imagery.max_pixels)
        except GridTooLargeError as exc:
            return MappingOutcome(event.event_id, "failed", str(exc))
        aoi_mask = rasterise(aoi, grid)
        seeds = points_to_pixels(event.hotspot_lons, event.hotspot_lats, grid)

        post, note, still_open = self._pick_post(event, aoi, grid, aoi_mask)
        if post is None:
            status = "imagery_pending" if still_open else "failed"
            return MappingOutcome(event.event_id, status, note)
        pre = self._pick_pre(event, aoi, grid, aoi_mask, post.scene.relative_orbit)
        methods = [m for m in self._seg.methods if m != "dnbr" or pre is not None]
        if not methods:
            return MappingOutcome(
                event.event_id,
                "failed",
                "no clear pre-fire scene for dNBR",
                post_scene=post.scene.key,
            )

        post_arrays = self._read_scene(post.scene, grid, self._imagery.read_threads)
        pre_arrays = self._read_scene(pre.scene, grid, self._imagery.read_threads) if pre else None
        outcome = MappingOutcome(
            event.event_id, "mapped", note,
            pre_scene=pre.scene.key if pre else None, post_scene=post.scene.key,
        )  # fmt: skip
        band_names = list(REFLECTANCE_ASSETS)
        with tempfile.TemporaryDirectory(prefix="narsat-map-") as tmp:
            workdir = Path(tmp)
            self._store_raster(
                conn,
                event,
                workdir,
                "post",
                None,
                encode_reflectance(post_arrays.reflectance),
                grid,
                -32768,
                band_names,
                scale=1e-4,
            )
            self._store_provenance(event, "post", post)
            if pre is not None and pre_arrays is not None:
                self._store_raster(
                    conn,
                    event,
                    workdir,
                    "pre",
                    None,
                    encode_reflectance(pre_arrays.reflectance),
                    grid,
                    -32768,
                    band_names,
                    scale=1e-4,
                )
                self._store_provenance(event, "pre", pre)
            for method in methods:
                if method == "dnbr" and pre_arrays is not None:
                    product = self._dnbr_product(
                        conn, event, workdir, grid, aoi_mask, seeds, pre_arrays, post_arrays
                    )
                else:
                    product = self._model_product(
                        conn, event, workdir, grid, aoi_mask, seeds, post_arrays
                    )
                self._store_product(conn, event, product, pre, post)
                outcome.products.append(product)
        if pre is None:
            outcome.note = "no clear pre-fire scene: dNBR skipped"
        return outcome


def _record_outcome(
    conn: Connection, event: EventRecord, outcome: MappingOutcome, now: dt.datetime
) -> None:
    conn.execute(
        """
        UPDATE fire_event
        SET mapping_status = %(status)s,
            mapping_note = %(note)s,
            mapping_attempts = mapping_attempts + 1,
            mapping_checked_at = %(now)s,
            mapped_at = CASE WHEN %(status)s = 'mapped' THEN %(now)s ELSE mapped_at END,
            mapped_n_hotspots = CASE WHEN %(status)s IN ('mapped', 'failed') THEN n_hotspots
                                     ELSE mapped_n_hotspots END,
            updated_at = now()
        WHERE event_id = %(id)s
        """,
        {"status": outcome.status, "note": outcome.note, "now": now, "id": event.event_id},
    )


def pending_events(
    conn: Connection,
    limit: int,
    recheck_hours: float = 12.0,
    now: dt.datetime | None = None,
) -> list[uuid.UUID]:
    """Events waiting for mapping: new ones first, then those still waiting for imagery.

    An event waiting for a clear scene is offered again only after ``recheck_hours``:
    Sentinel-2 revisits every few days, so asking the catalogue more often is pointless.
    """
    rows = conn.execute(
        """
        SELECT event_id FROM fire_event
        WHERE status IN ('active', 'cooling', 'closed')
          AND (mapping_status = 'pending'
               OR (mapping_status = 'imagery_pending'
                   AND (mapping_checked_at IS NULL
                        OR mapping_checked_at < %(now)s - %(hours)s * interval '1 hour')))
        ORDER BY (mapping_status = 'pending') DESC, n_hotspots DESC, last_seen
        LIMIT %(limit)s
        """,
        {"limit": limit, "hours": recheck_hours, "now": now or dt.datetime.now(dt.UTC)},
    ).fetchall()
    return [row["event_id"] for row in rows]


def record_crash(conn: Connection, event_id: uuid.UUID, error: str) -> None:
    """An unexpected error: retry on later runs, give up after ``MAX_ATTEMPTS``."""
    conn.execute(
        """
        UPDATE fire_event
        SET mapping_attempts = mapping_attempts + 1,
            mapping_checked_at = now(),
            mapping_note = %(note)s,
            mapping_status = CASE WHEN mapping_attempts + 1 >= %(max)s THEN 'failed'
                                  ELSE mapping_status END,
            mapped_n_hotspots = CASE WHEN mapping_attempts + 1 >= %(max)s THEN n_hotspots
                                     ELSE mapped_n_hotspots END,
            updated_at = now()
        WHERE event_id = %(id)s
        """,
        {"id": event_id, "note": f"error: {error[:400]}", "max": MAX_ATTEMPTS},
    )

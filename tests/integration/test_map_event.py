"""Burned-area mapping of an event, end to end, on synthetic scenes with a known answer.

Real code paths: PostGIS, STAC item handling, windowed raster reads (from local
GeoTIFFs instead of HTTPS), dNBR, post-processing, polygonisation, COG output, lake
storage and the HTTP contract of the inference service (its engine is replaced by a
stub so the test needs no model weights).
"""

from __future__ import annotations

import datetime as dt
import io
import json
import math
import uuid
from pathlib import Path

import httpx2 as httpx
import numpy as np
import pytest
import rasterio

from narsat.config import DbSettings, ImagerySettings, LakeSettings, SegmentationSettings
from narsat.db import connect
from narsat.imagery import indices
from narsat.lake.storage import LakeStorage
from narsat.pipeline.compare import compare_all, explain_disagreement, load_event_rasters
from narsat.pipeline.evaluate import EvaluationScope, evaluate
from narsat.pipeline.map_event import EventMapper, pending_events, record_crash
from narsat.pipeline.validate import compute_agreement
from narsat.segmentation.client import InferenceClient
from tests import synthetic
from tests.integration.helpers import fire_over_synthetic_burn, single_event, square

pytestmark = pytest.mark.integration
UTC = dt.UTC
PRE = dt.datetime(2023, 7, 10, 10, 30, tzinfo=UTC)
POST = dt.datetime(2023, 7, 28, 10, 30, tzinfo=UTC)
NOW = dt.datetime(2023, 10, 1, tzinfo=UTC)
TRUE_AREA_HA = float(synthetic.truth_20m().burned.sum() * 0.04)


@pytest.fixture(scope="module")
def items(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    directory = tmp_path_factory.mktemp("s2")
    return {
        "pre": synthetic.write_scene(
            directory, "S2A_T31SEA_20230710", PRE, burned=False, noise_seed=1
        ),
        "post": synthetic.write_scene(
            directory, "S2A_T31SEA_20230728", POST, burned=True, cloudy=True, noise_seed=2
        ),
        "post_overcast": synthetic.write_scene(
            directory,
            "S2A_T31SEA_20230726",
            dt.datetime(2023, 7, 26, 10, 30, tzinfo=UTC),
            burned=True,
            noise_seed=3,
        ),
    }


def _overcast(item: dict, directory: Path) -> dict:
    """A copy of an item whose scene classification is cloud everywhere."""
    source = item["assets"]["scl"]["href"]
    target = directory / "overcast_scl.tif"
    with rasterio.open(source) as src:
        profile = src.profile
        with rasterio.open(target, "w", **profile) as dst:
            dst.write(np.full((src.height, src.width), indices.SCL_CLOUD_HIGH, dtype="uint8"), 1)
    assets = {**item["assets"], "scl": {**item["assets"]["scl"], "href": str(target)}}
    return {**item, "assets": assets}


def _stub_inference() -> InferenceClient:
    """Speaks the inference service's HTTP contract; scores burn from single-date NBR."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/model":
            return httpx.Response(200, json={
                "name": "stub-nbr", "version": "test", "engine": "stub",
                "bands": ["blue", "green", "red", "nir08", "swir16", "swir22"],
                "resolution_m": 30.0, "device": "cpu",
            })  # fmt: skip
        cube = np.load(io.BytesIO(request.content), allow_pickle=False)
        assert cube.shape[0] == 6
        nbr = np.nan_to_num(indices.nbr(cube), nan=1.0)
        probability = (1.0 / (1.0 + np.exp(20.0 * nbr))).astype("float32")
        buffer = io.BytesIO()
        np.save(buffer, probability, allow_pickle=False)
        return httpx.Response(200, content=buffer.getvalue())

    return InferenceClient("http://inference.test", transport=httpx.MockTransport(handler))


def _mapper(
    lake: LakeSettings, items: list[dict], methods: list[str], now: dt.datetime = NOW
) -> tuple[EventMapper, LakeStorage]:
    storage = LakeStorage.assets(lake)
    storage.ensure_ready()
    mapper = EventMapper(
        imagery=ImagerySettings(),
        seg=SegmentationSettings(methods=methods),
        storage=storage,
        search=synthetic.fake_search(items),
        inference=_stub_inference() if "model" in methods else None,
        clock=lambda: now,
    )
    return mapper, storage


def test_maps_the_burned_area_with_both_methods(
    db_settings: DbSettings, lake_settings: LakeSettings, items: dict[str, dict]
) -> None:
    event_id = single_event(db_settings)
    mapper, storage = _mapper(lake_settings, [items["pre"], items["post"]], ["dnbr", "model"])

    with connect(db_settings) as conn:
        assert pending_events(conn, 10) == [event_id]
        outcome = mapper.map_event(conn, event_id)

    assert outcome.status == "mapped"
    assert outcome.pre_scene == "sentinel-2a_20230710_R108"
    assert outcome.post_scene == "sentinel-2a_20230728_R108"
    assert {p.method for p in outcome.products} == {"dnbr", "model"}

    with connect(db_settings) as conn:
        products = {
            row["method"]: row
            for row in conn.execute(
                "SELECT method, model_version, area_ha, n_polygons, threshold, clear_fraction, "
                "pre_item_ids, post_item_ids, qa_status, params, ST_IsValid(geom) AS valid, "
                "ST_Area(geom::geography) / 10000 AS postgis_ha, "
                "ST_Contains(geom, (SELECT centroid FROM fire_event)) AS over_fire "
                "FROM burned_area"
            ).fetchall()
        }
        event = conn.execute(
            "SELECT mapping_status, mapped_n_hotspots, n_hotspots, mapping_attempts, mapped_at "
            "FROM fire_event"
        ).fetchone()
        assets = conn.execute(
            "SELECT kind, method, uri, epsg, bands FROM raster_asset ORDER BY kind, method"
        ).fetchall()
        # Every raster's footprint covers the whole fire and they all share one grid.
        footprints = conn.execute(
            "SELECT bool_and(ST_Covers(a.footprint, e.hull)) AS cover_fire, "
            "count(DISTINCT ST_AsText(a.footprint)) AS distinct_footprints, "
            "bool_and(ST_IsValid(a.footprint)) AS valid "
            "FROM raster_asset a JOIN fire_event e USING (event_id)"
        ).fetchone()

    assert footprints == {"cover_fire": True, "distinct_footprints": 1, "valid": True}
    assert event is not None
    assert event["mapping_status"] == "mapped"
    assert event["mapped_n_hotspots"] == event["n_hotspots"] == 8
    assert event["mapping_attempts"] == 1

    dnbr = products["dnbr"]
    assert dnbr["model_version"] == "dnbr-v1"
    assert dnbr["area_ha"] == pytest.approx(TRUE_AREA_HA, rel=0.03)
    assert dnbr["postgis_ha"] == pytest.approx(dnbr["area_ha"], rel=0.005)
    assert dnbr["n_polygons"] == 1
    assert dnbr["valid"]
    assert dnbr["over_fire"]
    assert 0.10 <= dnbr["threshold"] <= 0.44
    assert dnbr["pre_item_ids"] == ["S2A_T31SEA_20230710"]
    assert dnbr["post_item_ids"] == ["S2A_T31SEA_20230728"]
    assert dnbr["qa_status"] == "auto"
    assert dnbr["params"]["threshold_source"].startswith("otsu")
    # The small cloud at the edge of the area lowers the analysable share a little.
    assert 0.9 < dnbr["clear_fraction"] < 1.0

    model = products["model"]
    assert model["model_version"] == "stub-nbr@test"
    assert model["area_ha"] == pytest.approx(TRUE_AREA_HA, rel=0.06)  # 30 m round trip
    assert model["pre_item_ids"] == ["S2A_T31SEA_20230710"]

    kinds = [(a["kind"], a["method"]) for a in assets]
    assert kinds == [
        ("dnbr", None),
        ("mask", "dnbr"),
        ("mask", "model"),
        ("post", None),
        ("pre", None),
        ("probability", "model"),
    ]
    assert all(a["epsg"] == 32631 for a in assets)
    keys = storage.list_keys(f"events/{event_id}")
    assert f"events/{event_id}/post.tif" in keys
    assert f"events/{event_id}/dnbr_mask.tif" in keys
    assert f"events/{event_id}/stac/post/S2A_T31SEA_20230728.json" in keys
    stored_item = json.loads(
        storage.read_bytes(f"events/{event_id}/stac/pre/S2A_T31SEA_20230710.json")
    )
    assert stored_item["id"] == "S2A_T31SEA_20230710"

    # The rasters are readable cloud-optimised GeoTIFFs with the documented encoding.
    post_uri = next(a["uri"] for a in assets if a["kind"] == "post")
    with rasterio.open(post_uri.removeprefix("file://")) as dataset:
        assert dataset.count == 6
        assert dataset.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
        assert dataset.descriptions[3] == "nir08"
    mask_uri = next(a["uri"] for a in assets if a["kind"] == "mask" and a["method"] == "dnbr")
    with rasterio.open(mask_uri.removeprefix("file://")) as dataset:
        mask = dataset.read(1)
        assert set(np.unique(mask)) <= {0, 1, 255}
        assert (mask == 1).sum() * 0.04 == pytest.approx(TRUE_AREA_HA, rel=0.03)


def test_explains_where_the_methods_disagree_from_the_stored_rasters(
    db_settings: DbSettings, lake_settings: LakeSettings, items: dict[str, dict]
) -> None:
    event_id = single_event(db_settings)
    mapper, storage = _mapper(lake_settings, [items["pre"], items["post"]], ["dnbr", "model"])
    with connect(db_settings) as conn:
        mapper.map_event(conn, event_id)
        rasters, product = load_event_rasters(conn, storage, event_id)
        areas = {
            row["method"]: row
            for row in conn.execute("SELECT method, area_ha, threshold FROM burned_area").fetchall()
        }

    # The stored COGs decode back to the mapper's grid and units.
    assert rasters.pixel_size_m == 20.0
    assert rasters.pre.shape[0] == rasters.post.shape[0] == 6
    assert rasters.pre.shape[1:] == rasters.dnbr.shape == rasters.model_mask.shape
    assert 0.0 < float(np.nanmedian(rasters.pre[3])) < 1.0  # reflectance, not digital numbers
    assert np.isnan(rasters.dnbr).any()  # the cloud at the edge of the area
    assert product["dnbr_threshold"] == pytest.approx(areas["dnbr"]["threshold"])
    assert product["min_pre_ndvi"] == SegmentationSettings().min_pre_ndvi

    result = explain_disagreement(rasters, **product)
    burned = result["burned_ha"]
    # Pixel counts agree with the stored polygon areas.
    assert burned["both"] + burned["dnbr_only"] == pytest.approx(areas["dnbr"]["area_ha"], rel=0.01)
    assert burned["both"] + burned["model_only"] == pytest.approx(
        areas["model"]["area_ha"], rel=0.01
    )
    assert sum(c["ha"] for c in result["model_only"]) == pytest.approx(
        burned["model_only"], abs=0.1 * len(result["model_only"])
    )


def test_compares_every_event_where_a_method_found_a_burn(
    db_settings: DbSettings, lake_settings: LakeSettings, items: dict[str, dict]
) -> None:
    event_id = single_event(db_settings)
    mapper, storage = _mapper(lake_settings, [items["pre"], items["post"]], ["dnbr", "model"])
    with connect(db_settings) as conn:
        mapper.map_event(conn, event_id)
        rasters, product = load_event_rasters(conn, storage, event_id)
        single = explain_disagreement(rasters, **product)
        report = compare_all(conn, storage)

    assert report["events_compared"] == 1
    assert report["skipped"] == []
    rows = {row["outcome"]: row for row in report["groups"]}
    assert set(rows) == {"both found burn", "all events"}
    assert rows["all events"]["burned_ha"]["both"] == pytest.approx(
        single["burned_ha"]["both"], abs=0.1
    )


def test_evaluation_against_reference_perimeters(
    db_settings: DbSettings, lake_settings: LakeSettings, items: dict[str, dict]
) -> None:
    event_id = single_event(db_settings)
    mapper, storage = _mapper(lake_settings, [items["pre"], items["post"]], ["dnbr", "model"])
    with connect(db_settings) as conn:
        mapper.map_event(conn, event_id)
        with pytest.raises(ValueError, match="no reference perimeters"):
            evaluate(conn, storage, EvaluationScope(source="test"))
        # Use the dNBR product itself as the reference: the vector-to-raster round trip
        # must give back (nearly) the same pixels.
        conn.execute(
            """
            INSERT INTO reference_perimeter (ref_id, source, area_ha, attrs, geom)
            SELECT 'test:1', 'test', area_ha, '{}'::jsonb, geom
            FROM burned_area WHERE method = 'dnbr'
            """
        )
        everywhere = evaluate(conn, storage, EvaluationScope(source="test"))
        conn.execute(
            "INSERT INTO admin_unit (country_iso3, level, code, name, source, geom) VALUES "
            "('DZA', 1, 'FAR', 'Far Away', 'test', ST_GeomFromText(%(square)s, 4326))",
            {"square": square(10.0, 30.0, 0.05)},
        )
        elsewhere = evaluate(conn, storage, EvaluationScope(source="test", admin1_pattern="^far"))

    assert everywhere["events_evaluated"] == 1
    assert everywhere["zone"] == "whole event grids"
    dnbr = everywhere["dnbr"]
    assert dnbr["precision"] >= 0.98
    assert dnbr["recall"] >= 0.98
    assert everywhere["reference_ha"] == pytest.approx(dnbr["mapped_ha"], rel=0.02)
    assert everywhere["dnbr_or_model_gated"]["recall"] >= dnbr["recall"]
    (event,) = everywhere["largest_reference_events"]
    assert event["event_id"] == str(event_id)
    assert event["dnbr_iou"] >= 0.96

    # A coverage zone that does not touch the event: nothing is evaluated.
    assert elsewhere["zone"] == ["Far Away"]
    assert elsewhere["events_evaluated"] == 0
    assert elsewhere["reference_ha"] == 0.0


def test_comparing_needs_both_products(
    db_settings: DbSettings, lake_settings: LakeSettings, items: dict[str, dict]
) -> None:
    event_id = single_event(db_settings)
    mapper, storage = _mapper(lake_settings, [items["pre"], items["post"]], ["dnbr"])
    with connect(db_settings) as conn:
        mapper.map_event(conn, event_id)
        with pytest.raises(ValueError, match="model_mask"):
            load_event_rasters(conn, storage, event_id)


def test_mapping_again_replaces_products_and_keeps_a_verdict_on_unchanged_geometry(
    db_settings: DbSettings, lake_settings: LakeSettings, items: dict[str, dict]
) -> None:
    event_id = single_event(db_settings)
    mapper, _storage = _mapper(lake_settings, [items["pre"], items["post"]], ["dnbr"])
    with connect(db_settings) as conn:
        mapper.map_event(conn, event_id)
        conn.execute("UPDATE burned_area SET qa_status = 'accepted', qa_note = 'looks right'")
    with connect(db_settings) as conn:
        again = mapper.map_event(conn, event_id)
        rows = conn.execute("SELECT qa_status, qa_note FROM burned_area").fetchall()
        n_assets = conn.execute("SELECT count(*) AS n FROM raster_asset").fetchone()
    assert again.status == "mapped"
    assert rows == [{"qa_status": "accepted", "qa_note": "looks right"}]
    assert n_assets == {"n": 4}  # pre, post, dnbr, dnbr mask: replaced, not duplicated


def test_waits_for_imagery_while_the_post_fire_window_is_open(
    db_settings: DbSettings, lake_settings: LakeSettings, items: dict[str, dict], tmp_path: Path
) -> None:
    event_id = single_event(db_settings)
    overcast = _overcast(items["post_overcast"], tmp_path)
    soon_after = dt.datetime(2023, 7, 27, tzinfo=UTC)  # only the overcast scene exists yet
    mapper, _ = _mapper(
        lake_settings, [items["pre"], overcast, items["post"]], ["dnbr"], now=soon_after
    )
    with connect(db_settings) as conn:
        outcome = mapper.map_event(conn, event_id)
        row = conn.execute(
            "SELECT mapping_status, mapping_note, mapped_n_hotspots FROM fire_event"
        ).fetchone()
        # Checked moments ago: not offered again until the re-check interval has passed.
        assert pending_events(conn, 10, now=soon_after + dt.timedelta(hours=1)) == []
        assert pending_events(conn, 10, now=soon_after + dt.timedelta(hours=13)) == [event_id]
    assert outcome.status == "imagery_pending"
    assert row is not None
    assert row["mapping_status"] == "imagery_pending"
    assert "0% clear" in row["mapping_note"]
    assert row["mapped_n_hotspots"] is None

    # Two days later the clear scene has been acquired: the event is mapped with it,
    # skipping the overcast one.
    later, _ = _mapper(
        lake_settings,
        [items["pre"], overcast, items["post"]],
        ["dnbr"],
        now=dt.datetime(2023, 7, 29, tzinfo=UTC),
    )
    with connect(db_settings) as conn:
        outcome = later.map_event(conn, event_id)
    assert outcome.status == "mapped"
    assert outcome.post_scene == "sentinel-2a_20230728_R108"


def test_fails_cleanly_when_no_usable_scene_appears_before_the_window_closes(
    db_settings: DbSettings, lake_settings: LakeSettings, items: dict[str, dict], tmp_path: Path
) -> None:
    event_id = single_event(db_settings)
    overcast = _overcast(items["post_overcast"], tmp_path)
    mapper, _ = _mapper(lake_settings, [items["pre"], overcast], ["dnbr"])
    with connect(db_settings) as conn:
        outcome = mapper.map_event(conn, event_id)
        row = conn.execute("SELECT mapping_status, mapped_n_hotspots FROM fire_event").fetchone()
        products = conn.execute("SELECT count(*) AS n FROM burned_area").fetchone()
    assert outcome.status == "failed"
    assert row == {"mapping_status": "failed", "mapped_n_hotspots": 8}
    assert products == {"n": 0}


def test_without_a_pre_fire_scene_only_the_single_date_model_runs(
    db_settings: DbSettings, lake_settings: LakeSettings, items: dict[str, dict]
) -> None:
    event_id = single_event(db_settings)
    with_model, _ = _mapper(lake_settings, [items["post"]], ["dnbr", "model"])
    with connect(db_settings) as conn:
        outcome = with_model.map_event(conn, event_id)
        methods = conn.execute("SELECT method, pre_datetime FROM burned_area").fetchall()
    assert outcome.status == "mapped"
    assert outcome.note == "no clear pre-fire scene: dNBR skipped"
    assert methods == [{"method": "model", "pre_datetime": None}]

    dnbr_only, _ = _mapper(lake_settings, [items["post"]], ["dnbr"])
    with connect(db_settings) as conn:
        assert dnbr_only.map_event(conn, event_id).status == "failed"


def test_unknown_event_and_crash_bookkeeping(
    db_settings: DbSettings, lake_settings: LakeSettings, items: dict[str, dict]
) -> None:
    mapper, _ = _mapper(lake_settings, [], ["dnbr"])
    event_id = single_event(db_settings)
    with connect(db_settings) as conn:
        assert mapper.map_event(conn, uuid.uuid4()).status == "failed"
        for attempt in range(1, 6):
            record_crash(conn, event_id, "RuntimeError: network down")
            row = conn.execute(
                "SELECT mapping_status, mapping_attempts, mapping_note FROM fire_event"
            ).fetchone()
            assert row is not None
            assert row["mapping_attempts"] == attempt
            # Retried four times, given up on the fifth.
            assert row["mapping_status"] == ("failed" if attempt == 5 else "pending")
        assert "network down" in row["mapping_note"]


def test_the_model_method_needs_an_inference_service(lake_settings: LakeSettings) -> None:
    with pytest.raises(ValueError, match="NARSAT_SEG_INFERENCE_URL"):
        EventMapper(
            ImagerySettings(),
            SegmentationSettings(methods=["model"]),
            LakeStorage.assets(lake_settings),
            synthetic.fake_search([]),
        )


def test_agreement_with_reference_perimeters_and_between_methods(
    db_settings: DbSettings, lake_settings: LakeSettings, items: dict[str, dict]
) -> None:
    event_id = single_event(db_settings)
    mapper, _ = _mapper(lake_settings, [items["pre"], items["post"]], ["dnbr", "model"])
    lon, lat = synthetic.truth_20m().centre_lonlat
    with connect(db_settings) as conn:
        mapper.map_event(conn, event_id)
        # A reference perimeter: a 900 m disc on the true burn (the synthetic truth), one
        # perimeter of another year at the same place, one far away, and a neighbouring
        # fire: a 300 m disc 2.5 km east, more than 750 m from every detection of the
        # event but within 2 km of the event's outline.
        east = 2500.0 / (111_320.0 * math.cos(math.radians(lat)))
        for ref_id, day, x, y, radius in [
            ("effis:1", "2023-07-23", lon, lat, 900),
            ("effis:2", "2021-08-10", lon, lat, 900),
            ("effis:3", "2023-07-23", lon + 1.0, lat, 900),
            ("effis:4", "2023-07-23", lon + east, lat, 300),
        ]:
            conn.execute(
                """
                INSERT INTO reference_perimeter (ref_id, source, fire_date, geom)
                VALUES (%(id)s, 'effis', %(day)s,
                        ST_Multi(ST_Buffer(ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 4326)::geography,
                                           %(radius)s)::geometry))
                """,
                {"id": ref_id, "day": day, "x": x, "y": y, "radius": radius},
            )
        hull_distance = conn.execute(
            "SELECT ST_Distance(e.hull::geography, r.geom::geography) AS m FROM fire_event e, "
            "reference_perimeter r WHERE r.ref_id = 'effis:4'"
        ).fetchone()
        assert hull_distance is not None
        assert hull_distance["m"] < 2000  # the old rule (2 km around the event) took it in
        written = compute_agreement(conn)
        rows = conn.execute(
            "SELECT b.method, v.ref_source, v.n_ref_perimeters, v.iou, v.precision_ref, "
            "v.recall_ref, v.mapped_ha, v.ref_ha FROM burned_area_validation v "
            "JOIN burned_area b USING (ba_id) ORDER BY v.ref_source, b.method"
        ).fetchall()
    assert written == {"effis": 2, "narsat:dnbr": 1}
    by_key = {(r["ref_source"], r["method"]): r for r in rows}
    reference = by_key[("effis", "dnbr")]
    # Wrong year, wrong place and the neighbouring fire are not matched.
    assert reference["n_ref_perimeters"] == 1
    assert reference["iou"] > 0.93
    assert reference["recall_ref"] > 0.95
    assert reference["ref_ha"] == pytest.approx(254.5, rel=0.02)
    between = by_key[("narsat:dnbr", "model")]
    assert between["iou"] > 0.9
    # Recomputing is idempotent.
    with connect(db_settings) as conn:
        assert compute_agreement(conn) == written
        assert conn.execute("SELECT count(*) AS n FROM burned_area_validation").fetchone() == {
            "n": 3
        }
    assert len(fire_over_synthetic_burn()) == 8
    # A reference polygon covering a whole fire complex (a 6 km disc, about 11,300 ha) is
    # clipped to the area the mapper examined: the burn beyond was never in the images.
    with connect(db_settings) as conn:
        conn.execute(
            """
            INSERT INTO reference_perimeter (ref_id, source, geom)
            VALUES ('complex:1', 'complex',
                    ST_Multi(ST_Buffer(ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 4326)::geography,
                                       6000)::geometry))
            """,
            {"x": lon, "y": lat},
        )
        compute_agreement(conn)
        complex_row = conn.execute(
            "SELECT v.ref_ha, v.recall_ref FROM burned_area_validation v "
            "JOIN burned_area b USING (ba_id) WHERE v.ref_source = 'complex' AND b.method = 'dnbr'"
        ).fetchone()
        conn.execute("DELETE FROM reference_perimeter WHERE source = 'complex'")
        compute_agreement(conn)
    assert complex_row is not None
    assert complex_row["ref_ha"] < 3000  # not 11,300 ha
    assert complex_row["recall_ref"] > 0.1
    # A product that no longer matches loses its stale row on the next computation.
    with connect(db_settings) as conn:
        conn.execute("DELETE FROM reference_perimeter WHERE ref_id = 'effis:1'")
        assert compute_agreement(conn) == {"effis": 0, "narsat:dnbr": 1}
        conn.execute("DELETE FROM reference_perimeter")
        compute_agreement(conn)
        sources = conn.execute("SELECT DISTINCT ref_source FROM burned_area_validation").fetchall()
        assert sources == [{"ref_source": "narsat:dnbr"}]

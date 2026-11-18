"""Object storage (a real S3 endpoint) and reference-data loaders (a real PostGIS)."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx2 as httpx
import numpy as np
import pytest
import rasterio
import shapely
from affine import Affine
from pydantic import SecretStr

from narsat.config import (
    BoundingBox,
    DbSettings,
    ImagerySettings,
    LakeSettings,
    SegmentationSettings,
)
from narsat.db import connect
from narsat.db.hotspots import insert_hotspots
from narsat.imagery.cog import write_cog
from narsat.imagery.reader import Grid
from narsat.lake.bronze import BronzeHotspotWriter, BronzeRecord, read_committed_offsets
from narsat.lake.catalog import BRONZE_HOTSPOTS, ensure_tables, load_catalog
from narsat.lake.storage import LakeStorage
from narsat.pipeline.map_event import EventMapper
from narsat.reference import admin, perimeters
from tests import synthetic
from tests.conftest import make_hotspot
from tests.integration.helpers import FIRE_START, fire_over_synthetic_burn, single_event

pytestmark = pytest.mark.integration
UTC = dt.UTC
BOX = BoundingBox(west=-13.5, south=27.0, east=11.8, north=37.6)


# =============================================================================== S3
class TestObjectStorage:
    def test_storage_round_trip(self, s3_lake_settings: LakeSettings, tmp_path: Path) -> None:
        storage = LakeStorage.assets(s3_lake_settings)
        storage.ensure_ready()
        storage.ensure_ready()  # idempotent
        local = tmp_path / "payload.bin"
        local.write_bytes(b"\x00\x01narsat")
        uri = storage.put_file(local, "events/e1/post.tif")
        assert uri == f"{s3_lake_settings.assets_uri}/events/e1/post.tif"
        storage.put_bytes(b'{"id": "x"}', "events/e1/stac/post/x.json")
        assert storage.read_bytes("events/e1/post.tif") == b"\x00\x01narsat"
        assert storage.exists("events/e1/stac/post/x.json")
        assert not storage.exists("events/e1/nothing")
        assert storage.list_keys("events/e1") == [
            "events/e1/post.tif", "events/e1/stac/post/x.json",
        ]  # fmt: skip
        storage.delete_prefix("events/e1")
        assert storage.list_keys("events") == []

    def test_iceberg_tables_live_on_object_storage(self, s3_lake_settings: LakeSettings) -> None:
        LakeStorage(s3_lake_settings.warehouse_uri, s3_lake_settings).ensure_ready()
        catalog = load_catalog(s3_lake_settings)
        tables = ensure_tables(catalog)
        assert tables[BRONZE_HOTSPOTS.identifier].location().startswith("s3://")

        records = [
            BronzeRecord(
                make_hotspot(36.7 + 0.001 * i, 4.6, FIRE_START + dt.timedelta(minutes=i)),
                "firms.hotspots.raw",
                0,
                i,
            )
            for i in range(5)
        ]
        writer = BronzeHotspotWriter(catalog)
        assert writer.append(records) == 5
        assert BronzeHotspotWriter(catalog).append(records) == 0  # exactly-once holds on S3 too
        table = load_catalog(s3_lake_settings).load_table(BRONZE_HOTSPOTS.identifier)
        assert table.scan().count() == 5
        assert read_committed_offsets(table) == {("firms.hotspots.raw", 0): 4}
        data_files = table.inspect.files().column("file_path").to_pylist()
        assert all(path.startswith(s3_lake_settings.warehouse_uri) for path in data_files)

    def test_a_cog_in_the_lake_is_readable_by_gdal_over_s3(
        self, s3_lake_settings: LakeSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What TiTiler does: GDAL range-reads the COG from the S3 endpoint.

        The settings mirror the TiTiler container's environment in compose.yaml.
        """
        grid = Grid(32631, Affine(20, 0, 640_000, 0, -20, 4_070_000), 300, 200)
        data = np.arange(300 * 200, dtype="float32").reshape(1, 200, 300)
        local = write_cog(tmp_path / "dnbr.tif", data, grid, float("nan"), ["dnbr"])
        storage = LakeStorage.assets(s3_lake_settings)
        storage.ensure_ready()
        uri = storage.put_file(local, "events/e2/dnbr.tif")

        endpoint = urlparse(os.environ["NARSAT_TEST_S3_ENDPOINT"])
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", os.environ["NARSAT_TEST_S3_ACCESS_KEY"])
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", os.environ["NARSAT_TEST_S3_SECRET_KEY"])
        gdal_env = {
            "AWS_S3_ENDPOINT": endpoint.netloc,
            "AWS_HTTPS": "YES" if endpoint.scheme == "https" else "NO",
            "AWS_VIRTUAL_HOSTING": "FALSE",
            "AWS_REGION": "us-east-1",
        }
        with rasterio.Env(**gdal_env), rasterio.open(uri) as dataset:
            assert dataset.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
            assert dataset.crs.to_epsg() == 32631
            window = dataset.read(1, window=((10, 12), (20, 23)))
        assert np.array_equal(window, data[0, 10:12, 20:23])

    def test_mapping_writes_its_rasters_to_object_storage(
        self, db_settings: DbSettings, s3_lake_settings: LakeSettings, tmp_path: Path
    ) -> None:
        items = [
            synthetic.write_scene(
                tmp_path,
                "PRE",
                dt.datetime(2023, 7, 10, 10, 30, tzinfo=UTC),
                burned=False,
                noise_seed=1,
            ),
            synthetic.write_scene(
                tmp_path,
                "POST",
                dt.datetime(2023, 7, 28, 10, 30, tzinfo=UTC),
                burned=True,
                noise_seed=2,
            ),
        ]
        event_id = single_event(db_settings)
        storage = LakeStorage.assets(s3_lake_settings)
        storage.ensure_ready()
        mapper = EventMapper(
            ImagerySettings(), SegmentationSettings(methods=["dnbr"]), storage,
            synthetic.fake_search(items), clock=lambda: dt.datetime(2023, 10, 1, tzinfo=UTC),
        )  # fmt: skip
        with connect(db_settings) as conn:
            outcome = mapper.map_event(conn, event_id)
            uris = [r["uri"] for r in conn.execute("SELECT uri FROM raster_asset").fetchall()]
        assert outcome.status == "mapped"
        assert len(uris) == 4
        assert all(uri.startswith(s3_lake_settings.assets_uri) for uri in uris)
        keys = storage.list_keys(f"events/{event_id}")
        assert f"events/{event_id}/dnbr_mask.tif" in keys
        assert f"events/{event_id}/stac/pre/PRE.json" in keys


# =============================================================================== boundaries
def _square(lon: float, lat: float, half: float) -> dict:
    w, e, s, n = lon - half, lon + half, lat - half, lat + half
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def _geoboundaries_transport(level: int) -> httpx.MockTransport:
    features = (
        [
            {
                "type": "Feature",
                "properties": {"shapeName": "Algeria", "shapeISO": "DZA", "shapeID": "DZA-ADM0-1"},
                "geometry": _square(3.0, 33.0, 6.0),
            }
        ]
        if level == 0
        else [
            {
                "type": "Feature",
                "properties": {"shapeName": "Béjaïa", "shapeISO": "DZ-06", "shapeID": "DZA-ADM1-6"},
                "geometry": _square(4.6, 36.7, 0.5),
            },
            {
                "type": "Feature",
                "properties": {"shapeName": "Jijel", "shapeISO": "", "shapeID": "DZA-ADM1-18"},
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [_square(5.9, 36.7, 0.3)["coordinates"]],
                },
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.geoboundaries.org":
            assert request.url.path == f"/api/current/gbOpen/DZA/ADM{level}/"
            return httpx.Response(200, json={
                "boundaryISO": "DZA", "boundaryType": f"ADM{level}",
                "boundaryYearRepresented": "2019", "boundarySource": "Test source",
                "boundaryLicense": "CC BY 4.0",
                "gjDownloadURL": f"https://data.test/DZA-ADM{level}.geojson",
                "simplifiedGeometryGeoJSON": f"https://data.test/DZA-ADM{level}_simplified.geojson",
            })  # fmt: skip
        assert request.url.path.endswith("_simplified.geojson")
        return httpx.Response(200, json={"type": "FeatureCollection", "features": features})

    return httpx.MockTransport(handler)


class TestAdminBoundaries:
    def test_geoboundaries_layers_are_loaded_with_their_licence(
        self, db_settings: DbSettings
    ) -> None:
        fire = fire_over_synthetic_burn()
        with connect(db_settings) as conn:
            insert_hotspots(conn, fire)  # stored before any boundary exists
            for level in (0, 1):
                layer = admin.fetch_geoboundaries("dza", level, _geoboundaries_transport(level))
                assert layer.licence == "CC BY 4.0"
                assert "geoBoundaries gbOpen DZA" in layer.source
                admin.store_layer(conn, layer)
            units = conn.execute(
                "SELECT level, code, name, licence, ST_GeometryType(geom) AS type, "
                "ST_IsValid(geom) AS valid FROM admin_unit ORDER BY level, name"
            ).fetchall()
            assert [(u["level"], u["code"], u["name"]) for u in units] == [
                (0, "DZA", "Algeria"), (1, "DZ-06", "Béjaïa"), (1, "DZA-ADM1-18", "Jijel"),
            ]  # fmt: skip
            assert {u["type"] for u in units} == {"ST_MultiPolygon"}
            assert all(u["valid"] and u["licence"] == "CC BY 4.0" for u in units)

            from narsat.db.hotspots import backfill_admin

            assert backfill_admin(conn) == 16  # 8 hotspots x (country + first-level unit)
            row = conn.execute(
                "SELECT count(*) AS n FROM hotspot h JOIN admin_unit a ON a.admin_id = h.admin1_id "
                "WHERE h.country_iso3 = 'DZA' AND a.name = 'Béjaïa'"
            ).fetchone()
            assert row == {"n": 8}

            # Loading a layer again replaces it instead of duplicating it.
            admin.store_layer(
                conn, admin.fetch_geoboundaries("DZA", 1, _geoboundaries_transport(1))
            )
            assert conn.execute(
                "SELECT count(*) AS n FROM admin_unit WHERE level = 1"
            ).fetchone() == {"n": 2}

    def test_a_local_file_with_other_attribute_names(
        self, db_settings: DbSettings, tmp_path: Path
    ) -> None:
        path = tmp_path / "wilayas.geojson"
        path.write_text(json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"NOM": "Tizi Ouzou", "CODE": "15"},
             "geometry": _square(4.05, 36.7, 0.3)},
            {"type": "Feature", "properties": {"NOM": "Vide", "CODE": "99"},
             "geometry": {"type": "Polygon", "coordinates": []}},
        ]}), encoding="utf-8")  # fmt: skip
        layer = admin.load_geojson_file(path, "dza", 1, "Official 2025 boundaries", "open")
        with connect(db_settings) as conn:
            assert admin.store_layer(conn, layer, name_field="NOM", code_field="CODE") == 1
            row = conn.execute("SELECT country_iso3, code, name, source FROM admin_unit").fetchone()
        assert row == {
            "country_iso3": "DZA",
            "code": "15",
            "name": "Tizi Ouzou",
            "source": "Official 2025 boundaries",
        }


# =============================================================================== perimeters
def _perimeter_features() -> list[dict]:
    return [
        {
            "type": "Feature",
            "properties": {
                "id": 101,
                "FIREDATE": "2023-07-23 14:05:00",
                "AREA_HA": "1234.5",
                "COUNTRY": "DZ",
            },
            "geometry": _square(4.6, 36.7, 0.02),
        },
        {
            "type": "Feature",
            "properties": {"id": 102, "FIREDATE": "2023/08/02", "AREA_HA": None, "COUNTRY": "TN"},
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [_square(8.8, 36.9, 0.01)["coordinates"]],
            },
        },
        {
            "type": "Feature",
            "properties": {"id": 103, "FIREDATE": "2023-07-25", "AREA_HA": 800, "COUNTRY": "GR"},
            "geometry": _square(23.5, 38.2, 0.05),
        },  # Greece: outside the area of interest
    ]


class TestReferencePerimeters:
    def test_geojson_is_described_filtered_and_stored(
        self, db_settings: DbSettings, tmp_path: Path
    ) -> None:
        path = tmp_path / "effis.geojson"
        path.write_text(
            json.dumps({"type": "FeatureCollection", "features": _perimeter_features()})
        )
        description = perimeters.describe(path)
        assert description["features"] == 3
        assert description["fields"] == ["AREA_HA", "COUNTRY", "FIREDATE", "id"]

        loaded = perimeters.read_perimeters(path, "effis", perimeters.FieldMap(), BOX)
        assert [p.ref_id for p in loaded] == ["effis:101", "effis:102"]
        assert loaded[0].fire_date == dt.date(2023, 7, 23)
        assert loaded[1].fire_date == dt.date(2023, 8, 2)
        assert (loaded[0].area_ha, loaded[1].area_ha) == (1234.5, None)

        with connect(db_settings) as conn:
            assert perimeters.store_perimeters(conn, "effis", loaded) == 2
            assert perimeters.store_perimeters(conn, "effis", loaded) == 2  # replaced
            rows = conn.execute(
                "SELECT ref_id, fire_date, area_ha, attrs->>'COUNTRY' AS country, "
                "ST_GeometryType(geom) AS type FROM reference_perimeter ORDER BY ref_id"
            ).fetchall()
        assert [(r["ref_id"], r["country"], r["type"]) for r in rows] == [
            ("effis:101", "DZ", "ST_MultiPolygon"), ("effis:102", "TN", "ST_MultiPolygon"),
        ]  # fmt: skip

    def test_other_attribute_names_and_missing_fields(self, tmp_path: Path) -> None:
        path = tmp_path / "custom.geojson"
        path.write_text(json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"fid": "A7", "start": "20230723"},
             "geometry": _square(4.6, 36.7, 0.02)},
        ]}))  # fmt: skip
        fields = perimeters.FieldMap(
            id_field="fid", date_field="start", area_field="surface", country_field="pays"
        )
        (perimeter,) = perimeters.read_perimeters(path, "custom", fields)
        assert perimeter.ref_id == "custom:A7"
        assert perimeter.fire_date == dt.date(2023, 7, 23)
        assert perimeter.area_ha is None
        assert perimeter.country is None

    def test_shapefile_in_another_projection_is_reprojected(self, tmp_path: Path) -> None:
        pyogrio_raw = pytest.importorskip("pyogrio.raw")
        from pyproj import Transformer

        to_utm = Transformer.from_crs(4326, 32631, always_xy=True).transform
        polygon = shapely.transform(
            shapely.geometry.shape(_square(4.6, 36.7, 0.02)), to_utm, interleaved=False
        )
        path = tmp_path / "effis_utm.shp"
        pyogrio_raw.write(
            path,
            geometry=np.array([shapely.to_wkb(polygon)], dtype=object),
            field_data=[
                np.array([555], dtype="int32"),
                np.array(["2023-07-23"], dtype=object),
                np.array([321.0]),
            ],
            fields=["id", "FIREDATE", "AREA_HA"],
            crs="EPSG:32631",
            geometry_type="Polygon",
            driver="ESRI Shapefile",
        )
        (perimeter,) = perimeters.read_perimeters(path, "effis", perimeters.FieldMap(), BOX)
        assert perimeter.ref_id == "effis:555"
        assert perimeter.fire_date == dt.date(2023, 7, 23)
        assert perimeter.area_ha == 321.0
        centroid = perimeter.geometry.centroid
        assert (centroid.x, centroid.y) == pytest.approx((4.6, 36.7), abs=1e-4)
        assert perimeters.describe(path)["fields"] == ["AREA_HA", "FIREDATE", "id"]


# =============================================================================== catalog
class TestPostgresCatalog:
    """The Compose stack keeps the Iceberg catalog in PostgreSQL rather than SQLite."""

    def test_exactly_once_append_and_refresh_with_a_postgres_catalog(
        self, db_settings: DbSettings, tmp_path: Path
    ) -> None:
        import psycopg

        dsn = db_settings.dsn.get_secret_value()
        url = "postgresql+psycopg://" + dsn.split("://", 1)[1]
        lake = LakeSettings(
            catalog_uri=SecretStr(url),
            warehouse_uri=f"file://{tmp_path}/warehouse",
            assets_uri=f"file://{tmp_path}/assets",
        )
        try:
            catalog = load_catalog(lake)
            tables = ensure_tables(catalog)
            ensure_tables(load_catalog(lake))  # a second start finds everything in place
            records = [
                BronzeRecord(make_hotspot(36.7 + 0.001 * i, 4.6, FIRE_START + dt.timedelta(minutes=i)),
                             "firms.hotspots.raw", 1, 100 + i)
                for i in range(4)
            ]  # fmt: skip
            assert BronzeHotspotWriter(catalog).append(records) == 4
            assert BronzeHotspotWriter(load_catalog(lake)).append(records[2:]) == 0
            table = load_catalog(lake).load_table(BRONZE_HOTSPOTS.identifier)
            assert table.scan().count() == 4
            assert read_committed_offsets(table) == {("firms.hotspots.raw", 1): 103}
            assert len(tables) == 5
            with psycopg.connect(dsn) as conn:
                registered = conn.execute(
                    "SELECT count(*) FROM iceberg_tables WHERE catalog_name = %s",
                    (lake.catalog_name,),
                ).fetchone()
            assert registered == (5,)
        finally:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("DROP TABLE IF EXISTS iceberg_tables, iceberg_namespace_properties")

"""The REST API against a real PostGIS (FastAPI TestClient, real connection pool)."""

from __future__ import annotations

import datetime as dt
import struct
import time
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from narsat.api import auth
from narsat.api.app import create_app
from narsat.api.routers import auth_routes
from narsat.config import ApiSettings, DbSettings, Settings
from narsat.db import connect
from narsat.quality import gates
from tests import synthetic
from tests.conftest import make_hotspot
from tests.integration.helpers import (
    FIRE_START,
    fire_over_synthetic_burn,
    square,
    store_and_cluster,
)

pytestmark = pytest.mark.integration
UTC = dt.UTC
PASSWORD = "a-long-test-password"
LON, LAT = synthetic.truth_20m().centre_lonlat


@pytest.fixture
def seeded(db_settings: DbSettings) -> dict[str, uuid.UUID]:
    """Two fires (one mapped), a gas flare, boundaries, two users and a mart row."""
    ids: dict[str, uuid.UUID] = {}
    oran = [
        make_hotspot(35.60 + 0.003 * i, -0.60, FIRE_START + dt.timedelta(days=20, hours=i))
        for i in range(3)
    ]
    flare = [make_hotspot(31.6712, 6.0703, FIRE_START - dt.timedelta(days=d)) for d in range(15)]
    with connect(db_settings) as conn:
        for iso3, level, code, name, lon, lat, half in [
            ("DZA", 0, "DZA", "Algeria", 3.0, 33.0, 6.0),
            ("DZA", 1, "DZ-06", "Béjaïa", 4.6, 36.7, 0.5),
            ("DZA", 1, "DZ-31", "Oran", -0.6, 35.6, 0.4),
        ]:
            conn.execute(
                "INSERT INTO admin_unit (country_iso3, level, code, name, source, licence, geom) "
                "VALUES (%(iso3)s, %(level)s, %(code)s, %(name)s, 'test', 'CC BY 4.0', "
                "ST_GeomFromText(%(wkt)s, 4326))",
                {
                    "iso3": iso3,
                    "level": level,
                    "code": code,
                    "name": name,
                    "wkt": square(lon, lat, half),
                },
            )
        store_and_cluster(conn, fire_over_synthetic_burn() + oran + flare)
        events = conn.execute(
            "SELECT event_id, n_hotspots FROM fire_event ORDER BY n_hotspots DESC"
        ).fetchall()
        ids["bejaia"], ids["oran"] = events[0]["event_id"], events[1]["event_id"]
        ids["burned"] = uuid.uuid4()
        conn.execute(
            """
            INSERT INTO burned_area (ba_id, event_id, method, model_version, pre_item_ids,
                                     post_item_ids, pre_datetime, post_datetime, threshold,
                                     clear_fraction, area_ha, n_polygons, geom)
            VALUES (%(ba)s, %(event)s, 'dnbr', 'dnbr-v1', '{S2A_PRE}', '{S2A_POST}',
                    '2023-07-10T10:30Z', '2023-07-28T10:30Z', 0.31, 0.97, 254.3, 1,
                    ST_Multi(ST_Buffer(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
                                       900)::geometry))
            """,
            {"ba": ids["burned"], "event": ids["bejaia"], "lon": LON, "lat": LAT},
        )
        conn.execute(
            "UPDATE fire_event SET mapping_status = 'mapped', mapped_at = now(), "
            "mapped_n_hotspots = n_hotspots WHERE event_id = %(id)s",
            {"id": ids["bejaia"]},
        )
        conn.execute(
            "INSERT INTO raster_asset (asset_id, event_id, kind, method, uri, epsg, width, height, "
            "bands, footprint) VALUES (gen_random_uuid(), %(e)s, 'post', NULL, "
            "'s3://narsat-lake/assets/events/x/post.tif', 32631, 250, 250, '{blue,green,red}', "
            "ST_MakeEnvelope(4.55, 36.70, 4.62, 36.76, 4326)), "
            "(gen_random_uuid(), %(e)s, 'mask', 'dnbr', 'file:///data/lake/events/x/dnbr_mask.tif', "
            "32631, 250, 250, '{burned}', ST_MakeEnvelope(4.55, 36.70, 4.62, 36.76, 4326))",
            {"e": ids["bejaia"]},
        )
        for email, role in (("analyst@example.org", "analyst"), ("admin@example.org", "admin")):
            conn.execute(
                "INSERT INTO app_user (email, password_hash, role) VALUES (%(e)s, %(h)s, %(r)s)",
                {"e": email, "h": auth.hash_password(PASSWORD), "r": role},
            )
        conn.execute(
            "INSERT INTO mart.burned_area_admin_year VALUES (2023, 'DZA', 'Béjaïa', 'dnbr', 1, 254.3)"
        )
        conn.execute(
            "INSERT INTO mart.fire_activity_daily VALUES ('2023-07-22', 'DZA', 'Béjaïa', 2, 1, 41.0),"
            " ('2023-07-23', 'DZA', 'Béjaïa', 4, 1, 98.0), ('2023-07-23', 'DZA', 'Oran', 1, 1, 9.0)"
        )
        gates.record(conn, [
            gates.CheckResult("silver.hotspots", "schema", "pass", observed={"rows": 11}),
            gates.CheckResult("silver.hotspots", "hotspots_with_country", "warn", "warn",
                              {"ratio": 0.8}),
        ], run_id="run-1")  # fmt: skip
    return ids


@pytest.fixture
def client(db_settings: DbSettings, seeded: dict[str, uuid.UUID]) -> Iterator[TestClient]:
    settings = Settings(
        db=db_settings,
        api=ApiSettings(jwt_secret=SecretStr("t" * 40), titiler_public_url="http://tiles.test"),
    )
    auth_routes._attempts.clear()
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _token(client: TestClient, email: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/token", data={"username": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


class TestSystem:
    def test_health_ready_and_metrics(self, client: TestClient) -> None:
        assert client.get("/health").json()["status"] == "ok"
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready", "schema_revision": "0001"}
        client.get("/api/v1/stats/summary")
        metrics = client.get("/metrics/").text
        assert 'narsat_api_requests_total{method="GET",route="/api/v1/stats/summary"' in metrics

    def test_openapi_document_lists_the_public_routes(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/events" in paths
        assert "/api/v1/tiles/{layer}/{z}/{x}/{y}.mvt" in paths
        assert "/health" not in paths  # operational endpoints are not part of the public API


class TestEvents:
    def test_list_is_geojson_without_static_sources_or_tiny_events(
        self, client: TestClient, seeded: dict[str, uuid.UUID]
    ) -> None:
        response = client.get("/api/v1/events")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/geo+json"
        body = response.json()
        assert body["type"] == "FeatureCollection"
        assert body["numberReturned"] == 2
        # Most recently active first: the Oran fire is three weeks younger.
        assert [f["id"] for f in body["features"]] == [str(seeded["oran"]), str(seeded["bejaia"])]
        bejaia = body["features"][1]
        assert bejaia["geometry"]["type"] == "Polygon"
        assert bejaia["properties"]["n_hotspots"] == 8
        assert bejaia["properties"]["admin1_name"] == "Béjaïa"
        assert bejaia["properties"]["burned_area_ha"] == {"dnbr": 254.3}
        assert bejaia["properties"]["mapping_status"] == "mapped"
        assert "geom" not in bejaia["properties"]

    def test_filters(self, client: TestClient, seeded: dict[str, uuid.UUID]) -> None:
        def ids(**params: object) -> list[str]:
            response = client.get("/api/v1/events", params=params)
            assert response.status_code == 200, response.text
            return [f["id"] for f in response.json()["features"]]

        assert ids(bbox="4,36,5,37.5") == [str(seeded["bejaia"])]
        assert ids(end="2023-07-31T00:00:00Z") == [str(seeded["bejaia"])]
        assert ids(start="2023-08-01T00:00:00") == [str(seeded["oran"])]  # naive -> UTC
        assert ids(mapping_status="mapped") == [str(seeded["bejaia"])]
        assert ids(min_hotspots=5) == [str(seeded["bejaia"])]
        assert ids(country="mar") == []
        assert len(ids(limit=1)) == 1
        assert ids(limit=1, offset=1) == [str(seeded["bejaia"])]
        centroid = client.get("/api/v1/events", params={"geometry": "centroid"}).json()
        assert {f["geometry"]["type"] for f in centroid["features"]} == {"Point"}

    def test_invalid_parameters_are_rejected_with_422(self, client: TestClient) -> None:
        assert client.get("/api/v1/events", params={"bbox": "1,2,3"}).status_code == 422
        assert client.get("/api/v1/events", params={"bbox": "5,36,4,37"}).status_code == 422
        assert client.get("/api/v1/events", params={"limit": 0}).status_code == 422
        assert client.get("/api/v1/events", params={"geometry": "blob"}).status_code == 422

    def test_detail_has_products_rasters_and_tile_templates(
        self, client: TestClient, seeded: dict[str, uuid.UUID]
    ) -> None:
        detail = client.get(f"/api/v1/events/{seeded['bejaia']}").json()
        assert detail["event_id"] == str(seeded["bejaia"])
        assert detail["hull"]["type"] == "Polygon"
        assert len(detail["bbox"]) == 4
        (product,) = detail["burned_areas"]
        assert (product["method"], product["area_ha"], product["qa_status"]) == (
            "dnbr",
            254.3,
            "auto",
        )
        assert product["pre_item_ids"] == ["S2A_PRE"]
        assert product["validation"] == []
        rasters = {(r["kind"], r["method"]): r for r in detail["rasters"]}
        assert rasters[("post", None)]["bounds"] == [4.55, 36.70, 4.62, 36.76]
        post = rasters[("post", None)]["tiles"]
        assert post["true_color"].startswith(
            "http://tiles.test/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3%3A%2F%2Fnarsat-lake"
        )
        assert "bidx=3&bidx=2&bidx=1" in post["true_color"]
        assert "bidx=6&bidx=4&bidx=3" in post["swir_false_color"]
        # A local lake is addressed by path, which is what TiTiler reads from a mounted volume.
        assert "url=%2Fdata%2Flake%2Fevents" in rasters[("mask", "dnbr")]["tiles"]["mask"]

    def test_unknown_or_malformed_event(self, client: TestClient) -> None:
        assert client.get(f"/api/v1/events/{uuid.uuid4()}").status_code == 404
        assert client.get("/api/v1/events/not-a-uuid").status_code == 422

    def test_event_hotspots_and_burned_areas(
        self, client: TestClient, seeded: dict[str, uuid.UUID]
    ) -> None:
        hotspots = client.get(f"/api/v1/events/{seeded['bejaia']}/hotspots").json()
        assert len(hotspots["features"]) == 8
        stamps = [f["properties"]["acq_ts"] for f in hotspots["features"]]
        assert stamps == sorted(stamps)
        assert hotspots["features"][0]["geometry"]["type"] == "Point"
        burned = client.get(f"/api/v1/events/{seeded['bejaia']}/burned-areas").json()
        assert [f["properties"]["method"] for f in burned["features"]] == ["dnbr"]
        assert burned["features"][0]["geometry"]["type"] == "MultiPolygon"
        empty = client.get(f"/api/v1/events/{seeded['oran']}/burned-areas").json()
        assert empty["features"] == []


class TestHotspots:
    def test_static_sources_are_hidden_unless_requested(self, client: TestClient) -> None:
        default = client.get("/api/v1/hotspots").json()
        assert default["numberReturned"] == 11
        everything = client.get("/api/v1/hotspots", params={"include_static": True}).json()
        assert everything["numberReturned"] == 26
        box = client.get("/api/v1/hotspots", params={"bbox": "-1,35,0,36"}).json()
        assert box["numberReturned"] == 3
        assert {f["properties"]["country_iso3"] for f in box["features"]} == {"DZA"}


def _tile_xy(z: int) -> tuple[int, int]:
    import math

    x = int((LON + 180.0) / 360.0 * 2**z)
    y = int((1.0 - math.asinh(math.tan(math.radians(LAT))) / math.pi) / 2.0 * 2**z)
    return x, y


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _fields(data: bytes) -> list[tuple[int, bytes]]:
    """Length-delimited fields of a protobuf message (enough to walk a vector tile)."""
    out, pos = [], 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if wire == 2:
            length, pos = _varint(data, pos)
            out.append((number, data[pos : pos + length]))
            pos += length
        elif wire == 0:
            _, pos = _varint(data, pos)
        else:
            pos += 4 if wire == 5 else 8
    return out


def count_features(tile: bytes) -> int:
    """Number of features in a Mapbox Vector Tile (Tile.layers = 3, Layer.features = 2)."""
    layers = [body for number, body in _fields(tile) if number == 3]
    return sum(1 for layer in layers for number, _ in _fields(layer) if number == 2)


class TestVectorTiles:
    def test_tiles_contain_data_where_the_fire_is(self, client: TestClient) -> None:
        z = 9
        x, y = _tile_xy(z)
        for layer in ("hotspots", "events", "burned_areas"):
            response = client.get(f"/api/v1/tiles/{layer}/{z}/{x}/{y}.mvt")
            assert response.status_code == 200, layer
            assert response.headers["content-type"] == "application/vnd.mapbox-vector-tile"
            assert response.headers["cache-control"] == "public, max-age=60"
            assert layer.encode() in response.content  # the layer name is embedded in the tile
            assert len(response.content) > 30

    def test_detections_are_thinned_to_one_per_screen_pixel_at_low_zoom(
        self, client: TestClient
    ) -> None:
        def features(z: int) -> int:
            x, y = _tile_xy(z)
            return count_features(client.get(f"/api/v1/tiles/hotspots/{z}/{x}/{y}.mvt").content)

        # From zoom 8 up every detection of the fire is in the tile...
        assert features(9) == features(8) == 8
        # ...below, the eight detections (within a kilometre) share one or two pixels...
        assert 1 <= features(7) < 8
        assert 1 <= features(4) <= 2
        # ...and the strongest detection (27 MW in the fixture) is the one that is kept.
        x, y = _tile_xy(4)
        tile = client.get(f"/api/v1/tiles/hotspots/4/{x}/{y}.mvt").content
        assert struct.pack("<d", 27.0) in tile

    def test_empty_tiles_time_filters_and_bounds(self, client: TestClient) -> None:
        assert client.get("/api/v1/tiles/hotspots/9/0/0.mvt").status_code == 204
        z = 9
        x, y = _tile_xy(z)
        later = client.get(
            f"/api/v1/tiles/hotspots/{z}/{x}/{y}.mvt", params={"start": "2024-01-01T00:00:00Z"}
        )
        assert later.status_code == 204
        assert client.get("/api/v1/tiles/hotspots/3/9/1.mvt").status_code == 404
        assert client.get("/api/v1/tiles/roads/3/1/1.mvt").status_code == 422
        other_method = client.get(
            f"/api/v1/tiles/burned_areas/{z}/{x}/{y}.mvt", params={"method": "model"}
        )
        assert other_method.status_code == 204


class TestStatisticsAndQuality:
    def test_summary(self, client: TestClient) -> None:
        summary = client.get("/api/v1/stats/summary").json()
        assert summary["hotspots"] == 11
        assert summary["static_hotspots"] == 15
        assert summary["events"] == 2
        assert summary["mapped_events"] == 1
        assert summary["burned_area_ha"] == {"dnbr": 254.3}
        # Timestamps are UTC whatever the time zone of the database server or of this host.
        assert summary["first_acquisition"] == "2023-07-08T12:00:00Z"
        assert summary["last_acquisition"].endswith("Z")

    def test_marts(self, client: TestClient) -> None:
        by_admin = client.get("/api/v1/stats/burned-area", params={"year": 2023}).json()
        assert by_admin == [
            {
                "fire_year": 2023,
                "country_iso3": "DZA",
                "admin1_name": "Béjaïa",
                "method": "dnbr",
                "n_events": 1,
                "area_ha": 254.3,
            }
        ]
        activity = client.get("/api/v1/stats/activity").json()
        assert [(a["activity_date"], a["n_hotspots"]) for a in activity] == [
            ("2023-07-22", 2), ("2023-07-23", 5),
        ]  # fmt: skip
        assert client.get("/api/v1/stats/activity", params={"country": "TUN"}).json() == []
        assert client.get("/api/v1/stats/events").json() == []

    def test_quality_and_freshness(self, client: TestClient) -> None:
        checks = client.get("/api/v1/quality/checks").json()
        assert [(c["check_name"], c["status"]) for c in checks] == [
            ("hotspots_with_country", "warn"), ("schema", "pass"),
        ]  # fmt: skip
        freshness = client.get("/api/v1/quality/freshness").json()
        assert freshness["mapping_queue"] == 1  # the Oran fire
        assert freshness["last_acquisition"].startswith("2023-08-11")
        assert freshness["last_quality_run"] is not None

    def test_platform_health_gauges_are_exported(self, client: TestClient) -> None:
        """The state left by the batch jobs, read from the database by the API."""
        import re

        def value(text: str, name: str) -> float:
            match = re.search(rf"^{re.escape(name)} (\S+)$", text, re.MULTILINE)
            assert match, name
            return float(match.group(1))

        text = ""
        for _ in range(50):  # the first refresh runs right after start-up
            text = client.get("/metrics/").text
            if "narsat_platform_metrics_refresh_timestamp_seconds" in text and value(
                text, "narsat_platform_metrics_refresh_timestamp_seconds"
            ):
                break
            time.sleep(0.1)
        assert value(text, "narsat_mapping_queue_events") == 1  # the Oran fire
        assert value(text, "narsat_mapping_failed_events") == 0
        assert value(text, 'narsat_dq_failed_checks{severity="error"}') == 0
        assert value(text, 'narsat_dq_failed_checks{severity="warn"}') == 1
        last = dt.datetime.fromtimestamp(
            value(text, "narsat_data_last_acquisition_timestamp_seconds"), tz=dt.UTC
        )
        assert last.date() == dt.date(2023, 8, 11)
        assert value(text, "narsat_dq_last_run_timestamp_seconds") > 1.7e9

    def test_admin_units(self, client: TestClient) -> None:
        units = client.get("/api/v1/admin-units", params={"country": "dza"}).json()
        assert [u["name"] for u in units] == ["Béjaïa", "Oran"]
        assert units[0]["licence"] == "CC BY 4.0"
        assert "geom" not in units[0]


class TestAuthAndQa:
    def test_login_and_identity(self, client: TestClient) -> None:
        headers = _token(client, "analyst@example.org")
        me = client.get("/api/v1/auth/me", headers=headers).json()
        assert (me["email"], me["role"]) == ("analyst@example.org", "analyst")
        # E-mail lookup is case-insensitive (citext).
        assert _token(client, "ANALYST@example.org")

    def test_wrong_credentials_get_the_same_answer_for_known_and_unknown_accounts(
        self, client: TestClient
    ) -> None:
        wrong = client.post(
            "/api/v1/auth/token", data={"username": "analyst@example.org", "password": "nope"}
        )
        unknown = client.post(
            "/api/v1/auth/token", data={"username": "ghost@example.org", "password": "nope"}
        )
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()

    def test_login_is_throttled(self, client: TestClient) -> None:
        for _ in range(auth_routes.MAX_ATTEMPTS):
            client.post("/api/v1/auth/token", data={"username": "x@example.org", "password": "y"})
        blocked = client.post(
            "/api/v1/auth/token", data={"username": "analyst@example.org", "password": PASSWORD}
        )
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "60"

    def test_qa_needs_a_token_and_is_recorded(
        self, client: TestClient, seeded: dict[str, uuid.UUID], db_settings: DbSettings
    ) -> None:
        url = f"/api/v1/burned-areas/{seeded['burned']}/qa"
        verdict = {"status": "rejected", "note": "harvested field, not a fire"}
        assert client.post(url, json=verdict).status_code == 401
        assert (
            client.post(url, json=verdict, headers={"Authorization": "Bearer nonsense"}).status_code
            == 401
        )

        headers = _token(client, "analyst@example.org")
        response = client.post(url, json=verdict, headers=headers)
        assert response.status_code == 200
        assert response.json()["qa_status"] == "rejected"
        with connect(db_settings) as conn:
            row = conn.execute(
                "SELECT b.qa_status, b.qa_note, u.email::text AS email, "
                "b.qa_at IS NOT NULL AS dated "
                "FROM burned_area b JOIN app_user u ON u.user_id = b.qa_by"
            ).fetchone()
        assert row == {
            "qa_status": "rejected",
            "qa_note": verdict["note"],
            "email": "analyst@example.org",
            "dated": True,
        }
        # Rejected polygons leave the public totals.
        assert client.get("/api/v1/stats/summary").json()["burned_area_ha"] == {}

        cleared = client.post(url, json={"status": "auto"}, headers=headers).json()
        assert (cleared["qa_status"], cleared["qa_at"]) == ("auto", None)
        assert client.post(url, json={"status": "maybe"}, headers=headers).status_code == 422
        missing = client.post(
            f"/api/v1/burned-areas/{uuid.uuid4()}/qa", json=verdict, headers=headers
        )
        assert missing.status_code == 404

    def test_requeue_is_admin_only(self, client: TestClient, seeded: dict[str, uuid.UUID]) -> None:
        url = f"/api/v1/events/{seeded['bejaia']}/remap"
        assert client.post(url).status_code == 401
        assert client.post(url, headers=_token(client, "analyst@example.org")).status_code == 403
        accepted = client.post(url, headers=_token(client, "admin@example.org"))
        assert accepted.status_code == 202
        assert accepted.json()["mapping_status"] == "pending"
        detail = client.get(f"/api/v1/events/{seeded['bejaia']}").json()
        assert detail["mapping_note"] == "re-queued by an administrator"

    def test_expired_tokens_are_refused(self, client: TestClient) -> None:
        user = auth.User(uuid.uuid4(), "analyst@example.org", "analyst")
        old = dt.datetime.now(UTC) - dt.timedelta(hours=3)
        token = auth.issue_token(user, "t" * 40, ttl_minutes=60, now=old)
        response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


def test_errors_do_not_leak_internals(db_settings: DbSettings) -> None:
    settings = Settings(db=db_settings, api=ApiSettings(jwt_secret=SecretStr("t" * 40)))
    with connect(db_settings) as conn:
        conn.execute("ALTER TABLE mart.event_summary RENAME TO event_summary_hidden")
    try:
        with TestClient(create_app(settings), raise_server_exceptions=False) as client:
            response = client.get("/api/v1/stats/events")
        assert response.status_code == 500
        assert response.json() == {"detail": "internal server error"}
        assert "event_summary" not in response.text
    finally:
        with connect(db_settings) as conn:
            conn.execute("ALTER TABLE mart.event_summary_hidden RENAME TO event_summary")


def test_largest_fires_are_ranked_by_the_dnbr_area(db_settings: DbSettings) -> None:
    """The model's larger areas must not push its false positives to the top."""
    settings = Settings(db=db_settings, api=ApiSettings(jwt_secret=SecretStr("t" * 40)))
    rows = [
        # name, dNBR ha, model ha
        ("model only", 0.0, 900.0),
        ("both, larger model", 300.0, 800.0),
        ("both, larger dnbr", 500.0, 400.0),
        ("dnbr only", 100.0, 0.0),
    ]
    with connect(db_settings) as conn:
        for name, dnbr, model in rows:
            conn.execute(
                """
                INSERT INTO mart.event_summary (event_id, country_iso3, admin1_name, first_seen,
                    last_seen, duration_hours, n_hotspots, frp_max_mw, area_dnbr_ha,
                    area_model_ha, method_iou)
                VALUES (gen_random_uuid(), 'DZA', %(name)s, '2023-07-24', '2023-07-24', 0, 5,
                        10, %(dnbr)s, %(model)s, NULL)
                """,
                {"name": name, "dnbr": dnbr, "model": model},
            )
    try:
        with TestClient(create_app(settings)) as client:
            events = client.get("/api/v1/stats/events").json()
        assert [e["admin1_name"] for e in events] == [
            "both, larger dnbr", "both, larger model", "dnbr only", "model only",
        ]  # fmt: skip
    finally:
        with connect(db_settings) as conn:
            conn.execute("DELETE FROM mart.event_summary")

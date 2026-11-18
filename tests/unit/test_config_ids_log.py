from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import SecretStr, ValidationError

from narsat import ids
from narsat.config import (
    ApiSettings,
    BoundingBox,
    DbSettings,
    LakeSettings,
    SegmentationSettings,
    Settings,
)
from narsat.log import redact

UTC = dt.UTC


class TestBoundingBox:
    def test_parse_and_contains(self) -> None:
        box = BoundingBox.parse("-13.5,27,11.8,37.6")
        assert box.as_tuple() == (-13.5, 27.0, 11.8, 37.6)
        assert box.contains(4.6, 36.7)
        assert not box.contains(14.0, 37.5)  # Sicily
        assert box.as_firms() == "-13.5,27,11.8,37.6"

    @pytest.mark.parametrize("text", ["1,2,3", "5,10,4,20", "0,50,10,40", "a,b,c,d"])
    def test_rejects_bad_boxes(self, text: str) -> None:
        with pytest.raises(ValueError):  # noqa: PT011 - pydantic and float() both raise it
            BoundingBox.parse(text)


class TestSettings:
    def test_defaults_are_usable_without_any_environment(self) -> None:
        settings = Settings()
        assert settings.env == "dev"
        assert settings.aoi.countries == ["DZA", "MAR", "TUN"]
        assert settings.firms.map_key is None
        assert settings.imagery.collection == "sentinel-2-c1-l2a"
        assert settings.seg.methods == ["dnbr"]

    def test_environment_overrides_and_csv_lists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NARSAT_AOI_BBOX", "-2,34,9,37.5")
        monkeypatch.setenv("NARSAT_AOI_COUNTRIES", "dza, tun")
        monkeypatch.setenv("NARSAT_FIRMS_SOURCES", "VIIRS_NOAA21_NRT")
        monkeypatch.setenv("NARSAT_FIRMS_MAP_KEY", "abcdef0123456789abcdef0123456789")
        monkeypatch.setenv("NARSAT_SEG_METHODS", "dnbr,model")
        monkeypatch.setenv("NARSAT_KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
        settings = Settings()
        assert settings.aoi.bbox.west == -2
        assert settings.aoi.countries == ["dza", "tun"]
        assert settings.firms.sources == ["VIIRS_NOAA21_NRT"]
        assert settings.seg.methods == ["dnbr", "model"]
        assert settings.kafka.bootstrap_servers == "kafka:9092"
        # The key never appears in a dump or repr.
        assert "abcdef0123456789" not in repr(settings)
        assert "abcdef0123456789" not in settings.model_dump_json()

    def test_unknown_segmentation_method_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SegmentationSettings(methods=["dnbr", "magic"])

    def test_day_range_is_bounded_like_the_firms_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NARSAT_FIRMS_DAY_RANGE", "6")
        with pytest.raises(ValidationError):
            Settings()

    def test_production_refuses_the_development_jwt_secret(self) -> None:
        with pytest.raises(ValidationError, match="NARSAT_API_JWT_SECRET"):
            Settings(env="prod")
        ok = Settings(env="prod", api=ApiSettings(jwt_secret=SecretStr("x" * 48)))
        assert ok.env == "prod"

    def test_sqlalchemy_url_uses_psycopg3(self) -> None:
        db = DbSettings(dsn=SecretStr("postgresql://u:p@db:5432/narsat"))
        assert db.sqlalchemy_url() == "postgresql+psycopg://u:p@db:5432/narsat"

    def test_lake_uri_scheme_is_validated(self) -> None:
        with pytest.raises(ValidationError):
            LakeSettings(warehouse_uri="/just/a/path")
        assert LakeSettings(warehouse_uri="s3://bucket/wh/").warehouse_uri == "s3://bucket/wh"


class TestIds:
    def test_hotspot_id_is_deterministic_and_ignores_processing_level(self) -> None:
        ts = dt.datetime(2023, 7, 23, 1, 34, tzinfo=UTC)
        a = ids.hotspot_id("VIIRS", "N20", ts, 36.70012, 4.60234)
        b = ids.hotspot_id("viirs", "n20", ts, 36.70012, 4.60234)
        assert a == b
        assert a.version == 5

    def test_hotspot_id_changes_with_any_key_part(self) -> None:
        ts = dt.datetime(2023, 7, 23, 1, 34, tzinfo=UTC)
        base = ids.hotspot_id("VIIRS", "N20", ts, 36.70012, 4.60234)
        assert base != ids.hotspot_id("VIIRS", "N21", ts, 36.70012, 4.60234)
        assert base != ids.hotspot_id(
            "VIIRS", "N20", ts + dt.timedelta(minutes=1), 36.70012, 4.60234
        )
        assert base != ids.hotspot_id("VIIRS", "N20", ts, 36.70013, 4.60234)

    def test_hotspot_id_is_the_same_in_any_timezone(self) -> None:
        utc = dt.datetime(2023, 7, 23, 1, 34, tzinfo=UTC)
        algiers = utc.astimezone(dt.timezone(dt.timedelta(hours=1)))
        assert ids.hotspot_id("VIIRS", "N20", utc, 1.0, 2.0) == ids.hotspot_id(
            "VIIRS", "N20", algiers, 1.0, 2.0
        )

    def test_naive_timestamps_are_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            ids.hotspot_id("VIIRS", "N20", dt.datetime(2023, 7, 23, 1, 34), 1.0, 2.0)

    def test_derived_ids_are_stable(self) -> None:
        seed = uuid.UUID("11111111-1111-5111-8111-111111111111")
        event = ids.event_id(seed)
        assert event == ids.event_id(seed)
        assert ids.burned_area_id(event, "dnbr") != ids.burned_area_id(event, "model")
        assert ids.asset_id(event, "mask", "dnbr") != ids.asset_id(event, "mask", "model")
        assert ids.asset_id(event, "post") == ids.asset_id(event, "post", None)


class TestRedaction:
    def test_firms_key_in_path_and_query(self) -> None:
        key = "0123456789abcdef0123456789abcdef"
        url = (
            f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/VIIRS_SNPP_NRT/-13,27,12,38/2"
        )
        assert key not in redact(url)
        assert "/api/area/csv/***/VIIRS_SNPP_NRT" in redact(url)
        assert key not in redact(f"GET /mapserver/mapkey_status/?MAP_KEY={key}")

    def test_password_in_connection_url(self) -> None:
        assert redact("postgresql://narsat:s3cret@db:5432/narsat") == (
            "postgresql://narsat:***@db:5432/narsat"
        )

    def test_plain_text_is_untouched(self) -> None:
        assert redact("36 hotspots published") == "36 hotspots published"

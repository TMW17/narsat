from __future__ import annotations

import datetime as dt
import io
import json
import uuid
from pathlib import Path

import httpx2 as httpx
import numpy as np
import pytest
from typer.testing import CliRunner

from narsat.api import auth
from narsat.cli import app
from narsat.config import BoundingBox
from narsat.segmentation.client import (
    InferenceClient,
    InferenceError,
    InferenceUnavailableError,
)
from narsat.streaming.runner import merge_ranges
from narsat.streaming.sink import Incoming, validate_message
from tests.conftest import make_hotspot

UTC = dt.UTC
BOX = BoundingBox(west=-13.5, south=27.0, east=11.8, north=37.6)
NOW = dt.datetime(2023, 7, 24, 6, 0, tzinfo=UTC)
T0 = dt.datetime(2023, 7, 23, 1, 34, tzinfo=UTC)


def _incoming(payload: dict | bytes | None, key: bytes | None = None) -> Incoming:
    value = payload if isinstance(payload, bytes | type(None)) else json.dumps(payload).encode()
    return Incoming("firms.hotspots.raw", 0, 7, key, value)


def _valid_payload() -> dict:
    return json.loads(make_hotspot(36.70012, 4.60234, T0).model_dump_json())


class TestMessageContract:
    def test_valid_message_is_parsed(self) -> None:
        payload = _valid_payload()
        message = validate_message(_incoming(payload, payload["hotspot_id"].encode()), BOX, NOW)
        assert not isinstance(message, str)
        assert message.latitude == pytest.approx(36.70012)

    @pytest.mark.parametrize(
        ("mutation", "reason"),
        [
            (lambda p: p.pop("acq_ts"), "schema: acq_ts"),
            (lambda p: p.update(latitude=123.0), "schema: latitude"),
            (lambda p: p.update(confidence_level="certain"), "schema: confidence_level"),
            (lambda p: p.update(unexpected="field"), "schema: unexpected"),
            (lambda p: p.update(schema_version=2), "schema: schema_version"),
            (lambda p: p.update(frp_mw=-1.0), "schema: frp_mw"),
            (lambda p: p.update(hotspot_id=str(uuid.uuid4())), "natural key"),
        ],
    )
    def test_contract_violations_are_named(self, mutation, reason: str) -> None:  # type: ignore[no-untyped-def]
        payload = _valid_payload()
        mutation(payload)
        outcome = validate_message(_incoming(payload), BOX, NOW)
        assert isinstance(outcome, str)
        assert reason in outcome

    def test_empty_and_non_json_payloads(self) -> None:
        assert validate_message(_incoming(None), BOX, NOW) == "empty payload"
        outcome = validate_message(_incoming(b"\xff\xfe not json"), BOX, NOW)
        assert isinstance(outcome, str)
        assert outcome.startswith("schema:")

    def test_key_must_match_the_identifier(self) -> None:
        outcome = validate_message(_incoming(_valid_payload(), b"something-else"), BOX, NOW)
        assert outcome == "record key differs from hotspot_id"

    def test_outside_area_of_interest(self) -> None:
        sicily = json.loads(make_hotspot(37.5, 14.0, T0).model_dump_json())
        assert validate_message(_incoming(sicily), BOX, NOW) == "outside the area of interest"

    def test_acquisition_in_the_future(self) -> None:
        future = json.loads(make_hotspot(36.7, 4.6, NOW + dt.timedelta(hours=5)).model_dump_json())
        assert validate_message(_incoming(future), BOX, NOW) == "acquisition time is in the future"
        soon = json.loads(make_hotspot(36.7, 4.6, NOW + dt.timedelta(minutes=30)).model_dump_json())
        assert not isinstance(validate_message(_incoming(soon), BOX, NOW), str)


def test_merge_ranges() -> None:
    a = (T0, T0 + dt.timedelta(hours=2))
    b = (T0 - dt.timedelta(hours=1), T0 + dt.timedelta(hours=1))
    assert merge_ranges(None, None) is None
    assert merge_ranges(a, None) == a
    assert merge_ranges(None, b) == b
    assert merge_ranges(a, b) == (b[0], a[1])


class TestAuth:
    USER = auth.User(
        uuid.UUID("11111111-2222-4333-8444-555555555555"), "analyst@example.org", "analyst"
    )
    SECRET = "s" * 40

    def test_password_hash_round_trip(self) -> None:
        hashed = auth.hash_password("correct horse battery")
        assert hashed.startswith("$argon2id$")
        assert auth.verify_password("correct horse battery", hashed)
        assert not auth.verify_password("wrong", hashed)
        assert not auth.verify_password("anything", "not-a-hash")
        assert not auth.needs_rehash(hashed)

    def test_token_round_trip(self) -> None:
        token = auth.issue_token(self.USER, self.SECRET, ttl_minutes=5)
        assert auth.read_token(token, self.SECRET) == self.USER

    def test_expired_token(self) -> None:
        past = dt.datetime.now(UTC) - dt.timedelta(hours=2)
        token = auth.issue_token(self.USER, self.SECRET, ttl_minutes=5, now=past)
        with pytest.raises(auth.TokenError):
            auth.read_token(token, self.SECRET)

    def test_wrong_key_and_garbage(self) -> None:
        token = auth.issue_token(self.USER, self.SECRET, ttl_minutes=5)
        with pytest.raises(auth.TokenError):
            auth.read_token(token, "another-secret-another-secret-another")
        with pytest.raises(auth.TokenError):
            auth.read_token("not.a.token", self.SECRET)

    def test_unsigned_tokens_are_refused(self) -> None:
        import jwt

        forged = jwt.encode(
            {
                "sub": str(self.USER.user_id),
                "role": "admin",
                "iss": "narsat-api",
                "exp": dt.datetime.now(UTC) + dt.timedelta(hours=1),
            },
            None,
            algorithm="none",
        )
        with pytest.raises(auth.TokenError):
            auth.read_token(forged, self.SECRET)

    def test_unknown_role_is_refused(self) -> None:
        import jwt

        token = jwt.encode(
            {
                "sub": str(self.USER.user_id),
                "role": "root",
                "iss": "narsat-api",
                "exp": dt.datetime.now(UTC) + dt.timedelta(hours=1),
            },
            self.SECRET,
            algorithm="HS256",
        )
        with pytest.raises(auth.TokenError):
            auth.read_token(token, self.SECRET)


def _npy(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, array, allow_pickle=False)
    return buffer.getvalue()


class TestInferenceClient:
    def test_segment_round_trip(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            cube = np.load(io.BytesIO(request.content), allow_pickle=False)
            seen["shape"], seen["dtype"] = cube.shape, cube.dtype
            seen["content_type"] = request.headers["content-type"]
            return httpx.Response(200, content=_npy(cube.mean(axis=0).astype("float32")))

        client = InferenceClient("http://inference:8090", transport=httpx.MockTransport(handler))
        cube = np.full((6, 8, 9), 0.25, dtype="float64")
        probability = client.segment(cube)
        assert probability.shape == (8, 9)
        assert probability.dtype == np.float32
        assert seen == {
            "shape": (6, 8, 9),
            "dtype": np.dtype("float32"),
            "content_type": "application/x-npy",
        }

    def test_model_info(self) -> None:
        body = {
            "name": "Prithvi-EO-2.0-300M-BurnScars",
            "version": "1",
            "engine": "prithvi",
            "bands": ["blue"],
            "resolution_m": 30,
            "device": "cpu",
        }
        client = InferenceClient(
            "http://x", transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
        )
        info = client.model_info()
        assert (info.name, info.resolution_m, info.device) == (body["name"], 30.0, "cpu")

    def test_wrong_output_shape_is_an_error(self) -> None:
        client = InferenceClient(
            "http://x",
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, content=_npy(np.zeros((2, 2), dtype="float32")))
            ),
        )
        with pytest.raises(InferenceError, match="shape"):
            client.segment(np.zeros((6, 8, 9), dtype="float32"))

    def test_client_errors_are_not_retried(self) -> None:
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(422, text="expected 6 bands")

        client = InferenceClient("http://x", transport=httpx.MockTransport(handler))
        with pytest.raises(InferenceError, match="422"):
            client.segment(np.zeros((6, 4, 4), dtype="float32"))
        assert calls["n"] == 1

    def test_unavailable_service_is_retried_then_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("tenacity.nap.time.sleep", lambda _s: None)
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503)

        client = InferenceClient("http://x", transport=httpx.MockTransport(handler))
        with pytest.raises(InferenceUnavailableError):
            client.model_info()
        assert calls["n"] == 4

    def test_cube_must_be_three_dimensional(self) -> None:
        client = InferenceClient(
            "http://x", transport=httpx.MockTransport(lambda _r: httpx.Response(200))
        )
        with pytest.raises(ValueError, match="bands, height, width"):
            client.segment(np.zeros((4, 4), dtype="float32"))


class TestCli:
    runner = CliRunner()

    def test_version(self) -> None:
        result = self.runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert result.stdout.strip()

    def test_config_masks_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NARSAT_FIRMS_MAP_KEY", "0123456789abcdef0123456789abcdef")
        monkeypatch.setenv("NARSAT_DB_DSN", "postgresql://narsat:hunter2@db/narsat")
        result = self.runner.invoke(app, ["config"])
        assert result.exit_code == 0
        assert "0123456789abcdef" not in result.stdout
        assert "hunter2" not in result.stdout
        config = json.loads(result.stdout)
        assert config["firms"]["map_key"] == "**********"
        assert config["imagery"]["collection"] == "sentinel-2-c1-l2a"

    def test_export_schemas(self, tmp_path: Path) -> None:
        result = self.runner.invoke(app, ["export-schemas", "--out", str(tmp_path / "schemas")])
        assert result.exit_code == 0
        schema = json.loads((tmp_path / "schemas" / "hotspot.v1.schema.json").read_text())
        assert schema["additionalProperties"] is False
        assert {"hotspot_id", "acq_ts", "latitude", "longitude"} <= set(schema["required"])
        assert (tmp_path / "schemas" / "fire_event.v1.schema.json").exists()

    def test_firms_commands_explain_a_missing_key(self) -> None:
        result = self.runner.invoke(app, ["firms", "check"])
        assert result.exit_code != 0
        assert "NARSAT_FIRMS_MAP_KEY" in str(result.exception)

    def test_bad_dates_are_rejected_before_any_work(self) -> None:
        result = self.runner.invoke(
            app,
            [
                "firms",
                "backfill",
                "--source",
                "VIIRS_SNPP_SP",
                "--start",
                "23/07/2023",
                "--end",
                "2023-07-28",
            ],
        )
        assert result.exit_code != 0

    def test_lake_init_and_info_on_a_local_lake(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NARSAT_LAKE_CATALOG_URI", f"sqlite:///{tmp_path}/lake/catalog.db")
        monkeypatch.setenv("NARSAT_LAKE_WAREHOUSE_URI", f"file://{tmp_path}/lake/warehouse")
        monkeypatch.setenv("NARSAT_LAKE_ASSETS_URI", f"file://{tmp_path}/lake/assets")
        init = self.runner.invoke(app, ["lake", "init"])
        assert init.exit_code == 0, init.output
        assert "table ready: bronze.firms_hotspots" in init.stdout
        info = self.runner.invoke(app, ["lake", "info"])
        assert info.exit_code == 0, info.output
        report = json.loads(info.stdout)
        assert report["silver.hotspots"]["rows"] == 0
        assert set(report) == {
            "bronze.firms_hotspots",
            "silver.hotspots",
            "silver.fire_events",
            "gold.burned_areas",
            "gold.burned_area_validation",
        }

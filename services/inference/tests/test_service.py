"""Tiling, the baseline engine and the HTTP contract. No deep-learning framework needed."""

from __future__ import annotations

import io

import numpy as np
import pytest
from fastapi.testclient import TestClient

from narsat_inference.app import create_app
from narsat_inference.config import InferenceSettings
from narsat_inference.engines import BANDS, EngineInfo, NbrBaselineEngine
from narsat_inference.tiling import blend_window, predict_tiled, tile_starts

VEGETATION = (0.03, 0.06, 0.05, 0.35, 0.18, 0.08)
BURNED = (0.04, 0.05, 0.06, 0.10, 0.22, 0.20)


def _npy(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, array, allow_pickle=False)
    return buffer.getvalue()


def _scene(height: int = 40, width: int = 50) -> np.ndarray:
    cube = np.empty((6, height, width), dtype="float32")
    for band in range(6):
        cube[band] = VEGETATION[band]
        cube[band, 10:20, 10:30] = BURNED[band]
    return cube


class TestTiling:
    @pytest.mark.parametrize(
        ("length", "tile", "overlap", "expected"),
        [
            (300, 512, 64, [0]),
            (512, 512, 64, [0]),
            (600, 512, 64, [0, 88]),
            (1000, 512, 64, [0, 448, 488]),
            (1024, 512, 0, [0, 512]),
        ],
    )
    def test_tile_starts_cover_the_raster(
        self, length: int, tile: int, overlap: int, expected: list[int]
    ) -> None:
        starts = tile_starts(length, tile, overlap)
        assert starts == expected
        covered = np.zeros(max(length, tile), dtype=bool)
        for start in starts:
            covered[start : start + tile] = True
        assert covered[:length].all()

    def test_overlap_must_leave_a_stride(self) -> None:
        with pytest.raises(ValueError, match="overlap"):
            tile_starts(1000, 64, 64)

    def test_blend_window_is_one_in_the_centre_and_positive_everywhere(self) -> None:
        window = blend_window(64, 16)
        assert window.shape == (64, 64)
        assert window[32, 32] == 1.0
        assert 0 < window[0, 0] < window[8, 8] < 1
        assert np.array_equal(blend_window(8, 0), np.ones((8, 8), dtype="float32"))

    def test_identity_model_is_reproduced_exactly_across_tiles(self) -> None:
        rng = np.random.default_rng(0)
        cube = rng.random((6, 150, 210), dtype="float32")
        # A "model" that returns band 0: blending must give band 0 back, seams included.
        result = predict_tiled(cube, lambda batch: batch[:, 0], tile=64, overlap=16, batch_size=3)
        assert result.shape == (150, 210)
        assert np.allclose(result, cube[0], atol=1e-6)

    def test_rasters_smaller_than_a_tile_are_padded_and_cropped(self) -> None:
        cube = np.full((6, 20, 30), 0.5, dtype="float32")
        seen: list[tuple[int, ...]] = []

        def model(batch: np.ndarray) -> np.ndarray:
            seen.append(batch.shape)
            return batch[:, 0]

        result = predict_tiled(cube, model, tile=64, overlap=16)
        assert seen == [(1, 6, 64, 64)]
        assert result.shape == (20, 30)
        assert np.allclose(result, 0.5)

    def test_batches_respect_the_batch_size(self) -> None:
        sizes: list[int] = []

        def model(batch: np.ndarray) -> np.ndarray:
            sizes.append(len(batch))
            return batch[:, 0]

        predict_tiled(np.zeros((6, 100, 100), dtype="float32"), model, 64, 16, batch_size=3)
        assert sizes == [3, 1]  # 2 x 2 tiles

    def test_wrong_model_output_shape_is_detected(self) -> None:
        with pytest.raises(ValueError, match="model returned shape"):
            predict_tiled(np.zeros((6, 64, 64), dtype="float32"), lambda b: b[:, 0, :8, :8], 64, 0)

    def test_input_must_be_three_dimensional(self) -> None:
        with pytest.raises(ValueError, match="bands, height, width"):
            predict_tiled(np.zeros((64, 64), dtype="float32"), lambda b: b, 64, 0)


class TestNbrBaseline:
    def test_burned_pixels_score_high_and_vegetation_low(self) -> None:
        probability = NbrBaselineEngine().predict(_scene())
        assert probability.shape == (40, 50)
        assert probability.dtype == np.float32
        assert probability[10:20, 10:30].min() > 0.95
        assert np.delete(probability, np.s_[10:20], axis=0).max() < 0.01

    def test_missing_input_gives_zero_not_nan(self) -> None:
        cube = _scene()
        cube[:, 0:5, 0:5] = np.nan
        probability = NbrBaselineEngine().predict(cube)
        assert np.isfinite(probability).all()
        assert (probability[0:5, 0:5] == 0).all()


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app(InferenceSettings(engine="nbr", max_pixels=10_000))) as test_client:
        yield test_client


class TestHttpContract:
    def test_health_and_model_description(self, client: TestClient) -> None:
        assert client.get("/health").json()["status"] == "ok"
        model = client.get("/v1/model").json()
        assert model["engine"] == "nbr"
        assert model["bands"] == list(BANDS)
        assert model["resolution_m"] == 30.0
        assert model["device"] == "cpu"

    def test_segment_round_trip(self, client: TestClient) -> None:
        response = client.post(
            "/v1/segment", content=_npy(_scene()), headers={"Content-Type": "application/x-npy"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-npy"
        assert float(response.headers["x-inference-seconds"]) >= 0
        probability = np.load(io.BytesIO(response.content), allow_pickle=False)
        assert probability.shape == (40, 50)
        assert probability.dtype == np.float32
        assert probability[15, 20] > 0.95

    @pytest.mark.parametrize(
        ("body", "status_code", "fragment"),
        [
            (b"not an npy file", 400, "not a valid .npy"),
            (_npy(np.zeros((3, 8, 8), dtype="float32")), 422, "expected shape (6, height, width)"),
            (_npy(np.zeros((6, 8), dtype="float32")), 422, "expected shape"),
            (_npy(np.zeros((6, 8, 8), dtype="int16")), 422, "floating-point"),
            (_npy(np.full((6, 8, 8), 1500.0, dtype="float32")), 422, "digital numbers"),
            (_npy(np.zeros((6, 200, 200), dtype="float32")), 413, "limit"),
        ],
    )
    def test_bad_requests_are_explained(
        self, client: TestClient, body: bytes, status_code: int, fragment: str
    ) -> None:
        response = client.post("/v1/segment", content=body)
        assert response.status_code == status_code
        assert fragment in response.json()["detail"]

    def test_pickled_payloads_are_never_loaded(self, client: TestClient) -> None:
        buffer = io.BytesIO()
        np.save(buffer, np.array([{"a": 1}], dtype=object), allow_pickle=True)
        response = client.post("/v1/segment", content=buffer.getvalue())
        assert response.status_code == 400

    def test_a_custom_engine_can_be_injected(self) -> None:
        class Constant:
            info = EngineInfo("constant", "0", "test", 30.0, "cpu")

            def predict(self, cube: np.ndarray) -> np.ndarray:
                return np.full(cube.shape[1:], 0.25, dtype="float32")

        app = create_app(InferenceSettings(engine="nbr"), engine_factory=lambda _s: Constant())
        with TestClient(app) as test_client:
            assert test_client.get("/v1/model").json()["name"] == "constant"
            response = test_client.post("/v1/segment", content=_npy(_scene()))
            assert np.load(io.BytesIO(response.content)).mean() == pytest.approx(0.25)

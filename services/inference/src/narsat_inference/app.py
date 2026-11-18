"""HTTP API of the inference service.

    GET  /health      liveness
    GET  /v1/model    which model answers, on which device
    POST /v1/segment  reflectance cube (.npy, float32, bands x height x width)
                      -> burn probability (.npy, float32, height x width)

The payload is a NumPy ``.npy`` file rather than JSON: a 1,000 x 1,000 scene is
24 MB of binary and would be several times that as text. ``allow_pickle`` is off in
both directions, so a request body can never execute code.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict

import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool

from narsat_inference import __version__
from narsat_inference.config import InferenceSettings
from narsat_inference.engines import BANDS, Engine, NbrBaselineEngine

log = logging.getLogger("narsat_inference")

NPY = "application/x-npy"


def build_engine(settings: InferenceSettings) -> Engine:
    if settings.engine == "nbr":
        return NbrBaselineEngine()
    # Imported here: PyTorch and TerraTorch are only needed for this engine.
    from narsat_inference.prithvi import PrithviBurnScarsEngine

    return PrithviBurnScarsEngine(settings)


def _read_cube(body: bytes, settings: InferenceSettings) -> np.ndarray:
    try:
        cube = np.load(io.BytesIO(body), allow_pickle=False)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"body is not a valid .npy array: {exc}"
        ) from exc
    if cube.ndim != 3 or cube.shape[0] != len(BANDS):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"expected shape ({len(BANDS)}, height, width) with bands {list(BANDS)}, "
            f"got {tuple(cube.shape)}",
        )
    if cube.dtype.kind != "f":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"expected floating-point reflectance in 0-1 units, got dtype {cube.dtype}",
        )
    if cube.shape[1] * cube.shape[2] > settings.max_pixels:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"raster has {cube.shape[1] * cube.shape[2]} pixels; the limit is "
            f"{settings.max_pixels}",
        )
    finite = cube[np.isfinite(cube)]
    if finite.size and float(finite.max()) > 2.0:
        # A common mistake: sending digital numbers (0-10000) instead of reflectance.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "values look like digital numbers; send reflectance in 0-1 units",
        )
    return cube.astype("float32", copy=False)


def create_app(
    settings: InferenceSettings | None = None,
    engine_factory: Callable[[InferenceSettings], Engine] = build_engine,
) -> FastAPI:
    settings = settings or InferenceSettings()
    # One request at a time on the model: predictable memory use on a laptop.
    lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        started = time.perf_counter()
        app.state.engine = await run_in_threadpool(engine_factory, settings)
        log.info(
            "engine ready: %s (%.1fs)", asdict(app.state.engine.info), time.perf_counter() - started
        )
        yield

    app = FastAPI(
        title="NarSat inference service",
        version=__version__,
        summary="Burn-scar probability from Sentinel-2 surface reflectance.",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/model")
    async def model(request: Request) -> dict[str, object]:
        info = request.app.state.engine.info
        return {
            **asdict(info),
            "bands": list(BANDS),
            "tile_size": settings.tile_size,
            "tile_overlap": settings.tile_overlap,
        }

    @app.post(
        "/v1/segment",
        response_class=Response,
        responses={200: {"content": {NPY: {}}, "description": "float32 (height, width)"}},
    )
    async def segment(request: Request) -> Response:
        declared = int(request.headers.get("content-length", "0") or 0)
        if declared > settings.max_body_mb * 1024 * 1024:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "request body too large")
        cube = _read_cube(await request.body(), settings)
        engine: Engine = request.app.state.engine

        def run() -> np.ndarray:
            with lock:
                return engine.predict(cube)

        started = time.perf_counter()
        probability = await run_in_threadpool(run)
        elapsed = time.perf_counter() - started
        log.info("segmented %dx%d in %.2fs", cube.shape[2], cube.shape[1], elapsed)
        buffer = io.BytesIO()
        np.save(buffer, probability.astype("float32"), allow_pickle=False)
        return Response(
            buffer.getvalue(),
            media_type=NPY,
            headers={"X-Inference-Seconds": f"{elapsed:.3f}"},
        )

    return app

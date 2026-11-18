"""FastAPI application factory.

``create_app`` wires the connection pool, the routers, CORS and the request metrics.
Interactive documentation is served at ``/docs`` and the OpenAPI document at
``/openapi.json``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram, make_asgi_app
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from narsat import __version__
from narsat.api import platform_metrics
from narsat.api.routers import auth_routes, events, hotspots, stats, tiles
from narsat.config import Settings, get_settings
from narsat.db import SESSION_OPTIONS
from narsat.log import configure_logging, get_logger

log = get_logger(__name__)

HTTP_REQUESTS = Counter("narsat_api_requests_total", "API requests", ["method", "route", "status"])
HTTP_SECONDS = Histogram(
    "narsat_api_request_seconds",
    "API request duration",
    ["route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)


API_PREFIX = "/api/v1"
#: How often the platform-health gauges are read from the database (seconds).
PLATFORM_METRICS_INTERVAL_S = 30.0


def _route_template(request: Request) -> str:
    """The route pattern (``/api/v1/events/{event_id}``), never the concrete path.

    Metric labels must have bounded cardinality: one series per route, not per event.
    The matched route only knows its path relative to the router it was declared on,
    so the API prefix is put back when the request came in under it.
    """
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if template is None:
        return "unmatched"
    prefixed = request.url.path.startswith(API_PREFIX) and not template.startswith(API_PREFIX)
    return f"{API_PREFIX}{template}" if prefixed else str(template)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            settings.db.dsn.get_secret_value(),
            min_size=settings.db.pool_min_size,
            max_size=settings.db.pool_max_size,
            kwargs={
                "row_factory": dict_row,
                "application_name": "narsat-api",
                "options": SESSION_OPTIONS,
            },
            open=False,
        )
        await pool.open()
        app.state.pool = pool
        refresher = asyncio.create_task(
            platform_metrics.refresh_forever(pool, PLATFORM_METRICS_INTERVAL_S)
        )
        log.info("api_started", version=__version__, env=settings.env)
        try:
            yield
        finally:
            refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresher
            await pool.close()

    app = FastAPI(
        title="NarSat API",
        version=__version__,
        summary="Wildfire hotspots, fire events and burned areas in the Maghreb.",
        description=(
            "Active-fire data courtesy of NASA FIRMS. Contains modified Copernicus Sentinel "
            "data. Burned-area products are automated estimates, not official statistics."
        ),
        lifespan=lifespan,
        openapi_tags=[
            {"name": "events", "description": "Fire events and burned-area products"},
            {"name": "hotspots", "description": "Satellite thermal detections"},
            {"name": "tiles", "description": "Mapbox Vector Tiles"},
            {"name": "statistics", "description": "Aggregates from the lakehouse marts"},
            {"name": "data quality", "description": "Quality gates and freshness"},
            {"name": "auth", "description": "Tokens for analysts and administrators"},
        ],
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def observe(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            route = _route_template(request)
            HTTP_REQUESTS.labels(request.method, route, str(status_code)).inc()
            HTTP_SECONDS.labels(route).observe(time.perf_counter() - started)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals (SQL, paths) to clients; the trace goes to the log.
        log.exception("api_unhandled_error", path=request.url.path, error=type(exc).__name__)
        return JSONResponse(
            {"detail": "internal server error"}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

    @app.get("/health", tags=["system"], include_in_schema=False)
    async def health() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok", "version": __version__}

    @app.get("/ready", tags=["system"], include_in_schema=False)
    async def ready(request: Request) -> JSONResponse:
        """Readiness: the database answers and the schema is migrated."""
        try:
            async with request.app.state.pool.connection() as conn:
                row = await (
                    await conn.execute("SELECT version_num FROM alembic_version")
                ).fetchone()
            return JSONResponse({"status": "ready", "schema_revision": row["version_num"]})
        except Exception as exc:
            return JSONResponse(
                {"status": "not ready", "reason": type(exc).__name__},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

    for module in (events, hotspots, tiles, stats, auth_routes):
        app.include_router(module.router, prefix=API_PREFIX)
    app.mount("/metrics", make_asgi_app())
    return app

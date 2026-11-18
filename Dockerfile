# syntax=docker/dockerfile:1
#
# NarSat platform image: REST API, FIRMS poller, Kafka-to-lake consumer and the
# `narsat` command line (migrations, mapping, lake maintenance).
#
#   docker build -t narsat .
#
# Dependencies come from uv.lock, so the image contains exactly the versions that were
# tested. Every binary dependency (GDAL in rasterio and pyogrio, librdkafka in
# confluent-kafka, libpq in psycopg) ships inside its wheel, apart from one small system
# library (expat), and the same file builds natively on Apple Silicon (arm64) and x86-64.

ARG PYTHON_IMAGE=python:3.13-slim-trixie
ARG UV_VERSION=0.11.32

FROM ${PYTHON_IMAGE} AS build
ARG UV_VERSION
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/narsat \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
WORKDIR /src

# Dependencies first: this layer is rebuilt only when the lock file changes.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project \
        --extra imagery --extra api --extra vector

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
        --extra imagery --extra api --extra vector

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="narsat" \
      org.opencontainers.image.description="Maghreb wildfire and burned-area EO lakehouse" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PATH=/opt/narsat/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NARSAT_LOG_JSON=true \
    # GDAL reads Sentinel-2 COGs over HTTP: no directory listings, and keep connections.
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    GDAL_HTTP_MULTIPLEX=YES \
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES=YES \
    VSI_CACHE=TRUE

# rasterio's wheels bundle GDAL but link to the system's expat XML library, which slim
# images do not include.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 narsat \
    && useradd --system --uid 10001 --gid narsat --home-dir /app --shell /usr/sbin/nologin narsat \
    && mkdir -p /app/var \
    && chown -R narsat:narsat /app

COPY --from=build /opt/narsat /opt/narsat

WORKDIR /app
USER narsat
EXPOSE 8000

# The API's liveness endpoint; the other roles of this image override or disable it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

CMD ["narsat", "serve-api", "--host", "0.0.0.0", "--port", "8000"]

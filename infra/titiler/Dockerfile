# syntax=docker/dockerfile:1
#
# TiTiler: renders map tiles from the Cloud-Optimized GeoTIFFs of the lake.
#
# Built from the PyPI release instead of the upstream image, so that the version is the
# one NarSat was tested with (tests/integration/test_titiler_tiles.py) and the image
# builds natively on arm64 and x86-64 (rasterio wheels bundle GDAL).
FROM python:3.13-slim-trixie

ARG TITILER_VERSION=2.4.1
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Only Cloud-Optimized GeoTIFFs are served: no directory listings, no other formats.
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif \
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES=YES \
    GDAL_HTTP_MULTIPLEX=YES \
    VSI_CACHE=TRUE \
    VSI_CACHE_SIZE=50000000 \
    GDAL_CACHEMAX=200 \
    # The STAC, mosaic and multidimensional endpoints are not used by NarSat.
    TITILER_API_DISABLE_STAC=TRUE \
    TITILER_API_DISABLE_MOSAIC=TRUE \
    TITILER_API_DISABLE_ZARR=TRUE \
    TITILER_API_TELEMETRY_ENABLED=FALSE

# rasterio's wheels bundle GDAL but link to the system's expat XML library, which slim
# images do not include.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install "titiler.application==${TITILER_VERSION}" "uvicorn[standard]>=0.54,<1" \
    && groupadd --system --gid 10001 titiler \
    && useradd --system --uid 10001 --gid titiler --no-create-home --shell /usr/sbin/nologin titiler

USER titiler
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
CMD ["uvicorn", "titiler.application.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]

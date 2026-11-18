# syntax=docker/dockerfile:1
#
# NarSat inference service: Prithvi-EO-2.0 burn-scar segmentation on CPU.
#
#   docker build -t narsat-inference services/inference
#
# A container on macOS cannot use the Apple GPU. On a MacBook the service is several
# times faster when run natively with MPS (see README.md); this image is for Linux
# hosts and for running the whole stack with one command.
FROM python:3.15-slim-trixie

# Since PyTorch 2.11 the default Linux wheels (x86-64 and arm64) bundle the NVIDIA CUDA
# libraries, several gigabytes that a CPU container never uses. The CPU wheel index
# published by the PyTorch project avoids them. Pass an empty value to use PyPI.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1

# rasterio (pulled in by TerraTorch) bundles GDAL but link to the system's expat XML library, which slim
# images do not include.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 narsat \
    && useradd --system --uid 10001 --gid narsat --home-dir /app --shell /usr/sbin/nologin narsat \
    && mkdir -p /app /models \
    && chown narsat:narsat /app /models

WORKDIR /app

RUN if [ -n "${TORCH_INDEX_URL}" ]; then \
      pip install --index-url "${TORCH_INDEX_URL}" "torch>=2.15,<3" torchvision; \
    fi

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[prithvi]"

USER narsat
EXPOSE 8080
# The checkpoint (about 1.3 GB) is downloaded on first start into the /models volume.
VOLUME ["/models"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=600s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]

CMD ["uvicorn", "narsat_inference.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]

# syntax=docker/dockerfile:1
#
# Apache Airflow 3 with the NarSat command line and dbt, each in its own virtual
# environment so that their dependencies never have to agree:
#
#   /opt/airflow-venv  Airflow, installed with the constraints file published by the
#                      Airflow project for this exact version and Python version
#   /opt/narsat        the platform (from uv.lock) and dbt; the DAGs call its executables
#
# Build from the repository root:  docker build -f infra/airflow/Dockerfile -t narsat-airflow .

ARG PYTHON_IMAGE=python:3.13-slim-trixie
ARG UV_VERSION=0.11.32

FROM ${PYTHON_IMAGE} AS narsat
ARG UV_VERSION
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/narsat PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra all
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra all

FROM ${PYTHON_IMAGE}
ARG UV_VERSION
ARG AIRFLOW_VERSION=3.4.0
ARG AIRFLOW_PYTHON=3.13

ENV AIRFLOW_HOME=/opt/airflow \
    PATH=/opt/airflow-venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NARSAT_CLI=/opt/narsat/bin/narsat \
    NARSAT_DBT=/opt/narsat/bin/dbt \
    NARSAT_DBT_PROJECT_DIR=/opt/narsat/transform \
    NARSAT_MARTS_DIR=/opt/narsat/var/marts \
    NARSAT_DBT_WORK_DIR=/tmp/narsat-dbt

RUN --mount=type=cache,target=/root/.cache/uv \
    pip install --no-cache-dir "uv==${UV_VERSION}" \
    && uv venv /opt/airflow-venv \
    && uv pip install --python /opt/airflow-venv/bin/python \
        "apache-airflow[postgres]==${AIRFLOW_VERSION}" \
        apache-airflow-providers-standard \
        --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${AIRFLOW_PYTHON}.txt"

# rasterio's wheels bundle GDAL but link to the system's expat XML library, which slim
# images do not include.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 narsat \
    && useradd --system --uid 10001 --gid narsat --home-dir /opt/airflow --shell /usr/sbin/nologin narsat \
    && mkdir -p /opt/airflow/dags /opt/airflow/logs /opt/narsat/var/marts

COPY --from=narsat /opt/narsat /opt/narsat
COPY transform /opt/narsat/transform
COPY orchestration/dags /opt/airflow/dags
COPY infra/airflow/entrypoint.sh /usr/local/bin/narsat-airflow

RUN chown -R narsat:narsat /opt/airflow /opt/narsat/var
USER narsat
WORKDIR /opt/airflow
ENTRYPOINT ["/usr/local/bin/narsat-airflow"]
CMD ["version"]

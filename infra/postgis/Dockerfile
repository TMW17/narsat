# syntax=docker/dockerfile:1
#
# PostgreSQL 18 with PostGIS 3, built from the Debian packages of the official
# PostgreSQL image. The widely used postgis/postgis image is published for x86-64 only;
# this one builds natively on Apple Silicon (arm64) as well.
FROM postgres:18-trixie

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-18-postgis-3 \
    && rm -rf /var/lib/apt/lists/*

COPY initdb/ /docker-entrypoint-initdb.d/

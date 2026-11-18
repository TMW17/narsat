#!/usr/bin/env bash
# First start of the database container only (an empty data directory): create the
# roles and databases of the stack. Passwords come from the container environment
# (.env), never from this file.
#
#   narsat          application data (PostGIS) - owner: narsat
#   narsat_catalog  Apache Iceberg SQL catalog - owner: narsat
#   airflow         Airflow metadata           - owner: airflow (orchestration profile)
set -euo pipefail

: "${NARSAT_DB_PASSWORD:?NARSAT_DB_PASSWORD must be set}"
: "${AIRFLOW_DB_PASSWORD:?AIRFLOW_DB_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -v narsat_password="$NARSAT_DB_PASSWORD" \
    -v airflow_password="$AIRFLOW_DB_PASSWORD" <<'SQL'
CREATE ROLE narsat LOGIN PASSWORD :'narsat_password';
CREATE ROLE airflow LOGIN PASSWORD :'airflow_password';
CREATE DATABASE narsat OWNER narsat;
CREATE DATABASE narsat_catalog OWNER narsat;
CREATE DATABASE airflow OWNER airflow;
SQL

# PostGIS is not a "trusted" extension: it has to be created by a superuser. The
# migrations then find it in place (CREATE EXTENSION IF NOT EXISTS).
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname narsat <<'SQL'
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS citext;
SQL

# Development

## Tooling

| Part | Tools |
| --- | --- |
| Python 3.13 package | [uv](https://docs.astral.sh/uv/) (environment and lock file), ruff, mypy `--strict`, pytest, Hypothesis |
| Web application | Node.js 22.13 or later (24 recommended), npm, TypeScript, ESLint, Vitest |
| Inference service | its own `pyproject.toml` under `services/inference` |
| Orchestration | Apache Airflow 3.4 in its own environment (see `.github/workflows/ci.yml`) |

```bash
make install    # uv sync --extra all --group dev, then npm ci in web/
make lint
make test       # unit tests: no database, no network
```

`pre-commit install` runs formatting, linting and type checking before each commit.

## Integration tests

Integration tests use real services and are selected automatically when the
corresponding environment variables are set; otherwise they are skipped.

| Variable | Service | Example |
| --- | --- | --- |
| `NARSAT_TEST_DB_DSN` | PostgreSQL with PostGIS and citext; the database name must contain `test` (tables are emptied between tests) | `postgresql://narsat:narsat@127.0.0.1:5432/narsat_test` |
| `NARSAT_TEST_S3_ENDPOINT`, `NARSAT_TEST_S3_ACCESS_KEY`, `NARSAT_TEST_S3_SECRET_KEY` | S3-compatible store | `http://127.0.0.1:8333` |
| `NARSAT_TEST_TITILER_URL` | a TiTiler that can read local files | `http://127.0.0.1:8083` |

Kafka needs no service: the tests use librdkafka's in-process mock cluster.

The quickest way to get the services is the Compose stack itself:

```bash
docker compose up -d postgis seaweedfs
docker compose exec postgis psql -U postgres -c "CREATE DATABASE narsat_test OWNER narsat"
docker compose exec postgis psql -U postgres -d narsat_test \
    -c "CREATE EXTENSION postgis" -c "CREATE EXTENSION citext"
export NARSAT_TEST_DB_DSN=postgresql://narsat:$NARSAT_DB_PASSWORD@127.0.0.1:5432/narsat_test
NARSAT_DB_DSN=$NARSAT_TEST_DB_DSN uv run narsat db upgrade
export NARSAT_TEST_S3_ENDPOINT=http://127.0.0.1:8333
export NARSAT_TEST_S3_ACCESS_KEY=$NARSAT_S3_ACCESS_KEY NARSAT_TEST_S3_SECRET_KEY=$NARSAT_S3_SECRET_KEY
make test-integration
```

(`source .env` first, or replace the variables by their values.)

## Running parts without Docker

* API with reload: `make dev-api` (reads `.env`; needs `NARSAT_DB_DSN` reachable).
* Web application with hot reload: `make dev-web`, then http://localhost:5173. Vite
  forwards `/api` to `localhost:8000` and `/titiler` to `localhost:8083`
  (`NARSAT_DEV_API` and `NARSAT_DEV_TITILER` override them).
* Inference service: see `services/inference/README.md`.
* dbt: `make marts` builds and publishes the marts from the lake named in `.env`.

## End-to-end browser test

`tests/e2e/` drives a real browser through the production build of the web application,
with the API, TiTiler, the inference service (weight-free engine), the lakehouse and dbt
behind it. It uses **synthetic** data and is meant for checking the software, never for
producing figures. See [tests/e2e/README.md](../tests/e2e/README.md).

## Conventions

* Settings are environment variables with the `NARSAT_` prefix, grouped by section
  (`narsat config` prints the effective configuration with secrets masked).
* Database changes are Alembic migrations in `src/narsat/db/migrations`.
* Pipeline logic goes into the package and is exposed as a CLI command; DAGs only call
  commands.
* Architecture decisions are recorded in `docs/adr/`.

## Before publishing

`scripts/prepublish-check.sh` is read-only. It lists what `git add --all` would commit
and fails if it finds any of these:

* a file that must stay local (`.env`, virtual environments, data, keys);
* a file over 1 MB;
* the value of a `*KEY*`, `*SECRET*`, `*PASSWORD*` or `*TOKEN*` variable of `.env`;
* a common credential pattern;
* an absolute home-directory path.

It prints names, never secret values:

```bash
bash scripts/prepublish-check.sh
```


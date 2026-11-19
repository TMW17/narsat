# Validation report

This report states what was **executed** to validate NarSat 0.1 and with what result,
and what was **not** executed. It has two parts:

* **Sections 1 to 5: development environment, 17 November 2026.** No Docker daemon and
  no access to the data providers, so no real data was processed.
* **Section 6: the first real run, 17–18 November 2026,** on the target machine with
  live data. That run confirmed most of the open items of section 4, and the problems
  it found are listed there with their fixes.

Measured results (detections, mapping, accuracy) are in [results.md](results.md).

## 1. Environment

| Item | Value |
| --- | --- |
| Machine | Linux x86-64 cloud workspace, 2 CPU threads, 8 GB memory |
| Docker | Compose CLI 5.7 available, **no Docker daemon**: images could not be built or run |
| Network | Package indexes (PyPI, npm) and raw GitHub content reachable; Docker registries, Hugging Face, NASA FIRMS, Earth Search, geoBoundaries, OpenFreeMap and apache.org **not reachable** |
| Python | 3.13.18, dependencies from `uv.lock` (172 packages) |
| Node.js | 24.22.1 |

Services run natively for the tests:

| Service | Version | Notes |
| --- | --- | --- |
| PostgreSQL / PostGIS | 18.6 / 3.7.0 | conda-forge build |
| SeaweedFS (S3) | 4.53 | conda-forge build, authenticated S3 endpoint |
| Kafka | librdkafka 2.16.0 mock cluster | in-process; **not** an Apache Kafka broker |
| TiTiler | 2.4.1 | from PyPI, the version pinned in `infra/titiler` |
| Apache Airflow | 3.4.0 with its published constraints | metadata database in PostgreSQL |
| dbt | dbt-core 1.12.7, dbt-duckdb 1.11.1, DuckDB 2.0.0 | |
| PyTorch / TerraTorch | 2.15.0 / 1.2.14 | CPU |
| nginx | 1.31.8 | conda-forge build, running `web/nginx.conf` |
| Prometheus | conda-forge build (version string not embedded; a 2.x release) | not the v3.13 image pinned in Compose |
| Browser | Chromium (Playwright 1.63), software WebGL | |

## 2. Checks executed

| Area | Check | Result |
| --- | --- | --- |
| Python code | `ruff format --check`, `ruff check` | clean (101 files) |
| | `mypy --strict` | no issues (69 source files) |
| | `uv lock --check` | lock file consistent |
| Python tests | unit and integration, against the services above | **230 passed, 0 skipped** |
| | same suite without any service | 167 passed, 63 skipped (as designed) |
| Inference service | contract and tiling tests without PyTorch | 24 passed, 1 skipped |
| | with PyTorch and TerraTorch: builds the full Prithvi-EO-2.0 300M architecture (324.2 M parameters) with **random weights**, strict checkpoint loading, tiled prediction | 30 passed |
| | memory and time, 1024 x 1024 px request, batch 2, 2 threads | peak 3.6 GB, 34 s |
| dbt | `dbt build` on a lakehouse built by the real refresh (inside the integration tests) | 50 models and tests passed; marts published to PostgreSQL |
| Airflow | DAG integrity tests | 5 passed |
| | scheduler, API server and DAG processor on PostgreSQL through `infra/airflow/entrypoint.sh`; runs triggered through the REST API, tasks executed through the execution API (a stand-in `narsat` executable recorded the commands) | runs succeeded; commands as expected; a parameter carrying a shell injection was refused at trigger time |
| | login with the password from the environment; wrong password | token issued; 401 |
| Web application | `tsc --noEmit`, ESLint, Vitest, production build | clean; 32 tests passed |
| End to end | browser test through **nginx with the shipped configuration**, lake on the S3 store in the Compose layout, API, TiTiler, inference service (weight-free engine), dbt marts; **synthetic data** | 22 of 22 checks passed |
| | nginx tile allowlist probed with crafted requests (local file, internal URL, other bucket, other prefix, `..` in key, repeated `url`, percent-encoded parameter name, extra parameter) | all refused with 403; valid tiles 200 |
| Database init | `infra/postgis/initdb/10-narsat.sh` on a fresh PostgreSQL 18 cluster with SCRAM authentication; migrations as the unprivileged `narsat` user | roles, databases and extensions created; right password accepted, wrong one refused |
| Iceberg catalog | SQL catalog in PostgreSQL (Compose configuration) and SQLite | exactly-once append verified on both |
| Monitoring | every metric named in alert rules and the dashboard is exported by the code (unit test) | passed |
| | Prometheus with `infra/prometheus/alerts.yml` scraping the local API | 14 rules loaded, all evaluated without error; database-derived gauges match the API's freshness endpoint |
| Compose | `docker compose config` for every profile combination; required values reported when missing | valid (17 services) |
| Scripts and CI | ShellCheck on shell scripts; actionlint on the workflow; the CI's Compose step run locally | clean |

## 3. Defects found by the validation and fixed

Running the checks above found real problems; each was fixed and is covered by a test
or a check that now passes.

| Found by | Problem | Fix |
| --- | --- | --- |
| Design review of the sink | Deleting Kafka's volume would make bronze silently drop new messages (offsets restart at 0) | Topic identifier stored with the offsets; stale offsets discarded (unit tests) |
| Browser test via nginx | MapLibre's module worker served as `application/octet-stream`: map never loaded data | `.mjs` served as JavaScript |
| Header check | Security headers dropped in locations with their own `add_header` | Headers repeated where needed |
| Probe | 404 tiles cached for a day | Cache header on successful responses only |
| Browser test | Map collapsed to 300 px (MapLibre's stylesheet overrides positioning) | Sized by width and height |
| Browser test | Tiles requested outside the image (404s) | Raster footprint stored, bounds sent to the map |
| Screenshot review | Daily charts skipped empty days; axis label rounded 2.5 to 3 | Days filled, labels exact |
| API test on a non-UTC host | Timestamps returned in the server's time zone | Every database session set to UTC |
| dbt on an empty lake | dbt-duckdb writes a row of NULLs for an empty model | Placeholder rows ignored when publishing |
| Type review | Web types used different status names than the database | Types aligned, tests added |
| Monitoring design | Batch jobs' metrics lost when their process exits | Health gauges derived from the database |
| CI step run locally | Placeholder pattern missed a variable containing a digit | Pattern fixed |
| Memory measurement | Documented inference memory (3 GB) was a guess | Replaced by the measured 3.6 GB |
| My own test of the init script | A first run used trust authentication, so it did not test passwords | Re-run with SCRAM |

## 4. Not executed

These parts are written and reviewed but were **not run** in this environment. They
should be checked first on a machine with Docker and internet access.

| Not executed | Why | How it gets checked |
| --- | --- | --- |
| Building and running any Docker image | no Docker daemon | first `docker compose up --build`; the CI workflow builds all six images |
| A real Apache Kafka broker (`apache/kafka:4.4.0`), including the volume-ownership step for its data directory | registry not reachable | Compose stack; the stream code was tested against librdkafka's mock cluster only |
| Live NASA FIRMS requests (and the key-status and data-availability URLs) | not reachable | `narsat firms check` with a real key |
| Live Earth Search STAC searches and COG reads | not reachable | mapping a real event; the code was tested on synthetic COGs with the same band names, scaling metadata and classification |
| The published Prithvi weights and configuration | Hugging Face not reachable | first start of the inference service; tests used an architecture file written to match the published configuration, which must be confirmed |
| geoBoundaries API and EFFIS files | not reachable / manual download | `narsat reference load-admin`, `describe-perimeters`; EFFIS field names are configurable because they are not guaranteed |
| Grafana, and Prometheus v3.13 | images not reachable | `--profile observability`; dashboard JSON and rules are validated as described above |
| The OpenFreeMap base map | not reachable | opening the web application; it falls back to a plain background |
| Apple Silicon specifics (arm64 images, MPS inference) | x86-64 Linux | first run on the MacBook |
| Behaviour at scale (a full fire season, thousands of events) | no real data | backfill of a past season |
| **Accuracy of either burned-area method** | no real data | the benchmark planned in the roadmap |

## 5. Assumptions to confirm on the first real run

1. The published `burn_scars_config.yaml` of Prithvi-EO-2.0-300M-BurnScars describes the
   same architecture as `services/inference/tests/fixtures/burn_scars_architecture.yaml`
   (the strict checkpoint load fails loudly if not).
2. The `apache/kafka` image runs as a user named `appuser` (the Compose volume-init step
   fails loudly if not).
3. Image tags `apache/kafka:4.4.0`, `chrislusf/seaweedfs:4.53`, `prom/prometheus:v3.13.3`,
   `grafana/grafana:13.1.1`, `postgres:18-trixie`, `python:3.13-slim-trixie`,
   `node:24-alpine` and `nginx:stable-alpine` exist for arm64 and x86-64.
4. The FIRMS area API returns the documented CSV columns for each product, including the
   standard-processing archives used for backfills.

## 6. First real run (17–18 November 2026)

### Environment

| Item | Value |
| --- | --- |
| Machine | MacBook Pro, Apple Silicon, 16 GB, macOS 26 |
| Containers | Colima VM (4 CPUs, 8 GB, 100 GB disk), Docker 29.5.2, Compose 5.1.4, buildx 0.39.0 |
| Model service | Run natively: Python 3.13, PyTorch 2.15.0, TerraTorch 1.2.14, device MPS |
| Data | Live NASA FIRMS (near-real-time and `VIIRS_SNPP_SP` archive), Earth Search Sentinel-2 L2A, Hugging Face (Prithvi weights), geoBoundaries, OpenFreeMap, NEALGEBA v1.0 (Zenodo) |

### What ran

| Item | Result |
| --- | --- |
| All images built and run on arm64 (platform, PostGIS, TiTiler, web, Airflow) | Built and healthy |
| Apache Kafka 4.4.0 broker, including the volume-ownership step | Ran; the stream consumer wrote bronze, PostGIS and events |
| Live FIRMS poll | 892 detections, 197 events on the first poll |
| geoBoundaries load | Algeria 48, Morocco 12, Tunisia 24 first-level units |
| Backfill of July–August 2023 | 20,784 detections, 16,305 flagged static |
| Prithvi-EO-2.0-300M-BurnScars weights | Downloaded; the strict checkpoint load passed |
| Mapping with both methods | 403 of 403 queued events mapped |
| Comparison, evaluation against NEALGEBA | Ran; see [results.md](results.md) sections 4 and 5 |
| Lakehouse refresh behind the quality gates | No blocking failure, one warning (explained in results.md, section 6) |
| Airflow 3: DAGs loaded; lakehouse DAG triggered | Success; `dbt build` PASS=50 |
| Web application with the OpenFreeMap base map | Map, Statistics and Data quality pages checked by screenshot |
| CI workflow on GitHub, first push (run 44387229170, 18 November 2026) | All 11 jobs passed in about 8 minutes: ruff, mypy and the Python tests against PostGIS and SeaweedFS; inference contract tests; Airflow DAG integrity; web checks and build; Compose, ShellCheck and actionlint; the six images built and smoke-tested on the amd64 runners |

### Defects found by the run, and fixed

Each fix came with tests. In the development environment, the suite grew from 230 to
273 Python tests, all passing.

| Found by | Problem | Fix |
| --- | --- | --- |
| First `docker compose up` | Every command failed with `libexpat.so.1` missing: rasterio's wheels need it and slim images lack it; the CLI imported the imaging stack at start-up | `libexpat1` in all images; imaging imported only by the commands that need it |
| First run on the Mac | Builds failed without BuildKit (`--mount` requires it); Homebrew's Docker ships without `buildx` | Requirement documented |
| Region tables after loading boundaries | Events created before the boundaries never got a country; fires in Spain and Portugal would have been queued for mapping | Attribution of existing events, with a coastal tolerance; events outside the configured countries are not mapped |
| Mapping Bouira | One failed DNS lookup made a whole event fail (GDAL retries HTTP errors, not network failures) | Image reads and catalogue searches retried with back-off |
| Overnight mapping run | A failed block read reports "Read failed" and hides its network cause in the chained exception | The retry check reads the exception chain |
| Writing the inference docs | They said to run the model service on port 8080, which the web application uses | Port 8090 |
| Loading NEALGEBA | Text dates were read day-first; a month-first file would have been misread silently | `--date-format` and `--no-date` options |
| Screenshot review | A small fire next to a large one showed recall 0.04 against NEALGEBA: polygons within 2 km of an event took in the neighbouring fire | Polygons matched by distance to the event's own detections (750 m); stale comparison rows removed |
| Screenshot review | After that, the largest Bejaia event still showed recall 0.36: one NEALGEBA polygon can cover a whole fire complex | Reference clipped to the area the mapper examined |
| Screenshot review | The detections chart said "per day" while showing monthly totals; the largest fires were ranked by the model's area; "0 min" for single-pass fires; "Show on map" did not zoom to the fire | Label follows the grouping; ranking by dNBR; "one pass"; map resized before zooming |
| My own type check | A first version of the matching fix read a settings field that does not exist, so the agreement command would have crashed | Caught before delivery; a test now runs the command as a subprocess |

### Assumptions of section 5

| Assumption | Outcome |
| --- | --- |
| 1. Prithvi configuration | Confirmed: the strict checkpoint load passed |
| 2. Kafka image user | Confirmed: the volume-init step and the broker ran |
| 3. Image tags for arm64 | Confirmed for all except `prom/prometheus` and `grafana/grafana`, which were not pulled |
| 4. FIRMS CSV columns | Confirmed for the near-real-time products and `VIIRS_SNPP_SP` |

### Still not executed

| Not executed | Note |
| --- | --- |
| The `observability` profile (Prometheus v3.13, Grafana) | Rules and dashboard are only checked by tests |
| EFFIS perimeters | NEALGEBA was used instead; it covers north-east Algeria only |
| The end-to-end browser test on the Mac | The pages were checked by hand with screenshots |
| Mapping of the live November 2026 events | None was queued for mapping during the run |


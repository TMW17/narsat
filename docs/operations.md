# Operations

How to run NarSat with Docker Compose, load data, and deal with the usual problems.
Commands assume the repository root as working directory.

## 1. First start

Docker needs Compose v2 and BuildKit: the images use `RUN --mount`. On macOS with
Homebrew's Docker and Colima, install the `docker-buildx` formula too, otherwise the
build stops with "the --mount option requires BuildKit".

```bash
cp .env.example .env            # fill in every value marked "required"
docker compose up -d --build    # core platform
docker compose ps               # "init" must show "Exited (0)", the others "running"
docker compose logs init        # schema, topics and lake tables created
```

The first build downloads base images and Python wheels (several minutes). `init` is a
one-shot container: it waits for the database, applies the migrations, creates the Kafka
topics and the Iceberg tables. It is safe to run again (`docker compose up init`).

Then load the administrative boundaries, used to attribute detections to a country and a
first-level unit (wilaya, region, governorate). They come from geoBoundaries (gbOpen):

```bash
for c in DZA MAR TUN; do
  docker compose exec api narsat reference load-admin --iso3 $c --level 0
  docker compose exec api narsat reference load-admin --iso3 $c --level 1
done
```

Create an analyst account for the review workflow (the password is asked for):

```bash
docker compose exec api narsat users create you@example.org --role admin
```

## 2. Data paths

| Path | Command | Scheduled by |
| --- | --- | --- |
| Live detections | `narsat firms poll` (service `poller`) | always on |
| Stream to lake, PostGIS and events | `narsat stream consume` (service `consumer`) | always on |
| Event status, mapping, agreement | `narsat events update`, `narsat map pending`, `narsat reference agreement` | DAG `narsat_map_events`, hourly |
| Lakehouse rebuild and marts | `narsat lake refresh`, `dbt build`, `narsat lake publish-marts` | DAG `narsat_lakehouse_refresh`, 02:30 UTC |
| Compaction, snapshot expiry | `narsat lake maintain` | DAG `narsat_lake_maintenance`, Sunday 05:00 UTC |
| History | `narsat firms backfill` | DAG `narsat_firms_backfill`, manual |

Without the `orchestration` profile, run the batch commands by hand
(`docker compose exec api narsat map pending`, and so on).

## 3. Loading a past fire season

The near-real-time products only cover recent weeks. For a past season use the
standard-processing products (`*_SP`), which are the science-quality archive:

1. Start Airflow: `docker compose --profile orchestration up -d --build`, open
   http://localhost:8081 (user `admin`, password `AIRFLOW_ADMIN_PASSWORD`).
2. Trigger `narsat_firms_backfill` with a source and a date range, for example
   `VIIRS_SNPP_SP` from `2023-07-01` to `2023-08-31`. The detections go through Kafka
   like live ones; the consumer stores them and builds the events.
3. Events become `closed` three days after their last detection, measured from now, so
   historical events close at the next `narsat_map_events` run and are mapped in
   batches of ten per hour (`NARSAT_MAP_BATCH`).
4. Trigger `narsat_lakehouse_refresh` once the mapping queue is empty, or wait for the
   night. Use `narsat lake refresh --all` after a large backfill to rebuild every month.

FIRMS allows 5,000 transactions per 10 minutes per key and at most 5 days per request: a
two-month backfill is about a dozen requests per product.

## 4. Reference perimeters (optional)

EFFIS burnt-area perimeters can be downloaded from the EFFIS data portal after accepting
its terms; NarSat does not download them for you. Inspect the file first, because the
attribute names vary between exports:

```bash
docker compose cp burnt_areas.shp api:/tmp/   # with its .dbf, .shx, .prj
docker compose exec api narsat reference describe-perimeters /tmp/burnt_areas.shp
docker compose exec api narsat reference load-perimeters /tmp/burnt_areas.shp \
    --source effis --id-field id --date-field FIREDATE --area-field AREA_HA
docker compose exec api narsat reference agreement
```

### NEALGEBA, an open reference for north-east Algeria

NEALGEBA (CC BY 4.0) has one shapefile per year. Load the 2023 layer by place only,
because its dates are detection dates that lag the fires, then evaluate the maps inside
the six wilayas it covers:

```bash
unzip -o ~/Downloads/nealgeba/NEALGEBA_v1.0.zip 'NEALGEBA_v1.0/2023_*' -d ~/Downloads/nealgeba
docker compose cp ~/Downloads/nealgeba/NEALGEBA_v1.0 api:/tmp/nealgeba
docker compose exec api narsat reference describe-perimeters /tmp/nealgeba/2023_NEALGEBA_v1.0.shp
docker compose exec api narsat reference load-perimeters /tmp/nealgeba/2023_NEALGEBA_v1.0.shp \
    --source nealgeba --no-date --area-field BA_ha
docker compose exec api narsat reference agreement
docker compose exec api narsat map evaluate --source nealgeba \
    --admin1 '^(b[eé]ja[iï]a|skikda|el.?tarf|jijel|annaba|tizi.?ouzou)$'
```

`narsat map evaluate` burns the reference polygons into each event's 20 m grid. It
reports precision, recall, IoU and Dice for four maps: dNBR, the model, the model kept
only where dNBR shows change (at least 0.10 on vegetated ground), and the union of dNBR
and that gated model. A pixel shared by overlapping events is counted once.

## 5. Foundation-model inference

```bash
docker compose --profile ml up -d --build   # CPU inference in a container
```

Set `NARSAT_SEG_METHODS=dnbr,model` in `.env` and recreate the services that read it
(`docker compose up -d`). The first start downloads about 1.3 GB of weights into the
`inference-models` volume. On a Mac, running the service natively uses the GPU (MPS) and
is much faster: see `services/inference/README.md`, then set
`NARSAT_SEG_INFERENCE_URL=http://host.docker.internal:8090` (8080 is taken by the web application). Events mapped before the
model was enabled can be mapped again from the event panel (admin) or with
`narsat map event <id>`.

To see where the two methods disagree on an event, and why, run
`narsat map compare <id>`. It reads the stored rasters and puts every pixel that only
one method calls burned into one class: skipped by dNBR because the ground was bare or
water before the fire, skipped for lack of a clear view, unchanged (dNBR below 0.10),
low severity (0.10 to 0.27), below the event's threshold, or removed by the patch
cleaning. Each class comes with its area and the median dNBR and NDVI before and after
the fire. `narsat map compare --all` adds up the classes over every event where a method
found a burn, grouped by outcome (both found a burn, only dNBR, only the model).
`analysis/mapping_summary.sql` summarises a mapping run (status, products, agreement and
the largest fires); run it after `narsat reference agreement`.

## 6. Monitoring

```bash
docker compose --profile observability up -d
```

Grafana (http://localhost:3000) opens on the *NarSat overview* dashboard. Prometheus
(http://localhost:9090/alerts) evaluates the rules in `infra/prometheus/alerts.yml`;
there is no notification channel configured, by design for a local stack.

On a new installation `NarsatQualityRunsStopped` fires until the first lakehouse
refresh has run: trigger `narsat_lakehouse_refresh` once.

## 7. Troubleshooting

| Symptom | Check | Remedy |
| --- | --- | --- |
| `poller` restarting | `docker compose logs poller` | `configuration error: NARSAT_FIRMS_MAP_KEY is not set`: add the key to `.env`, then `docker compose up -d poller` |
| FIRMS HTTP 403/429 in poller logs | key valid? polling too often? | Check the key at the FIRMS site; keep `NARSAT_FIRMS_POLL_INTERVAL_S` at 600 or more |
| Detections stop | Grafana *Minutes since last successful poll*; `consumer` logs | Restart the failing service; the 2-day polling window recovers short gaps |
| Messages in the dead-letter topic | `docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic firms.hotspots.dlq --from-beginning --property print.headers=true` | The `narsat-reason` header says why |
| Event stuck in *Waiting for imagery* | mapping note on the event | Normal while clouds persist; it is retried every 12 hours for 45 days |
| Event *Failed* | `narsat map queue`, `mapping_note` | Fix the cause (network, inference service), then `narsat map event <id>` |
| Lakehouse refresh fails | Data quality page, `dq_result` | A blocking check failed: the tables kept their previous version. Fix the data or the check, rerun the DAG |
| Map without satellite layers | browser developer tools, `titiler` logs | 403: URL not allowed by nginx (see ADR 0008); 404 outside the image is normal |
| Map shows a plain grey background | network access to tiles.openfreemap.org | The fire data is still shown; the base map needs internet access |
| Containers killed, exit code 137 | Docker Desktop memory | Raise the memory limit, or stop the `ml` profile |
| Kafka data volume deleted | consumer log `kafka_topic_recreated` | Expected: the lake discards the old offsets and keeps the new messages |

## 8. Backups

| What | How |
| --- | --- |
| PostgreSQL (operational data, catalog, Airflow) | `docker compose exec postgis pg_dumpall -U postgres > narsat-$(date +%F).sql` |
| Lake files (Iceberg data, rasters) | any S3 client against `http://localhost:8333` with the keys of `.env`, e.g. `aws s3 sync s3://narsat ./backup --endpoint-url http://localhost:8333` |

The Iceberg catalog (database `narsat_catalog`) and the files must be backed up together:
the catalog points at metadata files in the bucket.

## 9. Stopping and resetting

```bash
docker compose --profile orchestration --profile ml --profile observability down     # keep data
docker compose --profile orchestration --profile ml --profile observability down -v  # delete all data
```

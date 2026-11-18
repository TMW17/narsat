"""NarSat batch pipelines for Apache Airflow 3.

Airflow schedules and retries; it does not contain pipeline logic. Every task runs one
command of the ``narsat`` CLI (or ``dbt``), which lives in its own virtual environment
inside the Airflow image, so the platform's dependencies never have to agree with
Airflow's. The same commands can be run by hand: a failed task is reproduced by copying
its command into a shell.

The continuously running parts of the platform (FIRMS poller, Kafka-to-lake consumer,
API) are long-lived services in ``compose.yaml`` and are deliberately not Airflow tasks.

DAGs
----
``narsat_map_events``        hourly   close quiet events, map the queued ones, score agreement
``narsat_lakehouse_refresh`` daily    silver/gold tables -> dbt marts -> API database
``narsat_lake_maintenance``  weekly   compact bronze files and expire old snapshots
``narsat_firms_backfill``    manual   load historical detections for a date range

Configuration (environment of the Airflow containers)
-----------------------------------------------------
``NARSAT_CLI``              path of the ``narsat`` executable
``NARSAT_DBT``              path of the ``dbt`` executable
``NARSAT_DBT_PROJECT_DIR``  the ``transform/`` directory of the repository
``NARSAT_MARTS_DIR``        where dbt writes the Parquet marts
``NARSAT_DBT_WORK_DIR``     writable directory for dbt's target and log files
``NARSAT_MAP_BATCH``        maximum number of events mapped per hourly run

The ``NARSAT_*`` settings of the platform itself (database, lake, Kafka, FIRMS key) are
read from the same environment by the CLI.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pendulum
from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG, Param

NARSAT = os.environ.get("NARSAT_CLI", "/opt/narsat/bin/narsat")
DBT = os.environ.get("NARSAT_DBT", "/opt/narsat/bin/dbt")
DBT_PROJECT_DIR = os.environ.get("NARSAT_DBT_PROJECT_DIR", "/opt/narsat/transform")
MARTS_DIR = os.environ.get("NARSAT_MARTS_DIR", "/opt/narsat/var/marts")
DBT_WORK_DIR = os.environ.get("NARSAT_DBT_WORK_DIR", "/tmp/narsat-dbt")  # noqa: S108
MAP_BATCH = int(os.environ.get("NARSAT_MAP_BATCH", "10"))

START = pendulum.datetime(2026, 1, 1, tz="UTC")
TAGS = ["narsat"]
DEFAULT_ARGS = {
    "owner": "narsat",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=30),
}

# FIRMS sources accepted by the backfill. "SP" (standard processing) is the
# science-quality archive; "NRT" covers the most recent weeks.
BACKFILL_SOURCES = [
    "VIIRS_SNPP_SP",
    "VIIRS_NOAA20_SP",
    "VIIRS_NOAA21_SP",
    "MODIS_SP",
    "VIIRS_SNPP_NRT",
    "VIIRS_NOAA20_NRT",
    "VIIRS_NOAA21_NRT",
    "MODIS_NRT",
]


def cli(task_id: str, arguments: str, *, timeout: timedelta, **kwargs: object) -> BashOperator:
    """A task that runs ``narsat <arguments>``."""
    return BashOperator(
        task_id=task_id,
        bash_command=f"{NARSAT} {arguments}",
        execution_timeout=timeout,
        **kwargs,
    )


# --------------------------------------------------------------------------- mapping
with DAG(
    dag_id="narsat_map_events",
    description="Close quiet fire events, map burned areas for queued events, score agreement.",
    schedule="20 * * * *",
    start_date=START,
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    dagrun_timeout=timedelta(hours=3),
    tags=[*TAGS, "mapping"],
    doc_md=(
        "Runs every hour.\n\n"
        "1. **update_events** - re-evaluates event status. Events change state because time "
        "passes (active -> cooling -> closed), not only because detections arrive, so this "
        "must run even when the stream is silent. Closed events enter the mapping queue.\n"
        "2. **map_pending** - searches Sentinel-2 scenes and maps the burned area of queued "
        "events. An event without a usable post-fire scene yet stays queued and is "
        "re-examined later; this is expected, not a failure.\n"
        "3. **score_agreement** - recomputes agreement with reference perimeters and "
        "between mapping methods."
    ),
) as map_events:
    update_events = cli("update_events", "events update", timeout=timedelta(minutes=20))
    map_pending = cli(
        "map_pending",
        f"map pending --limit {MAP_BATCH}",
        timeout=timedelta(hours=2),
        # Imagery reads are retried inside the command; a second full attempt is enough.
        retries=1,
    )
    score_agreement = cli("score_agreement", "reference agreement", timeout=timedelta(minutes=20))
    update_events >> map_pending >> score_agreement


# --------------------------------------------------------------------------- lakehouse
with DAG(
    dag_id="narsat_lakehouse_refresh",
    description="Rebuild silver/gold tables behind the quality gates, build and publish marts.",
    schedule="30 2 * * *",
    start_date=START,
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    dagrun_timeout=timedelta(hours=2),
    tags=[*TAGS, "lakehouse", "dbt"],
    doc_md=(
        "Runs every night (02:30 UTC).\n\n"
        "1. **refresh_lake** - rebuilds the silver months touched recently and the gold "
        "tables. A failed blocking quality check stops the run here: silver keeps its last "
        "good version and the failure is recorded in `dq_result`.\n"
        "2. **build_marts** - `dbt build` (models and tests) over the lakehouse with DuckDB.\n"
        "3. **publish_marts** - loads the Parquet marts into the API database, each table "
        "replaced in one transaction."
    ),
) as lakehouse_refresh:
    refresh_lake = cli(
        "refresh_lake",
        "lake refresh --recent-days 45",
        timeout=timedelta(minutes=45),
        # A failed quality gate is a data problem: retrying does not fix it.
        retries=0,
    )
    build_marts = BashOperator(
        task_id="build_marts",
        bash_command=(
            f"mkdir -p {DBT_WORK_DIR} {MARTS_DIR} && "
            f"{DBT} build --project-dir {DBT_PROJECT_DIR} --profiles-dir {DBT_PROJECT_DIR} "
            f"--target-path {DBT_WORK_DIR}/target --log-path {DBT_WORK_DIR}/logs --no-use-colors"
        ),
        append_env=True,
        env={"NARSAT_MARTS_DIR": MARTS_DIR, "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"},
        execution_timeout=timedelta(minutes=30),
        retries=0,
    )
    publish_marts = cli(
        "publish_marts", f"lake publish-marts --dir {MARTS_DIR}", timeout=timedelta(minutes=10)
    )
    refresh_lake >> build_marts >> publish_marts


# --------------------------------------------------------------------------- maintenance
with DAG(
    dag_id="narsat_lake_maintenance",
    description="Compact small bronze files and expire old Iceberg snapshots.",
    schedule="0 5 * * 0",
    start_date=START,
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    dagrun_timeout=timedelta(hours=2),
    tags=[*TAGS, "lakehouse", "maintenance"],
    doc_md=(
        "Runs on Sundays (05:00 UTC). The stream consumer commits a small file every few "
        "seconds; closed months are rewritten into one file each and snapshots older than "
        "the retention period are expired. The month still being written is left alone."
    ),
) as lake_maintenance:
    cli("maintain_lake", "lake maintain", timeout=timedelta(hours=1))


# --------------------------------------------------------------------------- backfill
with DAG(
    dag_id="narsat_firms_backfill",
    description="Load historical FIRMS detections for a date range (manual trigger).",
    schedule=None,
    start_date=START,
    catchup=False,
    max_active_runs=1,
    default_args={**DEFAULT_ARGS, "retries": 0},
    tags=[*TAGS, "ingestion", "manual"],
    params={
        "source": Param(
            "VIIRS_SNPP_SP",
            enum=BACKFILL_SOURCES,
            description="FIRMS dataset. Use the *_SP archives for past fire seasons.",
        ),
        "start": Param(
            "2023-07-01", type="string", format="date", description="First day (UTC), YYYY-MM-DD"
        ),
        "end": Param(
            "2023-08-31", type="string", format="date", description="Last day (UTC), YYYY-MM-DD"
        ),
    },
    doc_md=(
        "Trigger with a source and a date range. The detections are published to Kafka like "
        "live ones, so they go through the same validation, land in bronze and are clustered "
        "by the stream consumer; nothing else needs to run afterwards.\n\n"
        "FIRMS allows 5,000 transactions per 10 minutes per key and at most 5 days per "
        "request: a two-month backfill is about 12 requests per source."
    ),
) as firms_backfill:
    # The three parameters are validated against the schema above (an enumeration and
    # ISO dates) before the task starts, so they are safe to place in the command line.
    cli(
        "backfill",
        "firms backfill --source {{ params.source }} --start {{ params.start }} "
        "--end {{ params.end }}",
        timeout=timedelta(hours=6),
    )

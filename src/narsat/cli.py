"""Command-line interface: ``narsat <group> <command>``.

Every operational action of the platform is a command here. The Airflow DAGs, the
Compose services and a developer at a terminal all run the same commands, so there
is exactly one implementation of each step.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import uuid
from pathlib import Path
from typing import Annotated

import typer

from narsat import __version__
from narsat.config import Settings, get_settings
from narsat.log import configure_logging, get_logger

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
db_app = typer.Typer(no_args_is_help=True, help="Operational database (PostGIS).")
lake_app = typer.Typer(no_args_is_help=True, help="Lakehouse (Iceberg tables and object storage).")
stream_app = typer.Typer(no_args_is_help=True, help="Kafka topics and the stream consumer.")
firms_app = typer.Typer(no_args_is_help=True, help="NASA FIRMS ingestion.")
events_app = typer.Typer(no_args_is_help=True, help="Fire events.")
reference_app = typer.Typer(no_args_is_help=True, help="Boundaries and reference perimeters.")
map_app = typer.Typer(no_args_is_help=True, help="Burned-area mapping.")
users_app = typer.Typer(no_args_is_help=True, help="API users.")
app.add_typer(db_app, name="db")
app.add_typer(lake_app, name="lake")
app.add_typer(stream_app, name="stream")
app.add_typer(firms_app, name="firms")
app.add_typer(events_app, name="events")
app.add_typer(reference_app, name="reference")
app.add_typer(map_app, name="map")
app.add_typer(users_app, name="users")

log = get_logger("narsat.cli")


def _settings() -> Settings:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    return settings


def _echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("expected a date as YYYY-MM-DD") from exc


# =============================================================================== general
@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command("config")
def show_config() -> None:
    """Print the effective configuration (secrets are masked)."""
    _echo_json(_settings().model_dump(mode="json"))


@app.command("export-schemas")
def export_schemas(
    out: Annotated[Path, typer.Option(help="Output directory")] = Path("schemas"),
) -> None:
    """Write the JSON Schema of the Kafka message contracts."""
    from narsat.schemas import FireEventMessage, HotspotMessage

    out.mkdir(parents=True, exist_ok=True)
    for name, model in (("hotspot.v1", HotspotMessage), ("fire_event.v1", FireEventMessage)):
        target = out / f"{name}.schema.json"
        target.write_text(
            json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        typer.echo(f"wrote {target}")


@app.command("serve-api")
def serve_api(
    host: Annotated[str, typer.Option()] = "0.0.0.0",  # noqa: S104 - container default
    port: Annotated[int, typer.Option()] = 8000,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code change (development)")] = False,
) -> None:
    """Run the REST API with uvicorn."""
    import uvicorn

    settings = _settings()
    uvicorn.run(
        "narsat.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
    )


# =============================================================================== db
@db_app.command("wait")
def db_wait(timeout: Annotated[float, typer.Option(help="Seconds")] = 60.0) -> None:
    """Block until the database accepts connections."""
    from narsat.db import wait_until_ready

    wait_until_ready(_settings().db, timeout)
    typer.echo("database is ready")


@db_app.command("upgrade")
def db_upgrade(revision: Annotated[str, typer.Argument()] = "head") -> None:
    """Apply the schema migrations."""
    from narsat.db import migrate

    settings = _settings()
    migrate.upgrade(settings.db, revision)
    typer.echo(f"database at revision {migrate.current_revision(settings.db)}")


@db_app.command("downgrade")
def db_downgrade(revision: Annotated[str, typer.Argument()]) -> None:
    """Revert the schema to an earlier revision (``base`` drops everything)."""
    from narsat.db import migrate

    migrate.downgrade(_settings().db, revision)
    typer.echo(f"database downgraded to {revision}")


# =============================================================================== lake
@lake_app.command("init")
def lake_init() -> None:
    """Create the storage locations, the Iceberg namespaces and the tables."""
    from narsat.lake.catalog import ensure_tables, load_catalog
    from narsat.lake.storage import LakeStorage

    settings = _settings()
    for uri in (settings.lake.warehouse_uri, settings.lake.assets_uri):
        LakeStorage(uri, settings.lake).ensure_ready()
    tables = ensure_tables(load_catalog(settings.lake))
    for name in tables:
        typer.echo(f"table ready: {name}")


@lake_app.command("info")
def lake_info() -> None:
    """Row counts, snapshot counts and data-file counts of every table."""
    from narsat.lake.catalog import ALL_TABLES, load_catalog

    catalog = load_catalog(_settings().lake)
    report = {}
    for definition in ALL_TABLES:
        table = catalog.load_table(definition.identifier)
        snapshot = table.current_snapshot()
        report[definition.identifier] = {
            "rows": int(table.scan().count()) if snapshot else 0,
            "snapshots": len(table.snapshots()),
            "data_files": table.inspect.files().num_rows if snapshot else 0,
            "location": table.location(),
        }
    _echo_json(report)


@lake_app.command("refresh")
def lake_refresh(
    start: Annotated[str | None, typer.Option("--from", help="First day (YYYY-MM-DD)")] = None,
    end: Annotated[str | None, typer.Option("--to", help="Last day (YYYY-MM-DD)")] = None,
    recent_days: Annotated[int, typer.Option(help="Months touched by the last N days")] = 45,
    all_months: Annotated[bool, typer.Option("--all", help="Rebuild every month")] = False,
) -> None:
    """Rebuild silver and gold behind the quality gates. Exits 1 if a gate fails."""
    from narsat.db import connect
    from narsat.lake import silver
    from narsat.lake.catalog import ensure_tables, load_catalog
    from narsat.pipeline.lake_refresh import refresh
    from narsat.quality.gates import GateFailed

    settings = _settings()
    catalog = load_catalog(settings.lake)
    ensure_tables(catalog)
    months: list[str] | None
    if all_months:
        months = None
    elif start or end:
        today = dt.datetime.now(dt.UTC).date()
        months = silver.months_between(
            _parse_date(start) if start else today, _parse_date(end) if end else today
        )
    else:
        today = dt.datetime.now(dt.UTC).date()
        months = silver.months_between(today - dt.timedelta(days=recent_days), today)
    if months is not None:
        present = set(silver.available_months(catalog))
        months = [month for month in months if month in present]
    try:
        with connect(settings.db) as conn:
            report = refresh(catalog, conn, settings, months)
    except GateFailed as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        for failure in exc.failures:
            typer.echo(f"  {failure.suite}.{failure.check_name}: {failure.observed}", err=True)
        raise typer.Exit(1) from exc
    _echo_json(
        {
            "run_id": report.run_id,
            "months": report.months,
            "rows": report.rows,
            "warnings": [c.check_name for c in report.checks if c.status == "warn"],
        }
    )


@lake_app.command("maintain")
def lake_maintain(
    min_files: Annotated[int, typer.Option(help="Compact months with at least N files")] = 8,
) -> None:
    """Expire old snapshots and compact closed months of bronze."""
    from narsat.lake import maintenance
    from narsat.lake.catalog import load_catalog

    settings = _settings()
    catalog = load_catalog(settings.lake)
    maintenance.compact_bronze(catalog, min_files=min_files)
    maintenance.expire_snapshots(catalog, settings.lake.snapshot_retention_days)
    typer.echo("maintenance done")


@lake_app.command("publish-marts")
def lake_publish_marts(
    marts_dir: Annotated[Path, typer.Option("--dir", help="Directory with the dbt Parquet marts")],
) -> None:
    """Load the dbt marts into PostgreSQL for the API."""
    from narsat.db import connect
    from narsat.pipeline.marts import publish_all

    with connect(_settings().db) as conn:
        _echo_json(publish_all(conn, marts_dir))


# =============================================================================== stream
@stream_app.command("init-topics")
def stream_init_topics() -> None:
    """Create the Kafka topics (idempotent)."""
    from narsat.streaming.kafka import ensure_topics

    _echo_json(ensure_topics(_settings().kafka))


@stream_app.command("consume")
def stream_consume() -> None:
    """Run the hotspot sink: Kafka -> bronze + PostGIS -> fire events."""
    from narsat import metrics
    from narsat.services import build_stream_runner, stop_on_signals

    settings = _settings()
    metrics.start_metrics_server(settings.metrics_port)
    runner, close = build_stream_runner(settings)
    try:
        runner.run(stop_on_signals())
    finally:
        close()


# =============================================================================== firms
@firms_app.command("check")
def firms_check() -> None:
    """Verify the MAP_KEY and list the date range served for each source."""
    from narsat.services import build_firms_client

    settings = _settings()
    with build_firms_client(settings) as client:
        status = client.key_status()
        availability = client.data_availability()
    wanted = set(settings.firms.sources)
    _echo_json(
        {
            "key_status": status,
            "configured_sources": [row for row in availability if row.get("data_id") in wanted],
        }
    )


@firms_app.command("poll")
def firms_poll(
    once: Annotated[bool, typer.Option(help="Run a single cycle and exit")] = False,
) -> None:
    """Poll FIRMS and publish new detections to Kafka."""
    from narsat import metrics
    from narsat.services import build_poller, stop_on_signals

    settings = _settings()
    poller = build_poller(settings)
    if once:
        results = poller.poll_once()
        _echo_json([r.__dict__ for r in results])
        if all(r.error for r in results):
            raise typer.Exit(1)
        return
    metrics.start_metrics_server(settings.metrics_port)
    poller.run_forever(settings.firms.poll_interval_s, stop_on_signals())


@firms_app.command("backfill")
def firms_backfill(
    source: Annotated[str, typer.Option(help="FIRMS source, e.g. VIIRS_SNPP_SP")],
    start: Annotated[str, typer.Option(help="First day (YYYY-MM-DD)")],
    end: Annotated[str, typer.Option(help="Last day (YYYY-MM-DD)")],
) -> None:
    """Replay a past period onto the hotspot topic (five days per request)."""
    from narsat.services import build_poller

    poller = build_poller(_settings(), persistent_state=False)
    results = poller.backfill(source, _parse_date(start), _parse_date(end))
    _echo_json([r.__dict__ for r in results])
    if any(r.error for r in results):
        raise typer.Exit(1)


# =============================================================================== events
@events_app.command("update")
def events_update(
    full: Annotated[bool, typer.Option(help="Re-cluster every hotspot from scratch")] = False,
) -> None:
    """Re-cluster hotspots into fire events and apply status transitions."""
    from narsat.db import connect
    from narsat.services import build_event_updater

    settings = _settings()
    with connect(settings.db) as conn:
        result = build_event_updater(settings).update(conn, full=full)
    _echo_json(
        {
            "hotspots_considered": result.hotspots_considered,
            "events_upserted": len(result.events_upserted),
            "events_created": len(result.events_created),
            "events_merged": len(result.events_merged),
            "events_dissolved": len(result.events_dissolved),
            "hotspots_flagged_static": result.hotspots_flagged_static,
            "queued_for_mapping": result.queued_for_mapping,
        }
    )


@events_app.command("list")
def events_list(limit: Annotated[int, typer.Option()] = 20) -> None:
    """Largest live events, with their mapping status."""
    from narsat.db import connect

    with connect(_settings().db) as conn:
        rows = conn.execute(
            """
            SELECT event_id, status, mapping_status, country_iso3, first_seen, last_seen,
                   n_hotspots, round(hull_area_ha::numeric, 0) AS hull_ha, mapping_note
            FROM fire_event
            WHERE status IN ('active', 'cooling', 'closed')
            ORDER BY n_hotspots DESC LIMIT %(limit)s
            """,
            {"limit": limit},
        ).fetchall()
    _echo_json(rows)


# =============================================================================== reference
@reference_app.command("load-admin")
def reference_load_admin(
    iso3: Annotated[str, typer.Option(help="Country code, e.g. DZA")],
    level: Annotated[int, typer.Option(help="0 = country, 1 = first-level units")] = 1,
    path: Annotated[
        Path | None, typer.Option(help="Local GeoJSON instead of geoBoundaries")
    ] = None,
    name_field: Annotated[str, typer.Option()] = "shapeName",
    code_field: Annotated[str, typer.Option()] = "shapeISO",
    source: Annotated[str | None, typer.Option(help="Attribution text for a local file")] = None,
    licence: Annotated[str | None, typer.Option(help="Licence of a local file")] = None,
) -> None:
    """Load administrative boundaries and attribute existing hotspots to them."""
    from narsat.db import connect
    from narsat.db.hotspots import backfill_admin
    from narsat.reference import admin

    if path is not None:
        if source is None:
            raise typer.BadParameter("--source is required with --path (who published the file?)")
        layer = admin.load_geojson_file(path, iso3, level, source, licence)
    else:
        layer = admin.fetch_geoboundaries(iso3, level)
    with connect(_settings().db) as conn:
        stored = admin.store_layer(conn, layer, name_field=name_field, code_field=code_field)
        attributed = backfill_admin(conn)
    _echo_json(
        {
            "units": stored,
            "source": layer.source,
            "licence": layer.licence,
            "hotspots_attributed": attributed,
        }
    )


@reference_app.command("describe-perimeters")
def reference_describe_perimeters(path: Annotated[Path, typer.Argument(exists=True)]) -> None:
    """Show the attributes of a perimeter file before loading it."""
    from narsat.reference import perimeters

    _echo_json(perimeters.describe(path))


@reference_app.command("load-perimeters")
def reference_load_perimeters(
    path: Annotated[Path, typer.Argument(exists=True)],
    source: Annotated[str, typer.Option(help="Short source name, e.g. effis")] = "effis",
    id_field: Annotated[str, typer.Option()] = "id",
    date_field: Annotated[str, typer.Option()] = "FIREDATE",
    area_field: Annotated[str, typer.Option()] = "AREA_HA",
    country_field: Annotated[str, typer.Option()] = "COUNTRY",
    date_format: Annotated[
        str | None, typer.Option(help="strptime pattern of text dates, e.g. %m/%d/%Y")
    ] = None,
    no_date: Annotated[
        bool, typer.Option("--no-date", help="Ignore dates: match perimeters by place only")
    ] = False,
) -> None:
    """Load reference burnt-area perimeters that intersect the area of interest."""
    from narsat.db import connect
    from narsat.reference import perimeters

    settings = _settings()
    fields = perimeters.FieldMap(
        id_field, None if no_date else date_field, area_field, country_field, date_format
    )
    loaded = perimeters.read_perimeters(path, source, fields, settings.aoi.bbox)
    with connect(settings.db) as conn:
        stored = perimeters.store_perimeters(conn, source, loaded)
    _echo_json(
        {
            "source": source,
            "perimeters": stored,
            "with_date": sum(1 for p in loaded if p.fire_date is not None),
        }
    )


@reference_app.command("agreement")
def reference_agreement() -> None:
    """Compare mapped burned areas with the reference perimeters and between methods."""
    from narsat.db import connect
    from narsat.pipeline.validate import compute_agreement

    settings = _settings()
    with connect(settings.db) as conn:
        _echo_json(
            compute_agreement(
                conn,
                match_m=settings.seg.seed_distance_m,
                aoi_m=settings.imagery.aoi_buffer_m,
            )
        )


# =============================================================================== mapping
def _map_one(settings: Settings, event_id: uuid.UUID) -> dict[str, object]:
    from narsat.db import connect
    from narsat.pipeline.map_event import record_crash
    from narsat.services import build_event_mapper

    mapper = build_event_mapper(settings)
    try:
        with connect(settings.db) as conn:
            outcome = mapper.map_event(conn, event_id)
    except Exception as exc:
        log.exception("event_mapping_crashed", event_id=str(event_id))
        with connect(settings.db) as conn:
            record_crash(conn, event_id, f"{type(exc).__name__}: {exc}")
        return {"event_id": str(event_id), "status": "error", "note": str(exc)[:300]}
    return {
        "event_id": str(event_id),
        "status": outcome.status,
        "note": outcome.note,
        "pre_scene": outcome.pre_scene,
        "post_scene": outcome.post_scene,
        "seconds": round(outcome.seconds, 1),
        "products": {
            p.method: {"area_ha": round(p.polygons.area_ha, 2), "polygons": p.polygons.n_polygons}
            for p in outcome.products
        },
    }


@map_app.command("event")
def map_event(event_id: Annotated[str, typer.Argument(help="Event UUID")]) -> None:
    """Map the burned area of one event (re-running replaces its products)."""
    result = _map_one(_settings(), uuid.UUID(event_id))
    _echo_json(result)
    if result["status"] in ("error", "failed"):
        raise typer.Exit(1)


@map_app.command("compare")
def map_compare(
    event_id: Annotated[str | None, typer.Argument(help="Event UUID")] = None,
    all_events: Annotated[
        bool, typer.Option("--all", help="Every event where a method found a burn, in totals")
    ] = False,
) -> None:
    """Explain, pixel by pixel, where the dNBR and model maps disagree."""
    from narsat.db import connect
    from narsat.lake.storage import LakeStorage
    from narsat.pipeline.compare import compare_all, explain_disagreement, load_event_rasters

    if (event_id is None) == (not all_events):
        typer.echo("give either an event id or --all", err=True)
        raise typer.Exit(2)
    settings = _settings()
    storage = LakeStorage.assets(settings.lake)
    with connect(settings.db) as conn:
        if all_events:
            _echo_json(compare_all(conn, storage))
            return
        assert event_id is not None
        try:
            rasters, product = load_event_rasters(conn, storage, uuid.UUID(event_id))
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    _echo_json({"event_id": event_id, **explain_disagreement(rasters, **product)})


@map_app.command("evaluate")
def map_evaluate(
    source: Annotated[str, typer.Option(help="Reference source name, as loaded")] = "nealgeba",
    admin1: Annotated[
        str | None,
        typer.Option(help="Regular expression on first-level unit names: the reference's coverage"),
    ] = None,
    since: Annotated[
        str | None, typer.Option(help="Events first seen on or after YYYY-MM-DD")
    ] = None,
    until: Annotated[str | None, typer.Option(help="Events first seen before YYYY-MM-DD")] = None,
) -> None:
    """Accuracy of the burned-area maps against reference perimeters, pixel by pixel."""
    from narsat.db import connect
    from narsat.lake.storage import LakeStorage
    from narsat.pipeline.evaluate import EvaluationScope, evaluate

    settings = _settings()
    scope = EvaluationScope(
        source=source,
        admin1_pattern=admin1,
        since=dt.date.fromisoformat(since) if since else None,
        until=dt.date.fromisoformat(until) if until else None,
    )
    with connect(settings.db) as conn:
        try:
            result = evaluate(conn, LakeStorage.assets(settings.lake), scope)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    _echo_json(result)


@map_app.command("pending")
def map_pending(limit: Annotated[int, typer.Option(help="Maximum events this run")] = 10) -> None:
    """Map the events waiting in the queue. Exits 1 only if every attempt crashed."""
    from narsat.db import connect
    from narsat.pipeline.map_event import pending_events

    settings = _settings()
    with connect(settings.db) as conn:
        queue = pending_events(conn, limit)
    results = [_map_one(settings, event_id) for event_id in queue]
    _echo_json({"queued": len(queue), "results": results})
    if results and all(r["status"] == "error" for r in results):
        raise typer.Exit(1)


@map_app.command("queue")
def map_queue(limit: Annotated[int, typer.Option()] = 50) -> None:
    """Print the identifiers of the events waiting for mapping (one per line)."""
    from narsat.db import connect
    from narsat.pipeline.map_event import pending_events

    with connect(_settings().db) as conn:
        for event_id in pending_events(conn, limit):
            typer.echo(str(event_id))


# =============================================================================== users
@users_app.command("create")
def users_create(
    email: Annotated[str, typer.Argument()],
    role: Annotated[str, typer.Option(help="analyst or admin")] = "analyst",
    password: Annotated[
        str,
        typer.Option(
            prompt=True,
            hide_input=True,
            confirmation_prompt=True,
            envvar="NARSAT_NEW_USER_PASSWORD",
        ),
    ] = "",
) -> None:
    """Create or update an API user."""
    from narsat.api.auth import hash_password
    from narsat.db import connect

    if role not in ("analyst", "admin"):
        raise typer.BadParameter("role must be 'analyst' or 'admin'")
    if len(password) < 10:
        raise typer.BadParameter("the password must have at least 10 characters")
    with connect(_settings().db) as conn:
        conn.execute(
            """
            INSERT INTO app_user (email, password_hash, role)
            VALUES (%(email)s, %(hash)s, %(role)s)
            ON CONFLICT (email) DO UPDATE
            SET password_hash = EXCLUDED.password_hash, role = EXCLUDED.role, is_active = true
            """,
            {"email": email, "hash": hash_password(password), "role": role},
        )
    typer.echo(f"user {email} ready ({role})")


def main() -> None:
    """Console entry point: a configuration problem is reported in one line, not a traceback."""
    from narsat.errors import ConfigurationError

    try:
        app()
    except ConfigurationError as exc:
        typer.echo(f"configuration error: {exc}", err=True)
        sys.exit(2)


if __name__ == "__main__":  # pragma: no cover
    main()

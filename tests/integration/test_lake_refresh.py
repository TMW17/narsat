"""Bronze -> silver -> gold behind the quality gates, and mart publication."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from narsat.config import DbSettings, LakeSettings, Settings
from narsat.db import connect
from narsat.lake.bronze import BronzeHotspotWriter, BronzeRecord
from narsat.lake.catalog import ensure_tables, load_catalog
from narsat.pipeline import marts
from narsat.pipeline.lake_refresh import refresh
from narsat.quality.gates import GateFailed
from tests.conftest import make_hotspot
from tests.integration.helpers import (
    FIRE_START,
    fire_over_synthetic_burn,
    square,
    store_and_cluster,
)

pytestmark = pytest.mark.integration
UTC = dt.UTC
TOPIC = "firms.hotspots.raw"


def _settings(db: DbSettings, lake: LakeSettings) -> Settings:
    return Settings(db=db, lake=lake)


def _land(catalog, hotspots: list, start_offset: int = 0) -> None:  # type: ignore[no-untyped-def]
    records = [BronzeRecord(h, TOPIC, 0, start_offset + i) for i, h in enumerate(hotspots)]
    # Ingested a few hours after the newest acquisition of the batch.
    ingested = max(h.acq_ts for h in hotspots) + dt.timedelta(hours=4)
    BronzeHotspotWriter(catalog).append(records, now=ingested)


def _seed(db: DbSettings, lake: LakeSettings):  # type: ignore[no-untyped-def]
    """One mapped fire in PostGIS and the same detections (one of them twice) in bronze."""
    catalog = load_catalog(lake)
    ensure_tables(catalog)
    fire = fire_over_synthetic_burn()
    _land(catalog, [*fire, fire[2]])
    with connect(db) as conn:
        conn.execute(
            "INSERT INTO admin_unit (country_iso3, level, code, name, source, geom) VALUES "
            "('DZA', 0, 'DZA', 'Algeria', 'test', ST_GeomFromText(%(c)s, 4326)), "
            "('DZA', 1, 'DZ-06', 'Béjaïa', 'test', ST_GeomFromText(%(w)s, 4326))",
            {"c": square(3.0, 34.0, 5.0), "w": square(4.6, 36.7, 0.5)},
        )
        result = store_and_cluster(conn, fire)
        event_id = result.events_created[0]
        conn.execute(
            """
            INSERT INTO burned_area (ba_id, event_id, method, model_version, post_datetime,
                                     clear_fraction, area_ha, n_polygons, geom)
            VALUES (gen_random_uuid(), %(id)s, 'dnbr', 'dnbr-v1', '2023-07-28T10:30Z', 0.97,
                    254.0, 1, ST_GeomFromText(%(wkt)s, 4326))
            """,
            {"id": event_id, "wkt": square(4.60, 36.74, 0.008)},
        )
    return catalog, event_id


def test_refresh_builds_silver_and_gold_and_records_the_checks(
    db_settings: DbSettings, lake_settings: LakeSettings
) -> None:
    catalog, event_id = _seed(db_settings, lake_settings)
    with connect(db_settings) as conn:
        report = refresh(catalog, conn, _settings(db_settings, lake_settings))

    assert report.months == ["2023-07"]
    assert report.rows == {
        "silver.hotspots": 8,  # nine bronze rows, one duplicate
        "silver.fire_events": 1,
        "gold.burned_areas": 1,
        "gold.burned_area_validation": 0,
    }
    assert report.failed == []

    hotspots = catalog.load_table("silver.hotspots").scan().to_pandas()
    assert hotspots["hotspot_id"].is_unique
    assert hotspots["event_id"].unique().tolist() == [str(event_id)]
    assert hotspots["country_iso3"].unique().tolist() == ["DZA"]
    assert hotspots["admin1_name"].unique().tolist() == ["Béjaïa"]
    assert not hotspots["is_static_source"].any()

    events = catalog.load_table("silver.fire_events").scan().to_pandas()
    assert events.loc[0, "n_hotspots"] == 8
    assert events.loc[0, "admin1_name"] == "Béjaïa"
    assert isinstance(events.loc[0, "hull_wkb"], bytes)

    burned = catalog.load_table("gold.burned_areas").scan().to_pandas()
    assert burned.loc[0, "area_ha"] == 254.0
    assert burned.loc[0, "xmin"] < burned.loc[0, "xmax"]

    with connect(db_settings) as conn:
        recorded = conn.execute(
            "SELECT suite, status, count(*) AS n FROM dq_result WHERE run_id = %(run)s "
            "GROUP BY suite, status ORDER BY suite",
            {"run": report.run_id},
        ).fetchall()
    assert {(r["suite"], r["status"]) for r in recorded} == {
        ("gold.burned_areas", "pass"), ("silver.fire_events", "pass"),
        ("silver.hotspots", "pass"),
    }  # fmt: skip


def test_refresh_is_idempotent(db_settings: DbSettings, lake_settings: LakeSettings) -> None:
    catalog, _ = _seed(db_settings, lake_settings)
    settings = _settings(db_settings, lake_settings)
    with connect(db_settings) as conn:
        first = refresh(catalog, conn, settings)
        second = refresh(catalog, conn, settings)
    assert first.rows == second.rows
    assert catalog.load_table("silver.hotspots").scan().count() == 8
    assert catalog.load_table("silver.fire_events").scan().count() == 1


def test_a_failing_gate_blocks_publication_and_is_recorded(
    db_settings: DbSettings, lake_settings: LakeSettings
) -> None:
    catalog, _ = _seed(db_settings, lake_settings)
    settings = _settings(db_settings, lake_settings)
    with connect(db_settings) as conn:
        refresh(catalog, conn, settings)

    # A record that bypassed the stream sink's contract: a detection in Berlin.
    _land(catalog, [make_hotspot(52.5, 13.4, FIRE_START)], start_offset=100)
    with connect(db_settings) as conn, pytest.raises(GateFailed) as excinfo:
        refresh(catalog, conn, settings)
    assert any("latitude" in f.check_name for f in excinfo.value.failures)

    # Silver still holds the last good version...
    assert catalog.load_table("silver.hotspots").scan().count() == 8
    # ...and the failure is visible in the quality history even though the run aborted.
    with connect(db_settings) as conn:
        failed = conn.execute(
            "SELECT check_name FROM dq_result WHERE status = 'fail' AND severity = 'error'"
        ).fetchall()
    assert any("latitude" in row["check_name"] for row in failed)


def test_only_requested_months_are_rebuilt(
    db_settings: DbSettings, lake_settings: LakeSettings
) -> None:
    catalog, _ = _seed(db_settings, lake_settings)
    august = [
        make_hotspot(35.1 + 0.002 * i, 1.2, dt.datetime(2023, 8, 5, 12, i, tzinfo=UTC))
        for i in range(3)
    ]
    _land(catalog, august, start_offset=200)
    with connect(db_settings) as conn:
        store_and_cluster(conn, august)
        report = refresh(catalog, conn, _settings(db_settings, lake_settings), ["2023-08"])
    assert report.rows["silver.hotspots"] == 3
    months = catalog.load_table("silver.hotspots").scan().to_pandas()["acq_month"].unique()
    assert months.tolist() == ["2023-08"]


def _write_marts(directory: Path, event_id: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({
            "activity_date": [dt.date(2023, 7, 22), dt.date(2023, 7, 23)],
            "country_iso3": ["DZA", "DZA"],
            "admin1_name": ["Béjaïa", "Béjaïa"],
            "n_hotspots": [2, 6],
            "n_events": [1, 1],
            "frp_sum_mw": [41.0, 147.0],
            "extra_column_from_dbt": ["ignored", "ignored"],
        }),
        directory / "fire_activity_daily.parquet",
    )  # fmt: skip
    pq.write_table(
        pa.table({
            "fire_year": [2023], "country_iso3": ["DZA"], "admin1_name": ["Béjaïa"],
            "method": ["dnbr"], "n_events": [1], "area_ha": [254.0],
        }),
        directory / "burned_area_admin_year.parquet",
    )  # fmt: skip
    pq.write_table(
        pa.table({
            "event_id": [event_id], "country_iso3": ["DZA"], "admin1_name": ["Béjaïa"],
            "first_seen": [dt.datetime(2023, 7, 22, 12, tzinfo=UTC)],
            "last_seen": [dt.datetime(2023, 7, 24, 6, tzinfo=UTC)],
            "duration_hours": [42.0], "n_hotspots": [8], "frp_max_mw": [27.0],
            "area_dnbr_ha": [254.0], "area_model_ha": pa.array([None], type=pa.float64()),
            "method_iou": pa.array([None], type=pa.float64()),
        }),
        directory / "event_summary.parquet",
    )  # fmt: skip


def test_marts_are_published_atomically_and_replace_previous_content(
    db_settings: DbSettings, tmp_path: Path
) -> None:
    event_id = "0e7c6a2e-58a0-5d0c-9a7b-1f6f4f4f9a11"
    _write_marts(tmp_path / "marts", event_id)
    with connect(db_settings) as conn:
        assert marts.publish_all(conn, tmp_path / "marts") == {
            "fire_activity_daily": 2, "burned_area_admin_year": 1, "event_summary": 1,
        }  # fmt: skip
        assert marts.publish_all(conn, tmp_path / "marts")["fire_activity_daily"] == 2
        daily = conn.execute(
            "SELECT activity_date, n_hotspots FROM mart.fire_activity_daily ORDER BY 1"
        ).fetchall()
        summary = conn.execute(
            "SELECT event_id::text AS event_id, area_dnbr_ha, area_model_ha FROM mart.event_summary"
        ).fetchone()
    assert [(d["activity_date"], d["n_hotspots"]) for d in daily] == [
        (dt.date(2023, 7, 22), 2), (dt.date(2023, 7, 23), 6),
    ]  # fmt: skip
    assert summary == {"event_id": event_id, "area_dnbr_ha": 254.0, "area_model_ha": None}


def test_mart_with_missing_columns_is_refused(db_settings: DbSettings, tmp_path: Path) -> None:
    pq.write_table(pa.table({"fire_year": [2023]}), tmp_path / "burned_area_admin_year.parquet")
    with connect(db_settings) as conn, pytest.raises(marts.MartSchemaError, match="lacks columns"):
        marts.publish_mart(
            conn, "burned_area_admin_year", tmp_path / "burned_area_admin_year.parquet"
        )
    with connect(db_settings) as conn, pytest.raises(FileNotFoundError, match="dbt build"):
        marts.publish_all(conn, tmp_path / "nothing-here")

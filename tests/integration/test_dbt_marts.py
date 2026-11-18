"""The analytics path end to end: PostGIS -> Iceberg -> dbt (DuckDB) -> Parquet -> PostgreSQL.

``dbt build`` is run as a subprocess against a lakehouse built by the real refresh, with
the dbt project of the repository. The expected figures are worked out by hand from the
seeded fires.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from narsat.config import DbSettings, LakeSettings, Settings
from narsat.db import connect
from narsat.lake.bronze import BronzeHotspotWriter, BronzeRecord
from narsat.lake.catalog import ensure_tables, load_catalog
from narsat.pipeline import marts
from narsat.pipeline.lake_refresh import refresh
from narsat.pipeline.validate import compute_agreement
from tests.conftest import make_hotspot
from tests.integration.helpers import fire_over_synthetic_burn, square, store_and_cluster

pytest.importorskip("dbt.adapters.duckdb", reason="install the 'transform' extra to run dbt")

pytestmark = pytest.mark.integration
UTC = dt.UTC
PROJECT_DIR = Path(__file__).resolve().parents[2] / "transform"
DBT = Path(sys.executable).parent / "dbt"


def _seed(db: DbSettings, lake: LakeSettings) -> dict[str, str]:
    """Three fires.

    * ``kabylie``: 8 detections over three days in Béjaïa, mapped by both methods.
    * ``late``: detections just before midnight UTC (already the next day in Algiers), in
      Algeria but outside every first-level unit; its only product was rejected. It starts
      as two separate events that a later detection joins: one of them ends up "merged".
    * ``morocco``: a single detection where no boundary is loaded.
    """
    kabylie = fire_over_synthetic_burn()
    late = [
        make_hotspot(
            35.1 + 0.002 * i, 1.2, dt.datetime(2023, 8, 5, 23, 10 + 10 * i, tzinfo=UTC), frp=5.0
        )
        for i in range(3)
    ]
    evening = dt.datetime(2023, 8, 5, 23, 0, tzinfo=UTC)
    # 2.9 km north of the first group (further than the 2 km linking distance)...
    north = [
        make_hotspot(35.130 + 0.002 * i, 1.2, evening + dt.timedelta(minutes=15 + 10 * i), frp=5.0)
        for i in range(2)
    ]
    # ...until a detection half-way links the two groups.
    bridge = [make_hotspot(35.117, 1.2, evening + dt.timedelta(minutes=40), frp=5.0)]
    morocco = [make_hotspot(33.0, -7.5, dt.datetime(2023, 8, 10, 12, 0, tzinfo=UTC), frp=7.5)]
    everything = [*kabylie, *late, *north, *bridge, *morocco]

    catalog = load_catalog(lake)
    ensure_tables(catalog)
    BronzeHotspotWriter(catalog).append(
        [BronzeRecord(h, "firms.hotspots.raw", 0, i) for i, h in enumerate(everything)],
        now=dt.datetime(2023, 8, 10, 16, 0, tzinfo=UTC),
    )
    with connect(db) as conn:
        conn.execute(
            "INSERT INTO admin_unit (country_iso3, level, code, name, source, geom) VALUES "
            "('DZA', 0, 'DZA', 'Algeria', 'test', ST_GeomFromText(%(c)s, 4326)), "
            "('DZA', 1, 'DZ-06', 'Béjaïa', 'test', ST_GeomFromText(%(w)s, 4326))",
            {"c": square(3.0, 34.0, 5.0), "w": square(4.6, 36.7, 0.5)},
        )
        store_and_cluster(conn, [*kabylie, *late, *north, *morocco])
        merged = store_and_cluster(conn, bridge).events_merged
        assert len(merged) == 1
        events = {
            name: str(conn.execute(
                "SELECT event_id FROM hotspot WHERE hotspot_id = %(id)s", {"id": fire[0].hotspot_id}
            ).fetchone()["event_id"])
            for name, fire in (("kabylie", kabylie), ("late", late), ("morocco", morocco))
        }  # fmt: skip
        insert = """
            INSERT INTO burned_area (ba_id, event_id, method, model_version, post_datetime,
                                     clear_fraction, area_ha, n_polygons, qa_status, geom)
            VALUES (gen_random_uuid(), %(id)s, %(method)s, %(version)s, '2023-07-28T10:30Z',
                    0.97, %(area)s, 1, %(qa)s, ST_GeomFromText(%(wkt)s, 4326))
        """
        products = [
            (events["kabylie"], "dnbr", "dnbr-v1", 254.0, "auto", square(4.60, 36.74, 0.008)),
            (events["kabylie"], "model", "prithvi-test", 200.0, "accepted",
             square(4.60, 36.74, 0.007)),
            (events["late"], "dnbr", "dnbr-v1", 999.0, "rejected", square(1.2, 35.1, 0.01)),
        ]  # fmt: skip
        for event_id, method, version, area, qa, wkt in products:
            conn.execute(insert, {"id": event_id, "method": method, "version": version,
                                  "area": area, "qa": qa, "wkt": wkt})  # fmt: skip
        compute_agreement(conn)
        refresh(catalog, conn, Settings(db=db, lake=lake))
    return events


def _dbt_build(lake: LakeSettings, workdir: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "NARSAT_LAKE_CATALOG_URI": lake.catalog_uri.get_secret_value(),
        "NARSAT_LAKE_WAREHOUSE_URI": lake.warehouse_uri,
        "NARSAT_MARTS_DIR": str(workdir / "marts"),
        # The host of this test may be in any time zone; the profile pins DuckDB to UTC.
        "TZ": "Africa/Algiers",
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    return subprocess.run(
        [
            str(DBT), "build", "--project-dir", str(PROJECT_DIR), "--profiles-dir", str(PROJECT_DIR),
            "--target-path", str(workdir / "target"), "--log-path", str(workdir / "logs"),
            "--no-use-colors",
        ],
        cwd=workdir, env=env, capture_output=True, text=True, timeout=600, check=False,
    )  # fmt: skip


def test_dbt_builds_the_marts_and_they_reach_the_api_database(
    db_settings: DbSettings, lake_settings: LakeSettings, tmp_path: Path
) -> None:
    events = _seed(db_settings, lake_settings)

    result = _dbt_build(lake_settings, tmp_path)
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]
    assert "ERROR=0" in result.stdout
    assert "WARN=0" in result.stdout

    # --- the Parquet files dbt wrote -------------------------------------------
    daily = pq.read_table(tmp_path / "marts" / "fire_activity_daily.parquet").to_pylist()
    by_day = {(r["activity_date"], r["country_iso3"], r["admin1_name"]): r for r in daily}
    assert {
        key: (r["n_hotspots"], r["n_events"], r["frp_sum_mw"]) for key, r in by_day.items()
    } == {
        (dt.date(2023, 7, 22), "DZA", "Béjaïa"): (2, 1, 41.0),
        (dt.date(2023, 7, 23), "DZA", "Béjaïa"): (4, 1, 94.0),
        (dt.date(2023, 7, 24), "DZA", "Béjaïa"): (2, 1, 53.0),
        # 23:10-23:30 UTC stays on 5 August although it is already the 6th in Algiers.
        (dt.date(2023, 8, 5), "DZA", "Unknown"): (6, 1, 30.0),
        (dt.date(2023, 8, 10), "UNK", "Unknown"): (1, 1, 7.5),
    }

    yearly = pq.read_table(tmp_path / "marts" / "burned_area_admin_year.parquet").to_pylist()
    assert sorted(yearly, key=lambda r: r["method"]) == [
        {
            "fire_year": 2023,
            "country_iso3": "DZA",
            "admin1_name": "Béjaïa",
            "method": "dnbr",
            "n_events": 1,
            "area_ha": 254.0,
        },
        {
            "fire_year": 2023,
            "country_iso3": "DZA",
            "admin1_name": "Béjaïa",
            "method": "model",
            "n_events": 1,
            "area_ha": 200.0,
        },
    ]  # the rejected 999 ha product is not reported

    summary = {
        r["event_id"]: r
        for r in pq.read_table(tmp_path / "marts" / "event_summary.parquet").to_pylist()
    }
    assert set(summary) == set(events.values())
    kabylie = summary[events["kabylie"]]
    assert kabylie["duration_hours"] == 42.0
    assert (kabylie["n_hotspots"], kabylie["frp_max_mw"]) == (8, 27.0)
    assert (kabylie["area_dnbr_ha"], kabylie["area_model_ha"]) == (254.0, 200.0)
    # Nested squares of half-width 0.007 and 0.008 degrees: IoU = (7/8)^2.
    assert kabylie["method_iou"] == pytest.approx(0.7656, abs=0.002)
    late = summary[events["late"]]
    assert late["duration_hours"] == pytest.approx(30 / 60)
    assert late["n_hotspots"] == 6  # both groups and the bridge
    assert (late["area_dnbr_ha"], late["area_model_ha"], late["method_iou"]) == (None, None, None)
    assert summary[events["morocco"]]["duration_hours"] == 0.0

    # --- published to PostgreSQL, where the API reads them ----------------------
    with connect(db_settings) as conn:
        published = marts.publish_all(conn, tmp_path / "marts")
        assert published == {
            "fire_activity_daily": 5, "burned_area_admin_year": 2, "event_summary": 3,
        }  # fmt: skip
        totals = conn.execute(
            "SELECT sum(n_hotspots)::int AS hotspots, "
            "(SELECT count(*)::int FROM hotspot) AS stored FROM mart.fire_activity_daily"
        ).fetchone()
        assert totals == {"hotspots": 15, "stored": 15}
        # The lake keeps the absorbed event; the marts do not report it.
        retired = conn.execute(
            "SELECT count(*)::int AS n FROM fire_event WHERE status = 'merged'"
        ).fetchone()
        assert retired == {"n": 1}
        row = conn.execute(
            "SELECT m.area_dnbr_ha, e.n_hotspots FROM mart.event_summary m "
            "JOIN fire_event e USING (event_id) WHERE m.event_id = %(id)s",
            {"id": events["kabylie"]},
        ).fetchone()
        assert row == {"area_dnbr_ha": 254.0, "n_hotspots": 8}


def test_an_empty_lakehouse_yields_empty_marts(
    db_settings: DbSettings, lake_settings: LakeSettings, tmp_path: Path
) -> None:
    """First run of a new installation: the tables exist but hold nothing yet."""
    ensure_tables(load_catalog(lake_settings))
    result = _dbt_build(lake_settings, tmp_path)
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]

    # dbt-duckdb writes a placeholder row of NULLs for an empty model...
    placeholder = pq.read_table(tmp_path / "marts" / "event_summary.parquet").to_pylist()
    assert len(placeholder) == 1
    assert set(placeholder[0].values()) == {None}
    # ...which must not reach the API database (its key columns are NOT NULL).
    with connect(db_settings) as conn:
        conn.execute(
            "INSERT INTO mart.burned_area_admin_year VALUES (2022, 'DZA', 'Old', 'dnbr', 1, 1.0)"
        )
        assert marts.publish_all(conn, tmp_path / "marts") == {
            "fire_activity_daily": 0, "burned_area_admin_year": 0, "event_summary": 0,
        }  # fmt: skip
        remaining = conn.execute(
            "SELECT count(*)::int AS n FROM mart.burned_area_admin_year"
        ).fetchone()
    assert remaining == {"n": 0}

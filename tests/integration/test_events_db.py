"""Hotspot storage and fire-event maintenance against a real PostGIS."""

from __future__ import annotations

import datetime as dt

import pytest

from narsat.config import DbSettings
from narsat.db import connect, migrate
from narsat.db.hotspots import backfill_admin, insert_hotspots
from tests import synthetic
from tests.conftest import make_hotspot
from tests.integration.helpers import (
    AFTER_FIRE,
    FIRE_START,
    fire_over_synthetic_burn,
    square,
    store_and_cluster,
    updater,
)

pytestmark = pytest.mark.integration
UTC = dt.UTC


def _flare(days: int, start: dt.datetime = FIRE_START) -> list:
    return [make_hotspot(31.6712, 6.0703, start - dt.timedelta(days=d)) for d in range(days)]


def test_schema_is_at_head_and_has_postgis(db_settings: DbSettings) -> None:
    assert migrate.current_revision(db_settings) == "0001"
    with connect(db_settings) as conn:
        row = conn.execute("SELECT postgis_version() AS v").fetchone()
        assert row is not None
        tables = conn.execute(
            "SELECT count(*) AS n FROM information_schema.tables "
            "WHERE table_schema IN ('public', 'mart') AND table_type = 'BASE TABLE'"
        ).fetchone()
    assert tables is not None
    assert tables["n"] >= 13  # 10 operational + 3 marts (+ alembic_version, spatial_ref_sys)


def test_insert_is_idempotent_and_fills_the_generated_geometry(db_settings: DbSettings) -> None:
    fire = fire_over_synthetic_burn()
    with connect(db_settings) as conn:
        assert len(insert_hotspots(conn, fire)) == 8
        assert insert_hotspots(conn, fire) == []
        assert len(insert_hotspots(conn, [*fire[:3], make_hotspot(36.5, 5.0, FIRE_START)])) == 1
        row = conn.execute(
            "SELECT ST_X(geom) AS x, ST_Y(geom) AS y, ST_SRID(geom) AS srid, longitude, latitude "
            "FROM hotspot WHERE hotspot_id = %(id)s",
            {"id": fire[0].hotspot_id},
        ).fetchone()
    assert row is not None
    assert (row["x"], row["y"], row["srid"]) == (row["longitude"], row["latitude"], 4326)


def test_standard_processing_type_marks_static_sources(db_settings: DbSettings) -> None:
    with connect(db_settings) as conn:
        insert_hotspots(conn, [
            make_hotspot(36.7, 4.6, FIRE_START, type_code=0),
            make_hotspot(31.6, 6.0, FIRE_START, type_code=2),
            make_hotspot(36.9, 7.7, FIRE_START, type_code=None),
        ])  # fmt: skip
        flags = conn.execute(
            "SELECT type_code, is_static_source FROM hotspot ORDER BY type_code NULLS LAST"
        ).fetchall()
    assert [(f["type_code"], f["is_static_source"]) for f in flags] == [
        (0, False), (2, True), (None, False),
    ]  # fmt: skip


def test_events_are_created_and_keep_their_identity_as_they_grow(db_settings: DbSettings) -> None:
    fire = fire_over_synthetic_burn()
    with connect(db_settings) as conn:
        first = store_and_cluster(conn, fire[:4], as_of=FIRE_START + dt.timedelta(days=1))
        assert len(first.events_created) == 1
        event_id = first.events_created[0]

        second = store_and_cluster(conn, fire[4:], as_of=FIRE_START + dt.timedelta(days=2))
        assert second.events_created == []
        assert second.events_upserted == [event_id]

        row = conn.execute(
            "SELECT status, n_hotspots, n_high_confidence, frp_max_mw, sensors, "
            "ST_IsValid(hull) AS valid, hull_area_ha, "
            "ST_Contains(hull, centroid) AS centroid_inside FROM fire_event"
        ).fetchone()
        assert row is not None
        assert (row["status"], row["n_hotspots"], row["n_high_confidence"]) == ("active", 8, 4)
        assert row["frp_max_mw"] == 27.0
        assert row["sensors"] == ["VIIRS/N20"]
        assert row["valid"]
        assert row["centroid_inside"]
        assert 150 < row["hull_area_ha"] < 600

        # Re-running with nothing new rewrites nothing.
        again = updater().update(
            conn, as_of=FIRE_START + dt.timedelta(days=2), touched=(fire[0].acq_ts, fire[-1].acq_ts)
        )
        assert again.events_upserted == []


def test_time_moves_events_to_closed_and_into_the_mapping_queue(db_settings: DbSettings) -> None:
    with connect(db_settings) as conn:
        store_and_cluster(conn, fire_over_synthetic_burn(), as_of=FIRE_START + dt.timedelta(days=2))
        states = []
        for days in (2, 4, 6):
            updater().update(conn, as_of=FIRE_START + dt.timedelta(days=days))
            row = conn.execute("SELECT status, mapping_status FROM fire_event").fetchone()
            assert row is not None
            states.append((row["status"], row["mapping_status"]))
    # Last detection is 42 h after FIRE_START: active (<=48 h quiet), cooling (<=3 d), closed.
    assert states == [("active", "not_ready"), ("cooling", "not_ready"), ("closed", "pending")]


def test_single_detection_events_are_not_queued_for_mapping(db_settings: DbSettings) -> None:
    with connect(db_settings) as conn:
        store_and_cluster(conn, [make_hotspot(35.2, 1.4, FIRE_START)])
        row = conn.execute("SELECT status, mapping_status, mapping_note FROM fire_event").fetchone()
    assert row is not None
    assert (row["status"], row["mapping_status"]) == ("closed", "skipped")
    assert "minimum" in row["mapping_note"]


def test_a_bridging_detection_merges_two_events(db_settings: DbSettings) -> None:
    west = [
        make_hotspot(36.700, 4.600 + 0.004 * i, FIRE_START + dt.timedelta(hours=i))
        for i in range(3)
    ]
    east = [
        make_hotspot(36.700, 4.660 + 0.004 * i, FIRE_START + dt.timedelta(hours=5 + i))
        for i in range(3)
    ]
    with connect(db_settings) as conn:
        result = store_and_cluster(conn, west + east)
        assert len(result.events_created) == 2
        older = conn.execute(
            "SELECT event_id FROM fire_event ORDER BY first_seen LIMIT 1"
        ).fetchone()
        assert older is not None
        # A product on the younger event, to check that retiring it removes the product.
        younger = next(e for e in result.events_created if e != older["event_id"])
        conn.execute(
            """
            INSERT INTO burned_area (ba_id, event_id, method, model_version, post_datetime,
                                     clear_fraction, area_ha, n_polygons, geom)
            VALUES (gen_random_uuid(), %(id)s, 'dnbr', 'dnbr-v1', now(), 1, 10, 1,
                    ST_GeomFromText(%(wkt)s, 4326))
            """,
            {"id": younger, "wkt": square(4.66, 36.70, 0.002)},
        )

        # The fire front crosses the gap: detections every ~1.4 km between the two.
        bridge = [
            make_hotspot(36.700, 4.612 + 0.016 * i, FIRE_START + dt.timedelta(hours=9))
            for i in range(3)
        ]
        merged = store_and_cluster(conn, bridge)
        assert merged.events_merged == {younger: older["event_id"]}

        rows = conn.execute(
            "SELECT event_id, status, n_hotspots, merged_into FROM fire_event ORDER BY n_hotspots"
        ).fetchall()
        assert [(r["status"], r["n_hotspots"]) for r in rows] == [("merged", 0), ("closed", 9)]
        assert rows[0]["merged_into"] == older["event_id"]
        owners = conn.execute("SELECT DISTINCT event_id FROM hotspot").fetchall()
        assert [o["event_id"] for o in owners] == [older["event_id"]]
        assert conn.execute("SELECT count(*) AS n FROM burned_area").fetchone() == {"n": 0}


def test_a_gas_flare_is_recognised_and_never_becomes_an_event(db_settings: DbSettings) -> None:
    with connect(db_settings) as conn:
        result = store_and_cluster(conn, fire_over_synthetic_burn() + _flare(15))
        assert result.static_cells_added == 1
        assert result.hotspots_flagged_static == 15
        assert len(result.events_created) == 1
        events = conn.execute("SELECT n_hotspots FROM fire_event").fetchall()
        assert [e["n_hotspots"] for e in events] == [8]

        # A later detection in the known cell is flagged at insert time.
        late = make_hotspot(31.6715, 6.0701, FIRE_START + dt.timedelta(days=30))
        insert_hotspots(conn, [late])
        row = conn.execute(
            "SELECT is_static_source FROM hotspot WHERE hotspot_id = %(id)s",
            {"id": late.hotspot_id},
        ).fetchone()
    assert row == {"is_static_source": True}


def test_an_event_that_turns_out_to_be_a_flare_is_dissolved(db_settings: DbSettings) -> None:
    flare = _flare(15)
    with connect(db_settings) as conn:
        # First week: not enough days to tell, so the detections form an ordinary event.
        early = sorted(flare, key=lambda h: h.acq_ts)[:6]
        first = store_and_cluster(conn, early, as_of=FIRE_START)
        assert len(first.events_created) == 1
        # With more days of data the cell crosses the persistence threshold.
        later = store_and_cluster(conn, sorted(flare, key=lambda h: h.acq_ts)[6:])
        assert later.events_dissolved == first.events_created
        row = conn.execute("SELECT status, n_hotspots, mapping_status FROM fire_event").fetchone()
        assert row == {"status": "dissolved", "n_hotspots": 0, "mapping_status": "skipped"}
        assert conn.execute(
            "SELECT count(*) AS n FROM hotspot WHERE event_id IS NOT NULL"
        ).fetchone() == {"n": 0}


def test_full_recluster_matches_the_incremental_result(db_settings: DbSettings) -> None:
    fire = fire_over_synthetic_burn()
    other = [
        make_hotspot(34.9 + 0.003 * i, -1.3, FIRE_START + dt.timedelta(days=3, hours=i))
        for i in range(5)
    ]
    with connect(db_settings) as conn:
        for batch in (fire[:3], other[:2], fire[3:], other[2:]):
            store_and_cluster(conn, batch)
        before = conn.execute("SELECT hotspot_id, event_id FROM hotspot ORDER BY 1").fetchall()
        result = updater().update(conn, as_of=AFTER_FIRE, full=True)
        after = conn.execute("SELECT hotspot_id, event_id FROM hotspot ORDER BY 1").fetchall()
    assert before == after
    assert result.events_upserted == []
    assert result.hotspots_considered == 13


def test_admin_attribution_and_country_filter(db_settings: DbSettings) -> None:
    fire = fire_over_synthetic_burn()
    spain = [
        make_hotspot(36.75 + 0.003 * i, -4.40, FIRE_START + dt.timedelta(hours=i)) for i in range(4)
    ]
    with connect(db_settings) as conn:
        for iso3, level, code, name, lon, lat, half in [
            ("DZA", 0, "DZA", "Algeria", 3.0, 34.0, 5.0),
            ("DZA", 1, "DZ-06", "Béjaïa", 4.6, 36.7, 0.5),
            ("ESP", 0, "ESP", "Spain", -4.4, 37.5, 1.2),
        ]:
            conn.execute(
                "INSERT INTO admin_unit (country_iso3, level, code, name, source, geom) "
                "VALUES (%(iso3)s, %(level)s, %(code)s, %(name)s, 'test', "
                "ST_GeomFromText(%(wkt)s, 4326))",
                {
                    "iso3": iso3,
                    "level": level,
                    "code": code,
                    "name": name,
                    "wkt": square(lon, lat, half),
                },
            )
        store_and_cluster(conn, fire + spain)
        events = conn.execute(
            "SELECT e.country_iso3, a.name, e.mapping_status, e.mapping_note FROM fire_event e "
            "LEFT JOIN admin_unit a ON a.admin_id = e.admin1_id ORDER BY e.country_iso3"
        ).fetchall()
        assert [(e["country_iso3"], e["name"], e["mapping_status"]) for e in events] == [
            ("DZA", "Béjaïa", "pending"),
            ("ESP", None, "skipped"),
        ]
        assert "outside the configured countries" in events[1]["mapping_note"]
        hotspots = conn.execute(
            "SELECT country_iso3, count(*) AS n, count(admin1_id) AS with_admin1 "
            "FROM hotspot GROUP BY 1 ORDER BY 1"
        ).fetchall()
        assert [(h["country_iso3"], h["n"], h["with_admin1"]) for h in hotspots] == [
            ("DZA", 8, 8), ("ESP", 4, 0),
        ]  # fmt: skip
        assert backfill_admin(conn) == 0  # nothing left to attribute


def _add_unit(conn, iso3: str, level: int, name: str, lon: float, lat: float, half: float) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        "INSERT INTO admin_unit (country_iso3, level, code, name, source, geom) "
        "VALUES (%(iso3)s, %(level)s, %(code)s, %(name)s, 'test', ST_GeomFromText(%(wkt)s, 4326))",
        {"iso3": iso3, "level": level, "code": f"{iso3}-{name}", "name": name,
         "wkt": square(lon, lat, half)},
    )  # fmt: skip


def test_events_created_before_the_boundaries_are_attributed_later(db_settings: DbSettings) -> None:
    """The live feed starts before `reference load-admin` has run."""
    fire = fire_over_synthetic_burn()
    with connect(db_settings) as conn:
        # While the fire burns, nothing is known about boundaries.
        store_and_cluster(conn, fire, as_of=FIRE_START + dt.timedelta(hours=12))
        assert conn.execute("SELECT country_iso3 FROM fire_event").fetchone() == {
            "country_iso3": None
        }
        lon, lat = synthetic.truth_20m().centre_lonlat
        _add_unit(conn, "DZA", 0, "Algeria", 3.0, 34.0, 5.0)
        _add_unit(conn, "DZA", 1, "Bejaia", lon, lat, 0.5)
        # The next scheduled update attributes it, without any new detection.
        updater().update(conn, as_of=AFTER_FIRE)
        row = conn.execute(
            "SELECT e.country_iso3, a.name, e.mapping_status FROM fire_event e "
            "JOIN admin_unit a ON a.admin_id = e.admin1_id"
        ).fetchone()
    assert row == {"country_iso3": "DZA", "name": "Bejaia", "mapping_status": "pending"}


def test_a_coastal_fire_just_offshore_of_the_boundary_is_attributed(
    db_settings: DbSettings,
) -> None:
    fire = fire_over_synthetic_burn()
    lon, lat = synthetic.truth_20m().centre_lonlat
    with connect(db_settings) as conn:
        # The unit's edge runs 0.02 degrees (about 2 km) south of the fire.
        _add_unit(conn, "DZA", 0, "Algeria", lon, lat - 0.52, 0.5)
        _add_unit(conn, "DZA", 1, "Coast", lon, lat - 0.52, 0.5)
        store_and_cluster(conn, fire)
        row = conn.execute(
            "SELECT e.country_iso3, a.name, e.mapping_status FROM fire_event e "
            "LEFT JOIN admin_unit a ON a.admin_id = e.admin1_id"
        ).fetchone()
    assert row == {"country_iso3": "DZA", "name": "Coast", "mapping_status": "pending"}


def test_fires_in_no_configured_country_are_not_mapped(db_settings: DbSettings) -> None:
    """A fire in Spain, whose boundaries are not loaded, has no country at all."""
    spain = [
        make_hotspot(37.2 + 0.003 * i, -4.40, FIRE_START + dt.timedelta(hours=i)) for i in range(4)
    ]
    with connect(db_settings) as conn:
        _add_unit(conn, "DZA", 0, "Algeria", 3.0, 30.0, 5.0)
        store_and_cluster(conn, spain)
        row = conn.execute(
            "SELECT country_iso3, mapping_status, mapping_note FROM fire_event"
        ).fetchone()
    assert row["country_iso3"] is None
    assert row["mapping_status"] == "skipped"
    assert row["mapping_note"] == "outside the configured countries"


def test_without_any_boundary_loaded_fires_are_still_mapped(db_settings: DbSettings) -> None:
    """Before boundaries exist there is no basis to exclude anything."""
    with connect(db_settings) as conn:
        store_and_cluster(conn, fire_over_synthetic_burn())
        row = conn.execute("SELECT mapping_status FROM fire_event").fetchone()
    assert row == {"mapping_status": "pending"}

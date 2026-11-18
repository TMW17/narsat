"""Initial operational schema.

Revision ID: 0001
Revises:
Create Date: 2026-11-11
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = [
    "CREATE EXTENSION IF NOT EXISTS postgis",
    "CREATE EXTENSION IF NOT EXISTS citext",
    # ------------------------------------------------------------------ reference
    """
    CREATE TABLE admin_unit (
        admin_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        country_iso3  CHAR(3) NOT NULL,
        level         SMALLINT NOT NULL CHECK (level BETWEEN 0 AND 3),
        code          TEXT NOT NULL,
        name          TEXT NOT NULL,
        source        TEXT NOT NULL,
        licence       TEXT,
        geom          geometry(MultiPolygon, 4326) NOT NULL,
        loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (country_iso3, level, code)
    )
    """,
    "CREATE INDEX admin_unit_geom_gix ON admin_unit USING GIST (geom)",
    "CREATE INDEX admin_unit_level_idx ON admin_unit (level, country_iso3)",
    """
    CREATE TABLE app_user (
        user_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        email          CITEXT NOT NULL UNIQUE,
        password_hash  TEXT NOT NULL,
        role           TEXT NOT NULL CHECK (role IN ('analyst', 'admin')),
        is_active      BOOLEAN NOT NULL DEFAULT true,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Grid cells that behave like fixed heat sources (see narsat.events.static_sources).
    """
    CREATE TABLE static_cell (
        cell_y            INTEGER NOT NULL,
        cell_x            INTEGER NOT NULL,
        cell_deg          DOUBLE PRECISION NOT NULL,
        n_days            INTEGER NOT NULL,
        origin            TEXT NOT NULL DEFAULT 'persistence'
                          CHECK (origin IN ('persistence', 'manual')),
        first_flagged_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (cell_y, cell_x)
    )
    """,
    # ------------------------------------------------------------------ fire events
    """
    CREATE TABLE fire_event (
        event_id           UUID PRIMARY KEY,
        seed_hotspot_id    UUID NOT NULL,
        status             TEXT NOT NULL
                           CHECK (status IN ('active', 'cooling', 'closed', 'merged', 'dissolved')),
        mapping_status     TEXT NOT NULL DEFAULT 'not_ready'
                           CHECK (mapping_status IN ('not_ready', 'pending', 'imagery_pending',
                                                     'mapped', 'failed', 'skipped')),
        mapping_note       TEXT,
        mapping_attempts   INTEGER NOT NULL DEFAULT 0,
        mapping_checked_at TIMESTAMPTZ,
        mapped_at          TIMESTAMPTZ,
        mapped_n_hotspots  INTEGER,
        first_seen         TIMESTAMPTZ NOT NULL,
        last_seen          TIMESTAMPTZ NOT NULL,
        n_hotspots         INTEGER NOT NULL CHECK (n_hotspots >= 0),
        n_high_confidence  INTEGER NOT NULL DEFAULT 0,
        frp_max_mw         DOUBLE PRECISION,
        frp_sum_mw         DOUBLE PRECISION,
        sensors            TEXT[] NOT NULL DEFAULT '{}',
        country_iso3       CHAR(3),
        admin1_id          BIGINT REFERENCES admin_unit (admin_id) ON DELETE SET NULL,
        centroid           geometry(Point, 4326) NOT NULL,
        hull               geometry(Polygon, 4326) NOT NULL,
        hull_area_ha       DOUBLE PRECISION NOT NULL,
        merged_into        UUID REFERENCES fire_event (event_id),
        created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK (first_seen <= last_seen)
    )
    """,
    "CREATE INDEX fire_event_hull_gix ON fire_event USING GIST (hull)",
    "CREATE INDEX fire_event_last_seen_idx ON fire_event (last_seen DESC)",
    "CREATE INDEX fire_event_country_idx ON fire_event (country_iso3, first_seen)",
    """
    CREATE INDEX fire_event_mapping_queue_idx ON fire_event (last_seen)
        WHERE mapping_status IN ('pending', 'imagery_pending')
    """,
    # ------------------------------------------------------------------ hotspots
    """
    CREATE TABLE hotspot (
        hotspot_id        UUID PRIMARY KEY,
        source            TEXT NOT NULL,
        satellite         TEXT NOT NULL,
        instrument        TEXT NOT NULL CHECK (instrument IN ('VIIRS', 'MODIS')),
        acq_ts            TIMESTAMPTZ NOT NULL,
        latitude          DOUBLE PRECISION NOT NULL CHECK (latitude BETWEEN -90 AND 90),
        longitude         DOUBLE PRECISION NOT NULL CHECK (longitude BETWEEN -180 AND 180),
        geom              geometry(Point, 4326) GENERATED ALWAYS AS
                          (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)) STORED,
        brightness_k      DOUBLE PRECISION,
        brightness_2_k    DOUBLE PRECISION,
        scan_km           DOUBLE PRECISION,
        track_km          DOUBLE PRECISION,
        frp_mw            DOUBLE PRECISION,
        confidence_raw    TEXT NOT NULL,
        confidence_level  TEXT NOT NULL CHECK (confidence_level IN ('low', 'nominal', 'high')),
        daynight          CHAR(1),
        version           TEXT,
        type_code         SMALLINT,
        is_static_source  BOOLEAN NOT NULL DEFAULT false,
        country_iso3      CHAR(3),
        admin1_id         BIGINT REFERENCES admin_unit (admin_id) ON DELETE SET NULL,
        event_id          UUID REFERENCES fire_event (event_id) ON DELETE SET NULL,
        fetched_at        TIMESTAMPTZ NOT NULL,
        ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX hotspot_geom_gix ON hotspot USING GIST (geom)",
    "CREATE INDEX hotspot_acq_ts_idx ON hotspot (acq_ts)",
    "CREATE INDEX hotspot_event_idx ON hotspot (event_id)",
    "CREATE INDEX hotspot_ingested_idx ON hotspot (ingested_at)",
    # ------------------------------------------------------------------ products
    """
    CREATE TABLE burned_area (
        ba_id           UUID PRIMARY KEY,
        event_id        UUID NOT NULL REFERENCES fire_event (event_id) ON DELETE CASCADE,
        method          TEXT NOT NULL CHECK (method IN ('dnbr', 'model')),
        model_version   TEXT NOT NULL,
        pre_item_ids    TEXT[] NOT NULL DEFAULT '{}',
        post_item_ids   TEXT[] NOT NULL DEFAULT '{}',
        pre_datetime    TIMESTAMPTZ,
        post_datetime   TIMESTAMPTZ NOT NULL,
        threshold       DOUBLE PRECISION,
        mean_score      DOUBLE PRECISION,
        clear_fraction  DOUBLE PRECISION NOT NULL CHECK (clear_fraction BETWEEN 0 AND 1),
        area_ha         DOUBLE PRECISION NOT NULL CHECK (area_ha >= 0),
        n_polygons      INTEGER NOT NULL CHECK (n_polygons >= 0),
        geom            geometry(MultiPolygon, 4326) NOT NULL,
        params          JSONB NOT NULL DEFAULT '{}',
        qa_status       TEXT NOT NULL DEFAULT 'auto'
                        CHECK (qa_status IN ('auto', 'accepted', 'rejected')),
        qa_by           UUID REFERENCES app_user (user_id) ON DELETE SET NULL,
        qa_at           TIMESTAMPTZ,
        qa_note         TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (event_id, method)
    )
    """,
    "CREATE INDEX burned_area_geom_gix ON burned_area USING GIST (geom)",
    """
    CREATE TABLE raster_asset (
        asset_id    UUID PRIMARY KEY,
        event_id    UUID NOT NULL REFERENCES fire_event (event_id) ON DELETE CASCADE,
        kind        TEXT NOT NULL CHECK (kind IN ('pre', 'post', 'dnbr', 'mask', 'probability')),
        method      TEXT,
        uri         TEXT NOT NULL,
        epsg        INTEGER NOT NULL,
        width       INTEGER NOT NULL,
        height      INTEGER NOT NULL,
        bands       TEXT[] NOT NULL DEFAULT '{}',
        -- Ground extent of the raster in WGS84: lets map clients request only the tiles
        -- that exist instead of guessing the extent from the event outline.
        footprint   geometry(Polygon, 4326) NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE NULLS NOT DISTINCT (event_id, kind, method)
    )
    """,
    # ------------------------------------------------------------------ validation
    """
    CREATE TABLE reference_perimeter (
        ref_id        TEXT PRIMARY KEY,
        source        TEXT NOT NULL,
        fire_date     DATE,
        country_iso3  CHAR(3),
        area_ha       DOUBLE PRECISION,
        attrs         JSONB NOT NULL DEFAULT '{}',
        geom          geometry(MultiPolygon, 4326) NOT NULL,
        loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX reference_perimeter_geom_gix ON reference_perimeter USING GIST (geom)",
    "CREATE INDEX reference_perimeter_date_idx ON reference_perimeter (fire_date)",
    """
    CREATE TABLE burned_area_validation (
        ba_id             UUID NOT NULL REFERENCES burned_area (ba_id) ON DELETE CASCADE,
        ref_source        TEXT NOT NULL,
        n_ref_perimeters  INTEGER NOT NULL,
        mapped_ha         DOUBLE PRECISION NOT NULL,
        ref_ha            DOUBLE PRECISION NOT NULL,
        intersection_ha   DOUBLE PRECISION NOT NULL,
        union_ha          DOUBLE PRECISION NOT NULL,
        iou               DOUBLE PRECISION,
        precision_ref     DOUBLE PRECISION,
        recall_ref        DOUBLE PRECISION,
        computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (ba_id, ref_source)
    )
    """,
    # ------------------------------------------------------------------ data quality
    """
    CREATE TABLE dq_result (
        dq_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id      TEXT NOT NULL,
        suite       TEXT NOT NULL,
        check_name  TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('pass', 'warn', 'fail')),
        severity    TEXT NOT NULL CHECK (severity IN ('warn', 'error')),
        observed    JSONB NOT NULL DEFAULT '{}',
        detail      TEXT,
        checked_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX dq_result_suite_idx ON dq_result (suite, checked_at DESC)",
    # ------------------------------------------------------------------ marts (filled from dbt)
    "CREATE SCHEMA IF NOT EXISTS mart",
    """
    CREATE TABLE mart.fire_activity_daily (
        activity_date  DATE NOT NULL,
        country_iso3   TEXT NOT NULL,
        admin1_name    TEXT NOT NULL,
        n_hotspots     BIGINT NOT NULL,
        n_events       BIGINT NOT NULL,
        frp_sum_mw     DOUBLE PRECISION,
        PRIMARY KEY (activity_date, country_iso3, admin1_name)
    )
    """,
    """
    CREATE TABLE mart.burned_area_admin_year (
        fire_year     INTEGER NOT NULL,
        country_iso3  TEXT NOT NULL,
        admin1_name   TEXT NOT NULL,
        method        TEXT NOT NULL,
        n_events      BIGINT NOT NULL,
        area_ha       DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (fire_year, country_iso3, admin1_name, method)
    )
    """,
    """
    CREATE TABLE mart.event_summary (
        event_id         UUID PRIMARY KEY,
        country_iso3     TEXT NOT NULL,
        admin1_name      TEXT NOT NULL,
        first_seen       TIMESTAMPTZ NOT NULL,
        last_seen        TIMESTAMPTZ NOT NULL,
        duration_hours   DOUBLE PRECISION NOT NULL,
        n_hotspots       BIGINT NOT NULL,
        frp_max_mw       DOUBLE PRECISION,
        area_dnbr_ha     DOUBLE PRECISION,
        area_model_ha    DOUBLE PRECISION,
        method_iou       DOUBLE PRECISION
    )
    """,
]

DOWNGRADE = [
    "DROP SCHEMA IF EXISTS mart CASCADE",
    "DROP TABLE IF EXISTS dq_result",
    "DROP TABLE IF EXISTS burned_area_validation",
    "DROP TABLE IF EXISTS reference_perimeter",
    "DROP TABLE IF EXISTS raster_asset",
    "DROP TABLE IF EXISTS burned_area",
    "DROP TABLE IF EXISTS hotspot",
    "DROP TABLE IF EXISTS fire_event",
    "DROP TABLE IF EXISTS static_cell",
    "DROP TABLE IF EXISTS app_user",
    "DROP TABLE IF EXISTS admin_unit",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)

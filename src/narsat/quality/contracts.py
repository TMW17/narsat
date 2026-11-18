"""Column-level contracts for the silver and gold tables (pandera schemas).

A contract answers "is this frame fit to be published under this table name?".
Cross-table rules (completeness against bronze, referential consistency) live in
``narsat.quality.gates`` because they need more than one frame.
"""

from __future__ import annotations

import pandera.pandas as pa

from narsat.config import BoundingBox

CONFIDENCE_LEVELS = ["low", "nominal", "high"]
EVENT_STATUSES = ["active", "cooling", "closed", "merged", "dissolved"]
MAPPING_STATUSES = ["not_ready", "pending", "imagery_pending", "mapped", "failed", "skipped"]
METHODS = ["dnbr", "model"]
QA_STATUSES = ["auto", "accepted", "rejected"]

_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def silver_hotspots(bbox: BoundingBox) -> pa.DataFrameSchema:
    return pa.DataFrameSchema(
        {
            "hotspot_id": pa.Column(str, pa.Check.str_matches(_UUID_PATTERN), unique=True),
            "instrument": pa.Column(str, pa.Check.isin(["VIIRS", "MODIS"])),
            "acq_ts": pa.Column("datetime64[us, UTC]"),
            "latitude": pa.Column(float, pa.Check.in_range(bbox.south, bbox.north)),
            "longitude": pa.Column(float, pa.Check.in_range(bbox.west, bbox.east)),
            "frp_mw": pa.Column(float, pa.Check.ge(0), nullable=True),
            "brightness_k": pa.Column(float, pa.Check.in_range(200, 2000), nullable=True),
            "confidence_level": pa.Column(str, pa.Check.isin(CONFIDENCE_LEVELS)),
            "is_static_source": pa.Column(bool),
            "acq_month": pa.Column(str, pa.Check.str_matches(r"^\d{4}-\d{2}$")),
        },
        checks=[
            pa.Check(
                lambda df: df["acq_month"] == df["acq_ts"].dt.strftime("%Y-%m"),
                name="acq_month_matches_acq_ts",
            ),
            pa.Check(
                lambda df: df["first_ingested_at"] >= df["acq_ts"],
                name="ingested_after_acquisition",
            ),
        ],
        strict=False,
        coerce=False,
    )


def silver_events() -> pa.DataFrameSchema:
    return pa.DataFrameSchema(
        {
            "event_id": pa.Column(str, pa.Check.str_matches(_UUID_PATTERN), unique=True),
            "status": pa.Column(str, pa.Check.isin(EVENT_STATUSES)),
            "mapping_status": pa.Column(str, pa.Check.isin(MAPPING_STATUSES)),
            "n_hotspots": pa.Column("int32", pa.Check.ge(0)),
            "centroid_lon": pa.Column(float, pa.Check.in_range(-180, 180)),
            "centroid_lat": pa.Column(float, pa.Check.in_range(-90, 90)),
            "hull_area_ha": pa.Column(float, pa.Check.gt(0)),
        },
        checks=[
            pa.Check(lambda df: df["first_seen"] <= df["last_seen"], name="first_before_last"),
            pa.Check(
                lambda df: (df["n_hotspots"] > 0) | df["status"].isin(["merged", "dissolved"]),
                name="live_events_have_hotspots",
            ),
        ],
        strict=False,
        coerce=False,
    )


def gold_burned_areas(min_clear_fraction: float) -> pa.DataFrameSchema:
    return pa.DataFrameSchema(
        {
            "ba_id": pa.Column(str, pa.Check.str_matches(_UUID_PATTERN), unique=True),
            "event_id": pa.Column(str, pa.Check.str_matches(_UUID_PATTERN)),
            "method": pa.Column(str, pa.Check.isin(METHODS)),
            "area_ha": pa.Column(float, pa.Check.ge(0)),
            "n_polygons": pa.Column("int32", pa.Check.ge(0)),
            "clear_fraction": pa.Column(float, pa.Check.in_range(min_clear_fraction, 1.0)),
            "qa_status": pa.Column(str, pa.Check.isin(QA_STATUSES)),
        },
        checks=[
            pa.Check(
                lambda df: (df["n_polygons"] > 0) == (df["area_ha"] > 0),
                name="area_consistent_with_polygons",
            ),
        ],
        unique=["event_id", "method"],
        strict=False,
        coerce=False,
    )

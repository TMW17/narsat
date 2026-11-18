"""Build the SYNTHETIC fixture used by the browser smoke test (``ui_smoke.cjs``).

Nothing here is real data: the fires are invented and the "Sentinel-2 scenes" are the
synthetic rasters of ``tests/synthetic.py`` (uniform vegetation with a burned disc).
The fixture exists to exercise the whole chain in front of a real browser: PostGIS,
the mapping pipeline, the inference service, the lakehouse, dbt, the API, TiTiler and
the web application. Never use it for screenshots or figures that describe NarSat's
results.

It rebuilds the schema of the database named by ``NARSAT_TEST_DB_DSN`` from scratch,
and refuses to run unless that database name contains "test".

    python -m tests.e2e.seed            # see tests/e2e/README.md for the full procedure
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from pydantic import SecretStr

from narsat.api import auth
from narsat.config import DbSettings, ImagerySettings, LakeSettings, SegmentationSettings, Settings
from narsat.db import connect, migrate
from narsat.lake.bronze import BronzeHotspotWriter, BronzeRecord
from narsat.lake.catalog import ensure_tables, load_catalog
from narsat.lake.storage import LakeStorage, s3_filesystem
from narsat.pipeline import marts
from narsat.pipeline.lake_refresh import refresh
from narsat.pipeline.map_event import EventMapper
from narsat.pipeline.validate import compute_agreement
from narsat.segmentation.client import InferenceClient
from tests import synthetic
from tests.conftest import make_hotspot
from tests.integration.helpers import fire_over_synthetic_burn, square, store_and_cluster

UTC = dt.UTC
REPO = Path(__file__).resolve().parents[2]
ANALYST = ("analyst@example.org", "synthetic-fixture-password")


def main() -> None:
    dsn = os.environ.get("NARSAT_TEST_DB_DSN", "")
    if "test" not in dsn.rsplit("/", 1)[-1]:
        sys.exit("NARSAT_TEST_DB_DSN must name a database whose name contains 'test'")
    inference_url = os.environ.get("NARSAT_E2E_INFERENCE_URL")
    root = Path(os.environ.get("NARSAT_E2E_DIR", "/tmp/narsat-e2e"))
    shutil.rmtree(root, ignore_errors=True)
    (root / "scenes").mkdir(parents=True)

    db = DbSettings(dsn=SecretStr(dsn))
    migrate.downgrade(db, "base")
    migrate.upgrade(db)
    s3_endpoint = os.environ.get("NARSAT_E2E_S3_ENDPOINT")
    if s3_endpoint:
        # The layout of the Compose stack: bucket "narsat", warehouse/ and assets/.
        lake = LakeSettings(
            catalog_uri=SecretStr(f"sqlite:///{root}/lake/catalog.db"),
            warehouse_uri="s3://narsat/warehouse",
            assets_uri="s3://narsat/assets",
            s3_endpoint=s3_endpoint,
            s3_access_key=SecretStr(os.environ["NARSAT_E2E_S3_ACCESS_KEY"]),
            s3_secret_key=SecretStr(os.environ["NARSAT_E2E_S3_SECRET_KEY"]),
        )
        LakeStorage.assets(lake).ensure_ready()
        filesystem = s3_filesystem(lake)
        for prefix in ("narsat/warehouse", "narsat/assets"):
            with contextlib.suppress(OSError):
                filesystem.delete_dir(prefix)
    else:
        lake = LakeSettings(
            catalog_uri=SecretStr(f"sqlite:///{root}/lake/catalog.db"),
            warehouse_uri=f"file://{root}/lake/warehouse",
            assets_uri=f"file://{root}/lake/assets",
        )
    catalog = load_catalog(lake)
    ensure_tables(catalog)

    kabylie = fire_over_synthetic_burn()
    plateau = [
        make_hotspot(35.10 + 0.003 * i, 1.20 + 0.002 * i,
                     dt.datetime(2023, 8, 5, 11, 0, tzinfo=UTC) + dt.timedelta(hours=5 * i),
                     confidence="n" if i % 3 else "h", frp=9.0 + 2 * i)
        for i in range(6)
    ]  # fmt: skip
    coast = [
        make_hotspot(36.90 + 0.002 * i, 8.75, dt.datetime(2023, 8, 12, 13, 10 * i, tzinfo=UTC),
                     confidence="l", frp=4.0)
        for i in range(3)
    ]  # fmt: skip
    everything = [*kabylie, *plateau, *coast]
    BronzeHotspotWriter(catalog).append(
        [BronzeRecord(h, "firms.hotspots.raw", 0, i) for i, h in enumerate(everything)],
        now=dt.datetime(2023, 8, 12, 18, 0, tzinfo=UTC),
    )

    items = [
        synthetic.write_scene(root / "scenes", "SYNTHETIC_PRE", dt.datetime(2023, 7, 10, 10, 30, tzinfo=UTC),
                              burned=False, noise_seed=1),
        synthetic.write_scene(root / "scenes", "SYNTHETIC_POST", dt.datetime(2023, 7, 28, 10, 30, tzinfo=UTC),
                              burned=True, noise_seed=2),
    ]  # fmt: skip
    storage = LakeStorage.assets(lake)
    storage.ensure_ready()
    methods = ["dnbr", "model"] if inference_url else ["dnbr"]
    mapper = EventMapper(
        ImagerySettings(),
        SegmentationSettings(methods=methods, inference_url=inference_url),
        storage,
        synthetic.fake_search(items),
        inference=InferenceClient(inference_url) if inference_url else None,
        clock=lambda: dt.datetime(2023, 10, 1, tzinfo=UTC),
    )

    with connect(db) as conn:
        conn.execute(
            "INSERT INTO admin_unit (country_iso3, level, code, name, source, licence, geom) VALUES "
            "('DZA', 0, 'DZA', 'Algeria', 'synthetic fixture', 'none', ST_GeomFromText(%(a)s, 4326)), "
            "('DZA', 1, 'DZ-06', 'Béjaïa', 'synthetic fixture', 'none', ST_GeomFromText(%(b)s, 4326)), "
            "('DZA', 1, 'DZ-14', 'Tiaret', 'synthetic fixture', 'none', ST_GeomFromText(%(t)s, 4326)), "
            "('TUN', 0, 'TUN', 'Tunisia', 'synthetic fixture', 'none', ST_GeomFromText(%(n)s, 4326))",
            {"a": square(3.0, 34.0, 5.0), "b": square(4.6, 36.7, 0.5), "t": square(1.2, 35.1, 0.5),
             "n": square(9.5, 35.0, 1.5)},
        )  # fmt: skip
        store_and_cluster(conn, everything)
        event_id = conn.execute(
            "SELECT event_id FROM hotspot WHERE hotspot_id = %(id)s", {"id": kabylie[0].hotspot_id}
        ).fetchone()["event_id"]
        outcome = mapper.map_event(conn, event_id)
        compute_agreement(conn)
        report = refresh(catalog, conn, Settings(db=db, lake=lake))
        conn.execute(
            "INSERT INTO app_user (email, password_hash, role) VALUES (%(e)s, %(h)s, 'analyst')",
            {"e": ANALYST[0], "h": auth.hash_password(ANALYST[1])},
        )

    dbt = subprocess.run(
        [
            str(Path(sys.executable).parent / "dbt"), "build",
            "--project-dir", str(REPO / "transform"), "--profiles-dir", str(REPO / "transform"),
            "--target-path", str(root / "dbt" / "target"), "--log-path", str(root / "dbt" / "logs"),
            "--no-use-colors",
        ],
        cwd=root, capture_output=True, text=True, check=False,
        env={
            **os.environ,
            "NARSAT_LAKE_CATALOG_URI": lake.catalog_uri.get_secret_value(),
            "NARSAT_LAKE_WAREHOUSE_URI": lake.warehouse_uri,
            "NARSAT_LAKE_S3_ENDPOINT": lake.s3_endpoint or "",
            "NARSAT_LAKE_S3_ACCESS_KEY": (
                lake.s3_access_key.get_secret_value() if lake.s3_access_key else ""
            ),
            "NARSAT_LAKE_S3_SECRET_KEY": (
                lake.s3_secret_key.get_secret_value() if lake.s3_secret_key else ""
            ),
            "NARSAT_MARTS_DIR": str(root / "marts"),
        },
    )  # fmt: skip
    if dbt.returncode != 0:
        sys.exit(dbt.stdout[-4000:] + dbt.stderr[-2000:])
    with connect(db) as conn:
        published = marts.publish_all(conn, root / "marts")

    summary = {
        "event_id": str(event_id),
        "mapping": outcome.status,
        "methods": methods,
        "lake_rows": report.rows,
        "failed_checks": [check.check_name for check in report.failed],
        "dbt": dbt.stdout.strip().splitlines()[-1],
        "marts": published,
        "analyst": {"email": ANALYST[0], "password": ANALYST[1]},
    }
    (root / "fixture.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

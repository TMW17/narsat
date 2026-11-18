"""Refresh the silver and gold layers of the lakehouse, behind quality gates.

Order matters: each layer is validated *before* it is written, so a failing gate
leaves the previously published tables untouched.

    bronze + PostGIS enrichment -> [gate] -> silver.hotspots
    PostGIS events              -> [gate] -> silver.fire_events
    PostGIS products            -> [gate] -> gold.burned_areas, gold.burned_area_validation
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from pyiceberg.catalog import Catalog

from narsat.config import Settings
from narsat.db import Connection
from narsat.lake import gold, silver
from narsat.lake.catalog import SILVER_HOTSPOTS
from narsat.log import get_logger
from narsat.quality import contracts, gates

log = get_logger(__name__)


@dataclass
class RefreshReport:
    run_id: str
    months: list[str]
    rows: dict[str, int] = field(default_factory=dict)
    checks: list[gates.CheckResult] = field(default_factory=list)

    @property
    def failed(self) -> list[gates.CheckResult]:
        return [check for check in self.checks if check.blocking]


def refresh(
    catalog: Catalog,
    conn: Connection,
    settings: Settings,
    months: list[str] | None = None,
) -> RefreshReport:
    """Rebuild silver for ``months`` (default: every month present in bronze) and gold.

    Raises ``GateFailed`` when a blocking check fails. Check results are recorded in
    ``dq_result`` in every case; the caller commits the connection.
    """
    target_months = months if months is not None else silver.available_months(catalog)
    report = RefreshReport(run_id=uuid.uuid4().hex, months=target_months)

    def gate(results: list[gates.CheckResult]) -> None:
        report.checks.extend(results)
        gates.record(conn, results, report.run_id)
        # Persist the verdict even though the exception below aborts the run.
        conn.commit()
        gates.enforce(results)

    # --- silver.hotspots --------------------------------------------------------
    if target_months:
        bronze = silver.read_bronze_months(catalog, target_months)
        frame = silver.build_silver_hotspots_frame(
            bronze, silver.read_enrichment(conn, target_months)
        )
        gate(
            gates.silver_hotspot_checks(
                frame,
                set(bronze["hotspot_id"]) if not bronze.empty else set(),
                contracts.silver_hotspots(settings.aoi.bbox),
            )
        )
        report.rows[SILVER_HOTSPOTS.identifier] = silver.write_silver_hotspots(catalog, frame)

    # --- silver.fire_events -----------------------------------------------------
    events = silver.read_events_frame(conn)
    all_hotspots = (
        catalog.load_table(SILVER_HOTSPOTS.identifier)
        .scan(selected_fields=("hotspot_id", "event_id"))
        .to_pandas()
    )
    gate(gates.silver_event_checks(events, all_hotspots, contracts.silver_events()))
    report.rows["silver.fire_events"] = silver.write_silver_events(catalog, events)

    # --- gold -------------------------------------------------------------------
    burned = gold.read_burned_areas_frame(conn)
    gate(
        gates.gold_burned_area_checks(
            burned, events, contracts.gold_burned_areas(min_clear_fraction=0.0)
        )
    )
    report.rows.update(gold.write_gold(catalog, burned, gold.read_validation_frame(conn)))

    log.info(
        "lake_refreshed",
        run_id=report.run_id,
        months=target_months,
        **{key.replace(".", "_"): value for key, value in report.rows.items()},
    )
    return report

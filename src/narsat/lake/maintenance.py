"""Housekeeping for the Iceberg tables.

Streaming appends create one small Parquet file and one snapshot per micro-batch.
Two jobs keep that under control:

* ``expire_snapshots`` drops snapshots older than the retention period (the current
  snapshot is always kept, so no data is lost; only time travel beyond the retention
  window goes away);
* ``compact_bronze_month`` rewrites one closed month of bronze into a single file.
  The rewrite is an Iceberg overwrite limited to that month, and it carries the Kafka
  offsets forward so the exactly-once bookkeeping of the stream sink survives it.
"""

from __future__ import annotations

import datetime as dt

from pyiceberg.catalog import Catalog
from pyiceberg.exceptions import CommitFailedException
from pyiceberg.expressions import EqualTo
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random

from narsat.lake.bronze import read_offset_state
from narsat.lake.catalog import ALL_TABLES, BRONZE_HOTSPOTS
from narsat.log import get_logger

log = get_logger(__name__)


def expire_snapshots(catalog: Catalog, retention_days: int, now: dt.datetime | None = None) -> None:
    cutoff = (now or dt.datetime.now(dt.UTC)) - dt.timedelta(days=retention_days)
    for definition in ALL_TABLES:
        table = catalog.load_table(definition.identifier)
        before = len(table.snapshots())
        if before <= 1:
            continue
        table.maintenance.expire_snapshots().older_than(cutoff).commit()
        after = len(catalog.load_table(definition.identifier).snapshots())
        log.info("snapshots_expired", table=definition.identifier, before=before, after=after)


def bronze_file_counts(catalog: Catalog) -> dict[str, int]:
    """Number of data files per month partition of bronze."""
    table = catalog.load_table(BRONZE_HOTSPOTS.identifier)
    return {
        p["partition"]["acq_month"]: int(p["file_count"])
        for p in table.inspect.partitions().to_pylist()
    }


@retry(
    retry=retry_if_exception_type(CommitFailedException),
    wait=wait_random(0.2, 1.5),
    stop=stop_after_attempt(6),
    reraise=True,
)
def compact_bronze_month(catalog: Catalog, month: str) -> int:
    """Rewrite one month of bronze as a single file. Returns the number of rows."""
    table = catalog.load_table(BRONZE_HOTSPOTS.identifier)
    only_month = EqualTo("acq_month", month)
    data = table.scan(row_filter=only_month).to_arrow()
    if data.num_rows == 0:
        return 0
    # The rewrite becomes the newest snapshot: it must carry the stream's offsets and
    # topic identifiers forward, or the next consumer start would replay old messages.
    properties = read_offset_state(table).snapshot_properties()
    # Fails with CommitFailedException if the stream sink committed since the scan;
    # the retry then starts again from the new table state, so no row is lost.
    table.overwrite(
        data.cast(table.schema().as_arrow()),
        overwrite_filter=only_month,
        snapshot_properties=properties,
    )
    log.info("bronze_compacted", month=month, rows=data.num_rows)
    return int(data.num_rows)


def compact_bronze(catalog: Catalog, min_files: int = 8, current_month: str | None = None) -> None:
    """Compact every past month that has accumulated at least ``min_files`` files."""
    current = current_month or dt.datetime.now(dt.UTC).strftime("%Y-%m")
    for month, files in sorted(bronze_file_counts(catalog).items()):
        if month < current and files >= min_files:
            compact_bronze_month(catalog, month)

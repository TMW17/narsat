from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from narsat.config import BoundingBox, LakeSettings
from narsat.lake import maintenance, silver
from narsat.lake.bronze import (
    OFFSETS_PROPERTY,
    BronzeHotspotWriter,
    BronzeRecord,
    decode_offsets,
    encode_offsets,
    read_committed_offsets,
    read_offset_state,
)
from narsat.lake.catalog import ALL_TABLES, BRONZE_HOTSPOTS, ensure_tables, load_catalog
from narsat.lake.storage import LakeStorage, resolve_uri
from narsat.quality import contracts, gates
from tests.conftest import make_hotspot

UTC = dt.UTC
T0 = dt.datetime(2023, 7, 23, 1, 30, tzinfo=UTC)
TOPIC = "firms.hotspots.raw"
BOX = BoundingBox(west=-13.5, south=27.0, east=11.8, north=37.6)


def _records(start: int, count: int, partition: int = 0, day_offset: int = 0) -> list[BronzeRecord]:
    return [
        BronzeRecord(
            make_hotspot(36.70 + 0.001 * i, 4.60, T0 + dt.timedelta(days=day_offset, minutes=i)),
            TOPIC,
            partition,
            i,
        )
        for i in range(start, start + count)
    ]


@pytest.fixture
def catalog(lake_settings: LakeSettings):  # type: ignore[no-untyped-def]
    catalog = load_catalog(lake_settings)
    ensure_tables(catalog)
    return catalog


class TestCatalog:
    def test_tables_are_created_once(self, catalog) -> None:  # type: ignore[no-untyped-def]
        again = ensure_tables(catalog)
        assert set(again) == {definition.identifier for definition in ALL_TABLES}
        bronze = catalog.load_table(BRONZE_HOTSPOTS.identifier)
        assert [f.name for f in bronze.spec().fields] == ["acq_month"]

    def test_relative_uris_are_anchored(self, tmp_path: Path) -> None:
        assert resolve_uri("file://var/lake") == f"file://{tmp_path}/var/lake"
        assert resolve_uri("s3://bucket/prefix/") == "s3://bucket/prefix"


class TestBronzeExactlyOnce:
    def test_offsets_round_trip(self) -> None:
        offsets = {(TOPIC, 0): 41, (TOPIC, 2): 7, ("other", 1): 3}
        assert decode_offsets(encode_offsets(offsets)) == offsets

    def test_append_stores_rows_and_offsets_atomically(self, catalog) -> None:  # type: ignore[no-untyped-def]
        writer = BronzeHotspotWriter(catalog)
        assert writer.append(_records(0, 5)) == 5
        table = catalog.load_table(BRONZE_HOTSPOTS.identifier)
        assert table.scan().count() == 5
        assert read_committed_offsets(table) == {(TOPIC, 0): 4}
        assert OFFSETS_PROPERTY in table.current_snapshot().summary.additional_properties

    def test_replayed_batch_is_not_written_twice(self, catalog) -> None:  # type: ignore[no-untyped-def]
        writer = BronzeHotspotWriter(catalog)
        writer.append(_records(0, 5))
        # The consumer crashed before committing to Kafka and restarts from offset 0:
        # a brand-new writer learns the high-water mark from the table itself.
        restarted = BronzeHotspotWriter(catalog)
        assert restarted.committed_offsets == {(TOPIC, 0): 4}
        assert restarted.append(_records(0, 5)) == 0
        assert restarted.append(_records(3, 5)) == 3  # offsets 5, 6, 7 are new
        table = catalog.load_table(BRONZE_HOTSPOTS.identifier)
        offsets = sorted(table.scan().to_arrow().column("kafka_offset").to_pylist())
        assert offsets == list(range(8))

    def test_partitions_are_tracked_independently(self, catalog) -> None:  # type: ignore[no-untyped-def]
        writer = BronzeHotspotWriter(catalog)
        writer.append(_records(0, 3, partition=0) + _records(0, 2, partition=1))
        assert writer.committed_offsets == {(TOPIC, 0): 2, (TOPIC, 1): 1}
        assert writer.append(_records(0, 3, partition=1)) == 1

    def test_stale_writer_sees_what_another_writer_committed(self, catalog) -> None:  # type: ignore[no-untyped-def]
        first = BronzeHotspotWriter(catalog)
        second = BronzeHotspotWriter(catalog)  # e.g. took the partition over in a rebalance
        second.append(_records(0, 6))
        # ``first`` still believes nothing was written, but must not duplicate the rows.
        assert first.append(_records(0, 6)) == 0
        assert catalog.load_table(BRONZE_HOTSPOTS.identifier).scan().count() == 6

    def test_a_recreated_topic_does_not_lose_its_new_messages(self, catalog) -> None:  # type: ignore[no-untyped-def]
        """Kafka's data was wiped: the topic exists again with offsets starting at 0."""
        assert BronzeHotspotWriter(catalog, {TOPIC: "id-A"}).append(_records(0, 5)) == 5
        # Same incarnation after a restart: replays are still skipped.
        assert BronzeHotspotWriter(catalog, {TOPIC: "id-A"}).append(_records(0, 5)) == 0

        recreated = BronzeHotspotWriter(catalog, {TOPIC: "id-B"})
        new_messages = _records(0, 3, day_offset=2)  # offsets 0-2 again, other detections
        assert recreated.append(new_messages) == 3
        table = catalog.load_table(BRONZE_HOTSPOTS.identifier)
        assert table.scan().count() == 8
        state = read_offset_state(table)
        assert state.offsets == {(TOPIC, 0): 2}
        assert state.topic_ids == {TOPIC: "id-B"}
        # ...and exactly-once holds again for the new incarnation.
        assert BronzeHotspotWriter(catalog, {TOPIC: "id-B"}).append(new_messages) == 0

    def test_without_topic_ids_the_recorded_offsets_are_trusted(self, catalog) -> None:  # type: ignore[no-untyped-def]
        """A broker that cannot report identifiers must not cause duplicates."""
        BronzeHotspotWriter(catalog, {TOPIC: "id-A"}).append(_records(0, 5))
        assert BronzeHotspotWriter(catalog).append(_records(0, 5)) == 0
        assert BronzeHotspotWriter(catalog, {}).append(_records(3, 4)) == 2
        table = catalog.load_table(BRONZE_HOTSPOTS.identifier)
        assert read_offset_state(table).topic_ids == {TOPIC: "id-A"}

    def test_rows_keep_their_values(self, catalog) -> None:  # type: ignore[no-untyped-def]
        BronzeHotspotWriter(catalog).append(_records(0, 1), now=T0 + dt.timedelta(hours=4))
        row = catalog.load_table(BRONZE_HOTSPOTS.identifier).scan().to_arrow().to_pylist()[0]
        assert row["acq_month"] == "2023-07"
        assert row["kafka_topic"] == TOPIC
        assert row["instrument"] == "VIIRS"
        assert row["acq_ts"] == T0
        assert row["ingested_at"] == T0 + dt.timedelta(hours=4)


class TestMaintenance:
    def test_compaction_merges_files_and_keeps_the_offsets(self, catalog) -> None:  # type: ignore[no-untyped-def]
        writer = BronzeHotspotWriter(catalog, {TOPIC: "id-A"})
        for start in range(0, 12, 3):
            writer.append(_records(start, 3))
        assert maintenance.bronze_file_counts(catalog) == {"2023-07": 4}

        assert maintenance.compact_bronze_month(catalog, "2023-07") == 12
        assert maintenance.bronze_file_counts(catalog) == {"2023-07": 1}
        table = catalog.load_table(BRONZE_HOTSPOTS.identifier)
        assert table.scan().count() == 12
        assert read_committed_offsets(table) == {(TOPIC, 0): 11}
        assert read_offset_state(table).topic_ids == {TOPIC: "id-A"}
        # The stream sink continues exactly where it was.
        assert BronzeHotspotWriter(catalog, {TOPIC: "id-A"}).append(_records(10, 4)) == 2

    def test_compact_bronze_only_touches_past_months_with_enough_files(self, catalog) -> None:  # type: ignore[no-untyped-def]
        writer = BronzeHotspotWriter(catalog)
        for start in range(0, 6, 2):
            writer.append(_records(start, 2))
        maintenance.compact_bronze(catalog, min_files=5, current_month="2023-08")
        assert maintenance.bronze_file_counts(catalog) == {"2023-07": 3}
        maintenance.compact_bronze(catalog, min_files=3, current_month="2023-07")
        assert maintenance.bronze_file_counts(catalog) == {"2023-07": 3}  # current month is live
        maintenance.compact_bronze(catalog, min_files=3, current_month="2023-08")
        assert maintenance.bronze_file_counts(catalog) == {"2023-07": 1}

    def test_expire_snapshots_keeps_the_data(self, catalog) -> None:  # type: ignore[no-untyped-def]
        writer = BronzeHotspotWriter(catalog)
        for start in range(0, 6, 2):
            writer.append(_records(start, 2))
        maintenance.expire_snapshots(
            catalog, retention_days=0, now=dt.datetime.now(UTC) + dt.timedelta(minutes=5)
        )
        table = catalog.load_table(BRONZE_HOTSPOTS.identifier)
        assert len(table.snapshots()) == 1
        assert table.scan().count() == 6
        assert read_committed_offsets(table) == {(TOPIC, 0): 5}


def _bronze_frame(catalog, records: list[BronzeRecord]) -> pd.DataFrame:  # type: ignore[no-untyped-def]
    BronzeHotspotWriter(catalog).append(records, now=T0 + dt.timedelta(hours=6))
    return silver.read_bronze_months(catalog, ["2023-07"])


def _enrichment(bronze: pd.DataFrame, event: str | None = "0e7c6a2e-58a0-5d0c-9a7b-1f6f4f4f9a11"):
    return pd.DataFrame({
        "hotspot_id": bronze["hotspot_id"].drop_duplicates().tolist(),
        "is_static_source": False,
        "country_iso3": "DZA",
        "admin1_name": "Béjaïa",
        "event_id": event,
    })  # fmt: skip


class TestSilver:
    def test_months_between(self) -> None:
        assert silver.months_between(dt.date(2023, 11, 20), dt.date(2024, 2, 1)) == [
            "2023-11", "2023-12", "2024-01", "2024-02",
        ]  # fmt: skip
        with pytest.raises(ValueError, match="before start"):
            silver.months_between(dt.date(2024, 2, 1), dt.date(2024, 1, 1))

    def test_duplicates_collapse_to_the_first_arrival(self, catalog) -> None:  # type: ignore[no-untyped-def]
        records = _records(0, 4)
        duplicate = BronzeRecord(records[1].message, TOPIC, 0, 99)  # republished later
        bronze = _bronze_frame(catalog, [*records, duplicate])
        assert len(bronze) == 5
        frame = silver.build_silver_hotspots_frame(bronze, _enrichment(bronze))
        assert len(frame) == 4
        assert frame["hotspot_id"].is_unique
        assert list(frame.columns) == silver.SILVER_HOTSPOTS.schema.column_names
        assert frame["admin1_name"].unique().tolist() == ["Béjaïa"]

    def test_missing_enrichment_becomes_null_not_an_error(self, catalog) -> None:  # type: ignore[no-untyped-def]
        bronze = _bronze_frame(catalog, _records(0, 3))
        enrichment = _enrichment(bronze).iloc[:1]
        frame = silver.build_silver_hotspots_frame(bronze, enrichment)
        assert frame["event_id"].notna().sum() == 1
        assert frame["is_static_source"].tolist() == [False, False, False]

    def test_write_is_idempotent_per_month(self, catalog) -> None:  # type: ignore[no-untyped-def]
        bronze = _bronze_frame(catalog, _records(0, 4))
        frame = silver.build_silver_hotspots_frame(bronze, _enrichment(bronze))
        silver.write_silver_hotspots(catalog, frame)
        silver.write_silver_hotspots(catalog, frame)
        table = catalog.load_table(silver.SILVER_HOTSPOTS.identifier)
        assert table.scan().count() == 4
        assert silver.available_months(catalog) == ["2023-07"]

    def test_empty_bronze_gives_an_empty_silver_frame(self) -> None:
        empty = pd.DataFrame(columns=BRONZE_HOTSPOTS.schema.column_names)
        frame = silver.build_silver_hotspots_frame(empty, pd.DataFrame())
        assert frame.empty
        assert list(frame.columns) == silver.SILVER_HOTSPOTS.schema.column_names


class TestQualityGates:
    def test_valid_silver_passes(self, catalog) -> None:  # type: ignore[no-untyped-def]
        bronze = _bronze_frame(catalog, _records(0, 4))
        frame = silver.build_silver_hotspots_frame(bronze, _enrichment(bronze))
        results = gates.silver_hotspot_checks(
            frame, set(bronze["hotspot_id"]), contracts.silver_hotspots(BOX)
        )
        assert [r.status for r in results] == ["pass"] * 4
        gates.enforce(results)  # does not raise

    def test_out_of_area_and_bad_values_fail_the_schema(self, catalog) -> None:  # type: ignore[no-untyped-def]
        bronze = _bronze_frame(catalog, _records(0, 4))
        frame = silver.build_silver_hotspots_frame(bronze, _enrichment(bronze))
        frame.loc[0, "latitude"] = 52.5  # Berlin
        frame.loc[1, "confidence_level"] = "certain"
        frame.loc[2, "frp_mw"] = -3.0
        results = gates.schema_checks("silver.hotspots", frame, contracts.silver_hotspots(BOX))
        failed = {r.check_name for r in results if r.status == "fail"}
        assert any("latitude" in name for name in failed)
        assert any("confidence_level" in name for name in failed)
        assert any("frp_mw" in name for name in failed)
        with pytest.raises(gates.GateFailed) as excinfo:
            gates.enforce(results)
        assert len(excinfo.value.failures) == len(failed)

    def test_a_rule_over_several_columns_is_reported_once(self, catalog) -> None:  # type: ignore[no-untyped-def]
        bronze = _bronze_frame(catalog, _records(0, 4))
        frame = silver.build_silver_hotspots_frame(bronze, _enrichment(bronze))
        # Two rows claim to have been ingested before the satellite acquired them.
        frame.loc[[0, 2], "first_ingested_at"] = frame.loc[[0, 2], "acq_ts"] - pd.Timedelta(hours=1)
        results = gates.schema_checks("silver.hotspots", frame, contracts.silver_hotspots(BOX))
        assert [r.check_name for r in results] == ["schema[ingested_after_acquisition]"]
        assert results[0].observed == {"violations": 2, "example_rows": [0, 2]}

    def test_rows_missing_from_silver_block_publication(self, catalog) -> None:  # type: ignore[no-untyped-def]
        bronze = _bronze_frame(catalog, _records(0, 4))
        frame = silver.build_silver_hotspots_frame(bronze, _enrichment(bronze)).iloc[:3]
        results = gates.silver_hotspot_checks(
            frame, set(bronze["hotspot_id"]), contracts.silver_hotspots(BOX)
        )
        completeness = next(r for r in results if r.check_name == "complete_versus_bronze")
        assert completeness.blocking
        assert completeness.observed["missing_in_silver"] == 1

    def test_warnings_do_not_block(self, catalog) -> None:  # type: ignore[no-untyped-def]
        bronze = _bronze_frame(catalog, _records(0, 4))
        frame = silver.build_silver_hotspots_frame(bronze, _enrichment(bronze, event=None))
        results = gates.silver_hotspot_checks(
            frame, set(bronze["hotspot_id"]), contracts.silver_hotspots(BOX)
        )
        unassigned = next(r for r in results if r.check_name == "hotspots_assigned_to_event")
        assert unassigned.status == "warn"
        assert not unassigned.blocking
        gates.enforce(results)

    def test_ratio_check_is_vacuously_true_on_empty_input(self) -> None:
        assert gates.ratio_check("s", "c", 0, 0, 0.99).status == "pass"
        assert gates.ratio_check("s", "c", 1, 2, 0.99).status == "fail"
        assert gates.ratio_check("s", "c", 1, 2, 0.99, "warn").status == "warn"


class TestLocalStorage:
    def test_put_read_list_delete(self, lake_settings: LakeSettings, tmp_path: Path) -> None:
        storage = LakeStorage.assets(lake_settings)
        storage.ensure_ready()
        local = tmp_path / "x.bin"
        local.write_bytes(b"abc")
        uri = storage.put_file(local, "events/e1/post.tif")
        assert uri == f"{resolve_uri(lake_settings.assets_uri)}/events/e1/post.tif"
        storage.put_bytes(b"{}", "events/e1/stac/post/item.json")
        assert storage.exists("events/e1/post.tif")
        assert storage.read_bytes("events/e1/post.tif") == b"abc"
        assert storage.list_keys("events/e1") == [
            "events/e1/post.tif", "events/e1/stac/post/item.json",
        ]  # fmt: skip
        storage.delete_prefix("events/e1")
        assert storage.list_keys("events") == []
        assert not storage.exists("events/e1/post.tif")

    @pytest.mark.parametrize("key", ["/etc/passwd", "../outside", "a/../../b"])
    def test_keys_cannot_escape_the_lake(self, lake_settings: LakeSettings, key: str) -> None:
        with pytest.raises(ValueError, match="invalid lake key"):
            LakeStorage.assets(lake_settings).uri(key) and LakeStorage.assets(lake_settings).exists(
                key
            )

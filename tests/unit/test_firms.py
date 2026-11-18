from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx2 as httpx
import pytest

from narsat.config import BoundingBox
from narsat.ingestion.firms import FirmsClient, FirmsError, FirmsTransientError, parse_area_csv
from narsat.ingestion.poller import FirmsPoller
from narsat.ingestion.state import SeenCache
from tests.conftest import FIXTURES

UTC = dt.UTC
FETCHED = dt.datetime(2023, 7, 24, 6, 0, tzinfo=UTC)
BOX = BoundingBox(west=-13.5, south=27.0, east=11.8, north=37.6)
KEY = "0123456789abcdef0123456789abcdef"


class TestParser:
    def test_viirs_rows(self) -> None:
        text = (FIXTURES / "firms_viirs_nrt.csv").read_text()
        result = parse_area_csv(text, "VIIRS_NOAA20_NRT", FETCHED)
        assert len(result.hotspots) == 5
        first = result.hotspots[0]
        assert first.instrument == "VIIRS"
        assert first.satellite == "N20"
        # acq_time "134" means 01:34 UTC.
        assert first.acq_ts == dt.datetime(2023, 7, 23, 1, 34, tzinfo=UTC)
        assert first.brightness_k == pytest.approx(338.52)
        assert first.brightness_2_k == pytest.approx(295.31)
        assert first.frp_mw == pytest.approx(12.45)
        assert [h.confidence_level for h in result.hotspots[:3]] == ["nominal", "high", "low"]
        # "46" means 00:46 UTC.
        assert result.hotspots[3].acq_ts == dt.datetime(2023, 7, 24, 0, 46, tzinfo=UTC)
        assert first.type_code is None

    def test_bad_rows_are_reported_not_dropped(self) -> None:
        text = (FIXTURES / "firms_viirs_nrt.csv").read_text()
        result = parse_area_csv(text, "VIIRS_NOAA20_NRT", FETCHED)
        assert [r.line_number for r in result.rejected] == [7, 8]
        assert "confidence" in result.rejected[0].reason
        assert result.rejected[1].row["latitude"] == "not-a-number"

    def test_modis_columns_and_confidence_scale(self) -> None:
        text = (FIXTURES / "firms_modis_nrt.csv").read_text()
        result = parse_area_csv(text, "MODIS_NRT", FETCHED)
        assert not result.rejected
        assert [h.instrument for h in result.hotspots] == ["MODIS"] * 3
        assert [h.confidence_level for h in result.hotspots] == ["nominal", "high", "low"]
        assert result.hotspots[0].brightness_k == pytest.approx(325.6)
        assert result.hotspots[0].brightness_2_k == pytest.approx(298.2)
        assert {h.satellite for h in result.hotspots} == {"Terra", "Aqua"}

    def test_standard_processing_type_column(self) -> None:
        text = (FIXTURES / "firms_viirs_sp.csv").read_text()
        result = parse_area_csv(text, "VIIRS_SNPP_SP", FETCHED)
        assert [h.type_code for h in result.hotspots] == [0, 2]

    def test_identifiers_are_reproducible(self) -> None:
        text = (FIXTURES / "firms_viirs_nrt.csv").read_text()
        first = parse_area_csv(text, "VIIRS_NOAA20_NRT", FETCHED)
        second = parse_area_csv(text, "VIIRS_NOAA20_NRT", FETCHED + dt.timedelta(hours=5))
        assert [h.hotspot_id for h in first.hotspots] == [h.hotspot_id for h in second.hotspots]

    def test_empty_and_header_only_bodies(self) -> None:
        assert parse_area_csv("", "VIIRS_SNPP_NRT", FETCHED).hotspots == []
        header = (FIXTURES / "firms_viirs_nrt.csv").read_text().splitlines()[0]
        assert parse_area_csv(header + "\n", "VIIRS_SNPP_NRT", FETCHED).hotspots == []

    def test_missing_required_columns(self) -> None:
        with pytest.raises(FirmsError, match="lacks columns"):
            parse_area_csv("a,b,c\n1,2,3\n", "VIIRS_SNPP_NRT", FETCHED)


def _client(handler: httpx.MockTransport) -> FirmsClient:
    return FirmsClient(KEY, transport=handler)


class TestClient:
    def test_url_layout(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, text="latitude,longitude,acq_date,acq_time,confidence\n")

        client = _client(httpx.MockTransport(handler))
        client.area_csv("VIIRS_SNPP_NRT", BOX, 2)
        client.area_csv("VIIRS_SNPP_SP", BOX, 5, dt.date(2023, 7, 22))
        assert seen == [
            f"/api/area/csv/{KEY}/VIIRS_SNPP_NRT/-13.5,27,11.8,37.6/2",
            f"/api/area/csv/{KEY}/VIIRS_SNPP_SP/-13.5,27,11.8,37.6/5/2023-07-22",
        ]

    def test_error_body_with_http_200_is_an_error_and_hides_the_key(self) -> None:
        client = _client(
            httpx.MockTransport(lambda _r: httpx.Response(200, text="Invalid MAP_KEY."))
        )
        with pytest.raises(FirmsError) as excinfo:
            client.area_csv("VIIRS_SNPP_NRT", BOX, 1)
        assert "invalid map_key" in str(excinfo.value).lower()
        assert KEY not in str(excinfo.value)

    def test_day_range_is_checked_before_any_request(self) -> None:
        client = _client(httpx.MockTransport(lambda _r: httpx.Response(500)))
        with pytest.raises(FirmsError, match="day_range"):
            client.area_csv("VIIRS_SNPP_NRT", BOX, 6)

    def test_transient_errors_are_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tenacity.nap.time.sleep", lambda _s: None)
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(503)
            return httpx.Response(200, text="latitude,longitude,acq_date,acq_time,confidence\n")

        assert _client(httpx.MockTransport(handler)).area_csv("MODIS_NRT", BOX, 1)
        assert calls["n"] == 3

    def test_network_failure_never_leaks_the_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tenacity.nap.time.sleep", lambda _s: None)

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach {request.url}")

        with pytest.raises(FirmsTransientError) as excinfo:
            _client(httpx.MockTransport(handler)).area_csv("MODIS_NRT", BOX, 1)
        assert KEY not in str(excinfo.value)

    def test_a_key_is_mandatory(self) -> None:
        with pytest.raises(FirmsError, match="MAP_KEY"):
            FirmsClient("")


class FakeProducer:
    """Records what the poller publishes; can refuse deliveries."""

    def __init__(self, fail_keys: set[bytes] | None = None) -> None:
        self.messages: list[tuple[str, bytes, bytes]] = []
        self._pending: list[tuple[object, bytes]] = []
        self._fail = fail_keys or set()

    def produce(
        self,
        topic: str,
        value: bytes | None = None,
        key: bytes | None = None,
        *,
        on_delivery: object = None,
    ) -> None:
        assert value is not None
        assert key is not None
        self.messages.append((topic, key, value))
        self._pending.append((on_delivery, key))

    def poll(self, timeout: float = 0) -> int:
        return 0

    def flush(self, timeout: float = 0) -> int:
        for callback, key in self._pending:
            assert callable(callback)
            callback("broker says no" if key in self._fail else None, None)
        self._pending.clear()
        return 0


def _poller(producer: FakeProducer, cache: SeenCache, body: str) -> FirmsPoller:
    client = _client(httpx.MockTransport(lambda _r: httpx.Response(200, text=body)))
    return FirmsPoller(
        client, producer, "firms.hotspots.raw", BOX, ["VIIRS_NOAA20_NRT"], 2, cache,
        clock=lambda: FETCHED,
    )  # fmt: skip


class TestPoller:
    BODY = (FIXTURES / "firms_viirs_nrt.csv").read_text()

    def test_publishes_new_detections_inside_the_aoi_once(self, tmp_path: Path) -> None:
        producer = FakeProducer()
        cache = SeenCache(tmp_path / "state.json")
        poller = _poller(producer, cache, self.BODY)

        stats = poller.poll_once()[0]
        assert (stats.fetched, stats.rejected, stats.outside_aoi, stats.published) == (7, 2, 1, 4)
        assert all(topic == "firms.hotspots.raw" for topic, _k, _v in producer.messages)
        # The record key is the hotspot identifier.
        key, value = producer.messages[0][1], producer.messages[0][2]
        assert key.decode() in value.decode()

        again = poller.poll_once()[0]
        assert (again.published, again.already_seen) == (0, 4)
        assert len(producer.messages) == 4

    def test_state_survives_a_restart(self, tmp_path: Path) -> None:
        _poller(FakeProducer(), SeenCache(tmp_path / "state.json"), self.BODY).poll_once()
        producer = FakeProducer()
        restarted = _poller(producer, SeenCache(tmp_path / "state.json"), self.BODY)
        # Entries are pruned relative to the clock (July 2023), so they are still known.
        assert restarted.poll_once()[0].published == 0
        assert producer.messages == []

    def test_failed_deliveries_are_retried_next_cycle(self, tmp_path: Path) -> None:
        cache = SeenCache(tmp_path / "state.json")
        first = FakeProducer()
        probe = _poller(first, SeenCache(None), self.BODY)
        probe.poll_once()
        refused = {first.messages[0][1]}

        failing = FakeProducer(fail_keys=refused)
        stats = _poller(failing, cache, self.BODY).poll_once()[0]
        assert (stats.published, stats.failed) == (3, 1)

        healthy = FakeProducer()
        retry = _poller(healthy, cache, self.BODY).poll_once()[0]
        assert retry.published == 1
        assert healthy.messages[0][1] in refused

    def test_a_failing_source_is_reported_not_raised(self) -> None:
        client = _client(
            httpx.MockTransport(lambda _r: httpx.Response(200, text="Invalid MAP_KEY."))
        )
        poller = FirmsPoller(
            client,
            FakeProducer(),
            "t",
            BOX,
            ["VIIRS_SNPP_NRT"],
            2,
            SeenCache(None),
            clock=lambda: FETCHED,
        )
        stats = poller.poll_once()[0]
        assert stats.error is not None
        assert stats.published == 0

    def test_backfill_walks_five_day_windows(self) -> None:
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path.split("/", 6)[-1])
            return httpx.Response(200, text="latitude,longitude,acq_date,acq_time,confidence\n")

        poller = FirmsPoller(
            _client(httpx.MockTransport(handler)),
            FakeProducer(),
            "t",
            BOX,
            ["VIIRS_SNPP_SP"],
            2,
            SeenCache(None),
            clock=lambda: FETCHED,
        )
        poller.backfill("VIIRS_SNPP_SP", dt.date(2023, 7, 20), dt.date(2023, 7, 31))
        assert requested == [
            "-13.5,27,11.8,37.6/5/2023-07-20",
            "-13.5,27,11.8,37.6/5/2023-07-25",
            "-13.5,27,11.8,37.6/2/2023-07-30",
        ]


class TestSeenCache:
    def test_prune_and_persist(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "state.json"
        cache = SeenCache(path, retention_days=3)
        cache.add("old", dt.date(2023, 7, 1))
        cache.add("new", dt.date(2023, 7, 9))
        assert cache.prune(dt.date(2023, 7, 10)) == 1
        assert "new" in cache
        assert "old" not in cache
        cache.save()
        assert len(SeenCache(path)) == 1

    def test_corrupt_state_file_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("{not json")
        assert len(SeenCache(path)) == 0

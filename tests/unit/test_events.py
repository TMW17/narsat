from __future__ import annotations

import datetime as dt
import uuid

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from narsat import ids
from narsat.events.clustering import NOISE, st_dbscan
from narsat.events.reconcile import reconcile
from narsat.events.static_sources import cell_index, persistent_cells
from narsat.geo import geodesic_area_ha, hull_of_points, utm_epsg_for

UTC = dt.UTC
T0 = dt.datetime(2023, 7, 23, tzinfo=UTC)


def _cluster(points: list[tuple[float, float, float]], **kwargs: float) -> np.ndarray:
    lat, lon, hours = (np.array(column) for column in zip(*points, strict=True))
    params = {"eps_km": 2.0, "eps_hours": 72.0, "min_samples": 1} | kwargs
    return st_dbscan(lat, lon, hours, **params)  # type: ignore[arg-type]


class TestStDbscan:
    def test_space_and_time_both_have_to_be_close(self) -> None:
        labels = _cluster([
            (36.700, 4.600, 0.0),    # fire A
            (36.705, 4.605, 6.0),    # A: ~0.7 km away, 6 h later
            (36.710, 4.610, 60.0),   # A: chained through the previous point
            (36.700, 4.600, 500.0),  # same place three weeks later: another fire
            (35.000, 3.000, 1.0),    # 200 km away
        ])  # fmt: skip
        assert labels[0] == labels[1] == labels[2]
        assert len({labels[0], labels[3], labels[4]}) == 3

    def test_coincident_detections_cluster_together(self) -> None:
        # Two satellites reporting the same pixel centre: distance exactly zero.
        labels = _cluster([(36.7, 4.6, 0.0), (36.7, 4.6, 0.5), (36.7, 4.6, 1.0)], min_samples=3)
        assert set(labels) == {0}

    def test_min_samples_marks_isolated_detections_as_noise(self) -> None:
        points = [(36.700, 4.600, 0.0), (36.701, 4.601, 1.0), (36.9, 5.5, 0.0)]
        assert _cluster(points, min_samples=1).tolist().count(NOISE) == 0
        labels = _cluster(points, min_samples=2)
        assert labels[0] == labels[1] != NOISE
        assert labels[2] == NOISE

    def test_eps_is_a_ground_distance_in_kilometres(self) -> None:
        # 0.02 degrees of latitude is about 2.2 km.
        points = [(36.70, 4.60, 0.0), (36.72, 4.60, 0.0)]
        assert len(set(_cluster(points, eps_km=2.0))) == 2
        assert len(set(_cluster(points, eps_km=2.5))) == 1

    def test_empty_input_and_bad_parameters(self) -> None:
        empty = np.array([])
        assert st_dbscan(empty, empty, empty, 2.0, 72.0).size == 0
        with pytest.raises(ValueError, match="positive"):
            st_dbscan(np.array([1.0]), np.array([1.0]), np.array([0.0]), 0.0, 72.0)
        with pytest.raises(ValueError, match="equal length"):
            st_dbscan(np.array([1.0, 2.0]), np.array([1.0]), np.array([0.0]), 2.0, 72.0)

    def test_result_does_not_depend_on_input_order(self) -> None:
        rng = np.random.default_rng(7)
        lat = 36.5 + rng.random(120) * 0.3
        lon = 4.5 + rng.random(120) * 0.3
        hours = rng.random(120) * 200
        order = rng.permutation(120)
        a = st_dbscan(lat, lon, hours, 2.0, 48.0)
        b = st_dbscan(lat[order], lon[order], hours[order], 2.0, 48.0)
        # Same partition: two points share a label in one run iff they do in the other.
        same_a = a[order][:, None] == a[order][None, :]
        same_b = b[:, None] == b[None, :]
        assert np.array_equal(same_a, same_b)


def _hid(n: int) -> uuid.UUID:
    return uuid.UUID(int=n, version=4)


def _ts(hours: float) -> dt.datetime:
    return T0 + dt.timedelta(hours=hours)


class TestReconcile:
    def test_new_clusters_are_seeded_by_their_earliest_detection(self) -> None:
        hotspots = [_hid(1), _hid(2), _hid(3)]
        out = reconcile(hotspots, [0, 0, 1], [_ts(5), _ts(1), _ts(2)], [None] * 3, {})
        assert out.event_ids[0] == out.event_ids[1] == ids.event_id(_hid(2))
        assert out.event_ids[2] == ids.event_id(_hid(3))
        assert out.seeds == {ids.event_id(_hid(2)): _hid(2), ids.event_id(_hid(3)): _hid(3)}
        assert not out.merged
        assert not out.dissolved

    def test_identity_survives_growth_even_with_an_earlier_late_arrival(self) -> None:
        event = ids.event_id(_hid(2))
        out = reconcile(
            [_hid(1), _hid(2), _hid(9)],
            [0, 0, 0],
            [_ts(5), _ts(1), _ts(0)],  # hotspot 9 was acquired before the seed
            [event, event, None],
            {event: _hid(2)},
        )
        assert set(out.event_ids) == {event}
        assert out.seeds[event] == _hid(2)

    def test_merge_keeps_the_event_with_the_earliest_seed(self) -> None:
        older, newer = ids.event_id(_hid(1)), ids.event_id(_hid(2))
        out = reconcile(
            [_hid(1), _hid(2), _hid(3)],
            [0, 0, 0],  # hotspot 3 bridges the two fires
            [_ts(0), _ts(10), _ts(11)],
            [older, newer, None],
            {older: _hid(1), newer: _hid(2)},
        )
        assert set(out.event_ids) == {older}
        assert out.merged == {newer: older}
        assert not out.dissolved

    def test_split_gives_the_identity_to_the_part_holding_the_seed(self) -> None:
        event = ids.event_id(_hid(1))
        out = reconcile(
            [_hid(1), _hid(2), _hid(3)],
            [0, 1, 1],
            [_ts(0), _ts(1), _ts(2)],
            [event, event, event],
            {event: _hid(1)},
        )
        assert out.event_ids[0] == event
        assert out.event_ids[1] == out.event_ids[2] == ids.event_id(_hid(2))
        assert event not in out.merged

    def test_event_whose_detections_all_became_noise_is_dissolved(self) -> None:
        event = ids.event_id(_hid(1))
        out = reconcile([_hid(1)], [NOISE], [_ts(0)], [event], {event: _hid(1)})
        assert out.event_ids == [None]
        assert out.dissolved == {event}

    def test_event_that_lost_its_seed_follows_its_detections(self) -> None:
        # The seed (hotspot 1) is no longer in the run, e.g. reclassified as a static source.
        gone = ids.event_id(_hid(1))
        out = reconcile([_hid(2), _hid(3)], [0, 0], [_ts(1), _ts(2)], [gone, gone], {gone: _hid(1)})
        successor = ids.event_id(_hid(2))
        assert set(out.event_ids) == {successor}
        assert out.merged == {gone: successor}

    def test_event_with_no_detections_left_at_all_is_dissolved(self) -> None:
        gone = ids.event_id(_hid(1))
        out = reconcile([], [], [], [], {gone: _hid(1)})
        assert out.dissolved == {gone}

    def test_length_mismatch_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="same length"):
            reconcile([_hid(1)], [0, 0], [_ts(0)], [None], {})


@st.composite
def clustering_runs(draw: st.DrawFn) -> tuple[list[uuid.UUID], list[int], list[dt.datetime]]:
    n = draw(st.integers(min_value=1, max_value=25))
    labels = draw(st.lists(st.integers(min_value=-1, max_value=5), min_size=n, max_size=n))
    hours = draw(st.lists(st.integers(min_value=0, max_value=400), min_size=n, max_size=n))
    return [_hid(i + 1) for i in range(n)], labels, [_ts(h) for h in hours]


class TestReconcileProperties:
    @given(clustering_runs())
    @settings(max_examples=200, deadline=None)
    def test_one_identity_per_cluster_and_idempotence(
        self, run: tuple[list[uuid.UUID], list[int], list[dt.datetime]]
    ) -> None:
        hotspots, labels, stamps = run
        first = reconcile(hotspots, labels, stamps, [None] * len(hotspots), {})

        # Noise has no event; each cluster has exactly one identity; identities are distinct.
        by_label: dict[int, set[uuid.UUID | None]] = {}
        for label, event in zip(labels, first.event_ids, strict=True):
            if label == NOISE:
                assert event is None
            else:
                by_label.setdefault(label, set()).add(event)
        assert all(len(events) == 1 for events in by_label.values())
        identities = [next(iter(events)) for events in by_label.values()]
        assert len(set(identities)) == len(identities)
        # Every event's seed is one of its own members.
        for event, seed in first.seeds.items():
            assert first.event_ids[hotspots.index(seed)] == event

        # Feeding the result back in changes nothing.
        second = reconcile(hotspots, labels, stamps, first.event_ids, first.seeds)
        assert second.event_ids == first.event_ids
        assert second.seeds == first.seeds
        assert not second.merged
        assert not second.dissolved

    @given(clustering_runs(), st.integers(min_value=0, max_value=400))
    @settings(max_examples=100, deadline=None)
    def test_adding_a_detection_to_a_cluster_never_renames_it(
        self, run: tuple[list[uuid.UUID], list[int], list[dt.datetime]], extra_hours: int
    ) -> None:
        hotspots, labels, stamps = run
        first = reconcile(hotspots, labels, stamps, [None] * len(hotspots), {})
        clustered = [label for label in labels if label != NOISE]
        if not clustered:
            return
        target = clustered[0]
        grown = reconcile(
            [*hotspots, _hid(10_000)],
            [*labels, target],
            [*stamps, _ts(extra_hours)],
            [*first.event_ids, None],
            first.seeds,
        )
        assert grown.event_ids[:-1] == first.event_ids
        assert grown.event_ids[-1] == first.event_ids[labels.index(target)]


class TestStaticSources:
    def test_cells_lit_on_many_days_are_persistent(self) -> None:
        days = [dt.date(2023, 7, d) for d in range(1, 16)]
        flare_lat = [31.6712 + 0.0005 * (i % 3) for i in range(15)]  # jitter inside one cell
        fire_days = [dt.date(2023, 7, 23)] * 6 + [dt.date(2023, 7, 24)] * 4
        fire_lat = [36.70 + 0.004 * i for i in range(10)]
        cells = persistent_cells(
            np.array(flare_lat + fire_lat),
            np.array([6.0703] * 15 + [4.60] * 10),
            np.array(days + fire_days),
            cell_deg=0.02,
            min_days=10,
        )
        assert len(cells) == 1
        assert cells.loc[0, "n_days"] == 15
        assert cells.loc[0, "cell_y"] == cell_index(np.array([31.6712]), 0.02)[0]

    def test_several_detections_on_one_day_count_once(self) -> None:
        cells = persistent_cells(
            np.array([31.67] * 30), np.array([6.07] * 30), np.array([dt.date(2023, 7, 1)] * 30),
            cell_deg=0.02, min_days=2,
        )  # fmt: skip
        assert cells.empty

    def test_empty_input(self) -> None:
        cells = persistent_cells(np.array([]), np.array([]), np.array([]), 0.02, 10)
        assert list(cells.columns) == ["cell_y", "cell_x", "n_days"]
        assert cells.empty

    def test_cell_index_matches_sql_floor_for_negative_coordinates(self) -> None:
        assert cell_index(np.array([-0.001, 0.0, 0.019, 0.02]), 0.02).tolist() == [-1, 0, 0, 1]


class TestGeo:
    def test_utm_zone(self) -> None:
        assert utm_epsg_for(4.6, 36.7) == 32631  # Kabylie
        assert utm_epsg_for(-6.8, 34.0) == 32629  # Rabat
        assert utm_epsg_for(10.2, 36.8) == 32632  # Tunis
        assert utm_epsg_for(18.4, -33.9) == 32734  # southern hemisphere

    def test_single_point_hull_is_a_disc_of_the_footprint(self) -> None:
        hull = hull_of_points([4.6], [36.7], buffer_m=500.0)
        assert hull.geom_type == "Polygon"
        assert hull.is_valid
        # A 500 m disc is 78.5 ha; the polygon approximation is slightly smaller.
        assert geodesic_area_ha(hull) == pytest.approx(78.5, rel=0.03)

    def test_hull_contains_all_points(self) -> None:
        from shapely.geometry import Point

        lons, lats = [4.60, 4.62, 4.61], [36.70, 36.70, 36.72]
        hull = hull_of_points(lons, lats, buffer_m=375.0)
        assert all(hull.contains(Point(x, y)) for x, y in zip(lons, lats, strict=True))

    def test_hull_needs_points(self) -> None:
        with pytest.raises(ValueError, match="at least one point"):
            hull_of_points([], [], 500.0)

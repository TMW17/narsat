from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.enums import Resampling
from shapely.geometry import Point, box

from narsat.config import SegmentationSettings
from narsat.geo import buffer_metres, geodesic_area_ha
from narsat.imagery import indices, reader, stac
from narsat.imagery.cog import REFLECTANCE_NODATA, encode_reflectance, write_cog
from narsat.imagery.reader import Grid, GridTooLargeError
from narsat.imagery.vectorize import points_to_pixels, polygonise, rasterise
from narsat.segmentation.dnbr import segment_dnbr
from narsat.segmentation.postprocess import (
    clean_mask,
    keep_patches_near_seeds,
    remove_small_patches,
)
from tests import synthetic

UTC = dt.UTC
PRE = dt.datetime(2023, 7, 10, 10, 30, tzinfo=UTC)
POST = dt.datetime(2023, 7, 28, 10, 30, tzinfo=UTC)


@pytest.fixture(scope="module")
def scenes(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    directory = tmp_path_factory.mktemp("scenes")
    return {
        "pre": synthetic.write_scene(directory, "S2A_T31SEA_PRE", PRE, burned=False, noise_seed=1),
        "post": synthetic.write_scene(
            directory, "S2A_T31SEA_POST", POST, burned=True, cloudy=True, noise_seed=2
        ),
    }


@pytest.fixture(scope="module")
def truth() -> synthetic.Truth:
    return synthetic.truth_20m()


@pytest.fixture(scope="module")
def grid(truth: synthetic.Truth) -> Grid:
    aoi = buffer_metres(Point(*truth.centre_lonlat), 2000.0)
    return Grid.for_geometry(aoi, 20.0, max_pixels=1_000_000)


@pytest.fixture(scope="module")
def arrays(scenes: dict[str, dict], grid: Grid) -> dict[str, reader.SceneArrays]:
    return {
        role: reader.read_scene(stac.group_scenes([item])[0], grid, threads=2)
        for role, item in scenes.items()
    }


def _truth_on(grid: Grid, mask: np.ndarray) -> np.ndarray:
    """Crop a truth mask (full 6 km synthetic tile, 20 m) to the analysis grid."""
    col = round((grid.transform.c - synthetic.ORIGIN_X) / 20.0)
    row = round((synthetic.ORIGIN_Y - grid.transform.f) / 20.0)
    return mask[row : row + grid.height, col : col + grid.width]


class TestIndices:
    def test_normalised_difference_handles_zero_denominator(self) -> None:
        a = np.array([0.4, 0.0, 0.1], dtype="float32")
        b = np.array([0.1, 0.0, 0.1], dtype="float32")
        out = indices.normalised_difference(a, b)
        assert out[0] == pytest.approx(0.6)
        assert np.isnan(out[1])
        assert out[2] == pytest.approx(0.0)

    def test_nbr_separates_burned_from_vegetation(self) -> None:
        vegetation = np.array(synthetic.VEGETATION, dtype="float32").reshape(6, 1, 1)
        burned = np.array(synthetic.BURNED, dtype="float32").reshape(6, 1, 1)
        assert indices.nbr(vegetation)[0, 0] > 0.5
        assert indices.nbr(burned)[0, 0] < -0.2
        assert indices.dnbr(vegetation, burned)[0, 0] > 0.66  # "high severity" class
        assert 0 < indices.rbr(vegetation, burned)[0, 0] < indices.dnbr(vegetation, burned)[0, 0]

    def test_otsu_splits_a_bimodal_sample_between_the_modes(self) -> None:
        rng = np.random.default_rng(0)
        sample = np.concatenate([rng.normal(0.02, 0.03, 4000), rng.normal(0.75, 0.05, 1500)])
        threshold = indices.otsu_threshold(sample)
        assert threshold is not None
        assert 0.2 < threshold < 0.6

    def test_otsu_declines_small_or_constant_samples(self) -> None:
        assert indices.otsu_threshold(np.array([0.1, 0.2, 0.3])) is None
        assert indices.otsu_threshold(np.full(500, 0.3)) is None
        assert indices.otsu_threshold(np.full(500, np.nan)) is None

    def test_dark_area_class_is_kept_as_observed(self) -> None:
        scl = np.array(
            [
                [
                    indices.SCL_DARK,
                    indices.SCL_CLOUD_HIGH,
                    indices.SCL_VEGETATION,
                    indices.SCL_NO_DATA,
                    indices.SCL_CLOUD_SHADOW,
                ]
            ],
            dtype="uint8",
        )
        reflectance = np.full((6, 1, 5), 0.2, dtype="float32")
        assert indices.observed(scl, reflectance).tolist() == [[True, False, True, False, False]]
        reflectance[3, 0, 2] = np.nan
        assert not indices.observed(scl, reflectance)[0, 2]


class TestStac:
    def test_scaling_from_the_raster_extension(self, scenes: dict[str, dict]) -> None:
        assert stac.reflectance_scaling(scenes["pre"], "nir08") == (0.0001, -0.1)

    def test_scaling_fallbacks_for_items_without_raster_metadata(self) -> None:
        def item(baseline: str, applied: bool | None) -> dict:
            properties: dict = {"s2:processing_baseline": baseline}
            if applied is not None:
                properties["earthsearch:boa_offset_applied"] = applied
            return {"properties": properties, "assets": {"red": {"href": "x"}}}

        assert stac.reflectance_scaling(item("03.01", None), "red") == (1e-4, 0.0)
        assert stac.reflectance_scaling(item("05.09", False), "red") == (1e-4, -0.1)
        assert stac.reflectance_scaling(item("05.09", True), "red") == (1e-4, 0.0)

    def test_relative_orbit_from_product_name(self, scenes: dict[str, dict]) -> None:
        assert stac.relative_orbit(scenes["pre"]) == 108
        assert stac.relative_orbit({"properties": {}}) is None
        assert stac.relative_orbit({"properties": {"sat:relative_orbit": 51}}) == 51

    def test_grouping_mosaics_tiles_and_drops_duplicate_deliveries(
        self, scenes: dict[str, dict]
    ) -> None:
        base = scenes["post"]

        def variant(item_id: str, tile: str, generated: str, when: dt.datetime = POST) -> dict:
            properties = {
                **base["properties"],
                "grid:code": tile,
                "s2:generation_time": generated,
                "datetime": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            return {**base, "id": item_id, "properties": properties}

        items = [
            variant("A_old", "MGRS-31SEA", "2023-07-28T12:00:00Z"),
            variant("A_new", "MGRS-31SEA", "2023-07-29T08:00:00Z"),  # reprocessed delivery
            variant("B", "MGRS-31SFA", "2023-07-28T12:00:00Z"),      # neighbouring tile
            variant("C", "MGRS-31SEA", "2023-08-02T12:00:00Z", POST + dt.timedelta(days=5)),
        ]  # fmt: skip
        grouped = stac.group_scenes(items)
        assert [scene.item_ids for scene in grouped] == [["C"], ["A_new", "B"]]
        assert grouped[0].datetime > grouped[1].datetime
        assert grouped[1].relative_orbit == 108


class TestGrid:
    def test_grid_is_aligned_and_covers_the_geometry(self, truth: synthetic.Truth) -> None:
        aoi = buffer_metres(Point(*truth.centre_lonlat), 1000.0)
        grid = Grid.for_geometry(aoi, 20.0, 1_000_000)
        assert grid.epsg == 32631
        assert grid.transform.c % 20 == 0
        assert grid.transform.f % 20 == 0
        assert 100 <= grid.width <= 102
        assert 100 <= grid.height <= 102
        left, bottom, right, top = grid.bounds
        assert right - left == grid.width * 20
        assert top - bottom == grid.height * 20

    def test_pixel_budget_is_enforced(self) -> None:
        with pytest.raises(GridTooLargeError, match="above the limit"):
            Grid.for_geometry(box(4.0, 36.0, 5.0, 37.0), 20.0, max_pixels=1_000_000)

    def test_coarser_grid_keeps_the_footprint(self, grid: Grid) -> None:
        coarse = grid.with_resolution(30.0)
        assert coarse.bounds[0] == grid.bounds[0]
        assert coarse.bounds[3] == grid.bounds[3]
        assert coarse.width * 30 >= grid.width * 20


class TestReader:
    def test_reflectance_is_decoded_with_scale_and_offset(
        self, arrays: dict[str, reader.SceneArrays], grid: Grid, truth: synthetic.Truth
    ) -> None:
        pre = arrays["pre"].reflectance
        assert pre.shape == (6, grid.height, grid.width)
        assert pre.dtype == np.float32
        vegetation = ~_truth_on(grid, truth.bare)
        # Median over vegetation pixels recovers the reflectance that was encoded.
        for index, expected in enumerate(synthetic.VEGETATION):
            assert np.nanmedian(pre[index][vegetation]) == pytest.approx(expected, abs=0.003)

    def test_ten_metre_bands_are_averaged_to_twenty(
        self, arrays: dict[str, reader.SceneArrays], grid: Grid, truth: synthetic.Truth
    ) -> None:
        # Averaging four 10 m pixels halves the noise standard deviation (0.004 -> 0.002).
        red = arrays["pre"].reflectance[indices.RED][~_truth_on(grid, truth.bare)]
        assert 0.0012 < float(np.nanstd(red)) < 0.0028

    def test_scl_is_read_with_nearest_neighbour(
        self, arrays: dict[str, reader.SceneArrays], grid: Grid, truth: synthetic.Truth
    ) -> None:
        scl = arrays["post"].scl
        assert scl.dtype == np.uint8
        assert set(np.unique(scl)) <= {
            indices.SCL_DARK,
            indices.SCL_VEGETATION,
            indices.SCL_BARE,
            indices.SCL_CLOUD_HIGH,
        }
        assert np.array_equal(scl == indices.SCL_DARK, _truth_on(grid, truth.burned))

    def test_area_outside_the_tile_is_nodata(self, scenes: dict[str, dict]) -> None:
        # A grid shifted half outside the 6 km synthetic tile.
        from affine import Affine

        outside = Grid(
            32631, Affine(20, 0, synthetic.ORIGIN_X - 2000, 0, -20, synthetic.ORIGIN_Y), 200, 100
        )
        data = reader.read_scene(stac.group_scenes([scenes["pre"]])[0], outside, threads=1)
        assert np.isfinite(data.reflectance[:, :, 100:]).all()
        assert (data.scl[:, :100] == 0).all()
        # GDAL's averaging kernel may reach one source pixel across the tile edge for the
        # 10 m bands, so the test is on what the pipeline uses: a pixel counts as observed
        # only when *all* bands are present and SCL is not "no data".
        assert np.isnan(data.reflectance[:, :, :99]).all()
        assert not indices.observed(data.scl, data.reflectance)[:, :100].any()
        assert indices.observed(data.scl, data.reflectance)[:, 100:].all()

    def test_resample_round_trip_keeps_values(self, grid: Grid) -> None:
        field = np.full(grid.shape, 0.25, dtype="float32")
        coarse = reader.resample(field, grid, grid.with_resolution(30.0), Resampling.average)
        back = reader.resample(coarse, grid.with_resolution(30.0), grid, Resampling.bilinear)
        assert back.shape == grid.shape
        assert np.nanmax(np.abs(back - 0.25)) < 1e-5


class TestDnbr:
    def test_recovers_the_burned_patch(
        self, arrays: dict[str, reader.SceneArrays], grid: Grid, truth: synthetic.Truth
    ) -> None:
        pre, post = arrays["pre"], arrays["post"]
        result = segment_dnbr(
            pre.reflectance, post.reflectance, pre.scl, post.scl, SegmentationSettings()
        )
        burned = _truth_on(grid, truth.burned)
        intersection = (result.mask & burned).sum()
        union = (result.mask | burned).sum()
        assert intersection / union > 0.97
        assert result.threshold_source in ("otsu", "otsu-clamped")
        assert 0.10 <= result.threshold <= 0.44

    def test_cloud_and_bare_ground_are_not_analysable(
        self, arrays: dict[str, reader.SceneArrays], grid: Grid, truth: synthetic.Truth
    ) -> None:
        pre, post = arrays["pre"], arrays["post"]
        result = segment_dnbr(
            pre.reflectance, post.reflectance, pre.scl, post.scl, SegmentationSettings()
        )
        cloud = _truth_on(grid, truth.cloud)
        bare = _truth_on(grid, truth.bare)
        assert not result.analysable[cloud].any()
        assert not result.analysable[bare].any()  # NDVI below the vegetation floor
        assert np.isnan(result.dnbr[cloud]).all()
        assert not result.mask[cloud | bare].any()

    def test_fixed_threshold_mode(self, arrays: dict[str, reader.SceneArrays]) -> None:
        pre, post = arrays["pre"], arrays["post"]
        settings = SegmentationSettings(dnbr_threshold_mode="fixed", dnbr_fixed_threshold=0.27)
        result = segment_dnbr(pre.reflectance, post.reflectance, pre.scl, post.scl, settings)
        assert result.threshold == 0.27
        assert result.threshold_source == "fixed"

    def test_unburned_pair_yields_no_burn(self, arrays: dict[str, reader.SceneArrays]) -> None:
        pre = arrays["pre"]
        result = segment_dnbr(
            pre.reflectance, pre.reflectance, pre.scl, pre.scl, SegmentationSettings()
        )
        # No change at all: Otsu has nothing to split, the fixed threshold applies.
        assert result.threshold_source == "fixed"
        assert not result.mask.any()


class TestPostprocess:
    def test_small_patches_are_removed(self) -> None:
        mask = np.zeros((40, 40), dtype=bool)
        mask[2:4, 2:4] = True  # 4 px
        mask[10:20, 10:20] = True  # 100 px
        out = remove_small_patches(mask, min_pixels=25)
        assert out.sum() == 100
        assert not out[2:4, 2:4].any()

    def test_only_patches_near_a_detection_are_kept(self) -> None:
        mask = np.zeros((60, 60), dtype=bool)
        mask[5:15, 5:15] = True  # the fire
        mask[40:50, 40:50] = True  # a harvested field, far from any detection
        out = keep_patches_near_seeds(mask, np.array([18]), np.array([10]), max_distance_px=5)
        assert out[5:15, 5:15].all()
        assert not out[40:50, 40:50].any()

    def test_without_seeds_nothing_is_kept(self) -> None:
        mask = np.ones((5, 5), dtype=bool)
        empty = np.array([], dtype="int64")
        assert not keep_patches_near_seeds(mask, empty, empty, 3).any()

    def test_clean_mask_combines_both_rules_and_can_disable_seeding(self) -> None:
        mask = np.zeros((60, 60), dtype=bool)
        mask[5:15, 5:15] = True
        mask[40:50, 40:50] = True
        mask[30, 30] = True
        seeds = (np.array([10]), np.array([10]))
        with_seeds = clean_mask(mask, 20.0, 1.0, *seeds, seed_distance_m=100.0)
        assert with_seeds.sum() == 100
        without = clean_mask(mask, 20.0, 1.0, *seeds, seed_distance_m=0.0)
        assert without.sum() == 200  # both large patches, the single pixel is gone

    def test_holes_are_not_filled(self) -> None:
        mask = np.ones((20, 20), dtype=bool)
        mask[8:12, 8:12] = False  # an unburned island
        out = clean_mask(mask, 20.0, 0.1, np.array([0]), np.array([0]), 100.0)
        assert not out[8:12, 8:12].any()


class TestVectorise:
    def test_area_matches_the_pixel_count(self, grid: Grid, truth: synthetic.Truth) -> None:
        burned = _truth_on(grid, truth.burned)
        result = polygonise(burned, grid)
        assert result.n_polygons == 1
        assert result.geometry.is_valid
        assert result.area_ha == pytest.approx(burned.sum() * 0.04, rel=0.01)
        assert geodesic_area_ha(result.geometry) == pytest.approx(result.area_ha)
        lon, lat = truth.centre_lonlat
        assert result.geometry.contains(Point(lon, lat))

    def test_empty_mask(self, grid: Grid) -> None:
        result = polygonise(np.zeros(grid.shape, dtype=bool), grid)
        assert result.geometry.is_empty
        assert (result.area_ha, result.n_polygons) == (0.0, 0)

    def test_diagonal_pixels_stay_separate_and_valid(self, grid: Grid) -> None:
        mask = np.zeros(grid.shape, dtype=bool)
        mask[10, 10] = mask[11, 11] = True
        result = polygonise(mask, grid, simplify=False)
        assert result.n_polygons == 2
        assert result.geometry.is_valid

    def test_rasterise_round_trip(self, grid: Grid, truth: synthetic.Truth) -> None:
        burned = _truth_on(grid, truth.burned)
        polygons = polygonise(burned, grid, simplify=False)
        assert np.array_equal(rasterise(polygons.geometry, grid), burned)

    def test_points_to_pixels(self, grid: Grid, truth: synthetic.Truth) -> None:
        lon, lat = truth.centre_lonlat
        rows, cols = points_to_pixels(np.array([lon, 0.0]), np.array([lat, 0.0]), grid)
        assert rows.size == 1  # the second point is far outside the grid
        assert _truth_on(grid, truth.burned)[rows[0], cols[0]]

    def test_shape_mismatch(self, grid: Grid) -> None:
        with pytest.raises(ValueError, match="does not match grid"):
            polygonise(np.zeros((3, 3), dtype=bool), grid)


class TestCog:
    def test_reflectance_encoding(self) -> None:
        values = np.array([[0.1234, np.nan, -0.02, 5.0]], dtype="float32")
        encoded = encode_reflectance(values)
        assert encoded.dtype == np.int16
        assert encoded.tolist() == [[1234, REFLECTANCE_NODATA, -200, 32767]]

    def test_written_file_is_a_tiled_geotiff_with_overviews(
        self, tmp_path: Path, arrays: dict[str, reader.SceneArrays], grid: Grid
    ) -> None:
        path = write_cog(
            tmp_path / "post.tif", encode_reflectance(arrays["post"].reflectance), grid,
            REFLECTANCE_NODATA, list(stac.REFLECTANCE_ASSETS), scale=1e-4,
        )  # fmt: skip
        with rasterio.open(path) as dataset:
            assert dataset.count == 6
            assert dataset.crs.to_epsg() == 32631
            assert dataset.transform == grid.transform
            assert dataset.nodata == REFLECTANCE_NODATA
            assert dataset.descriptions == stac.REFLECTANCE_ASSETS
            assert dataset.profile["tiled"]
            assert dataset.compression.value.upper() == "DEFLATE"
            assert dataset.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG"
            nir = dataset.read(4, masked=True)
        assert float(np.ma.median(nir)) * 1e-4 == pytest.approx(0.35, abs=0.01)

    def test_band_count_must_match(self, tmp_path: Path, grid: Grid) -> None:
        with pytest.raises(ValueError, match="does not match"):
            write_cog(
                tmp_path / "x.tif", np.zeros((2, *grid.shape), dtype="uint8"), grid, 0, ["only-one"]
            )


class TestReadRetries:
    """A DNS hiccup or a reset connection must not fail a whole event."""

    def _grid(self) -> Grid:
        return Grid(32631, Affine(20, 0, 600_000, 0, -20, 4_000_000), 4, 3)

    def test_transient_network_errors_are_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from rasterio.errors import RasterioIOError

        from narsat.imagery import reader

        path = self._write_tif()  # before rasterio.open is patched
        calls = {"n": 0}
        real_open = reader.rasterio.open

        def flaky_open(href: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] < 3:
                raise RasterioIOError(
                    "CURL error: Could not resolve host: e84-earth-search-sentinel-data.s3"
                )
            return real_open(href, *args, **kwargs)

        monkeypatch.setattr(reader.rasterio, "open", flaky_open)
        monkeypatch.setattr(reader._read_asset.retry, "sleep", lambda _: None)  # type: ignore[attr-defined]
        data = reader._read_asset(str(path), self._grid(), True)
        assert calls["n"] == 3
        assert data.shape == (3, 4)

    def test_permanent_errors_fail_at_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from rasterio.errors import RasterioIOError

        from narsat.imagery import reader

        calls = {"n": 0}

        def missing(*_: object, **__: object) -> None:
            calls["n"] += 1
            raise RasterioIOError("HTTP response code: 404")

        monkeypatch.setattr(reader.rasterio, "open", missing)
        monkeypatch.setattr(reader._read_asset.retry, "sleep", lambda _: None)  # type: ignore[attr-defined]
        with pytest.raises(RasterioIOError):
            reader._read_asset("https://example.test/missing.tif", self._grid(), True)
        assert calls["n"] == 1

    def test_giving_up_after_the_last_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from rasterio.errors import RasterioIOError

        from narsat.imagery import reader

        calls = {"n": 0}

        def always_down(*_: object, **__: object) -> None:
            calls["n"] += 1
            raise RasterioIOError("CURL error: Connection timed out after 30000 milliseconds")

        monkeypatch.setattr(reader.rasterio, "open", always_down)
        monkeypatch.setattr(reader._read_asset.retry, "sleep", lambda _: None)  # type: ignore[attr-defined]
        with pytest.raises(RasterioIOError):
            reader._read_asset("https://example.test/a.tif", self._grid(), False)
        assert calls["n"] == reader.READ_ATTEMPTS

    def test_a_failed_block_read_is_judged_by_the_error_it_chains(self) -> None:
        from rasterio.errors import RasterioIOError

        from narsat.imagery.reader import is_transient

        def read_failed(cause: str) -> RasterioIOError:
            try:
                try:
                    raise RuntimeError(cause)
                except RuntimeError as inner:
                    raise RasterioIOError(
                        "Read failed. See previous exception for details."
                    ) from inner
            except RasterioIOError as outer:
                return outer

        assert is_transient(read_failed("CURL error: Connection timed out after 30001 ms"))
        assert not is_transient(read_failed("TIFFReadEncodedTile() failed."))

    @pytest.mark.parametrize(
        ("message", "transient"),
        [
            ("CURL error: Could not resolve host: example.test", True),
            ("CURL error: Couldn't resolve host 'example.test'", True),
            ("CURL error: Connection reset by peer", True),
            ("CURL error: Failed to connect to example.test port 443", True),
            ("HTTP response code: 503", True),
            ("HTTP response code: 429", True),
            ("HTTP response code: 404", False),
            ("HTTP response code: 403", False),
            ("'/data/x.tif' not recognized as a supported file format.", False),
        ],
    )
    def test_which_errors_are_transient(self, message: str, transient: bool) -> None:
        from rasterio.errors import RasterioIOError

        from narsat.imagery.reader import is_transient

        assert is_transient(RasterioIOError(message)) is transient
        assert is_transient(ValueError(message)) is False

    def _write_tif(self) -> Path:
        import tempfile

        import rasterio

        path = Path(tempfile.mkdtemp()) / "scl.tif"
        with rasterio.open(
            path, "w", driver="GTiff", width=4, height=3, count=1, dtype="uint8",
            crs="EPSG:32631", transform=Affine(20, 0, 600_000, 0, -20, 4_000_000), nodata=0,
        ) as dst:  # fmt: skip
            dst.write(np.full((1, 3, 4), 4, dtype="uint8"))
        return path


def test_stac_requests_retry_with_back_off_and_time_out() -> None:
    io = stac.stac_io()
    assert io.timeout == stac.STAC_TIMEOUT
    retry = io.session.get_adapter("https://earth-search.aws.element84.com").max_retries
    assert retry.total == 5
    assert retry.backoff_factor == 1.0
    assert 503 in retry.status_forcelist
    assert retry.allowed_methods is None  # the POST search may be repeated

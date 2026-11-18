"""The raster tile URLs handed out by the API, requested from a real TiTiler.

Skipped unless ``NARSAT_TEST_TITILER_URL`` points at a running TiTiler that can read
this machine's files, for example::

    pip install titiler.application uvicorn
    uvicorn titiler.application.main:app --port 8083 &
    NARSAT_TEST_TITILER_URL=http://127.0.0.1:8083 pytest tests/integration/test_titiler_tiles.py
"""

from __future__ import annotations

import datetime as dt
import math
import os
import warnings
from pathlib import Path

import httpx2 as httpx
import numpy as np
import pytest
import rasterio
from rasterio.errors import NotGeoreferencedWarning
from rasterio.io import MemoryFile

from narsat.api.routers.events import tile_urls
from narsat.config import (
    ApiSettings,
    DbSettings,
    ImagerySettings,
    LakeSettings,
    SegmentationSettings,
    Settings,
)
from narsat.db import connect
from narsat.lake.storage import LakeStorage
from narsat.pipeline.map_event import EventMapper
from tests import synthetic
from tests.integration.helpers import single_event

pytestmark = pytest.mark.integration
UTC = dt.UTC
ZOOM = 13


def _tile_at(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def _decode(png: bytes) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)  # a PNG has no georeferencing
        with MemoryFile(png) as memory, memory.open() as dataset:
            return dataset.read()


@pytest.fixture
def titiler() -> str:
    url = os.environ.get("NARSAT_TEST_TITILER_URL")
    if not url:
        pytest.skip("set NARSAT_TEST_TITILER_URL to test the raster tile URLs against TiTiler")
    return url.rstrip("/")


def test_every_raster_layer_of_a_mapped_event_renders(
    titiler: str, db_settings: DbSettings, lake_settings: LakeSettings, tmp_path: Path
) -> None:
    items = [
        synthetic.write_scene(tmp_path, "PRE", dt.datetime(2023, 7, 10, 10, 30, tzinfo=UTC),
                              burned=False, noise_seed=1),
        synthetic.write_scene(tmp_path, "POST", dt.datetime(2023, 7, 28, 10, 30, tzinfo=UTC),
                              burned=True, noise_seed=2),
    ]  # fmt: skip
    event_id = single_event(db_settings)
    storage = LakeStorage.assets(lake_settings)
    storage.ensure_ready()
    mapper = EventMapper(
        ImagerySettings(), SegmentationSettings(methods=["dnbr"]), storage,
        synthetic.fake_search(items), clock=lambda: dt.datetime(2023, 10, 1, tzinfo=UTC),
    )  # fmt: skip
    with connect(db_settings) as conn:
        assert mapper.map_event(conn, event_id).status == "mapped"
        assets = conn.execute("SELECT kind, uri FROM raster_asset ORDER BY kind").fetchall()
    assert [a["kind"] for a in assets] == ["dnbr", "mask", "post", "pre"]

    settings = Settings(api=ApiSettings(titiler_public_url=titiler))
    lon, lat = synthetic.truth_20m().centre_lonlat
    x, y = _tile_at(lon, lat, ZOOM)
    rendered: dict[str, np.ndarray] = {}
    with httpx.Client(timeout=60.0) as client:
        assert client.get(f"{titiler}/healthz").status_code == 200
        for asset in assets:
            for name, template in tile_urls(settings, asset["uri"], asset["kind"]).items():
                url = (
                    template.replace("{z}", str(ZOOM)).replace("{x}", str(x)).replace("{y}", str(y))
                )
                response = client.get(url)
                assert response.status_code == 200, f"{name}: {response.text[:300]}"
                assert response.headers["content-type"] == "image/png"
                rendered[f"{asset['kind']}:{name}"] = _decode(response.content)

    assert set(rendered) == {
        "pre:true_color", "pre:swir_false_color", "post:true_color", "post:swir_false_color",
        "dnbr:dnbr", "mask:mask",
    }  # fmt: skip
    for name, image in rendered.items():
        assert image.shape[1:] == (256, 256), name
        alpha = image[-1]
        assert alpha.max() == 255, f"{name} is fully transparent"
    # The burn changes the post-fire image and leaves the pre-fire one alone.
    pre, post = rendered["pre:swir_false_color"], rendered["post:swir_false_color"]
    visible = (pre[-1] == 255) & (post[-1] == 255)
    assert np.abs(pre[:3].astype(int) - post[:3].astype(int))[:, visible].max() > 60
    # The mask raster holds 0 and 1: both must come out as distinct colours.
    mask = rendered["mask:mask"]
    colours = {tuple(c) for c in mask[:3, mask[-1] == 255].T.tolist()}
    assert len(colours) == 2


def test_tile_urls_point_at_the_file_path_or_the_object_uri() -> None:
    settings = Settings(api=ApiSettings(titiler_public_url="/titiler/"))
    local = tile_urls(settings, "file:///data/lake/assets/events/e/post.tif", "post")
    assert local["true_color"].startswith(
        "/titiler/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=%2Fdata%2Flake%2Fassets"
    )
    remote = tile_urls(settings, "s3://narsat/assets/events/e/dnbr.tif", "dnbr")
    assert "url=s3%3A%2F%2Fnarsat%2Fassets%2Fevents%2Fe%2Fdnbr.tif" in remote["dnbr"]
    assert set(tile_urls(settings, "s3://b/k.tif", "probability")) == {"probability"}
    assert rasterio.__version__  # the decoder used above

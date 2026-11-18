"""The Prithvi engine with the real architecture and random weights.

These tests need the ``prithvi`` extra (PyTorch + TerraTorch) and about 3 GB of
memory; they are skipped when it is not installed. They prove that

* the network described by the published config can be built with the installed
  TerraTorch,
* a Lightning-style checkpoint is loaded strictly, prefixes included,
* tiled inference runs end to end on a raster that is not a multiple of the tile.

They do **not** prove anything about accuracy: the weights are random. Running the
published checkpoint is a manual step (see the service README).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("terratorch")
yaml = pytest.importorskip("yaml")

from narsat_inference.config import InferenceSettings  # noqa: E402
from narsat_inference.prithvi import (  # noqa: E402
    PrithviBurnScarsEngine,
    build_task,
    fetch_model_files,
    load_checkpoint,
    normalisation,
)

pytestmark = pytest.mark.torch
CONFIG = Path(__file__).parent / "fixtures" / "burn_scars_architecture.yaml"


@pytest.fixture(scope="module")
def checkpoint(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A checkpoint in the layout Lightning writes: ``state_dict`` with a ``model.`` prefix."""
    torch.manual_seed(0)
    task = build_task(yaml.safe_load(CONFIG.read_text()))
    path = tmp_path_factory.mktemp("ckpt") / "random_weights.pt"
    state = {f"model.{key}": value for key, value in task.model.state_dict().items()}
    torch.save({"state_dict": state}, path)
    return path


@pytest.fixture(scope="module")
def engine(checkpoint: Path) -> PrithviBurnScarsEngine:
    settings = InferenceSettings(
        engine="prithvi", config_path=CONFIG, checkpoint_path=checkpoint, device="cpu",
        tile_size=512, tile_overlap=64, batch_size=1,
    )  # fmt: skip
    return PrithviBurnScarsEngine(settings)


def test_architecture_matches_the_published_model_size() -> None:
    task = build_task(yaml.safe_load(CONFIG.read_text()))
    parameters = sum(p.numel() for p in task.model.parameters())
    # ViT-L encoder (~304 M) plus the UNet decoder and necks.
    assert 300e6 < parameters < 350e6


def test_normalisation_statistics() -> None:
    means, stds = normalisation(yaml.safe_load(CONFIG.read_text()))
    assert means.shape == stds.shape == (6,)
    assert means[3] == pytest.approx(0.2323245)
    scaled = {"data": {"init_args": {"means": [m * 1e4 for m in means], "stds": list(stds * 1e4)}}}
    rescaled_means, _ = normalisation(scaled)
    assert np.allclose(rescaled_means, means, rtol=1e-5)


def test_checkpoint_loading_is_strict(tmp_path: Path) -> None:
    task = build_task(yaml.safe_load(CONFIG.read_text()))
    state = {f"model.{k}": v for k, v in task.model.state_dict().items()}
    state.pop(next(iter(state)))  # a checkpoint that lacks one tensor
    broken = tmp_path / "broken.pt"
    torch.save({"state_dict": state}, broken)
    with pytest.raises(RuntimeError, match="Missing key"):
        load_checkpoint(task, broken)


def test_loaded_weights_are_the_ones_in_the_checkpoint(
    engine: PrithviBurnScarsEngine, checkpoint: Path
) -> None:
    saved = torch.load(checkpoint, map_location="cpu", weights_only=True)["state_dict"]
    loaded = engine._task.model.state_dict()
    for key in ("encoder.patch_embed.proj.weight", "encoder.blocks.23.mlp.fc2.bias"):
        assert torch.equal(loaded[key], saved[f"model.{key}"])
    assert engine.info.engine == "prithvi"
    assert engine.info.version == "local"
    assert engine.info.device == "cpu"


def test_tiled_inference_on_an_arbitrary_raster(engine: PrithviBurnScarsEngine) -> None:
    rng = np.random.default_rng(0)
    cube = (rng.random((6, 600, 530), dtype="float32") * 0.4).astype("float32")
    cube[:, :40, :40] = np.nan
    probability = engine.predict(cube)
    assert probability.shape == (600, 530)
    assert probability.dtype == np.float32
    assert np.isfinite(probability).all()
    assert 0.0 <= float(probability.min()) <= float(probability.max()) <= 1.0
    assert (probability[:40, :40] == 0).all()  # missing input never yields a detection
    # Deterministic in eval mode.
    assert np.allclose(engine.predict(cube), probability, atol=1e-6)


def test_partial_local_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="set both"):
        fetch_model_files(InferenceSettings(engine="prithvi", config_path=CONFIG))

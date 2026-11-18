"""Prithvi-EO-2.0 burn-scar engine (IBM / NASA geospatial foundation model).

Model: ``ibm-nasa-geospatial/Prithvi-EO-2.0-300M-BurnScars`` on the Hugging Face Hub
(Apache-2.0). A ViT-L encoder pre-trained with masked auto-encoding on Harmonized
Landsat Sentinel-2 (HLS) data, with a UNet decoder fine-tuned on the HLS Burn Scars
dataset: single-date 512 x 512 chips at 30 m, six bands, two classes.

How it is used here
-------------------
* The architecture is built with TerraTorch from the ``model`` section of the YAML
  file published next to the weights, so it always matches the checkpoint; the
  checkpoint is then loaded strictly (a mismatch is an error, never a silent partial
  load).
* Inputs are standardised with the means and standard deviations of the same YAML.
* Rasters of any size are processed tile by tile (``narsat_inference.tiling``).

Known domain shift, to be measured rather than assumed away: the model saw HLS
(LaSRC atmospheric correction, BRDF-normalised, 30 m, United States), while NarSat
feeds it Sentinel-2 L2A (Sen2Cor, no BRDF normalisation) resampled to 30 m over
North Africa. Quantifying that gap is the project's research question.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

from narsat_inference.config import InferenceSettings
from narsat_inference.engines import BANDS, EngineInfo
from narsat_inference.tiling import predict_tiled

log = logging.getLogger("narsat_inference.prithvi")

BURN_CLASS = 1


def resolve_device(requested: str) -> str:
    import torch

    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def fetch_model_files(settings: InferenceSettings) -> tuple[Path, Path, str]:
    """Return ``(config yaml, checkpoint, version label)``, downloading them if needed.

    Files are cached by ``huggingface_hub`` (``HF_HOME``), so the 1.3 GB checkpoint is
    downloaded once.
    """
    if settings.config_path and settings.checkpoint_path:
        return settings.config_path, settings.checkpoint_path, "local"
    if settings.config_path or settings.checkpoint_path:
        raise ValueError("set both NARSAT_INFERENCE_CONFIG_PATH and ..._CHECKPOINT_PATH, or none")

    from huggingface_hub import HfApi, hf_hub_download

    api = HfApi()
    revision = settings.model_revision
    files = api.list_repo_files(settings.model_repo, revision=revision)
    checkpoints = [f for f in files if f.endswith((".pt", ".ckpt", ".pth"))]
    configs = [f for f in files if f.endswith((".yaml", ".yml")) and "/" not in f]
    if len(checkpoints) != 1 or len(configs) != 1:
        raise RuntimeError(
            f"cannot identify the checkpoint and config in {settings.model_repo}: "
            f"checkpoints={checkpoints}, configs={configs}. Set the paths explicitly."
        )
    commit = api.model_info(settings.model_repo, revision=revision).sha or revision
    config = hf_hub_download(settings.model_repo, configs[0], revision=commit)
    checkpoint = hf_hub_download(settings.model_repo, checkpoints[0], revision=commit)
    return Path(config), Path(checkpoint), commit[:12]


def normalisation(config: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """Per-band mean and standard deviation, in reflectance (0-1) units."""
    init_args = config["data"]["init_args"]
    means = np.asarray(init_args["means"], dtype="float32")
    stds = np.asarray(init_args["stds"], dtype="float32")
    if means.shape != (len(BANDS),) or stds.shape != (len(BANDS),):
        raise ValueError(f"expected {len(BANDS)} means and stds, got {means.shape}, {stds.shape}")
    if float(means.max()) > 1.0:  # statistics given in digital numbers (reflectance x 10000)
        means, stds = means / 10_000.0, stds / 10_000.0
    return means, stds


def build_task(config: dict[str, Any]) -> Any:
    """Instantiate the segmentation task described by the published config."""
    from terratorch.tasks import SemanticSegmentationTask

    init_args = config["model"]["init_args"]
    model_args = dict(init_args["model_args"])
    # The fine-tuned checkpoint contains every weight: do not fetch the base backbone.
    model_args["backbone_pretrained"] = False
    return SemanticSegmentationTask(model_args=model_args, model_factory=init_args["model_factory"])


def load_checkpoint(task: Any, checkpoint_path: Path) -> None:
    """Load fine-tuned weights the way ``terratorch.cli_tools.LightningInferenceModel`` does."""
    import torch
    from terratorch.cli_tools import remove_unexpected_prefix

    # weights_only=True: tensors only, no arbitrary pickled objects are executed.
    weights = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    if "state_dict" in weights:
        weights = weights["state_dict"]
    weights = remove_unexpected_prefix(weights)
    weights = {key.removeprefix("model."): value for key, value in weights.items()}
    task.model.load_state_dict(weights)  # strict


class PrithviBurnScarsEngine:
    def __init__(self, settings: InferenceSettings) -> None:
        import torch
        import yaml

        self._torch = torch
        self._settings = settings
        config_path, checkpoint_path, version = fetch_model_files(settings)
        config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))

        self._device = resolve_device(settings.device)
        task = build_task(config)
        load_checkpoint(task, checkpoint_path)
        self._task = task.eval().to(self._device)

        means, stds = normalisation(config)
        self._mean = torch.tensor(means, device=self._device).view(1, -1, 1, 1)
        self._std = torch.tensor(stds, device=self._device).view(1, -1, 1, 1)
        self.info = EngineInfo(
            name=settings.model_repo.rsplit("/", 1)[-1],
            version=version,
            engine="prithvi",
            resolution_m=30.0,
            device=self._device,
        )
        log.info("loaded %s (%s) on %s", self.info.name, version, self._device)

    def predict_batch(self, batch: np.ndarray) -> np.ndarray:
        """``(n, 6, tile, tile)`` reflectance -> ``(n, tile, tile)`` burn probability."""
        torch = self._torch
        with torch.inference_mode():
            x = torch.from_numpy(np.ascontiguousarray(batch, dtype="float32")).to(self._device)
            x = (x - self._mean) / self._std
            # The model expects (batch, bands, time, height, width) with one time step.
            logits = self._task(x.unsqueeze(2)).output
            probability = torch.softmax(logits, dim=1)[:, BURN_CLASS]
        result: np.ndarray = probability.float().cpu().numpy()
        return result

    def predict(self, cube: np.ndarray) -> np.ndarray:
        """``(6, height, width)`` reflectance (NaN = missing) -> ``(height, width)`` probability."""
        missing = ~np.isfinite(cube).all(axis=0)
        # Same convention as the training data module (``no_data_replace: 0``).
        filled = np.nan_to_num(cube, nan=0.0, posinf=0.0, neginf=0.0).astype("float32")
        probability = predict_tiled(
            filled,
            self.predict_batch,
            self._settings.tile_size,
            self._settings.tile_overlap,
            self._settings.batch_size,
        )
        probability[missing] = 0.0
        return probability

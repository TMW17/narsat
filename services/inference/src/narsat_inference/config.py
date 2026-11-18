"""Service configuration (environment variables prefixed ``NARSAT_INFERENCE_``)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class InferenceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NARSAT_INFERENCE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    #: ``prithvi`` is the foundation model; ``nbr`` is a single-date spectral baseline
    #: that needs no weights (CI, smoke tests, machines without the model).
    engine: Literal["prithvi", "nbr"] = "prithvi"

    # --- Prithvi-EO-2.0 burn-scar model (Hugging Face Hub) ---------------------------
    model_repo: str = "ibm-nasa-geospatial/Prithvi-EO-2.0-300M-BurnScars"
    #: Pin a commit of the model repository for reproducible results (``main`` follows it).
    model_revision: str = "main"
    #: Use local files instead of downloading (both must be set together).
    config_path: Path | None = None
    checkpoint_path: Path | None = None
    #: ``auto`` picks CUDA, then Apple MPS, then CPU.
    device: Literal["auto", "cpu", "cuda", "mps"] = "auto"

    # --- Tiling --------------------------------------------------------------------
    tile_size: int = Field(default=512, ge=64)
    tile_overlap: int = Field(default=64, ge=0)
    batch_size: int = Field(default=2, ge=1)

    # --- Limits --------------------------------------------------------------------
    #: Largest raster accepted in one request (pixels). 16 M covers 120 x 120 km at 30 m.
    max_pixels: int = Field(default=16_000_000, ge=4096)
    max_body_mb: int = Field(default=512, ge=1)

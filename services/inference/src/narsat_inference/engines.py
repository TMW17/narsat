"""Segmentation engines behind the HTTP contract.

An engine turns a reflectance cube into a burn probability raster. Band order is
fixed for all engines and matches the HLS bands the Prithvi burn-scar model was
fine-tuned on:

    0 blue (B02)   1 green (B03)   2 red (B04)   3 narrow NIR (B8A)
    4 SWIR 1 (B11)   5 SWIR 2 (B12)

Values are surface reflectance in 0-1 units; missing pixels are NaN.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

BANDS = ("blue", "green", "red", "nir08", "swir16", "swir22")
NIR, SWIR2 = 3, 5


@dataclass(frozen=True)
class EngineInfo:
    name: str
    version: str
    engine: str
    resolution_m: float
    device: str


class Engine(Protocol):
    info: EngineInfo

    def predict(self, cube: np.ndarray) -> np.ndarray:
        """``(6, height, width)`` reflectance -> ``(height, width)`` burn probability."""
        ...


class NbrBaselineEngine:
    """Single-date spectral baseline: a logistic function of the Normalised Burn Ratio.

    Fresh burn scars have low NIR and high SWIR reflectance, hence a strongly negative
    NBR. This engine maps NBR to a pseudo-probability with a logistic curve centred on
    ``centre``. It is **not** a validated burned-area product: dark soil, water and
    shadows also have low NBR. It exists so that the full pipeline can run and be
    tested on machines without the model weights, and as a floor that any learned
    model must beat.
    """

    def __init__(self, centre: float = -0.05, steepness: float = 15.0) -> None:
        self._centre = centre
        self._steepness = steepness
        self.info = EngineInfo(
            name="nbr-single-date-baseline",
            version="1",
            engine="nbr",
            resolution_m=30.0,
            device="cpu",
        )

    def predict(self, cube: np.ndarray) -> np.ndarray:
        nir, swir = cube[NIR].astype("float32"), cube[SWIR2].astype("float32")
        total = nir + swir
        with np.errstate(divide="ignore", invalid="ignore"):
            nbr = (nir - swir) / total
        probability = 1.0 / (1.0 + np.exp(self._steepness * (nbr - self._centre)))
        # No evidence where the input is missing.
        probability[~np.isfinite(nbr)] = 0.0
        result: np.ndarray = probability.astype("float32")
        return result

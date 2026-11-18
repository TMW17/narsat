"""Bi-temporal dNBR burned-area baseline.

The established remote-sensing method: compute the Normalised Burn Ratio before and
after the fire and threshold the difference. It needs no trained model, runs on a
laptop CPU in milliseconds and is the reference every learned model in this project
is compared against.

Threshold: Otsu's method on the dNBR values of the event's area of interest, clamped
to the range between the USGS "low severity" (0.10) and "moderate-high" (0.44)
boundaries. Otsu adapts to the scene, the clamp keeps it sane when the area is almost
entirely burned or almost entirely unburned (where the histogram is not bimodal).
A fixed threshold is available for reproducibility studies.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from narsat.config import SegmentationSettings
from narsat.imagery import indices

DNBR_VERSION = "dnbr-v1"


@dataclass(frozen=True)
class DnbrResult:
    dnbr: np.ndarray  # float32, NaN where not analysable
    mask: np.ndarray  # bool, raw (before patch post-processing)
    analysable: np.ndarray  # bool
    threshold: float
    threshold_source: str  # "otsu", "otsu-clamped" or "fixed"


def segment_dnbr(
    pre: np.ndarray,
    post: np.ndarray,
    pre_scl: np.ndarray,
    post_scl: np.ndarray,
    settings: SegmentationSettings,
) -> DnbrResult:
    """Threshold dNBR over the pixels that are observed in both dates.

    A pixel is analysable when it is visible before and after the fire, was not water
    before the fire and carried vegetation before the fire (NDVI above
    ``min_pre_ndvi``): bare ground cannot burn and its NBR changes for other reasons.
    """
    delta = indices.dnbr(pre, post)
    analysable = (
        indices.observed(pre_scl, pre)
        & indices.observed(post_scl, post)
        & (pre_scl != indices.SCL_WATER)
        & (np.nan_to_num(indices.ndvi(pre), nan=-1.0) >= settings.min_pre_ndvi)
        & np.isfinite(delta)
    )
    delta = np.where(analysable, delta, np.nan).astype("float32")

    threshold = settings.dnbr_fixed_threshold
    source = "fixed"
    if settings.dnbr_threshold_mode == "otsu":
        estimate = indices.otsu_threshold(delta[analysable])
        if estimate is not None:
            threshold = float(np.clip(estimate, settings.dnbr_otsu_min, settings.dnbr_otsu_max))
            source = "otsu" if threshold == estimate else "otsu-clamped"
    mask = analysable & (np.nan_to_num(delta, nan=-np.inf) >= threshold)
    return DnbrResult(delta, mask, analysable, threshold, source)

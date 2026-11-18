"""Turn a raw per-pixel burn mask into a product mask.

Two rules, applied to connected patches:

1. **Minimum size.** Patches smaller than the minimum mapping unit are dropped
   (isolated pixels are noise at 20 m).
2. **Thermal evidence.** A patch is kept only if it lies within a set distance of an
   active-fire detection of the event. This is what separates fire from the other
   things that lower NBR between two dates in North Africa: cereal harvest, ploughing,
   flooding. It combines two independent observations (thermal anomaly and spectral
   change) instead of trusting either alone.

Unburned islands inside a fire are real and are kept: holes are not filled.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage

# 8-connectivity: diagonal neighbours belong to the same patch.
_STRUCTURE = np.ones((3, 3), dtype=bool)


def remove_small_patches(mask: np.ndarray, min_pixels: int) -> np.ndarray:
    if min_pixels <= 1 or not mask.any():
        return mask.astype(bool)
    labels, count = ndimage.label(mask, structure=_STRUCTURE)
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    keep = sizes >= min_pixels
    keep[0] = False
    result: np.ndarray = keep[labels]
    return result


def keep_patches_near_seeds(
    mask: np.ndarray, seed_rows: np.ndarray, seed_cols: np.ndarray, max_distance_px: float
) -> np.ndarray:
    """Keep the patches that come within ``max_distance_px`` of any seed pixel."""
    if not mask.any():
        return mask.astype(bool)
    if seed_rows.size == 0:
        return np.zeros_like(mask, dtype=bool)
    seeds = np.zeros(mask.shape, dtype=bool)
    seeds[seed_rows, seed_cols] = True
    near = ndimage.distance_transform_edt(~seeds) <= max_distance_px
    labels, count = ndimage.label(mask, structure=_STRUCTURE)
    touched = np.unique(labels[near & (labels > 0)])
    keep = np.zeros(count + 1, dtype=bool)
    keep[touched] = True
    result: np.ndarray = keep[labels]
    return result


def clean_mask(
    mask: np.ndarray,
    resolution_m: float,
    min_patch_ha: float,
    seed_rows: np.ndarray,
    seed_cols: np.ndarray,
    seed_distance_m: float,
) -> np.ndarray:
    """Apply both rules. ``seed_distance_m <= 0`` disables the thermal-evidence rule."""
    pixel_ha = (resolution_m * resolution_m) / 10_000.0
    cleaned = remove_small_patches(mask.astype(bool), int(np.ceil(min_patch_ha / pixel_ha)))
    if seed_distance_m > 0:
        cleaned = keep_patches_near_seeds(
            cleaned, seed_rows, seed_cols, seed_distance_m / resolution_m
        )
    return cleaned

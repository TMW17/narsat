"""Spectral indices and masks used for burn mapping.

Band order of the reflectance stacks is fixed by ``REFLECTANCE_ASSETS``:
``blue, green, red, nir08 (B8A), swir16 (B11), swir22 (B12)``.

References
----------
* Key & Benson (2006), *Landscape Assessment*: NBR and dNBR, with the severity
  classes used by the USGS (dNBR 0.10 low, 0.27 moderate-low, 0.44 moderate-high,
  0.66 high).
* Parks, Dillon & Miller (2014): the relativised burn ratio RBR.
* Otsu (1979): histogram threshold maximising the between-class variance.
"""

from __future__ import annotations

import numpy as np

BLUE, GREEN, RED, NIR, SWIR1, SWIR2 = range(6)

# Sentinel-2 scene classification (Sen2Cor) codes.
SCL_NO_DATA, SCL_SATURATED, SCL_DARK, SCL_CLOUD_SHADOW = 0, 1, 2, 3
SCL_VEGETATION, SCL_BARE, SCL_WATER, SCL_UNCLASSIFIED = 4, 5, 6, 7
SCL_CLOUD_MEDIUM, SCL_CLOUD_HIGH, SCL_CIRRUS, SCL_SNOW = 8, 9, 10, 11

#: Classes that hide the surface. "Dark area" (2) is deliberately *not* in this set:
#: Sen2Cor often labels fresh burn scars as dark features, and masking them would
#: remove exactly the pixels being looked for.
SCL_OBSCURED = (
    SCL_NO_DATA, SCL_SATURATED, SCL_CLOUD_SHADOW, SCL_CLOUD_MEDIUM, SCL_CLOUD_HIGH,
    SCL_CIRRUS, SCL_SNOW,
)  # fmt: skip


def normalised_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """``(a - b) / (a + b)`` with NaN where the denominator vanishes."""
    total = a + b
    with np.errstate(divide="ignore", invalid="ignore"):
        result: np.ndarray = ((a - b) / total).astype("float32")
    result[~np.isfinite(result) | (np.abs(total) < 1e-6)] = np.nan
    return result


def nbr(reflectance: np.ndarray) -> np.ndarray:
    """Normalised Burn Ratio from narrow NIR (B8A) and SWIR 2 (B12)."""
    return normalised_difference(reflectance[NIR], reflectance[SWIR2])


def ndvi(reflectance: np.ndarray) -> np.ndarray:
    return normalised_difference(reflectance[NIR], reflectance[RED])


def dnbr(pre: np.ndarray, post: np.ndarray) -> np.ndarray:
    """Pre-fire minus post-fire NBR: positive where vegetation was lost."""
    delta: np.ndarray = (nbr(pre) - nbr(post)).astype("float32")
    return delta


def rbr(pre: np.ndarray, post: np.ndarray) -> np.ndarray:
    """Relativised burn ratio: dNBR / (NBR_pre + 1.001)."""
    pre_nbr = nbr(pre)
    ratio: np.ndarray = ((pre_nbr - nbr(post)) / (pre_nbr + 1.001)).astype("float32")
    return ratio


def observed(scl: np.ndarray, reflectance: np.ndarray) -> np.ndarray:
    """Pixels whose surface is visible: classified, not cloud/shadow/snow, all bands present."""
    return ~np.isin(scl, SCL_OBSCURED) & np.isfinite(reflectance).all(axis=0)


def otsu_threshold(values: np.ndarray, bins: int = 256) -> float | None:
    """Otsu's threshold of a 1-D sample; ``None`` if the sample is too small or constant."""
    sample = values[np.isfinite(values)]
    if sample.size < 50 or float(sample.max()) - float(sample.min()) < 1e-6:
        return None
    histogram, edges = np.histogram(sample, bins=bins)
    centres = (edges[:-1] + edges[1:]) / 2.0
    weights = histogram.astype("float64")
    cumulative = np.cumsum(weights)
    total = cumulative[-1]
    cumulative_mean = np.cumsum(weights * centres)
    background = cumulative[:-1]
    foreground = total - background
    valid = (background > 0) & (foreground > 0)
    if not valid.any():
        return None
    mean_background = np.divide(
        cumulative_mean[:-1], background, where=valid, out=np.zeros_like(background)
    )
    mean_foreground = np.divide(
        cumulative_mean[-1] - cumulative_mean[:-1],
        foreground,
        where=valid,
        out=np.zeros_like(foreground),
    )
    between = np.where(
        valid, background * foreground * (mean_background - mean_foreground) ** 2, -1.0
    )
    # When the two classes are separated by empty bins, every threshold in the gap has
    # the same (maximal) between-class variance. Taking the middle of that plateau,
    # rather than its first bin, puts the threshold as far as possible from both classes.
    maximal = np.flatnonzero(between >= between.max() * (1.0 - 1e-9))
    return float((edges[maximal[0] + 1] + edges[maximal[-1] + 1]) / 2.0)

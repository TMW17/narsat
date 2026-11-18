"""Persistent thermal anomalies (gas flares, steel works, cement kilns).

Near-real-time FIRMS files do not say whether a detection is a vegetation fire.
In North Africa that matters: the Saharan oil and gas fields flare continuously and
would otherwise dominate the event table.

A wildfire moves; a flare does not. The heuristic used here flags a grid cell as a
*static source* when detections fall in it on at least ``min_days`` distinct days
within the look-back window. It is deliberately conservative and has a known blind
spot, documented in ``docs/architecture.md``: a vegetation fire burning right next to
an industrial site is hidden with it.

Standard-processing files do carry the answer (``type`` 1, 2 or 3); when that column
is present it is used directly and this heuristic is not needed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def cell_index(values: np.ndarray | pd.Series, cell_deg: float) -> np.ndarray:
    """Integer grid index of a coordinate. Must match the SQL ``floor(x / cell_deg)``."""
    return np.floor(np.asarray(values, dtype="float64") / cell_deg).astype("int64")


def persistent_cells(
    latitudes: np.ndarray | pd.Series,
    longitudes: np.ndarray | pd.Series,
    acq_days: np.ndarray | pd.Series,
    cell_deg: float,
    min_days: int,
) -> pd.DataFrame:
    """Cells with detections on at least ``min_days`` distinct days.

    Returns a frame with ``cell_y``, ``cell_x`` and ``n_days``, sorted for determinism.
    """
    frame = pd.DataFrame(
        {
            "cell_y": cell_index(latitudes, cell_deg),
            "cell_x": cell_index(longitudes, cell_deg),
            "day": pd.Series(acq_days).to_numpy(),
        }
    )
    if frame.empty:
        return pd.DataFrame(
            {
                "cell_y": pd.Series(dtype="int64"),
                "cell_x": pd.Series(dtype="int64"),
                "n_days": pd.Series(dtype="int64"),
            }
        )
    counts = (
        frame.drop_duplicates()
        .groupby(["cell_y", "cell_x"], as_index=False)
        .agg(n_days=("day", "size"))
    )
    out = counts.loc[counts["n_days"] >= min_days].sort_values(["cell_y", "cell_x"])
    return out.reset_index(drop=True).astype({"n_days": "int64"})

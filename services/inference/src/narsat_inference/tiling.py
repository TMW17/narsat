"""Sliding-window inference with blended overlaps.

The burn-scar model was fine-tuned on 512 x 512 chips, while a fire's area of
interest has any size. The raster is therefore cut into overlapping tiles, each tile
is predicted, and the predictions are averaged with a weight that falls off towards
the tile border. Blending removes the visible seams (and the border artefacts of
transformer patches) that plain tile stitching leaves.

Pure NumPy: the model is passed in as a function, so this module is tested without
any deep-learning framework.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import numpy as np

#: ``(batch, bands, tile, tile) float32 -> (batch, tile, tile) float32`` probabilities.
PredictFunction = Callable[[np.ndarray], np.ndarray]


def tile_starts(length: int, tile: int, overlap: int) -> list[int]:
    """Start offsets of tiles covering ``length`` pixels; the last tile is flush with the end."""
    if length <= tile:
        return [0]
    stride = tile - overlap
    if stride <= 0:
        raise ValueError("overlap must be smaller than the tile size")
    starts = list(range(0, length - tile, stride))
    starts.append(length - tile)
    return starts


def blend_window(tile: int, overlap: int) -> np.ndarray:
    """Weight of each pixel of a tile: 1 in the centre, ramping down across the overlap."""
    ramp = np.ones(tile, dtype="float32")
    if overlap > 0:
        edge = np.linspace(0.0, 1.0, overlap + 2, dtype="float32")[1:-1]
        ramp[:overlap] = edge
        ramp[-overlap:] = edge[::-1]
    return np.outer(ramp, ramp)


def _batches(items: list[tuple[int, int]], size: int) -> Iterator[list[tuple[int, int]]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def predict_tiled(
    cube: np.ndarray,
    predict: PredictFunction,
    tile: int = 512,
    overlap: int = 64,
    batch_size: int = 2,
) -> np.ndarray:
    """Probability raster ``(height, width)`` for a ``(bands, height, width)`` cube.

    Rasters smaller than a tile are padded by reflection (the model always sees a
    full tile) and the prediction is cropped back.
    """
    if cube.ndim != 3:
        raise ValueError("expected an array of shape (bands, height, width)")
    _bands, height, width = cube.shape
    pad_h, pad_w = max(0, tile - height), max(0, tile - width)
    if pad_h or pad_w:
        mode = "reflect" if min(height, width) > 1 else "edge"
        cube = np.pad(cube, ((0, 0), (0, pad_h), (0, pad_w)), mode=mode)
    padded_h, padded_w = cube.shape[1:]

    window = blend_window(tile, overlap)
    total = np.zeros((padded_h, padded_w), dtype="float32")
    weight = np.zeros((padded_h, padded_w), dtype="float32")
    origins = [
        (row, col)
        for row in tile_starts(padded_h, tile, overlap)
        for col in tile_starts(padded_w, tile, overlap)
    ]
    for group in _batches(origins, batch_size):
        batch = np.stack([cube[:, r : r + tile, c : c + tile] for r, c in group]).astype("float32")
        output = np.asarray(predict(batch), dtype="float32")
        if output.shape != (len(group), tile, tile):
            raise ValueError(
                f"model returned shape {output.shape}, expected {(len(group), tile, tile)}"
            )
        for (row, col), probability in zip(group, output, strict=True):
            total[row : row + tile, col : col + tile] += probability * window
            weight[row : row + tile, col : col + tile] += window
    result: np.ndarray = (total / weight)[:height, :width]
    return np.clip(result, 0.0, 1.0)

"""Pixel-level evaluation against reference perimeters, on hand-built grids."""

from __future__ import annotations

import numpy as np
import pytest
from affine import Affine

from narsat.pipeline.compare import MASK_NODATA, EventRasters
from narsat.pipeline.evaluate import (
    FLAG_REFERENCE,
    VARIANTS,
    event_flags,
    merge,
    pixel_keys,
    scores,
    variant_masks,
)

NAN = float("nan")


def _rasters(
    dnbr_mask: list[int], model_mask: list[int], dnbr: list[float], left: float = 600_000.0
) -> EventRasters:
    width = len(dnbr_mask)
    flat = np.zeros((6, 1, width), dtype="float32")
    return EventRasters(
        dnbr_mask=np.array([dnbr_mask], dtype="uint8"),
        model_mask=np.array([model_mask], dtype="uint8"),
        dnbr=np.array([dnbr], dtype="float32"),
        pre=flat,
        post=flat,
        pixel_size_m=20.0,
        epsg=32631,
        transform=Affine(20, 0, left, 0, -20, 4_000_000),
    )


def test_the_four_maps() -> None:
    rasters = _rasters(
        dnbr_mask=[1, MASK_NODATA, 0, 0, 1, 0],
        model_mask=[1, 1, 1, 1, 0, 0],
        dnbr=[0.50, NAN, 0.04, 0.20, 0.45, 0.30],
    )
    masks = {name: mask[0].tolist() for name, mask in variant_masks(rasters).items()}
    assert masks["dnbr"] == [True, False, False, False, True, False]
    assert masks["model"] == [True, True, True, True, False, False]
    # Not analysed (bare before the fire) and unchanged pixels are dropped from the model.
    assert masks["model_gated"] == [True, False, False, True, False, False]
    assert masks["dnbr_or_model_gated"] == [True, False, False, True, True, False]


def test_the_same_ground_pixel_has_the_same_key_in_overlapping_grids() -> None:
    cols = np.arange(3)
    rows = np.zeros(3, dtype=int)
    first = pixel_keys(32631, Affine(20, 0, 600_000, 0, -20, 4_000_000), rows, cols)
    shifted = pixel_keys(32631, Affine(20, 0, 600_020, 0, -20, 4_000_000), rows, cols)
    assert first[1] == shifted[0]
    assert first[2] == shifted[1]
    other_zone = pixel_keys(32632, Affine(20, 0, 600_000, 0, -20, 4_000_000), rows, cols)
    assert not set(first) & set(other_zone)


def test_overlapping_events_count_a_pixel_once_and_combine_their_maps() -> None:
    a = _rasters([1, 1, 0], [0, 0, 0], [0.5, 0.5, 0.0])
    b = _rasters([0, 1, 1], [0, 0, 0], [0.0, 0.5, 0.5], left=600_020.0)  # one pixel to the east
    reference = np.ones((1, 3), dtype=bool)
    zone = np.ones((1, 3), dtype=bool)
    keys_a, flags_a = event_flags(a, reference, zone)
    keys_b, flags_b = event_flags(b, reference, zone)
    merged = merge([keys_a, keys_b], [flags_a, flags_b])

    # Four distinct ground pixels; the two shared ones are counted once.
    assert merged.size == 4
    burned = (merged & VARIANTS["dnbr"]) > 0
    # Event a burns pixels 0-1, event b pixels 1-2 of its own grid, i.e. 2-3 of a's.
    assert burned.sum() == 4


def test_scores() -> None:
    ref, dnbr = FLAG_REFERENCE, VARIANTS["dnbr"]
    flags = np.array([ref | dnbr] * 3 + [ref] + [dnbr], dtype=np.uint8)
    result = scores(flags, pixel_ha=0.04)
    assert result["reference_ha"] == pytest.approx(0.2, abs=0.05)
    assert result["dnbr"]["precision"] == 0.75
    assert result["dnbr"]["recall"] == 0.75
    assert result["dnbr"]["iou"] == 0.6
    assert result["dnbr"]["dice"] == 0.75
    assert result["model"]["precision"] is None  # nothing mapped
    assert result["model"]["recall"] == 0.0


def test_pixels_outside_the_zone_are_not_counted() -> None:
    rasters = _rasters([1, 1, 1], [1, 1, 1], [0.5, 0.5, 0.5])
    reference = np.ones((1, 3), dtype=bool)
    zone = np.array([[True, False, False]])
    keys, flags = event_flags(rasters, reference, zone)
    assert keys.size == flags.size == 1


def test_nothing_to_merge() -> None:
    assert merge([], []).size == 0

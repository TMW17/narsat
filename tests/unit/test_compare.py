"""Pixel-level explanation of where the dNBR and model maps disagree, on a known answer."""

from __future__ import annotations

import numpy as np
import pytest

from narsat.pipeline.compare import (
    MASK_NODATA,
    MODEL_ONLY_CLASSES,
    EventRasters,
    explain_disagreement,
)

GREEN = (0.05, 0.35)  # red, narrow NIR: NDVI 0.75
BARE = (0.25, 0.30)  # NDVI 0.09, below the 0.15 floor
NAN = float("nan")

# One pixel per case: (dNBR mask, model mask, dNBR, pre-fire red/NIR, post-fire red/NIR)
CASES: dict[str, tuple[int, int, float, tuple[float, float], tuple[float, float]]] = {
    "both": (1, 1, 0.60, GREEN, BARE),
    "bare_or_water_before": (MASK_NODATA, 1, NAN, BARE, BARE),
    "no_clear_view": (MASK_NODATA, 1, NAN, (NAN, NAN), BARE),
    "unchanged": (0, 1, 0.02, GREEN, GREEN),
    "low_severity": (0, 1, 0.20, GREEN, GREEN),
    "below_threshold": (0, 1, 0.28, GREEN, BARE),
    "removed_by_cleaning": (0, 1, 0.50, GREEN, BARE),
    "dnbr_only_model_blind": (1, MASK_NODATA, 0.55, GREEN, BARE),
    "dnbr_only_model_below": (1, 0, 0.45, GREEN, BARE),
    "neither": (0, 0, 0.01, GREEN, GREEN),
}


def _reflectance(pairs: list[tuple[float, float]]) -> np.ndarray:
    cube = np.full((6, 1, len(pairs)), 0.1, dtype="float32")
    for i, (red, nir) in enumerate(pairs):
        cube[:, 0, i] = NAN if np.isnan(red) else 0.1
        cube[2, 0, i] = red
        cube[3, 0, i] = nir
    return cube


def _rasters(cases: dict = CASES, pixel_size_m: float = 20.0) -> EventRasters:
    rows = list(cases.values())
    return EventRasters(
        dnbr_mask=np.array([[r[0] for r in rows]], dtype="uint8"),
        model_mask=np.array([[r[1] for r in rows]], dtype="uint8"),
        dnbr=np.array([[r[2] for r in rows]], dtype="float32"),
        pre=_reflectance([r[3] for r in rows]),
        post=_reflectance([r[4] for r in rows]),
        pixel_size_m=pixel_size_m,
    )


def test_every_disagreeing_pixel_gets_exactly_one_explanation() -> None:
    result = explain_disagreement(_rasters(), dnbr_threshold=0.30, min_pre_ndvi=0.15)

    assert result["burned_ha"] == {"both": 0.04, "dnbr_only": 0.08, "model_only": 0.24}
    by_class = {c["class"]: c for c in result["model_only"]}
    assert list(by_class) == list(MODEL_ONLY_CLASSES)
    for name, row in by_class.items():
        assert row["ha"] == 0.04, name
        assert row["share"] == pytest.approx(1 / 6, abs=0.001), name
        assert row["meaning"] == MODEL_ONLY_CLASSES[name]
    assert result["dnbr_only"] == {
        "model_could_not_see": 0.04,
        "model_scored_below_threshold": 0.04,
    }
    assert result["dnbr_threshold"] == 0.3
    assert result["min_pre_ndvi"] == 0.15


def test_reports_what_the_ground_looked_like() -> None:
    by_class = {
        c["class"]: c
        for c in explain_disagreement(_rasters(), dnbr_threshold=0.30, min_pre_ndvi=0.15)[
            "model_only"
        ]
    }
    # Still green after the fire: vegetation that did not burn.
    assert by_class["unchanged"]["median_post_ndvi"] == pytest.approx(0.75, abs=0.001)
    assert by_class["unchanged"]["median_dnbr"] == pytest.approx(0.02, abs=0.001)
    # Bare before the fire, so dNBR had nothing to measure.
    assert by_class["bare_or_water_before"]["median_pre_ndvi"] == pytest.approx(0.091, abs=0.001)
    assert by_class["bare_or_water_before"]["median_dnbr"] is None
    assert by_class["no_clear_view"]["median_pre_ndvi"] is None


def test_classes_stay_disjoint_when_the_threshold_is_below_the_low_severity_limit() -> None:
    cases = {**CASES, "low_severity": (0, 1, 0.20, GREEN, GREEN)}
    result = explain_disagreement(_rasters(cases), dnbr_threshold=0.15, min_pre_ndvi=0.15)
    by_class = {c["class"]: c["ha"] for c in result["model_only"]}

    # 0.20, 0.28 and 0.50 are all above a 0.15 threshold: the patches were cleaned away.
    assert by_class["removed_by_cleaning"] == 0.12
    assert by_class["low_severity"] == by_class["below_threshold"] == 0.0
    assert sum(by_class.values()) == pytest.approx(result["burned_ha"]["model_only"])


def test_no_disagreement_gives_empty_classes() -> None:
    agree = {k: v for k, v in CASES.items() if k in ("both", "neither")}
    result = explain_disagreement(_rasters(agree), dnbr_threshold=0.30, min_pre_ndvi=0.15)
    assert result["burned_ha"] == {"both": 0.04, "dnbr_only": 0.0, "model_only": 0.0}
    assert all(c["ha"] == 0.0 and c["share"] == 0.0 for c in result["model_only"])


def test_rasters_on_different_grids_are_refused() -> None:
    rasters = _rasters()
    shifted = EventRasters(
        dnbr_mask=rasters.dnbr_mask,
        model_mask=rasters.model_mask[:, :-1],
        dnbr=rasters.dnbr,
        pre=rasters.pre,
        post=rasters.post,
        pixel_size_m=20.0,
    )
    with pytest.raises(ValueError, match="one grid"):
        explain_disagreement(shifted, dnbr_threshold=0.30, min_pre_ndvi=0.15)


def test_totals_add_up_per_outcome_and_over_all_events() -> None:
    from narsat.pipeline.compare import measure, summarise

    one = measure(_rasters(), dnbr_threshold=0.30, min_pre_ndvi=0.15)
    agree = {k: v for k, v in CASES.items() if k in ("both", "neither")}
    two = measure(_rasters(agree, pixel_size_m=10.0), dnbr_threshold=0.30, min_pre_ndvi=0.15)

    rows = {row["outcome"]: row for row in summarise([("both", one), ("both", two), ("x", one)])}

    assert list(rows) == ["both", "x", "all events"]
    assert rows["both"]["events"] == 2
    # 20 m pixels are 0.04 ha, 10 m pixels 0.01 ha: areas are summed in hectares.
    assert rows["both"]["burned_ha"]["both"] == pytest.approx(0.05, abs=0.051)
    assert rows["all events"]["events"] == 3
    shares = rows["all events"]["model_only_share"]
    assert list(shares) == list(MODEL_ONLY_CLASSES)
    assert sum(shares.values()) == pytest.approx(1.0, abs=0.01)
    assert rows["x"]["dnbr_only"] == {
        "model_could_not_see": 0.0,
        "model_scored_below_threshold": 0.0,
    }


def test_summing_happens_before_rounding() -> None:
    from narsat.pipeline.compare import measure, summarise

    result = measure(_rasters(), dnbr_threshold=0.30, min_pre_ndvi=0.15)
    rows = summarise([("both", result)] * 100)
    # 100 events x 6 model-only pixels x 0.04 ha.
    assert rows[0]["burned_ha"]["model_only"] == pytest.approx(24.0)
    assert rows[0]["model_only_ha"]["unchanged"] == pytest.approx(4.0)


def test_nothing_to_summarise() -> None:
    from narsat.pipeline.compare import summarise

    assert summarise([]) == []

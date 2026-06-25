"""Tests for confidence blending + calibration (doc 04 §6, doc 06 §6).

Written test-first per the project's TDD mandate. Covers:
- ``blend_raw_likelihood`` monotonicity (↑corroboration ⇒ ≥; ↑n_competing ⇒ ≤),
  [0, 1] clamping, and input validation.
- ``Calibrator`` temperature scaling: T>1 softens an over-confident raw, T==1 is
  ~identity, and (0, 1) is guarded via epsilon.
- ``build_assessment`` stamps the band/version and bands a calibrated 0.9 → HIGH,
  0.3 → LOW.
"""

from __future__ import annotations

import math

import pytest

from sentinel.confidence import Calibrator, blend_raw_likelihood, build_assessment
from sentinel.contracts import ConfidenceAssessment
from sentinel.enums import (
    BAND_HIGH_THRESHOLD,
    BAND_MEDIUM_THRESHOLD,
    ConfidenceBand,
    band_for,
)

# --------------------------------------------------------------------------- #
# blend_raw_likelihood                                                        #
# --------------------------------------------------------------------------- #


def test_blend_returns_value_in_unit_interval() -> None:
    for sa in (0.0, 0.25, 0.5, 0.75, 1.0):
        for corr in (0, 1, 5):
            for comp in (0, 1, 4):
                value = blend_raw_likelihood(sa, corr, comp)
                assert 0.0 <= value <= 1.0


def test_blend_no_corroboration_no_competition_is_self_assessment() -> None:
    # With no corroboration boost and no competition penalty, the blend should
    # not move below the self-assessment (it is the starting point, doc 04 §6).
    assert blend_raw_likelihood(0.6, 0, 0) == pytest.approx(0.6)


def test_blend_monotonic_nondecreasing_in_corroboration() -> None:
    prev = blend_raw_likelihood(0.4, 0, 1)
    for corr in range(1, 8):
        cur = blend_raw_likelihood(0.4, corr, 1)
        assert cur >= prev
        prev = cur


def test_blend_monotonic_nonincreasing_in_competition() -> None:
    prev = blend_raw_likelihood(0.7, 3, 0)
    for comp in range(1, 8):
        cur = blend_raw_likelihood(0.7, 3, comp)
        assert cur <= prev
        prev = cur


def test_blend_corroboration_increases_confidence() -> None:
    base = blend_raw_likelihood(0.5, 0, 0)
    assert blend_raw_likelihood(0.5, 4, 0) > base


def test_blend_competition_caps_confidence() -> None:
    base = blend_raw_likelihood(0.9, 0, 0)
    assert blend_raw_likelihood(0.9, 0, 3) < base


def test_blend_rejects_self_assessment_out_of_range() -> None:
    with pytest.raises(ValueError):
        blend_raw_likelihood(-0.01, 0, 0)
    with pytest.raises(ValueError):
        blend_raw_likelihood(1.01, 0, 0)


def test_blend_rejects_negative_counts() -> None:
    with pytest.raises(ValueError):
        blend_raw_likelihood(0.5, -1, 0)
    with pytest.raises(ValueError):
        blend_raw_likelihood(0.5, 0, -1)


# --------------------------------------------------------------------------- #
# Calibrator                                                                  #
# --------------------------------------------------------------------------- #


def test_calibrator_t1_is_approximately_identity() -> None:
    cal = Calibrator(temperature=1.0, version="t1")
    for x in (0.05, 0.2, 0.5, 0.8, 0.95):
        assert cal.apply(x) == pytest.approx(x, abs=1e-9)


def test_calibrator_t_greater_than_one_softens_overconfidence() -> None:
    cal = Calibrator(temperature=2.0, version="t2")
    # An over-confident raw above the midpoint is pulled toward 0.5.
    assert cal.apply(0.9) < 0.9
    assert cal.apply(0.9) > 0.5


def test_calibrator_t_greater_than_one_raises_underconfidence() -> None:
    cal = Calibrator(temperature=2.0, version="t2")
    # Symmetrically, a low raw below the midpoint is pulled up toward 0.5.
    assert cal.apply(0.1) > 0.1
    assert cal.apply(0.1) < 0.5


def test_calibrator_guards_zero_and_one() -> None:
    cal = Calibrator(temperature=2.0, version="t2")
    low = cal.apply(0.0)
    high = cal.apply(1.0)
    assert 0.0 < low < high < 1.0
    assert math.isfinite(low) and math.isfinite(high)


def test_calibrator_is_monotonic() -> None:
    cal = Calibrator(temperature=2.0, version="t2")
    xs = [0.01, 0.1, 0.3, 0.5, 0.7, 0.9, 0.99]
    ys = [cal.apply(x) for x in xs]
    assert ys == sorted(ys)


def test_calibrator_rejects_nonpositive_temperature() -> None:
    with pytest.raises(ValueError):
        Calibrator(temperature=0.0, version="bad")
    with pytest.raises(ValueError):
        Calibrator(temperature=-1.0, version="bad")


# --------------------------------------------------------------------------- #
# build_assessment                                                            #
# --------------------------------------------------------------------------- #


def test_build_assessment_returns_confidence_assessment() -> None:
    cal = Calibrator(temperature=1.0, version="cal-v1")
    assessment = build_assessment(
        incident_id="inc_1",
        top_hypothesis_id="hyp_1",
        raw_likelihood=0.72,
        corroboration_count=2,
        n_competing=1,
        calibrator=cal,
    )
    assert isinstance(assessment, ConfidenceAssessment)
    assert assessment.incident_id == "inc_1"
    assert assessment.top_hypothesis_id == "hyp_1"
    assert assessment.corroboration_count == 2
    assert assessment.n_competing == 1


def test_build_assessment_stamps_calibrator_version() -> None:
    cal = Calibrator(temperature=1.5, version="cal-2026-06")
    assessment = build_assessment(
        incident_id="inc_2",
        top_hypothesis_id="hyp_2",
        raw_likelihood=0.5,
        corroboration_count=0,
        n_competing=0,
        calibrator=cal,
    )
    assert assessment.calibrator_version == "cal-2026-06"


def test_build_assessment_band_matches_band_for() -> None:
    cal = Calibrator(temperature=1.7, version="cal-v1")
    assessment = build_assessment(
        incident_id="inc_3",
        top_hypothesis_id="hyp_3",
        raw_likelihood=0.88,
        corroboration_count=3,
        n_competing=0,
        calibrator=cal,
    )
    assert assessment.band == band_for(assessment.calibrated_confidence)


def test_build_assessment_calibrated_value_is_the_applied_calibrator() -> None:
    cal = Calibrator(temperature=2.0, version="cal-v1")
    raw = 0.83
    expected = cal.apply(raw)
    assessment = build_assessment(
        incident_id="inc_4",
        top_hypothesis_id="hyp_4",
        raw_likelihood=raw,
        corroboration_count=0,
        n_competing=0,
        calibrator=cal,
    )
    assert assessment.calibrated_confidence == pytest.approx(expected)


def test_build_assessment_high_band_for_confident_raw() -> None:
    # T==1 is ~identity, so a raw of 0.9 stays HIGH (≥ 0.8).
    cal = Calibrator(temperature=1.0, version="cal-v1")
    assessment = build_assessment(
        incident_id="inc_5",
        top_hypothesis_id="hyp_5",
        raw_likelihood=0.9,
        corroboration_count=0,
        n_competing=0,
        calibrator=cal,
    )
    assert assessment.band == ConfidenceBand.HIGH
    assert assessment.calibrated_confidence >= BAND_HIGH_THRESHOLD


def test_build_assessment_low_band_for_weak_raw() -> None:
    # T==1 is ~identity, so a raw of 0.3 stays LOW (< 0.5).
    cal = Calibrator(temperature=1.0, version="cal-v1")
    assessment = build_assessment(
        incident_id="inc_6",
        top_hypothesis_id="hyp_6",
        raw_likelihood=0.3,
        corroboration_count=0,
        n_competing=0,
        calibrator=cal,
    )
    assert assessment.band == ConfidenceBand.LOW
    assert assessment.calibrated_confidence < BAND_MEDIUM_THRESHOLD

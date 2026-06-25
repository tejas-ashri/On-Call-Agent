"""Confidence calibration measurement (doc 06 §6.2–6.3), pure-Python.

Proves the calibration *works*, not just that a map exists: a reliability/ECE measurement
(§6.2) and an NLL-minimizing temperature fit (§6.3), then the headline — fitting the
temperature on an over-confident model and re-measuring ECE shows it collapse toward zero.
All stdlib-only and deterministic, matching ``sentinel.confidence``'s no-numpy stance.
"""

from __future__ import annotations

import math

from sentinel.confidence import Calibrator
from sentinel.eval.calibration import (
    ReliabilityBin,
    expected_calibration_error,
    fit_temperature,
    negative_log_likelihood,
    reliability_and_ece,
)


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _dataset(distortion: float) -> tuple[list[float], list[bool]]:
    """Samples whose STATED confidence is ``sigmoid(distortion·logit(p))`` while the true
    accuracy is ``p``. distortion>1 = over-confident; =1 = perfectly calibrated. Temperature
    scaling with T==distortion recovers calibration exactly, so the fit must recover it."""
    confidences: list[float] = []
    correct: list[bool] = []
    for p in (0.55, 0.65, 0.75, 0.85, 0.95):
        n = 200
        stated = _sigmoid(distortion * _logit(p))
        k = round(n * p)  # exactly p·n correct → bin accuracy == p, no rounding noise
        for i in range(n):
            confidences.append(stated)
            correct.append(i < k)
    return confidences, correct


def test_perfectly_calibrated_has_near_zero_ece() -> None:
    conf, correct = _dataset(distortion=1.0)  # stated == true accuracy
    assert expected_calibration_error(conf, correct) < 1e-6


def test_ece_flags_overconfidence() -> None:
    conf, correct = _dataset(distortion=3.0)
    assert expected_calibration_error(conf, correct) > 0.1


def test_reliability_bins_partition_weights_to_one() -> None:
    conf, correct = _dataset(distortion=3.0)
    bins, _ = reliability_and_ece(conf, correct)
    assert bins and all(isinstance(b, ReliabilityBin) for b in bins)
    assert math.isclose(sum(b.weight for b in bins), 1.0, abs_tol=1e-9)
    for b in bins:  # every bin's stats are valid probabilities
        assert 0.0 <= b.mean_confidence <= 1.0
        assert 0.0 <= b.accuracy <= 1.0


def test_fit_temperature_recovers_overconfidence() -> None:
    conf, correct = _dataset(distortion=3.0)
    t = fit_temperature(conf, correct)
    assert 2.0 < t < 4.0  # ≈ the distortion (3.0); T>1 softens over-confidence


def test_fit_temperature_near_one_when_calibrated() -> None:
    conf, correct = _dataset(distortion=1.0)
    assert 0.8 < fit_temperature(conf, correct) < 1.3


def test_fitted_temperature_lowers_nll_vs_identity() -> None:
    conf, correct = _dataset(distortion=3.0)
    t = fit_temperature(conf, correct)
    assert negative_log_likelihood(conf, correct, t) < negative_log_likelihood(conf, correct, 1.0)


def test_calibration_collapses_ece() -> None:
    """The headline: fit T on an over-confident model, apply the Calibrator, ECE collapses."""
    conf, correct = _dataset(distortion=3.0)
    raw_ece = expected_calibration_error(conf, correct)

    t = fit_temperature(conf, correct)
    calibrator = Calibrator(temperature=t, version="cal.test")
    calibrated = [calibrator.apply(c) for c in conf]
    calibrated_ece = expected_calibration_error(calibrated, correct)

    assert raw_ece > 0.1
    assert calibrated_ece < raw_ece
    assert calibrated_ece < 0.02  # essentially on the diagonal after calibration

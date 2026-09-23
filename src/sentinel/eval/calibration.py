"""Calibration measurement: reliability binning, ECE, and the temperature fit (doc 06 §6).

The escalation policy reads exactly one number — ``calibrated_confidence`` — and the bands
(``LOW`` < 0.5 ≤ ``MEDIUM`` < 0.8 ≤ ``HIGH``) must *mean* what they claim, or P6 ("confidence
is calibrated, not vibes") and the C1 autonomy guarantee collapse. This module quantifies
how true that is and fits the map that makes it true:

* :func:`reliability_and_ece` / :func:`expected_calibration_error` — bin predictions by
  confidence and measure the accuracy-weighted gap between confidence and observed accuracy
  (§6.2). ECE is the headline calibration metric (the CI gate caps it).
* :func:`fit_temperature` — fit the scalar ``T`` of :class:`sentinel.confidence.Calibrator`
  by minimizing negative log-likelihood (§6.3). ``T > 1`` softens over-confidence.

Deliberately stdlib-only (the doc sketches it in numpy/scipy; we port it to plain ``math``
to match :mod:`sentinel.confidence` and keep the core dependency-light). The minimizer is a
golden-section search — NLL of a temperature map is unimodal on the bounded interval.
""" 

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

_EPSILON = 1e-6  # clip probabilities off the logit/log singularities at 0 and 1 (§6.3)


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    """One bin of a reliability diagram (§6.2): a point (mean_confidence, accuracy)."""

    lo: float
    hi: float
    mean_confidence: float
    accuracy: float
    weight: float  # fraction of all samples that fell in this bin


def _validate(confidences: Sequence[float], correct: Sequence[bool]) -> None:
    if len(confidences) != len(correct):
        raise ValueError("confidences and correct must be the same length")
    for c in confidences:
        if not 0.0 <= c <= 1.0:
            raise ValueError(f"confidence out of range [0, 1]: {c}")


def reliability_and_ece(
    confidences: Sequence[float], correct: Sequence[bool], *, n_bins: int = 10
) -> tuple[list[ReliabilityBin], float]:
    """Bin by confidence; return the non-empty reliability bins and the scalar ECE.

    A perfectly calibrated predictor lies on the diagonal (mean_confidence == accuracy in
    every bin), giving ECE == 0. ECE is the accuracy-weighted mean ``|confidence - accuracy|``.
    """
    if n_bins < 1:
        raise ValueError(f"n_bins must be >= 1: {n_bins}")
    _validate(confidences, correct)
    n = len(confidences)
    if n == 0:
        return [], 0.0

    bins: list[ReliabilityBin] = []
    ece = 0.0
    for k in range(n_bins):
        lo = k / n_bins
        hi = (k + 1) / n_bins
        is_last = k == n_bins - 1
        members = [
            j
            for j in range(n)
            if confidences[j] >= lo and (confidences[j] <= hi if is_last else confidences[j] < hi)
        ]
        if not members:
            continue
        m = len(members)
        mean_conf = math.fsum(confidences[j] for j in members) / m
        accuracy = math.fsum(1.0 if correct[j] else 0.0 for j in members) / m
        weight = m / n
        bins.append(ReliabilityBin(lo, hi, mean_conf, accuracy, weight))
        ece += weight * abs(mean_conf - accuracy)
    return bins, ece


def expected_calibration_error(
    confidences: Sequence[float], correct: Sequence[bool], *, n_bins: int = 10
) -> float:
    """The scalar ECE — the accuracy-weighted gap between confidence and accuracy (§6.2)."""
    return reliability_and_ece(confidences, correct, n_bins=n_bins)[1]


def _clip(p: float) -> float:
    return min(max(p, _EPSILON), 1.0 - _EPSILON)


def negative_log_likelihood(
    confidences: Sequence[float], correct: Sequence[bool], temperature: float
) -> float:
    """Mean NLL of ``sigmoid(logit(confidence) / T)`` against binary correctness (§6.3).

    This is the objective :func:`fit_temperature` minimizes; exposed so the fit can be
    checked (a fitted ``T`` must beat the identity ``T == 1`` on a miscalibrated set).
    """
    if temperature <= 0.0:
        raise ValueError(f"temperature must be > 0: {temperature}")
    _validate(confidences, correct)
    if not confidences:
        return 0.0
    total = 0.0
    for c, y in zip(confidences, correct, strict=True):
        logit = math.log(_clip(c) / (1.0 - _clip(c)))
        p = _clip(1.0 / (1.0 + math.exp(-logit / temperature)))
        total += -(math.log(p) if y else math.log(1.0 - p))
    return total / len(confidences)


def fit_temperature(
    confidences: Sequence[float],
    correct: Sequence[bool],
    *,
    bounds: tuple[float, float] = (0.05, 10.0),
    tol: float = 1e-4,
) -> float:
    """Fit the scalar ``T > 0`` minimizing NLL (§6.3), via golden-section search.

    ``T > 1`` softens over-confident scores (the usual case); ``T < 1`` sharpens. Per the
    doc this is fit per model (ADR-008) on a held-out split; the search is deterministic.
    """
    _validate(confidences, correct)
    lo, hi = bounds
    if not 0.0 < lo < hi:
        raise ValueError(f"bounds must satisfy 0 < lo < hi: {bounds}")

    inv_phi = (math.sqrt(5.0) - 1.0) / 2.0  # 0.618…
    inv_phi2 = (3.0 - math.sqrt(5.0)) / 2.0  # 0.382…
    a, b = lo, hi
    c = a + inv_phi2 * (b - a)
    d = a + inv_phi * (b - a)
    fc = negative_log_likelihood(confidences, correct, c)
    fd = negative_log_likelihood(confidences, correct, d)
    while (b - a) > tol:
        if fc < fd:
            b, d, fd = d, c, fc
            c = a + inv_phi2 * (b - a)
            fc = negative_log_likelihood(confidences, correct, c)
        else:
            a, c, fc = c, d, fd
            d = a + inv_phi * (b - a)
            fd = negative_log_likelihood(confidences, correct, d)
    return (a + b) / 2.0

"""Confidence scoring + calibration (doc 04 §6, doc 06 §6).

Doc 04 §6 (*Confidence model & calibration*) defines how three concrete signals
are blended into a single **raw likelihood** — the Commander's structured
self-assessment, cross-specialist corroboration, and hypothesis separation
(``n_competing``) — and then mandates that this raw blend is *never* read by the
escalation policy directly: it must be mapped through a calibration function.

Doc 06 §6 (*Confidence calibration*) owns that map. Raw model likelihoods are
systematically over-confident, so the raw blend is passed through a **fitted
temperature-scaling** map ``sigmoid(logit(raw) / T)`` (doc 06 §6.3): ``T > 1``
softens over-confidence, ``T == 1`` is the identity. The fitted map is versioned
and stamped onto every assessment as ``calibrator_version`` so a past autonomous
decision is reproducible and a recalibration is auditable (doc 06 §6.3).

Banding (``LOW`` < 0.5 ≤ ``MEDIUM`` < 0.8 ≤ ``HIGH``, doc 00 §6) is applied via
:func:`sentinel.enums.band_for` *after* the calibration map — the map's job is to
make 0.5 and 0.8 mean 50% and 80% real accuracy (doc 06 §6.3).

This module is pure, offline, and stdlib-only (``math``); it imports the stable
core (:mod:`sentinel.enums`, :mod:`sentinel.contracts`) and never mutates it.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence

from sentinel.contracts import ConfidenceAssessment, Hypothesis, SpecialistFinding
from sentinel.enums import band_for

# Guards ``logit``/``sigmoid`` against the singularities at 0 and 1 (doc 06 §6.3
# clips probabilities to ``[1e-6, 1 - 1e-6]`` for the same reason).
_EPSILON = 1e-6

#: The bar a calibrator must clear before its bands may authorize autonomous (T3)
#: action: fitted on at least this many labelled incidents, with an expected
#: calibration error on held-out cases no worse than this (doc 06 §6.2).
MIN_CALIBRATION_CASES = 200
MAX_CALIBRATION_ECE = 0.05


#: Words that carry no diagnostic meaning when comparing causes.
_STOPWORDS = frozenset(
    {
        "the", "and", "that", "this", "with", "from", "into", "after", "before", "for", "was",
        "were", "has", "have", "had", "are", "its", "not", "but", "all", "any", "due", "via", "per",
        "our",
        "their", "than", "then", "them", "they", "which", "while", "when", "where", "caused",
        "causing", "cause", "causes", "likely", "possible", "probably", "root", "issue", "problem",
    }
)
_TERM = re.compile(r"[a-z0-9][a-z0-9_.+-]*")
#: Share of the shorter description's key terms that must be exceeded in the other.
_MIN_CAUSE_AGREEMENT = 0.5


def _stem(term: str) -> str:
    """Crude suffix stripping so 'exhausts' / 'exhausted' / 'exhausting' compare equal."""
    for suffix in ("ing", "ed", "es", "s"):
        if term.endswith(suffix) and len(term) - len(suffix) >= 4:
            return term[: -len(suffix)]
    return term


def _key_terms(text: str) -> set[str]:
    terms = (t.strip(".-") for t in _TERM.findall(text.lower()))
    return {_stem(t) for t in terms if len(t) >= 3 and t not in _STOPWORDS}


def cause_agrees(suspected_cause: str | None, statement: str) -> bool:
    """Whether a specialist's own suspected cause names the same thing as a hypothesis.

    A cheap, deterministic check: more than half of the shorter description's key
    terms (identifiers like ``abc123`` included) must appear in the other. It is
    deliberately strict — a missed match only lowers confidence and pages more; a
    false match would inflate it. A specialist that named no cause agrees with
    nothing.
    """
    if not suspected_cause:
        return False
    cause, claim = _key_terms(suspected_cause), _key_terms(statement)
    if not cause or not claim:
        return False
    return len(cause & claim) / min(len(cause), len(claim)) > _MIN_CAUSE_AGREEMENT


def independent_corroboration(
    hypothesis: Hypothesis, findings: Sequence[SpecialistFinding]
) -> int:
    """Count the *independent* corroboration behind ``hypothesis`` (doc 04 §6).

    :func:`blend_raw_likelihood` assumes each corroborating specialist is an
    independent signal. Two things must hold for a specialist to count:

    * **It agrees on its own.** The commander (an LLM) marks which roles support
      a hypothesis; that mark is only accepted when the specialist's own
      ``suspected_cause`` names the same thing (:func:`cause_agrees`). The
      commander's opinion of who agrees is not itself evidence.
    * **It is grounded.** Specialists that cite no evidence all reasoned over the
      same shared incident snapshot (typically the same model with a different
      role prompt), so their agreement is one correlated opinion, not N
      confirmations::

        agreeing    = commander-marked supporters whose own cause matches
        independent = |agreeing roles that cite their own evidence|
                    + (1 if any agreeing role cites none, else 0)
        corroboration = max(0, independent - |contradicting roles|)

    Contradictions are deliberately *not* discounted: dissent should cost
    confidence at full weight, and erring that way can only page more, never act
    more.

    Args:
        hypothesis: The leading hypothesis being scored.
        findings: Every specialist finding the hypothesis was synthesized from.

    Returns:
        The net independent corroboration count (``≥ 0``).
    """
    cause_of = {f.role: f.suspected_cause for f in findings}
    agreeing = {
        role
        for role in hypothesis.supporting_roles
        if cause_agrees(cause_of.get(role), hypothesis.statement)
    }
    evidenced = {f.role for f in findings if f.evidence}
    independent = len(agreeing & evidenced) + (1 if agreeing - evidenced else 0)
    return max(0, independent - len(set(hypothesis.contradicting_roles)))


def blend_raw_likelihood(
    self_assessment: float,
    corroboration_count: int,
    n_competing: int,
) -> float:
    """Blend the three doc 04 §6 signals into a raw likelihood in ``[0, 1]``.

    Per doc 04 §6, the raw likelihood starts from the Commander's structured
    ``self_assessment``, *increases* with independent cross-specialist
    ``corroboration_count`` ("corroboration is the single most predictive
    input"), and is *capped* by ``n_competing`` near-tied rival hypotheses
    ("two near-tied hypotheses cap confidence regardless of self-assessment").

    The formula is deliberately simple, documented, and monotonic::

        boosted = self_assessment + (1 - self_assessment) * (1 - 0.5 ** corroboration_count)
        raw     = boosted / (1 + n_competing)

    - The corroboration term closes the remaining gap to ``1.0`` with
      diminishing returns: ``corroboration_count == 0`` leaves the
      self-assessment unchanged; each additional independent corroboration adds
      a strictly smaller increment, and ``raw`` is non-decreasing in
      ``corroboration_count``.
    - The competition term is a multiplicative cap in ``(0, 1]``:
      ``n_competing == 0`` applies no penalty; more rivals shrink the score, and
      ``raw`` is non-increasing in ``n_competing``.

    Both factors lie in ``[0, 1]``, so the product is already in ``[0, 1]``; it
    is nonetheless explicitly clamped as a defensive guarantee.

    Args:
        self_assessment: The top hypothesis' raw likelihood, in ``[0, 1]``.
        corroboration_count: Independent supporting specialists (``≥ 0``), as
            computed by :func:`independent_corroboration`.
        n_competing: Rival hypotheses within the leader's likelihood margin
            (``≥ 0``).

    Returns:
        The raw likelihood in ``[0, 1]`` — the pre-calibration blend that
        :class:`Calibrator` then maps (doc 06 §6.1).

    Raises:
        ValueError: If ``self_assessment`` is outside ``[0, 1]`` or either count
            is negative.
    """
    if not 0.0 <= self_assessment <= 1.0:
        raise ValueError(f"self_assessment out of range [0, 1]: {self_assessment}")
    if corroboration_count < 0:
        raise ValueError(f"corroboration_count must be >= 0: {corroboration_count}")
    if n_competing < 0:
        raise ValueError(f"n_competing must be >= 0: {n_competing}")

    corroboration_gain = 1.0 - 0.5**corroboration_count
    boosted = self_assessment + (1.0 - self_assessment) * corroboration_gain
    raw = boosted / (1.0 + n_competing)
    return _clamp_unit(raw)


class Calibrator:
    """A versioned temperature-scaling calibration map (doc 06 §6.3).

    Maps a raw likelihood to a calibrated confidence via
    ``sigmoid(logit(raw) / T)``. ``T > 1`` softens over-confident raw scores
    (pulling them toward ``0.5``, the usual case), ``T == 1`` is approximately
    the identity, and ``T < 1`` sharpens. The fit procedure (minimizing NLL on a
    held-out split, per model) lives in doc 06 §6.3; this class only *applies* an
    already-chosen ``temperature`` and carries the ``version`` that gets stamped
    onto each :class:`~sentinel.contracts.ConfidenceAssessment` for auditability.
    """

    __slots__ = ("fitted_on", "holdout_ece", "temperature", "version")

    def __init__(
        self,
        temperature: float,
        version: str,
        *,
        fitted_on: int = 0,
        holdout_ece: float | None = None,
    ) -> None:
        """Construct a calibrator.

        Args:
            temperature: The scalar ``T`` of doc 06 §6.3; must be ``> 0``.
            version: The fitted-map identifier stamped as ``calibrator_version``.
            fitted_on: How many labelled incidents the fit used; ``0`` means the map
                was never fitted (a hand-picked ``T``).
            holdout_ece: Expected calibration error measured on held-out cases, if
                any were held out.

        Raises:
            ValueError: If ``temperature`` is not strictly positive or ``fitted_on``
                is negative.
        """
        if temperature <= 0.0:
            raise ValueError(f"temperature must be > 0: {temperature}")
        if fitted_on < 0:
            raise ValueError(f"fitted_on must be >= 0: {fitted_on}")
        self.temperature = temperature
        self.version = version
        self.fitted_on = fitted_on
        self.holdout_ece = holdout_ece

    def trusted_for_autonomy(
        self, *, min_cases: int = MIN_CALIBRATION_CASES, max_ece: float = MAX_CALIBRATION_ECE
    ) -> bool:
        """Whether this map's bands are evidenced enough to authorize T3 action.

        An unfitted map (``T`` chosen by hand), one fitted on too few incidents, or
        one never checked on held-out cases gives bands that only *look* calibrated —
        so HIGH from it must not move prod without a human.
        """
        return (
            self.fitted_on >= min_cases
            and self.holdout_ece is not None
            and self.holdout_ece <= max_ece
        )

    def to_json(self) -> str:
        """Serialize the fitted map — it is versioned configuration, not code."""
        return json.dumps(
            {
                "version": self.version,
                "temperature": self.temperature,
                "fitted_on": self.fitted_on,
                "holdout_ece": self.holdout_ece,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> Calibrator:
        """Load a map written by :meth:`to_json`."""
        data = json.loads(text)
        ece = data.get("holdout_ece")
        return cls(
            float(data["temperature"]),
            str(data["version"]),
            fitted_on=int(data.get("fitted_on", 0)),
            holdout_ece=float(ece) if ece is not None else None,
        )

    def apply(self, raw: float) -> float:
        """Apply temperature scaling to a raw likelihood (doc 06 §6.3).

        Computes ``sigmoid(logit(raw) / T)``. ``raw`` is clamped into
        ``(0, 1)`` by :data:`_EPSILON` so ``logit`` never hits its
        singularities at ``0`` or ``1``, mirroring doc 06 §6.3's probability
        clipping.

        Args:
            raw: The raw likelihood, in ``[0, 1]``.

        Returns:
            The calibrated confidence in ``(0, 1)``.
        """
        clamped = min(max(raw, _EPSILON), 1.0 - _EPSILON)
        logit = math.log(clamped / (1.0 - clamped))
        return _sigmoid(logit / self.temperature)


def build_assessment(
    *,
    incident_id: str,
    top_hypothesis_id: str,
    raw_likelihood: float,
    corroboration_count: int,
    n_competing: int,
    calibrator: Calibrator,
) -> ConfidenceAssessment:
    """Calibrate a raw likelihood and assemble a :class:`ConfidenceAssessment`.

    This is the assembly step doc 04 §6 describes: the raw blend is *mapped
    through the calibration function*, the canonical band cut (doc 00 §6) is
    applied to the calibrated number via :func:`sentinel.enums.band_for`, and
    the ``calibrator_version`` is stamped so the decision is reproducible
    (doc 06 §6.3). The returned model is the only confidence the §7 escalation
    policy may read (doc 04 §6).

    Args:
        incident_id: The incident the assessment belongs to.
        top_hypothesis_id: The leading hypothesis being scored.
        raw_likelihood: The pre-calibration blend (see
            :func:`blend_raw_likelihood`).
        corroboration_count: Independent supporting specialists (``≥ 0``),
            recorded on the assessment for transparency.
        n_competing: Rival hypotheses within the leader's margin (``≥ 0``),
            recorded on the assessment for transparency.
        calibrator: The fitted calibration map to apply.

    Returns:
        A frozen :class:`~sentinel.contracts.ConfidenceAssessment` whose
        ``band`` is ``band_for(calibrated_confidence)`` and whose
        ``calibrator_version`` is ``calibrator.version``.
    """
    calibrated = calibrator.apply(raw_likelihood)
    return ConfidenceAssessment(
        incident_id=incident_id,
        top_hypothesis_id=top_hypothesis_id,
        calibrated_confidence=calibrated,
        band=band_for(calibrated),
        corroboration_count=corroboration_count,
        n_competing=n_competing,
        calibrator_version=calibrator.version,
    )


def _sigmoid(x: float) -> float:
    """Numerically stable logistic sigmoid in ``(0, 1)``."""
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    exp_x = math.exp(x)
    return exp_x / (1.0 + exp_x)


def _clamp_unit(value: float) -> float:
    """Clamp ``value`` into the closed unit interval ``[0, 1]``."""
    return min(max(value, 0.0), 1.0)

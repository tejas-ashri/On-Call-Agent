import itertools

import pytest

from sentinel.contracts import EscalationDecision
from sentinel.enums import AutonomyTier, ConfidenceBand
from sentinel.escalation import PolicyInputs, decide_escalation

_OUTCOMES = {
    "act_and_report",
    "page_one_tap",
    "page_with_drafted_fix",
    "page_irreversible",
    "page_low_confidence",
}


def _decide(
    band: ConfidenceBand, tier: AutonomyTier, reversible: bool, n_competing: int = 1
) -> EscalationDecision:
    return decide_escalation(
        PolicyInputs(
            incident_id="inc_t",
            band=band,
            max_tier=tier,
            reversible=reversible,
            n_competing=n_competing,
        )
    )


@pytest.mark.parametrize(
    ("band", "tier", "reversible"),
    list(itertools.product(ConfidenceBand, AutonomyTier, [True, False])),
)
def test_matrix_is_total_and_only_t3_high_reversible_acts(
    band: ConfidenceBand, tier: AutonomyTier, reversible: bool
) -> None:
    decision = _decide(band, tier, reversible)
    # Totality: every input yields exactly one canonical outcome.
    assert decision.outcome in _OUTCOMES
    # Safety invariant (C1): the ONLY autonomous-write path is reversible + HIGH + T3.
    autonomous = decision.outcome == "act_and_report"
    assert autonomous == (
        reversible and band is ConfidenceBand.HIGH and tier is AutonomyTier.T3
    )


@pytest.mark.parametrize(
    ("band", "tier", "reversible", "outcome", "state"),
    [
        (ConfidenceBand.HIGH, AutonomyTier.T3, True, "act_and_report", "REMEDIATING"),
        (ConfidenceBand.MEDIUM, AutonomyTier.T3, True, "page_one_tap", "AWAITING_APPROVAL"),
        (ConfidenceBand.HIGH, AutonomyTier.T2, True, "page_one_tap", "AWAITING_APPROVAL"),
        (ConfidenceBand.MEDIUM, AutonomyTier.T2, True, "page_one_tap", "AWAITING_APPROVAL"),
        (ConfidenceBand.HIGH, AutonomyTier.T1, True, "page_with_drafted_fix", "ESCALATED"),
        (ConfidenceBand.HIGH, AutonomyTier.T0, True, "page_with_drafted_fix", "ESCALATED"),
        (ConfidenceBand.HIGH, AutonomyTier.T3, False, "page_irreversible", "ESCALATED"),
        (ConfidenceBand.LOW, AutonomyTier.T3, True, "page_low_confidence", "ESCALATED"),
    ],
)
def test_worked_rows_match_matrix(
    band: ConfidenceBand,
    tier: AutonomyTier,
    reversible: bool,
    outcome: str,
    state: str,
) -> None:
    decision = _decide(band, tier, reversible)
    assert decision.outcome == outcome
    assert decision.target_state == state


def test_irreversible_hard_stop_precedes_low_confidence() -> None:
    # LOW *and* irreversible: the irreversible stop is evaluated first and must win.
    decision = _decide(ConfidenceBand.LOW, AutonomyTier.T3, reversible=False)
    assert decision.outcome == "page_irreversible"


def test_decision_echoes_inputs_for_audit() -> None:
    decision = _decide(ConfidenceBand.HIGH, AutonomyTier.T3, reversible=True)
    assert decision.band is ConfidenceBand.HIGH
    assert decision.granted_tier is AutonomyTier.T3
    assert decision.reversible is True
    assert decision.rationale  # non-empty, auditable "why"

"""The graduated-escalation engine (doc 04 §7) — the product-defining logic.

The §00.6 escalation matrix as a **pure, deterministic, total** function of
three inputs: calibrated confidence band × granted autonomy tier × action
reversibility. Purity is a design requirement, not an accident — the diagnosis
is probabilistic, but the decision to touch prod is not: it is unit-testable
against the golden library (doc 06), trivially auditable, and immune to LLM
nondeterminism.

The ordering encodes the safety priority of C1: the two hard stops
(irreversible, LOW confidence) are evaluated BEFORE any tier check, so no
amount of granted autonomy can override them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sentinel.contracts.agents import EscalationDecision, EscalationOutcome
from sentinel.enums import AutonomyTier, ConfidenceBand

_TargetState = Literal["AWAITING_APPROVAL", "REMEDIATING", "ESCALATED"]


@dataclass(frozen=True)
class PolicyInputs:
    """The three axes of the §00.6 matrix, plus context for the rationale."""

    incident_id: str
    band: ConfidenceBand  # calibrated (P6), from ConfidenceAssessment
    max_tier: AutonomyTier  # from the AutonomyGrant for this (tenant × action_type)
    reversible: bool  # from RemediationPlan / ActionRequest
    n_competing: int = 0  # rival hypotheses within margin (for the rationale)


def decide_escalation(i: PolicyInputs) -> EscalationDecision:
    """Map ``(band, max_tier, reversible)`` to exactly one escalation outcome.

    Invariant (C1): the only autonomous-write path is ``reversible AND
    band==HIGH AND max_tier==T3``; every other input pages a human.
    """

    def out(outcome: EscalationOutcome, state: _TargetState, why: str) -> EscalationDecision:
        return EscalationDecision(
            incident_id=i.incident_id,
            outcome=outcome,
            target_state=state,
            band=i.band,
            granted_tier=i.max_tier,
            reversible=i.reversible,
            rationale=why,
        )

    # ---- Hard stop 1: irreversible actions ALWAYS page (even at T3). C1. ----
    if not i.reversible:
        return out(
            "page_irreversible",
            "ESCALATED",
            "Action is irreversible; policy forbids autonomous execution at any tier.",
        )

    # ---- Hard stop 2: LOW calibrated confidence ALWAYS pages. C1, P6. ----
    if i.band is ConfidenceBand.LOW:
        return out(
            "page_low_confidence",
            "ESCALATED",
            f"Calibrated confidence is LOW with {i.n_competing} competing hypotheses; "
            "presenting evidence to a human instead of acting.",
        )

    # ---- Tier ladder (only reachable for reversible + MEDIUM/HIGH). §00.6. ----
    if i.max_tier in (AutonomyTier.T0, AutonomyTier.T1):
        # Observe / Suggest: draft the fix, never execute.
        return out(
            "page_with_drafted_fix",
            "ESCALATED",
            f"Grant is {i.max_tier} (no execution authority); paging with drafted fix.",
        )

    if i.max_tier is AutonomyTier.T2:
        # Approve-to-act: one-tap human approval, fix pre-drafted.
        return out(
            "page_one_tap",
            "AWAITING_APPROVAL",
            "Grant is T2; reversible fix ready for one-tap approval.",
        )

    # max_tier == T3 (Autonomous), reversible, MEDIUM or HIGH.
    if i.band is ConfidenceBand.HIGH:
        return out(
            "act_and_report",
            "REMEDIATING",
            "T3 + HIGH calibrated confidence + reversible: acting autonomously, "
            "will verify and report.",
        )

    # T3 but only MEDIUM confidence → fall back to one-tap approval.
    return out(
        "page_one_tap",
        "AWAITING_APPROVAL",
        "T3 grant but confidence only MEDIUM; falling back to one-tap human approval.",
    )

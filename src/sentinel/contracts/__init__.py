"""Pydantic v2 domain and inter-agent contracts.

Re-exports the data shapes the rest of the system exchanges. Field names are
canonical per doc 00 §5 and doc 04 §4.
"""

from sentinel.contracts.actions import ActionRequest, ActionResult
from sentinel.contracts.agents import (
    ConfidenceAssessment,
    EscalationDecision,
    EscalationOutcome,
    EvidenceRef,
    Hypothesis,
    PagePayload,
    RemediationPlan,
    SpecialistFinding,
    SpecialistRole,
)
from sentinel.contracts.autonomy import AutonomyGrant
from sentinel.contracts.events import CanonicalEvent

__all__ = [
    "ActionRequest",
    "ActionResult",
    "AutonomyGrant",
    "CanonicalEvent",
    "ConfidenceAssessment",
    "EscalationDecision",
    "EscalationOutcome",
    "EvidenceRef",
    "Hypothesis",
    "PagePayload",
    "RemediationPlan",
    "SpecialistFinding",
    "SpecialistRole",
]

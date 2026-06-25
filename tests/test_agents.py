from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sentinel.contracts import (
    ConfidenceAssessment,
    EscalationDecision,
    EvidenceRef,
    PagePayload,
    RemediationPlan,
    SpecialistFinding,
)
from sentinel.enums import ConfidenceBand


def _now() -> datetime:
    return datetime(2026, 6, 13, tzinfo=UTC)


def test_specialist_finding_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValidationError):
        SpecialistFinding.model_validate(
            {"incident_id": "inc_x", "role": "log", "summary": "s", "self_confidence": 1.5}
        )


def test_specialist_finding_rejects_unknown_role() -> None:
    with pytest.raises(ValidationError):
        SpecialistFinding.model_validate(
            {"incident_id": "inc_x", "role": "psychic", "summary": "s"}
        )


def test_confidence_assessment_is_frozen() -> None:
    assessment = ConfidenceAssessment(
        incident_id="inc_x",
        top_hypothesis_id="h1",
        calibrated_confidence=0.9,
        band=ConfidenceBand.HIGH,
        corroboration_count=4,
        n_competing=0,
        calibrator_version="cal.v1",
    )
    with pytest.raises(ValidationError):
        assessment.__setattr__("calibrated_confidence", 0.1)


def test_escalation_decision_rejects_unknown_outcome() -> None:
    with pytest.raises(ValidationError):
        EscalationDecision.model_validate(
            {
                "incident_id": "inc_x",
                "outcome": "nuke_everything",
                "target_state": "ESCALATED",
                "band": "HIGH",
                "granted_tier": "T3",
                "reversible": True,
                "rationale": "no",
            }
        )


def test_page_payload_holds_the_four_mandated_things() -> None:
    assessment = ConfidenceAssessment(
        incident_id="inc_x",
        top_hypothesis_id="h1",
        calibrated_confidence=0.4,
        band=ConfidenceBand.LOW,
        corroboration_count=1,
        n_competing=2,
        calibrator_version="cal.v1",
    )
    plan = RemediationPlan(
        incident_id="inc_x",
        action_type="deploy.rollback",
        target="svc_api",
        reversible=True,
        summary="roll back abc123",
        estimated_blast_radius="single_service",
    )
    page = PagePayload(
        incident_id="inc_x",
        tenant_id="ten_x",
        suspected_root_cause="deploy abc123 exhausted the pool",
        confidence=assessment,
        evidence_chain=[
            EvidenceRef(kind="deploy", summary="abc123 40s before onset", observed_at=_now())
        ],
        competing_hypotheses=[],
        drafted_remediation=plan,
        outcome="page_with_drafted_fix",
    )
    # The §00.6 page contract: root cause, evidence chain, competing hypotheses, drafted fix.
    assert page.suspected_root_cause
    assert page.confidence.band is ConfidenceBand.LOW
    assert page.drafted_remediation is not None
    assert page.evidence_chain[0].kind == "deploy"

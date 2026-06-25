"""The incident-graph driver (doc 04 §2, §7, §11).

A runtime-agnostic realization of the LangGraph incident graph's control flow:
``intake → fan-out specialists → synthesize → score confidence → escalate →
plan → route (act | page)``. It wires the real escalation engine
(:mod:`sentinel.escalation`), confidence calibration (:mod:`sentinel.confidence`),
the independent reversibility floor (:mod:`sentinel.safety`), and — through the
:class:`~sentinel.orchestrator.protocols.ActionExecutor` seam — the credential
broker (doc 05). Every lifecycle transition is emitted as an
:class:`~sentinel.incidents.events.IncidentEvent`, so the run is event-sourced
(ADR-011): replaying ``run.events`` reproduces ``run.final_state``.

The agents, executor, and clock are injected, so the whole loop runs offline and
deterministically. In production the agents are LLM-backed and the fan-out is
concurrent; here specialists are awaited in sequence for deterministic ordering.
LangGraph (ADR-006) is the production runtime that adds durable checkpointing and
the HITL interrupt around this same control flow.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from sentinel.confidence import Calibrator, blend_raw_likelihood, build_assessment
from sentinel.contracts import (
    AutonomyGrant,
    CanonicalEvent,
    ConfidenceAssessment,
    EscalationDecision,
    EvidenceRef,
    Hypothesis,
    PagePayload,
    RemediationPlan,
    SpecialistFinding,
)
from sentinel.contracts.actions import ActionResult
from sentinel.correlator import IncidentDraft
from sentinel.enums import ActionStatus, ActionType, IncidentState
from sentinel.escalation import PolicyInputs, decide_escalation
from sentinel.incidents.events import IncidentEvent, IncidentEventType
from sentinel.orchestrator.protocols import (
    ActionExecutor,
    Commander,
    InvestigationContext,
    Planner,
    Specialist,
)
from sentinel.safety import classify_reversibility


class IncidentRun(BaseModel):
    """The full record of one incident-graph execution (doc 04 §11)."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    tenant_id: str
    final_state: IncidentState
    findings: list[SpecialistFinding]
    hypotheses: list[Hypothesis]
    events: list[IncidentEvent]
    assessment: ConfidenceAssessment | None = None
    decision: EscalationDecision | None = None
    plan: RemediationPlan | None = None
    page_payload: PagePayload | None = None
    action_result: ActionResult | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def run_incident(
    *,
    draft: IncidentDraft,
    events: Sequence[CanonicalEvent],
    specialists: Sequence[Specialist],
    commander: Commander,
    planner: Planner,
    executor: ActionExecutor,
    grant: AutonomyGrant,
    calibrator: Calibrator,
    competing_margin: float = 0.15,
    clock: Callable[[], datetime] = _utcnow,
) -> IncidentRun:
    """Run one incident end to end and return a fully-recorded :class:`IncidentRun`.

    The branch on ``decision.outcome`` is doc 00 §6's escalation matrix: only
    ``act_and_report`` touches prod (via ``executor``); every other outcome pages
    a human with the drafted fix attached and never acts (C1).
    """
    incident_id = draft.incident_id
    tenant_id = draft.tenant_id
    log: list[IncidentEvent] = []
    seq = 0

    def emit(
        etype: IncidentEventType, actor: str, payload: dict[str, object] | None = None
    ) -> None:
        nonlocal seq
        seq += 1
        log.append(
            IncidentEvent(
                incident_id=incident_id,
                tenant_id=tenant_id,
                seq=seq,
                type=etype,
                payload=payload or {},
                actor=actor,
                occurred_at=clock(),
            )
        )

    # intake → TRIAGING
    emit(
        IncidentEventType.INCIDENT_OPENED, "correlator", {"primary_service": draft.primary_service}
    )
    emit(IncidentEventType.TRIAGE_STARTED, "commander")

    # fan-out specialists (sequential here for determinism; concurrent in production)
    ctx = InvestigationContext(incident_id=incident_id, tenant_id=tenant_id, events=list(events))
    findings: list[SpecialistFinding] = [await s.investigate(ctx) for s in specialists]
    for finding in findings:
        emit(
            IncidentEventType.SPECIALIST_FINDING,
            f"specialist:{finding.role}",
            {"role": finding.role, "status": finding.status},
        )

    # synthesize
    hypotheses = await commander.synthesize(incident_id, findings)
    if not hypotheses:
        raise ValueError("commander returned no hypotheses to score")
    top = hypotheses[0]

    # score calibrated confidence (doc 04 §6)
    corroboration = max(0, len(top.supporting_roles) - len(top.contradicting_roles))
    n_competing = sum(
        1 for h in hypotheses[1:] if h.raw_likelihood >= top.raw_likelihood - competing_margin
    )
    raw = blend_raw_likelihood(top.raw_likelihood, corroboration, n_competing)
    assessment = build_assessment(
        incident_id=incident_id,
        top_hypothesis_id=top.hypothesis_id,
        raw_likelihood=raw,
        corroboration_count=corroboration,
        n_competing=n_competing,
        calibrator=calibrator,
    )
    emit(
        IncidentEventType.HYPOTHESIS_FORMED,
        "commander",
        {"root_cause": top.statement, "confidence_band": assessment.band.value},
    )
    emit(IncidentEventType.INCIDENT_DIAGNOSED, "commander")

    # plan + the independent reversibility floor (doc 05 §6) feeding the policy (doc 04 §7)
    plan = await planner.draft(incident_id, top)
    floor_reversible = classify_reversibility(ActionType(plan.action_type), plan.reversible)
    decision = decide_escalation(
        PolicyInputs(
            incident_id=incident_id,
            band=assessment.band,
            max_tier=grant.max_tier,
            reversible=floor_reversible,
            n_competing=n_competing,
        )
    )

    evidence_chain: list[EvidenceRef] = [ev for f in findings for ev in f.evidence]
    page_payload: PagePayload | None = None
    action_result: ActionResult | None = None
    final_state: IncidentState

    if decision.outcome == "act_and_report":
        emit(IncidentEventType.REMEDIATION_STARTED, "planner")
        action_result = await executor.execute(plan, tenant_id=tenant_id, incident_id=incident_id)
        emit(IncidentEventType.MONITORING_STARTED, "commander")
        if action_result.status is ActionStatus.SUCCEEDED and not action_result.rolled_back:
            emit(IncidentEventType.INCIDENT_RESOLVED, "commander")
            emit(IncidentEventType.INCIDENT_CLOSED, "system")
            final_state = IncidentState.CLOSED
        else:
            emit(
                IncidentEventType.INCIDENT_ESCALATED, "commander", {"reason": "remediation_failed"}
            )
            page_payload = _build_page(
                tenant_id, top, assessment, evidence_chain, hypotheses, plan, decision.outcome
            )
            final_state = IncidentState.ESCALATED
    else:
        page_payload = _build_page(
            tenant_id, top, assessment, evidence_chain, hypotheses, plan, decision.outcome
        )
        if decision.target_state == "AWAITING_APPROVAL":
            emit(IncidentEventType.APPROVAL_REQUESTED, "commander")
            final_state = IncidentState.AWAITING_APPROVAL
        else:
            emit(IncidentEventType.INCIDENT_ESCALATED, "commander", {"outcome": decision.outcome})
            final_state = IncidentState.ESCALATED

    return IncidentRun(
        incident_id=incident_id,
        tenant_id=tenant_id,
        final_state=final_state,
        findings=findings,
        hypotheses=list(hypotheses),
        events=log,
        assessment=assessment,
        decision=decision,
        plan=plan,
        page_payload=page_payload,
        action_result=action_result,
    )


def _build_page(
    tenant_id: str,
    top: Hypothesis,
    assessment: ConfidenceAssessment,
    evidence_chain: list[EvidenceRef],
    hypotheses: Sequence[Hypothesis],
    plan: RemediationPlan,
    outcome: str,
) -> PagePayload:
    """The §00.6 page contract: root cause, evidence chain, rivals, drafted fix."""
    return PagePayload(
        incident_id=assessment.incident_id,
        tenant_id=tenant_id,
        suspected_root_cause=top.statement,
        confidence=assessment,
        evidence_chain=evidence_chain,
        competing_hypotheses=list(hypotheses[1:]),
        drafted_remediation=plan,
        outcome=outcome,
    )

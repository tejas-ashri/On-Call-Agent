"""Fail-safe wrappers around the injected agents and executor (C1: an outage must page).

An LLM timeout, a rate limit, or a malformed reply must never abort an incident
silently — that would drop the page exactly when it matters. Each wrapper turns a
failure into the most conservative result the rest of the flow already handles:

* a failed specialist → a ``degraded`` finding with no evidence (it corroborates nothing);
* a failed or empty synthesis → one unsupported, zero-likelihood hypothesis (LOW → page);
* a failed planner → no plan, and a page without a drafted fix;
* a failed executor → a ``FAILED`` result, which escalates with the fix attached;
* a repeated action (crash/retry/duplicate approval) → the recorded result, or a
  ``FAILED`` "outcome unknown" result — never a second write (:func:`execute_once`).

Both orchestrators (:mod:`sentinel.orchestrator.run` and :mod:`sentinel.graph`)
use these, so they degrade identically.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

from sentinel.contracts import (
    CanonicalEvent,
    ConfidenceAssessment,
    EscalationDecision,
    Hypothesis,
    RemediationPlan,
    SpecialistFinding,
)
from sentinel.contracts.actions import ActionResult
from sentinel.enums import ActionStatus, ActionType, AutonomyTier
from sentinel.orchestrator.ledger import ActionLedger
from sentinel.orchestrator.protocols import (
    ActionExecutor,
    Commander,
    InvestigationContext,
    Planner,
    Specialist,
)

UNDETERMINED = "Root cause undetermined: automated diagnosis failed; investigate manually."


async def safe_investigate(specialist: Specialist, ctx: InvestigationContext) -> SpecialistFinding:
    """Run one specialist; a failure becomes a ``degraded`` finding instead of an abort."""
    try:
        return await specialist.investigate(ctx)
    except Exception as exc:
        return SpecialistFinding(
            incident_id=ctx.incident_id,
            role=specialist.role,
            status="degraded",
            summary=f"{specialist.role} specialist failed: {type(exc).__name__}",
        )


async def safe_synthesize(
    commander: Commander, incident_id: str, findings: Sequence[SpecialistFinding]
) -> list[Hypothesis]:
    """Synthesize hypotheses; failure or an empty result yields one unsupported hypothesis."""
    try:
        hypotheses = await commander.synthesize(incident_id, findings)
    except Exception:
        hypotheses = []
    return list(hypotheses) or [Hypothesis(hypothesis_id="h1", statement=UNDETERMINED)]


async def safe_draft(
    planner: Planner,
    incident_id: str,
    hypothesis: Hypothesis,
    events: Sequence[CanonicalEvent],
) -> RemediationPlan | None:
    """Draft a plan, or ``None`` if the planner fails or names an unknown action type."""
    try:
        plan = await planner.draft(incident_id, hypothesis, events)
        ActionType(plan.action_type)
    except Exception:
        return None
    return plan


def no_plan_decision(
    incident_id: str, assessment: ConfidenceAssessment, tier: AutonomyTier
) -> EscalationDecision:
    """The decision when no valid plan exists: page a human, never act."""
    return EscalationDecision(
        incident_id=incident_id,
        outcome="page_with_drafted_fix",
        target_state="ESCALATED",
        band=assessment.band,
        granted_tier=tier,
        reversible=False,
        rationale="Planner produced no valid remediation; paging without a drafted fix.",
    )


async def safe_execute(
    executor: ActionExecutor,
    plan: RemediationPlan,
    *,
    tenant_id: str,
    incident_id: str,
    action_id: str,
) -> ActionResult:
    """Execute a plan; an exception becomes a ``FAILED`` result (which escalates)."""
    try:
        return await executor.execute(
            plan, tenant_id=tenant_id, incident_id=incident_id, action_id=action_id
        )
    except Exception as exc:
        return ActionResult(
            action_id=action_id,
            status=ActionStatus.FAILED,
            detail=f"executor raised {type(exc).__name__}",
        )


async def execute_once(
    executor: ActionExecutor,
    ledger: ActionLedger,
    plan: RemediationPlan,
    *,
    tenant_id: str,
    incident_id: str,
    action_id: str,
) -> ActionResult:
    """Execute ``plan`` at most once per ``action_id`` (see :mod:`sentinel.orchestrator.ledger`).

    A re-run of an action that already completed returns the recorded result
    without touching prod. A re-run of one that started but never recorded a
    result — or any ledger failure — is a ``FAILED`` result: the write may or may
    not have happened, so a human must verify. Never a second attempt.
    """
    try:
        claimed = await ledger.claim(action_id)
        prior = None if claimed else await ledger.result(action_id)
    except Exception as exc:
        return ActionResult(
            action_id=action_id,
            status=ActionStatus.FAILED,
            detail=f"action ledger unavailable ({type(exc).__name__}); not executed",
        )
    if not claimed:
        if prior is not None:
            return prior
        return ActionResult(
            action_id=action_id,
            status=ActionStatus.FAILED,
            detail="a previous attempt started but recorded no result; outcome unknown, "
            "verify manually",
        )
    result = await safe_execute(
        executor, plan, tenant_id=tenant_id, incident_id=incident_id, action_id=action_id
    )
    # Best effort: if this write fails, a re-run sees "outcome unknown" and escalates.
    with contextlib.suppress(Exception):
        await ledger.complete(action_id, result)
    return result

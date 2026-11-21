"""Incident-graph node factories (doc 04 §2, §3, §7).

Each node is built by a factory that closes over an injected :class:`_Deps`, so
the graph carries no ambient authority and is fully testable offline (doc 04 §1).
Nodes return *partial* state updates; LangGraph merges them. The control flow is
identical to :func:`sentinel.orchestrator.run.run_incident` — this module is the
durable, interruptible realization of that same logic.

The specialist nodes deliberately emit **no** lifecycle events (only findings),
so every ``incident_events`` row is produced by a sequential node and ``seq``
stays gap-free — see :mod:`sentinel.graph.state`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from langgraph.types import interrupt

from sentinel.confidence import (
    Calibrator,
    blend_raw_likelihood,
    build_assessment,
    independent_corroboration,
)
from sentinel.contracts import Hypothesis, PagePayload, RemediationPlan
from sentinel.enums import ActionStatus, ActionType, AutonomyTier, IncidentState
from sentinel.escalation import (
    GrantLookup,
    PolicyInputs,
    decide_escalation,
    effective_tier,
    resolve_grant,
)
from sentinel.graph.state import IncidentGraphState
from sentinel.incidents.events import IncidentEvent, IncidentEventType
from sentinel.notify import Notifier
from sentinel.orchestrator.guard import (
    execute_once,
    no_plan_decision,
    safe_draft,
    safe_investigate,
    safe_synthesize,
)
from sentinel.orchestrator.ledger import ActionLedger, action_id_for
from sentinel.orchestrator.protocols import (
    ActionExecutor,
    Commander,
    InvestigationContext,
    Planner,
    Specialist,
)
from sentinel.safety import classify_reversibility

# Canonical specialist ordering, so SPECIALIST_FINDING events are emitted (and
# thus seq-numbered) deterministically regardless of concurrent completion order.
_ROLE_ORDER: dict[str, int] = {
    "log": 0,
    "metric": 1,
    "trace": 2,
    "change_deploy": 3,
    "error": 4,
}

Node = Callable[[IncidentGraphState], Awaitable[dict[str, object]]]

_TIER_RANK = {tier: rank for rank, tier in enumerate(AutonomyTier)}


class AuditSink(Protocol):
    """Records who decided what (doc 05 §5); :class:`sentinel.audit.AuditLog` conforms."""

    def append(
        self,
        *,
        tenant_id: str,
        actor: str,
        action: str,
        at: datetime,
        target: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> object: ...


@dataclass(frozen=True)
class _Deps:
    """The injected ports the graph runs against (doc 04 §3)."""

    commander: Commander
    planner: Planner
    executor: ActionExecutor
    grants: GrantLookup
    ledger: ActionLedger
    calibrator: Calibrator
    competing_margin: float
    clock: Callable[[], datetime]
    notifier: Notifier | None = None
    cost_breaker_open: Callable[[str], bool] | None = None
    approval_ttl: timedelta | None = None
    audit: AuditSink | None = None
    approvers: Callable[[str, str], bool] | None = None
    # False unless the calibrator is fitted and validated (see Calibrator.trusted_for_autonomy):
    # until then a HIGH band is not evidence enough to act without a human.
    autonomy_calibrated: bool = False

    def tier_for(self, tenant_id: str, action_type: str) -> tuple[AutonomyTier, str | None]:
        """The effective tier for one decision, and the id of the grant behind it.

        T3 is capped at T2 (act only after approval) while the calibrator is not
        trusted for autonomy.
        """
        grant = resolve_grant(self.grants, tenant_id=tenant_id, action_type=action_type)
        tier = effective_tier(
            grant,
            tenant_id=tenant_id,
            action_type=action_type,
            breaker_open=self.breaker_open(tenant_id),
        )
        if tier is AutonomyTier.T3 and not self.autonomy_calibrated:
            tier = AutonomyTier.T2
        return tier, grant.grant_id if grant is not None else None

    def approver_allowed(self, tenant_id: str, approver: str) -> bool:
        """Whether ``approver`` may approve actions for the tenant.

        With no approver directory configured nobody can — an approval nobody can
        attribute must not move prod (fail closed, C1). A failing directory denies.
        """
        if self.approvers is None:
            return False
        try:
            return bool(self.approvers(tenant_id, approver))
        except Exception:
            return False

    def breaker_open(self, tenant_id: str) -> bool:
        """Whether the tenant's cost breaker is open; an unreadable breaker counts as open."""
        if self.cost_breaker_open is None:
            return False
        try:
            return bool(self.cost_breaker_open(tenant_id))
        except Exception:
            return True

    async def notify(self, page: PagePayload) -> None:
        if self.notifier is not None:
            await self.notifier.page(page)

    def record(
        self,
        state: IncidentGraphState,
        actor: str,
        action: str,
        target: str | None,
        detail: Mapping[str, object],
    ) -> None:
        if self.audit is not None:
            self.audit.append(
                tenant_id=state["draft"].tenant_id,
                actor=actor,
                action=action,
                at=self.clock(),
                target=target,
                detail={"incident_id": state["draft"].incident_id, **detail},
            )


class _Emitter:
    """Accumulates lifecycle events within one node, advancing ``seq`` gap-free."""

    def __init__(self, state: IncidentGraphState, clock: Callable[[], datetime]) -> None:
        draft = state["draft"]
        self._incident_id = draft.incident_id
        self._tenant_id = draft.tenant_id
        self._clock = clock
        self._seq = state["seq"]
        self._log: list[IncidentEvent] = []

    def emit(
        self, etype: IncidentEventType, actor: str, payload: dict[str, object] | None = None
    ) -> None:
        self._seq += 1
        self._log.append(
            IncidentEvent(
                incident_id=self._incident_id,
                tenant_id=self._tenant_id,
                seq=self._seq,
                type=etype,
                payload=payload or {},
                actor=actor,
                occurred_at=self._clock(),
            )
        )

    def update(self, **fields: object) -> dict[str, object]:
        """Return the partial-state update for this node (events + new seq + fields)."""
        return {"event_log": self._log, "seq": self._seq, **fields}


def _build_page(
    state: IncidentGraphState,
    top: Hypothesis,
    plan: RemediationPlan | None,
    outcome: str,
    one_tap_action_id: str | None = None,
) -> PagePayload:
    """The doc 00 §6 page contract: root cause, evidence chain, rivals, drafted fix."""
    assessment = state["assessment"]
    assert assessment is not None  # set by the score node, which always precedes a page
    evidence_chain = [ev for f in state["findings"] for ev in f.evidence]
    return PagePayload(
        incident_id=assessment.incident_id,
        tenant_id=state["draft"].tenant_id,
        suspected_root_cause=top.statement,
        confidence=assessment,
        evidence_chain=evidence_chain,
        competing_hypotheses=list(state["hypotheses"][1:]),
        drafted_remediation=plan,
        one_tap_action_id=one_tap_action_id,
        outcome=outcome,
    )


def _parse_approval(payload: object, action_id: str) -> tuple[str | None, str | None]:
    """Interpret the human's resume value: ``(approver, denial_reason)``.

    An approval is ``{"approved": True, "action_id": <the page's one_tap_action_id>,
    "approver": <who>}``. Anything else denies (fail closed, C1): a bare ``True``,
    ``"yes"`` or ``1``; an approval of a *different* action (stale or replayed tap);
    or one that names no approver. Authorization of the approver is checked by the
    caller.
    """
    if not isinstance(payload, dict):
        return None, "denied"
    approver = payload.get("approver")
    approver = approver if isinstance(approver, str) and approver else None
    if payload.get("approved") is not True:
        return approver, "denied"
    if payload.get("action_id") != action_id:
        return approver, "action_mismatch"
    if approver is None:
        return None, "no_approver"
    return approver, None


def make_intake(deps: _Deps) -> Node:
    async def intake(state: IncidentGraphState) -> dict[str, object]:
        em = _Emitter(state, deps.clock)
        em.emit(
            IncidentEventType.INCIDENT_OPENED,
            "correlator",
            {"primary_service": state["draft"].primary_service},
        )
        em.emit(IncidentEventType.TRIAGE_STARTED, "commander")
        return em.update()

    return intake


def make_specialist(specialist: Specialist) -> Node:
    async def node(state: IncidentGraphState) -> dict[str, object]:
        draft = state["draft"]
        ctx = InvestigationContext(
            incident_id=draft.incident_id,
            tenant_id=draft.tenant_id,
            events=list(state["signal_events"]),
        )
        finding = await safe_investigate(specialist, ctx)
        return {"findings": [finding]}  # findings-only: never advances seq

    return node


def make_collect(deps: _Deps) -> Node:
    async def collect(state: IncidentGraphState) -> dict[str, object]:
        em = _Emitter(state, deps.clock)
        for finding in sorted(state["findings"], key=lambda f: _ROLE_ORDER.get(f.role, 99)):
            em.emit(
                IncidentEventType.SPECIALIST_FINDING,
                f"specialist:{finding.role}",
                {"role": finding.role, "status": finding.status},
            )
        return em.update()

    return collect


def make_synthesize(deps: _Deps) -> Node:
    async def synthesize(state: IncidentGraphState) -> dict[str, object]:
        hypotheses = await safe_synthesize(
            deps.commander, state["draft"].incident_id, list(state["findings"])
        )
        return {"hypotheses": hypotheses}

    return synthesize


def make_score(deps: _Deps) -> Node:
    async def score(state: IncidentGraphState) -> dict[str, object]:
        em = _Emitter(state, deps.clock)
        hypotheses = state["hypotheses"]
        top = hypotheses[0]
        corroboration = independent_corroboration(top, state["findings"])
        n_competing = sum(
            1
            for h in hypotheses[1:]
            if h.raw_likelihood >= top.raw_likelihood - deps.competing_margin
        )
        raw = blend_raw_likelihood(top.raw_likelihood, corroboration, n_competing)
        assessment = build_assessment(
            incident_id=state["draft"].incident_id,
            top_hypothesis_id=top.hypothesis_id,
            raw_likelihood=raw,
            corroboration_count=corroboration,
            n_competing=n_competing,
            calibrator=deps.calibrator,
        )
        em.emit(
            IncidentEventType.HYPOTHESIS_FORMED,
            "commander",
            {"root_cause": top.statement, "confidence_band": assessment.band.value},
        )
        em.emit(IncidentEventType.INCIDENT_DIAGNOSED, "commander")
        return em.update(assessment=assessment, n_competing=n_competing)

    return score


def make_plan_and_decide(deps: _Deps) -> Node:
    async def plan_and_decide(state: IncidentGraphState) -> dict[str, object]:
        em = _Emitter(state, deps.clock)
        draft = state["draft"]
        top = state["hypotheses"][0]
        plan = await safe_draft(deps.planner, draft.incident_id, top, state["signal_events"])
        assessment = state["assessment"]
        assert assessment is not None
        # The tenant's grant for *this* action type applies while active and while the
        # tenant is under its cost ceiling; no grant (or no plan) confers T0.
        tier, grant_id = deps.tier_for(
            draft.tenant_id, plan.action_type if plan is not None else ""
        )
        action_id = action_id_for(draft.incident_id, plan) if plan is not None else None
        if plan is None:
            decision = no_plan_decision(draft.incident_id, assessment, tier)
        else:
            # The independent reversibility floor (doc 05 §6) can only *downgrade* the
            # planner's claim — db.failover stays irreversible even if the planner lies.
            floor_reversible = classify_reversibility(
                ActionType(plan.action_type), plan.reversible
            )
            decision = decide_escalation(
                PolicyInputs(
                    incident_id=draft.incident_id,
                    band=assessment.band,
                    max_tier=tier,
                    reversible=floor_reversible,
                    n_competing=state["n_competing"],
                )
            )
        deps.record(
            state,
            "commander",
            "incident.decided",
            plan.target if plan is not None else None,
            {
                "outcome": decision.outcome,
                "granted_tier": tier.value,
                "grant_id": grant_id,
                "action_type": plan.action_type if plan is not None else None,
                "action_id": action_id,
            },
        )
        if decision.outcome == "act_and_report":
            return {"plan": plan, "decision": decision, "action_id": action_id}  # → act

        awaiting = decision.target_state == "AWAITING_APPROVAL"
        # The one-tap token is the action id: an approval must echo it (see await_approval).
        page = _build_page(state, top, plan, decision.outcome, action_id if awaiting else None)
        if awaiting:
            # APPROVAL_REQUESTED is emitted *here* (runs once, before the suspend),
            # so projecting the log while paused already reads AWAITING_APPROVAL.
            em.emit(IncidentEventType.APPROVAL_REQUESTED, "commander")
            await deps.notify(page)
            return em.update(
                plan=plan,
                decision=decision,
                action_id=action_id,
                page_payload=page,
                approval_requested_at=deps.clock(),
            )
        em.emit(IncidentEventType.INCIDENT_ESCALATED, "commander", {"outcome": decision.outcome})
        await deps.notify(page)
        return em.update(
            plan=plan,
            decision=decision,
            action_id=action_id,
            page_payload=page,
            final_state=IncidentState.ESCALATED,
        )

    return plan_and_decide


def make_await_approval(deps: _Deps) -> Node:
    async def await_approval(state: IncidentGraphState) -> dict[str, object]:
        plan = state["plan"]
        action_id = state.get("action_id")
        assert plan is not None and action_id is not None
        tenant_id = state["draft"].tenant_id
        # SUSPEND. interrupt() is the first effect in this node: on resume LangGraph
        # re-runs the node from the top, so nothing emitted before it may run twice.
        payload = interrupt(
            {
                "reason": "approval_required",
                "incident_id": state["draft"].incident_id,
                "action_id": action_id,
                "action_type": plan.action_type,
                "target": plan.target,
            }
        )
        em = _Emitter(state, deps.clock)
        approver, reason = _parse_approval(payload, action_id)
        if reason is None and approver is not None and not deps.approver_allowed(
            tenant_id, approver
        ):
            reason = "unauthorized_approver"
        requested_at = state.get("approval_requested_at")
        if (
            reason is None
            and deps.approval_ttl is not None
            and requested_at is not None
            and deps.clock() - requested_at > deps.approval_ttl
        ):
            reason = "expired"
        actor = f"human:{approver}" if approver else "human"
        if reason is None:
            em.emit(IncidentEventType.APPROVAL_GRANTED, actor, {"approver": approver})
            deps.record(state, actor, "approval.granted", plan.target, {"action_id": action_id})
            return em.update(approval=True)  # → act
        em.emit(IncidentEventType.APPROVAL_DENIED, actor, {"reason": reason})
        deps.record(
            state, actor, "approval.denied", plan.target, {"reason": reason, "action_id": action_id}
        )
        return em.update(approval=False, final_state=IncidentState.ESCALATED)

    return await_approval


def make_act(deps: _Deps) -> Node:
    async def act(state: IncidentGraphState) -> dict[str, object]:
        em = _Emitter(state, deps.clock)
        draft = state["draft"]
        plan = state["plan"]
        decision = state["decision"]
        action_id = state.get("action_id")
        assert plan is not None and decision is not None and action_id is not None
        # Re-checked here: while awaiting approval, spend may have crossed the ceiling
        # or the tenant may have revoked or lowered the grant.
        tier, _ = deps.tier_for(draft.tenant_id, plan.action_type)
        required = AutonomyTier.T3 if decision.outcome == "act_and_report" else AutonomyTier.T2
        blocked = (
            "cost_breaker_open"
            if deps.breaker_open(draft.tenant_id)
            else "grant_no_longer_permits"
            if _TIER_RANK[tier] < _TIER_RANK[required]
            else None
        )
        if blocked is not None:
            em.emit(IncidentEventType.INCIDENT_ESCALATED, "commander", {"reason": blocked})
            page = _build_page(state, state["hypotheses"][0], plan, decision.outcome)
            await deps.notify(page)
            return em.update(page_payload=page, final_state=IncidentState.ESCALATED)
        em.emit(IncidentEventType.REMEDIATION_STARTED, "planner")
        result = await execute_once(
            deps.executor,
            deps.ledger,
            plan,
            tenant_id=draft.tenant_id,
            incident_id=draft.incident_id,
            action_id=action_id,
        )
        deps.record(
            state,
            "planner",
            "action.executed",
            plan.target,
            {
                "action_type": plan.action_type,
                "action_id": action_id,
                "status": result.status.value,
            },
        )
        em.emit(IncidentEventType.MONITORING_STARTED, "commander")
        if result.status is ActionStatus.SUCCEEDED and not result.rolled_back:
            em.emit(IncidentEventType.INCIDENT_RESOLVED, "commander")
            em.emit(IncidentEventType.INCIDENT_CLOSED, "system")
            return em.update(action_result=result, final_state=IncidentState.CLOSED)
        # remediation failed or auto-rolled-back → escalate with the fix attached (C1)
        em.emit(IncidentEventType.INCIDENT_ESCALATED, "commander", {"reason": "remediation_failed"})
        page = _build_page(state, state["hypotheses"][0], plan, decision.outcome)
        await deps.notify(page)
        return em.update(
            action_result=result, page_payload=page, final_state=IncidentState.ESCALATED
        )

    return act

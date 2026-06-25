"""End-to-end orchestrator tests (doc 04 §2, §7, §11).

Drives the incident graph with deterministic fakes for the pluggable agents, and
one test wires the *real* MCP credential broker (doc 05 §2) through the executor
seam to prove the modules compose. No real LLM/infra; everything is offline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sentinel.confidence import Calibrator
from sentinel.contracts import (
    AutonomyGrant,
    CanonicalEvent,
    EvidenceRef,
    Hypothesis,
    RemediationPlan,
    SpecialistFinding,
    SpecialistRole,
)
from sentinel.contracts.actions import ActionResult
from sentinel.correlator import IncidentDraft
from sentinel.enums import (
    ActionStatus,
    ActionType,
    AutonomyTier,
    ConfidenceBand,
    EventKind,
    IncidentState,
    Severity,
)
from sentinel.incidents.projection import project
from sentinel.mcp.broker import Credential, MCPRouter, ToolBroker, ToolResult
from sentinel.orchestrator import (
    ActionExecutor,
    Commander,
    IncidentRun,
    InvestigationContext,
    Planner,
    Specialist,
    run_incident,
)

_T = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- builders
def _event() -> CanonicalEvent:
    return CanonicalEvent(
        event_id="evt_1",
        tenant_id="ten_a",
        integration_id="int_1",
        source="sentry",
        kind=EventKind.ERROR,
        service="svc_api",
        severity=Severity.ERROR,
        title="latency spike",
        fingerprint="fp1",
        labels={"commit_sha": "abc123"},
        occurred_at=_T,
        received_at=_T,
    )


def _draft() -> IncidentDraft:
    return IncidentDraft(
        incident_id="inc_1",
        tenant_id="ten_a",
        event_ids=["evt_1"],
        primary_service="svc_api",
        fingerprints=("fp1",),
        opened_at=_T,
    )


def _finding(role: SpecialistRole) -> SpecialistFinding:
    return SpecialistFinding(
        incident_id="inc_1",
        role=role,
        status="ok",
        summary=f"{role} corroborates deploy abc123",
        suspected_cause="deploy abc123",
        self_confidence=0.9,
        evidence=[EvidenceRef(kind="deploy", summary="abc123 40s before onset", observed_at=_T)],
    )


def _hypothesis(
    *, hid: str = "h1", likelihood: float = 0.95, supporting: list[SpecialistRole] | None = None
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hid,
        statement="deploy abc123 introduced an N+1 that exhausted the DB pool",
        supporting_roles=(
            supporting if supporting is not None else ["change_deploy", "metric", "trace"]
        ),
        raw_likelihood=likelihood,
        evidence=[EvidenceRef(kind="deploy", summary="abc123", observed_at=_T)],
    )


def _plan(*, action: str = "deploy.rollback", reversible: bool = True) -> RemediationPlan:
    return RemediationPlan(
        incident_id="inc_1",
        action_type=action,
        target="svc_api",
        reversible=reversible,
        summary="roll back to the prior release",
        rollback_ref="rel_prev",
        estimated_blast_radius="single_service",
    )


def _grant(tier: AutonomyTier) -> AutonomyGrant:
    return AutonomyGrant(
        grant_id="grant_1",
        tenant_id="ten_a",
        action_type=ActionType.DEPLOY_ROLLBACK,
        max_tier=tier,
        granted_by="human:1",
        granted_at=_T,
    )


# --------------------------------------------------------------------------- fakes
class FakeSpecialist:
    def __init__(self, role: SpecialistRole) -> None:
        self.role = role

    async def investigate(self, ctx: InvestigationContext) -> SpecialistFinding:
        return _finding(self.role)


class FakeCommander:
    def __init__(self, hypotheses: list[Hypothesis]) -> None:
        self._hypotheses = hypotheses

    async def synthesize(
        self, incident_id: str, findings: Sequence[SpecialistFinding]
    ) -> list[Hypothesis]:
        return self._hypotheses


class FakePlanner:
    def __init__(self, plan: RemediationPlan) -> None:
        self._plan = plan

    async def draft(self, incident_id: str, hypothesis: Hypothesis) -> RemediationPlan:
        return self._plan


class RecordingExecutor:
    def __init__(self, result: ActionResult) -> None:
        self._result = result
        self.calls: list[RemediationPlan] = []

    async def execute(
        self, plan: RemediationPlan, *, tenant_id: str, incident_id: str
    ) -> ActionResult:
        self.calls.append(plan)
        return self._result


def _run(
    *,
    grant_tier: AutonomyTier,
    hypotheses: list[Hypothesis] | None = None,
    plan: RemediationPlan | None = None,
    executor: ActionExecutor | None = None,
) -> tuple[IncidentRun, RecordingExecutor]:
    rec = RecordingExecutor(ActionResult(action_id="act_1", status=ActionStatus.SUCCEEDED))
    ex: ActionExecutor = executor if executor is not None else rec
    specialists: list[Specialist] = [
        FakeSpecialist("change_deploy"),
        FakeSpecialist("metric"),
        FakeSpecialist("trace"),
    ]
    commander: Commander = FakeCommander(hypotheses if hypotheses is not None else [_hypothesis()])
    planner: Planner = FakePlanner(plan if plan is not None else _plan())
    run = asyncio.run(
        run_incident(
            draft=_draft(),
            events=[_event()],
            specialists=specialists,
            commander=commander,
            planner=planner,
            executor=ex,
            grant=_grant(grant_tier),
            calibrator=Calibrator(temperature=1.0, version="cal.test"),
            clock=lambda: _T,
        )
    )
    return run, rec


# --------------------------------------------------------------------------- tests
def test_autonomous_happy_path_acts_and_closes() -> None:
    run, rec = _run(grant_tier=AutonomyTier.T3)
    assert run.decision is not None and run.decision.outcome == "act_and_report"
    assert len(rec.calls) == 1  # the executor (broker seam) was invoked exactly once
    assert run.action_result is not None and run.action_result.status == ActionStatus.SUCCEEDED
    assert run.page_payload is None
    assert run.final_state is IncidentState.CLOSED
    # event-sourcing integration: replaying the emitted log reproduces the final state.
    assert run.assessment is not None
    projection = project(run.events)
    assert projection.state is IncidentState.CLOSED
    assert projection.confidence_band is run.assessment.band
    assert projection.root_cause == run.hypotheses[0].statement


def test_low_grant_pages_with_drafted_fix() -> None:
    run, rec = _run(grant_tier=AutonomyTier.T1)
    assert run.decision is not None and run.decision.outcome == "page_with_drafted_fix"
    assert rec.calls == []  # never acted
    assert run.page_payload is not None
    assert run.page_payload.drafted_remediation is not None
    assert run.page_payload.suspected_root_cause == run.hypotheses[0].statement
    assert run.final_state is IncidentState.ESCALATED
    assert project(run.events).state is IncidentState.ESCALATED


def test_irreversible_action_pages_even_at_t3() -> None:
    # Planner claims reversible=True, but db.failover is hard-pinned by the floor.
    run, rec = _run(grant_tier=AutonomyTier.T3, plan=_plan(action="db.failover", reversible=True))
    assert run.decision is not None and run.decision.outcome == "page_irreversible"
    assert rec.calls == []
    assert run.final_state is IncidentState.ESCALATED


def test_low_confidence_pages_even_at_t3() -> None:
    # Low self-assessment AND no corroboration → calibrated LOW.
    run, rec = _run(
        grant_tier=AutonomyTier.T3, hypotheses=[_hypothesis(likelihood=0.2, supporting=[])]
    )
    assert run.assessment is not None and run.assessment.band is ConfidenceBand.LOW
    assert run.decision is not None and run.decision.outcome == "page_low_confidence"
    assert rec.calls == []


def test_t2_requests_one_tap_approval() -> None:
    run, rec = _run(grant_tier=AutonomyTier.T2)
    assert run.decision is not None and run.decision.outcome == "page_one_tap"
    assert rec.calls == []
    assert run.final_state is IncidentState.AWAITING_APPROVAL
    assert project(run.events).state is IncidentState.AWAITING_APPROVAL


def test_competing_hypotheses_attached_to_page() -> None:
    run, _ = _run(
        grant_tier=AutonomyTier.T1,
        hypotheses=[_hypothesis(hid="h1", likelihood=0.9), _hypothesis(hid="h2", likelihood=0.85)],
    )
    assert run.page_payload is not None
    competing_ids = [h.hypothesis_id for h in run.page_payload.competing_hypotheses]
    assert competing_ids == ["h2"]  # everything except the leader


# --- integration through the REAL credential broker (doc 05 §2) -------------------
class _FakeVault:
    def __init__(self) -> None:
        self.mints: list[tuple[str, str, str]] = []

    async def mint_scoped(
        self, *, tenant_id: str, provider: str, scope: str, ttl: timedelta
    ) -> Credential:
        self.mints.append((tenant_id, provider, scope))
        return Credential(
            lease_id="lease_1", secret="SUPER_SECRET", ttl_seconds=int(ttl.total_seconds())
        )

    async def revoke(self, lease_id: str) -> None:
        return None


class _FakeAudit:
    def __init__(self) -> None:
        self.entries: list[dict[str, object]] = []

    async def append(
        self, *, tenant_id: str, actor: str, action: str, target: str, detail: dict[str, object]
    ) -> None:
        self.entries.append({"tenant_id": tenant_id, "action": action, "detail": detail})


class _FakeServer:
    def __init__(self) -> None:
        self.invocations: list[tuple[str, str, str]] = []

    async def invoke(
        self, *, action: str, target: str, args: dict[str, object], credential: str
    ) -> ToolResult:
        self.invocations.append((action, target, credential))
        return ToolResult(ok=True, data={"rolled_back_to": "rel_prev"})


class _BrokerExecutor:
    """Adapter wiring the orchestrator's executor seam to a real ToolBroker."""

    def __init__(self, router: MCPRouter) -> None:
        self._router = router

    async def execute(
        self, plan: RemediationPlan, *, tenant_id: str, incident_id: str
    ) -> ActionResult:
        broker = ToolBroker(tenant_id=tenant_id, incident_id=incident_id, router=self._router)
        result = await broker.call(
            provider="vercel", action=plan.action_type, target=plan.target, write=True
        )
        status = ActionStatus.SUCCEEDED if result.ok else ActionStatus.FAILED
        return ActionResult(action_id="act_1", status=status)


def test_autonomous_path_drives_the_real_broker() -> None:
    vault, audit, server = _FakeVault(), _FakeAudit(), _FakeServer()
    router = MCPRouter(vault, audit, {"vercel": server})
    run, _ = _run(grant_tier=AutonomyTier.T3, executor=_BrokerExecutor(router))

    assert run.final_state is IncidentState.CLOSED
    assert server.invocations and server.invocations[0][0] == "deploy.rollback"
    assert vault.mints and vault.mints[0][0] == "ten_a"  # minted for THIS run's tenant
    assert "SUPER_SECRET" not in str(audit.entries)  # the secret is never audited (doc 05 §2.2)

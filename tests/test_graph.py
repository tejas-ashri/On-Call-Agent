"""The LangGraph incident runtime (doc 04 §2, §7, §9).

These tests drive the *durable* realization of the incident graph — a real
``StateGraph`` compiled with a checkpointer — and prove the two behaviors the
straight-line ``orchestrator.run_incident`` driver structurally cannot: a genuine
human-in-the-loop ``interrupt()`` at the T2 approval gate, and durable resume
across that suspend. The same injected ports and deterministic fakes as
``test_orchestrator`` are used, so everything runs offline. One test wires the
*real* credential broker through the executor seam, as the orchestrator suite
does, to prove the graph composes with it too.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

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
from sentinel.graph import IncidentGraphState, build_incident_graph, initial_state
from sentinel.incidents.events import IncidentEventType
from sentinel.incidents.projection import project
from sentinel.mcp.broker import Credential, MCPRouter, ToolBroker, ToolResult
from sentinel.orchestrator import (
    ActionExecutor,
    Commander,
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


def _graph(
    *,
    grant_tier: AutonomyTier,
    hypotheses: list[Hypothesis] | None = None,
    plan: RemediationPlan | None = None,
    executor: ActionExecutor | None = None,
) -> tuple[CompiledStateGraph[IncidentGraphState], RecordingExecutor]:
    """Build a compiled incident graph wired with deterministic fakes."""
    rec = RecordingExecutor(ActionResult(action_id="act_1", status=ActionStatus.SUCCEEDED))
    ex: ActionExecutor = executor if executor is not None else rec
    specialists: list[Specialist] = [
        FakeSpecialist("change_deploy"),
        FakeSpecialist("metric"),
        FakeSpecialist("trace"),
    ]
    commander: Commander = FakeCommander(hypotheses if hypotheses is not None else [_hypothesis()])
    planner: Planner = FakePlanner(plan if plan is not None else _plan())
    app = build_incident_graph(
        specialists=specialists,
        commander=commander,
        planner=planner,
        executor=ex,
        grant=_grant(grant_tier),
        calibrator=Calibrator(temperature=1.0, version="cal.test"),
        clock=lambda: _T,
    )
    return app, rec


def _cfg(thread: str = "inc_1") -> RunnableConfig:
    return {"configurable": {"thread_id": thread}}


# --------------------------------------------------------------------------- tests
def test_autonomous_path_runs_to_closed() -> None:
    app, rec = _graph(grant_tier=AutonomyTier.T3)
    out = asyncio.run(app.ainvoke(initial_state(draft=_draft(), events=[_event()]), _cfg()))

    assert "__interrupt__" not in out  # ran straight through, no human gate
    assert out["decision"].outcome == "act_and_report"
    assert len(rec.calls) == 1  # the executor seam was driven exactly once
    assert out["action_result"].status is ActionStatus.SUCCEEDED
    assert out["page_payload"] is None
    assert out["final_state"] is IncidentState.CLOSED
    # event-sourcing invariant: replaying the emitted log reproduces the final state.
    projection = project(out["event_log"])
    assert projection.state is IncidentState.CLOSED
    assert projection.confidence_band is out["assessment"].band
    assert projection.root_cause == out["hypotheses"][0].statement


def test_t2_suspends_at_approval_interrupt() -> None:
    app, rec = _graph(grant_tier=AutonomyTier.T2)
    out = asyncio.run(app.ainvoke(initial_state(draft=_draft(), events=[_event()]), _cfg()))

    # the graph SUSPENDED at the human gate rather than running to a terminal state
    assert "__interrupt__" in out
    assert out["__interrupt__"][0].value["reason"] == "approval_required"
    assert out["__interrupt__"][0].value["action_type"] == "deploy.rollback"
    assert rec.calls == []  # nothing has been executed — approval is still pending (C1)

    # the checkpoint is durable: the persisted state knows the pending node is the gate
    snap = asyncio.run(app.aget_state(_cfg()))
    assert snap.next == ("await_approval",)
    # projecting the *checkpointed* log already reads AWAITING_APPROVAL
    assert project(snap.values["event_log"]).state is IncidentState.AWAITING_APPROVAL


def test_t2_resume_approved_acts_and_closes() -> None:
    app, rec = _graph(grant_tier=AutonomyTier.T2)
    asyncio.run(app.ainvoke(initial_state(draft=_draft(), events=[_event()]), _cfg()))

    final = asyncio.run(app.ainvoke(Command(resume={"approved": True}), _cfg()))

    assert len(rec.calls) == 1  # resumed past the interrupt and acted exactly once
    assert final["action_result"].status is ActionStatus.SUCCEEDED
    assert final["final_state"] is IncidentState.CLOSED
    assert project(final["event_log"]).state is IncidentState.CLOSED


def test_t2_resume_denied_escalates_without_acting() -> None:
    app, rec = _graph(grant_tier=AutonomyTier.T2)
    asyncio.run(app.ainvoke(initial_state(draft=_draft(), events=[_event()]), _cfg()))

    final = asyncio.run(app.ainvoke(Command(resume={"approved": False}), _cfg()))

    assert rec.calls == []  # a denial is a hard stop — no side effect, ever (C1)
    assert final["final_state"] is IncidentState.ESCALATED
    assert project(final["event_log"]).state is IncidentState.ESCALATED


def test_irreversible_action_pages_even_at_t3() -> None:
    # Planner claims reversible=True, but db.failover is hard-pinned by the floor.
    app, rec = _graph(
        grant_tier=AutonomyTier.T3, plan=_plan(action="db.failover", reversible=True)
    )
    out = asyncio.run(app.ainvoke(initial_state(draft=_draft(), events=[_event()]), _cfg()))

    assert "__interrupt__" not in out  # no human gate — it's a straight page
    assert out["decision"].outcome == "page_irreversible"
    assert rec.calls == []
    assert out["final_state"] is IncidentState.ESCALATED
    assert project(out["event_log"]).state is IncidentState.ESCALATED


def test_low_confidence_pages_even_at_t3() -> None:
    # Low self-assessment AND no corroboration → calibrated LOW → hard stop.
    app, rec = _graph(
        grant_tier=AutonomyTier.T3, hypotheses=[_hypothesis(likelihood=0.2, supporting=[])]
    )
    out = asyncio.run(app.ainvoke(initial_state(draft=_draft(), events=[_event()]), _cfg()))

    assert out["assessment"].band is ConfidenceBand.LOW
    assert out["decision"].outcome == "page_low_confidence"
    assert rec.calls == []
    assert out["final_state"] is IncidentState.ESCALATED


def test_event_log_seq_is_gapfree_across_resume() -> None:
    # The hardest seq case: the stream must stay contiguous *across* the suspend.
    app, _ = _graph(grant_tier=AutonomyTier.T2)
    asyncio.run(app.ainvoke(initial_state(draft=_draft(), events=[_event()]), _cfg()))
    final = asyncio.run(app.ainvoke(Command(resume={"approved": True}), _cfg()))

    seqs = [e.seq for e in final["event_log"]]
    assert seqs == list(range(1, len(seqs) + 1))  # 1..N, gap-free over the resume boundary
    types = [e.type for e in final["event_log"]]
    assert IncidentEventType.APPROVAL_GRANTED in types
    assert types[-1] is IncidentEventType.INCIDENT_CLOSED


def test_graph_matches_driver_on_autonomous_path() -> None:
    # The graph is a *new realization* of run_incident's control flow; on the
    # non-interactive autonomous path the two must agree exactly.
    app, _ = _graph(grant_tier=AutonomyTier.T3)
    out = asyncio.run(app.ainvoke(initial_state(draft=_draft(), events=[_event()]), _cfg()))

    rec2 = RecordingExecutor(ActionResult(action_id="act_x", status=ActionStatus.SUCCEEDED))
    driver = asyncio.run(
        run_incident(
            draft=_draft(),
            events=[_event()],
            specialists=[
                FakeSpecialist("change_deploy"),
                FakeSpecialist("metric"),
                FakeSpecialist("trace"),
            ],
            commander=FakeCommander([_hypothesis()]),
            planner=FakePlanner(_plan()),
            executor=rec2,
            grant=_grant(AutonomyTier.T3),
            calibrator=Calibrator(temperature=1.0, version="cal.test"),
            clock=lambda: _T,
        )
    )
    assert driver.decision is not None and driver.assessment is not None
    assert out["final_state"] is driver.final_state
    assert out["decision"].outcome == driver.decision.outcome
    assert out["assessment"].band is driver.assessment.band


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
    """Adapter wiring the graph's executor seam to a real ToolBroker."""

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
    app, _ = _graph(grant_tier=AutonomyTier.T3, executor=_BrokerExecutor(router))

    out = asyncio.run(app.ainvoke(initial_state(draft=_draft(), events=[_event()]), _cfg()))

    assert out["final_state"] is IncidentState.CLOSED
    assert server.invocations and server.invocations[0][0] == "deploy.rollback"
    assert vault.mints and vault.mints[0][0] == "ten_a"  # minted for THIS run's tenant
    assert "SUPER_SECRET" not in str(audit.entries)  # the secret is never audited (doc 05 §2.2)

"""Safety guarantees around acting, paging and ingesting (C1, C2).

Each test pins one way the system could act when it must not, or stay silent
when it must page: grants applied outside their scope, forged webhooks, loosely
parsed approvals, agent failures that aborted the run, pages never delivered,
alerts dropped by dedup, the cost breaker not reaching the decision, an action
executed twice, an approval of the wrong action, and a grant revoked mid-wait.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from sentinel.audit import AuditLog, verify_chain
from sentinel.confidence import Calibrator
from sentinel.connectors import (
    IntegrationContext,
    RawSignal,
    SentryConnector,
    VercelConnector,
    webhook_signature,
)
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
from sentinel.edge import IngestionEdge, InMemoryDedupStore, InMemoryEventBus
from sentinel.enums import (
    ActionStatus,
    ActionType,
    AutonomyTier,
    ConfidenceBand,
    EventKind,
    IncidentState,
    IntegrationMethod,
    Severity,
)
from sentinel.escalation import GrantLookup, effective_tier, grant_lookup
from sentinel.graph import IncidentGraphState, build_incident_graph, initial_state
from sentinel.graph.nodes import _parse_approval
from sentinel.incidents.events import IncidentEventType
from sentinel.incidents.projection import project
from sentinel.notify import CapturingNotifier
from sentinel.orchestrator import InvestigationContext, run_incident
from sentinel.orchestrator.guard import UNDETERMINED, execute_once
from sentinel.orchestrator.ledger import ActionLedger, InMemoryActionLedger, action_id_for

# fitted and validated, so T3 grants may act (the calibration gate has its own tests)
_TRUSTED_CAL = Calibrator(temperature=1.0, version="cal.test", fitted_on=1000, holdout_ece=0.01)
_T = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
_ROLES: tuple[SpecialistRole, ...] = ("change_deploy", "metric", "trace")


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


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        hypothesis_id="h1",
        statement="deploy abc123 exhausted the DB pool",
        supporting_roles=list(_ROLES),
        raw_likelihood=0.95,
    )


def _plan() -> RemediationPlan:
    return RemediationPlan(
        incident_id="inc_1",
        action_type="deploy.rollback",
        target="svc_api",
        reversible=True,
        summary="roll back to the prior release",
        estimated_blast_radius="single_service",
    )


def _grant(tier: AutonomyTier = AutonomyTier.T3, **overrides: object) -> AutonomyGrant:
    fields: dict[str, object] = {
        "grant_id": "grant_1",
        "tenant_id": "ten_a",
        "action_type": ActionType.DEPLOY_ROLLBACK,
        "max_tier": tier,
        "granted_by": "human:1",
        "granted_at": _T,
        **overrides,
    }
    return AutonomyGrant.model_validate(fields)


# --------------------------------------------------------------------------- fakes
class Specialist:
    def __init__(self, role: SpecialistRole, *, fail: bool = False) -> None:
        self.role = role
        self._fail = fail

    async def investigate(self, ctx: InvestigationContext) -> SpecialistFinding:
        if self._fail:
            raise TimeoutError("model timed out")
        return SpecialistFinding(
            incident_id=ctx.incident_id,
            role=self.role,
            summary=f"{self.role} corroborates deploy abc123",
            suspected_cause="deploy abc123",
            self_confidence=0.9,
            evidence=[EvidenceRef(kind="deploy", summary="abc123", observed_at=_T)],
        )


class Commander:
    def __init__(self, result: list[Hypothesis] | Exception) -> None:
        self._result = result

    async def synthesize(
        self, incident_id: str, findings: Sequence[SpecialistFinding]
    ) -> list[Hypothesis]:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class Planner:
    def __init__(self, result: RemediationPlan | Exception) -> None:
        self._result = result

    async def draft(
        self, incident_id: str, hypothesis: Hypothesis, events: Sequence[CanonicalEvent]
    ) -> RemediationPlan:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class Executor:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[RemediationPlan] = []
        self.action_ids: list[str] = []

    async def execute(
        self, plan: RemediationPlan, *, tenant_id: str, incident_id: str, action_id: str
    ) -> ActionResult:
        self.calls.append(plan)
        self.action_ids.append(action_id)
        if self._fail:
            raise ConnectionError("provider down")
        return ActionResult(action_id=action_id, status=ActionStatus.SUCCEEDED)


class Clock:
    def __init__(self) -> None:
        self.now = _T

    def __call__(self) -> datetime:
        return self.now


def _graph(
    *,
    calibrator: Calibrator = _TRUSTED_CAL,
    require_calibrated_autonomy: bool = True,
    grant: AutonomyGrant | None = None,
    grants: GrantLookup | Sequence[AutonomyGrant] | None = None,
    ledger: ActionLedger | None = None,
    specialists: Sequence[Specialist] | None = None,
    commander: Commander | None = None,
    planner: Planner | None = None,
    executor: Executor | None = None,
    notifier: CapturingNotifier | None = None,
    breaker: Callable[[str], bool] | None = None,
    clock: Callable[[], datetime] = lambda: _T,
    audit: AuditLog | None = None,
    approvers: Callable[[str, str], bool] | None = lambda tenant_id, who: who == "alice",
) -> tuple[CompiledStateGraph[IncidentGraphState], Executor]:
    ex = executor or Executor()
    app = build_incident_graph(
        specialists=list(specialists or [Specialist(r) for r in _ROLES]),
        commander=commander or Commander([_hypothesis()]),
        planner=planner or Planner(_plan()),
        executor=ex,
        grants=grants if grants is not None else [grant or _grant()],
        ledger=ledger,
        calibrator=calibrator,
        require_calibrated_autonomy=require_calibrated_autonomy,
        clock=clock,
        notifier=notifier,
        cost_breaker_open=breaker,
        audit=audit,
        approvers=approvers,
        checkpointer=InMemorySaver(),
    )
    return app, ex


def _cfg() -> RunnableConfig:
    return {"configurable": {"thread_id": "inc_1"}}


def _start(app: CompiledStateGraph[IncidentGraphState]) -> dict[str, object]:
    return dict(asyncio.run(app.ainvoke(initial_state(draft=_draft(), events=[_event()]), _cfg())))


def _resume(app: CompiledStateGraph[IncidentGraphState], value: object) -> dict[str, object]:
    return dict(asyncio.run(app.ainvoke(Command(resume=value), _cfg())))


def _action_id(app: CompiledStateGraph[IncidentGraphState]) -> str:
    return str(asyncio.run(app.aget_state(_cfg())).values["action_id"])


def _approve(
    app: CompiledStateGraph[IncidentGraphState], approver: str = "alice"
) -> dict[str, object]:
    """Resume with a well-formed approval of the paused incident's action."""
    return _resume(app, {"approved": True, "action_id": _action_id(app), "approver": approver})


# --------------------------------------------------------------------------- 1. grant scope
@pytest.mark.parametrize(
    "overrides",
    [
        {"revoked_at": _T},
        {"tenant_id": "ten_other"},
        {"action_type": ActionType.FEATURE_FLAG_TOGGLE},
    ],
    ids=["revoked", "other-tenant", "other-action"],
)
def test_out_of_scope_grant_confers_nothing(overrides: dict[str, object]) -> None:
    grant = _grant(AutonomyTier.T3, **overrides)
    assert effective_tier(grant, tenant_id="ten_a", action_type="deploy.rollback") is (
        AutonomyTier.T0
    )

    app, ex = _graph(grant=grant)
    out = _start(app)
    assert ex.calls == []
    assert out["decision"].outcome == "page_with_drafted_fix"  # type: ignore[attr-defined]
    assert out["final_state"] is IncidentState.ESCALATED


def test_constrained_t3_grant_requires_approval() -> None:
    grant = _grant(AutonomyTier.T3, constraints={"max_rollbacks_per_day": 1})
    assert effective_tier(grant, tenant_id="ten_a", action_type="deploy.rollback") is (
        AutonomyTier.T2
    )
    app, ex = _graph(grant=grant)
    out = _start(app)
    assert "__interrupt__" in out and ex.calls == []


def test_in_scope_grant_still_acts() -> None:
    app, ex = _graph()
    out = _start(app)
    assert len(ex.calls) == 1 and out["final_state"] is IncidentState.CLOSED


def test_driver_applies_grant_scope_too() -> None:
    run = asyncio.run(
        run_incident(
            draft=_draft(),
            events=[_event()],
            specialists=[Specialist(r) for r in _ROLES],
            commander=Commander([_hypothesis()]),
            planner=Planner(_plan()),
            executor=(ex := Executor()),
            grants=[_grant(AutonomyTier.T3, revoked_at=_T)],
            calibrator=_TRUSTED_CAL,
            clock=lambda: _T,
        )
    )
    assert ex.calls == [] and run.final_state is IncidentState.ESCALATED


# --------------------------------------------------------------------------- 2. webhooks
_SECRET = "s3cret"


def _raw(
    payload: dict[str, object], *, header: str, sig: str | None, body: bytes | None
) -> RawSignal:
    return RawSignal(
        integration_id="int_1",
        method=IntegrationMethod.WEBHOOK,
        payload=payload,
        headers={header: sig} if sig is not None else {},
        received_at=_T,
        body=body,
    )


def _ictx(provider: str) -> IntegrationContext:
    return IntegrationContext(
        tenant_id="ten_a", integration_id="int_1", provider=provider, vault_ref="v"
    )


def _verify(connector: SentryConnector | VercelConnector, raw: RawSignal) -> bool:
    return asyncio.run(connector.verify_webhook(raw, _ictx(connector.capabilities.provider)))


_PAYLOAD: dict[str, object] = {"data": {"event": {"event_id": "e1", "issue_id": "7"}}}
_BODY = json.dumps(_PAYLOAD).encode()


def test_correctly_signed_webhooks_are_accepted() -> None:
    sentry = _raw(
        _PAYLOAD,
        header="Sentry-Hook-Signature",
        sig=webhook_signature(_SECRET, _BODY),
        body=_BODY,
    )
    vercel = _raw(
        _PAYLOAD,
        header="x-vercel-signature",
        sig=webhook_signature(_SECRET, _BODY, "sha1"),
        body=_BODY,
    )
    assert _verify(SentryConnector(webhook_secret=_SECRET), sentry)
    assert _verify(VercelConnector(webhook_secret=_SECRET), vercel)


@pytest.mark.parametrize(
    ("secret", "sig", "body", "payload"),
    [
        (None, webhook_signature(_SECRET, _BODY), _BODY, _PAYLOAD),  # no secret configured
        (_SECRET, None, _BODY, _PAYLOAD),  # unsigned
        (_SECRET, webhook_signature("wrong", _BODY), _BODY, _PAYLOAD),  # wrong key
        (_SECRET, webhook_signature(_SECRET, _BODY), None, _PAYLOAD),  # body not kept
        (_SECRET, webhook_signature(_SECRET, _BODY), _BODY, {"data": "forged"}),  # swapped
        (_SECRET, webhook_signature(_SECRET, b"not json"), b"not json", _PAYLOAD),
    ],
    ids=["no-secret", "unsigned", "wrong-key", "no-body", "swapped-payload", "non-json"],
)
def test_unverifiable_webhooks_are_rejected(
    secret: str | None, sig: str | None, body: bytes | None, payload: dict[str, object]
) -> None:
    raw = _raw(payload, header="sentry-hook-signature", sig=sig, body=body)
    assert not _verify(SentryConnector(webhook_secret=secret), raw)


# --------------------------------------------------------------------------- 3. approvals
@pytest.mark.parametrize(
    "value", [True, "false", "no", "true", 1, None, {}, {"approved": "no"}, {"approved": 1}]
)
def test_approval_parser_fails_closed(value: object) -> None:
    approver, reason = _parse_approval(value, "act_x")
    assert reason is not None
    ok = {"approved": True, "action_id": "act_x", "approver": "a"}
    assert _parse_approval(ok, "act_x") == ("a", None)


def test_approval_parser_binds_to_the_action_and_requires_an_approver() -> None:
    stale = {"approved": True, "action_id": "act_old", "approver": "a"}
    assert _parse_approval(stale, "act_x") == ("a", "action_mismatch")
    anonymous = {"approved": True, "action_id": "act_x"}
    assert _parse_approval(anonymous, "act_x") == (None, "no_approver")


@pytest.mark.parametrize(
    "value", [True, "false", "no", 1, {"approved": "no"}, {"approved": 1}, {"approved": True}]
)
def test_only_an_explicit_true_approves(value: object) -> None:
    app, ex = _graph(grant=_grant(AutonomyTier.T2))
    _start(app)
    final = _resume(app, value)
    assert ex.calls == []
    assert final["final_state"] is IncidentState.ESCALATED


def test_approval_records_the_approver() -> None:
    audit = AuditLog()
    app, ex = _graph(grant=_grant(AutonomyTier.T2), audit=audit)
    _start(app)
    final = _approve(app)
    assert len(ex.calls) == 1 and final["final_state"] is IncidentState.CLOSED
    granted = next(
        e
        for e in final["event_log"]  # type: ignore[attr-defined]
        if e.type is IncidentEventType.APPROVAL_GRANTED
    )
    assert granted.actor == "human:alice"
    actions = [e.action for e in audit.entries("ten_a")]
    assert actions == ["incident.decided", "approval.granted", "action.executed"]
    assert verify_chain(audit.entries("ten_a")).ok


def test_stale_approval_is_treated_as_denial() -> None:
    clock = Clock()
    app, ex = _graph(grant=_grant(AutonomyTier.T2), clock=clock)
    _start(app)
    clock.now = _T + timedelta(hours=2)
    final = _approve(app)
    assert ex.calls == []
    denied = final["event_log"][-1]  # type: ignore[index]
    assert denied.type is IncidentEventType.APPROVAL_DENIED
    assert denied.payload == {"reason": "expired"}
    assert project(final["event_log"]).state is IncidentState.ESCALATED  # type: ignore[arg-type]


# --------------------------------------------------------------------------- 4. failures page
def test_failed_specialist_degrades_instead_of_aborting() -> None:
    notifier = CapturingNotifier()
    specialists = [Specialist("change_deploy"), Specialist("metric", fail=True)]
    app, _ = _graph(specialists=specialists, grant=_grant(AutonomyTier.T1), notifier=notifier)
    out = _start(app)
    by_role = {f.role: f for f in out["findings"]}  # type: ignore[attr-defined]
    assert by_role["metric"].status == "degraded"
    assert by_role["metric"].summary == "metric specialist failed: TimeoutError"
    assert len(notifier.pages) == 1


@pytest.mark.parametrize("result", [RuntimeError("rate limited"), []], ids=["raises", "empty"])
def test_failed_synthesis_pages_low_confidence(result: list[Hypothesis] | Exception) -> None:
    notifier = CapturingNotifier()
    app, ex = _graph(commander=Commander(result), notifier=notifier)
    out = _start(app)
    assert ex.calls == []
    assert out["assessment"].band is ConfidenceBand.LOW  # type: ignore[attr-defined]
    assert out["decision"].outcome == "page_low_confidence"  # type: ignore[attr-defined]
    assert notifier.pages[0].suspected_root_cause == UNDETERMINED


@pytest.mark.parametrize(
    "result",
    [RuntimeError("bad json"), _plan().model_copy(update={"action_type": "rm.rf"})],
    ids=["raises", "unknown-action"],
)
def test_failed_planner_pages_without_a_plan(result: RemediationPlan | Exception) -> None:
    notifier = CapturingNotifier()
    app, ex = _graph(planner=Planner(result), notifier=notifier)
    out = _start(app)
    assert ex.calls == []
    assert out["final_state"] is IncidentState.ESCALATED
    assert notifier.pages[0].drafted_remediation is None


def test_raising_executor_escalates_with_the_fix() -> None:
    notifier = CapturingNotifier()
    app, ex = _graph(executor=Executor(fail=True), notifier=notifier)
    out = _start(app)
    assert len(ex.calls) == 1
    assert out["action_result"].status is ActionStatus.FAILED  # type: ignore[attr-defined]
    assert out["final_state"] is IncidentState.ESCALATED
    assert notifier.pages[0].drafted_remediation == _plan()


def test_driver_survives_agent_failures_too() -> None:
    notifier = CapturingNotifier()
    run = asyncio.run(
        run_incident(
            draft=_draft(),
            events=[_event()],
            specialists=[Specialist("metric", fail=True)],
            commander=Commander(RuntimeError("down")),
            planner=Planner(RuntimeError("down")),
            executor=Executor(),
            grants=[_grant()],
            calibrator=_TRUSTED_CAL,
            clock=lambda: _T,
            notifier=notifier,
        )
    )
    assert run.final_state is IncidentState.ESCALATED
    assert run.findings[0].status == "degraded"
    assert len(notifier.pages) == 1


# --------------------------------------------------------------------------- 5. pages delivered
def test_every_paging_outcome_is_delivered() -> None:
    for tier, interrupted in ((AutonomyTier.T1, False), (AutonomyTier.T2, True)):
        notifier = CapturingNotifier()
        app, _ = _graph(grant=_grant(tier), notifier=notifier)
        out = _start(app)
        assert ("__interrupt__" in out) is interrupted
        assert notifier.pages == [out["page_payload"]]


def test_clean_autonomous_fix_sends_no_page() -> None:
    notifier = CapturingNotifier()
    app, _ = _graph(notifier=notifier)
    _start(app)
    assert notifier.pages == []


# --------------------------------------------------------------------------- 6. dedup
class _FlakyBus(InMemoryEventBus):
    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    async def publish(self, event: CanonicalEvent) -> None:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("kafka unavailable")
        await super().publish(event)


def test_failed_publish_does_not_swallow_the_retry() -> None:
    bus = _FlakyBus()
    edge = IngestionEdge(bus=bus, dedup=InMemoryDedupStore())
    connector = SentryConnector(webhook_secret=_SECRET)
    payload: dict[str, object] = {"data": {"event": {"event_id": "e1", "issue_id": "7"}}}
    body = json.dumps(payload).encode()
    raw = _raw(
        payload, header="sentry-hook-signature", sig=webhook_signature(_SECRET, body), body=body
    )

    with pytest.raises(ConnectionError):
        asyncio.run(edge.ingest(raw, _ictx("sentry"), connector))
    retried = asyncio.run(edge.ingest(raw, _ictx("sentry"), connector))  # provider retry
    assert len(retried) == 1 and bus.tenant_events("ten_a") == retried
    assert asyncio.run(edge.ingest(raw, _ictx("sentry"), connector)) == []  # now a real dup


# --------------------------------------------------------------------------- 7. cost breaker
def test_open_breaker_forces_observe_only() -> None:
    app, ex = _graph(breaker=lambda tenant: tenant == "ten_a")
    out = _start(app)
    assert ex.calls == []
    assert out["decision"].granted_tier is AutonomyTier.T0  # type: ignore[attr-defined]


def test_unreadable_breaker_counts_as_open() -> None:
    def broken(tenant: str) -> bool:
        raise ConnectionError("redis down")

    app, ex = _graph(breaker=broken)
    _start(app)
    assert ex.calls == []


def test_breaker_opening_during_approval_blocks_the_action() -> None:
    state = {"open": False}
    notifier = CapturingNotifier()
    app, ex = _graph(
        grant=_grant(AutonomyTier.T2), breaker=lambda _: state["open"], notifier=notifier
    )
    _start(app)
    state["open"] = True
    final = _approve(app)
    assert ex.calls == []
    assert final["final_state"] is IncidentState.ESCALATED
    assert final["event_log"][-1].payload == {"reason": "cost_breaker_open"}  # type: ignore[index]
    assert len(notifier.pages) == 2  # the approval page, then the escalation


def test_driver_honours_the_breaker() -> None:
    run = asyncio.run(
        run_incident(
            draft=_draft(),
            events=[_event()],
            specialists=[Specialist(r) for r in _ROLES],
            commander=Commander([_hypothesis()]),
            planner=Planner(_plan()),
            executor=(ex := Executor()),
            grants=[_grant()],
            calibrator=_TRUSTED_CAL,
            clock=lambda: _T,
            cost_breaker_open=lambda _: True,
        )
    )
    assert ex.calls == [] and run.final_state is IncidentState.ESCALATED


# --------------------------------------------------------------------------- 6. at-most-once
def test_action_id_is_deterministic_and_tracks_what_the_write_does() -> None:
    plan = _plan()
    assert action_id_for("inc_1", plan) == action_id_for("inc_1", plan)
    reworded = plan.model_copy(update={"summary": "different words, same rollback"})
    assert action_id_for("inc_1", reworded) == action_id_for("inc_1", plan)
    assert action_id_for("inc_1", plan.model_copy(update={"target": "svc_b"})) != (
        action_id_for("inc_1", plan)
    )
    assert action_id_for("inc_2", plan) != action_id_for("inc_1", plan)


def test_execute_once_replays_the_recorded_result_instead_of_acting_again() -> None:
    ledger, ex = InMemoryActionLedger(), Executor()

    def run() -> ActionResult:
        return asyncio.run(
            execute_once(ex, ledger, _plan(), tenant_id="ten_a", incident_id="inc_1",
                         action_id="act_x")
        )

    first, second = run(), run()
    assert len(ex.calls) == 1
    assert ex.action_ids == ["act_x"]  # the idempotency key reaches the executor
    assert second == first


def test_claimed_action_without_a_result_escalates_as_outcome_unknown() -> None:
    # The process died between the provider call and recording its result.
    ledger, ex = InMemoryActionLedger(), Executor()
    asyncio.run(ledger.claim("act_x"))
    result = asyncio.run(
        execute_once(ex, ledger, _plan(), tenant_id="ten_a", incident_id="inc_1",
                     action_id="act_x")
    )
    assert ex.calls == []
    assert result.status is ActionStatus.FAILED
    assert "outcome unknown" in result.detail


class BrokenLedger:
    async def claim(self, action_id: str) -> bool:
        raise ConnectionError("db down")

    async def complete(self, action_id: str, result: ActionResult) -> None:
        raise ConnectionError("db down")

    async def result(self, action_id: str) -> ActionResult | None:
        raise ConnectionError("db down")


def test_unavailable_ledger_never_executes() -> None:
    app, ex = _graph(grant=_grant(AutonomyTier.T3), ledger=BrokenLedger())
    out = _start(app)
    assert ex.calls == []
    assert out["final_state"] is IncidentState.ESCALATED


def test_rerunning_an_incident_does_not_repeat_its_action() -> None:
    # A retried run of the same incident (crash before the act checkpoint, a
    # redelivered trigger) reaches act again with the same decision.
    ledger = InMemoryActionLedger()
    ex = Executor()
    for thread in ("run_1", "run_2"):
        app, _ = _graph(grant=_grant(AutonomyTier.T3), ledger=ledger, executor=ex)
        cfg: RunnableConfig = {"configurable": {"thread_id": thread}}
        out = asyncio.run(app.ainvoke(initial_state(draft=_draft(), events=[_event()]), cfg))
        assert out["final_state"] is IncidentState.CLOSED
    assert len(ex.calls) == 1


# --------------------------------------------------------------------------- 7. bound approvals
def test_page_and_interrupt_carry_the_one_tap_action_id() -> None:
    app, _ = _graph(grant=_grant(AutonomyTier.T2))
    out = _start(app)
    action_id = action_id_for("inc_1", _plan())
    assert out["action_id"] == action_id
    assert out["page_payload"].one_tap_action_id == action_id  # type: ignore[attr-defined]
    assert out["__interrupt__"][0].value["action_id"] == action_id  # type: ignore[index]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"approved": True, "action_id": "act_stale", "approver": "alice"}, "action_mismatch"),
        ({"approved": True, "approver": "alice"}, "action_mismatch"),
        ({"approved": True, "action_id": "<current>"}, "no_approver"),
        ({"approved": True, "action_id": "<current>", "approver": "mallory"},
         "unauthorized_approver"),
    ],
    ids=["stale-tap", "unbound", "anonymous", "not-an-approver"],
)
def test_approval_must_name_this_action_and_an_authorized_approver(
    payload: dict[str, object], reason: str
) -> None:
    app, ex = _graph(grant=_grant(AutonomyTier.T2))
    _start(app)
    if payload.get("action_id") == "<current>":
        payload = {**payload, "action_id": _action_id(app)}
    final = _resume(app, payload)
    assert ex.calls == []
    assert final["event_log"][-1].payload == {"reason": reason}  # type: ignore[index]
    assert final["final_state"] is IncidentState.ESCALATED


def test_without_an_approver_directory_no_approval_succeeds() -> None:
    app, ex = _graph(grant=_grant(AutonomyTier.T2), approvers=None)
    _start(app)
    final = _approve(app)
    assert ex.calls == []
    assert final["event_log"][-1].payload == {"reason": "unauthorized_approver"}  # type: ignore[index]


def test_failing_approver_directory_denies() -> None:
    def broken(tenant_id: str, who: str) -> bool:
        raise ConnectionError("directory down")

    app, ex = _graph(grant=_grant(AutonomyTier.T2), approvers=broken)
    _start(app)
    _approve(app)
    assert ex.calls == []


# --------------------------------------------------------------------------- 8. per-action grants
def _restart_plan() -> RemediationPlan:
    return _plan().model_copy(update={"action_type": "process.restart"})


def test_each_action_type_uses_its_own_grant() -> None:
    grants = [
        _grant(AutonomyTier.T1),  # deploy.rollback: suggest only
        _grant(AutonomyTier.T3, grant_id="grant_2", action_type=ActionType.PROCESS_RESTART),
    ]
    app, ex = _graph(grants=grants, planner=Planner(_restart_plan()))
    out = _start(app)
    assert out["decision"].granted_tier is AutonomyTier.T3  # type: ignore[attr-defined]
    assert len(ex.calls) == 1 and out["final_state"] is IncidentState.CLOSED

    app, ex = _graph(grants=grants)  # the rollback plan still gets only T1
    out = _start(app)
    assert ex.calls == []
    assert out["decision"].outcome == "page_with_drafted_fix"  # type: ignore[attr-defined]


def test_grant_lookup_prefers_active_then_the_lowest_tier() -> None:
    lookup = grant_lookup(
        [
            _grant(AutonomyTier.T3, grant_id="revoked", revoked_at=_T),
            _grant(AutonomyTier.T3, grant_id="t3"),
            _grant(AutonomyTier.T2, grant_id="t2"),
        ]
    )
    chosen = lookup("ten_a", "deploy.rollback")
    assert chosen is not None and chosen.grant_id == "t2"
    assert lookup("ten_a", "process.restart") is None
    assert lookup("ten_b", "deploy.rollback") is None


def test_failing_grant_store_confers_nothing() -> None:
    def broken(tenant_id: str, action_type: str) -> AutonomyGrant | None:
        raise ConnectionError("db down")

    app, ex = _graph(grants=broken)
    out = _start(app)
    assert ex.calls == []
    assert out["decision"].granted_tier is AutonomyTier.T0  # type: ignore[attr-defined]


def test_grant_revoked_while_awaiting_approval_blocks_the_action() -> None:
    live = {"grant": _grant(AutonomyTier.T2)}
    app, ex = _graph(grants=lambda tenant_id, action_type: live["grant"])
    _start(app)
    live["grant"] = _grant(AutonomyTier.T2, revoked_at=_T)
    final = _approve(app)
    assert ex.calls == []
    assert final["event_log"][-1].payload == {"reason": "grant_no_longer_permits"}  # type: ignore[index]
    assert final["final_state"] is IncidentState.ESCALATED


# --------------------------------------------------------------------------- 9. calibration gate
def test_unvalidated_calibrator_caps_t3_at_approval() -> None:
    untrusted = Calibrator(temperature=1.0, version="hand-picked")
    app, ex = _graph(grant=_grant(AutonomyTier.T3), calibrator=untrusted)
    out = _start(app)
    assert ex.calls == []  # a HIGH band from an unvalidated map does not move prod
    assert out["decision"].granted_tier is AutonomyTier.T2  # type: ignore[attr-defined]
    assert out["decision"].outcome == "page_one_tap"  # type: ignore[attr-defined]
    assert "__interrupt__" in out


def test_validated_calibrator_allows_t3_to_act() -> None:
    app, ex = _graph(grant=_grant(AutonomyTier.T3))
    out = _start(app)
    assert len(ex.calls) == 1 and out["final_state"] is IncidentState.CLOSED


def test_calibration_cap_can_be_lifted_explicitly() -> None:
    untrusted = Calibrator(temperature=1.0, version="demo")
    app, ex = _graph(
        grant=_grant(AutonomyTier.T3), calibrator=untrusted, require_calibrated_autonomy=False
    )
    _start(app)
    assert len(ex.calls) == 1


def test_driver_applies_the_same_calibration_cap() -> None:
    run = asyncio.run(
        run_incident(
            draft=_draft(),
            events=[_event()],
            specialists=[Specialist(r) for r in _ROLES],
            commander=Commander([_hypothesis()]),
            planner=Planner(_plan()),
            executor=(ex := Executor()),
            grants=[_grant(AutonomyTier.T3)],
            calibrator=Calibrator(temperature=1.0, version="hand-picked"),
        )
    )
    assert ex.calls == []
    assert run.final_state is IncidentState.AWAITING_APPROVAL

"""The ingestion edge + incident pipeline (doc 01 §3, doc 03 §3, doc 05 §4).

These tests wire the front of the system into one runnable path:
``raw provider webhook → authenticate → dedup → normalize → redact → publish``
(the edge), then ``consume → correlate → run the incident graph`` (the pipeline).
They prove the C2 guarantee end to end — **secrets are redacted before anything
is published**, on every persisting field — and that the tenant-keyed bus keeps
incidents isolated. Everything runs offline against the real Sentry connector,
the real redactor, the real correlator, and the real LangGraph runtime.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from sentinel.confidence import Calibrator
from sentinel.connectors.base import IntegrationContext, RawSignal
from sentinel.connectors.sentry import SentryConnector
from sentinel.contracts import (
    EvidenceRef,
    Hypothesis,
    RemediationPlan,
    SpecialistFinding,
    SpecialistRole,
)
from sentinel.contracts.actions import ActionResult
from sentinel.contracts.autonomy import AutonomyGrant
from sentinel.correlator import correlate
from sentinel.edge import (
    IngestionEdge,
    InMemoryDedupStore,
    InMemoryEventBus,
    WebhookRejected,
    run_incident_pipeline,
)
from sentinel.enums import (
    ActionStatus,
    ActionType,
    AutonomyTier,
    IncidentState,
    IntegrationMethod,
)
from sentinel.graph import build_incident_graph
from sentinel.orchestrator import Commander, InvestigationContext, Planner, Specialist

_T = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- builders
def _sentry_raw(
    *, event_id: str = "ev_s1", issue_id: str = "777", title: str = "boom", culprit: str = "h.get"
) -> RawSignal:
    event: dict[str, object] = {
        "event_id": event_id,
        "issue_id": issue_id,
        "level": "error",
        "exception": {"values": [{"type": "ValueError"}]},
        "title": title,
        "culprit": culprit,
        "release": "abc123",
        "timestamp": 1750000000.0,
    }
    return RawSignal(
        integration_id="int_1",
        method=IntegrationMethod.WEBHOOK,
        payload={"data": {"event": event}},
        headers={},
        received_at=_T,
    )


def _ctx(tenant_id: str = "ten_a") -> IntegrationContext:
    return IntegrationContext(
        tenant_id=tenant_id,
        integration_id="int_1",
        provider="sentry",
        vault_ref=f"vault/{tenant_id}/sentry/int_1",
    )


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        hypothesis_id="h1",
        statement="deploy abc123 introduced an N+1 that exhausted the DB pool",
        supporting_roles=["change_deploy", "metric", "trace"],
        raw_likelihood=0.95,
        evidence=[EvidenceRef(kind="deploy", summary="abc123", observed_at=_T)],
    )


def _plan() -> RemediationPlan:
    return RemediationPlan(
        incident_id="inc_1",
        action_type="deploy.rollback",
        target="svc_api",
        reversible=True,
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
        return SpecialistFinding(
            incident_id=ctx.incident_id,
            role=self.role,
            status="ok",
            summary=f"{self.role} corroborates deploy abc123",
            suspected_cause="deploy abc123",
            self_confidence=0.9,
            evidence=[EvidenceRef(kind="deploy", summary="abc123", observed_at=_T)],
        )


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


class _RejectingConnector(SentryConnector):
    async def verify_webhook(self, raw: RawSignal, ctx: IntegrationContext) -> bool:
        return False


def _autonomous_app(rec: RecordingExecutor):  # type: ignore[no-untyped-def]
    specialists: list[Specialist] = [
        FakeSpecialist("change_deploy"),
        FakeSpecialist("metric"),
        FakeSpecialist("trace"),
    ]
    commander: Commander = FakeCommander([_hypothesis()])
    planner: Planner = FakePlanner(_plan())
    return build_incident_graph(
        specialists=specialists,
        commander=commander,
        planner=planner,
        executor=rec,
        grant=_grant(AutonomyTier.T3),
        calibrator=Calibrator(temperature=1.0, version="cal.test"),
        clock=lambda: _T,
    )


# --------------------------------------------------------------------------- tests
def test_ingest_redacts_secrets_before_publishing() -> None:
    bus = InMemoryEventBus()
    edge = IngestionEdge(bus=bus, dedup=InMemoryDedupStore())
    aws = "AKIAIOSFODNN7EXAMPLE"
    ghp = "ghp_" + "a" * 36
    raw = _sentry_raw(title=f"NullPointer near {aws}", culprit=ghp)

    published = asyncio.run(edge.ingest(raw, _ctx(), SentryConnector()))

    assert len(published) == 1
    ev = published[0]
    # the title is redacted in place — the raw key never persists
    assert aws not in ev.title and "[REDACTED:aws_access_key_id]" in ev.title
    # the culprit label carried a token; it is redacted too (every persisting field)
    assert ghp not in str(ev.labels)
    # the redacted body holds no secret either
    assert aws not in ev.body_redacted and ghp not in ev.body_redacted
    # meta records each detected type (types→counts, never values; doc 05 §4.3)
    assert "aws_access_key_id" in ev.redaction_meta
    assert "github_pat" in ev.redaction_meta
    # it actually reached the tenant-keyed bus
    assert bus.tenant_events("ten_a") == [ev]


def test_duplicate_delivery_is_deduped() -> None:
    bus = InMemoryEventBus()
    edge = IngestionEdge(bus=bus, dedup=InMemoryDedupStore())
    raw = _sentry_raw()

    first = asyncio.run(edge.ingest(raw, _ctx(), SentryConnector()))
    second = asyncio.run(edge.ingest(raw, _ctx(), SentryConnector()))

    assert len(first) == 1 and second == []  # retry collapses (doc 03 §3)
    assert len(bus.tenant_events("ten_a")) == 1


def test_unverified_webhook_is_rejected() -> None:
    bus = InMemoryEventBus()
    edge = IngestionEdge(bus=bus, dedup=InMemoryDedupStore())

    with pytest.raises(WebhookRejected):
        asyncio.run(edge.ingest(_sentry_raw(), _ctx(), _RejectingConnector()))

    assert bus.tenant_events("ten_a") == []  # nothing persisted on a bad signature


def test_pipeline_collapses_storm_into_one_closed_incident() -> None:
    bus = InMemoryEventBus()
    edge = IngestionEdge(bus=bus, dedup=InMemoryDedupStore())
    # Two deliveries, same Sentry issue (→ same fingerprint → correlate) but distinct
    # delivery ids (→ both ingest, not deduped).
    conn = SentryConnector()
    asyncio.run(edge.ingest(_sentry_raw(event_id="ev_a", issue_id="777"), _ctx(), conn))
    asyncio.run(edge.ingest(_sentry_raw(event_id="ev_b", issue_id="777"), _ctx(), conn))
    assert len(bus.tenant_events("ten_a")) == 2

    rec = RecordingExecutor(ActionResult(action_id="act_1", status=ActionStatus.SUCCEEDED))
    app = _autonomous_app(rec)
    ids = iter(["inc_1", "inc_2", "inc_3"])
    results = asyncio.run(
        run_incident_pipeline(
            events=bus.tenant_events("ten_a"),
            app=app,
            window_seconds=300.0,
            new_incident_id=lambda: next(ids),
        )
    )

    assert len(results) == 1  # the storm collapsed to ONE incident
    assert results[0]["final_state"] is IncidentState.CLOSED
    assert len(rec.calls) == 1  # acted exactly once for the incident


def test_tenant_keyed_bus_isolates_incidents() -> None:
    bus = InMemoryEventBus()
    edge = IngestionEdge(bus=bus, dedup=InMemoryDedupStore())
    # Same Sentry issue id across two tenants must NEVER share an incident.
    conn = SentryConnector()
    asyncio.run(edge.ingest(_sentry_raw(event_id="a", issue_id="777"), _ctx("ten_a"), conn))
    asyncio.run(edge.ingest(_sentry_raw(event_id="b", issue_id="777"), _ctx("ten_b"), conn))

    a_events = bus.tenant_events("ten_a")
    b_events = bus.tenant_events("ten_b")
    assert len(a_events) == 1 and len(b_events) == 1
    assert all(e.tenant_id == "ten_a" for e in a_events)
    assert all(e.tenant_id == "ten_b" for e in b_events)
    # correlation over one partition yields only that tenant's incident
    a_drafts = correlate(a_events, window_seconds=300.0, new_incident_id=lambda: "inc_a")
    assert len(a_drafts) == 1 and a_drafts[0].tenant_id == "ten_a"

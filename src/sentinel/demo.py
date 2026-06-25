"""End-to-end demo: a 3 a.m. provider webhook → diagnosis → fix-or-page.

Run it:  ``./.venv/bin/python -m sentinel.demo``

It drives the *real* ingestion edge, correlator, escalation engine, and LangGraph
runtime — only the LLM agents are deterministic stubs, so the whole thing runs
offline and free. Two scenarios show the two halves of the product:

* **T3 (autonomous):** high-confidence, reversible → Sentinel rolls back and closes.
* **T1 (suggest):** same diagnosis, but the tenant only granted "suggest" → it
  pages a human with the fix attached (the 15-second-approval card).

Flip ``SENTINEL_USE_LLM=1`` with a key in ``.env`` to swap the stubs for Claude
(not wired in this demo — see the README).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Literal, cast

from langgraph.graph.state import CompiledStateGraph

from sentinel.confidence import Calibrator
from sentinel.connectors.base import IntegrationContext, RawSignal
from sentinel.connectors.sentry import SentryConnector
from sentinel.contracts import (
    AutonomyGrant,
    ConfidenceAssessment,
    EscalationDecision,
    EvidenceRef,
    Hypothesis,
    PagePayload,
    RemediationPlan,
    SpecialistFinding,
    SpecialistRole,
)
from sentinel.contracts.actions import ActionResult
from sentinel.edge import (
    IngestionEdge,
    InMemoryDedupStore,
    InMemoryEventBus,
    run_incident_pipeline,
)
from sentinel.enums import (
    ActionStatus,
    ActionType,
    AutonomyTier,
    IncidentState,
    IntegrationMethod,
)
from sentinel.graph import IncidentGraphState, build_incident_graph
from sentinel.notify import render_page
from sentinel.orchestrator import (
    ActionExecutor,
    Commander,
    InvestigationContext,
    Planner,
    Specialist,
)

_T = datetime(2026, 6, 17, 3, 14, tzinfo=UTC)
_TENANT = "ten_demo"
_ROOT_CAUSE = "deploy abc123 introduced an N+1 query that exhausted the DB connection pool"
_ROLES: tuple[SpecialistRole, ...] = ("change_deploy", "metric", "trace", "log", "error")

_EvidenceKind = Literal["log", "metric", "trace", "deploy", "error", "alert"]

# What each specialist independently observes — distinct signals that corroborate.
_EVIDENCE: dict[SpecialistRole, tuple[_EvidenceKind, str]] = {
    "change_deploy": ("deploy", "release abc123 shipped 40s before the error spike"),
    "metric": ("metric", "DB pool-checkout p99 jumped 12ms → 9.4s in the same minute"),
    "trace": ("trace", "87% of slow spans hit one repeated N+1 ORM call path"),
    "log": ("log", "'QueuePool limit reached' logged ~1,200×/min after the deploy"),
    "error": ("error", "OperationalError rate went 0 → 340/min post-release"),
}


# --------------------------------------------------------------------------- demo stubs
class _Specialist:
    def __init__(self, role: SpecialistRole) -> None:
        self.role = role

    async def investigate(self, ctx: InvestigationContext) -> SpecialistFinding:
        kind, observation = _EVIDENCE[self.role]
        return SpecialistFinding(
            incident_id=ctx.incident_id,
            role=self.role,
            summary=f"{self.role}: {observation}",
            suspected_cause="deploy abc123 — N+1 query exhausted the DB connection pool",
            self_confidence=0.9,
            evidence=[EvidenceRef(kind=kind, summary=observation, observed_at=_T)],
        )


class _Commander:
    def __init__(self, hypotheses: list[Hypothesis]) -> None:
        self._hypotheses = hypotheses

    async def synthesize(
        self, incident_id: str, findings: Sequence[SpecialistFinding]
    ) -> list[Hypothesis]:
        return self._hypotheses


class _Planner:
    def __init__(self, plan: RemediationPlan) -> None:
        self._plan = plan

    async def draft(self, incident_id: str, hypothesis: Hypothesis) -> RemediationPlan:
        return self._plan


class _Executor:
    def __init__(self) -> None:
        self.acted = False

    async def execute(
        self, plan: RemediationPlan, *, tenant_id: str, incident_id: str
    ) -> ActionResult:
        self.acted = True
        return ActionResult(action_id="act_demo", status=ActionStatus.SUCCEEDED)


# --------------------------------------------------------------------------- builders
def _hypothesis(
    *,
    hid: str = "h1",
    likelihood: float = 0.95,
    statement: str = _ROOT_CAUSE,
    supporting: list[SpecialistRole] | None = None,
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hid,
        statement=statement,
        supporting_roles=(
            supporting if supporting is not None else ["change_deploy", "metric", "trace"]
        ),
        raw_likelihood=likelihood,
        evidence=[EvidenceRef(kind="deploy", summary="release abc123", observed_at=_T)],
    )


def _plan() -> RemediationPlan:
    return RemediationPlan(
        incident_id="inc_demo",
        action_type="deploy.rollback",
        target="svc_api",
        reversible=True,
        summary="roll svc_api back to the prior release (abc122) to clear the pool exhaustion",
        rollback_ref="rel_abc122",
        estimated_blast_radius="single_service",
    )


def _grant(tier: AutonomyTier) -> AutonomyGrant:
    return AutonomyGrant(
        grant_id="grant_demo",
        tenant_id=_TENANT,
        action_type=ActionType.DEPLOY_ROLLBACK,
        max_tier=tier,
        granted_by="founder",
        granted_at=_T,
    )


def _sentry_raw(*, event_id: str) -> RawSignal:
    # A real-shaped Sentry webhook — including a Postgres DSN with a password, to
    # show the edge redacting secrets before anything persists (C2).
    event: dict[str, object] = {
        "event_id": event_id,
        "issue_id": "777",
        "level": "error",
        "exception": {"values": [{"type": "OperationalError"}]},
        "title": "OperationalError: QueuePool limit of size 20 overflow 10 reached",
        "culprit": "db.session.execute",
        "release": "abc123",
        "timestamp": 1_750_000_000.0,
        "context": {"dsn": "postgresql://app:S3cr3tP@ss@db.internal:5432/prod"},
    }
    return RawSignal(
        integration_id="int_demo",
        method=IntegrationMethod.WEBHOOK,
        payload={"data": {"event": event}},
        headers={},
        received_at=_T,
    )


def _ctx() -> IntegrationContext:
    return IntegrationContext(
        tenant_id=_TENANT,
        integration_id="int_demo",
        provider="sentry",
        vault_ref="vault/ten_demo/sentry/int_demo",
    )


def _build_graph(
    *, grant: AutonomyGrant, hypotheses: list[Hypothesis], executor: ActionExecutor
) -> CompiledStateGraph[IncidentGraphState]:
    specialists: list[Specialist] = [_Specialist(role) for role in _ROLES]
    commander: Commander = _Commander(hypotheses)
    planner: Planner = _Planner(_plan())
    return build_incident_graph(
        specialists=specialists,
        commander=commander,
        planner=planner,
        executor=executor,
        grant=grant,
        calibrator=Calibrator(temperature=1.0, version="cal.demo"),
        clock=lambda: _T,
    )


def _print_investigation(
    sink: Callable[[str], None], title: str, state: dict[str, object]
) -> None:
    findings = cast(list[SpecialistFinding], state["findings"])
    hypotheses = cast(list[Hypothesis], state["hypotheses"])
    assessment = cast(ConfidenceAssessment, state["assessment"])
    decision = cast(EscalationDecision, state["decision"])
    final_state = cast(IncidentState, state["final_state"])

    sink("")
    sink(f"── {title} " + "─" * max(0, 52 - len(title)))
    sink(f"  {len(findings)} specialists investigated in parallel")
    sink(f"  root cause:  {hypotheses[0].statement}")
    sink(
        f"  confidence:  {assessment.band.value} "
        f"(calibrated {assessment.calibrated_confidence:.0%})"
    )
    sink(f"  decision:    {decision.outcome}  →  {final_state.value}")
    result = state.get("action_result")
    if result is not None:
        action = cast(ActionResult, result)
        sink(f"  action:      executed {decision.outcome} → {action.status.value}")


async def run_demo(*, sink: Callable[[str], None] = print) -> dict[str, IncidentState]:
    """Run the two demo scenarios end to end; return ``{name: final_state}``."""
    sink("=" * 64)
    sink("  Project Sentinel — 3 a.m. incident, end to end (offline demo)")
    sink("=" * 64)

    # 1. Ingestion edge: authenticate → dedup → redact → normalize → publish.
    bus = InMemoryEventBus()
    edge = IngestionEdge(bus=bus, dedup=InMemoryDedupStore())
    connector = SentryConnector()
    for i in range(3):
        await edge.ingest(_sentry_raw(event_id=f"ev_{i}"), _ctx(), connector)
    events = bus.tenant_events(_TENANT)
    sink("")
    sink(f"Edge: ingested {len(events)} provider webhooks for {_TENANT}.")
    sink(f"  secrets redacted before persistence (C2): {events[0].redaction_meta}")

    outcomes: dict[str, IncidentState] = {}

    # Scenario A — the tenant granted T3: autonomous, reversible, high-confidence → act.
    executor = _Executor()
    app_a = _build_graph(
        grant=_grant(AutonomyTier.T3), hypotheses=[_hypothesis()], executor=executor
    )
    state_a = (
        await run_incident_pipeline(
            events=events, app=app_a, window_seconds=300.0, new_incident_id=lambda: "inc_demo_a"
        )
    )[0]
    _print_investigation(sink, "Scenario A — T3 autonomous", state_a)
    outcomes["autonomous"] = cast(IncidentState, state_a["final_state"])

    # Scenario B — the tenant granted only T1 (suggest): same diagnosis, but page a human.
    app_b = _build_graph(
        grant=_grant(AutonomyTier.T1),
        hypotheses=[
            _hypothesis(),
            _hypothesis(
                hid="h2", likelihood=0.55, statement="a noisy-neighbor query on the shared DB"
            ),
        ],
        executor=_Executor(),
    )
    state_b = (
        await run_incident_pipeline(
            events=events, app=app_b, window_seconds=300.0, new_incident_id=lambda: "inc_demo_b"
        )
    )[0]
    _print_investigation(sink, "Scenario B — T1 page-with-fix", state_b)
    page = state_b.get("page_payload")
    if page is not None:
        sink("")
        sink(render_page(cast(PagePayload, page)))
    outcomes["page"] = cast(IncidentState, state_b["final_state"])

    sink("")
    sink("=" * 64)
    return outcomes


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()

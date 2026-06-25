"""Run ONE incident through REAL Claude (opt-in — spends ~cents). Reports tokens + cost.

    PYTHONPATH=src ./.venv/bin/python -m sentinel.realrun

Requires ``ANTHROPIC_API_KEY`` in ``.env`` and ``SENTINEL_USE_LLM=1`` (else it
prints how to configure and exits without calling anything). Uses 3 specialists +
the commander on the cheapest model to keep the spend to a couple of cents, and a
T1 ("suggest") grant so it never touches prod — it pages with Claude's diagnosis
and a drafted fix.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast

from sentinel.agents import TokenMeter
from sentinel.agents.llm import build_llm_agents
from sentinel.confidence import Calibrator
from sentinel.config import load_settings
from sentinel.connectors.sentry import SentryConnector
from sentinel.contracts import (
    ConfidenceAssessment,
    EscalationDecision,
    Hypothesis,
    PagePayload,
    SpecialistFinding,
    SpecialistRole,
)
from sentinel.demo import _T, _TENANT, _ctx, _Executor, _grant, _plan, _Planner, _sentry_raw
from sentinel.edge import (
    IngestionEdge,
    InMemoryDedupStore,
    InMemoryEventBus,
    run_incident_pipeline,
)
from sentinel.enums import AutonomyTier, IncidentState
from sentinel.graph import build_incident_graph
from sentinel.notify import render_page

# 3 specialists keeps the real run to ~5 cheap calls.
_ROLES: tuple[SpecialistRole, ...] = ("change_deploy", "metric", "log")


async def run_real_incident(*, sink: Callable[[str], None] = print) -> dict[str, object]:
    settings = load_settings()
    if not settings.llm_ready:
        sink("LLM not configured — add ANTHROPIC_API_KEY to .env and set SENTINEL_USE_LLM=1.")
        return {}

    sink("=" * 64)
    sink(f"  REAL Claude run — model {settings.llm_model}  (this spends money)")
    sink("=" * 64)

    meter = TokenMeter(sink=sink)
    specialists, commander = build_llm_agents(settings=settings, meter=meter, roles=_ROLES)
    planner = _Planner(_plan())

    # Ingest a real-shaped incident; the edge redacts before the model sees anything.
    bus = InMemoryEventBus()
    edge = IngestionEdge(bus=bus, dedup=InMemoryDedupStore())
    connector = SentryConnector()
    for i in range(2):
        await edge.ingest(_sentry_raw(event_id=f"ev_{i}"), _ctx(), connector)
    events = bus.tenant_events(_TENANT)
    sink("")
    sink(f"Edge: {len(events)} events ingested; redaction meta = {events[0].redaction_meta}")
    sink("(Claude only ever sees the redacted content.)")
    sink("")

    app = build_incident_graph(
        specialists=specialists,
        commander=commander,
        planner=planner,
        executor=_Executor(),
        grant=_grant(AutonomyTier.T1),  # suggest-only: never acts, always pages with the fix
        calibrator=Calibrator(temperature=1.0, version="cal.real"),
        clock=lambda: _T,
    )
    states = await run_incident_pipeline(
        events=events, app=app, window_seconds=300.0, new_incident_id=lambda: "inc_real"
    )
    state = states[0]

    findings = cast(list[SpecialistFinding], state["findings"])
    hypotheses = cast(list[Hypothesis], state["hypotheses"])
    assessment = cast(ConfidenceAssessment, state["assessment"])
    decision = cast(EscalationDecision, state["decision"])
    final_state = cast(IncidentState, state["final_state"])

    sink("── Claude's investigation ────────────────────────────")
    for finding in findings:
        sink(f"  • {finding.role}: {finding.summary}")
    sink("")
    sink(f"  commander synthesized: {hypotheses[0].statement}")
    sink(
        f"  confidence: {assessment.band.value} "
        f"(calibrated {assessment.calibrated_confidence:.0%})"
    )
    sink(f"  decision:   {decision.outcome}  →  {final_state.value}")

    page = state.get("page_payload")
    if page is not None:
        sink("")
        sink(render_page(cast(PagePayload, page)))

    sink("")
    sink("=" * 64)
    sink(f"  COST: {meter.summary()}")
    sink("=" * 64)
    return {
        "final_state": final_state,
        "cost_usd": meter.total_cost_usd,
        "calls": meter.call_count,
    }


def main() -> None:
    asyncio.run(run_real_incident())


if __name__ == "__main__":
    main()

"""The incident pipeline: consume → correlate → run the graph (doc 01 §3, §5).

The back half of the ingestion path. Given one tenant's published events (drained
from the bus), it first correlates them against the tenant's **open** incidents
(:func:`~sentinel.correlator.correlate_with_open`) — a late alert joins the
incident it belongs to instead of opening a duplicate — and runs each genuinely
new incident through the durable incident graph
(:func:`~sentinel.graph.build_incident_graph`), one checkpoint thread per incident.

Every incident comes back with an explicit status. A run that stopped at the
approval ``interrupt()`` is ``awaiting_approval`` — paused, not finished — and the
approval endpoint continues it with :func:`resume_incident`.

It takes the already-consumed events (not the bus) so it stays decoupled from the
transport: in production a Kafka consumer hands events here exactly as the
in-memory bus's ``tenant_events`` does in tests. Pass the same ``registry`` to
every call (and to :func:`resume_incident`): it is what links one batch to the
next.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from sentinel.contracts import CanonicalEvent
from sentinel.correlator import correlate_with_open
from sentinel.enums import IncidentState
from sentinel.graph import IncidentGraphState, initial_state
from sentinel.ids import new_id
from sentinel.incidents.registry import (
    IncidentRegistry,
    IncidentStatus,
    TrackedIncident,
)


def _new_incident_id() -> str:
    return new_id("incident")


@dataclass(frozen=True)
class PipelineResult:
    """What happened to one incident in this batch.

    ``attached`` means the events joined an incident that was already open; the
    graph did not run again. Otherwise ``state`` is the graph's state after the
    run, and ``action_id`` is the one-tap token while ``awaiting_approval``.
    """

    incident_id: str
    status: IncidentStatus | Literal["attached"]
    state: dict[str, object] | None = None
    action_id: str | None = None
    attached_event_ids: tuple[str, ...] = ()


class IncidentNotAwaitingApproval(LookupError):
    """Raised when an approval targets an incident that is not paused for one."""


def _status_of(state: dict[str, object]) -> IncidentStatus:
    if state.get("__interrupt__"):
        return "awaiting_approval"
    final = state.get("final_state")
    if final is IncidentState.CLOSED:
        return "closed"
    if final is IncidentState.ESCALATED:
        return "escalated"
    return "running"


def _settle(incident_id: str, state: dict[str, object]) -> PipelineResult:
    status = _status_of(state)
    action_id = state.get("action_id") if status == "awaiting_approval" else None
    return PipelineResult(
        incident_id=incident_id,
        status=status,
        state=state,
        action_id=action_id if isinstance(action_id, str) else None,
    )


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


async def _record(registry: IncidentRegistry, fallback: TrackedIncident, **fields: object) -> None:
    """Update an incident's tracked fields, keeping any events attached meanwhile."""
    current = await registry.get(fallback.incident_id) or fallback
    await registry.save(current.model_copy(update=fields))


async def run_incident_pipeline(
    *,
    events: Sequence[CanonicalEvent],
    app: CompiledStateGraph[IncidentGraphState],
    registry: IncidentRegistry,
    window_seconds: float,
    new_incident_id: Callable[[], str] = _new_incident_id,
) -> list[PipelineResult]:
    """Correlate one tenant's events and run each new incident through the graph.

    Returns one result per incident touched: ``attached`` ones first, then new
    incidents in correlation order. Callers pass a single tenant's events;
    isolation is the bus's job (doc 01 §4).
    """
    if not events:
        return []

    open_incidents: dict[str, TrackedIncident] = {}
    for tenant_id in sorted({e.tenant_id for e in events}):
        for incident in await registry.open_for(tenant_id):
            open_incidents[incident.incident_id] = incident

    correlation = correlate_with_open(
        events,
        {iid: inc.events for iid, inc in open_incidents.items()},
        window_seconds=window_seconds,
        new_incident_id=new_incident_id,
    )
    by_id = {event.event_id: event for event in events}

    results: list[PipelineResult] = []
    for incident_id, fresh in correlation.attached.items():
        incident = open_incidents[incident_id]
        await registry.save(incident.model_copy(update={"events": [*incident.events, *fresh]}))
        results.append(
            PipelineResult(
                incident_id=incident_id,
                status="attached",
                attached_event_ids=tuple(e.event_id for e in fresh),
            )
        )

    for draft in correlation.drafts:
        draft_events = [by_id[event_id] for event_id in draft.event_ids if event_id in by_id]
        # Registered as running *before* the graph runs, so alerts that arrive
        # while it investigates attach to it instead of opening a twin.
        tracked = TrackedIncident(
            incident_id=draft.incident_id,
            tenant_id=draft.tenant_id,
            thread_id=draft.incident_id,
            status="running",
            events=draft_events,
            opened_at=draft.opened_at,
        )
        await registry.save(tracked)
        try:
            state = dict(
                await app.ainvoke(
                    initial_state(draft=draft, events=draft_events), _config(tracked.thread_id)
                )
            )
        except BaseException:
            # Not left "running": that would swallow every later alert for it.
            await _record(registry, tracked, status="failed")
            raise
        result = _settle(draft.incident_id, state)
        await _record(registry, tracked, status=result.status, action_id=result.action_id)
        results.append(result)
    return results


async def resume_incident(
    *,
    app: CompiledStateGraph[IncidentGraphState],
    registry: IncidentRegistry,
    incident_id: str,
    approval: object,
) -> PipelineResult:
    """Continue an incident paused for approval with the human's response.

    ``approval`` is passed to the graph's approval gate as-is; the gate decides
    whether it approves (see :func:`sentinel.graph.nodes._parse_approval`). Only
    an incident the registry shows as ``awaiting_approval`` can be resumed, so a
    second response to the same page is refused rather than replayed.
    """
    tracked = await registry.get(incident_id)
    if tracked is None or tracked.status != "awaiting_approval":
        raise IncidentNotAwaitingApproval(incident_id)
    state = dict(await app.ainvoke(Command(resume=approval), _config(tracked.thread_id)))
    result = _settle(incident_id, state)
    await _record(registry, tracked, status=result.status, action_id=result.action_id)
    return result

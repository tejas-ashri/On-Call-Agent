"""The incident pipeline: consume → correlate → run the graph (doc 01 §3, §5).

The back half of the ingestion path. Given one tenant's published events (drained
from the bus), it collapses the storm into ``IncidentDraft``s
(:func:`~sentinel.correlator.correlate`) and runs each through the durable
incident graph (:func:`~sentinel.graph.build_incident_graph`), one checkpoint
thread per incident. It closes the loop from a raw provider webhook to a
remediated or escalated incident, entirely offline.

It takes the already-consumed events (not the bus) so it stays decoupled from the
transport: in production a Kafka consumer hands events here exactly as the
in-memory bus's ``tenant_events`` does in tests.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from sentinel.contracts import CanonicalEvent
from sentinel.correlator import correlate
from sentinel.graph import IncidentGraphState, initial_state
from sentinel.ids import new_id


def _new_incident_id() -> str:
    return new_id("incident")


async def run_incident_pipeline(
    *,
    events: Sequence[CanonicalEvent],
    app: CompiledStateGraph[IncidentGraphState],
    window_seconds: float,
    new_incident_id: Callable[[], str] = _new_incident_id,
) -> list[dict[str, object]]:
    """Correlate one tenant's events into incidents and run each through the graph.

    Returns the final graph state per incident (in correlation order). Callers
    pass a single tenant's events; isolation is the bus's job (doc 01 §4).
    """
    if not events:
        return []

    drafts = correlate(list(events), window_seconds=window_seconds, new_incident_id=new_incident_id)
    by_id = {event.event_id: event for event in events}

    results: list[dict[str, object]] = []
    for draft in drafts:
        draft_events = [by_id[event_id] for event_id in draft.event_ids if event_id in by_id]
        config: RunnableConfig = {"configurable": {"thread_id": draft.incident_id}}
        state = await app.ainvoke(initial_state(draft=draft, events=draft_events), config)
        results.append(dict(state))
    return results

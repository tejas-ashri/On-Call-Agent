"""The incident graph's state schema (doc 04 §4).

LangGraph threads a single mutable state object through the nodes; nodes return
*partial* updates that LangGraph merges. The two channels that accumulate across
nodes — ``findings`` (filled by the parallel specialist super-step) and
``event_log`` (the event-sourced lifecycle stream) — carry an ``operator.add``
reducer so concurrent/sequential appends combine rather than overwrite. Every
other field is last-write-wins.

``seq`` is the monotonic event sequence. It is only ever advanced by *sequential*
nodes (the parallel specialists emit no lifecycle events, only findings), so the
stream stays gap-free from 1 — exactly the invariant :func:`sentinel.incidents.
projection.project` requires to replay the log into the same state the graph
reports.
"""

from __future__ import annotations

import operator
from collections.abc import Sequence
from typing import Annotated, TypedDict

from sentinel.contracts import (
    CanonicalEvent,
    ConfidenceAssessment,
    EscalationDecision,
    Hypothesis,
    PagePayload,
    RemediationPlan,
    SpecialistFinding,
)
from sentinel.contracts.actions import ActionResult
from sentinel.correlator import IncidentDraft
from sentinel.enums import IncidentState
from sentinel.incidents.events import IncidentEvent


class IncidentGraphState(TypedDict, total=False):
    """The state threaded through the incident graph (doc 04 §4)."""

    # inputs — set once by initial_state, never rewritten
    draft: IncidentDraft
    signal_events: list[CanonicalEvent]
    # accumulating channels (reducers)
    findings: Annotated[list[SpecialistFinding], operator.add]
    event_log: Annotated[list[IncidentEvent], operator.add]
    # working fields (last-write-wins)
    seq: int
    n_competing: int
    hypotheses: list[Hypothesis]
    assessment: ConfidenceAssessment | None
    decision: EscalationDecision | None
    plan: RemediationPlan | None
    page_payload: PagePayload | None
    action_result: ActionResult | None
    approval: bool | None
    final_state: IncidentState | None


def initial_state(
    *, draft: IncidentDraft, events: Sequence[CanonicalEvent]
) -> IncidentGraphState:
    """Build the start state for one incident-graph run."""
    return IncidentGraphState(
        draft=draft,
        signal_events=list(events),
        findings=[],
        event_log=[],
        seq=0,
        n_competing=0,
        hypotheses=[],
        assessment=None,
        decision=None,
        plan=None,
        page_payload=None,
        action_result=None,
        approval=None,
        final_state=None,
    )

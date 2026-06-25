"""Assemble the durable incident graph (doc 04 §2, §9).

``build_incident_graph`` wires the nodes from :mod:`sentinel.graph.nodes` into a
LangGraph ``StateGraph`` and compiles it with a checkpointer — the durability
substrate (ADR-006) that makes a run resumable across the human-in-the-loop
approval interrupt. The same injected ports as
:func:`sentinel.orchestrator.run.run_incident` are accepted, so the graph runs
offline with deterministic fakes and unchanged in production with LLM-backed
agents and a :class:`~sentinel.mcp.broker.ToolBroker`-backed executor.

Control flow::

    START → intake → (specialist_0 … specialist_n) → collect → synthesize
          → score → plan_and_decide ─┬─ act_and_report ───────→ act → END
                                     ├─ page_one_tap → await_approval ─[interrupt]
                                     │                    ├─ approved → act → END
                                     │                    └─ denied  → END
                                     └─ page_*  ──────────────────────────→ END

The default checkpointer is an in-memory saver (offline + tests). The production
swap is the Postgres checkpointer (doc 02 / doc 04 §9): pass it as ``checkpointer``
and nothing else changes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from sentinel.confidence import Calibrator
from sentinel.contracts import AutonomyGrant
from sentinel.graph.nodes import (
    Node,
    _Deps,
    make_act,
    make_await_approval,
    make_collect,
    make_intake,
    make_plan_and_decide,
    make_score,
    make_specialist,
    make_synthesize,
)
from sentinel.graph.state import IncidentGraphState
from sentinel.orchestrator.protocols import ActionExecutor, Commander, Planner, Specialist


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _register(g: StateGraph[IncidentGraphState], name: str, node: Node) -> None:
    """Attach a node, localizing LangGraph's one type-checking gap.

    ``StateGraph.add_node`` is overloaded over a *contravariant*, ``StateLike``-
    bound ``TypeVar`` (``langgraph.graph._node``); mypy cannot solve that variable
    when matching our ``Callable`` node alias against LangGraph's ``_Node``
    Protocol — a limitation the LangGraph authors flag as WIP in that module. The
    call is correct at runtime (the graph test suite proves it end to end), so the
    single unavoidable suppression lives here rather than at every call site.
    """
    g.add_node(name, node)  # type: ignore[call-overload]


def _route_after_decide(state: IncidentGraphState) -> str:
    decision = state["decision"]
    assert decision is not None
    if decision.outcome == "act_and_report":
        return "act"
    if decision.target_state == "AWAITING_APPROVAL":
        return "await_approval"
    return "page"  # already finalized (ESCALATED) in plan_and_decide → END


def _route_after_approval(state: IncidentGraphState) -> str:
    return "act" if state.get("approval") else "end"


def build_incident_graph(
    *,
    specialists: Sequence[Specialist],
    commander: Commander,
    planner: Planner,
    executor: ActionExecutor,
    grant: AutonomyGrant,
    calibrator: Calibrator,
    competing_margin: float = 0.15,
    clock: Callable[[], datetime] = _utcnow,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[IncidentGraphState]:
    """Build and compile the durable incident graph wired with the given ports."""
    deps = _Deps(
        commander=commander,
        planner=planner,
        executor=executor,
        grant=grant,
        calibrator=calibrator,
        competing_margin=competing_margin,
        clock=clock,
    )

    g: StateGraph[IncidentGraphState] = StateGraph(IncidentGraphState)
    _register(g, "intake", make_intake(deps))
    _register(g, "collect", make_collect(deps))
    _register(g, "synthesize", make_synthesize(deps))
    _register(g, "score", make_score(deps))
    _register(g, "plan_and_decide", make_plan_and_decide(deps))
    _register(g, "await_approval", make_await_approval(deps))
    _register(g, "act", make_act(deps))

    g.add_edge(START, "intake")

    # Parallel specialist fan-out: one node per specialist, converging at collect.
    specialist_names: list[str] = []
    for i, specialist in enumerate(specialists):
        name = f"specialist_{i}"
        _register(g, name, make_specialist(specialist))
        g.add_edge("intake", name)
        g.add_edge(name, "collect")
        specialist_names.append(name)
    if not specialist_names:  # degenerate: no specialists → straight to collect
        g.add_edge("intake", "collect")

    g.add_edge("collect", "synthesize")
    g.add_edge("synthesize", "score")
    g.add_edge("score", "plan_and_decide")
    g.add_conditional_edges(
        "plan_and_decide",
        _route_after_decide,
        {"act": "act", "await_approval": "await_approval", "page": END},
    )
    g.add_conditional_edges("await_approval", _route_after_approval, {"act": "act", "end": END})
    g.add_edge("act", END)

    return g.compile(checkpointer=checkpointer or InMemorySaver())

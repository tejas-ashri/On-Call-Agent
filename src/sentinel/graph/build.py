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
and nothing else changes. A graph that accepts approvals must be given one
explicitly (see :func:`build_incident_graph`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from sentinel.confidence import Calibrator
from sentinel.contracts import AutonomyGrant
from sentinel.escalation import GrantLookup, as_grant_lookup
from sentinel.graph.nodes import (
    AuditSink,
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
from sentinel.notify import Notifier
from sentinel.orchestrator.ledger import ActionLedger, InMemoryActionLedger
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
    grants: GrantLookup | Sequence[AutonomyGrant],
    calibrator: Calibrator,
    competing_margin: float = 0.15,
    clock: Callable[[], datetime] = _utcnow,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    notifier: Notifier | None = None,
    cost_breaker_open: Callable[[str], bool] | None = None,
    approval_ttl: timedelta | None = timedelta(hours=1),
    audit: AuditSink | None = None,
    ledger: ActionLedger | None = None,
    approvers: Callable[[str, str], bool] | None = None,
    require_calibrated_autonomy: bool = True,
) -> CompiledStateGraph[IncidentGraphState]:
    """Build and compile the durable incident graph wired with the given ports.

    ``notifier`` delivers every page the graph produces. ``cost_breaker_open``
    (``tenant_id → open?``) forces T0 while a tenant is over budget (doc 07 §5).
    An approval arriving more than ``approval_ttl`` after it was requested is
    treated as a denial. ``audit`` records each decision, approval and action.

    ``grants`` is either the tenants' grants or a live ``(tenant_id, action_type) →
    grant`` lookup; each decision uses the grant for its own action type, and
    ``act`` re-checks it. ``ledger`` makes execution at-most-once per action (the
    in-memory default is not durable — pass a Postgres-backed one in production).
    ``approvers`` (``tenant_id, approver → allowed?``) decides who may approve;
    without it every approval is denied.

    A T3 grant acts autonomously only if ``calibrator`` is trusted for autonomy
    (fitted on enough labelled incidents, validated on held-out ones — see
    :meth:`~sentinel.confidence.Calibrator.trusted_for_autonomy`); otherwise T3 is
    treated as T2. ``require_calibrated_autonomy=False`` lifts that cap, for demos.

    An incident waiting for approval lives only in the checkpointer, so a graph
    that can accept approvals needs a durable one (the Postgres saver): with
    ``approvers`` set, ``checkpointer`` must be passed explicitly — the implicit
    in-memory default would lose every paused incident on restart. Passing an
    ``InMemorySaver`` deliberately (tests, demos) is allowed.
    """
    if approvers is not None and checkpointer is None:
        raise ValueError(
            "approvals need a durable checkpointer: pass checkpointer= explicitly "
            "(e.g. the Postgres saver) when approvers are configured"
        )
    deps = _Deps(
        commander=commander,
        planner=planner,
        executor=executor,
        grants=as_grant_lookup(grants),
        ledger=ledger or InMemoryActionLedger(),
        calibrator=calibrator,
        competing_margin=competing_margin,
        clock=clock,
        notifier=notifier,
        cost_breaker_open=cost_breaker_open,
        approval_ttl=approval_ttl,
        audit=audit,
        approvers=approvers,
        autonomy_calibrated=(
            not require_calibrated_autonomy or calibrator.trusted_for_autonomy()
        ),
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

# Plan — LangGraph incident runtime (the durable, resumable orchestrator)

**Status:** in progress · **Slice:** make the incident graph *real* — durable checkpointing + a genuine human-in-the-loop interrupt at the T2 approval gate (doc 04 §2, §7, §9).

## Why this slice

`orchestrator/run_incident` is a faithful, runtime-agnostic realization of the
incident graph's control flow, but it is a straight-line driver: it cannot
*suspend* at an approval and *resume* later, and it has no durable checkpoint to
recover from. Those two properties — **HITL interrupt** and **durable resume** —
are the reason doc 04 specifies LangGraph (ADR-006). This slice backs the exact
same control flow with a real `StateGraph`, proving the behavior the pure driver
structurally cannot.

It reuses every logic module unchanged — escalation engine, confidence
calibration, the reversibility floor, the credential-broker seam, the
event-sourced projection. The graph is a *new realization*, not a refactor.

## Design decisions

- **State** — a `TypedDict` (`IncidentGraphState`) with `operator.add` reducers on
  the two accumulating channels (`findings`, `event_log`). Everything else is
  last-write-wins. LangGraph 1.x reads the `Annotated` reducers.
- **Parallel fan-out, sequential emission** — one node per injected specialist,
  all fanning out from `intake` and converging at `collect` (a real concurrent
  super-step). The specialist nodes return **findings only**; they never emit
  lifecycle events. Every lifecycle event is emitted by a *sequential* node, so
  the `seq` counter (held in state) stays gap-free from 1 — which is exactly what
  `incidents.project()` requires to replay. This is the key invariant.
- **The interrupt** — `await_approval` calls `interrupt()` as its first statement
  (verified: on resume LangGraph re-runs the node from the top, so any pre-
  interrupt side-effect would double-fire). `APPROVAL_REQUESTED` is therefore
  emitted by `plan_and_decide` (runs exactly once, before the suspend), so a
  projection of the log *while suspended* already reads `AWAITING_APPROVAL`.
- **Checkpointer** — `InMemorySaver` by default (offline + tests). The production
  swap is `langgraph-checkpoint-postgres` (doc 02 / doc 04 §9), wired in the
  persistence slice; the builder takes the checkpointer as a parameter so the
  swap is one argument.
- **Dependency injection** — the builder closes over the same injected ports as
  `run_incident` (`specialists, commander, planner, executor, grant, calibrator`),
  so the graph is fully testable offline with the existing deterministic fakes.

## Node graph

```
START → intake → (specialist_0 … specialist_n) → collect → synthesize
      → score → plan_and_decide
        ├─ act_and_report ───────────→ act ──→ END        (→ CLOSED | ESCALATED on failure)
        ├─ page_one_tap ─→ await_approval ─[interrupt]
        │                       ├─ approved → act → END
        │                       └─ denied  → END           (→ ESCALATED)
        └─ page_* ───────────────────→ page → END          (→ ESCALATED)
```

## Emission sequences (all replay through the existing `project()` unchanged)

- **Autonomous → CLOSED:** opened, triage_started, specialist_finding×N,
  hypothesis_formed, incident_diagnosed, remediation_started, monitoring_started,
  incident_resolved, incident_closed.
- **T2 suspended → AWAITING_APPROVAL:** … incident_diagnosed, approval_requested.
- **Resume approved → CLOSED:** … approval_requested, approval_granted,
  remediation_started, monitoring_started, incident_resolved, incident_closed.
- **Resume denied → ESCALATED:** … approval_requested, approval_denied.
- **Page (irreversible / low-confidence) → ESCALATED:** … incident_diagnosed,
  incident_escalated.

## Tests (TDD, RED first) — `tests/test_graph.py`

1. autonomous path runs to CLOSED; executor called once; no interrupt.
2. T2 first invoke **suspends** at the interrupt; `aget_state().next == ('await_approval',)`; executor not yet called; projection of the partial log is `AWAITING_APPROVAL` (durability).
3. resume **approved** → acts → CLOSED.
4. resume **denied** → ESCALATED; executor never called.
5. irreversible (`db.failover`) at T3 → page, no interrupt, ESCALATED, no action (floor wins).
6. low calibrated confidence at T3 → page, ESCALATED, no action.
7. emitted `event_log` replays via `project()` to the graph's `final_state` (autonomous + resumed-approved).
8. graph autonomous outcome **equals** `run_incident`'s (cross-realization equivalence).
9. the autonomous path drives the **real** `MCPRouter`/`ToolBroker` through the executor seam; tenant scoping correct; secret never audited.

## Files

- `src/sentinel/graph/state.py` — `IncidentGraphState` + `initial_state`.
- `src/sentinel/graph/nodes.py` — node factories (closures over a frozen `_Deps`).
- `src/sentinel/graph/build.py` — `build_incident_graph(...) → CompiledStateGraph`.
- `src/sentinel/graph/__init__.py` — public exports.
- `tests/test_graph.py` — the suite above.

## Out of scope (deliberately)

Real LLM specialists, the Postgres checkpointer wiring, and `Send`-based dynamic
fan-out (static per-specialist nodes are equivalent for the fixed 5-specialist
panel and keep the gate deterministic). Noted as follow-ons.

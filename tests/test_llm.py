"""LLM-backed agents — parsing logic, tested offline with a fake completer.

These never touch the network: the ``Completer`` seam is faked, so the
JSON-parsing and contract-building paths are verified for free. The real Anthropic
adapter (``anthropic_completer``) is glue exercised by the live run, not here.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from sentinel.agents.llm import Completer, LLMCommander, LLMPlanner, LLMSpecialist
from sentinel.contracts import CanonicalEvent, Hypothesis, SpecialistFinding, SpecialistRole
from sentinel.enums import EventKind, Severity
from sentinel.orchestrator import InvestigationContext
from sentinel.orchestrator.guard import safe_draft

_T = datetime(2026, 6, 17, 3, 14, tzinfo=UTC)


def _ctx() -> InvestigationContext:
    return InvestigationContext(
        incident_id="inc_1",
        tenant_id="ten_a",
        events=[
            CanonicalEvent(
                event_id="evt_1",
                tenant_id="ten_a",
                integration_id="int_1",
                source="sentry",
                kind=EventKind.ERROR,
                service="svc_api",
                severity=Severity.ERROR,
                title="OperationalError: QueuePool limit reached",
                fingerprint="fp1",
                occurred_at=_T,
                received_at=_T,
            )
        ],
    )


def _completer(*, spec: str = "{}", cmd: str = "{}") -> Completer:
    async def complete(label: str, system: str, user: str) -> str:
        return cmd if label == "commander" else spec

    return complete


def _finding() -> SpecialistFinding:
    return SpecialistFinding(
        incident_id="inc_1",
        role="metric",
        summary="DB pool exhausted",
        suspected_cause="deploy abc123",
        self_confidence=0.8,
    )


def test_specialist_parses_json_into_finding() -> None:
    comp = _completer(
        spec='{"summary": "DB pool exhausted after deploy abc123", '
        '"suspected_cause": "deploy abc123", "self_confidence": 0.8}'
    )
    finding = asyncio.run(LLMSpecialist("metric", complete=comp).investigate(_ctx()))
    assert finding.role == "metric"
    assert "DB pool exhausted" in finding.summary
    assert finding.suspected_cause == "deploy abc123"
    assert finding.self_confidence == 0.8


def test_specialist_tolerates_non_json_response() -> None:
    finding = asyncio.run(
        LLMSpecialist("log", complete=_completer(spec="sorry, I can't help")).investigate(_ctx())
    )
    assert finding.self_confidence == 0.5  # safe default
    assert finding.summary  # non-empty fallback
    assert finding.suspected_cause is None


def test_specialist_extracts_json_embedded_in_prose() -> None:
    comp = _completer(
        spec='Here:\n{"summary": "latency spike", "self_confidence": 0.3}\nhope it helps'
    )
    finding = asyncio.run(LLMSpecialist("trace", complete=comp).investigate(_ctx()))
    assert finding.summary == "latency spike"
    assert finding.self_confidence == 0.3


def _finding_as(role: SpecialistRole, cause: str) -> SpecialistFinding:
    return SpecialistFinding(
        incident_id="inc_1", role=role, summary=cause, suspected_cause=cause, self_confidence=0.8
    )


def test_commander_parses_hypothesis() -> None:
    comp = _completer(
        cmd='{"hypotheses": [{"statement": "deploy abc123 introduced an N+1 exhausting the '
        'DB pool", "likelihood": 0.9, "stances": {"metric": "supports"}}]}'
    )
    hypotheses = asyncio.run(LLMCommander(complete=comp).synthesize("inc_1", [_finding()]))
    assert len(hypotheses) == 1
    assert "abc123" in hypotheses[0].statement
    assert hypotheses[0].raw_likelihood == 0.9
    assert hypotheses[0].supporting_roles == ["metric"]


def test_commander_naming_a_different_cause_is_not_support() -> None:
    # Regression: any specialist with *a* suspected_cause used to count as supporting.
    findings = [
        _finding_as("change_deploy", "deploy abc123"),
        _finding_as("metric", "upstream DNS outage"),
        _finding_as("log", "deploy abc123"),
    ]
    comp = _completer(
        cmd='{"hypotheses": [{"statement": "deploy abc123", "likelihood": 0.8, "stances": '
        '{"change_deploy": "supports", "metric": "contradicts", "log": "supports"}}]}'
    )
    [top] = asyncio.run(LLMCommander(complete=comp).synthesize("inc_1", findings))
    assert top.supporting_roles == ["change_deploy", "log"]
    assert top.contradicting_roles == ["metric"]


def test_commander_ignores_stances_for_roles_that_did_not_report() -> None:
    comp = _completer(
        cmd='{"hypotheses": [{"statement": "x", "likelihood": 0.7, '
        '"stances": {"metric": "supports", "trace": "supports", "bogus": "supports"}}]}'
    )
    [top] = asyncio.run(LLMCommander(complete=comp).synthesize("inc_1", [_finding()]))
    assert top.supporting_roles == ["metric"]


def test_commander_returns_ranked_competing_hypotheses() -> None:
    comp = _completer(
        cmd='{"hypotheses": ['
        '{"statement": "DNS outage", "likelihood": 0.4},'
        '{"statement": "deploy abc123", "likelihood": 0.7},'
        '{"statement": "pool misconfig", "likelihood": 0.6},'
        '{"statement": "dropped: over the cap", "likelihood": 0.99}]}'
    )
    hypotheses = asyncio.run(LLMCommander(complete=comp).synthesize("inc_1", [_finding()]))
    assert [h.statement for h in hypotheses] == ["deploy abc123", "pool misconfig", "DNS outage"]
    assert [h.hypothesis_id for h in hypotheses] == ["h1", "h2", "h3"]


def test_commander_clamps_out_of_range_likelihood() -> None:
    comp = _completer(cmd='{"hypotheses": [{"statement": "x", "likelihood": 5}]}')
    hypotheses = asyncio.run(LLMCommander(complete=comp).synthesize("inc_1", [_finding()]))
    assert hypotheses[0].raw_likelihood == 1.0


def test_commander_unparseable_response_is_unsupported_and_zero_likelihood() -> None:
    hypotheses = asyncio.run(
        LLMCommander(complete=_completer(cmd="no json here")).synthesize("inc_1", [_finding()])
    )
    assert len(hypotheses) == 1
    assert hypotheses[0].raw_likelihood == 0.0
    assert hypotheses[0].supporting_roles == []


# ── prompt contents ──────────────────────────────────────────────────────────


def _event(i: int, *, body: str = "", labels: dict[str, str] | None = None) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=f"evt_{i}",
        tenant_id="ten_a",
        integration_id="int_1",
        source="sentry",
        kind=EventKind.ERROR,
        service="svc_api",
        severity=Severity.ERROR,
        title=f"error {i}",
        body_redacted=body,
        fingerprint=f"fp{i}",
        labels=labels or {},
        occurred_at=_T,
        received_at=_T,
    )


def _capturing() -> tuple[Completer, list[str]]:
    prompts: list[str] = []

    async def complete(label: str, system: str, user: str) -> str:
        prompts.append(user)
        return "{}"

    return complete, prompts


def test_specialist_prompt_includes_the_redacted_payload() -> None:
    comp, prompts = _capturing()
    ctx = InvestigationContext(
        incident_id="inc_1",
        tenant_id="ten_a",
        events=[_event(1, body='{"culprit": "orders.list", "stacktrace": "db.py:42"}')],
    )
    asyncio.run(LLMSpecialist("error", complete=comp).investigate(ctx))
    assert "db.py:42" in prompts[0]
    assert "orders.list" in prompts[0]


def test_specialist_prompt_bounds_payload_size_and_event_count() -> None:
    comp, prompts = _capturing()
    events = [_event(i, body="x" * 5000) for i in range(25)]
    ctx = InvestigationContext(incident_id="inc_1", tenant_id="ten_a", events=events)
    asyncio.run(LLMSpecialist("error", complete=comp).investigate(ctx))
    assert "x" * 1501 not in prompts[0]
    assert "[truncated]" in prompts[0]
    assert "error 19" in prompts[0] and "error 20" not in prompts[0]
    assert "(+5 more signal(s) omitted)" in prompts[0]


# ── planner ──────────────────────────────────────────────────────────────────


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        hypothesis_id="h1",
        statement="deploy dpl_9 introduced an N+1 query exhausting the DB pool",
        supporting_roles=["change_deploy", "metric"],
        raw_likelihood=0.9,
    )


def _deploy_events() -> list[CanonicalEvent]:
    return [_event(1, labels={"deployment.id": "dpl_9", "commit_sha": "a1b2c3d"})]


def _planner(reply: str) -> tuple[LLMPlanner, list[str]]:
    prompts: list[str] = []

    async def complete(label: str, system: str, user: str) -> str:
        assert label == "planner"
        prompts.append(user)
        return reply

    return LLMPlanner(complete=complete), prompts


def test_planner_builds_a_plan_grounded_in_the_signals() -> None:
    planner, prompts = _planner(
        '{"action_type": "deploy.rollback", "target": "dpl_9", "reversible": true, '
        '"summary": "Roll back dpl_9", "command": "vercel rollback dpl_9", '
        '"estimated_blast_radius": "single_service"}'
    )
    plan = asyncio.run(planner.draft("inc_1", _hypothesis(), _deploy_events()))
    assert plan.action_type == "deploy.rollback"
    assert plan.target == "dpl_9"
    assert plan.reversible is True
    assert plan.command == "vercel rollback dpl_9"
    assert plan.estimated_blast_radius == "single_service"
    # the model sees the hypothesis and the targets it may choose from
    assert "N+1" in prompts[0]
    assert "Candidate targets: svc_api, dpl_9, a1b2c3d" in prompts[0]


def test_planner_defaults_unstated_claims_to_the_conservative_side() -> None:
    planner, _ = _planner('{"action_type": "process.restart", "target": "svc_api"}')
    plan = asyncio.run(planner.draft("inc_1", _hypothesis(), _deploy_events()))
    assert plan.reversible is False
    assert plan.estimated_blast_radius == "global"
    assert plan.summary == "process.restart svc_api"


@pytest.mark.parametrize(
    "reply",
    [
        '{"action_type": "deploy.rollback", "target": "dpl_INVENTED"}',  # fabricated target
        '{"action_type": "rm -rf", "target": "dpl_9"}',  # unknown action
        '{"action_type": "none"}',  # nothing fits
        "I would roll it back.",  # unparseable
    ],
)
def test_planner_rejections_become_no_plan(reply: str) -> None:
    planner, _ = _planner(reply)
    assert asyncio.run(safe_draft(planner, "inc_1", _hypothesis(), _deploy_events())) is None


def test_planner_without_targetable_signals_makes_no_call() -> None:
    planner, prompts = _planner('{"action_type": "deploy.rollback", "target": "x"}')
    bare = _event(1).model_copy(update={"service": None})
    assert asyncio.run(safe_draft(planner, "inc_1", _hypothesis(), [bare])) is None
    assert prompts == []

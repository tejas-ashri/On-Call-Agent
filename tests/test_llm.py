"""LLM-backed agents — parsing logic, tested offline with a fake completer.

These never touch the network: the ``Completer`` seam is faked, so the
JSON-parsing and contract-building paths are verified for free. The real Anthropic
adapter (``anthropic_completer``) is glue exercised by the live run, not here.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sentinel.agents.llm import Completer, LLMCommander, LLMSpecialist
from sentinel.contracts import CanonicalEvent, SpecialistFinding, SpecialistRole
from sentinel.enums import EventKind, Severity
from sentinel.orchestrator import InvestigationContext

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

"""LLM-backed agents — parsing logic, tested offline with a fake completer.

These never touch the network: the ``Completer`` seam is faked, so the
JSON-parsing and contract-building paths are verified for free. The real Anthropic
adapter (``anthropic_completer``) is glue exercised by the live run, not here.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sentinel.agents.llm import Completer, LLMCommander, LLMSpecialist
from sentinel.contracts import CanonicalEvent, SpecialistFinding
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


def test_commander_parses_hypothesis() -> None:
    comp = _completer(
        cmd='{"statement": "deploy abc123 introduced an N+1 exhausting the DB pool", '
        '"confidence": 0.9}'
    )
    hypotheses = asyncio.run(LLMCommander(complete=comp).synthesize("inc_1", [_finding()]))
    assert len(hypotheses) == 1
    assert "abc123" in hypotheses[0].statement
    assert hypotheses[0].raw_likelihood == 0.9
    assert hypotheses[0].supporting_roles == ["metric"]  # the finding had a suspected_cause


def test_commander_clamps_out_of_range_confidence() -> None:
    comp = _completer(cmd='{"statement": "x", "confidence": 5}')
    hypotheses = asyncio.run(LLMCommander(complete=comp).synthesize("inc_1", [_finding()]))
    assert hypotheses[0].raw_likelihood == 1.0

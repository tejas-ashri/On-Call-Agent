"""Tool-using specialists — the investigation loop, tested offline.

The model is a scripted fake behind the ``ToolCompleter`` seam and the telemetry is
replayed from a golden case, so the loop, tool confinement, evidence grounding and
budgets are verified without network or provider credentials.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import get_args

import pytest

from sentinel.agents import TokenMeter
from sentinel.agents.llm import (
    InvestigatingSpecialist,
    LLMCommander,
    LLMSpecialist,
    ModelTurn,
    ToolCall,
    ToolResult,
    ToolResults,
    ToolSpec,
    TranscriptEntry,
    build_llm_agents,
    to_messages,
)
from sentinel.confidence import independent_corroboration
from sentinel.config import Settings
from sentinel.contracts import SpecialistFinding, SpecialistRole
from sentinel.eval.golden import GoldenCase, load_golden
from sentinel.orchestrator import InvestigationContext
from sentinel.tools import EvidenceKind, GoldenTelemetry, Observation, tools_for

_ROLES: tuple[SpecialistRole, ...] = get_args(SpecialistRole)


def _case(case_id: str) -> GoldenCase:
    return next(c for c in load_golden() if c.id == case_id)


def _ctx() -> InvestigationContext:
    return InvestigationContext(incident_id="inc_1", tenant_id="ten_a", events=[])


_Responder = Callable[[Sequence[TranscriptEntry], Sequence[ToolSpec]], ModelTurn]


class _Model:
    """A scripted fake model; records every transcript it was shown."""

    def __init__(self, respond: _Responder) -> None:
        self._respond = respond
        self.seen: list[list[TranscriptEntry]] = []

    async def __call__(
        self,
        label: str,
        system: str,
        transcript: Sequence[TranscriptEntry],
        tools: Sequence[ToolSpec],
    ) -> ModelTurn:
        self.seen.append(list(transcript))
        return self._respond(transcript, tools)


def _scripted(*turns: ModelTurn) -> _Model:
    queue = list(turns)
    return _Model(lambda transcript, tools: queue.pop(0))


def _call(name: str, **args: object) -> ModelTurn:
    return ModelTurn(tool_calls=(ToolCall(id=f"call_{name}", name=name, input=dict(args)),))


def _answer(cause: str = "", ids: object = ()) -> ModelTurn:
    ids_json = json.dumps(list(ids) if isinstance(ids, (list, tuple)) else ids)
    return ModelTurn(
        text=f'{{"summary": "s", "suspected_cause": "{cause}", '
        f'"self_confidence": 0.8, "evidence_ids": {ids_json}}}'
    )


def _investigator(
    role: SpecialistRole, model: _Model, *, case_id: str = "gold_0007", max_steps: int = 4
) -> InvestigatingSpecialist:
    source = GoldenTelemetry.from_case(_case(case_id))
    return InvestigatingSpecialist(
        role, complete=model, tools=tools_for(role, source), max_steps=max_steps
    )


# --------------------------------------------------------------------------- tools


def test_every_role_gets_exactly_its_own_tool() -> None:
    source = GoldenTelemetry([])
    names = {role: [t.name for t in tools_for(role, source)] for role in _ROLES}
    assert all(len(n) == 1 for n in names.values())
    assert len({n[0] for n in names.values()}) == len(_ROLES)  # no shared tools


def test_golden_telemetry_filters_by_kind_and_hides_the_answers() -> None:
    case = _case("gold_0007")
    deploys = asyncio.run(GoldenTelemetry.from_case(case).fetch("deploy", _ctx()))
    # the cited deploy and an unrelated preview deploy (a distractor), in time order
    assert [o.summary for o in deploys] == [
        "Vercel preview deployment f00d421 for branch docs-typo (not promoted)",
        "Vercel deployment abc123 promoted to production at 14:05Z",
    ]
    # The finding's conclusion is never served — only its raw observation.
    finding = next(f for f in case.findings if f.role == "change_deploy")
    assert all(o.summary != finding.summary for o in deploys)


def test_contains_filter_is_case_insensitive() -> None:
    source = GoldenTelemetry.from_case(_case("gold_0007"))
    assert asyncio.run(source.fetch("metric", _ctx(), contains="POOL"))
    assert asyncio.run(source.fetch("metric", _ctx(), contains="nothing-like-this")) == []


# --------------------------------------------------------------------------- the loop


def test_specialist_investigates_and_cites_returned_evidence() -> None:
    # obs_1 is the distractor preview deploy; obs_2 is the promoted one.
    model = _scripted(_call("list_recent_deploys"), _answer("deploy abc123", ["obs_2"]))
    finding = asyncio.run(_investigator("change_deploy", model).investigate(_ctx()))

    assert finding.status == "ok"
    assert finding.suspected_cause == "deploy abc123"
    assert finding.tool_calls == ["list_recent_deploys"]
    assert [ev.summary for ev in finding.evidence] == [
        "Vercel deployment abc123 promoted to production at 14:05Z"
    ]
    # The model saw the tool's result before answering.
    results = model.seen[1][-1]
    assert isinstance(results, ToolResults)
    assert results.results[0].content.startswith("obs_1 [deploy @ ")


def test_invented_or_malformed_evidence_ids_are_dropped() -> None:
    model = _scripted(
        _call("list_recent_deploys"),
        _answer("deploy abc123", ["obs_1", "obs_99", 5, {"x": 1}, "obs_1"]),
    )
    finding = asyncio.run(_investigator("change_deploy", model).investigate(_ctx()))
    assert len(finding.evidence) == 1


def test_specialist_cannot_call_another_roles_tool() -> None:
    model = _scripted(_call("search_logs"), _answer("guess", ["obs_1"]))
    finding = asyncio.run(_investigator("change_deploy", model).investigate(_ctx()))
    [result] = model.seen[1][-1].results  # type: ignore[union-attr]
    assert result.is_error and "unknown tool" in result.content
    assert finding.evidence == []
    assert finding.status == "no_signal"


def test_failing_tool_is_reported_to_the_model_not_raised() -> None:
    class _Broken:
        async def fetch(
            self, kind: EvidenceKind, ctx: InvestigationContext, *, contains: str | None = None
        ) -> list[Observation]:
            raise ConnectionError("provider down")

    model = _scripted(_call("search_logs"), _answer())
    spec = InvestigatingSpecialist("log", complete=model, tools=tools_for("log", _Broken()))
    finding = asyncio.run(spec.investigate(_ctx()))
    [result] = model.seen[1][-1].results  # type: ignore[union-attr]
    assert result == ToolResult("call_search_logs", "search_logs failed: ConnectionError", True)
    assert finding.status == "no_signal"


def test_role_with_no_data_reports_no_signal() -> None:
    # gold_0011 recorded no traces.
    model = _scripted(_call("search_traces"), _answer())
    finding = asyncio.run(_investigator("trace", model, case_id="gold_0011").investigate(_ctx()))
    [result] = model.seen[1][-1].results  # type: ignore[union-attr]
    assert result.content == "no matching trace data"
    assert finding.status == "no_signal" and finding.evidence == []


def test_step_budget_exhaustion_degrades_without_evidence() -> None:
    model = _Model(lambda transcript, tools: _call("query_metrics"))
    finding = asyncio.run(_investigator("metric", model, max_steps=2).investigate(_ctx()))
    assert finding.status == "degraded"
    assert finding.tool_calls == ["query_metrics", "query_metrics"]
    assert finding.evidence == [] and finding.self_confidence == 0.0


def test_max_steps_must_be_positive() -> None:
    with pytest.raises(ValueError):
        InvestigatingSpecialist("log", complete=_scripted(), tools=[], max_steps=0)


# --------------------------------------------------------------------------- end to end


def _diligent(transcript: Sequence[TranscriptEntry], tools: Sequence[ToolSpec]) -> ModelTurn:
    """Calls its one tool, then cites everything it found (or concludes nothing)."""
    last = transcript[-1]
    if isinstance(last, str):
        return _call(tools[0].name)
    assert isinstance(last, ToolResults)
    ids = [line.split(" ", 1)[0] for line in last.results[0].content.splitlines()]
    found = [i for i in ids if i.startswith("obs_")]
    return _answer("deploy abc123 exhausted the pool" if found else "", found)


def _commander_supporting(findings: Sequence[SpecialistFinding]) -> LLMCommander:
    stances = ", ".join(f'"{f.role}": "supports"' for f in findings if f.suspected_cause)

    async def complete(label: str, system: str, user: str) -> str:
        return f'{{"hypotheses": [{{"statement": "deploy abc123", "likelihood": 0.6, ' \
            f'"stances": {{{stances}}}}}]}}'

    return LLMCommander(complete=complete)


def test_all_five_investigators_restore_independent_corroboration() -> None:
    model = _Model(_diligent)
    findings = asyncio.run(_investigate_all(model))

    by_role = {f.role: f for f in findings}
    # gold_0007 recorded deploy, metric, trace and (distractor) log data; no errors.
    assert {r for r, f in by_role.items() if f.evidence} == {
        "change_deploy",
        "log",
        "metric",
        "trace",
    }
    assert by_role["error"].status == "no_signal"

    [top] = asyncio.run(_commander_supporting(findings).synthesize("inc_1", findings))
    assert sorted(top.supporting_roles) == ["change_deploy", "log", "metric", "trace"]
    assert independent_corroboration(top, findings) == 4


def test_single_call_specialists_agreeing_still_count_once() -> None:
    # Contrast: the same agreement from evidence-free single-call specialists.
    async def complete(label: str, system: str, user: str) -> str:
        return '{"summary": "s", "suspected_cause": "deploy abc123", "self_confidence": 0.9}'

    specialists = [LLMSpecialist(r, complete=complete) for r in _ROLES]
    findings = asyncio.run(_gather([s.investigate(_ctx()) for s in specialists]))
    [top] = asyncio.run(_commander_supporting(findings).synthesize("inc_1", findings))
    assert len(top.supporting_roles) == 5
    assert independent_corroboration(top, findings) == 1


async def _investigate_all(model: _Model) -> list[SpecialistFinding]:
    return await _gather([_investigator(r, model).investigate(_ctx()) for r in _ROLES])


async def _gather(coros: Sequence[Awaitable[SpecialistFinding]]) -> list[SpecialistFinding]:
    return list(await asyncio.gather(*coros))


# --------------------------------------------------------------------------- adapter + wiring


def test_to_messages_renders_a_tool_round_trip() -> None:
    call = ToolCall(id="tu_1", name="search_logs", input={"contains": "522"})
    messages = to_messages(
        [
            "incident text",
            ModelTurn(text="checking logs", tool_calls=(call,)),
            ToolResults((ToolResult("tu_1", "obs_1 [log] 3 isolated 522s"),)),
        ]
    )
    assert messages == [
        {"role": "user", "content": "incident text"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking logs"},
                {"type": "tool_use", "id": "tu_1", "name": "search_logs",
                 "input": {"contains": "522"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1",
                 "content": "obs_1 [log] 3 isolated 522s", "is_error": False},
            ],
        },
    ]


def test_build_llm_agents_wires_investigators_only_with_telemetry() -> None:
    settings = Settings(anthropic_api_key="sk-test", llm_model="claude-haiku-4-5", use_llm=True)
    meter = TokenMeter(sink=lambda _: None)
    plain, _, _ = build_llm_agents(settings=settings, meter=meter, roles=_ROLES)
    tooled, _, _ = build_llm_agents(
        settings=settings, meter=meter, roles=_ROLES, telemetry=GoldenTelemetry([])
    )
    assert all(isinstance(s, LLMSpecialist) for s in plain)
    assert all(isinstance(s, InvestigatingSpecialist) for s in tooled)

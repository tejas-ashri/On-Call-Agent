"""Real Claude-backed agents (opt-in; doc 04 §3).

The LLM realizations of the specialist, commander and planner roles. They depend only on a
``Completer`` seam — ``(label, system, user) -> assistant_text`` — so the parsing
and contract-building logic is unit-tested offline with a fake completer, while
the thin Anthropic adapter (:func:`anthropic_completer`) is exercised by the real
run. Every real call is metered (tokens + cost) through the injected
:class:`~sentinel.agents.meter.TokenMeter`.

:class:`InvestigatingSpecialist` is the tool-using variant: a bounded loop in
which Claude calls its role's read-only tools (:mod:`sentinel.tools`) and cites
the observations they returned. It sits behind the richer :data:`ToolCompleter`
seam, faked offline the same way; :func:`anthropic_tool_completer` is its adapter.

The agents only ever see the **redacted** ``CanonicalEvent``s the edge produced —
secrets never reach the model (C2).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from anthropic import AsyncAnthropic
from anthropic.types import (
    ContentBlockParam,
    Message,
    MessageParam,
    ToolParam,
    ToolResultBlockParam,
)

from sentinel.agents.meter import CallUsage, TokenMeter
from sentinel.config import Settings
from sentinel.contracts import (
    CanonicalEvent,
    Hypothesis,
    RemediationPlan,
    SpecialistFinding,
    SpecialistRole,
)
from sentinel.enums import ActionType
from sentinel.orchestrator import Commander, InvestigationContext, Planner, Specialist
from sentinel.tools import Observation, TelemetrySource, Tool, tools_for

# (label, system, user_prompt) -> assistant text. Metering happens inside the real one.
Completer = Callable[[str, str, str], Awaitable[str]]


@dataclass(frozen=True)
class ToolSpec:
    """A tool as the model sees it: name, when to use it, and its input schema."""

    name: str
    description: str
    input_schema: dict[str, object]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, object]


@dataclass(frozen=True)
class ModelTurn:
    """One assistant turn: free text and/or tool calls (no calls ⇒ the model is done)."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ToolResults:
    results: tuple[ToolResult, ...]


#: A transcript entry: the opening user prompt, an assistant turn, or tool results.
TranscriptEntry = str | ModelTurn | ToolResults

# (label, system, transcript, tools) -> next assistant turn. Metering happens inside the real one.
ToolCompleter = Callable[
    [str, str, Sequence[TranscriptEntry], Sequence[ToolSpec]], Awaitable[ModelTurn]
]


def _clamp01(value: object, default: float = 0.5) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        f = float(value)
    except ValueError:
        return default
    return max(0.0, min(1.0, f))


def _extract_json(text: str) -> dict[str, object]:
    """Tolerantly pull the first JSON object out of a model response."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _specialist_system(role: str) -> str:
    return (
        f"You are the {role} specialist in an automated SRE incident triage. You receive a "
        f"redacted incident (secrets already removed). Investigate ONLY the {role} angle and be "
        "concise. Respond with ONLY a JSON object, no prose: "
        '{"summary": "<one sentence>", "suspected_cause": "<short cause or empty>", '
        '"self_confidence": <number 0..1>}'
    )


def _investigator_system(role: str, tools: Sequence[Tool]) -> str:
    names = ", ".join(t.name for t in tools)
    return (
        f"You are the {role} specialist in an automated SRE incident triage. You receive a "
        f"redacted incident (secrets already removed). Investigate ONLY the {role} angle using "
        f"your tools ({names}); every tool result line starts with an observation id like "
        "obs_1. Call tools until you have enough evidence or they return nothing new, then "
        "stop calling tools and respond with ONLY a JSON object, no prose: "
        '{"summary": "<one sentence>", "suspected_cause": "<short cause or empty>", '
        '"self_confidence": <number 0..1>, "evidence_ids": ["<obs id>", ...]}. '
        "Cite only ids your tools returned that actually support your summary."
    )


#: Upper bound on hypotheses the commander may return (top + rivals).
_MAX_HYPOTHESES = 3


def _commander_system() -> str:
    return (
        "You are the incident commander. Given the specialists' findings, propose the most "
        f"likely root causes — the leader plus up to {_MAX_HYPOTHESES - 1} genuinely distinct "
        "rivals when the evidence admits them (never pad with paraphrases). For EVERY hypothesis, "
        "mark each specialist's stance: 'supports' only if that specialist's finding points to "
        "THIS cause, 'contradicts' if its finding points elsewhere or rules it out, otherwise "
        "'neutral'. Respond with ONLY a JSON object, no prose: "
        '{"hypotheses": [{"statement": "<one-sentence root cause>", '
        '"likelihood": <number 0..1>, '
        '"stances": {"<role>": "supports" | "contradicts" | "neutral"}}]}'
    )


#: Prompt budget for the signals: a storm can carry hundreds of events, and a payload
#: (stack trace, request context) can be many KB. Bodies beyond these are cut.
_MAX_PROMPT_EVENTS = 20
_MAX_BODY_CHARS = 1500


def _incident_text(ctx: InvestigationContext) -> str:
    return _signals_text(ctx.incident_id, ctx.events)


def _signals_text(incident_id: str, events: Sequence[CanonicalEvent]) -> str:
    lines = [f"Incident {incident_id} — {len(events)} signal(s):"]
    for e in events[:_MAX_PROMPT_EVENTS]:
        lines.append(
            f"- [{e.source}/{e.kind.value}] {e.title}  "
            f"(service={e.service}, severity={e.severity.value})"
        )
        if e.labels:
            lines.append(f"  labels: {e.labels}")
        # The edge-redacted provider payload: the stack trace, culprit and request
        # context the title and labels leave out. Already secret-free (C2).
        if body := e.body_redacted.strip():
            if len(body) > _MAX_BODY_CHARS:
                body = f"{body[:_MAX_BODY_CHARS]}… [truncated]"
            lines.append(f"  payload: {body}")
    if (omitted := len(events) - _MAX_PROMPT_EVENTS) > 0:
        lines.append(f"(+{omitted} more signal(s) omitted)")
    return "\n".join(lines)


def _findings_text(findings: Sequence[SpecialistFinding]) -> str:
    lines: list[str] = []
    for f in findings:
        lines.append(
            f"- {f.role}: {f.summary} (suspected: {f.suspected_cause or 'n/a'}, "
            f"confidence {f.self_confidence:.2f})"
        )
        lines.extend(f"    evidence [{ev.kind}]: {ev.summary}" for ev in f.evidence)
    return "\n".join(lines)


class LLMSpecialist:
    """A specialist whose investigation is a real Claude call (doc 04 §3)."""

    def __init__(self, role: SpecialistRole, *, complete: Completer) -> None:
        self.role = role
        self._complete = complete

    async def investigate(self, ctx: InvestigationContext) -> SpecialistFinding:
        text = await self._complete(
            f"specialist:{self.role}", _specialist_system(self.role), _incident_text(ctx)
        )
        data = _extract_json(text)
        raw_summary = str(data.get("summary", "")).strip()
        raw_cause = data.get("suspected_cause")
        cause = str(raw_cause).strip() if isinstance(raw_cause, str) else ""
        return SpecialistFinding(
            incident_id=ctx.incident_id,
            role=self.role,
            summary=raw_summary[:1000] or f"{self.role}: no clear signal",
            suspected_cause=cause[:200] or None,
            self_confidence=_clamp01(data.get("self_confidence")),
        )


class InvestigatingSpecialist:
    """A specialist that investigates with its role's tools in a bounded loop (doc 04 §3).

    Each step, Claude either calls tools — whose observations are fed back — or
    stops and answers. The finding's evidence is exactly the observations Claude
    cited *that a tool actually returned*: model-invented evidence is dropped, so
    evidence can never be fabricated (C1).
    """

    def __init__(
        self,
        role: SpecialistRole,
        *,
        complete: ToolCompleter,
        tools: Sequence[Tool],
        max_steps: int = 4,
    ) -> None:
        if max_steps < 1:
            raise ValueError(f"max_steps must be >= 1: {max_steps}")
        self.role = role
        self._complete = complete
        self._tools = {t.name: t for t in tools}
        self._specs = [ToolSpec(t.name, t.description, t.input_schema) for t in tools]
        self._system = _investigator_system(role, tools)
        self._max_steps = max_steps

    async def investigate(self, ctx: InvestigationContext) -> SpecialistFinding:
        observed: dict[str, Observation] = {}
        calls: list[str] = []
        transcript: list[TranscriptEntry] = [_incident_text(ctx)]
        for _ in range(self._max_steps):
            turn = await self._complete(
                f"specialist:{self.role}", self._system, transcript, self._specs
            )
            if not turn.tool_calls:
                return self._conclude(ctx, turn.text, observed, calls)
            transcript.append(turn)
            results: list[ToolResult] = []
            for call in turn.tool_calls:
                calls.append(call.name)
                results.append(await self._run(call, ctx, observed))
            transcript.append(ToolResults(tuple(results)))
        return SpecialistFinding(
            incident_id=ctx.incident_id,
            role=self.role,
            status="degraded",
            summary=f"{self.role}: step budget exhausted after {len(calls)} tool call(s)",
            self_confidence=0.0,
            tool_calls=calls,
        )

    async def _run(
        self, call: ToolCall, ctx: InvestigationContext, observed: dict[str, Observation]
    ) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(call.id, f"unknown tool {call.name!r}", is_error=True)
        try:
            found = await tool.run(call.input, ctx)
        except Exception as exc:  # a failing tool degrades the step, never the incident
            return ToolResult(call.id, f"{call.name} failed: {type(exc).__name__}", is_error=True)
        if not found:
            return ToolResult(call.id, f"no matching {tool.kind} data")
        lines: list[str] = []
        for obs in found:
            obs_id = f"obs_{len(observed) + 1}"
            observed[obs_id] = obs
            lines.append(f"{obs_id} [{obs.kind} @ {obs.observed_at.isoformat()}] {obs.summary}")
        return ToolResult(call.id, "\n".join(lines))

    def _conclude(
        self,
        ctx: InvestigationContext,
        text: str,
        observed: dict[str, Observation],
        calls: list[str],
    ) -> SpecialistFinding:
        data = _extract_json(text)
        raw_summary = str(data.get("summary", "")).strip()
        raw_cause = data.get("suspected_cause")
        cause = str(raw_cause).strip() if isinstance(raw_cause, str) else ""
        raw_ids = data.get("evidence_ids")
        ids = [i for i in raw_ids if isinstance(i, str)] if isinstance(raw_ids, list) else []
        evidence = [observed[i].to_evidence() for i in dict.fromkeys(ids) if i in observed]
        return SpecialistFinding(
            incident_id=ctx.incident_id,
            role=self.role,
            status="ok" if observed else "no_signal",
            summary=raw_summary[:1000] or f"{self.role}: no clear signal",
            suspected_cause=cause[:200] or None,
            self_confidence=_clamp01(data.get("self_confidence")),
            evidence=evidence,
            tool_calls=calls,
        )


class LLMCommander:
    """A commander whose synthesis is a real Claude call (doc 04 §3)."""

    def __init__(self, *, complete: Completer) -> None:
        self._complete = complete

    async def synthesize(
        self, incident_id: str, findings: Sequence[SpecialistFinding]
    ) -> list[Hypothesis]:
        text = await self._complete("commander", _commander_system(), _findings_text(findings))
        raw = _extract_json(text).get("hypotheses")
        items = [h for h in raw if isinstance(h, dict)] if isinstance(raw, list) else []
        roles = [f.role for f in findings]
        hypotheses = [_parse_hypothesis(item, roles) for item in items[:_MAX_HYPOTHESES]]
        if not hypotheses:
            # Unparseable synthesis: an unsupported, low-likelihood placeholder — it can only
            # ever page (LOW band), never authorize an action.
            hypotheses = [_parse_hypothesis({"likelihood": 0.0}, roles)]
        # Scoring treats hypotheses[0] as the leader, so rank by likelihood.
        hypotheses.sort(key=lambda h: h.raw_likelihood, reverse=True)
        return [
            h.model_copy(update={"hypothesis_id": f"h{i}"}) for i, h in enumerate(hypotheses, 1)
        ]


def _parse_hypothesis(item: dict[str, object], roles: Sequence[SpecialistRole]) -> Hypothesis:
    """Build one :class:`Hypothesis`; stances are trusted only for roles that reported.

    A role counts as supporting only on an explicit ``"supports"`` — merely having
    named *some* suspected cause is not agreement with *this* one.
    """
    stances = item.get("stances")
    stance_of = stances if isinstance(stances, dict) else {}
    statement = str(item.get("statement", "")).strip() or "undetermined root cause"
    return Hypothesis(
        hypothesis_id="h0",  # renumbered after ranking
        statement=statement[:600],
        supporting_roles=[r for r in roles if stance_of.get(r) == "supports"],
        contradicting_roles=[r for r in roles if stance_of.get(r) == "contradicts"],
        raw_likelihood=_clamp01(item.get("likelihood"), default=0.0),
    )


#: Label keys whose values are concrete things an action can target.
_TARGET_LABELS = ("deployment.id", "commit_sha", "release")
_BLAST_RADII = ("single_service", "shared", "global")


def _candidate_targets(events: Sequence[CanonicalEvent]) -> list[str]:
    """Every target the incident's signals name, in first-seen order.

    The planner may only pick from these: an action aimed at a deployment or
    service the signals never mentioned would be a fabricated target (C1).
    """
    found: dict[str, None] = {}
    for e in events:
        if e.service:
            found[e.service] = None
        for key in _TARGET_LABELS:
            if value := e.labels.get(key):
                found[value] = None
    return list(found)


def _planner_system() -> str:
    actions = ", ".join(a.value for a in ActionType)
    return (
        "You are the remediation planner in an automated SRE incident triage. Given the "
        "leading root-cause hypothesis and the incident's redacted signals, propose ONE "
        "remediation that would mitigate it. You never execute anything: a policy engine, and "
        "often a human, decides whether it runs. Prefer the least risky reversible action that "
        "addresses the cause (e.g. rolling back the deploy that introduced a regression over "
        f"failing over a database). action_type must be one of: {actions}. target must be "
        "copied exactly from the candidate targets you are given — never invent one. "
        "Respond with ONLY a JSON object, no prose: "
        '{"action_type": "<one of the above>", "target": "<a candidate target>", '
        '"params": {}, "reversible": true | false, "summary": "<one sentence>", '
        '"command": "<equivalent CLI command, or empty>", '
        '"estimated_blast_radius": "single_service" | "shared" | "global"}. '
        'If no listed action fits, respond {"action_type": "none"}.'
    )


def _planner_text(
    incident_id: str,
    hypothesis: Hypothesis,
    events: Sequence[CanonicalEvent],
    targets: Sequence[str],
) -> str:
    lines = [f"Leading hypothesis: {hypothesis.statement}"]
    if hypothesis.supporting_roles:
        lines.append(f"Supported by: {', '.join(hypothesis.supporting_roles)}")
    lines.extend(f"  evidence [{ev.kind}]: {ev.summary}" for ev in hypothesis.evidence)
    lines.append(f"Candidate targets: {', '.join(targets)}")
    lines.append("")
    lines.append(_signals_text(incident_id, events))
    return "\n".join(lines)


class LLMPlanner:
    """A planner whose remediation is a real Claude call (doc 04 §8).

    Only the *shape* of the plan comes from the model; nothing it claims is
    trusted further than it can be checked. An unknown action type, ``"none"``,
    or a target the signals never named raises, which
    :func:`~sentinel.orchestrator.guard.safe_draft` turns into "no plan" — a page
    without a drafted fix, never an action. An unstated ``reversible`` reads as
    ``False`` and an unstated blast radius as ``global``; the reversibility floor
    can only lower the claim further.
    """

    def __init__(self, *, complete: Completer) -> None:
        self._complete = complete

    async def draft(
        self, incident_id: str, hypothesis: Hypothesis, events: Sequence[CanonicalEvent]
    ) -> RemediationPlan:
        targets = _candidate_targets(events)
        if not targets:
            raise ValueError("no targetable resource in the incident's signals")
        text = await self._complete(
            "planner", _planner_system(), _planner_text(incident_id, hypothesis, events, targets)
        )
        data = _extract_json(text)
        action_type = ActionType(str(data.get("action_type", "")))
        target = str(data.get("target", "")).strip()
        if target not in targets:
            raise ValueError(f"planner target {target!r} is not named by the incident's signals")
        params = data.get("params")
        command = data.get("command")
        radius = data.get("estimated_blast_radius")
        summary = str(data.get("summary", "")).strip() or f"{action_type.value} {target}"
        return RemediationPlan(
            incident_id=incident_id,
            action_type=action_type.value,
            target=target,
            params=params if isinstance(params, dict) else {},
            reversible=data.get("reversible") is True,
            summary=summary[:600],
            command=command.strip() if isinstance(command, str) and command.strip() else None,
            estimated_blast_radius=radius if radius in _BLAST_RADII else "global",
        )


def anthropic_completer(
    client: AsyncAnthropic, *, model: str, meter: TokenMeter, max_tokens: int = 700
) -> Completer:
    """Build a metered :data:`Completer` backed by the Anthropic Messages API."""

    async def complete(label: str, system: str, user: str) -> str:
        messages: list[MessageParam] = [{"role": "user", "content": user}]
        response = await client.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages
        )
        _record(meter, model, label, response)
        return "".join(block.text for block in response.content if block.type == "text")

    return complete


def _record(meter: TokenMeter, model: str, label: str, response: Message) -> None:
    meter.record(
        model=model,
        label=label,
        usage=CallUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
            cache_creation_input_tokens=response.usage.cache_creation_input_tokens or 0,
        ),
    )


def to_messages(transcript: Sequence[TranscriptEntry]) -> list[MessageParam]:
    """Render a provider-neutral transcript as Anthropic Messages API messages."""
    messages: list[MessageParam] = []
    for entry in transcript:
        if isinstance(entry, str):
            messages.append({"role": "user", "content": entry})
        elif isinstance(entry, ModelTurn):
            blocks: list[ContentBlockParam] = []
            if entry.text:
                blocks.append({"type": "text", "text": entry.text})
            blocks.extend(
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
                for c in entry.tool_calls
            )
            messages.append({"role": "assistant", "content": blocks})
        else:
            results: list[ToolResultBlockParam] = [
                {
                    "type": "tool_result",
                    "tool_use_id": r.call_id,
                    "content": r.content,
                    "is_error": r.is_error,
                }
                for r in entry.results
            ]
            messages.append({"role": "user", "content": results})
    return messages


def anthropic_tool_completer(
    client: AsyncAnthropic, *, model: str, meter: TokenMeter, max_tokens: int = 1024
) -> ToolCompleter:
    """Build a metered :data:`ToolCompleter` backed by Messages API tool use."""

    async def complete(
        label: str, system: str, transcript: Sequence[TranscriptEntry], tools: Sequence[ToolSpec]
    ) -> ModelTurn:
        tool_params: list[ToolParam] = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=to_messages(transcript),
            tools=tool_params,
        )
        _record(meter, model, label, response)
        text = "".join(block.text for block in response.content if block.type == "text")
        calls = tuple(
            ToolCall(
                block.id, block.name, dict(block.input) if isinstance(block.input, dict) else {}
            )
            for block in response.content
            if block.type == "tool_use"
        )
        return ModelTurn(text=text, tool_calls=calls)

    return complete


def build_llm_agents(
    *,
    settings: Settings,
    meter: TokenMeter,
    roles: Sequence[SpecialistRole],
    telemetry: TelemetrySource | None = None,
) -> tuple[list[Specialist], Commander, Planner]:
    """Construct LLM-backed specialists, commander and planner (requires ``llm_ready``).

    With ``telemetry``, specialists are :class:`InvestigatingSpecialist`s that query it
    through their role's tools; without it, they are single-call :class:`LLMSpecialist`s.
    """
    if not settings.llm_ready or settings.anthropic_api_key is None:
        raise RuntimeError("LLM not configured: set ANTHROPIC_API_KEY and SENTINEL_USE_LLM=1")
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    complete = anthropic_completer(client, model=settings.llm_model, meter=meter)
    specialists: list[Specialist]
    if telemetry is None:
        specialists = [LLMSpecialist(role, complete=complete) for role in roles]
    else:
        tool_complete = anthropic_tool_completer(client, model=settings.llm_model, meter=meter)
        specialists = [
            InvestigatingSpecialist(
                role, complete=tool_complete, tools=tools_for(role, telemetry)
            )
            for role in roles
        ]
    commander: Commander = LLMCommander(complete=complete)
    planner: Planner = LLMPlanner(complete=complete)
    return specialists, commander, planner

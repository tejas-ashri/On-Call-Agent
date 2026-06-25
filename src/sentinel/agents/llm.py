"""Real Claude-backed agents (opt-in; doc 04 §3).

The LLM realizations of the specialist and commander roles. They depend only on a
``Completer`` seam — ``(label, system, user) -> assistant_text`` — so the parsing
and contract-building logic is unit-tested offline with a fake completer, while
the thin Anthropic adapter (:func:`anthropic_completer`) is exercised by the real
run. Every real call is metered (tokens + cost) through the injected
:class:`~sentinel.agents.meter.TokenMeter`.

The agents only ever see the **redacted** ``CanonicalEvent``s the edge produced —
secrets never reach the model (C2).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam

from sentinel.agents.meter import CallUsage, TokenMeter
from sentinel.config import Settings
from sentinel.contracts import Hypothesis, SpecialistFinding, SpecialistRole
from sentinel.orchestrator import Commander, InvestigationContext, Specialist

# (label, system, user_prompt) -> assistant text. Metering happens inside the real one.
Completer = Callable[[str, str, str], Awaitable[str]]


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


def _commander_system() -> str:
    return (
        "You are the incident commander. Given the specialists' findings, synthesize the single "
        "most likely root cause. Respond with ONLY a JSON object, no prose: "
        '{"statement": "<one-sentence root cause>", "confidence": <number 0..1>}'
    )


def _incident_text(ctx: InvestigationContext) -> str:
    lines = [f"Incident {ctx.incident_id} — {len(ctx.events)} signal(s):"]
    for e in ctx.events:
        lines.append(
            f"- [{e.source}/{e.kind.value}] {e.title}  "
            f"(service={e.service}, severity={e.severity.value})"
        )
        if e.labels:
            lines.append(f"  labels: {e.labels}")
    return "\n".join(lines)


def _findings_text(findings: Sequence[SpecialistFinding]) -> str:
    return "\n".join(
        f"- {f.role}: {f.summary} (suspected: {f.suspected_cause or 'n/a'}, "
        f"confidence {f.self_confidence:.2f})"
        for f in findings
    )


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


class LLMCommander:
    """A commander whose synthesis is a real Claude call (doc 04 §3)."""

    def __init__(self, *, complete: Completer) -> None:
        self._complete = complete

    async def synthesize(
        self, incident_id: str, findings: Sequence[SpecialistFinding]
    ) -> list[Hypothesis]:
        text = await self._complete("commander", _commander_system(), _findings_text(findings))
        data = _extract_json(text)
        statement = str(data.get("statement", "")).strip() or "undetermined root cause"
        supporting = [f.role for f in findings if f.suspected_cause]
        return [
            Hypothesis(
                hypothesis_id="h1",
                statement=statement[:600],
                supporting_roles=supporting,
                raw_likelihood=_clamp01(data.get("confidence")),
            )
        ]


def anthropic_completer(
    client: AsyncAnthropic, *, model: str, meter: TokenMeter, max_tokens: int = 700
) -> Completer:
    """Build a metered :data:`Completer` backed by the Anthropic Messages API."""

    async def complete(label: str, system: str, user: str) -> str:
        messages: list[MessageParam] = [{"role": "user", "content": user}]
        response = await client.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages
        )
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
        return "".join(block.text for block in response.content if block.type == "text")

    return complete


def build_llm_agents(
    *, settings: Settings, meter: TokenMeter, roles: Sequence[SpecialistRole]
) -> tuple[list[Specialist], Commander]:
    """Construct LLM-backed specialists + commander from settings (requires ``llm_ready``)."""
    if not settings.llm_ready or settings.anthropic_api_key is None:
        raise RuntimeError("LLM not configured: set ANTHROPIC_API_KEY and SENTINEL_USE_LLM=1")
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    complete = anthropic_completer(client, model=settings.llm_model, meter=meter)
    specialists: list[Specialist] = [LLMSpecialist(role, complete=complete) for role in roles]
    commander: Commander = LLMCommander(complete=complete)
    return specialists, commander

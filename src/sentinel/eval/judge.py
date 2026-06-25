"""L2 LLM-as-judge (doc 06 §3.2).

Root-cause quality isn't exact-match-able, so L2 scores the end-to-end diagnosis against
the golden ground truth on a **weighted rubric** with a strong judge model (deliberately
different from and stronger than the diagnoser — it doesn't grade its own homework). The
judge sees only redacted doc-04 contracts (C2) and returns a typed :class:`RubricScore`,
validated like any agent contract. **Hallucinated evidence is a hard veto** (C1): inventing
a signal to justify a write fails L2 outright, regardless of the weighted total.

The :data:`Judge` seam is ``(prompt) -> json_text`` so the rubric/parse logic is unit-tested
offline with a fake judge; :func:`build_llm_judge` is the thin Anthropic adapter (opt-in,
metered, lazily imported so this module needs no ``anthropic`` to load).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from sentinel.contracts.agents import (
    ConfidenceAssessment,
    EvidenceRef,
    Hypothesis,
    RemediationPlan,
)
from sentinel.eval.golden import GoldenCase

if TYPE_CHECKING:
    from sentinel.agents.meter import TokenMeter
    from sentinel.config import Settings

# (prompt) -> assistant JSON text. Metering happens inside the real adapter.
Judge = Callable[[str], Awaitable[str]]

# A stronger model than the (Haiku) diagnoser — the judge must not grade its own homework.
_STRONG_JUDGE_MODEL = "claude-sonnet-4-6"

_WEIGHTS: dict[str, float] = {
    "root_cause_correctness": 0.40,
    "evidence_soundness": 0.25,
    "remediation_actionability": 0.20,
    "calibration_honesty": 0.10,
    "no_hallucinated_evidence": 0.05,
}


class RubricScore(BaseModel):
    """Judge output — validated like any agent contract; a malformed score is an eval error."""

    model_config = ConfigDict(extra="forbid")

    root_cause_correctness: Literal[0, 1, 2, 3]
    evidence_soundness: Literal[0, 1, 2, 3]
    remediation_actionability: Literal[0, 1, 2, 3]
    calibration_honesty: Literal[0, 1, 2, 3]
    no_hallucinated_evidence: Literal[0, 3]  # binary: present (3) or vetoed (0)
    rationale: str = Field(max_length=800)  # auditable, redacted
    judge_model_id: str  # provenance: which judge + version


def weighted(score: RubricScore) -> float:
    """The weighted rubric total in ``[0, 1]`` (each dimension normalized by its max of 3)."""
    total = 0.0
    for dim, w in _WEIGHTS.items():
        total += float(getattr(score, dim)) / 3.0 * w
    return total


def passes(score: RubricScore, *, threshold: float) -> bool:
    """True iff the weighted total clears ``threshold`` AND evidence wasn't fabricated.

    The hallucination dimension is a veto, not merely a low sub-score (C1): a 0 there fails
    L2 regardless of the weighted total.
    """
    if score.no_hallucinated_evidence == 0:
        return False
    return weighted(score) >= threshold


def _extract_json(text: str) -> dict[str, object]:
    """Tolerantly pull the first JSON object out of a judge response."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


_JUDGE_SYSTEM = (
    "You are a senior SRE grading an automated incident diagnosis against ground truth. "
    "Score each rubric dimension 0-3 (no_hallucinated_evidence is binary: 3 if every cited "
    "signal exists in the replay, else 0). Respond with ONLY a JSON object: "
    '{"root_cause_correctness": <0-3>, "evidence_soundness": <0-3>, '
    '"remediation_actionability": <0-3>, "calibration_honesty": <0-3>, '
    '"no_hallucinated_evidence": <0 or 3>, "rationale": "<one sentence>", '
    '"judge_model_id": "<your model id>"}'
)


def _judge_prompt(
    case: GoldenCase,
    hypotheses: Sequence[Hypothesis],
    assessment: ConfidenceAssessment,
    evidence: Sequence[EvidenceRef],
    remediation: RemediationPlan | None,
) -> str:
    exp = case.expected
    rem = (
        f"{remediation.action_type} (reversible={remediation.reversible})"
        if remediation is not None
        else "none drafted"
    )
    lines = [
        "GROUND TRUTH (golden case):",
        f"  root_cause_class: {exp.root_cause_class}",
        f"  smoking_gun_signal: {exp.smoking_gun_signal}",
        f"  expected_band: {exp.expected_band.value}",
        f"  correct_remediation: {exp.correct_remediation.action_type} "
        f"(reversible={exp.correct_remediation.reversible})",
        "",
        "PRODUCED DIAGNOSIS (redacted):",
        f"  top hypothesis: {hypotheses[0].statement if hypotheses else 'none'}",
        f"  calibrated confidence: {assessment.band.value} "
        f"({assessment.calibrated_confidence:.0%})",
        f"  drafted remediation: {rem}",
        "  evidence: " + "; ".join(e.summary for e in evidence),
    ]
    return "\n".join(lines)


async def judge_diagnosis(
    case: GoldenCase,
    *,
    hypotheses: Sequence[Hypothesis],
    assessment: ConfidenceAssessment,
    evidence: Sequence[EvidenceRef],
    remediation: RemediationPlan | None,
    judge: Judge,
) -> RubricScore:
    """Score a diagnosis against the golden ground truth; return a validated RubricScore.

    All inputs are doc-04 contracts and already redacted (C2). The returned score is
    validated, mirroring doc 04's validate-every-hop discipline.
    """
    raw = await judge(_judge_prompt(case, hypotheses, assessment, evidence, remediation))
    return RubricScore.model_validate(_extract_json(raw))


def build_llm_judge(
    *, settings: Settings, meter: TokenMeter, model: str | None = None
) -> Judge:
    """Build a metered Anthropic-backed judge on a strong model (requires ``llm_ready``)."""
    if not settings.llm_ready or settings.anthropic_api_key is None:
        raise RuntimeError("LLM not configured: set ANTHROPIC_API_KEY and SENTINEL_USE_LLM=1")
    from anthropic import AsyncAnthropic

    from sentinel.agents.llm import anthropic_completer

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    complete = anthropic_completer(client, model=model or _STRONG_JUDGE_MODEL, meter=meter)

    async def judge(prompt: str) -> str:
        return await complete("judge", _JUDGE_SYSTEM, prompt)

    return judge

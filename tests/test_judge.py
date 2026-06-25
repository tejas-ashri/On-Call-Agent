"""L2 LLM-as-judge harness (doc 06 §3.2), tested offline with a fake judge.

The judge scores an end-to-end diagnosis against the golden ground truth on a weighted
rubric and returns a typed ``RubricScore``. Hallucinated evidence is a hard veto, not a
low sub-score (C1). The real Anthropic judge is glue exercised by a live run, not here.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sentinel.contracts.agents import (
    ConfidenceAssessment,
    EvidenceRef,
    Hypothesis,
    RemediationPlan,
)
from sentinel.enums import ConfidenceBand
from sentinel.eval.golden import GoldenCase, load_golden
from sentinel.eval.judge import Judge, RubricScore, judge_diagnosis, passes, weighted

_T = datetime(2026, 5, 2, 14, 11, tzinfo=UTC)


def _score(**over: object) -> RubricScore:
    base: dict[str, object] = {
        "root_cause_correctness": 3,
        "evidence_soundness": 3,
        "remediation_actionability": 3,
        "calibration_honesty": 3,
        "no_hallucinated_evidence": 3,
        "rationale": "matches root_cause_class and cites the decisive deploy signal",
        "judge_model_id": "fake-judge-1",
    }
    base.update(over)
    return RubricScore.model_validate(base)


def _fake_judge(payload: str) -> Judge:
    async def judge(prompt: str) -> str:
        return payload

    return judge


def _bundle() -> tuple[GoldenCase, list[Hypothesis], ConfidenceAssessment, list[EvidenceRef]]:
    case = load_golden()[0]
    hyps = [
        Hypothesis(
            hypothesis_id="h1",
            statement="deploy abc123 introduced an N+1 exhausting the pool",
            supporting_roles=["change_deploy", "metric"],
            raw_likelihood=0.86,
        )
    ]
    assess = ConfidenceAssessment(
        incident_id=case.incident_id,
        top_hypothesis_id="h1",
        calibrated_confidence=0.9,
        band=ConfidenceBand.HIGH,
        corroboration_count=3,
        n_competing=0,
        calibrator_version="cal.test",
    )
    evidence = [EvidenceRef(kind="deploy", summary="deploy abc123", observed_at=_T,
                            raw_ref="obj://redacted/dep")]
    return case, hyps, assess, evidence


def test_weighted_perfect_score_is_one() -> None:
    assert weighted(_score()) == 1.0


def test_hallucinated_evidence_is_a_veto() -> None:
    s = _score(no_hallucinated_evidence=0)
    assert weighted(s) > 0.8  # the weighted total would still pass…
    assert not passes(s, threshold=0.8)  # …but fabricated evidence vetoes it outright (C1)


def test_passes_respects_threshold() -> None:
    weak = _score(
        root_cause_correctness=0,
        evidence_soundness=0,
        remediation_actionability=0,
        calibration_honesty=0,
    )
    assert not passes(weak, threshold=0.5)
    assert passes(_score(), threshold=0.8)


def test_judge_diagnosis_parses_into_rubric_score() -> None:
    case, hyps, assess, evidence = _bundle()
    judge = _fake_judge(_score().model_dump_json())
    result = asyncio.run(
        judge_diagnosis(
            case, hypotheses=hyps, assessment=assess, evidence=evidence,
            remediation=None, judge=judge,
        )
    )
    assert isinstance(result, RubricScore)
    assert result.root_cause_correctness == 3
    assert passes(result, threshold=0.8)


def test_judge_diagnosis_tolerates_prose_wrapped_json() -> None:
    case, hyps, assess, evidence = _bundle()
    plan = RemediationPlan(
        incident_id=case.incident_id, action_type="deploy.rollback", target="svc_api",
        reversible=True, summary="roll back abc123", estimated_blast_radius="single_service",
    )
    judge = _fake_judge(f"Sure, here is my score:\n{_score().model_dump_json()}\nhope it helps")
    result = asyncio.run(
        judge_diagnosis(
            case, hypotheses=hyps, assessment=assess, evidence=evidence,
            remediation=plan, judge=judge,
        )
    )
    assert result.judge_model_id == "fake-judge-1"

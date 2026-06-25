"""Evaluation harness (doc 06): the golden incident library, the deterministic L1 checks
built on it, the calibration/ECE measurement, and the L2 LLM-as-judge. Pure and offline —
no judge model is *called* here (the real judge is opt-in glue), no network, stdlib math."""

from __future__ import annotations

from sentinel.eval.calibration import (
    ReliabilityBin,
    expected_calibration_error,
    fit_temperature,
    negative_log_likelihood,
    reliability_and_ece,
)
from sentinel.eval.golden import ARCHETYPES, GoldenCase, golden_dir, load_golden
from sentinel.eval.judge import (
    Judge,
    RubricScore,
    build_llm_judge,
    judge_diagnosis,
    passes,
    weighted,
)

__all__ = [
    "ARCHETYPES",
    "GoldenCase",
    "Judge",
    "ReliabilityBin",
    "RubricScore",
    "build_llm_judge",
    "expected_calibration_error",
    "fit_temperature",
    "golden_dir",
    "judge_diagnosis",
    "load_golden",
    "negative_log_likelihood",
    "passes",
    "reliability_and_ece",
    "weighted",
]

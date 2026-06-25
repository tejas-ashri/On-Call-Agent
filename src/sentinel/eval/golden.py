"""The golden incident library + loader (doc 06 §2).

A versioned, replayable corpus of incidents, **stratified by stack archetype** (§2.1) so
the evals don't overfit to one provider's signal shape. Each YAML case
(``golden/<archetype>/<name>.yaml``) carries the redacted ``SpecialistFinding``s a correct
diagnosis produced, plus the graded ``expected`` ground truth — notably
``expected_outcome_by_grant``, which lets a single case test the entire §00.6 escalation
matrix (§2.2). The corpus is the substrate for the L1 evals
(``tests/test_golden_eval.py``) and, later, the calibration/ECE measurement (doc 06 §6).

Cases inherit their secret-free property from the ingestion boundary (ADR-004): every
embedded ``EvidenceRef.raw_ref`` is either absent or an ``obj://redacted/`` pointer — the
L1 layer asserts exactly that (C2/P3).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from sentinel.contracts.agents import EscalationOutcome, SpecialistFinding, SpecialistRole
from sentinel.enums import AutonomyTier, ConfidenceBand

Archetype = Literal["edge_db", "container_pg", "jamstack", "error_led", "uptime_led"]
ARCHETYPES: tuple[Archetype, ...] = (
    "edge_db",
    "container_pg",
    "jamstack",
    "error_led",
    "uptime_led",
)


class GoldenStack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: list[str]
    integration_mode: Literal["agentless", "agent"] = "agentless"


class GoldenRemediation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: str
    reversible: bool  # drives the §00.6 matrix
    acceptable_alternatives: list[str] = Field(default_factory=list)


class GoldenExpectation(BaseModel):
    """The graded ground truth for a case (§2.2)."""

    model_config = ConfigDict(extra="forbid")

    root_cause_class: str
    primary_service_kind: str
    smoking_gun_signal: SpecialistRole  # which specialist SHOULD carry the decisive evidence
    required_supporting_roles: list[SpecialistRole] = Field(default_factory=list)
    expected_band: ConfidenceBand
    correct_remediation: GoldenRemediation
    expected_outcome_by_grant: dict[AutonomyTier, EscalationOutcome]


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: int  # bumped on any `expected` change; the corpus is superseded, never edited away
    archetype: Archetype
    incident_id: str
    stack: GoldenStack
    title: str
    findings: list[SpecialistFinding]
    expected: GoldenExpectation
    labels: list[str] = Field(default_factory=list)
    provenance: dict[str, str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _stamp_incident_id(cls, data: Any) -> Any:
        """Findings in YAML omit incident_id (DRY); stamp the case's id onto each."""
        if isinstance(data, dict):
            iid = data.get("incident_id")
            findings = data.get("findings")
            if iid and isinstance(findings, list):
                for f in findings:
                    if isinstance(f, dict):
                        f.setdefault("incident_id", iid)
        return data


def golden_dir() -> Path:
    """The corpus root (``<repo>/golden`` by default; override with SENTINEL_GOLDEN_DIR)."""
    override = os.environ.get("SENTINEL_GOLDEN_DIR")
    return Path(override) if override else Path(__file__).resolve().parents[3] / "golden"


def load_golden(*, stratified: bool = False) -> list[GoldenCase]:
    """Load and validate every ``golden/**/*.yaml`` case, sorted by id.

    With ``stratified=True``, raise unless every archetype (§2.1) is represented — the
    CI coverage assertion.
    """
    cases = [
        GoldenCase.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in sorted(golden_dir().rglob("*.yaml"))
    ]
    cases.sort(key=lambda c: c.id)
    if stratified:
        missing = set(ARCHETYPES) - {c.archetype for c in cases}
        if missing:
            raise AssertionError(f"golden corpus missing archetypes: {sorted(missing)}")
    return cases

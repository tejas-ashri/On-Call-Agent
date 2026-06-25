"""Inter-agent contracts (doc 04 §4).

The Pydantic v2 models the Commander, specialists, and Planner exchange. Agents
communicate only through these validated shapes — never free text — which is
what makes the system debuggable, evaluable (doc 06), persistable (doc 02), and
brokerable (doc 05). Shared vocabulary (``ConfidenceBand``, ``AutonomyTier``) is
imported from :mod:`sentinel.enums` rather than redefined.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from sentinel.enums import AutonomyTier, ConfidenceBand

SpecialistRole = Literal["log", "metric", "trace", "change_deploy", "error"]

EscalationOutcome = Literal[
    "act_and_report",  # T3 autonomous
    "page_one_tap",  # T2 approval + drafted fix
    "page_with_drafted_fix",  # T0/T1 — never act
    "page_irreversible",  # irreversible → always page
    "page_low_confidence",  # LOW band → page with competing hypotheses
]

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class EvidenceRef(BaseModel):
    """A pointer to redacted evidence — never raw secrets (C2/P3)."""

    model_config = ConfigDict(frozen=True)

    event_id: str | None = None
    kind: Literal["log", "metric", "trace", "deploy", "error", "alert"]
    summary: str = Field(max_length=400)
    raw_ref: str | None = None
    observed_at: datetime


class SpecialistFinding(BaseModel):
    """Output of every specialist — one per role per (re)dispatch."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    role: SpecialistRole
    status: Literal["ok", "degraded", "no_signal"] = "ok"
    summary: str = Field(max_length=1_000)
    signals: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    suspected_cause: str | None = None
    self_confidence: Confidence = 0.0  # raw self-assessment — NOT used for autonomy
    tool_calls: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    """A candidate root cause synthesized by the Commander."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    statement: str = Field(max_length=600)
    supporting_roles: list[SpecialistRole] = Field(default_factory=list)
    contradicting_roles: list[SpecialistRole] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    raw_likelihood: Confidence = 0.0  # pre-calibration


class ConfidenceAssessment(BaseModel):
    """Output of ``score_confidence`` — the ONLY confidence the policy may read."""

    model_config = ConfigDict(frozen=True)

    incident_id: str
    top_hypothesis_id: str
    calibrated_confidence: Confidence  # post-calibration (P6, doc 06)
    band: ConfidenceBand
    corroboration_count: int = Field(ge=0)
    n_competing: int = Field(ge=0)
    calibrator_version: str


class RemediationPlan(BaseModel):
    """Planner output — the 'fix' attached to every page or executed at T2/T3."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    action_type: str
    target: str
    params: dict[str, object] = Field(default_factory=dict)
    reversible: bool  # drives the §00.6 matrix
    summary: str = Field(max_length=600)
    diff: str | None = None
    pr_url: str | None = None
    command: str | None = None
    rollback_ref: str | None = None
    estimated_blast_radius: Literal["single_service", "shared", "global"]


class EscalationDecision(BaseModel):
    """Output of the policy in doc 04 §7 — a pure function of its inputs."""

    model_config = ConfigDict(frozen=True)

    incident_id: str
    outcome: EscalationOutcome
    target_state: Literal["AWAITING_APPROVAL", "REMEDIATING", "ESCALATED"]
    band: ConfidenceBand
    granted_tier: AutonomyTier
    reversible: bool
    rationale: str = Field(max_length=600)


class PagePayload(BaseModel):
    """What a human receives — the 15-second-approval artifact."""

    model_config = ConfigDict(frozen=True)

    incident_id: str
    tenant_id: str
    suspected_root_cause: str
    confidence: ConfidenceAssessment
    evidence_chain: list[EvidenceRef]
    competing_hypotheses: list[Hypothesis]
    drafted_remediation: RemediationPlan | None
    one_tap_action_id: str | None = None
    outcome: str

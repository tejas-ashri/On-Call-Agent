"""Pluggable agent + executor interfaces for the incident graph (doc 04 §3).

The orchestrator (`run.py`) depends only on these structural Protocols, never on
concrete LLM clients or the credential broker — so it is testable offline with
deterministic fakes, and the real implementations (LLM-backed specialists, a
`ToolBroker`-backed executor) plug in unchanged. Every agent is tenant-bound by
the caller (doc 04 §1); these interfaces carry no ambient authority.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from sentinel.contracts import (
    CanonicalEvent,
    Hypothesis,
    RemediationPlan,
    SpecialistFinding,
    SpecialistRole,
)
from sentinel.contracts.actions import ActionResult


class InvestigationContext(BaseModel):
    """The redacted slice a specialist investigates (doc 04 §2 ``intake``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str
    tenant_id: str
    events: list[CanonicalEvent]


class Specialist(Protocol):
    """A domain specialist (Log/Metric/Trace/Change-Deploy/Error, doc 04 §3).

    Confined to its domain's tools; returns exactly one finding per dispatch.
    """

    role: SpecialistRole

    async def investigate(self, ctx: InvestigationContext) -> SpecialistFinding: ...


class Commander(Protocol):
    """The Incident Commander: synthesizes findings into ranked hypotheses (doc 04 §3)."""

    async def synthesize(
        self, incident_id: str, findings: Sequence[SpecialistFinding]
    ) -> list[Hypothesis]: ...


class Planner(Protocol):
    """Drafts a remediation for the leading hypothesis; never executes (doc 04 §8)."""

    async def draft(self, incident_id: str, hypothesis: Hypothesis) -> RemediationPlan: ...


class ActionExecutor(Protocol):
    """Executes an approved write (doc 04 §2 ``act``).

    The seam between the orchestrator's decision and the side effect: the real
    implementation routes through the MCP credential broker (doc 05 §2); tests
    inject a recording fake. The orchestrator decides; the executor acts.
    """

    async def execute(
        self, plan: RemediationPlan, *, tenant_id: str, incident_id: str
    ) -> ActionResult: ...

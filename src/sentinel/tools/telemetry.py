"""Read-only telemetry sources the specialists investigate with (doc 04 §3).

A :class:`TelemetrySource` answers "what did the tenant's stack observe?" for one
evidence kind. Specialists never see a source directly — they reach it through
their role's tools (:mod:`sentinel.tools.toolset`), so each one is confined to its
own domain's data.

:class:`GoldenTelemetry` is the offline source: it replays the redacted
observations recorded in a golden case (doc 06 §2), so the whole investigation
loop runs and is tested without provider credentials. A live source (Vercel,
Sentry, …) implements the same Protocol and plugs in unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from sentinel.contracts import EvidenceRef, SpecialistFinding
from sentinel.orchestrator import InvestigationContext

if TYPE_CHECKING:
    from sentinel.eval.golden import GoldenCase

EvidenceKind = Literal["log", "metric", "trace", "deploy", "error", "alert"]


class Observation(BaseModel):
    """One redacted data point a tool returned — the only thing evidence is built from."""

    model_config = ConfigDict(frozen=True)

    kind: EvidenceKind
    summary: str = Field(max_length=400)
    observed_at: datetime
    raw_ref: str | None = None
    event_id: str | None = None

    def to_evidence(self) -> EvidenceRef:
        return EvidenceRef(
            event_id=self.event_id,
            kind=self.kind,
            summary=self.summary,
            raw_ref=self.raw_ref,
            observed_at=self.observed_at,
        )


class TelemetrySource(Protocol):
    """A tenant-scoped, read-only view of one stack's telemetry."""

    async def fetch(
        self, kind: EvidenceKind, ctx: InvestigationContext, *, contains: str | None = None
    ) -> list[Observation]: ...


class GoldenTelemetry:
    """Replays a golden case's recorded observations, filtered per evidence kind.

    Only the raw observations are exposed — never a finding's ``summary`` or
    ``suspected_cause``, which are the answers the specialist must reach itself.
    """

    def __init__(self, findings: Sequence[SpecialistFinding]) -> None:
        self._observations = sorted(
            (
                Observation(
                    kind=ev.kind,
                    summary=ev.summary,
                    observed_at=ev.observed_at,
                    raw_ref=ev.raw_ref,
                    event_id=ev.event_id,
                )
                for f in findings
                for ev in f.evidence
            ),
            key=lambda o: o.observed_at,
        )

    @classmethod
    def from_case(cls, case: GoldenCase) -> GoldenTelemetry:
        return cls(case.findings)

    async def fetch(
        self, kind: EvidenceKind, ctx: InvestigationContext, *, contains: str | None = None
    ) -> list[Observation]:
        needle = contains.lower() if contains else None
        return [
            o
            for o in self._observations
            if o.kind == kind and (needle is None or needle in o.summary.lower())
        ]

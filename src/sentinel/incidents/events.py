"""The append-only incident event log (doc 02 §4.1, ADR-011).

``incident_events`` is the source of truth for an incident; the live
:class:`~sentinel.incidents.projection.IncidentProjection` is a pure fold over
it (doc 02 §4.3). This module owns the event *vocabulary* — the verbatim
``type`` strings of the doc 02 §4.1 table — and the immutable
:class:`IncidentEvent` envelope. The lifecycle these events express is doc 00
§5.3 (``OPEN → TRIAGING → DIAGNOSED → {AWAITING_APPROVAL | REMEDIATING |
ESCALATED} → RESOLVED → CLOSED``, optional ``MONITORING`` after
``REMEDIATING``).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class IncidentEventType(StrEnum):
    """The ``incident_events.type`` vocabulary, verbatim per doc 02 §4.1.

    Each member's value is the wire string persisted in the append-only log;
    the projection effect of each type is defined in the §4.1 table and applied
    by :func:`sentinel.incidents.projection.fold`.
    """

    INCIDENT_OPENED = "incident.opened"
    TRIAGE_STARTED = "triage.started"
    SPECIALIST_FINDING = "specialist.finding"
    HYPOTHESIS_FORMED = "hypothesis.formed"
    INCIDENT_DIAGNOSED = "incident.diagnosed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_DENIED = "approval.denied"
    REMEDIATION_STARTED = "remediation.started"
    MONITORING_STARTED = "monitoring.started"
    INCIDENT_ESCALATED = "incident.escalated"
    INCIDENT_RESOLVED = "incident.resolved"
    INCIDENT_CLOSED = "incident.closed"
    CHECKPOINT_SAVED = "checkpoint.saved"


class IncidentEvent(BaseModel):
    """One immutable fact appended to the incident log (doc 02 §4, ADR-011).

    Mirrors the ``incident_events`` row (doc 02 §2.1): ``(tenant_id,
    incident_id, seq)`` is the per-incident ordering key, ``seq`` is monotonic
    and gap-free, and the row is never mutated (P8). Frozen and closed to
    unknown fields so a persisted fact cannot drift after the fact; the
    ``payload`` is post-redaction (ADR-004, P3).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str
    tenant_id: str
    seq: int
    type: IncidentEventType
    payload: dict[str, object] = Field(default_factory=dict)
    actor: str
    occurred_at: datetime

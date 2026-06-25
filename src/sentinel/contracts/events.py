"""The canonical telemetry envelope (doc 00 §5.1)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from sentinel.enums import EventKind, Severity


class CanonicalEvent(BaseModel):
    """The normalized event every connector emits; the lingua franca of the system.

    Immutable once built (``frozen``) and closed to unknown fields
    (``extra="forbid"``). ``body_redacted`` is left empty by the connector's
    ``normalize()`` and filled by the edge redaction stage before persistence
    (ADR-004, P3).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    tenant_id: str
    integration_id: str
    source: str
    kind: EventKind
    service: str | None = None
    severity: Severity
    title: str
    body_redacted: str = ""
    fingerprint: str
    labels: dict[str, str] = Field(default_factory=dict)
    redaction_meta: dict[str, object] = Field(default_factory=dict)
    raw_ref: str | None = None
    occurred_at: datetime
    received_at: datetime

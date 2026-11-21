"""Which incidents are still open, and where each one stands (doc 01 §5, doc 04 §9).

The incident graph knows one incident at a time; the pipeline needs to know all
of a tenant's live ones. The registry answers two questions the pipeline could
not before:

* **Is this alert part of something already open?** Late alerts are correlated
  against the open incidents' events (:func:`sentinel.correlator.correlate_with_open`)
  instead of opening a duplicate incident.
* **Is this incident finished, or paused waiting for a human?** A run that hit the
  approval ``interrupt()`` is ``awaiting_approval`` — not done — and
  :func:`sentinel.edge.pipeline.resume_incident` continues it.

``escalated`` incidents stay open (a human owns them now) until :meth:`resolve`;
``closed`` ones do not, so a recurrence after a successful fix is a new incident.
:class:`InMemoryIncidentRegistry` is the offline default; production backs the
Protocol with the ``incidents`` table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from sentinel.contracts import CanonicalEvent

#: ``failed`` is a run that crashed: it is not open, so the next alert retries it.
IncidentStatus = Literal["running", "awaiting_approval", "escalated", "closed", "failed"]

#: Statuses whose incidents new alerts may still join.
OPEN_STATUSES: frozenset[IncidentStatus] = frozenset(
    {"running", "awaiting_approval", "escalated"}
)


class TrackedIncident(BaseModel):
    """One incident's lifecycle status, its graph thread, and every event it holds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str
    tenant_id: str
    thread_id: str  # the incident graph's checkpoint thread
    status: IncidentStatus
    events: list[CanonicalEvent]
    opened_at: datetime
    action_id: str | None = None  # the one-tap token while awaiting approval

    @property
    def open(self) -> bool:
        return self.status in OPEN_STATUSES


class IncidentRegistry(Protocol):
    async def get(self, incident_id: str) -> TrackedIncident | None: ...

    async def save(self, incident: TrackedIncident) -> None: ...

    async def open_for(self, tenant_id: str) -> list[TrackedIncident]:
        """The tenant's open incidents, oldest first."""
        ...

    async def resolve(self, incident_id: str) -> None:
        """A human closed an escalated incident; later alerts open a new one."""
        ...


class InMemoryIncidentRegistry:
    """An offline :class:`IncidentRegistry` (process-local)."""

    def __init__(self) -> None:
        self._incidents: dict[str, TrackedIncident] = {}

    async def get(self, incident_id: str) -> TrackedIncident | None:
        return self._incidents.get(incident_id)

    async def save(self, incident: TrackedIncident) -> None:
        self._incidents[incident.incident_id] = incident

    async def open_for(self, tenant_id: str) -> list[TrackedIncident]:
        return sorted(
            (i for i in self._incidents.values() if i.tenant_id == tenant_id and i.open),
            key=lambda i: i.opened_at,
        )

    async def resolve(self, incident_id: str) -> None:
        """A human closed an escalated incident; later alerts open a new one."""
        if (incident := self._incidents.get(incident_id)) is not None:
            self._incidents[incident_id] = incident.model_copy(update={"status": "closed"})

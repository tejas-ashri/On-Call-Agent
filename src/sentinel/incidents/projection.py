"""The incident projection and its pure fold (doc 02 §4.3, ADR-011).

The ``incidents`` row is a *projection* rebuilt by folding ``incident_events``
in ``seq`` order (doc 02 §4). :func:`fold` is the pure
``(projection, event) -> projection`` transition of the §4.1 table; because it
is pure and ordered, the same events always yield the same projection — the
foundation of replay (doc 02 §4.4) and of the lifecycle in doc 00 §5.3.
:func:`project` is the full replay that also enforces the log's integrity
invariants (gap-free ``seq`` from 1, first event ``incident.opened``).
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from sentinel.enums import ConfidenceBand, IncidentState
from sentinel.incidents.events import IncidentEvent, IncidentEventType

# Event types that set the projection's ``state`` to a fixed value (doc 02 §4.1).
_STATE_TRANSITIONS: dict[IncidentEventType, IncidentState] = {
    IncidentEventType.TRIAGE_STARTED: IncidentState.TRIAGING,
    IncidentEventType.INCIDENT_DIAGNOSED: IncidentState.DIAGNOSED,
    IncidentEventType.APPROVAL_REQUESTED: IncidentState.AWAITING_APPROVAL,
    IncidentEventType.APPROVAL_GRANTED: IncidentState.REMEDIATING,
    IncidentEventType.APPROVAL_DENIED: IncidentState.ESCALATED,
    IncidentEventType.REMEDIATION_STARTED: IncidentState.REMEDIATING,
    IncidentEventType.MONITORING_STARTED: IncidentState.MONITORING,
    IncidentEventType.INCIDENT_ESCALATED: IncidentState.ESCALATED,
    IncidentEventType.INCIDENT_RESOLVED: IncidentState.RESOLVED,
    IncidentEventType.INCIDENT_CLOSED: IncidentState.CLOSED,
}


class IncidentProjection(BaseModel):
    """The live incident view folded from ``incident_events`` (doc 02 §4, §2.1).

    Field names are canonical per doc 00 §5.3 / doc 02 §2.1. ``version`` equals
    the last applied ``incident_events.seq`` (the optimistic-concurrency token,
    doc 02 §4.2). Closed to unknown fields; not frozen because the fold
    *replaces* it via ``model_copy`` rather than mutating in place — see
    :func:`fold`.
    """

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    tenant_id: str
    state: IncidentState
    confidence_band: ConfidenceBand | None = None
    root_cause: str | None = None
    primary_service: str | None = None
    version: int


def fold(projection: IncidentProjection | None, event: IncidentEvent) -> IncidentProjection:
    """Apply one event to the projection, returning a new instance (doc 02 §4.3).

    Pure: the input ``projection`` is never mutated (a new value is produced via
    construction / ``model_copy``), so replaying the same events always yields
    the same result (doc 02 §4.4). ``version`` is always set to ``event.seq``.
    The per-``type`` effect follows the doc 02 §4.1 table:

    * ``incident.opened`` creates the row in ``OPEN`` and sets ``primary_service``
      from the payload (``projection`` must be ``None`` here).
    * ``hypothesis.formed`` sets ``root_cause`` and ``confidence_band`` from the
      payload without changing ``state``.
    * ``specialist.finding`` / ``checkpoint.saved`` change no domain field.
    * every other type sets ``state`` per :data:`_STATE_TRANSITIONS`.
    """
    if event.type is IncidentEventType.INCIDENT_OPENED:
        if projection is not None:
            raise ValueError("incident.opened may only be the first event")
        primary_service = event.payload.get("primary_service")
        return IncidentProjection(
            incident_id=event.incident_id,
            tenant_id=event.tenant_id,
            state=IncidentState.OPEN,
            primary_service=primary_service if isinstance(primary_service, str) else None,
            version=event.seq,
        )

    if projection is None:
        raise ValueError(
            f"first event must be {IncidentEventType.INCIDENT_OPENED!s}, got {event.type!s}"
        )

    updates: dict[str, object] = {"version": event.seq}

    if event.type is IncidentEventType.HYPOTHESIS_FORMED:
        root_cause = event.payload.get("root_cause")
        if isinstance(root_cause, str):
            updates["root_cause"] = root_cause
        band = event.payload.get("confidence_band")
        if isinstance(band, str):
            updates["confidence_band"] = ConfidenceBand(band)
    elif event.type in _STATE_TRANSITIONS:
        updates["state"] = _STATE_TRANSITIONS[event.type]
    # specialist.finding / checkpoint.saved: no domain change beyond version.

    return projection.model_copy(update=updates)


def project(events: Iterable[IncidentEvent]) -> IncidentProjection:
    """Fold an event stream into a projection, enforcing log integrity (doc 02 §4).

    Events are applied in arrival order, which MUST already be ``seq`` order:
    ``seq`` must be the contiguous sequence ``1, 2, 3, …`` (doc 02 §2.1's
    gap-free per-incident ordering, §4.2's append invariant). A gap, duplicate,
    or out-of-order ``seq`` raises :class:`ValueError`, as does an empty stream
    or a first event that is not ``incident.opened``.
    """
    projection: IncidentProjection | None = None
    expected_seq = 1
    for event in events:
        if event.seq != expected_seq:
            raise ValueError(
                f"non-contiguous incident_events.seq: expected {expected_seq}, got {event.seq}"
            )
        if projection is None and event.type is not IncidentEventType.INCIDENT_OPENED:
            raise ValueError(
                f"first event must be {IncidentEventType.INCIDENT_OPENED!s}, got {event.type!s}"
            )
        projection = fold(projection, event)
        expected_seq += 1

    if projection is None:
        raise ValueError("cannot project an empty incident_events stream")
    return projection

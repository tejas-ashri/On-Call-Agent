"""Event-sourced incident repository (doc 02 §4).

``incident_events`` is the append-only source of truth; the ``incidents`` row is a
projection rebuilt by folding events in ``seq`` order (P8). The fold is a pure function
of ``(projection, event) -> projection``, so the same events always yield the same
projection — the foundation of replay and the golden-incident evals (doc 06).

Every function here takes an already-RLS-scoped :class:`AsyncSession` (see
:func:`sentinel.data.session.tenant_session`), so all reads and writes are confined to
one tenant by the database itself. Notably ``rebuild_incident`` overwrites the
projection **in place via UPDATE** rather than DELETE+INSERT: the app role is granted no
DELETE on the projection (doc 02 §3.3), and respecting that least-privilege grant is the
point — the rebuild needs no elevated role.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# §4.1 event vocabulary → resulting projection state. Types not present here
# (e.g. specialist.finding, hypothesis.formed) do not change state.
_STATE_TRANSITIONS: dict[str, str] = {
    "incident.opened": "OPEN",
    "triage.started": "TRIAGING",
    "incident.diagnosed": "DIAGNOSED",
    "approval.requested": "AWAITING_APPROVAL",
    "approval.granted": "REMEDIATING",
    "approval.denied": "ESCALATED",
    "remediation.started": "REMEDIATING",
    "monitoring.started": "MONITORING",
    "incident.escalated": "ESCALATED",
    "incident.resolved": "RESOLVED",
    "incident.closed": "CLOSED",
}


@dataclass(frozen=True, slots=True)
class IncidentProjection:
    """The folded view of an incident — mirrors the durable ``incidents`` row."""

    state: str
    root_cause: str | None
    confidence_band: str | None
    version: int


def _as_dict(payload: object) -> dict[str, object]:
    """asyncpg returns jsonb as a JSON string; tolerate str | dict | None."""
    if isinstance(payload, str):
        loaded = json.loads(payload) if payload else {}
        return loaded if isinstance(loaded, dict) else {}
    if isinstance(payload, dict):
        return payload
    return {}


def fold(
    proj: IncidentProjection, seq: int, type_: str, payload: Mapping[str, object]
) -> IncidentProjection:
    """Pure (projection, event) -> projection. The single source of projection truth."""
    state = _STATE_TRANSITIONS.get(type_, proj.state)
    root_cause = proj.root_cause
    band = proj.confidence_band
    if type_ == "hypothesis.formed":
        rc = payload.get("root_cause")
        cb = payload.get("confidence_band")
        if isinstance(rc, str):
            root_cause = rc
        if isinstance(cb, str):
            band = cb
    return IncidentProjection(state=state, root_cause=root_cause, confidence_band=band, version=seq)


async def insert_incident(
    session: AsyncSession,
    *,
    incident_id: str,
    tenant_id: str,
    state: str = "OPEN",
    title: str | None = None,
) -> None:
    """Insert a projection row directly (used by the isolation proof, not event sourcing)."""
    await session.execute(
        text(
            "INSERT INTO incidents (incident_id, tenant_id, state, title) "
            "VALUES (:iid, :tid, :state, :title)"
        ),
        {"iid": incident_id, "tid": tenant_id, "state": state, "title": title},
    )


async def append_incident_event(
    session: AsyncSession,
    *,
    tenant_id: str,
    incident_id: str,
    type_: str,
    payload: Mapping[str, object] | None = None,
    actor: str,
) -> int:
    """Append the next ``seq`` for an incident and apply it to the projection (§4.2).

    Append + projection happen in one transaction under one ``app.tenant_id`` scope, so
    the projection can never drift from the log within a committed unit of work.
    """
    data = dict(payload or {})
    seq = int(
        (
            await session.execute(
                text(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM incident_events "
                    "WHERE incident_id = :iid"
                ),
                {"iid": incident_id},
            )
        ).scalar_one()
    )

    await session.execute(
        text(
            "INSERT INTO incident_events "
            "(incident_event_id, tenant_id, incident_id, seq, type, payload, actor) "
            "VALUES (gen_random_uuid(), :tid, :iid, :seq, :type, CAST(:payload AS jsonb), :actor)"
        ),
        {
            "tid": tenant_id,
            "iid": incident_id,
            "seq": seq,
            "type": type_,
            "payload": json.dumps(data),
            "actor": actor,
        },
    )

    await _apply_to_projection(
        session, tenant_id=tenant_id, incident_id=incident_id, seq=seq, type_=type_, payload=data
    )
    return seq


async def _apply_to_projection(
    session: AsyncSession,
    *,
    tenant_id: str,
    incident_id: str,
    seq: int,
    type_: str,
    payload: Mapping[str, object],
) -> None:
    """Apply one event to the durable projection. incident.opened creates the row;
    every later event UPDATEs it, COALESCE-preserving fields the event doesn't touch."""
    if type_ == "incident.opened":
        await session.execute(
            text(
                "INSERT INTO incidents (incident_id, tenant_id, state, version) "
                "VALUES (:iid, :tid, 'OPEN', :seq)"
            ),
            {"iid": incident_id, "tid": tenant_id, "seq": seq},
        )
        return

    new_state = _STATE_TRANSITIONS.get(type_)
    rc = payload.get("root_cause") if type_ == "hypothesis.formed" else None
    cb = payload.get("confidence_band") if type_ == "hypothesis.formed" else None
    await session.execute(
        text(
            "UPDATE incidents SET "
            "state = COALESCE(:state, state), "
            "root_cause = COALESCE(:rc, root_cause), "
            "confidence_band = COALESCE(:band, confidence_band), "
            "version = :seq "
            "WHERE incident_id = :iid AND tenant_id = :tid"
        ),
        {
            "state": new_state,
            "rc": rc if isinstance(rc, str) else None,
            "band": cb if isinstance(cb, str) else None,
            "seq": seq,
            "iid": incident_id,
            "tid": tenant_id,
        },
    )


async def rebuild_incident(
    session: AsyncSession, *, tenant_id: str, incident_id: str
) -> IncidentProjection:
    """Reconstruct the projection from scratch by folding the event log (§4.4).

    Used as the correctness check in CI: a freshly folded projection must equal the live
    one. RLS scopes the read; we overwrite in place (UPDATE) to stay within the app
    role's grants — it has no DELETE on the projection.
    """
    rows = (
        await session.execute(
            text(
                "SELECT seq, type, payload FROM incident_events "
                "WHERE incident_id = :iid ORDER BY seq ASC"
            ),
            {"iid": incident_id},
        )
    ).all()

    proj = IncidentProjection(state="OPEN", root_cause=None, confidence_band=None, version=0)
    for row in rows:
        proj = fold(proj, int(row[0]), str(row[1]), _as_dict(row[2]))

    await session.execute(
        text(
            "UPDATE incidents SET state = :state, root_cause = :rc, "
            "confidence_band = :band, version = :ver "
            "WHERE incident_id = :iid AND tenant_id = :tid"
        ),
        {
            "state": proj.state,
            "rc": proj.root_cause,
            "band": proj.confidence_band,
            "ver": proj.version,
            "iid": incident_id,
            "tid": tenant_id,
        },
    )
    return proj

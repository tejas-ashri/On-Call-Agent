"""Durable inbox/outbox idempotency over Postgres (doc 03 §3).

Providers retry; a naive handler would manufacture duplicate ``CanonicalEvent``s — duplicate
incidents, duplicate pages, at worst a double write (a C1 violation). The fix is the
inbox/outbox pattern with a deterministic idempotency key:

* **Inbox** — every delivery is recorded with ``UNIQUE(integration_id, idempotency_key)``.
  A redelivery hits the constraint (``ON CONFLICT DO NOTHING``) and is acknowledged without
  reprocessing, so we never double-emit.
* **Outbox** — ``normalize()``'s events are written to ``event_outbox`` *in the same
  transaction as the inbox insert*; a relay drains them to the bus. This makes delivery
  exactly-once **relative to the inbox decision** even if the process crashes mid-flight.

Every statement is RLS-scoped to the tenant (doc 02 §3). Reuses the deterministic key from
:func:`sentinel.connectors.idempotency.idempotency_key` at the call sites.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, str):
        loaded = json.loads(value) if value else {}
        return loaded if isinstance(loaded, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


async def inbox_insert(
    session: AsyncSession, *, tenant_id: str, integration_id: str, idempotency_key: str
) -> bool:
    """Record a delivery; return True if new, False if a duplicate (deduped by the unique key)."""
    row = (
        await session.execute(
            text(
                "INSERT INTO webhook_inbox (inbox_id, tenant_id, integration_id, idempotency_key) "
                "VALUES (gen_random_uuid(), :t, :i, :k) "
                "ON CONFLICT (integration_id, idempotency_key) DO NOTHING "
                "RETURNING inbox_id"
            ),
            {"t": tenant_id, "i": integration_id, "k": idempotency_key},
        )
    ).first()
    return row is not None


async def outbox_enqueue(
    session: AsyncSession, *, tenant_id: str, events: Sequence[Mapping[str, object]]
) -> int:
    """Write each (redacted) CanonicalEvent into the outbox as ``pending``; return the count."""
    for event in events:
        await session.execute(
            text(
                "INSERT INTO event_outbox (outbox_id, tenant_id, payload) "
                "VALUES (gen_random_uuid(), :t, CAST(:p AS jsonb))"
            ),
            {"t": tenant_id, "p": json.dumps(dict(event))},
        )
    return len(events)


async def accept_webhook(
    session: AsyncSession,
    *,
    tenant_id: str,
    integration_id: str,
    idempotency_key: str,
    events: Sequence[Mapping[str, object]],
) -> bool:
    """Inbox-dedup then enqueue, in the caller's single transaction.

    Returns True if this delivery was processed, False if it was a duplicate (acknowledged
    but not re-emitted). The atomic inbox+outbox commit is what makes the relay exactly-once
    relative to the accept decision.
    """
    if not await inbox_insert(
        session, tenant_id=tenant_id, integration_id=integration_id, idempotency_key=idempotency_key
    ):
        return False
    await outbox_enqueue(session, tenant_id=tenant_id, events=events)
    return True


async def outbox_drain(session: AsyncSession, *, tenant_id: str) -> list[dict[str, object]]:
    """Claim all pending outbox rows and mark them relayed atomically; return their payloads."""
    rows = (
        await session.execute(
            text(
                "UPDATE event_outbox SET status = 'relayed', relayed_at = now() "
                "WHERE tenant_id = :t AND status = 'pending' RETURNING payload"
            ),
            {"t": tenant_id},
        )
    ).all()
    return [_as_dict(r[0]) for r in rows]

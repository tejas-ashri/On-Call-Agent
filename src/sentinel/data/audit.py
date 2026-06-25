"""Durable audit hash-chain over Postgres ``audit_log`` (doc 05 §5, doc 02 §3.3).

Reuses the pure mechanism in :mod:`sentinel.audit`. Appends are serialized per tenant by
the ``UNIQUE(tenant_id, seq)`` constraint, RLS scopes every read/write to the tenant, and
the append-only grants (no ``UPDATE``/``DELETE`` for the app role) mean the application
role *cannot* rewrite history — only a privileged operator could, and :func:`verify_audit`
would catch even that.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.audit import GENESIS, AuditEntry, VerifyResult, entry_hash, verify_chain


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, str):
        loaded = json.loads(value) if value else {}
        return loaded if isinstance(loaded, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


async def append_audit(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor: str,
    action: str,
    at: datetime,
    target: str | None = None,
    detail: Mapping[str, object] | None = None,
) -> tuple[int, bytes]:
    """Append one tamper-evident entry for ``tenant_id`` and return ``(seq, entry_hash)``."""
    last = (
        await session.execute(
            text(
                "SELECT entry_hash, seq FROM audit_log "
                "WHERE tenant_id = :t ORDER BY seq DESC LIMIT 1"
            ),
            {"t": tenant_id},
        )
    ).first()
    prev_hash, seq = (GENESIS, 1) if last is None else (bytes(last[0]), int(last[1]) + 1)

    det = dict(detail or {})
    row: dict[str, object] = {
        "tenant_id": tenant_id,
        "seq": seq,
        "actor": actor,
        "action": action,
        "target": target,
        "detail": det,
        "at": at,
    }
    digest = entry_hash(seq, prev_hash, row)
    await session.execute(
        text(
            "INSERT INTO audit_log "
            "(audit_id, tenant_id, seq, actor, action, target, detail, entry_hash, prev_hash, at) "
            "VALUES (gen_random_uuid(), :t, :seq, :actor, :action, :target, "
            "CAST(:detail AS jsonb), :eh, :ph, :at)"
        ),
        {
            "t": tenant_id,
            "seq": seq,
            "actor": actor,
            "action": action,
            "target": target,
            "detail": json.dumps(det),
            "eh": digest,
            "ph": prev_hash,
            "at": at,
        },
    )
    return seq, digest


async def verify_audit(session: AsyncSession, tenant_id: str) -> VerifyResult:
    """Read the tenant's chain in seq order and verify it (see sentinel.audit.verify_chain)."""
    rows = (
        await session.execute(
            text(
                "SELECT tenant_id, seq, actor, action, target, detail, at, prev_hash, entry_hash "
                "FROM audit_log WHERE tenant_id = :t ORDER BY seq ASC"
            ),
            {"t": tenant_id},
        )
    ).all()
    entries = [
        AuditEntry(
            tenant_id=str(r[0]),
            seq=int(r[1]),
            actor=str(r[2]),
            action=str(r[3]),
            target=str(r[4]) if r[4] is not None else None,
            detail=_as_dict(r[5]),
            at=r[6],
            prev_hash=bytes(r[7]),
            entry_hash=bytes(r[8]),
        )
        for r in rows
    ]
    return verify_chain(entries)

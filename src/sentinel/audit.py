"""Tamper-evident per-tenant audit-log hash-chain (doc 05 §5).

doc 02 owns the ``audit_log`` *table*; this module owns the *mechanism* that makes it
tamper-evident and independently verifiable. Each entry's ``entry_hash`` is
``SHA-256(seq ‖ prev_hash ‖ canonical(row))`` with length-prefixed framing (``‖``);
``prev_hash`` is the previous entry's hash for the same tenant, genesis = 32 zero bytes.
Altering or deleting any historical entry changes its hash and breaks every later link —
:func:`verify_chain` detects exactly that, and doc 02 §3.3's append-only grants (no
``UPDATE``/``DELETE`` for the app role) mean the application cannot rewrite history at all.

:func:`canonical` serializes only the *semantic* fields (sorted keys, UTC isoformat), so
the hash is reproducible byte-for-byte by anyone holding the row — the property that lets
a tenant export their chain and verify it independently (doc 05 §5.3).

Pure and stdlib-only (``hashlib``, ``json``); the durable append/verify over Postgres
lives in :mod:`sentinel.data.audit` and reuses these functions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

GENESIS = b"\x00" * 32  # genesis prev_hash: 32 zero bytes (doc 02 audit_log)


def _lp(b: bytes) -> bytes:
    """Length-prefixed framing: an 8-byte big-endian length, then the bytes.

    Framing removes concatenation ambiguity — ``a ‖ b`` can't collide with ``a' ‖ b'``.
    """
    return len(b).to_bytes(8, "big") + b


def canonical(row: Mapping[str, object]) -> bytes:
    """Deterministically serialize the semantic fields (sorted keys, UTC isoformat)."""
    at = row.get("at")
    payload = {
        "tenant_id": row["tenant_id"],
        "seq": row["seq"],
        "actor": row["actor"],
        "action": row["action"],
        "target": row.get("target"),
        "detail": row.get("detail") or {},  # already redacted — the log records authority, not data
        "at": at.isoformat() if isinstance(at, datetime) else at,
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def entry_hash(seq: int, prev_hash: bytes, row: Mapping[str, object]) -> bytes:
    """``SHA-256(seq ‖ prev_hash ‖ canonical(row))`` → the 32-byte ``entry_hash``."""
    h = hashlib.sha256()
    h.update(_lp(seq.to_bytes(8, "big")))
    h.update(_lp(prev_hash))
    h.update(_lp(canonical(row)))
    return h.digest()


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One link in a tenant's hash chain — mirrors a durable ``audit_log`` row."""

    tenant_id: str
    seq: int
    actor: str
    action: str
    target: str | None
    detail: Mapping[str, object]
    at: datetime
    prev_hash: bytes
    entry_hash: bytes


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of walking a chain; ``head`` doubles as a checkpointable anchor (doc 05 §5.3)."""

    ok: bool
    broken_at: int | None = None
    reason: str | None = None
    head: bytes | None = None


def _row_of(entry: AuditEntry) -> dict[str, object]:
    return {
        "tenant_id": entry.tenant_id,
        "seq": entry.seq,
        "actor": entry.actor,
        "action": entry.action,
        "target": entry.target,
        "detail": entry.detail,
        "at": entry.at,
    }


def verify_chain(entries: Sequence[AuditEntry]) -> VerifyResult:
    """Walk entries in seq order, recompute each hash, and assert linkage + contiguity.

    O(n) and dependent only on the rows themselves — anyone holding an exported chain can
    run it. Returns the first break (seq + reason) or ``ok`` with the head hash.
    """
    prev_hash = GENESIS
    expected_seq = 1
    for e in entries:
        if e.seq != expected_seq:
            return VerifyResult(ok=False, broken_at=e.seq, reason="non-contiguous seq")
        if e.prev_hash != prev_hash:
            return VerifyResult(ok=False, broken_at=e.seq, reason="broken prev_hash linkage")
        if e.entry_hash != entry_hash(e.seq, prev_hash, _row_of(e)):
            return VerifyResult(ok=False, broken_at=e.seq, reason="entry_hash mismatch")
        prev_hash = e.entry_hash
        expected_seq += 1
    return VerifyResult(ok=True, head=prev_hash)


class AuditLog:
    """An in-memory per-tenant hash chain.

    The durable variant (appending to Postgres ``audit_log`` with RLS scoping and the
    ``UNIQUE(tenant_id, seq)`` serializer) lives in :mod:`sentinel.data.audit`; this one is
    for offline use and tests. ``at`` is supplied by the caller to keep appends deterministic.
    """

    def __init__(self) -> None:
        self._chains: dict[str, list[AuditEntry]] = {}

    def append(
        self,
        *,
        tenant_id: str,
        actor: str,
        action: str,
        at: datetime,
        target: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> AuditEntry:
        chain = self._chains.setdefault(tenant_id, [])
        prev_hash = chain[-1].entry_hash if chain else GENESIS
        seq = len(chain) + 1
        det: Mapping[str, object] = detail or {}
        row = {
            "tenant_id": tenant_id,
            "seq": seq,
            "actor": actor,
            "action": action,
            "target": target,
            "detail": det,
            "at": at,
        }
        entry = AuditEntry(
            tenant_id=tenant_id,
            seq=seq,
            actor=actor,
            action=action,
            target=target,
            detail=det,
            at=at,
            prev_hash=prev_hash,
            entry_hash=entry_hash(seq, prev_hash, row),
        )
        chain.append(entry)
        return entry

    def entries(self, tenant_id: str) -> list[AuditEntry]:
        return list(self._chains.get(tenant_id, []))

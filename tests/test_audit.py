"""Tamper-evident audit-log hash-chain (doc 05 §5), pure mechanism.

Each entry's ``entry_hash`` is ``SHA-256(seq ‖ prev_hash ‖ canonical(row))`` with
length-prefixed framing and a genesis ``prev_hash`` of 32 zero bytes. Altering or
deleting any historical entry changes its hash and breaks every later link — which
``verify_chain`` detects. Pure and offline (hashlib only); the durable Postgres-backed
variant is exercised under the skip-gated DB tests.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from sentinel.audit import GENESIS, AuditLog, canonical, entry_hash, verify_chain

_T = datetime(2026, 6, 25, 2, 0, tzinfo=UTC)


def _three() -> AuditLog:
    log = AuditLog()
    log.append(
        tenant_id="ten_a", actor="human:u1", action="grant.created",
        target="deploy.rollback", detail={"max_tier": "T2"}, at=_T,
    )
    log.append(
        tenant_id="ten_a", actor="system", action="action.executed",
        target="svc_api", detail={"action_type": "deploy.rollback"}, at=_T,
    )
    log.append(
        tenant_id="ten_a", actor="human:u1", action="grant.revoked",
        target="deploy.rollback", detail={}, at=_T,
    )
    return log


def test_genesis_is_32_zero_bytes() -> None:
    assert GENESIS == b"\x00" * 32


def test_chain_links_and_verifies() -> None:
    entries = _three().entries("ten_a")
    assert [e.seq for e in entries] == [1, 2, 3]
    assert entries[0].prev_hash == GENESIS
    assert entries[1].prev_hash == entries[0].entry_hash
    assert entries[2].prev_hash == entries[1].entry_hash
    result = verify_chain(entries)
    assert result.ok and result.head == entries[-1].entry_hash


def test_per_tenant_chains_are_independent() -> None:
    log = _three()
    log.append(tenant_id="ten_b", actor="system", action="integration.connected", at=_T)
    assert [e.seq for e in log.entries("ten_b")] == [1]  # B's chain starts fresh at genesis
    assert log.entries("ten_b")[0].prev_hash == GENESIS
    assert verify_chain(log.entries("ten_b")).ok


def test_tampering_detail_breaks_chain() -> None:
    entries = _three().entries("ten_a")
    forged = list(entries)
    forged[1] = replace(forged[1], detail={"action_type": "rm -rf"})  # edit, don't re-hash
    result = verify_chain(forged)
    assert not result.ok
    assert result.broken_at == 2


def test_deleting_an_entry_is_detected() -> None:
    entries = _three().entries("ten_a")
    result = verify_chain([entries[0], entries[2]])  # drop seq 2
    assert not result.ok
    assert result.broken_at == 3


def test_canonical_is_order_independent() -> None:
    base = {
        "tenant_id": "t", "seq": 1, "actor": "a", "action": "x",
        "target": None, "detail": {"b": 2, "a": 1}, "at": _T,
    }
    assert canonical(base) == canonical({**base, "detail": {"a": 1, "b": 2}})


def test_entry_hash_is_32_bytes_and_depends_on_prev() -> None:
    row = {
        "tenant_id": "t", "seq": 1, "actor": "a", "action": "x",
        "target": None, "detail": {}, "at": _T,
    }
    h1 = entry_hash(1, GENESIS, row)
    h2 = entry_hash(1, b"\x11" * 32, row)
    assert len(h1) == 32
    assert h1 != h2  # prev_hash is mixed in → re-parenting changes the hash

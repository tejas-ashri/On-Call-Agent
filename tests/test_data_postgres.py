"""Cross-tenant isolation proof + event sourcing over a REAL Postgres (doc 02 §6, §4).

ADR-001 mandates a test proving tenant A cannot read tenant B — including via a *forged*
filter — against a real database with the §3 policies applied, connected as the
NOBYPASSRLS app role. This is that test, plus an event-sourced append/projection/rebuild
check that exercises the repository under RLS.

It **skips cleanly when Postgres is unreachable**, so the rest of the suite stays offline.
Provision locally with:  PYTHONPATH=src ./.venv/bin/python -m sentinel.data.bootstrap

The fixture re-provisions schema (and, unless SENTINEL_TEST_RLS=0, the policies) each
session, so the test is self-contained. Running once with SENTINEL_TEST_RLS=0 is how you
watch the isolation asserts FAIL pre-RLS — proof the test catches a breach, and that RLS,
not application code, is what closes it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Coroutine, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel.audit import VerifyResult
from sentinel.data import (
    append_incident_event,
    insert_incident,
    make_engine,
    rebuild_incident,
    session_factory,
    tenant_session,
)
from sentinel.data.audit import append_audit, verify_audit
from sentinel.data.inbox_outbox import accept_webhook, outbox_drain
from sentinel.data.repository import IncidentProjection

# Canonical uuids. (In the app, ULIDs render to inc_…/evt_… strings backed by these uuids;
# the SQL isolation proof is about RLS, not id formatting, so we use plain uuids.)
A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
INC_A = "11111111-1111-1111-1111-111111111111"
INC_B = "22222222-2222-2222-2222-222222222222"
INC_ES = "33333333-3333-3333-3333-333333333333"
_AUDIT_T = datetime(2026, 6, 25, 2, 0, tzinfo=UTC)
_INT = "dddddddd-dddd-dddd-dddd-dddddddddddd"

def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _pg_ready() -> bool:
    """True iff asyncpg + the data layer import AND a Postgres server answers."""
    try:
        import asyncpg  # noqa: F401

        from sentinel.data import bootstrap
    except Exception:
        return False

    async def _check() -> bool:
        try:
            conn = await bootstrap._connect("postgres")
            await conn.close()
            return True
        except Exception:
            return False

    try:
        return _run(_check())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_ready(), reason="Postgres not reachable")

_RLS_ON = os.environ.get("SENTINEL_TEST_RLS", "1") != "0"


@asynccontextmanager
async def _app() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a session factory bound to a fresh, disposed-after engine (loop-safe)."""
    engine = make_engine()
    try:
        yield session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _schema() -> bool:
    """Provision schema once per session; policies unless SENTINEL_TEST_RLS=0."""
    from sentinel.data import bootstrap

    _run(bootstrap.setup(with_policies=_RLS_ON))
    return _RLS_ON


@pytest.fixture
def two_tenants() -> None:
    """Clean DB, seed tenants A & B, and one incident each — seeded AS THAT TENANT."""
    from sentinel.data import bootstrap

    async def _seed() -> None:
        conn = await bootstrap._connect(bootstrap._DB_NAME)
        try:
            await conn.execute("TRUNCATE incidents, incident_events, tenants CASCADE")
            await conn.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES ($1, 'ten_a'), ($2, 'ten_b')",
                A,
                B,
            )
        finally:
            await conn.close()
        async with _app() as f:
            async with tenant_session(f, A) as s:  # write also passes through RLS WITH CHECK
                await insert_incident(s, incident_id=INC_A, tenant_id=A)
            async with tenant_session(f, B) as s:
                await insert_incident(s, incident_id=INC_B, tenant_id=B)

    _run(_seed())


# ── §6: the cross-tenant isolation proof ──────────────────────────────────────


def test_a_cannot_select_b(two_tenants: None) -> None:
    """Scoped to A, a plain SELECT returns ONLY A's rows."""

    async def _t() -> Sequence[object]:
        async with _app() as f, tenant_session(f, A) as s:
            return (await s.execute(text("SELECT tenant_id FROM incidents"))).scalars().all()

    rows = _run(_t())
    assert rows, "A must see its own row"
    assert all(str(r) == A for r in rows), "A saw a foreign tenant's row — RLS BREACH"


def test_forged_filter_is_powerless(two_tenants: None) -> None:
    """Even an explicit, crafted WHERE tenant_id = B returns nothing while scoped to A.

    RLS ANDs its own predicate onto ours; the forged filter cannot widen visibility.
    """

    async def _t() -> Sequence[object]:
        async with _app() as f, tenant_session(f, A) as s:
            return (
                await s.execute(text("SELECT * FROM incidents WHERE tenant_id = :b"), {"b": B})
            ).all()

    assert _run(_t()) == [], "Forged tenant_id filter leaked B's data — RLS BREACH"


def test_cannot_insert_for_another_tenant(two_tenants: None) -> None:
    """WITH CHECK blocks stamping a row with someone else's tenant_id."""

    async def _t() -> None:
        async with _app() as f, tenant_session(f, A) as s:
            await s.execute(
                text(
                    "INSERT INTO incidents (incident_id, tenant_id, state) "
                    "VALUES (:i, :b, 'OPEN')"
                ),
                {"i": "44444444-4444-4444-4444-444444444444", "b": B},
            )

    with pytest.raises(ProgrammingError):  # RLS WITH CHECK → asyncpg policy violation
        _run(_t())


def test_missing_guc_is_fail_closed(two_tenants: None) -> None:
    """A connection that NEVER set app.tenant_id sees NOTHING (NULL matches nothing)."""

    async def _t() -> Sequence[object]:
        # deliberately NOT via tenant_session() → app.tenant_id never set
        async with _app() as f, f() as s, s.begin():
            return (await s.execute(text("SELECT * FROM incidents"))).all()

    assert _run(_t()) == [], "Unset app.tenant_id was fail-OPEN — must be fail-closed"


def test_app_role_has_no_bypass(two_tenants: None) -> None:
    """Guard the guarantee itself: the runtime role must not be able to bypass RLS."""

    async def _t() -> object:
        async with _app() as f, f() as s, s.begin():
            return (
                await s.execute(
                    text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).scalar_one()

    assert _run(_t()) is False, "Runtime role has BYPASSRLS — every policy is cosmetic"


# ── §4: event-sourced projection + replay, under RLS ──────────────────────────


def test_event_sourced_projection_rebuild_and_isolation(two_tenants: None) -> None:
    """Append the incident lifecycle as events, fold to a projection, rebuild from the
    log (must match), and confirm tenant B cannot see A's event stream."""
    lifecycle: list[tuple[str, dict[str, object], str]] = [
        ("incident.opened", {}, "correlator"),
        ("triage.started", {}, "commander"),
        (
            "hypothesis.formed",
            {"root_cause": "deploy abc123 leaked DB connections", "confidence_band": "HIGH"},
            "commander",
        ),
        ("incident.diagnosed", {}, "commander"),
        ("incident.resolved", {}, "commander"),
        ("incident.closed", {}, "system"),
    ]

    async def _t() -> tuple[Sequence[object], IncidentProjection, object]:
        async with _app() as f:
            async with tenant_session(f, A) as s:
                for type_, payload, actor in lifecycle:
                    await append_incident_event(
                        s,
                        tenant_id=A,
                        incident_id=INC_ES,
                        type_=type_,
                        payload=payload,
                        actor=actor,
                    )
            async with tenant_session(f, A) as s:
                live = (
                    await s.execute(
                        text(
                            "SELECT state, root_cause, confidence_band, version "
                            "FROM incidents WHERE incident_id = :i"
                        ),
                        {"i": INC_ES},
                    )
                ).one()
            async with tenant_session(f, A) as s:
                proj = await rebuild_incident(s, tenant_id=A, incident_id=INC_ES)
            async with tenant_session(f, B) as s:
                b_sees = (
                    await s.execute(
                        text("SELECT count(*) FROM incident_events WHERE incident_id = :i"),
                        {"i": INC_ES},
                    )
                ).scalar_one()
        return live, proj, b_sees

    live, proj, b_sees = _run(_t())

    # Live projection reflects the full folded lifecycle (state, root_cause, band, version).
    assert live[0] == "CLOSED"
    assert live[3] == 6
    assert live[1] == "deploy abc123 leaked DB connections"
    assert live[2] == "HIGH"
    # Rebuild from the append-only log reproduces it exactly (replay determinism, P8).
    assert (proj.state, proj.version) == ("CLOSED", 6)
    assert proj.root_cause == "deploy abc123 leaked DB connections"
    assert proj.confidence_band == "HIGH"
    # And the stream is tenant-private.
    assert b_sees == 0, "tenant B saw A's incident_events — RLS BREACH"


# ── doc 05 §5: the tamper-evident audit hash-chain, over Postgres ─────────────


def test_audit_chain_appends_and_verifies(two_tenants: None) -> None:
    async def _t() -> VerifyResult:
        async with _app() as f:
            async with tenant_session(f, A) as s:
                await append_audit(
                    s, tenant_id=A, actor="human:u1", action="grant.created",
                    target="deploy.rollback", detail={"max_tier": "T2"}, at=_AUDIT_T,
                )
                await append_audit(
                    s, tenant_id=A, actor="system", action="action.executed",
                    target="svc_api", detail={"action_type": "deploy.rollback"}, at=_AUDIT_T,
                )
            async with tenant_session(f, A) as s:
                return await verify_audit(s, A)

    result = _run(_t())
    assert result.ok
    assert result.head is not None


def test_audit_tamper_is_detected(two_tenants: None) -> None:
    """A privileged operator edits a historical row; verify() recomputes and catches the break."""
    from sentinel.data import bootstrap

    async def _t() -> VerifyResult:
        async with _app() as f, tenant_session(f, A) as s:
            await append_audit(s, tenant_id=A, actor="system", action="x", at=_AUDIT_T)
            await append_audit(s, tenant_id=A, actor="system", action="y", at=_AUDIT_T)
        conn = await bootstrap._connect(bootstrap._DB_NAME)  # superuser: bypasses RLS + grants
        try:
            await conn.execute(
                "UPDATE audit_log SET detail = '{\"evil\": true}'::jsonb "
                "WHERE tenant_id = $1 AND seq = 1",
                A,
            )
        finally:
            await conn.close()
        async with _app() as f, tenant_session(f, A) as s:
            return await verify_audit(s, A)

    result = _run(_t())
    assert not result.ok
    assert result.broken_at == 1


def test_app_role_cannot_rewrite_audit_log(two_tenants: None) -> None:
    """Append-only at the grant layer: the app role has no UPDATE on audit_log (doc 02 §3.3)."""

    async def _t() -> None:
        async with _app() as f, tenant_session(f, A) as s:
            await append_audit(s, tenant_id=A, actor="system", action="x", at=_AUDIT_T)
        async with _app() as f, tenant_session(f, A) as s:
            await s.execute(
                text("UPDATE audit_log SET actor = 'evil' WHERE tenant_id = :t"), {"t": A}
            )

    with pytest.raises(ProgrammingError):  # permission denied — no UPDATE grant
        _run(_t())


# ── doc 03 §3: durable inbox/outbox — exactly-once into the bus ───────────────


def test_inbox_outbox_is_exactly_once(two_tenants: None) -> None:
    """Two deliveries of one webhook → the inbox dedups → the bus sees the event once;
    tenant B's outbox stays RLS-private."""
    events: list[dict[str, object]] = [{"event_id": "evt_1", "title": "deploy.error abc123"}]

    async def _t() -> tuple[bool, bool, list[dict[str, object]], list[dict[str, object]]]:
        async with _app() as f:
            async with tenant_session(f, A) as s:
                first = await accept_webhook(
                    s,
                    tenant_id=A,
                    integration_id=_INT,
                    idempotency_key="vercel:dep_1",
                    events=events,
                )
            async with tenant_session(f, A) as s:
                dup = await accept_webhook(
                    s,
                    tenant_id=A,
                    integration_id=_INT,
                    idempotency_key="vercel:dep_1",
                    events=events,
                )
            async with tenant_session(f, A) as s:
                a_drained = await outbox_drain(s, tenant_id=A)
            async with tenant_session(f, B) as s:
                b_drained = await outbox_drain(s, tenant_id=B)
        return first, dup, a_drained, b_drained

    first, dup, a_drained, b_drained = _run(_t())
    assert first is True
    assert dup is False  # redelivery deduped at the inbox
    assert len(a_drained) == 1  # exactly once into the bus, despite two deliveries
    assert a_drained[0]["event_id"] == "evt_1"
    assert b_drained == []  # tenant B's outbox is RLS-private

"""Durable persistence layer (doc 02): Postgres schema, Row-Level Security, the
event-sourced incident repository, the tamper-evident audit hash-chain, and the
inbox/outbox idempotency pair.

Everything here is **opt-in and gated on a reachable Postgres** — the rest of Sentinel
stays infra-free and offline. The headline this module exists to prove is doc 02 §3 /
ADR-001: *even a buggy or adversarial query cannot cross tenants*, because the database
itself appends the tenant predicate to every statement (RLS), not the application.
"""

from __future__ import annotations

from sentinel.data.audit import append_audit, verify_audit
from sentinel.data.inbox_outbox import (
    accept_webhook,
    inbox_insert,
    outbox_drain,
    outbox_enqueue,
)
from sentinel.data.repository import (
    append_incident_event,
    insert_incident,
    rebuild_incident,
)
from sentinel.data.session import (
    APP_DSN,
    make_engine,
    session_factory,
    tenant_session,
)

__all__ = [
    "APP_DSN",
    "accept_webhook",
    "append_audit",
    "append_incident_event",
    "inbox_insert",
    "insert_incident",
    "make_engine",
    "outbox_drain",
    "outbox_enqueue",
    "rebuild_incident",
    "session_factory",
    "tenant_session",
    "verify_audit",
]

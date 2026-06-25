"""Connector-layer tests (doc 03 §1.1, §3, §6).

Mapping fidelity is a per-connector test surface (ADR-009 consequence): the
EXACT Sentry §6.1 and Vercel §6.2 webhook payloads are fed through ``normalize``
and the resulting :class:`CanonicalEvent` fields are asserted field-by-field.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from sentinel.connectors import (
    Connector,
    ConnectorBase,
    ConnectorCapabilities,
    HealthStatus,
    IntegrationContext,
    RawSignal,
    SentryConnector,
    VercelConnector,
    WriteCapability,
    idempotency_key,
)
from sentinel.contracts import ActionRequest, CanonicalEvent
from sentinel.enums import (
    ActionStatus,
    ActionType,
    AutonomyTier,
    EventKind,
    IntegrationMethod,
    Severity,
)

# ── fixtures: the EXACT payloads from doc 03 §6 ──────────────────────────────

_RECEIVED_AT = datetime(2026, 6, 13, 12, 5, tzinfo=UTC)

SENTRY_PAYLOAD: dict[str, object] = {
    "action": "triggered",
    "installation": {"uuid": "a8e4f1c2-..."},
    "data": {
        "event": {
            "event_id": "9f2c3a7b1e7d4f0a9c2b5d6e8f1a2b3c",
            "issue_id": "4827193",
            "level": "fatal",
            "title": "TypeError: Cannot read properties of undefined (reading 'id')",
            "culprit": "checkout.charge(payments/stripe.ts)",
            "project": "1234567",
            "platform": "javascript",
            "environment": "production",
            "release": "web@2026.6.12-a1b2c3d",
            "timestamp": 1749816000.123,
            "web_url": "https://acme.sentry.io/issues/4827193/events/9f2c3a7b.../",
            "tags": [["browser", "Chrome 137"], ["url", "https://acme.app/checkout"]],
            "exception": {
                "values": [
                    {
                        "type": "TypeError",
                        "value": "Cannot read ...",
                        "stacktrace": {"frames": ["..."]},
                    }
                ]
            },
        }
    },
}

VERCEL_PAYLOAD: dict[str, object] = {
    "type": "deployment.error",
    "id": "wh_d9f8e7c6b5a4",
    "createdAt": 1749816300000,
    "region": "iad1",
    "payload": {
        "deployment": {
            "id": "dpl_8Kd2...",
            "url": "acme-web-7x9.vercel.app",
            "name": "acme-web",
            "meta": {"githubCommitSha": "a1b2c3d", "githubCommitRef": "main"},
        },
        "project": {"id": "prj_4Qm1..."},
        "target": "production",
        "plan": "pro",
        "regions": ["iad1", "sfo1"],
        "links": {
            "deployment": "https://vercel.com/acme/acme-web/dpl_8Kd2...",
            "project": "https://vercel.com/acme/acme-web",
        },
    },
}


def _ctx(provider: str) -> IntegrationContext:
    return IntegrationContext(
        tenant_id="ten_acme",
        integration_id=f"int_{provider}",
        provider=provider,
        vault_ref=f"secret/data/tenants/ten_acme/integrations/{provider}",
    )


def _raw(provider: str, payload: dict[str, object]) -> RawSignal:
    return RawSignal(
        integration_id=f"int_{provider}",
        method=IntegrationMethod.WEBHOOK,
        payload=payload,
        received_at=_RECEIVED_AT,
    )


# ── Sentry §6.1 ──────────────────────────────────────────────────────────────


def test_sentry_normalize_maps_every_field() -> None:
    conn = SentryConnector()
    ctx = _ctx("sentry")
    raw = _raw("sentry", SENTRY_PAYLOAD)
    events = asyncio.run(conn.normalize(raw, ctx))

    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, CanonicalEvent)
    assert ev.source == "sentry"
    assert ev.tenant_id == "ten_acme"
    assert ev.integration_id == "int_sentry"
    # exception present -> error (issue alert with no exception -> alert)
    assert ev.kind is EventKind.ERROR
    # level fatal -> critical (lossy map per §6.1)
    assert ev.severity is Severity.CRITICAL
    assert ev.title == "TypeError: Cannot read properties of undefined (reading 'id')"
    # adapter emits empty body; the edge fills it (ADR-004 / doc 05)
    assert ev.body_redacted == ""
    # fingerprint = sha256("sentry:issue:" + issue_id) — trust Sentry's grouping
    import hashlib

    assert ev.fingerprint == hashlib.sha256(b"sentry:issue:4827193").hexdigest()
    # release is preserved (drives the Change/Deploy specialist)
    assert ev.labels["release"] == "web@2026.6.12-a1b2c3d"
    assert ev.labels["environment"] == "production"
    assert ev.labels["platform"] == "javascript"
    assert ev.labels["sentry.event_id"] == "9f2c3a7b1e7d4f0a9c2b5d6e8f1a2b3c"
    # epoch-seconds float -> tz-aware UTC datetime
    assert ev.occurred_at == datetime.fromtimestamp(1749816000.123, tz=UTC)
    assert ev.occurred_at.tzinfo is not None
    assert ev.received_at == _RECEIVED_AT
    # generated evt_ id
    assert ev.event_id.startswith("evt_")


def test_sentry_no_exception_is_alert() -> None:
    payload: dict[str, object] = {
        "action": "triggered",
        "installation": {"uuid": "u"},
        "data": {
            "event": {
                "event_id": "e1",
                "issue_id": "777",
                "level": "warning",
                "title": "Issue alert fired",
                "project": "p",
                "environment": "production",
                "timestamp": 1749816000.0,
            }
        },
    }
    conn = SentryConnector()
    events = asyncio.run(conn.normalize(_raw("sentry", payload), _ctx("sentry")))
    assert events[0].kind is EventKind.ALERT
    assert events[0].severity is Severity.WARNING


def test_sentry_level_severity_map() -> None:
    conn = SentryConnector()
    cases = {
        "fatal": Severity.CRITICAL,
        "error": Severity.ERROR,
        "warning": Severity.WARNING,
        "info": Severity.INFO,
        "debug": Severity.DEBUG,
    }
    for level, expected in cases.items():
        payload: dict[str, object] = {
            "data": {
                "event": {
                    "event_id": f"e-{level}",
                    "issue_id": "1",
                    "level": level,
                    "title": "t",
                    "timestamp": 1749816000.0,
                }
            }
        }
        ev = asyncio.run(conn.normalize(_raw("sentry", payload), _ctx("sentry")))[0]
        assert ev.severity is expected


def test_sentry_is_read_only() -> None:
    conn = SentryConnector()
    assert conn.capabilities.provider == "sentry"
    assert conn.capabilities.writes == ()
    assert conn.capabilities.supports_write(ActionType.DEPLOY_ROLLBACK) is None
    assert EventKind.ERROR in conn.capabilities.reads


# ── Vercel §6.2 ──────────────────────────────────────────────────────────────


def test_vercel_normalize_maps_every_field() -> None:
    conn = VercelConnector()
    ctx = _ctx("vercel")
    raw = _raw("vercel", VERCEL_PAYLOAD)
    events = asyncio.run(conn.normalize(raw, ctx))

    assert len(events) == 1
    ev = events[0]
    assert ev.source == "vercel"
    assert ev.kind is EventKind.DEPLOY  # constant
    # deployment.error -> error
    assert ev.severity is Severity.ERROR
    assert ev.body_redacted == ""
    # fingerprint = sha256("vercel:" + deployment.id + ":" + type)
    import hashlib

    assert (
        ev.fingerprint
        == hashlib.sha256(b"vercel:dpl_8Kd2...:deployment.error").hexdigest()
    )
    # THE keystone cross-provider join key
    assert ev.labels["commit_sha"] == "a1b2c3d"
    assert ev.labels["commit_ref"] == "main"
    assert ev.labels["target"] == "production"
    assert ev.labels["region"] == "iad1"
    # epoch-millis -> tz-aware UTC
    assert ev.occurred_at == datetime.fromtimestamp(1749816300000 / 1000, tz=UTC)
    assert "acme-web" in ev.title
    assert ev.event_id.startswith("evt_")


def test_vercel_severity_from_type() -> None:
    conn = VercelConnector()
    cases = {
        "deployment.error": Severity.ERROR,
        "deployment.succeeded": Severity.INFO,
        "deployment.ready": Severity.INFO,
        "deployment.canceled": Severity.WARNING,
        "deployment.created": Severity.INFO,
    }
    for typ, expected in cases.items():
        payload = {
            "type": typ,
            "id": f"wh_{typ}",
            "createdAt": 1749816300000,
            "payload": {"deployment": {"id": "dpl_1", "name": "n", "meta": {}}},
        }
        ev = asyncio.run(conn.normalize(_raw("vercel", payload), _ctx("vercel")))[0]
        assert ev.severity is expected


def test_vercel_supports_write_rollback() -> None:
    conn = VercelConnector()
    cap = conn.capabilities.supports_write(ActionType.DEPLOY_ROLLBACK)
    assert cap is not None
    assert cap.action_type is ActionType.DEPLOY_ROLLBACK
    assert conn.capabilities.supports_write(ActionType.DEPLOY_REDEPLOY) is not None
    # an action it cannot perform
    assert conn.capabilities.supports_write(ActionType.DB_FAILOVER) is None


# ── idempotency §3 ───────────────────────────────────────────────────────────


def test_idempotency_native_vercel_id() -> None:
    raw = _raw("vercel", VERCEL_PAYLOAD)
    assert idempotency_key(raw, "vercel") == "vercel:wh_d9f8e7c6b5a4"


def test_idempotency_native_github_delivery_header() -> None:
    raw = RawSignal(
        integration_id="int_gh",
        method=IntegrationMethod.WEBHOOK,
        payload={"action": "completed"},
        headers={"x-github-delivery": "12345-abcde"},
        received_at=_RECEIVED_AT,
    )
    assert idempotency_key(raw, "github") == "github:12345-abcde"


def test_idempotency_native_grafana_groupkey() -> None:
    raw = _raw("grafana", {"groupKey": "{}/{}:{alertname=X}", "alerts": []})
    assert idempotency_key(raw, "grafana") == "grafana:{}/{}:{alertname=X}"


def test_idempotency_falls_back_to_content_hash() -> None:
    # Sentry has no native key registered -> content hash, prefixed provider:h:
    raw = _raw("sentry", SENTRY_PAYLOAD)
    key = idempotency_key(raw, "sentry")
    assert key.startswith("sentry:h:")
    assert len(key.split(":h:")[1]) == 32  # sha256 hexdigest truncated to 32


def test_idempotency_hash_is_stable_and_ignores_received_at() -> None:
    raw1 = RawSignal(
        integration_id="int_sentry",
        method=IntegrationMethod.WEBHOOK,
        payload=SENTRY_PAYLOAD,
        received_at=datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
    )
    raw2 = RawSignal(
        integration_id="int_sentry",
        method=IntegrationMethod.WEBHOOK,
        payload=SENTRY_PAYLOAD,
        received_at=datetime(2026, 6, 13, 23, 59, tzinfo=UTC),  # later retry
    )
    # a legitimate retry must hash identically or it slips through as "new"
    assert idempotency_key(raw1, "sentry") == idempotency_key(raw2, "sentry")


def test_idempotency_native_missing_value_falls_back() -> None:
    # vercel registered but payload lacks "id" -> fall back to content hash
    raw = _raw("vercel", {"type": "deployment.error", "payload": {}})
    key = idempotency_key(raw, "vercel")
    assert key.startswith("vercel:h:")


# ── Protocol conformance & ConnectorBase.execute guard ───────────────────────


def test_connectors_satisfy_protocol() -> None:
    assert isinstance(SentryConnector(), Connector)
    assert isinstance(VercelConnector(), Connector)
    assert isinstance(SentryConnector(), ConnectorBase)


def test_healthcheck_returns_status() -> None:
    status = asyncio.run(SentryConnector().healthcheck(_ctx("sentry")))
    assert isinstance(status, HealthStatus)
    assert status.healthy is True


def _action(action_type: ActionType) -> ActionRequest:
    return ActionRequest(
        action_id="act_1",
        tenant_id="ten_acme",
        incident_id="inc_1",
        action_type=action_type,
        target={"deployment_id": "dpl_8Kd2..."},
        reversible=True,
        proposed_tier=AutonomyTier.T2,
        required_tier=AutonomyTier.T2,
    )


def test_execute_raises_when_capability_missing() -> None:
    # Sentry is read-only: execute must raise (no-op guard in ConnectorBase)
    conn = SentryConnector()
    with pytest.raises(Exception):  # noqa: B017 -- CapabilityError subclass
        asyncio.run(conn.execute(_action(ActionType.DEPLOY_ROLLBACK), _ctx("sentry")))


def test_execute_dispatches_when_capability_present() -> None:
    conn = VercelConnector()
    result = asyncio.run(
        conn.execute(_action(ActionType.DEPLOY_ROLLBACK), _ctx("vercel"))
    )
    assert result.action_id == "act_1"
    assert result.status is ActionStatus.SUCCEEDED


def test_write_capability_defaults() -> None:
    cap = WriteCapability(action_type=ActionType.PROCESS_RESTART, reversible=True)
    assert cap.requires_agent is False
    assert cap.min_tier_hint == "T2"
    assert cap.idempotent is True


def test_connector_capabilities_methods_frozenset() -> None:
    caps = ConnectorCapabilities(
        provider="x",
        reads=frozenset({EventKind.ERROR}),
        methods=frozenset({IntegrationMethod.WEBHOOK}),
    )
    assert caps.writes == ()
    assert caps.oauth_scopes == ()

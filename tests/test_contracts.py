from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sentinel.contracts import ActionRequest, AutonomyGrant, CanonicalEvent
from sentinel.enums import ActionType, AutonomyTier, EventKind, Severity


def _now() -> datetime:
    return datetime(2026, 6, 13, 12, 0, tzinfo=UTC)


def _event() -> CanonicalEvent:
    return CanonicalEvent(
        event_id="evt_x",
        tenant_id="ten_x",
        integration_id="int_x",
        source="sentry",
        kind=EventKind.ERROR,
        severity=Severity.CRITICAL,
        title="boom",
        fingerprint="fp",
        occurred_at=_now(),
        received_at=_now(),
    )


def test_canonical_event_constructs_with_defaults() -> None:
    ev = _event()
    assert ev.body_redacted == ""  # adapter emits empty; the edge fills it (ADR-004)
    assert ev.service is None
    assert ev.labels == {}
    assert ev.kind is EventKind.ERROR


def test_canonical_event_is_frozen() -> None:
    ev = _event()
    with pytest.raises(ValidationError):
        ev.__setattr__("title", "mutated")


def test_canonical_event_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CanonicalEvent.model_validate(
            {
                "event_id": "evt_x",
                "tenant_id": "ten_x",
                "integration_id": "int_x",
                "source": "sentry",
                "kind": "error",
                "severity": "error",
                "title": "t",
                "fingerprint": "fp",
                "occurred_at": _now(),
                "received_at": _now(),
                "surprise": "nope",
            }
        )


def test_autonomy_grant_active_flag() -> None:
    grant = AutonomyGrant(
        grant_id="grant_x",
        tenant_id="ten_x",
        action_type=ActionType.DEPLOY_ROLLBACK,
        max_tier=AutonomyTier.T2,
        granted_by="human:1",
        granted_at=_now(),
    )
    assert grant.active
    assert not grant.model_copy(update={"revoked_at": _now()}).active


def test_autonomy_grant_defaults_to_t0() -> None:
    grant = AutonomyGrant(
        grant_id="grant_x",
        tenant_id="ten_x",
        action_type=ActionType.PROCESS_RESTART,
        granted_by="human:1",
        granted_at=_now(),
    )
    assert grant.max_tier is AutonomyTier.T0


def test_action_request_rejects_bad_status() -> None:
    with pytest.raises(ValidationError):
        ActionRequest.model_validate(
            {
                "action_id": "act_x",
                "tenant_id": "ten_x",
                "incident_id": "inc_x",
                "action_type": "process.restart",
                "target": {"service": "svc_x"},
                "reversible": True,
                "proposed_tier": "T2",
                "required_tier": "T2",
                "status": "bogus",
            }
        )

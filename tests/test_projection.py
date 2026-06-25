"""Tests for the event-sourced incident projection (doc 02 §4, ADR-011).

Drives the pure fold over the append-only ``incident_events`` log and the
``project`` replay function, asserting the lifecycle of doc 00 §5.3.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sentinel.enums import ConfidenceBand, IncidentState
from sentinel.incidents import (
    IncidentEvent,
    IncidentEventType,
    IncidentProjection,
    fold,
    project,
)


def _at(offset: int = 0) -> datetime:
    return datetime(2026, 6, 14, 12, 0, offset, tzinfo=UTC)


def _ev(seq: int, type_: IncidentEventType, **payload: object) -> IncidentEvent:
    return IncidentEvent(
        incident_id="inc_x",
        tenant_id="ten_x",
        seq=seq,
        type=type_,
        payload=dict(payload),
        actor="system",
        occurred_at=_at(seq),
    )


def _lifecycle_stream() -> list[IncidentEvent]:
    """A full happy-path lifecycle: opened → … → closed (doc 00 §5.3)."""
    return [
        _ev(1, IncidentEventType.INCIDENT_OPENED, primary_service="svc_api"),
        _ev(2, IncidentEventType.TRIAGE_STARTED),
        _ev(3, IncidentEventType.SPECIALIST_FINDING, role="log"),
        _ev(
            4,
            IncidentEventType.HYPOTHESIS_FORMED,
            root_cause="bad deploy",
            confidence_band="HIGH",
        ),
        _ev(5, IncidentEventType.INCIDENT_DIAGNOSED),
        _ev(6, IncidentEventType.APPROVAL_REQUESTED),
        _ev(7, IncidentEventType.APPROVAL_GRANTED),
        _ev(8, IncidentEventType.REMEDIATION_STARTED),
        _ev(9, IncidentEventType.MONITORING_STARTED),
        _ev(10, IncidentEventType.INCIDENT_RESOLVED),
        _ev(11, IncidentEventType.INCIDENT_CLOSED),
    ]


def test_event_type_vocabulary_is_verbatim() -> None:
    """The §4.1 vocabulary, stored as its wire string."""
    assert {t.value for t in IncidentEventType} == {
        "incident.opened",
        "triage.started",
        "specialist.finding",
        "hypothesis.formed",
        "incident.diagnosed",
        "approval.requested",
        "approval.granted",
        "approval.denied",
        "remediation.started",
        "monitoring.started",
        "incident.escalated",
        "incident.resolved",
        "incident.closed",
        "checkpoint.saved",
    }
    # StrEnum members are usable as their wire string at runtime.
    assert IncidentEventType.INCIDENT_OPENED.value == "incident.opened"
    assert str(IncidentEventType.CHECKPOINT_SAVED) == "checkpoint.saved"


def test_incident_event_is_frozen_and_forbids_extra() -> None:
    ev = _ev(1, IncidentEventType.INCIDENT_OPENED)
    with pytest.raises(Exception):  # noqa: B017 — pydantic raises ValidationError
        ev.seq = 2  # type: ignore[misc]
    with pytest.raises(Exception):  # noqa: B017
        IncidentEvent.model_validate(
            {
                "incident_id": "inc_x",
                "tenant_id": "ten_x",
                "seq": 1,
                "type": "incident.opened",
                "actor": "system",
                "occurred_at": _at(),
                "surprise": "nope",
            }
        )


def test_projection_constructs_with_field_defaults() -> None:
    proj = IncidentProjection(
        incident_id="inc_x",
        tenant_id="ten_x",
        state=IncidentState.OPEN,
        version=1,
    )
    assert proj.confidence_band is None
    assert proj.root_cause is None
    assert proj.primary_service is None
    with pytest.raises(Exception):  # noqa: B017 — extra="forbid"
        IncidentProjection.model_validate(
            {
                "incident_id": "inc_x",
                "tenant_id": "ten_x",
                "state": "OPEN",
                "version": 1,
                "surprise": "nope",
            }
        )


def test_incident_event_payload_defaults_to_empty() -> None:
    ev = IncidentEvent(
        incident_id="inc_x",
        tenant_id="ten_x",
        seq=1,
        type=IncidentEventType.INCIDENT_OPENED,
        actor="system",
        occurred_at=_at(),
    )
    assert ev.payload == {}


def test_full_lifecycle_folds_to_closed() -> None:
    proj = project(_lifecycle_stream())
    assert proj.state is IncidentState.CLOSED
    assert proj.version == 11
    assert proj.root_cause == "bad deploy"
    assert proj.confidence_band is ConfidenceBand.HIGH
    assert proj.primary_service == "svc_api"
    assert proj.incident_id == "inc_x"
    assert proj.tenant_id == "ten_x"


def test_opened_creates_open_with_primary_service() -> None:
    proj = fold(None, _ev(1, IncidentEventType.INCIDENT_OPENED, primary_service="svc_db"))
    assert proj.state is IncidentState.OPEN
    assert proj.primary_service == "svc_db"
    assert proj.version == 1
    assert proj.root_cause is None
    assert proj.confidence_band is None


def test_each_transition_sets_expected_state() -> None:
    cases: list[tuple[IncidentEventType, IncidentState]] = [
        (IncidentEventType.TRIAGE_STARTED, IncidentState.TRIAGING),
        (IncidentEventType.INCIDENT_DIAGNOSED, IncidentState.DIAGNOSED),
        (IncidentEventType.APPROVAL_REQUESTED, IncidentState.AWAITING_APPROVAL),
        (IncidentEventType.APPROVAL_GRANTED, IncidentState.REMEDIATING),
        (IncidentEventType.REMEDIATION_STARTED, IncidentState.REMEDIATING),
        (IncidentEventType.MONITORING_STARTED, IncidentState.MONITORING),
        (IncidentEventType.INCIDENT_RESOLVED, IncidentState.RESOLVED),
        (IncidentEventType.INCIDENT_CLOSED, IncidentState.CLOSED),
    ]
    base = fold(None, _ev(1, IncidentEventType.INCIDENT_OPENED, primary_service="svc_api"))
    for type_, expected in cases:
        proj = fold(base, _ev(2, type_))
        assert proj.state is expected, type_
        assert proj.version == 2


def test_approval_denied_routes_to_escalated() -> None:
    base = fold(None, _ev(1, IncidentEventType.INCIDENT_OPENED))
    proj = fold(base, _ev(2, IncidentEventType.APPROVAL_DENIED))
    assert proj.state is IncidentState.ESCALATED


def test_incident_escalated_routes_to_escalated() -> None:
    base = fold(None, _ev(1, IncidentEventType.INCIDENT_OPENED))
    proj = fold(base, _ev(2, IncidentEventType.INCIDENT_ESCALATED))
    assert proj.state is IncidentState.ESCALATED


def test_hypothesis_sets_root_cause_and_band_without_state_change() -> None:
    base = fold(None, _ev(1, IncidentEventType.INCIDENT_OPENED))
    triaging = fold(base, _ev(2, IncidentEventType.TRIAGE_STARTED))
    proj = fold(
        triaging,
        _ev(3, IncidentEventType.HYPOTHESIS_FORMED, root_cause="oom", confidence_band="MEDIUM"),
    )
    assert proj.root_cause == "oom"
    assert proj.confidence_band is ConfidenceBand.MEDIUM
    assert proj.state is IncidentState.TRIAGING  # hypothesis does not move state
    assert proj.version == 3


def test_evidence_and_checkpoint_events_do_not_change_state() -> None:
    base = fold(None, _ev(1, IncidentEventType.INCIDENT_OPENED))
    triaging = fold(base, _ev(2, IncidentEventType.TRIAGE_STARTED))
    after_finding = fold(triaging, _ev(3, IncidentEventType.SPECIALIST_FINDING, role="metric"))
    assert after_finding.state is IncidentState.TRIAGING
    assert after_finding.version == 3
    after_ckpt = fold(after_finding, _ev(4, IncidentEventType.CHECKPOINT_SAVED, ref="ck_1"))
    assert after_ckpt.state is IncidentState.TRIAGING
    assert after_ckpt.version == 4


def test_version_always_tracks_seq() -> None:
    proj = fold(None, _ev(1, IncidentEventType.INCIDENT_OPENED))
    proj = fold(proj, _ev(2, IncidentEventType.TRIAGE_STARTED))
    proj = fold(proj, _ev(7, IncidentEventType.INCIDENT_DIAGNOSED))  # fold itself is gap-blind
    assert proj.version == 7


def test_fold_is_pure_input_unchanged() -> None:
    base = fold(None, _ev(1, IncidentEventType.INCIDENT_OPENED, primary_service="svc_api"))
    snapshot = base.model_dump()
    new = fold(base, _ev(2, IncidentEventType.TRIAGE_STARTED))
    assert base.model_dump() == snapshot  # original untouched
    assert new is not base
    assert new.state is IncidentState.TRIAGING
    assert base.state is IncidentState.OPEN


def test_replay_is_deterministic() -> None:
    stream = _lifecycle_stream()
    a = project(stream)
    b = project(list(stream))
    assert a == b
    assert a.model_dump() == b.model_dump()


def test_project_requires_first_event_to_be_opened() -> None:
    with pytest.raises(ValueError, match="incident.opened"):
        project([_ev(1, IncidentEventType.TRIAGE_STARTED)])


def test_project_rejects_seq_not_starting_at_one() -> None:
    with pytest.raises(ValueError, match="seq"):
        project([_ev(2, IncidentEventType.INCIDENT_OPENED)])


def test_project_rejects_gap_in_seq() -> None:
    with pytest.raises(ValueError, match="seq"):
        project(
            [
                _ev(1, IncidentEventType.INCIDENT_OPENED),
                _ev(3, IncidentEventType.TRIAGE_STARTED),  # gap: missing 2
            ]
        )


def test_project_rejects_duplicate_seq() -> None:
    with pytest.raises(ValueError, match="seq"):
        project(
            [
                _ev(1, IncidentEventType.INCIDENT_OPENED),
                _ev(2, IncidentEventType.TRIAGE_STARTED),
                _ev(2, IncidentEventType.INCIDENT_DIAGNOSED),  # duplicate seq
            ]
        )


def test_project_rejects_out_of_order_seq() -> None:
    with pytest.raises(ValueError, match="seq"):
        project(
            [
                _ev(1, IncidentEventType.INCIDENT_OPENED),
                _ev(3, IncidentEventType.INCIDENT_DIAGNOSED),
                _ev(2, IncidentEventType.TRIAGE_STARTED),  # arrives out of order
            ]
        )


def test_project_rejects_empty_stream() -> None:
    with pytest.raises(ValueError, match="empty"):
        project([])


def test_project_accepts_iterator() -> None:
    proj = project(iter(_lifecycle_stream()))
    assert proj.state is IncidentState.CLOSED

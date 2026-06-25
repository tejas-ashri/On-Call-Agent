"""Tests for the Correlator (doc 01 §5 "correlator", doc 04 §11).

Pins the storm-collapse behaviour: tenant isolation (P1), the time window,
the cross-provider ``labels["commit_sha"]`` join key (doc 03 §6.2), transitive
grouping, modal ``primary_service``, and deterministic output.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from sentinel.contracts import CanonicalEvent
from sentinel.correlator import IncidentDraft, correlate
from sentinel.enums import EventKind, Severity

_BASE = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def _ev(
    *,
    event_id: str,
    tenant_id: str = "ten_a",
    fingerprint: str = "fp",
    service: str | None = None,
    commit_sha: str | None = None,
    offset_s: float = 0.0,
) -> CanonicalEvent:
    labels: dict[str, str] = {}
    if commit_sha is not None:
        labels["commit_sha"] = commit_sha
    return CanonicalEvent(
        event_id=event_id,
        tenant_id=tenant_id,
        integration_id="int_x",
        source="sentry",
        kind=EventKind.ERROR,
        service=service,
        severity=Severity.ERROR,
        title="boom",
        fingerprint=fingerprint,
        labels=labels,
        occurred_at=_BASE + timedelta(seconds=offset_s),
        received_at=_BASE + timedelta(seconds=offset_s),
    )


def _fixed_ids() -> Callable[[], str]:
    """A deterministic incident-id factory: inc_0, inc_1, ... in call order."""
    seq = iter(f"inc_{i}" for i in range(1000))
    return lambda: next(seq)


def test_storm_sharing_commit_sha_collapses_to_one_incident() -> None:
    # 5 events, distinct fingerprints + services, joined only by commit_sha,
    # all inside the window → exactly one incident with all 5 event_ids.
    events = [
        _ev(event_id=f"evt_{i}", fingerprint=f"fp_{i}", service=f"svc_{i}",
            commit_sha="a1b2c3d", offset_s=float(i))
        for i in range(5)
    ]
    drafts = correlate(events, window_seconds=60.0, new_incident_id=_fixed_ids())
    assert len(drafts) == 1
    draft = drafts[0]
    assert isinstance(draft, IncidentDraft)
    assert set(draft.event_ids) == {f"evt_{i}" for i in range(5)}
    assert len(draft.event_ids) == 5
    assert draft.tenant_id == "ten_a"
    assert draft.opened_at == _BASE  # earliest occurred_at


def test_two_tenants_never_merge_even_with_identical_fingerprints() -> None:
    events = [
        _ev(event_id="evt_a", tenant_id="ten_a", fingerprint="same", service="svc_x"),
        _ev(event_id="evt_b", tenant_id="ten_b", fingerprint="same", service="svc_x"),
    ]
    drafts = correlate(events, window_seconds=60.0, new_incident_id=_fixed_ids())
    assert len(drafts) == 2
    tenants = {d.tenant_id for d in drafts}
    assert tenants == {"ten_a", "ten_b"}
    for d in drafts:
        assert len(d.event_ids) == 1


def test_event_outside_window_opens_separate_incident() -> None:
    # Same fingerprint, but the second event is beyond window_seconds.
    events = [
        _ev(event_id="evt_1", fingerprint="fp", offset_s=0.0),
        _ev(event_id="evt_2", fingerprint="fp", offset_s=120.0),
    ]
    drafts = correlate(events, window_seconds=60.0, new_incident_id=_fixed_ids())
    assert len(drafts) == 2
    assert [d.event_ids for d in drafts] == [["evt_1"], ["evt_2"]]


def test_window_boundary_is_inclusive() -> None:
    # Exactly window_seconds apart → still joined (<= boundary).
    events = [
        _ev(event_id="evt_1", fingerprint="fp", offset_s=0.0),
        _ev(event_id="evt_2", fingerprint="fp", offset_s=60.0),
    ]
    drafts = correlate(events, window_seconds=60.0, new_incident_id=_fixed_ids())
    assert len(drafts) == 1
    assert set(drafts[0].event_ids) == {"evt_1", "evt_2"}


def test_unrelated_events_stay_separate() -> None:
    # Inside the window but sharing no key: different fingerprint, service, commit.
    events = [
        _ev(event_id="evt_1", fingerprint="fp_1", service="svc_1", commit_sha="x"),
        _ev(event_id="evt_2", fingerprint="fp_2", service="svc_2", commit_sha="y"),
    ]
    drafts = correlate(events, window_seconds=60.0, new_incident_id=_fixed_ids())
    assert len(drafts) == 2
    assert {tuple(d.event_ids) for d in drafts} == {("evt_1",), ("evt_2",)}


def test_null_service_does_not_join() -> None:
    # Two events both with service=None must NOT join on "shared null service".
    events = [
        _ev(event_id="evt_1", fingerprint="fp_1", service=None),
        _ev(event_id="evt_2", fingerprint="fp_2", service=None),
    ]
    drafts = correlate(events, window_seconds=60.0, new_incident_id=_fixed_ids())
    assert len(drafts) == 2


def test_grouping_is_transitive_within_window() -> None:
    # A-B share service (within window); B-C share commit_sha (within window).
    # A and C share nothing directly, yet land in one incident via B.
    events = [
        _ev(event_id="evt_a", fingerprint="fp_a", service="svc_shared", offset_s=0.0),
        _ev(event_id="evt_b", fingerprint="fp_b", service="svc_shared",
            commit_sha="c", offset_s=5.0),
        _ev(event_id="evt_c", fingerprint="fp_c", commit_sha="c", offset_s=10.0),
    ]
    drafts = correlate(events, window_seconds=60.0, new_incident_id=_fixed_ids())
    assert len(drafts) == 1
    assert set(drafts[0].event_ids) == {"evt_a", "evt_b", "evt_c"}


def test_primary_service_is_the_modal_non_null_service() -> None:
    # svc_hot appears 3x, svc_cold 1x, one event has no service.
    events = [
        _ev(event_id="evt_1", fingerprint="fp", service="svc_hot", offset_s=0.0),
        _ev(event_id="evt_2", fingerprint="fp", service="svc_hot", offset_s=1.0),
        _ev(event_id="evt_3", fingerprint="fp", service="svc_hot", offset_s=2.0),
        _ev(event_id="evt_4", fingerprint="fp", service="svc_cold", offset_s=3.0),
        _ev(event_id="evt_5", fingerprint="fp", service=None, offset_s=4.0),
    ]
    drafts = correlate(events, window_seconds=60.0, new_incident_id=_fixed_ids())
    assert len(drafts) == 1
    assert drafts[0].primary_service == "svc_hot"


def test_primary_service_is_none_when_no_event_has_a_service() -> None:
    events = [
        _ev(event_id="evt_1", fingerprint="fp", service=None, offset_s=0.0),
        _ev(event_id="evt_2", fingerprint="fp", service=None, offset_s=1.0),
    ]
    drafts = correlate(events, window_seconds=60.0, new_incident_id=_fixed_ids())
    assert len(drafts) == 1
    assert drafts[0].primary_service is None


def test_output_is_ordered_by_opened_at() -> None:
    # Three independent incidents introduced out of chronological order.
    events = [
        _ev(event_id="evt_late", fingerprint="fp_late", offset_s=200.0),
        _ev(event_id="evt_early", fingerprint="fp_early", offset_s=0.0),
        _ev(event_id="evt_mid", fingerprint="fp_mid", offset_s=100.0),
    ]
    drafts = correlate(events, window_seconds=10.0, new_incident_id=_fixed_ids())
    assert [d.opened_at for d in drafts] == sorted(d.opened_at for d in drafts)
    assert [d.event_ids[0] for d in drafts] == ["evt_early", "evt_mid", "evt_late"]


def test_output_is_deterministic_across_input_orderings() -> None:
    base = [
        _ev(event_id="evt_1", fingerprint="fp_x", service="svc_1", commit_sha="k",
            offset_s=0.0),
        _ev(event_id="evt_2", fingerprint="fp_y", service="svc_1", offset_s=2.0),
        _ev(event_id="evt_3", fingerprint="fp_z", commit_sha="k", offset_s=4.0),
        _ev(event_id="evt_4", fingerprint="fp_lonely", service="svc_2", offset_s=500.0),
    ]
    forward = correlate(base, window_seconds=60.0, new_incident_id=_fixed_ids())
    reversed_ = correlate(list(reversed(base)), window_seconds=60.0,
                          new_incident_id=_fixed_ids())

    def signature(drafts: list[IncidentDraft]) -> list[tuple[object, ...]]:
        return [
            (d.tenant_id, tuple(d.event_ids), d.primary_service, d.fingerprints,
             d.opened_at)
            for d in drafts
        ]

    assert signature(forward) == signature(reversed_)
    # Sanity: 4 events → 2 incidents (1+2+3 joined, 4 alone & late).
    assert len(forward) == 2


def test_default_id_factory_produces_incident_prefix() -> None:
    # No new_incident_id passed → must mint a real inc_ id from sentinel.ids.
    drafts = correlate([_ev(event_id="evt_1")], window_seconds=60.0)
    assert len(drafts) == 1
    assert drafts[0].incident_id.startswith("inc_")


def test_empty_input_yields_no_incidents() -> None:
    assert correlate([], window_seconds=60.0, new_incident_id=_fixed_ids()) == []


def test_fingerprints_are_sorted_unique_tuple() -> None:
    events = [
        _ev(event_id="evt_1", fingerprint="fp_b", service="svc", offset_s=0.0),
        _ev(event_id="evt_2", fingerprint="fp_a", service="svc", offset_s=1.0),
        _ev(event_id="evt_3", fingerprint="fp_a", service="svc", offset_s=2.0),
    ]
    drafts = correlate(events, window_seconds=60.0, new_incident_id=_fixed_ids())
    assert len(drafts) == 1
    assert drafts[0].fingerprints == ("fp_a", "fp_b")


def test_incident_draft_is_frozen() -> None:
    draft = correlate([_ev(event_id="evt_1")], window_seconds=60.0,
                      new_incident_id=_fixed_ids())[0]
    with pytest.raises(ValidationError):
        draft.__setattr__("tenant_id", "ten_evil")

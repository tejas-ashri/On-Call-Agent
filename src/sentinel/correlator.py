"""The Correlator: collapse a storm of related events into one incident draft.

Doc 01 §5 ("``correlator`` consumes ``evt.normalized``, collapses storms of
related events into a single ``Incident`` ... and emits the opening
``incident.events`` event") and doc 04 §11. This module owns the *pure*
correlation algorithm — the streaming/Redis-window/projection plumbing named
in doc 01 lives elsewhere; here is the deterministic grouping logic.

Grouping rules (per doc 01 §5, doc 03 §6.2):

* **Tenant isolation (P1).** Events are *never* grouped across ``tenant_id``;
  correlation runs independently inside each tenant.
* **Time window.** Two same-tenant events are *adjacent* only if their
  ``occurred_at`` values are within ``window_seconds`` of each other.
* **Join keys.** Two adjacent events join when they share **any** of: an
  identical ``fingerprint``, the same ``labels["commit_sha"]`` (the
  cross-provider join key of doc 03 §6.2 — stitches a Vercel deploy, a GitHub
  push, and a Sentry ``release`` error spike into one incident; a short SHA
  matches the full SHA it abbreviates), or an
  identical *non-null* ``service``. A null ``service`` never joins.
* **Transitivity.** Grouping is transitive within the window: adjacency edges
  form a graph whose connected components are incidents (so A–B and B–C can
  place A and C together even if A–C is itself outside the window).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from sentinel.contracts import CanonicalEvent
from sentinel.ids import new_id


class IncidentDraft(BaseModel):
    """A correlated cluster of events proposed as one incident (doc 01 §5).

    A value object: immutable once built (``frozen``) and closed to unknown
    fields (``extra="forbid"``). It is the correlator's hand-off to the
    orchestrator (doc 04), not yet a persisted ``incidents`` row.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str
    tenant_id: str
    event_ids: list[str]
    primary_service: str | None
    fingerprints: tuple[str, ...]
    opened_at: datetime


def _default_incident_id() -> str:
    return new_id("incident")


def _order_key(event: CanonicalEvent) -> tuple[datetime, str]:
    """Stable per-event sort key: earliest first, then ``event_id`` to break ties.

    Used everywhere ordering must not depend on input order (determinism).
    """
    return (event.occurred_at, event.event_id)


def _adjacent(a: CanonicalEvent, b: CanonicalEvent, window_seconds: float) -> bool:
    """True iff ``a`` and ``b`` are within the window AND share a join key.

    Caller guarantees ``a.tenant_id == b.tenant_id``.
    """
    if abs((a.occurred_at - b.occurred_at).total_seconds()) > window_seconds:
        return False
    if a.fingerprint == b.fingerprint:
        return True
    if _same_commit(a.labels.get("commit_sha"), b.labels.get("commit_sha")):
        return True
    return a.service is not None and a.service == b.service


#: Shortest abbreviated SHA treated as identifying a commit (git's default).
_MIN_SHA_LEN = 7


def _same_commit(a: str | None, b: str | None) -> bool:
    """True iff two commit SHAs name the same commit.

    Providers disagree on length — Vercel sends the full 40-char SHA, a Sentry
    release often carries the 7-char short form — so a short SHA matches any full
    SHA it prefixes. Below ``_MIN_SHA_LEN`` only an exact match counts.
    """
    if a is None or b is None:
        return False
    a, b = a.lower(), b.lower()
    if a == b:
        return True
    short, full = sorted((a, b), key=len)
    return len(short) >= _MIN_SHA_LEN and full.startswith(short)


def _components(events: list[CanonicalEvent], window_seconds: float) -> list[list[int]]:
    """Connected components of the adjacency graph over ``events`` (by index).

    Union-find over an O(n^2) pairwise scan: storms are bounded, so the simple,
    obviously-correct form is preferred over premature optimisation.
    """
    parent = list(range(len(events)))

    def find(i: int) -> int:
        root = i
        while parent[root] != root:
            root = parent[root]
        while parent[i] != root:  # path compression
            parent[i], i = root, parent[i]
        return root

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    for i in range(len(events)):
        for j in range(i + 1, len(events)):
            if _adjacent(events[i], events[j], window_seconds):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(len(events)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _primary_service(events: list[CanonicalEvent]) -> str | None:
    """The most common non-null ``service``; ties broken deterministically.

    Tie-break: among the modal services, pick the one whose earliest member
    sorts first by ``_order_key`` — independent of input order.
    """
    counts: Counter[str] = Counter(e.service for e in events if e.service is not None)
    if not counts:
        return None
    top = max(counts.values())
    modal = {svc for svc, n in counts.items() if n == top}
    first_seen = {
        svc: min(_order_key(e) for e in events if e.service == svc) for svc in modal
    }
    return min(modal, key=lambda svc: first_seen[svc])


def _draft(events: list[CanonicalEvent], incident_id: str) -> IncidentDraft:
    """Build one ``IncidentDraft`` from a non-empty, single-tenant event group."""
    ordered = sorted(events, key=_order_key)
    return IncidentDraft(
        incident_id=incident_id,
        tenant_id=ordered[0].tenant_id,
        event_ids=[e.event_id for e in ordered],
        primary_service=_primary_service(ordered),
        fingerprints=tuple(sorted({e.fingerprint for e in ordered})),
        opened_at=ordered[0].occurred_at,
    )


def correlate(
    events: Iterable[CanonicalEvent],
    *,
    window_seconds: float,
    new_incident_id: Callable[[], str] = _default_incident_id,
) -> list[IncidentDraft]:
    """Collapse a storm of related events into one ``IncidentDraft`` per cluster.

    Never groups across ``tenant_id`` (P1). Within a tenant, events join the
    same incident when they are within ``window_seconds`` of each other and
    share any of ``fingerprint`` / ``labels["commit_sha"]`` / non-null
    ``service``; grouping is transitive within the window (see module docstring).
    Output is deterministic and ordered by ``opened_at`` (then by the first
    event's ``occurred_at``/``event_id`` to break ties), independent of the
    order events are supplied in.

    ``new_incident_id`` is injectable so tests can pin ids; it defaults to
    ``new_id("incident")``.
    """
    by_tenant: dict[str, list[CanonicalEvent]] = {}
    for event in events:
        by_tenant.setdefault(event.tenant_id, []).append(event)

    # Gather each cluster's ordered events first, then sort clusters by their
    # stable key, and only THEN mint ids — so identical inputs in any order
    # produce identical incident_ids, not just identical groupings.
    clusters: list[list[CanonicalEvent]] = []
    for tenant_events in by_tenant.values():
        for component in _components(tenant_events, window_seconds):
            clusters.append(sorted((tenant_events[i] for i in component), key=_order_key))

    clusters.sort(key=lambda group: _order_key(group[0]))
    return [_draft(group, new_incident_id()) for group in clusters]


class Correlation(BaseModel):
    """New events split into those joining incidents already open, and new incidents."""

    model_config = ConfigDict(frozen=True)

    attached: dict[str, list[CanonicalEvent]]  # incident_id → its newly arrived events
    drafts: list[IncidentDraft]


def correlate_with_open(
    events: Iterable[CanonicalEvent],
    open_incidents: Mapping[str, Sequence[CanonicalEvent]],
    *,
    window_seconds: float,
    new_incident_id: Callable[[], str] = _default_incident_id,
) -> Correlation:
    """Correlate new events against still-open incidents before opening new ones.

    Alerts keep arriving after an incident opens. :func:`correlate` alone would
    turn each late batch into a fresh incident — a second page, and possibly a
    second action. Here the open incidents' events take part in the same
    adjacency graph (same join keys, same window, same tenant isolation), so a
    new event that joins an open incident — directly or transitively through
    other new events — is attached to it. Only clusters touching no open incident
    become new drafts. If one cluster bridges several open incidents, the
    earliest-opened one gets the events.
    """
    owner: dict[str, str] = {}  # event_id → open incident_id
    first_seen: dict[str, tuple[datetime, str]] = {}
    pool: dict[str, list[CanonicalEvent]] = {}
    for incident_id, incident_events in open_incidents.items():
        for e in incident_events:
            owner[e.event_id] = incident_id
            pool.setdefault(e.tenant_id, []).append(e)
        if incident_events:
            first_seen[incident_id] = min(_order_key(e) for e in incident_events)
    for e in events:
        if e.event_id not in owner:  # a redelivered event is already accounted for
            pool.setdefault(e.tenant_id, []).append(e)

    attached: dict[str, list[CanonicalEvent]] = {}
    clusters: list[list[CanonicalEvent]] = []
    for tenant_events in pool.values():
        for component in _components(tenant_events, window_seconds):
            members = [tenant_events[i] for i in component]
            fresh = sorted((e for e in members if e.event_id not in owner), key=_order_key)
            if not fresh:
                continue
            touched = {owner[e.event_id] for e in members if e.event_id in owner}
            if touched:
                target = min(touched, key=lambda inc: first_seen[inc])
                attached.setdefault(target, []).extend(fresh)
            else:
                clusters.append(fresh)

    clusters.sort(key=lambda group: _order_key(group[0]))
    return Correlation(
        attached=attached, drafts=[_draft(group, new_incident_id()) for group in clusters]
    )

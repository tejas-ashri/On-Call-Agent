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
  identical ``fingerprint``, an identical ``labels["commit_sha"]`` (the
  cross-provider join key of doc 03 §6.2 — stitches a Vercel deploy, a GitHub
  push, and a Sentry ``release`` error spike into one incident), or an
  identical *non-null* ``service``. A null ``service`` never joins.
* **Transitivity.** Grouping is transitive within the window: adjacency edges
  form a graph whose connected components are incidents (so A–B and B–C can
  place A and C together even if A–C is itself outside the window).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
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
    a_commit = a.labels.get("commit_sha")
    if a_commit is not None and a_commit == b.labels.get("commit_sha"):
        return True
    return a.service is not None and a.service == b.service


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

"""The event bus seam (doc 01 §3, §4).

In production this is Kafka, partitioned by ``tenant_id`` so one tenant's storm
can never starve another's and ordering is per-tenant (doc 01 §4, ADR-002). The
control plane depends only on the :class:`EventBus` publish seam; the in-memory
implementation here models the tenant-keyed partitioning faithfully so the whole
ingestion path runs offline and deterministically.
"""

from __future__ import annotations

from typing import Protocol

from sentinel.contracts import CanonicalEvent


class EventBus(Protocol):
    """The producer seam the ingestion edge writes to (doc 01 §3)."""

    async def publish(self, event: CanonicalEvent) -> None: ...


class InMemoryEventBus:
    """An offline :class:`EventBus` that partitions events by ``tenant_id``.

    Mirrors the doc-01 Kafka topic keyed on ``tenant_id``: :meth:`publish` routes
    each event to its tenant's partition, and :meth:`tenant_events` is the
    per-tenant consumer view the pipeline drains. Isolation is structural — a
    consumer only ever sees one tenant's partition.
    """

    def __init__(self) -> None:
        self._partitions: dict[str, list[CanonicalEvent]] = {}

    async def publish(self, event: CanonicalEvent) -> None:
        self._partitions.setdefault(event.tenant_id, []).append(event)

    def tenant_events(self, tenant_id: str) -> list[CanonicalEvent]:
        """The ordered events in one tenant's partition (a defensive copy)."""
        return list(self._partitions.get(tenant_id, []))

    def tenants(self) -> list[str]:
        """Every tenant that currently has a partition."""
        return list(self._partitions)

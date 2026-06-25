"""The ingestion edge (doc 01 §3, doc 03 §3, doc 05 §4, ADR-004).

The first thing a provider's telemetry touches. It runs the fixed pipeline

    authenticate → dedup → normalize → **redact** → produce

and the ordering is load-bearing: redaction happens *here*, before anything is
published to Kafka or written to any durable store, so the control plane is
secret-free by construction (C2, doc 05 §4.1). The edge redacts **every
persisting string field** the connector produced — ``title``, each ``labels``
value, and a serialized ``body`` derived from the raw payload — because a secret
in any of them would otherwise survive normalization (the connector's
``normalize`` only sets ``body_redacted=''`` as a placeholder, doc 03 §1.1).

External dependencies are injected seams (:class:`~sentinel.edge.bus.EventBus`,
:class:`DedupStore`, the redactor), so the edge runs offline with in-memory
fakes and unchanged against Kafka/Redis in production.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from typing import Protocol

from sentinel.connectors.base import Connector, IntegrationContext, RawSignal
from sentinel.connectors.idempotency import idempotency_key
from sentinel.contracts import CanonicalEvent
from sentinel.edge.bus import EventBus
from sentinel.redaction import RedactionResult, redact


class WebhookRejected(ValueError):
    """Raised when an inbound webhook fails provider signature verification (doc 03 §3)."""


class DedupStore(Protocol):
    """Records seen idempotency keys (Redis in prod; doc 03 §3)."""

    async def check_and_record(self, key: str) -> bool:
        """Return ``True`` if ``key`` was already seen; otherwise record it and return ``False``."""
        ...


class InMemoryDedupStore:
    """An offline :class:`DedupStore` backed by a set (the Redis SETNX in prod)."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    async def check_and_record(self, key: str) -> bool:
        if key in self._seen:
            return True
        self._seen.add(key)
        return False


class IngestionEdge:
    """Runs authenticate → dedup → normalize → redact → produce (doc 01 §3)."""

    def __init__(
        self,
        *,
        bus: EventBus,
        dedup: DedupStore,
        redactor: Callable[..., RedactionResult] = redact,
    ) -> None:
        self._bus = bus
        self._dedup = dedup
        self._redactor = redactor

    async def ingest(
        self, raw: RawSignal, ctx: IntegrationContext, connector: Connector
    ) -> list[CanonicalEvent]:
        """Process one inbound delivery, returning the events published (``[]`` if a dup)."""
        # 1. authenticate — verify the provider signature before touching the payload
        if not await connector.verify_webhook(raw, ctx):
            raise WebhookRejected(f"{ctx.provider}: webhook signature verification failed")

        # 2. dedup — providers retry; the same delivery must normalize at most once (C1)
        if await self._dedup.check_and_record(idempotency_key(raw, ctx.provider)):
            return []

        # 3. normalize — pure provider-native → CanonicalEvent (body_redacted left empty)
        events = await connector.normalize(raw, ctx)

        # 4. redact + 5. produce — scrub every persisting field, THEN publish (C2)
        published: list[CanonicalEvent] = []
        for event in events:
            redacted = self._redact_event(event, raw)
            await self._bus.publish(redacted)
            published.append(redacted)
        return published

    def _redact_event(self, event: CanonicalEvent, raw: RawSignal) -> CanonicalEvent:
        """Fill ``body_redacted`` and scrub ``title``/``labels`` (doc 05 §4)."""
        meta: Counter[str] = Counter()

        title = self._redactor(event.title)
        meta.update(title.meta)

        # The redacted body is the whole provider-native payload, scrubbed — it
        # gives the specialists context while guaranteeing no secret persists.
        body = self._redactor(json.dumps(raw.payload, sort_keys=True, default=str))
        meta.update(body.meta)

        redacted_labels: dict[str, str] = {}
        for key, value in event.labels.items():
            result = self._redactor(value)
            meta.update(result.meta)
            redacted_labels[key] = result.text

        return event.model_copy(
            update={
                "title": title.text,
                "body_redacted": body.text,
                "labels": redacted_labels,
                "redaction_meta": dict(meta),
            }
        )

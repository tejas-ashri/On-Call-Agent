"""The ingestion edge and incident pipeline (doc 01 §3).

Wires the front of the system into one runnable path: a provider webhook is
authenticated, deduped, normalized, and **redacted before persistence** by the
`IngestionEdge`, published to a tenant-keyed `EventBus`, then drained by
`run_incident_pipeline` which correlates the storm into incidents and runs each
through the durable incident graph. External systems (Kafka, Redis) are injected
seams with in-memory implementations, so the whole path runs offline.
"""

from sentinel.edge.bus import EventBus, InMemoryEventBus
from sentinel.edge.ingest import (
    DedupStore,
    IngestionEdge,
    InMemoryDedupStore,
    WebhookRejected,
)
from sentinel.edge.pipeline import run_incident_pipeline

__all__ = [
    "DedupStore",
    "EventBus",
    "InMemoryDedupStore",
    "InMemoryEventBus",
    "IngestionEdge",
    "WebhookRejected",
    "run_incident_pipeline",
]

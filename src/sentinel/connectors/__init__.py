"""The connector layer (doc 03).

A uniform :class:`Connector` adapter every provider implements, a capability
model that turns "read signals" and "write actions" into first-class data, and
the deterministic webhook idempotency key. Adding the Nth provider is writing
one adapter, not changing the system (ADR-009). Everything downstream speaks
only :class:`~sentinel.contracts.CanonicalEvent` (ADR-009).
"""

from sentinel.connectors.base import (
    CapabilityError,
    Connector,
    ConnectorBase,
    ConnectorCapabilities,
    HealthStatus,
    IntegrationContext,
    RawSignal,
    WriteCapability,
)
from sentinel.connectors.idempotency import idempotency_key
from sentinel.connectors.sentry import SentryConnector
from sentinel.connectors.vercel import VercelConnector

__all__ = [
    "CapabilityError",
    "Connector",
    "ConnectorBase",
    "ConnectorCapabilities",
    "HealthStatus",
    "IntegrationContext",
    "RawSignal",
    "SentryConnector",
    "VercelConnector",
    "WriteCapability",
    "idempotency_key",
]

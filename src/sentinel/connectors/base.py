"""The ``Connector`` Protocol and capability model (doc 03 §1.1).

A connector is the *whole* integration for one provider; the adapter is the
``normalize()`` code inside it (doc 03 §1). Heterogeneity is the enemy, so every
provider conforms to one :class:`Connector` Protocol and promises the rest of
Sentinel only a stream of :class:`~sentinel.contracts.CanonicalEvent` plus a
declared :class:`ConnectorCapabilities`. Two rules make this scale (doc 03 §1):
capabilities are *data, not code paths*, and ingestion (``normalize``) is
separated from action (``execute``).

This module reuses the canonical enums from :mod:`sentinel.enums` verbatim:
:class:`~sentinel.enums.EventKind` is the signal-type enum (its ``kind`` values
from ``CanonicalEvent`` §5.1), and ``ActionType``/``IntegrationMethod`` are the
write-action and integration-mode vocabularies. The doc's §1.1 listing inlines
copies of these; here they are imported so there is a single source of truth.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from sentinel.contracts import ActionRequest, ActionResult, CanonicalEvent
from sentinel.enums import ActionType, EventKind, IntegrationMethod


class CapabilityError(RuntimeError):
    """Raised by ``execute`` when the connector does not advertise the action.

    Doc 03 §1.1: ``execute`` MUST be a no-op (raise) unless
    ``capabilities.supports_write(action.action_type)`` is truthy.
    """


# ── Capability model (the heart of the moat, doc 03 §1.1) ────────────────────
class WriteCapability(BaseModel):
    """A single write ``action_type`` a connector can perform (doc 03 §1.1).

    ``reversible`` feeds ``ActionRequest.reversible`` (§5.4) — an irreversible
    action always pages; ``requires_agent`` is True when only the Sentinel Agent
    can perform it; ``min_tier_hint`` is the connector's recommended floor while
    policy (doc 04) stays authoritative; ``idempotent`` marks it safe to retry.
    """

    model_config = ConfigDict(extra="forbid")

    action_type: ActionType
    reversible: bool
    requires_agent: bool = False
    min_tier_hint: str = "T2"
    idempotent: bool = True


class ConnectorCapabilities(BaseModel):
    """What a connector declares it reads and writes (doc 03 §1.1).

    The orchestrator (doc 04) never asks "is this Vercel?" — it asks whether the
    connector advertises a given ``action_type`` via :meth:`supports_write`.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    reads: frozenset[EventKind]
    writes: tuple[WriteCapability, ...] = ()
    methods: frozenset[IntegrationMethod]
    oauth_scopes: tuple[str, ...] = ()

    def supports_write(self, at: ActionType) -> WriteCapability | None:
        """Return the matching :class:`WriteCapability`, or ``None`` (doc 03 §1.1)."""
        return next((w for w in self.writes if w.action_type == at), None)


# ── Inputs the connector receives (doc 03 §1.1) ──────────────────────────────
class IntegrationContext(BaseModel):
    """Per-tenant binding, hydrated from ``tenant_integrations`` (doc 02/03 §1.1).

    Carries no raw secret: credentials are brokered JIT (P9, ADR-005); the
    connector receives a ``vault_ref`` handle scoped per row, never a durable key.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    integration_id: str
    provider: str
    vault_ref: str
    config: dict[str, str] = Field(default_factory=dict)


class RawSignal(BaseModel):
    """What the edge hands the adapter (doc 03 §1.1).

    A verified, idempotency-checked, provider-native payload plus headers.
    """

    model_config = ConfigDict(extra="forbid")

    integration_id: str
    method: IntegrationMethod
    payload: dict[str, object]  # provider-native JSON (doc 03 §1.1: ``payload: dict``)
    headers: dict[str, str] = Field(default_factory=dict)
    received_at: datetime


class HealthStatus(BaseModel):
    """Cheap liveness/credential-validity result (doc 03 §1.1, §4).

    ``token_expires_at`` surfaces refresh urgency to the background reconciler.
    """

    model_config = ConfigDict(extra="forbid")

    healthy: bool
    detail: str = ""
    token_expires_at: datetime | None = None


# ── The contract (doc 03 §1.1) ───────────────────────────────────────────────
@runtime_checkable
class Connector(Protocol):
    """The canonical, structural connector interface (doc 03 §1.1).

    Connectors conform rather than inherit; ``runtime_checkable`` lets fakes be
    asserted in tests. Referenced by doc 04 (which calls ``execute``) and doc 01.
    """

    capabilities: ConnectorCapabilities

    async def verify_webhook(self, raw: RawSignal, ctx: IntegrationContext) -> bool:
        """Validate the provider signature/HMAC before anything touches the payload."""
        ...

    async def normalize(
        self, raw: RawSignal, ctx: IntegrationContext
    ) -> list[CanonicalEvent]:
        """Provider-native payload -> 0..N ``CanonicalEvent``. Pure; NO network I/O.

        Emits ``body_redacted=''`` as a placeholder; the edge redaction stage
        (doc 05) fills ``body_redacted`` + ``redaction_meta`` before Kafka.
        """
        ...

    async def healthcheck(self, ctx: IntegrationContext) -> HealthStatus:
        """Liveness/credential-validity probe powering the trust dashboard (§4)."""
        ...

    async def execute(
        self, action: ActionRequest, ctx: IntegrationContext
    ) -> ActionResult:
        """Perform a write; a no-op (raise) unless the capability is advertised."""
        ...


class ConnectorBase(ABC):
    """Shared plumbing so adapters stay tiny and uniform (doc 03 §1.1).

    Supplies the guarded ``execute()``: it raises :class:`CapabilityError`
    unless ``capabilities.supports_write(action.action_type)`` is truthy, then
    dispatches to :meth:`_do_execute`. (Idempotency lives in
    :mod:`sentinel.connectors.idempotency`; rate-limit/retry plumbing per §5 is
    out of scope for this infra-free layer.)
    """

    capabilities: ConnectorCapabilities

    @abstractmethod
    async def normalize(
        self, raw: RawSignal, ctx: IntegrationContext
    ) -> list[CanonicalEvent]: ...

    async def verify_webhook(self, raw: RawSignal, ctx: IntegrationContext) -> bool:
        """Signature verification (doc 03 §3). HMAC keys are brokered JIT, so the
        infra-free base accepts by default; concrete edges override with the
        provider HMAC check before this layer is reached."""
        return True

    async def healthcheck(self, ctx: IntegrationContext) -> HealthStatus:
        """Default healthy probe (doc 03 §1.1, §4); concrete connectors may refine."""
        return HealthStatus(healthy=True)

    async def execute(
        self, action: ActionRequest, ctx: IntegrationContext
    ) -> ActionResult:
        """Guarded write dispatch (doc 03 §1.1)."""
        cap = self.capabilities.supports_write(ActionType(action.action_type))
        if cap is None:
            raise CapabilityError(
                f"{self.capabilities.provider} cannot {action.action_type}"
            )
        return await self._do_execute(action, ctx, cap)

    @abstractmethod
    async def _do_execute(
        self,
        action: ActionRequest,
        ctx: IntegrationContext,
        cap: WriteCapability,
    ) -> ActionResult: ...

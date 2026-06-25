"""Sentry connector — read-only error/alert ingestion (doc 03 §2, §6.1).

Sentry speaks issue/event JSON over the Integration Platform webhook (HMAC in
``sentry-hook-signature``). It is read-only (no write capability). ``normalize``
implements the field-by-field mapping from doc 03 §6.1, including the judgment
call to adopt Sentry's ``issue_id`` as the dedup root rather than re-clustering
what Sentry already grouped, and to preserve ``release`` (the single most useful
correlation key for the Change/Deploy specialist, doc 04).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sentinel.connectors.base import (
    ConnectorBase,
    ConnectorCapabilities,
    IntegrationContext,
    RawSignal,
    WriteCapability,
)
from sentinel.contracts import ActionRequest, ActionResult, CanonicalEvent
from sentinel.enums import EventKind, IntegrationMethod, Severity
from sentinel.ids import new_id

# Sentry level -> canonical Severity. Lossy: ``fatal`` collapses to ``critical``
# (doc 03 §2/§6.1 — "level→severity is lossy").
_LEVEL_TO_SEVERITY: dict[str, Severity] = {
    "fatal": Severity.CRITICAL,
    "error": Severity.ERROR,
    "warning": Severity.WARNING,
    "info": Severity.INFO,
    "debug": Severity.DEBUG,
}


class SentryConnector(ConnectorBase):
    """Sentry adapter (doc 03 §6.1). Read-only: ``writes`` is empty."""

    capabilities = ConnectorCapabilities(
        provider="sentry",
        reads=frozenset({EventKind.ERROR, EventKind.ALERT}),
        writes=(),  # read-only (doc 03 §2)
        methods=frozenset({IntegrationMethod.WEBHOOK}),
        # Least-privilege scopes (doc 03 §4): never project:write.
        oauth_scopes=("org:read", "project:read", "event:read"),
    )

    async def normalize(
        self, raw: RawSignal, ctx: IntegrationContext
    ) -> list[CanonicalEvent]:
        """Sentry issue/event webhook -> ``CanonicalEvent`` (doc 03 §6.1). Pure."""
        event = self._event(raw.payload)
        if event is None:
            return []

        issue_id = str(event.get("issue_id", ""))
        level = str(event.get("level", "error"))
        has_exception = bool(event.get("exception"))

        labels: dict[str, str] = {}
        for key in ("platform", "environment", "release", "culprit"):
            value = event.get(key)
            if value is not None:
                labels[key] = str(value)
        tags = event.get("tags")
        if isinstance(tags, list):
            for pair in tags:
                if isinstance(pair, list | tuple) and len(pair) == 2:
                    labels[f"tag.{pair[0]}"] = str(pair[1])
        # Sentry's own event id is retained as a label (doc 03 §6.1).
        if (sentry_event_id := event.get("event_id")) is not None:
            labels["sentry.event_id"] = str(sentry_event_id)

        # issue alerts with no exception -> "alert"; exceptions -> "error" (§6.1).
        kind = EventKind.ERROR if has_exception else EventKind.ALERT
        # epoch-seconds float -> tz-aware UTC datetime (§6.1).
        occurred_at = self._epoch_to_dt(event.get("timestamp"), divisor=1.0)

        return [
            CanonicalEvent(
                event_id=new_id("event"),
                tenant_id=ctx.tenant_id,
                integration_id=ctx.integration_id,
                source="sentry",
                kind=kind,
                service=None,  # resolved to svc_ by discovery (doc 02); else null
                severity=_LEVEL_TO_SEVERITY.get(level, Severity.ERROR),
                title=str(event.get("title", "")),
                body_redacted="",  # the edge fills it (ADR-004, doc 05)
                # Trust Sentry's grouping: adopt issue_id as the dedup root.
                fingerprint=hashlib.sha256(
                    f"sentry:issue:{issue_id}".encode()
                ).hexdigest(),
                labels=labels,
                occurred_at=occurred_at,
                received_at=raw.received_at,
            )
        ]

    async def _do_execute(
        self,
        action: ActionRequest,
        ctx: IntegrationContext,
        cap: WriteCapability,
    ) -> ActionResult:
        # Unreachable: ConnectorBase.execute guards on the empty ``writes`` set
        # and raises CapabilityError before dispatch (doc 03 §1.1).
        raise NotImplementedError("Sentry is read-only (doc 03 §2)")

    @staticmethod
    def _event(payload: dict[str, object]) -> dict[str, object] | None:
        data = payload.get("data")
        event = data.get("event") if isinstance(data, dict) else None
        return event if isinstance(event, dict) else None

    @staticmethod
    def _epoch_to_dt(value: object, *, divisor: float) -> datetime:
        seconds = float(value) / divisor if isinstance(value, int | float) else 0.0
        return datetime.fromtimestamp(seconds, tz=UTC)

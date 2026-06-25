"""Vercel connector — deploy ingestion + rollback/redeploy (doc 03 §2, §6.2).

Vercel speaks deployment-lifecycle JSON over a webhook (``x-vercel-signature``,
HMAC-SHA1 over the raw body). ``normalize`` implements the field-by-field
mapping from doc 03 §6.2; the keystone is ``labels["commit_sha"] =
meta.githubCommitSha``, the cross-provider join that lets the correlator stitch
a Vercel deploy, a GitHub push, and a Sentry ``release``-tagged error spike into
one incident (ADR-009). ``deployment.id`` is retained because it is exactly the
handle the ``deploy.rollback``/``deploy.redeploy`` write actions target (§7).
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
from sentinel.enums import (
    ActionStatus,
    ActionType,
    EventKind,
    IntegrationMethod,
    Severity,
)
from sentinel.ids import new_id

# Vercel webhook ``type`` -> canonical Severity (doc 03 §6.2).
_TYPE_TO_SEVERITY: dict[str, Severity] = {
    "deployment.error": Severity.ERROR,
    "deployment.succeeded": Severity.INFO,
    "deployment.ready": Severity.INFO,
    "deployment.canceled": Severity.WARNING,
    "deployment.created": Severity.INFO,
}

# Human label for the deploy outcome, used to render the event title.
_TYPE_TO_TITLE: dict[str, str] = {
    "deployment.error": "Vercel deploy failed",
    "deployment.succeeded": "Vercel deploy succeeded",
    "deployment.ready": "Vercel deploy ready",
    "deployment.canceled": "Vercel deploy canceled",
    "deployment.created": "Vercel deploy created",
}


class VercelConnector(ConnectorBase):
    """Vercel adapter (doc 03 §6.2). Writes: ``deploy.rollback``/``deploy.redeploy``."""

    capabilities = ConnectorCapabilities(
        provider="vercel",
        reads=frozenset({EventKind.DEPLOY, EventKind.LOG}),
        writes=(
            # Promotion of a prior deployment is the real "rollback" (doc 03 §2);
            # both are reversible (re-promote) and idempotent on the deploy id.
            WriteCapability(
                action_type=ActionType.DEPLOY_ROLLBACK,
                reversible=True,
                requires_agent=False,
                min_tier_hint="T2",
                idempotent=True,
            ),
            WriteCapability(
                action_type=ActionType.DEPLOY_REDEPLOY,
                reversible=True,
                requires_agent=False,
                min_tier_hint="T2",
                idempotent=True,
            ),
        ),
        methods=frozenset({IntegrationMethod.WEBHOOK}),
        # read for ingest; read-write only when deploy.rollback is granted (§4).
        oauth_scopes=("read",),
    )

    async def normalize(
        self, raw: RawSignal, ctx: IntegrationContext
    ) -> list[CanonicalEvent]:
        """Vercel deploy webhook -> ``CanonicalEvent`` (doc 03 §6.2). Pure."""
        typ = str(raw.payload.get("type", ""))
        inner = raw.payload.get("payload")
        inner = inner if isinstance(inner, dict) else {}
        deployment = inner.get("deployment")
        deployment = deployment if isinstance(deployment, dict) else {}
        meta = deployment.get("meta")
        meta = meta if isinstance(meta, dict) else {}

        deployment_id = str(deployment.get("id", ""))
        name = str(deployment.get("name", ""))
        target = inner.get("target")  # may be null (preview)

        labels: dict[str, str] = {"deployment.id": deployment_id}
        if target is not None:
            labels["target"] = str(target)
        if (region := raw.payload.get("region")) is not None:
            labels["region"] = str(region)
        regions = inner.get("regions")
        if isinstance(regions, list):
            labels["regions"] = ",".join(str(r) for r in regions)
        if (url := deployment.get("url")) is not None:
            labels["deployment.url"] = str(url)
        # THE keystone cross-provider join key (doc 03 §6.2).
        if (commit_sha := meta.get("githubCommitSha")) is not None:
            labels["commit_sha"] = str(commit_sha)
        if (commit_ref := meta.get("githubCommitRef")) is not None:
            labels["commit_ref"] = str(commit_ref)

        target_suffix = f" ({target})" if target is not None else ""
        title = f"{_TYPE_TO_TITLE.get(typ, typ)}: {name}{target_suffix}"

        return [
            CanonicalEvent(
                event_id=new_id("event"),
                tenant_id=ctx.tenant_id,
                integration_id=ctx.integration_id,
                source="vercel",
                kind=EventKind.DEPLOY,  # constant (doc 03 §6.2)
                service=None,  # prj_+target -> svc_ via discovery (doc 02)
                severity=_TYPE_TO_SEVERITY.get(typ, Severity.INFO),
                title=title,
                body_redacted="",  # the edge fills it (ADR-004, doc 05)
                # one deploy lifecycle = one fingerprint family (doc 03 §6.2).
                fingerprint=hashlib.sha256(
                    f"vercel:{deployment_id}:{typ}".encode()
                ).hexdigest(),
                labels=labels,
                # epoch-millis -> tz-aware UTC (doc 03 §6.2).
                occurred_at=self._millis_to_dt(raw.payload.get("createdAt")),
                received_at=raw.received_at,
            )
        ]

    async def _do_execute(
        self,
        action: ActionRequest,
        ctx: IntegrationContext,
        cap: WriteCapability,
    ) -> ActionResult:
        """Perform an allow-listed deploy write (doc 03 §1.1, §7).

        Infra-free layer: the real call promotes/redeploys ``deployment.id`` via
        the Vercel API with a JIT credential injected by the MCP Router (doc 05).
        Here we acknowledge the dispatch so the capability guard is exercisable.
        """
        return ActionResult(
            action_id=action.action_id,
            status=ActionStatus.SUCCEEDED,
            detail=f"vercel {cap.action_type} dispatched for {action.target}",
        )

    @staticmethod
    def _millis_to_dt(value: object) -> datetime:
        ms = float(value) if isinstance(value, int | float) else 0.0
        return datetime.fromtimestamp(ms / 1000.0, tz=UTC)

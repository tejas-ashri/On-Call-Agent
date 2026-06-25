"""Metering events and the versioned price list (doc 07 §2 and §4, ADR-010).

This module owns the shape on the metering wire and the cost attribution table:

- :class:`Metric` mirrors ``usage_meter.metric`` (doc 02, doc 07 §2): the six
  things we count, verbatim.
- :class:`UsageEvent` is the frozen Pydantic v2 record producers publish to the
  ``metering.usage`` Kafka topic; the metering service writes one row per event
  and dedupes on ``UNIQUE(tenant_id, idempotency_key)``. ADR-010 requires the
  key be *deterministic* — derived from the source event, never the wall clock —
  so a Kafka at-least-once replay recomputes the same key and collides rather
  than double-charging.
- :class:`PriceList` is the versioned ``(metric, model_tier) -> unit_cost``
  table of doc 07 §4. The metering service reads it to snapshot
  ``unit_cost_usd`` at accrual; the aggregator never re-reads it (snapshots are
  immutable — invoices must not move when prices change).

This file is offline and pure: no Kafka, no Postgres, no Redis.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field


class Metric(StrEnum):
    """The six metered dimensions, mirroring ``usage_meter.metric`` (doc 07 §2).

    Defined here (not in :mod:`sentinel.enums`) because metering owns this
    vocabulary; the string values are the canonical on-the-wire identifiers and
    must match doc 02 verbatim.
    """

    LLM_TOKENS_IN = "llm_tokens_in"
    LLM_TOKENS_OUT = "llm_tokens_out"
    TOOL_CALL = "tool_call"
    INCIDENT = "incident"
    EVENT_INGESTED = "event_ingested"
    ACTION_EXECUTED = "action_executed"


class UsageEvent(BaseModel):
    """One metering event; becomes one row in ``usage_meter`` (doc 07 §2).

    Frozen and ``extra="forbid"``: the event is an immutable fact published to
    ``metering.usage`` and partition-keyed by ``tenant_id`` (ADR-007). The
    load-bearing field is :attr:`idempotency_key` — ADR-010 requires retries
    never double-charge, so it is derived deterministically from the *source
    event* (never a UUID, never ``occurred_at``); the metering insert is
    ``ON CONFLICT (tenant_id, idempotency_key) DO NOTHING``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    incident_id: str | None = None  # null for non-incident usage (e.g. event_ingested)
    metric: Metric
    quantity: Decimal = Field(gt=0)
    model: str | None = None  # resolved model id; set for llm_* metrics (doc 02)
    unit_cost_usd: Decimal | None = None  # snapshotted at accrual by the service (§4)
    occurred_at: datetime
    idempotency_key: str

    @classmethod
    def for_llm(
        cls,
        *,
        tenant_id: str,
        incident_id: str,
        node: str,
        call_seq: int,
        metric: Metric,
        tokens: int,
        model: str,
    ) -> Self:
        """Build an LLM-token event with a DETERMINISTIC idempotency key (ADR-010).

        The key is ``f"{incident_id}:{node}:{call_seq}:{metric.value}"`` — e.g.
        ``"inc_01HZ:synthesize:3:llm_tokens_out"``. It is a pure function of the
        source-event coordinates (which incident, which orchestrator node, which
        call in that node, which token direction), so replaying the same LLM call
        recomputes the same key and dedupes on insert instead of inflating the
        bill. ``occurred_at`` is set to the build time for the in-flight record,
        but it is deliberately NOT part of the key.
        """
        from datetime import UTC

        key = f"{incident_id}:{node}:{call_seq}:{metric.value}"
        return cls(
            tenant_id=tenant_id,
            incident_id=incident_id,
            metric=metric,
            quantity=Decimal(tokens),
            model=model,
            occurred_at=datetime.now(UTC),
            idempotency_key=key,
        )


# Doc 07 §4 — assumed plausible 2026 Anthropic-class list prices. The
# architecture depends only on the ~3-5x spread between tiers (ADR-008), not the
# exact cents; these are stated as assumptions. Keys are (metric, model_tier),
# where the tier is None for the non-LLM, model-independent metrics.
_PRICES_V2026_01: dict[tuple[Metric, str | None], Decimal] = {
    (Metric.LLM_TOKENS_IN, "cheap"): Decimal("0.000001"),  # $1.00 / Mtok in
    (Metric.LLM_TOKENS_OUT, "cheap"): Decimal("0.000005"),  # $5.00 / Mtok out
    (Metric.LLM_TOKENS_IN, "strong"): Decimal("0.000003"),  # $3.00 / Mtok in
    (Metric.LLM_TOKENS_OUT, "strong"): Decimal("0.000015"),  # $15.00 / Mtok out
    (Metric.LLM_TOKENS_IN, "premium"): Decimal("0.000005"),  # $5.00 / Mtok in
    (Metric.LLM_TOKENS_OUT, "premium"): Decimal("0.000025"),  # $25.00 / Mtok out
    (Metric.TOOL_CALL, None): Decimal("0.0002"),  # blended provider + broker
    (Metric.EVENT_INGESTED, None): Decimal("0.0000060"),  # ingestion + storage
}


class PriceList:
    """Versioned ``(metric, model_tier) -> unit_cost_usd`` table (doc 07 §4).

    Repricing = publishing a new version; it affects only rows accrued after the
    change, because the metering service snapshots ``unit_cost_usd`` onto each
    row at accrual time and the aggregator never re-reads this table (doc 07 §4:
    "snapshots are immutable"). Seeded with the assumed 2026 prices.
    """

    def __init__(
        self,
        *,
        version: str = "2026-01",
        prices: dict[tuple[Metric, str | None], Decimal] | None = None,
    ) -> None:
        self.version = version
        self._prices: dict[tuple[Metric, str | None], Decimal] = (
            dict(_PRICES_V2026_01) if prices is None else dict(prices)
        )

    def unit_cost(self, metric: Metric, model_tier: str | None) -> Decimal:
        """Return the unit cost for ``(metric, model_tier)`` (doc 07 §4).

        Raises :class:`KeyError` for an unpriced pair — ``incident`` and
        ``action_executed`` are billed by other dimensions (doc 07 §1/§3) and a
        bad/unknown tier must fail loudly rather than silently under-charge.
        """
        return self._prices[(metric, model_tier)]

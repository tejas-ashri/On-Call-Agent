"""Metering and the per-tenant cost circuit breaker (doc 07 §2/§4/§5, ADR-010).

The concrete realization of P7 ("everything metered, everything bounded") and
ADR-010 ("per-tenant cost ceilings + idempotent metering as first-class
governance; cost is also a safety control"). Two halves:

- :mod:`sentinel.billing.metering` — the on-the-wire :class:`UsageEvent` with a
  deterministic idempotency key (replays never double-charge) and the versioned
  :class:`PriceList` (doc 07 §4 cost attribution).
- :mod:`sentinel.billing.breaker` — :func:`accrue_and_check`, the cost circuit
  breaker; an open breaker degrades the tenant to T0 + page (C1: cost caps blast
  radius). Redis is abstracted behind the :class:`BudgetStore` Protocol.
"""

from __future__ import annotations

from sentinel.billing.breaker import (
    SOFT_THRESHOLDS_PCT,
    BreakerResult,
    BudgetStore,
    accrue_and_check,
)
from sentinel.billing.metering import Metric, PriceList, UsageEvent

__all__ = [
    "SOFT_THRESHOLDS_PCT",
    "BreakerResult",
    "BudgetStore",
    "Metric",
    "PriceList",
    "UsageEvent",
    "accrue_and_check",
]

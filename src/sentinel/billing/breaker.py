"""The per-tenant cost circuit breaker (doc 07 §5, ADR-010).

Every tenant has a hard monthly ceiling (``tenants.cost_ceiling_usd``). On every
``usage_meter`` accrual the metering service increments a Redis budget counter
and evaluates the breaker; the moment an accrual crosses the ceiling, the next
autonomy decision sees the breaker **open**.

ADR-010 ("cost is also a safety control") and C1 ("never cause an outage") make
this more than a margin lever: an open breaker means **degrade the tenant to T0
(observe-only) + page** (doc 00/04 own that tier semantics). Capping spend caps
blast radius — a dumpster-fire tenant costs us at most their ceiling, and over
budget we stop *writing to prod* but still diagnose-and-page (fail-safe: cost
exhaustion and a stuck agent converge on the same safe behavior).

Below the hard cap there are soft warning thresholds (doc 07 §5) at 50/80/95%,
each fired once, so a tenant is never surprised by the cap.

The Redis primitives are abstracted behind the :class:`BudgetStore` Protocol so
this module stays offline and pure — tests inject an in-memory fake (no Redis).
The breaker's two set-once semantics (``try_open``/``try_warn``) correspond to
Redis ``SET NX`` and are what make paging and warning idempotent under Kafka's
at-least-once replays.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

#: Soft warning thresholds (percent of ceiling) before the hard cap (doc 07 §5):
#: 50% noted on the dashboard, 80% heads-up, 95% "autonomy about to be capped".
#: Ordered ascending so a single accrual that vaults several bands reports the
#: highest one crossed.
SOFT_THRESHOLDS_PCT: tuple[int, ...] = (50, 80, 95)


@runtime_checkable
class BudgetStore(Protocol):
    """The budget/breaker state store (doc 02's ``budget:``/``breaker:`` keys).

    A thin Protocol over the Redis operations doc 07 §5 needs, so the breaker is
    testable with an in-memory fake. All three methods are keyed by tenant; the
    two ``try_*`` methods are **set-once** (Redis ``SET NX``) — they return True
    only the first time the bit flips, which is what makes paging and warning
    idempotent across retries.
    """

    def incr(self, tenant_id: str, yyyymm: str, delta: Decimal) -> Decimal:
        """Add ``delta`` to ``budget:{tenant_id}:month:{yyyymm}``; return the new total."""
        ...

    def try_open(self, tenant_id: str) -> bool:
        """Flip ``breaker:{tenant_id}`` to open; True only on the FIRST flip (SET NX)."""
        ...

    def try_warn(self, tenant_id: str, yyyymm: str, pct: int) -> bool:
        """Mark the ``pct`` soft threshold warned; True only the FIRST time (SET NX)."""
        ...


class BreakerResult(BaseModel):
    """The outcome of one :func:`accrue_and_check` evaluation (doc 07 §5).

    ``open=True`` means **degrade the tenant to T0 (observe-only) + page** — the
    caller (doc 04 §7 escalation engine) must force the outcome to "page", never
    "act", regardless of grant or confidence. ``paged`` is True only on the open
    *transition* (so the page fires exactly once); ``warned_pct`` is the highest
    soft threshold this accrual crossed for the first time, else ``None``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    open: bool
    spent: Decimal
    fraction: float
    paged: bool
    warned_pct: int | None = None


def accrue_and_check(
    store: BudgetStore,
    *,
    tenant_id: str,
    yyyymm: str,
    ceiling_usd: Decimal,
    delta_usd: Decimal,
) -> BreakerResult:
    """Accrue ``delta_usd`` and evaluate the cost breaker (doc 07 §5, ADR-010).

    Called on every ``usage_meter`` insert. Increments the monthly budget then:

    - if ``spent >= ceiling`` → **open** the breaker (page once via
      ``try_open``) and return ``open=True``; the caller then forces the tenant
      to **T0 + page** (C1: cost caps blast radius). No soft warnings fire on an
      open result — the cap supersedes them.
    - otherwise fire soft warnings at 50/80/95% once each (``try_warn``),
      reporting the highest threshold newly crossed.

    A zero ``ceiling_usd`` is treated as "any spend is over budget": the breaker
    opens and ``fraction`` is reported as ``0.0`` (a documented sentinel — there
    is no meaningful ratio against a zero ceiling).
    """
    spent = store.incr(tenant_id, yyyymm, delta_usd)
    fraction = float(spent / ceiling_usd) if ceiling_usd > 0 else 0.0

    # Hard cap: open the breaker. Idempotent flip pages exactly once (doc 07 §5).
    if ceiling_usd <= 0 or spent >= ceiling_usd:
        paged = store.try_open(tenant_id)
        return BreakerResult(
            open=True, spent=spent, fraction=fraction, paged=paged, warned_pct=None
        )

    # Soft thresholds: warn once per band crossed; report the highest new one.
    warned_pct: int | None = None
    for pct in SOFT_THRESHOLDS_PCT:
        if fraction >= pct / 100 and store.try_warn(tenant_id, yyyymm, pct):
            warned_pct = pct

    return BreakerResult(
        open=False, spent=spent, fraction=fraction, paged=False, warned_pct=warned_pct
    )

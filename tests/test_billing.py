"""Tests for the metering + cost circuit breaker (doc 07 §2/§4/§5, ADR-010).

Written test-first. Two properties dominate:

- **Idempotent metering** (ADR-010): ``UsageEvent.for_llm`` derives a key from
  the *source event*, never the wall clock, so a replay recomputes the SAME key
  and dedupes on ``UNIQUE(tenant_id, idempotency_key)``.
- **The breaker is set-once** (doc 07 §5): it pages exactly once on the open
  transition and warns exactly once per soft threshold — verified against an
  in-memory ``FakeBudgetStore`` (no Redis).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sentinel.billing import (
    BreakerResult,
    BudgetStore,
    Metric,
    PriceList,
    UsageEvent,
    accrue_and_check,
)


class FakeBudgetStore:
    """In-memory stand-in for the Redis-backed ``BudgetStore`` (doc 07 §5).

    Models exactly the three set-once semantics the real store gives us:
    ``incr`` accumulates a running total; ``try_open``/``try_warn`` are
    ``SET NX`` — True only on the first flip.
    """

    def __init__(self) -> None:
        self.totals: dict[tuple[str, str], Decimal] = {}
        self.opened: set[str] = set()
        self.warned: set[tuple[str, str, int]] = set()

    def incr(self, tenant_id: str, yyyymm: str, delta: Decimal) -> Decimal:
        key = (tenant_id, yyyymm)
        total = self.totals.get(key, Decimal(0)) + delta
        self.totals[key] = total
        return total

    def try_open(self, tenant_id: str) -> bool:
        if tenant_id in self.opened:
            return False
        self.opened.add(tenant_id)
        return True

    def try_warn(self, tenant_id: str, yyyymm: str, pct: int) -> bool:
        key = (tenant_id, yyyymm, pct)
        if key in self.warned:
            return False
        self.warned.add(key)
        return True


def test_fake_store_satisfies_protocol() -> None:
    # Structural check: the fake is a valid BudgetStore (runtime_checkable Protocol).
    store: BudgetStore = FakeBudgetStore()
    assert isinstance(store, BudgetStore)


# --------------------------------------------------------------------------- #
# Metric enum
# --------------------------------------------------------------------------- #


def test_metric_values_mirror_usage_meter() -> None:
    # The six values are verbatim doc 02 / doc 07 §2 — the on-the-wire strings.
    assert {m.value for m in Metric} == {
        "llm_tokens_in",
        "llm_tokens_out",
        "tool_call",
        "incident",
        "event_ingested",
        "action_executed",
    }


# --------------------------------------------------------------------------- #
# UsageEvent + idempotency (ADR-010)
# --------------------------------------------------------------------------- #


def _for_llm(call_seq: int = 3) -> UsageEvent:
    return UsageEvent.for_llm(
        tenant_id="ten_abc",
        incident_id="inc_01HZ",
        node="synthesize",
        call_seq=call_seq,
        metric=Metric.LLM_TOKENS_OUT,
        tokens=6000,
        model="claude-strong",
    )


def test_for_llm_key_is_deterministic_across_two_builds() -> None:
    # ADR-010: a replay recomputes the SAME key (no time/uuid). The two builds
    # are independent objects but MUST collide on idempotency_key.
    a = _for_llm()
    b = _for_llm()
    assert a is not b
    assert a.idempotency_key == b.idempotency_key
    assert a.idempotency_key == "inc_01HZ:synthesize:3:llm_tokens_out"


def test_for_llm_key_varies_with_source_dimensions() -> None:
    # A different call in the same node is a different chargeable event.
    assert _for_llm(call_seq=3).idempotency_key != _for_llm(call_seq=4).idempotency_key


def test_for_llm_populates_fields() -> None:
    ev = _for_llm()
    assert ev.tenant_id == "ten_abc"
    assert ev.incident_id == "inc_01HZ"
    assert ev.metric is Metric.LLM_TOKENS_OUT
    assert ev.quantity == Decimal(6000)
    assert ev.model == "claude-strong"


def test_usage_event_is_frozen() -> None:
    ev = _for_llm()
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError on frozen
        ev.quantity = Decimal(1)  # type: ignore[misc]


def test_usage_event_forbids_extra_fields() -> None:
    from datetime import UTC, datetime

    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        UsageEvent(  # type: ignore[call-arg]
            tenant_id="ten_abc",
            metric=Metric.INCIDENT,
            quantity=Decimal(1),
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            idempotency_key="k",
            bogus="nope",
        )


def test_usage_event_quantity_must_be_positive() -> None:
    from datetime import UTC, datetime

    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError (gt=0)
        UsageEvent(
            tenant_id="ten_abc",
            metric=Metric.INCIDENT,
            quantity=Decimal(0),
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            idempotency_key="k",
        )


# --------------------------------------------------------------------------- #
# PriceList (doc 07 §4)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("metric", "tier", "expected"),
    [
        (Metric.LLM_TOKENS_IN, "cheap", Decimal("0.000001")),
        (Metric.LLM_TOKENS_OUT, "cheap", Decimal("0.000005")),
        (Metric.LLM_TOKENS_IN, "strong", Decimal("0.000003")),
        (Metric.LLM_TOKENS_OUT, "strong", Decimal("0.000015")),
        (Metric.LLM_TOKENS_IN, "premium", Decimal("0.000005")),
        (Metric.LLM_TOKENS_OUT, "premium", Decimal("0.000025")),
        (Metric.TOOL_CALL, None, Decimal("0.0002")),
        (Metric.EVENT_INGESTED, None, Decimal("0.0000060")),
    ],
)
def test_unit_cost_matches_doc_07_section_4(
    metric: Metric, tier: str | None, expected: Decimal
) -> None:
    assert PriceList().unit_cost(metric, tier) == expected


def test_unit_cost_unknown_metric_tier_raises() -> None:
    # incident / action_executed are billed by other dimensions, not priced here.
    with pytest.raises(KeyError):
        PriceList().unit_cost(Metric.INCIDENT, None)


def test_unit_cost_unknown_tier_for_llm_raises() -> None:
    with pytest.raises(KeyError):
        PriceList().unit_cost(Metric.LLM_TOKENS_IN, "gold")


def test_price_list_is_versioned() -> None:
    assert PriceList().version  # non-empty version stamp


# --------------------------------------------------------------------------- #
# Breaker (doc 07 §5)
# --------------------------------------------------------------------------- #

_CEIL = Decimal("50.00")
_YM = "202606"


def test_breaker_stays_closed_below_ceiling() -> None:
    store = FakeBudgetStore()
    r = accrue_and_check(
        store, tenant_id="ten_a", yyyymm=_YM, ceiling_usd=_CEIL, delta_usd=Decimal("10.00")
    )
    assert isinstance(r, BreakerResult)
    assert r.open is False
    assert r.paged is False
    assert r.spent == Decimal("10.00")
    assert r.fraction == pytest.approx(0.20)
    assert r.warned_pct is None


def test_breaker_opens_at_exactly_the_ceiling() -> None:
    store = FakeBudgetStore()
    r = accrue_and_check(
        store, tenant_id="ten_a", yyyymm=_YM, ceiling_usd=_CEIL, delta_usd=_CEIL
    )
    assert r.open is True  # spent >= ceiling
    assert r.paged is True  # first crossing pages
    assert r.spent == _CEIL
    assert r.fraction == pytest.approx(1.0)


def test_breaker_opens_over_the_ceiling() -> None:
    store = FakeBudgetStore()
    r = accrue_and_check(
        store, tenant_id="ten_a", yyyymm=_YM, ceiling_usd=_CEIL, delta_usd=Decimal("75.00")
    )
    assert r.open is True
    assert r.paged is True
    assert r.fraction == pytest.approx(1.5)


def test_breaker_pages_only_on_first_crossing() -> None:
    # Idempotent try_open: the open transition pages exactly once, even though
    # every subsequent accrual still reports open=True (caller forces T0+page).
    store = FakeBudgetStore()
    first = accrue_and_check(
        store, tenant_id="ten_a", yyyymm=_YM, ceiling_usd=_CEIL, delta_usd=_CEIL
    )
    second = accrue_and_check(
        store, tenant_id="ten_a", yyyymm=_YM, ceiling_usd=_CEIL, delta_usd=Decimal("1.00")
    )
    assert first.open is True and first.paged is True
    assert second.open is True and second.paged is False


def test_soft_warnings_fire_once_per_threshold() -> None:
    # Crossing 50/80/95% each warns exactly once; re-crossing the same band is silent.
    store = FakeBudgetStore()

    r50 = accrue_and_check(
        store, tenant_id="ten_a", yyyymm=_YM, ceiling_usd=_CEIL, delta_usd=Decimal("25.00")
    )  # 50%
    assert r50.open is False
    assert r50.warned_pct == 50

    # Still in the 50% band (60%): no NEW threshold crossed, no warning.
    r60 = accrue_and_check(
        store, tenant_id="ten_a", yyyymm=_YM, ceiling_usd=_CEIL, delta_usd=Decimal("5.00")
    )
    assert r60.warned_pct is None

    # Cross 80% (to 82%): warns at 80 once.
    r80 = accrue_and_check(
        store, tenant_id="ten_a", yyyymm=_YM, ceiling_usd=_CEIL, delta_usd=Decimal("11.00")
    )
    assert r80.warned_pct == 80

    # Cross 95% (to 96%): warns at 95 once.
    r95 = accrue_and_check(
        store, tenant_id="ten_a", yyyymm=_YM, ceiling_usd=_CEIL, delta_usd=Decimal("7.00")
    )
    assert r95.warned_pct == 95


def test_single_accrue_crossing_multiple_thresholds_reports_highest() -> None:
    # One big accrual that vaults from 0 to 96% should report the highest crossed
    # threshold (95) and mark all lower ones warned so they never re-fire.
    store = FakeBudgetStore()
    r = accrue_and_check(
        store, tenant_id="ten_a", yyyymm=_YM, ceiling_usd=_CEIL, delta_usd=Decimal("48.00")
    )  # 96%
    assert r.open is False
    assert r.warned_pct == 95
    # All three soft thresholds are now consumed.
    assert ("ten_a", _YM, 50) in store.warned
    assert ("ten_a", _YM, 80) in store.warned
    assert ("ten_a", _YM, 95) in store.warned


def test_fraction_computed_correctly() -> None:
    store = FakeBudgetStore()
    r = accrue_and_check(
        store,
        tenant_id="ten_a",
        yyyymm=_YM,
        ceiling_usd=Decimal("40.00"),
        delta_usd=Decimal("10.00"),
    )
    assert r.fraction == pytest.approx(0.25)


def test_zero_ceiling_opens_immediately() -> None:
    # A $0 ceiling means any spend is over budget; open + page on first accrual.
    store = FakeBudgetStore()
    r = accrue_and_check(
        store, tenant_id="ten_a", yyyymm=_YM, ceiling_usd=Decimal("0"), delta_usd=Decimal("0.01")
    )
    assert r.open is True
    assert r.paged is True
    assert r.fraction == 0.0  # documented sentinel for a zero ceiling


def test_tenants_are_isolated() -> None:
    store = FakeBudgetStore()
    accrue_and_check(
        store, tenant_id="ten_a", yyyymm=_YM, ceiling_usd=_CEIL, delta_usd=_CEIL
    )
    # ten_b is untouched by ten_a opening its breaker.
    r = accrue_and_check(
        store, tenant_id="ten_b", yyyymm=_YM, ceiling_usd=_CEIL, delta_usd=Decimal("1.00")
    )
    assert r.open is False
    assert r.paged is False

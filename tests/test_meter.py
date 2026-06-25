"""Token + cost metering (doc 07 — cost governance).

The mechanism that answers "how much did it use": every call's tokens and dollar
cost, plus a running total. Cost math is pinned to the published Haiku rates.
"""

from __future__ import annotations

from sentinel.agents import CallUsage, TokenMeter, cost_usd


def test_haiku_cost_math() -> None:
    # Haiku 4.5: $1.00 / 1M input, $5.00 / 1M output
    assert cost_usd("claude-haiku-4-5", CallUsage(input_tokens=1_000_000, output_tokens=0)) == 1.0
    assert cost_usd("claude-haiku-4-5", CallUsage(input_tokens=0, output_tokens=1_000_000)) == 5.0
    blended = cost_usd("claude-haiku-4-5", CallUsage(input_tokens=1_000, output_tokens=500))
    assert abs(blended - (0.001 + 0.0025)) < 1e-9


def test_unknown_model_falls_back_to_cheapest() -> None:
    assert cost_usd("mystery-model", CallUsage(input_tokens=1_000_000, output_tokens=0)) == 1.0


def test_cache_reads_are_discounted() -> None:
    full = cost_usd("claude-haiku-4-5", CallUsage(input_tokens=1_000_000, output_tokens=0))
    cached = cost_usd(
        "claude-haiku-4-5",
        CallUsage(input_tokens=0, output_tokens=0, cache_read_input_tokens=1_000_000),
    )
    assert cached == full * 0.1


def test_meter_accumulates_and_reports() -> None:
    lines: list[str] = []
    meter = TokenMeter(sink=lines.append)
    meter.record(model="claude-haiku-4-5", label="specialist:metric", usage=CallUsage(1_000, 200))
    meter.record(model="claude-haiku-4-5", label="commander", usage=CallUsage(2_000, 400))

    assert meter.call_count == 2
    assert meter.total_input_tokens == 3_000
    assert meter.total_output_tokens == 600
    # cost is linear, so the total equals the cost of the combined tokens
    assert abs(meter.total_cost_usd - cost_usd("claude-haiku-4-5", CallUsage(3_000, 600))) < 1e-9
    assert len(lines) == 2 and "specialist:metric" in lines[0]
    assert "total $" in meter.summary()

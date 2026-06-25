"""Token + cost metering for LLM calls (doc 07 — cost governance).

Every real Claude call is recorded here with its token counts and dollar cost,
and a one-line tally is printed as it happens — so usage is always visible
("how much did it use"). Prices are per the claude-api current pricing (USD per
1M tokens). Cache reads/writes are priced at the documented ~0.1× / ~1.25× of the
input rate. This module is pure and offline; nothing here makes a network call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens for a model (input, output)."""

    input_per_mtok: float
    output_per_mtok: float


# Current pricing (USD / 1M tokens), per the claude-api model table.
PRICES: dict[str, ModelPrice] = {
    "claude-haiku-4-5": ModelPrice(1.0, 5.0),
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0),
    "claude-opus-4-8": ModelPrice(5.0, 25.0),
}
_DEFAULT_PRICE = PRICES["claude-haiku-4-5"]  # default to the cheapest if unknown
_CACHE_READ_FACTOR = 0.1
_CACHE_WRITE_FACTOR = 1.25


@dataclass(frozen=True)
class CallUsage:
    """Token usage from one ``messages.create`` response (mirrors ``response.usage``)."""

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


def cost_usd(model: str, usage: CallUsage) -> float:
    """Dollar cost of one call, including discounted cache reads and writes."""
    price = PRICES.get(model, _DEFAULT_PRICE)
    return (
        usage.input_tokens / 1_000_000 * price.input_per_mtok
        + usage.cache_read_input_tokens / 1_000_000 * price.input_per_mtok * _CACHE_READ_FACTOR
        + usage.cache_creation_input_tokens
        / 1_000_000
        * price.input_per_mtok
        * _CACHE_WRITE_FACTOR
        + usage.output_tokens / 1_000_000 * price.output_per_mtok
    )


@dataclass(frozen=True)
class _Entry:
    label: str
    model: str
    usage: CallUsage
    cost_usd: float


class TokenMeter:
    """Records each LLM call's tokens + cost and prints a running tally (doc 07)."""

    def __init__(self, *, sink: Callable[[str], None] = print) -> None:
        self._sink = sink
        self._entries: list[_Entry] = []

    def record(self, *, model: str, label: str, usage: CallUsage) -> float:
        """Record one call, print a one-line cost note, and return the call's cost."""
        call_cost = cost_usd(model, usage)
        self._entries.append(_Entry(label=label, model=model, usage=usage, cost_usd=call_cost))
        self._sink(
            f"  💸 {label}: {usage.input_tokens} in + {usage.output_tokens} out "
            f"on {model} → ${call_cost:.4f}"
        )
        return call_cost

    @property
    def call_count(self) -> int:
        return len(self._entries)

    @property
    def total_input_tokens(self) -> int:
        return sum(e.usage.input_tokens for e in self._entries)

    @property
    def total_output_tokens(self) -> int:
        return sum(e.usage.output_tokens for e in self._entries)

    @property
    def total_cost_usd(self) -> float:
        return sum(e.cost_usd for e in self._entries)

    def summary(self) -> str:
        """A one-line run total, e.g. ``7 LLM calls · 9,120 in + 3,400 out · total $0.0262``."""
        return (
            f"{self.call_count} LLM calls · {self.total_input_tokens:,} in + "
            f"{self.total_output_tokens:,} out · total ${self.total_cost_usd:.4f}"
        )

"""LLM-facing agent plumbing (doc 04 §3, doc 07).

The cost-governance primitives that make real Claude usage transparent: a
:class:`TokenMeter` that records every call's tokens and dollar cost and prints a
running tally, plus the per-model price table. The actual LLM-backed specialists
plug in behind ``Settings.llm_ready`` (doc 04 §3) — until then the orchestrator
runs on deterministic fakes and spends nothing.
"""

from sentinel.agents.meter import PRICES, CallUsage, ModelPrice, TokenMeter, cost_usd

__all__ = ["PRICES", "CallUsage", "ModelPrice", "TokenMeter", "cost_usd"]

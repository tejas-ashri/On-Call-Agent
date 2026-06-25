"""Independent safety floors that the escalation policy (doc 04 §7) trusts.

This package holds deterministic, LLM-independent checks that close the open
risks doc 04 §10 records about trusting Planner-supplied judgments absolutely.
Its first member is the rule-based reversibility classifier of doc 05 §6, which
can only ever downgrade ``reversible``, never upgrade it.
"""

from __future__ import annotations

from sentinel.safety.reversibility import (
    IRREVERSIBLE_ACTION_TYPES,
    classify_reversibility,
)

__all__ = ["IRREVERSIBLE_ACTION_TYPES", "classify_reversibility"]

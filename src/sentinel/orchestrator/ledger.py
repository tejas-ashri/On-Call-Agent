"""At-most-once execution of remediation actions (doc 04 §2 ``act``, C1).

LangGraph checkpoints *after* a node returns. If the process dies after a write
reached the provider but before the ``act`` checkpoint, the node re-runs on
resume — and a naive executor would roll back (or restart, or scale) twice. The
same happens when a delivery is retried or an approval arrives twice.

Two pieces close that gap:

* :func:`action_id_for` derives a **deterministic** id from the incident and the
  plan, so every re-run of the same decision names the same action. It is also
  the one-tap approval token (an approval must echo it) and the idempotency key
  handed to the executor for the provider call.
* :class:`ActionLedger` is a durable claim table keyed on that id. ``act`` claims
  the id *before* executing; a second claim fails, so the action never runs
  twice. If the first attempt recorded its result, the re-run reuses it; if it
  did not, the outcome is unknown and the incident escalates for a human to
  verify rather than guessing.

:class:`InMemoryActionLedger` is the offline default; production backs the same
Protocol with a Postgres table (``INSERT ... ON CONFLICT DO NOTHING``).
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from sentinel.contracts import RemediationPlan
from sentinel.contracts.actions import ActionResult


def action_id_for(incident_id: str, plan: RemediationPlan) -> str:
    """The deterministic id of executing ``plan`` for ``incident_id``.

    Covers everything that changes what the write does (action, target, params),
    and nothing that doesn't (summary, command text), so a re-drafted plan with
    the same effect maps to the same action.
    """
    material = json.dumps(
        {
            "incident_id": incident_id,
            "action_type": plan.action_type,
            "target": plan.target,
            "params": plan.params,
        },
        sort_keys=True,
        default=str,
    )
    return f"act_{hashlib.sha256(material.encode()).hexdigest()[:32]}"


class ActionLedger(Protocol):
    """A durable record of which actions have been attempted, and how they ended."""

    async def claim(self, action_id: str) -> bool:
        """Atomically claim ``action_id``: ``True`` the first time, ``False`` ever after."""
        ...

    async def complete(self, action_id: str, result: ActionResult) -> None:
        """Record the result of a claimed action."""
        ...

    async def result(self, action_id: str) -> ActionResult | None:
        """The recorded result, or ``None`` if the action never completed."""
        ...


class InMemoryActionLedger:
    """An offline :class:`ActionLedger` (process-local, so not durable across restarts)."""

    def __init__(self) -> None:
        self._claimed: set[str] = set()
        self._results: dict[str, ActionResult] = {}

    async def claim(self, action_id: str) -> bool:
        if action_id in self._claimed:
            return False
        self._claimed.add(action_id)
        return True

    async def complete(self, action_id: str, result: ActionResult) -> None:
        self._results[action_id] = result

    async def result(self, action_id: str) -> ActionResult | None:
        return self._results.get(action_id)

"""Write-action request/result contracts (doc 00 §5.4)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from sentinel.enums import ActionStatus, ActionType, AutonomyTier


class ActionRequest(BaseModel):
    """A proposed write against a tenant's infrastructure.

    ``reversible`` drives the escalation hard stop (doc 04 §7); ``required_tier``
    is the minimum autonomy tier needed to execute it.
    """

    model_config = ConfigDict(extra="forbid")

    action_id: str
    tenant_id: str
    incident_id: str
    action_type: ActionType
    target: dict[str, object]
    params: dict[str, object] = Field(default_factory=dict)
    reversible: bool
    proposed_tier: AutonomyTier
    required_tier: AutonomyTier
    status: ActionStatus = ActionStatus.PROPOSED
    approved_by: str | None = None
    executed_at: datetime | None = None
    rollback_ref: str | None = None


class ActionResult(BaseModel):
    """The outcome of executing an :class:`ActionRequest`."""

    model_config = ConfigDict(extra="forbid")

    action_id: str
    status: ActionStatus
    detail: str = ""
    rolled_back: bool = False

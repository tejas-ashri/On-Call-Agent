"""Tenant-issued autonomy grants (doc 00 §5.2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from sentinel.enums import ActionType, AutonomyTier


class AutonomyGrant(BaseModel):
    """The highest autonomy tier a tenant permits for one ``action_type``.

    Default is ``T0`` (observe-only) — autonomy is always opt-in (ADR-003, P5).
    """

    model_config = ConfigDict(extra="forbid")

    grant_id: str
    tenant_id: str
    action_type: ActionType
    max_tier: AutonomyTier = AutonomyTier.T0
    constraints: dict[str, object] = Field(default_factory=dict)
    granted_by: str
    granted_at: datetime
    revoked_at: datetime | None = None

    @property
    def active(self) -> bool:
        """A grant is in force until it is revoked."""
        return self.revoked_at is None

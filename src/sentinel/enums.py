"""Canonical enums and the confidence-band cut (doc 00 §4–§6, doc 03, doc 04).

These string values are the single source of truth referenced verbatim by the
contracts, the escalation engine, and (later) the persistence and connector
layers. They MUST match the constitution.
"""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class EventKind(StrEnum):
    ALERT = "alert"
    LOG = "log"
    METRIC = "metric"
    DEPLOY = "deploy"
    ERROR = "error"
    TRACE = "trace"
    PING = "ping"


class IntegrationMethod(StrEnum):
    WEBHOOK = "webhook"
    POLL = "poll"
    AGENT = "agent"


class ActionType(StrEnum):
    """Write action types; mirror ``AutonomyGrant.action_type`` (doc 00 §5.2)."""

    DEPLOY_ROLLBACK = "deploy.rollback"
    DEPLOY_REDEPLOY = "deploy.redeploy"
    PROCESS_RESTART = "process.restart"
    SERVICE_SCALE = "service.scale"
    SERVICE_PAUSE = "service.pause"
    CONFIG_SET_ENV = "config.set_env"
    DB_FAILOVER = "db.failover"
    FEATURE_FLAG_TOGGLE = "feature_flag.toggle"


class AutonomyTier(StrEnum):
    T0 = "T0"  # Observe — read-only, page only (default)
    T1 = "T1"  # Suggest — drafts a fix, never executes
    T2 = "T2"  # Approve-to-act — executes after one-tap approval
    T3 = "T3"  # Autonomous — auto-executes reversible HIGH-confidence actions


class ConfidenceBand(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class IncidentState(StrEnum):
    OPEN = "OPEN"
    TRIAGING = "TRIAGING"
    DIAGNOSED = "DIAGNOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REMEDIATING = "REMEDIATING"
    MONITORING = "MONITORING"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


# Confidence band cut (doc 00 §6): LOW < 0.5 ≤ MEDIUM < 0.8 ≤ HIGH.
BAND_MEDIUM_THRESHOLD = 0.5
BAND_HIGH_THRESHOLD = 0.8


def band_for(confidence: float) -> ConfidenceBand:
    """Map a calibrated confidence in [0, 1] to its canonical band."""
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence out of range [0, 1]: {confidence}")
    if confidence < BAND_MEDIUM_THRESHOLD:
        return ConfidenceBand.LOW
    if confidence < BAND_HIGH_THRESHOLD:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.HIGH

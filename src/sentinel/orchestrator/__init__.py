"""The incident orchestrator — the runtime-agnostic incident-graph driver (doc 04).

`run_incident` drives intake → specialists → synthesize → score → escalate →
act/page, wiring the escalation engine, confidence calibration, the reversibility
floor, and the credential-broker seam. Agents and the executor are injected
Protocols (`protocols.py`), so the loop is fully testable offline.
"""

from sentinel.orchestrator.protocols import (
    ActionExecutor,
    Commander,
    InvestigationContext,
    Planner,
    Specialist,
)
from sentinel.orchestrator.run import IncidentRun, run_incident

__all__ = [
    "ActionExecutor",
    "Commander",
    "IncidentRun",
    "InvestigationContext",
    "Planner",
    "Specialist",
    "run_incident",
]

"""Event-sourced incident projection (doc 02 §4, ADR-011).

The append-only ``incident_events`` log is the source of truth; the live
incident view is a pure fold over it (doc 02 §4.3, §4.4). Re-exports the event
vocabulary, the immutable event envelope, the projection shape, and the pure
``fold`` / ``project`` functions.
"""

from sentinel.incidents.events import IncidentEvent, IncidentEventType
from sentinel.incidents.projection import IncidentProjection, fold, project

__all__ = [
    "IncidentEvent",
    "IncidentEventType",
    "IncidentProjection",
    "fold",
    "project",
]

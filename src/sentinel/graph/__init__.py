"""The durable LangGraph incident runtime (doc 04 §2, §9).

`build_incident_graph` compiles the same control flow as
`sentinel.orchestrator.run_incident` into a real LangGraph `StateGraph` with a
checkpointer, adding the two properties the straight-line driver cannot have:
durable resume, and a genuine human-in-the-loop `interrupt()` at the T2 approval
gate. `initial_state` builds the start state; `IncidentGraphState` is the schema.
"""

from sentinel.graph.build import build_incident_graph
from sentinel.graph.state import IncidentGraphState, initial_state

__all__ = ["IncidentGraphState", "build_incident_graph", "initial_state"]

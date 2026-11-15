"""Per-role investigation tools (doc 04 §3: each specialist is confined to its domain).

Every tool is read-only and bound to exactly one evidence kind, so a specialist
can only ever see its own slice of the incident. That separation is what makes
specialists' agreement count as independent corroboration
(:func:`sentinel.confidence.independent_corroboration`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sentinel.contracts import SpecialistRole
from sentinel.orchestrator import InvestigationContext
from sentinel.tools.telemetry import EvidenceKind, Observation, TelemetrySource


def _filter_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "contains": {
                "type": "string",
                "description": "Optional case-insensitive text filter; omit to list everything.",
            }
        },
    }


@dataclass(frozen=True)
class Tool:
    """A named, read-only query over one evidence kind."""

    name: str
    description: str
    kind: EvidenceKind
    source: TelemetrySource
    input_schema: dict[str, object] = field(default_factory=_filter_schema)

    async def run(self, args: Mapping[str, object], ctx: InvestigationContext) -> list[Observation]:
        contains = args.get("contains")
        needle = contains.strip() if isinstance(contains, str) and contains.strip() else None
        return await self.source.fetch(self.kind, ctx, contains=needle)


# role → (tool name, evidence kind, description)
_ROLE_TOOLS: dict[SpecialistRole, tuple[str, EvidenceKind, str]] = {
    "log": (
        "search_logs",
        "log",
        "Search the incident window's application and edge logs.",
    ),
    "metric": (
        "query_metrics",
        "metric",
        "List metric anomalies (latency, saturation, pool usage, error rates) in the window.",
    ),
    "trace": (
        "search_traces",
        "trace",
        "Search distributed traces and span patterns for the affected requests.",
    ),
    "change_deploy": (
        "list_recent_deploys",
        "deploy",
        "List deploys, releases, and config changes around the incident window.",
    ),
    "error": (
        "list_error_groups",
        "error",
        "List grouped exceptions and error-rate changes from error tracking.",
    ),
}


def tools_for(role: SpecialistRole, source: TelemetrySource) -> list[Tool]:
    """The tools a specialist of ``role`` may call — only its own domain's."""
    name, kind, description = _ROLE_TOOLS[role]
    return [Tool(name=name, description=description, kind=kind, source=source)]

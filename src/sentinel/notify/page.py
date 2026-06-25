"""Render and deliver the incident page (doc 00 §6, doc 04 §8).

The page is the product's signature artifact: when Sentinel can't (or isn't yet
trusted to) act autonomously, it wakes a human with the investigation already
done — root cause, calibrated confidence, a drafted fix, the evidence chain, and
the rival hypotheses — so the founder approves in ~15 seconds instead of starting
from zero at 3 a.m.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from sentinel.contracts import PagePayload


def render_page(page: PagePayload) -> str:
    """Render a :class:`PagePayload` as a human-readable page (the §00.6 contract)."""
    a = page.confidence
    lines: list[str] = [
        f"🚨  Incident {page.incident_id} — needs you   (outcome: {page.outcome})",
        "",
        "Suspected root cause",
        f"    {page.suspected_root_cause}",
        "",
        (
            f"Confidence: {a.band.value}  "
            f"(calibrated {a.calibrated_confidence:.0%}, "
            f"{a.corroboration_count} corroborating, {a.n_competing} competing)"
        ),
    ]

    plan = page.drafted_remediation
    if plan is not None:
        reversibility = "reversible ✓" if plan.reversible else "IRREVERSIBLE ✗"
        details = []
        if plan.rollback_ref:
            details.append(f"rollback: {plan.rollback_ref}")
        details.append(f"blast radius: {plan.estimated_blast_radius}")
        lines += [
            "",
            f"Drafted fix  ({reversibility})",
            f"    {plan.action_type} → {plan.target}",
            f"    {plan.summary}",
            f"    ({' · '.join(details)})",
        ]

    if page.evidence_chain:
        lines += ["", "Evidence"]
        lines += [f"    • [{ev.kind}] {ev.summary}" for ev in page.evidence_chain]

    if page.competing_hypotheses:
        lines += ["", "Competing hypotheses"]
        lines += [
            f"    • {h.statement}  (raw {h.raw_likelihood:.0%})"
            for h in page.competing_hypotheses
        ]

    lines += [""]
    if plan is not None:
        lines.append("→ Approve in one tap to apply the fix, or open the incident to investigate.")
    else:
        lines.append("→ Open the incident to investigate.")
    return "\n".join(lines)


class Notifier(Protocol):
    """Delivers a page to a human (Slack / SMS / voice in production)."""

    async def page(self, payload: PagePayload) -> None: ...


class ConsoleNotifier:
    """The offline default: prints the rendered page to a sink (``print`` by default)."""

    def __init__(self, sink: Callable[[str], None] = print) -> None:
        self._sink = sink

    async def page(self, payload: PagePayload) -> None:
        self._sink(render_page(payload))


class CapturingNotifier:
    """A test/double Notifier that records every page it is handed."""

    def __init__(self) -> None:
        self.pages: list[PagePayload] = []
        self.rendered: list[str] = []

    async def page(self, payload: PagePayload) -> None:
        self.pages.append(payload)
        self.rendered.append(render_page(payload))

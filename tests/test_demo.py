"""The end-to-end demo runner (raw webhook → diagnosis → fix-or-page)."""

from __future__ import annotations

import asyncio

from sentinel.demo import run_demo
from sentinel.enums import IncidentState


def test_demo_runs_both_scenarios_end_to_end() -> None:
    lines: list[str] = []
    outcomes = asyncio.run(run_demo(sink=lines.append))

    assert outcomes["autonomous"] is IncidentState.CLOSED  # T3 acted and closed
    assert outcomes["page"] is IncidentState.ESCALATED  # T1 paged a human

    text = "\n".join(lines)
    assert "Approve in one tap" in text  # the Notifier card rendered the drafted fix
    assert "deploy.rollback" in text
    # C2 proven end to end: the DB password never appears anywhere in the output,
    # but the fact that it was redacted IS recorded in the meta.
    assert "S3cr3tP@ss" not in text
    assert "pg_password" in text

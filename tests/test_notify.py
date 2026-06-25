"""The Notifier — the 15-second-approval page (doc 00 §6, doc 04 §8)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sentinel.contracts import (
    ConfidenceAssessment,
    EvidenceRef,
    Hypothesis,
    PagePayload,
    RemediationPlan,
)
from sentinel.enums import ConfidenceBand
from sentinel.notify import CapturingNotifier, render_page

_T = datetime(2026, 6, 17, 3, 14, tzinfo=UTC)


def _page() -> PagePayload:
    return PagePayload(
        incident_id="inc_1",
        tenant_id="ten_a",
        suspected_root_cause="deploy abc123 exhausted the DB connection pool",
        confidence=ConfidenceAssessment(
            incident_id="inc_1",
            top_hypothesis_id="h1",
            calibrated_confidence=0.91,
            band=ConfidenceBand.HIGH,
            corroboration_count=3,
            n_competing=0,
            calibrator_version="cal.test",
        ),
        evidence_chain=[
            EvidenceRef(
                kind="deploy",
                summary="release abc123 shipped 40s before the spike",
                observed_at=_T,
            )
        ],
        competing_hypotheses=[
            Hypothesis(
                hypothesis_id="h2", statement="noisy neighbor on shared DB", raw_likelihood=0.4
            )
        ],
        drafted_remediation=RemediationPlan(
            incident_id="inc_1",
            action_type="deploy.rollback",
            target="svc_api",
            reversible=True,
            summary="roll back to the prior release abc122",
            rollback_ref="rel_abc122",
            estimated_blast_radius="single_service",
        ),
        outcome="page_one_tap",
    )


def test_render_page_includes_the_signature_fields() -> None:
    card = render_page(_page())
    assert "Incident inc_1" in card
    assert "deploy abc123 exhausted the DB connection pool" in card
    assert "HIGH" in card and "91%" in card
    assert "deploy.rollback → svc_api" in card
    assert "reversible ✓" in card
    assert "release abc123 shipped 40s before the spike" in card
    assert "noisy neighbor on shared DB" in card  # competing hypothesis attached
    assert "Approve in one tap" in card


def test_render_page_without_fix_prompts_investigation() -> None:
    page = _page().model_copy(update={"drafted_remediation": None})
    card = render_page(page)
    assert "Open the incident to investigate." in card
    assert "Approve in one tap" not in card


def test_capturing_notifier_records_pages() -> None:
    notifier = CapturingNotifier()
    asyncio.run(notifier.page(_page()))
    assert len(notifier.pages) == 1
    assert "deploy.rollback" in notifier.rendered[0]

"""Golden incident library + L1 eval harness (doc 06 §2, §3.1).

Deterministic, offline, no judge: replay each curated golden case and assert on typed
output — the smoking-gun specialist fires with a valid contract, every EvidenceRef is
redacted (C2), and the pure escalation policy returns the right verdict for every grant
tier (§2.2 ``expected_outcome_by_grant``). The corpus is stratified by stack archetype
(§2.1), including the deliberately thin ``uptime_led`` stratum.

The exhaustive escalation cube is already covered by ``test_escalation.py``; this layer
is the *golden-case-driven* half of L1.
"""

from __future__ import annotations

import pytest

from sentinel.contracts.agents import SpecialistFinding
from sentinel.escalation import PolicyInputs, decide_escalation
from sentinel.eval.golden import ARCHETYPES, GoldenCase, load_golden

GOLDEN: list[GoldenCase] = load_golden()
_IDS = [c.id for c in GOLDEN]


def test_corpus_nonempty_and_schema_valid() -> None:
    assert GOLDEN, "golden corpus is empty"
    for case in GOLDEN:
        assert case.findings, f"{case.id}: a golden case must carry findings"
        for f in case.findings:
            assert isinstance(f, SpecialistFinding)  # Pydantic-validated on load
            assert f.incident_id == case.incident_id  # loader stamps the case id


def test_corpus_is_stratified() -> None:
    """≥1 case per archetype (§2.1) — overfitting to one stack's signal shape is the risk."""
    covered = {c.archetype for c in GOLDEN}
    assert covered == set(ARCHETYPES), f"missing archetypes: {set(ARCHETYPES) - covered}"


@pytest.mark.parametrize("case", GOLDEN, ids=_IDS)
def test_smoking_gun_specialist_fires(case: GoldenCase) -> None:
    """The specialist the case names as decisive must carry a corroborating finding."""
    by_role = {f.role: f for f in case.findings}
    decisive = by_role[case.expected.smoking_gun_signal]
    assert decisive.status == "ok", f"{decisive.role} degraded on its own smoking gun"
    assert decisive.suspected_cause is not None
    assert decisive.evidence, "decisive specialist returned no EvidenceRef"


@pytest.mark.parametrize("case", GOLDEN, ids=_IDS)
def test_required_supporting_roles_present(case: GoldenCase) -> None:
    ok_roles = {f.role for f in case.findings if f.status == "ok"}
    missing = set(case.expected.required_supporting_roles) - ok_roles
    assert not missing, f"{case.id}: missing supporting roles {missing}"


@pytest.mark.parametrize("case", GOLDEN, ids=_IDS)
def test_all_evidence_is_redacted(case: GoldenCase) -> None:
    """Every cited EvidenceRef points at a redacted artifact — never a raw secret (C2/P3)."""
    for f in case.findings:
        for ev in f.evidence:
            assert ev.raw_ref is None or ev.raw_ref.startswith("obj://redacted/")


@pytest.mark.parametrize("case", GOLDEN, ids=_IDS)
def test_golden_expected_outcomes_per_tier(case: GoldenCase) -> None:
    """One replay tests the whole §00.6 matrix: assert the verdict for every grant tier."""
    band = case.expected.expected_band
    rev = case.expected.correct_remediation.reversible
    for tier, expected in case.expected.expected_outcome_by_grant.items():
        d = decide_escalation(PolicyInputs(case.incident_id, band, tier, rev))
        assert d.outcome == expected, f"{case.id}@{tier.value}: {d.outcome} != {expected}"

import pytest

from sentinel.enums import (
    BAND_HIGH_THRESHOLD,
    BAND_MEDIUM_THRESHOLD,
    ActionType,
    AutonomyTier,
    ConfidenceBand,
    IncidentState,
    band_for,
)


def test_enum_string_values_match_constitution() -> None:
    assert ActionType.DEPLOY_ROLLBACK.value == "deploy.rollback"
    assert ActionType.DB_FAILOVER.value == "db.failover"
    assert AutonomyTier.T0.value == "T0"
    assert AutonomyTier.T3.value == "T3"
    assert ConfidenceBand.HIGH.value == "HIGH"
    assert IncidentState.AWAITING_APPROVAL.value == "AWAITING_APPROVAL"


def test_band_thresholds_are_canonical() -> None:
    assert BAND_MEDIUM_THRESHOLD == 0.5
    assert BAND_HIGH_THRESHOLD == 0.8


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        (0.0, ConfidenceBand.LOW),
        (0.49, ConfidenceBand.LOW),
        (0.5, ConfidenceBand.MEDIUM),
        (0.79, ConfidenceBand.MEDIUM),
        (0.8, ConfidenceBand.HIGH),
        (1.0, ConfidenceBand.HIGH),
    ],
)
def test_band_for_boundaries(confidence: float, expected: ConfidenceBand) -> None:
    assert band_for(confidence) is expected


@pytest.mark.parametrize("bad", [-0.1, 1.1, 2.0])
def test_band_for_rejects_out_of_range(bad: float) -> None:
    with pytest.raises(ValueError):
        band_for(bad)

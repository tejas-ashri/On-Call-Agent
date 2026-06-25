import itertools

import pytest

from sentinel.enums import ActionType
from sentinel.safety import IRREVERSIBLE_ACTION_TYPES, classify_reversibility


def test_pinned_membership_is_exactly_what_doc_05_mandates() -> None:
    # Doc 05 §6: db.failover and config.set_env are hard-pinned irreversible
    # regardless of any LLM claim (high blast radius / shared resource).
    assert ActionType.DB_FAILOVER in IRREVERSIBLE_ACTION_TYPES
    assert ActionType.CONFIG_SET_ENV in IRREVERSIBLE_ACTION_TYPES


def test_pinned_type_with_planner_true_is_forced_false() -> None:
    # The independent floor overrides an over-optimistic Planner claim.
    assert classify_reversibility(ActionType.DB_FAILOVER, planner_reversible=True) is False
    assert classify_reversibility(ActionType.CONFIG_SET_ENV, planner_reversible=True) is False


def test_non_pinned_type_with_planner_true_stays_true() -> None:
    # deploy.rollback is inherently reversible (doc 04 §8); the floor agrees.
    assert classify_reversibility(ActionType.DEPLOY_ROLLBACK, planner_reversible=True) is True


def test_non_pinned_type_with_planner_false_stays_false() -> None:
    # The floor NEVER upgrades a Planner False into True.
    assert classify_reversibility(ActionType.DEPLOY_ROLLBACK, planner_reversible=False) is False


def test_pinned_type_with_planner_false_stays_false() -> None:
    assert classify_reversibility(ActionType.DB_FAILOVER, planner_reversible=False) is False


@pytest.mark.parametrize(
    ("action_type", "planner_reversible"),
    list(itertools.product(ActionType, [True, False])),
)
def test_floor_only_ever_downgrades_and_pins_hold(
    action_type: ActionType, planner_reversible: bool
) -> None:
    result = classify_reversibility(action_type, planner_reversible)

    # P2 applied to autonomy: the result is always logically <= the Planner's
    # claim — two independent assessments must AGREE before reversible is True.
    assert result <= planner_reversible

    # Hard-pinned types are False regardless of what the Planner claimed.
    if action_type in IRREVERSIBLE_ACTION_TYPES:
        assert result is False

    # Conversely, agreement is the only way to True: non-pinned + Planner True.
    expected = planner_reversible and action_type not in IRREVERSIBLE_ACTION_TYPES
    assert result is expected

"""The independent rule-based reversibility floor (doc 05 §6).

Doc 04 §7/§10 records an open risk: the escalation policy "trusts the Planner's
``reversible`` absolutely," so a single mislabeled irreversible action (e.g. a
"scale to 0" that drops in-flight state) would slip the irreversible hard stop
that C1 depends on. This module is that risk's floor — a second, deterministic
assessment, independent of any LLM, keyed purely on ``ActionType``.

Per doc 05 §6, this classifier "can only ever *downgrade* ``reversible``, never
upgrade it": two independent assessments (the Planner's claim AND this rule)
must AGREE an action is reversible before it runs autonomously — P2 (defense in
depth) applied to the autonomy decision. Action types with high blast radius or
that are not cleanly reversible are hard-pinned irreversible regardless of what
any LLM claims (doc 05 §6: "``db.failover`` or ``config.set_env`` against a
shared resource is hard-pinned irreversible regardless of what any LLM claims").
"""

from __future__ import annotations

from sentinel.enums import ActionType

#: Action types pinned irreversible regardless of any Planner/LLM claim (doc 05 §6).
#:
#: Rationale — each is high blast radius and/or not cleanly reversible:
#: - ``DB_FAILOVER``: promoting a replica is a one-way, stateful topology change
#:   (doc 03 marks it ``requires_agent=True``); "failing back" is a fresh,
#:   risky operation, not a clean undo.
#: - ``CONFIG_SET_ENV``: a shared-resource env mutation whose effect (restarts,
#:   in-flight requests reading the new value, downstream caches) cannot be
#:   guaranteed reversible by restoring the prior value.
#:
#: This is the floor beneath doc 05's allow-list, not the whole list: the set is
#: intentionally conservative and may only grow. Membership is asserted by tests.
IRREVERSIBLE_ACTION_TYPES: frozenset[ActionType] = frozenset(
    {
        ActionType.DB_FAILOVER,
        ActionType.CONFIG_SET_ENV,
    }
)


def classify_reversibility(action_type: ActionType, planner_reversible: bool) -> bool:
    """Return the floor-adjusted reversibility for ``action_type`` (doc 05 §6).

    The result is ``planner_reversible AND action_type not in
    IRREVERSIBLE_ACTION_TYPES``. Two consequences, both load-bearing for C1:

    - It NEVER turns a Planner ``False`` into ``True`` — an independent rule
      cannot manufacture autonomy the Planner withheld.
    - It FORCES ``False`` for hard-pinned types even when the Planner said
      ``True`` — the LLM cannot self-grant autonomy for ``db.failover`` /
      ``config.set_env``.

    Thus the output is always logically ``<=`` ``planner_reversible``: this is a
    downgrade-only floor, realizing "two independent assessments must agree it's
    reversible before it runs autonomously" (P2 applied to the autonomy
    decision). The boolean is fed verbatim to the §7 escalation policy in place
    of the raw Planner claim.
    """
    return planner_reversible and action_type not in IRREVERSIBLE_ACTION_TYPES

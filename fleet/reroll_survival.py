"""Pure opening-survival priorities for reroll battle policies."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from policy import UpgradeRule
from upgrades import target_reached


# Spend a bounded opening amount before returning to the existing economy plan.
_STARTER_TARGETS = (
    ("defense_absolute", 10),
    ("thorns", 11),
    ("damage", 12),
    ("attack_speed", 1.10),
    ("health", 20),
)


def prioritize_survival(
    rules: tuple[UpgradeRule, ...],
    observations: Mapping[str, Mapping[str, Any]],
) -> tuple[UpgradeRule, ...]:
    """Promote unfinished starters using observations from the current run only.

    The caller supplies already-unlocked rules and clears observations between
    runs. Reached starters retain their original target and ordering, so economy
    resumes before ordinary combat scaling. Unknown values retain the starter cap
    until OCR can establish completion; purchase eligibility stays with ``choose``.
    """
    by_id = {rule.upgrade_id: rule for rule in rules}
    starters: list[UpgradeRule] = []
    for upgrade_id, target in _STARTER_TARGETS:
        rule = by_id.get(upgrade_id)
        if rule is None or not rule.enabled:
            continue
        observation = observations.get(upgrade_id, {})
        if observation.get("status") == "maxed":
            continue
        value = observation.get("value")
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and target_reached(upgrade_id, value, target)):
            continue
        starters.append(replace(rule, target=target))
    promoted = {rule.upgrade_id for rule in starters}
    return (*starters, *(rule for rule in rules if rule.upgrade_id not in promoted))

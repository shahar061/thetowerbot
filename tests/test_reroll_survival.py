"""Bounded opening purchases for fresh reroll battles."""

from __future__ import annotations

import pytest

from fleet.reroll_survival import prioritize_survival
from policy import AutopilotPolicy, UpgradeRule, choose


RULES = (
    UpgradeRule("cash_per_wave", target=10),
    UpgradeRule("coins_per_kill_bonus", target=1.25),
    UpgradeRule("cash_bonus", target=1.25),
    UpgradeRule("defense_absolute"),
    UpgradeRule("thorns", target=51),
    UpgradeRule("health"),
    UpgradeRule("coins_per_wave", target=10),
    UpgradeRule("damage"),
    UpgradeRule("attack_speed"),
)
STARTERS = {
    "defense_absolute": 10,
    "thorns": 11,
    "damage": 12,
    "attack_speed": 1.10,
    "health": 20,
}


def observation(value: object, price: int = 5) -> dict[str, object]:
    return {"status": "available", "value": value, "price": price}


@pytest.mark.parametrize("upgrade_id,target", STARTERS.items())
def test_each_incomplete_starter_precedes_affordable_utilities(
    upgrade_id: str, target: float,
) -> None:
    observations = {key: observation(cap) for key, cap in STARTERS.items()}
    observations[upgrade_id] = observation(target / 2)
    observations["cash_per_wave"] = observation(1)
    rules = prioritize_survival(RULES, observations)
    assert rules[0] == UpgradeRule(upgrade_id, target=target)
    assert choose(AutopilotPolicy(enabled=True, rules=rules), observations,
                  {"cash": 20}).upgrade_id == upgrade_id


def test_economy_resumes_after_caps_then_original_scaling_continues() -> None:
    observations = {key: observation(cap) for key, cap in STARTERS.items()}
    observations["cash_per_wave"] = observation(1)
    rules = prioritize_survival(RULES, observations)
    assert rules == RULES
    policy = AutopilotPolicy(enabled=True, rules=rules)
    assert choose(policy, observations, {"cash": 20}).upgrade_id == "cash_per_wave"
    observations["cash_per_wave"] = observation(10)
    assert choose(policy, observations, {"cash": 20}).upgrade_id == "defense_absolute"


def test_expensive_starters_allow_affordable_economy_fallback() -> None:
    observations = {key: observation(0, price=100) for key in STARTERS}
    observations["cash_per_wave"] = observation(1)
    rules = prioritize_survival(RULES, observations)
    assert choose(AutopilotPolicy(enabled=True, rules=rules), observations,
                  {"cash": 20}).upgrade_id == "cash_per_wave"


def test_rules_remain_unique_and_missing_locked_defenses_are_not_added() -> None:
    available = tuple(rule for rule in RULES
                      if rule.upgrade_id not in {"defense_absolute", "thorns"})
    rules = prioritize_survival(available, {})
    ids = [rule.upgrade_id for rule in rules]
    assert ids[:3] == ["damage", "attack_speed", "health"]
    assert len(ids) == len(set(ids)) == len(available)
    assert set(ids) == {rule.upgrade_id for rule in available}


@pytest.mark.parametrize("value", [None, "100", True, float("nan"), float("inf")])
def test_unreadable_values_keep_capped_starter_rules(value: object) -> None:
    rules = prioritize_survival(RULES, {"damage": observation(value)})
    damage = next(rule for rule in rules if rule.upgrade_id == "damage")
    assert damage.target == 12


def test_maxed_starters_and_disabled_rules_are_not_promoted() -> None:
    original = (UpgradeRule("cash_per_wave", target=10),
                UpgradeRule("damage", enabled=False), UpgradeRule("health"))
    assert prioritize_survival(original, {"health": {"status": "maxed"}}) == original

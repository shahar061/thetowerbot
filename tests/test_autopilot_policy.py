"""Semantic upgrade catalog and pure autopilot decision policy."""

from __future__ import annotations

import dataclasses
import json

import pytest

from policy import AutopilotPolicy, PolicyError, UpgradeRule, choose, preset_rules
from strategy import ControlError, Shopping, ShoppingRule, Strategy
from upgrades import CATALOG, by_id, catalog_payload, resolve, target_reached


def observation(
    value: float,
    *,
    price: int = 10,
    status: str = "available",
) -> dict[str, object]:
    return {
        "value": value,
        "price": price,
        "status": status,
        "observed_at": "2026-09-04T10:00:00Z",
    }


def test_catalog_contains_every_standard_upgrade_and_known_unlock_tile() -> None:
    # Removing any category's late-game rows would make the editor claim an
    # account cannot plan an upgrade that the game actually shows.
    by_category = {
        category: [upgrade for upgrade in CATALOG if upgrade.category == category]
        for category in ("ATTACK", "DEFENSE", "UTILITY")
    }
    assert len(by_category["ATTACK"]) == 21
    assert len(by_category["DEFENSE"]) == 23
    assert len(by_category["UTILITY"]) == 16
    assert by_id("rend_armor_mult") is not None
    assert by_id("wall_rebuild") is not None
    assert by_id("enemy_health_level_skip") is not None
    assert {upgrade.id for upgrade in CATALOG if upgrade.unlock} == {
        "unlock_cash_bonuses",
        "unlock_bounce_shot",
        "unlock_coin_bonuses",
        "unlock_defense_upgrades",
        "unlock_free_upgrades",
        "unlock_knockback",
        "unlock_lifesteal",
        "unlock_multishot",
        "unlock_orbs",
        "unlock_range_upgrades",
        "unlock_rapid_fire",
        "unlock_thorns",
    }


@pytest.mark.parametrize(
    "text,category,want",
    [
        ("Defense %", None, "defense_percent"),
        ("defence percent", "DEFENSE", "defense_percent"),
        ("Def Abs", None, "defense_absolute"),
        ("Thorn Damage", None, "thorns"),
        ("Coins/Kill Bonus", None, "coins_per_kill_bonus"),
        ("Coins Per Wave", "utility", "coins_per_wave"),
        ("Damage/Meter", None, "damage_per_meter"),
        ("Unlock Coin Bonuses", None, "unlock_coin_bonuses"),
        ("Unlock Thorn Damage", "defense", "unlock_thorns"),
        ("Unlock Life Steal", None, "unlock_lifesteal"),
        ("Unlock Knock Back", None, "unlock_knockback"),
        ("Unlock Free Upgrade", None, "unlock_free_upgrades"),
        ("Unlock Multi Shot", None, "unlock_multishot"),
        ("Unlock Rapidfire", None, "unlock_rapid_fire"),
        ("Unlock Bounce Shot Upgrades", "ATTACK", "unlock_bounce_shot"),
    ],
)
def test_resolve_accepts_game_labels_guide_names_and_ocr_variants(
    text: str, category: str | None, want: str
) -> None:
    found = resolve(text, category)
    assert found is not None
    assert found.id == want


def test_resolve_rejects_an_unknown_or_wrong_category_name() -> None:
    assert resolve("Laser Bananas") is None
    assert resolve("Health", "ATTACK") is None


def test_sequential_cash_and_coin_unlocks_have_distinct_identities() -> None:
    cash = resolve("Unlock Cash Bonuses", "UTILITY")
    coins = resolve("Unlock Coin Bonuses", "UTILITY")
    assert cash is not None and coins is not None
    assert cash.id == "unlock_cash_bonuses"
    assert coins.id == "unlock_coin_bonuses"


@pytest.mark.parametrize(
    "unlock_id,children",
    [
        ("unlock_range_upgrades", ("range", "damage_per_meter")),
        ("unlock_multishot", ("multishot_chance", "multishot_targets")),
        ("unlock_rapid_fire", ("rapid_fire_chance", "rapid_fire_duration")),
        ("unlock_bounce_shot", ("bounce_shot_chance", "bounce_shot_targets", "bounce_shot_range")),
        ("unlock_defense_upgrades", ("defense_percent", "defense_absolute")),
        ("unlock_thorns", ("thorns",)),
        ("unlock_lifesteal", ("lifesteal",)),
        ("unlock_knockback", ("knockback_chance", "knockback_force")),
        ("unlock_orbs", ("orb_speed", "orbs")),
        ("unlock_cash_bonuses", ("cash_bonus", "cash_per_wave")),
        ("unlock_coin_bonuses", ("coins_per_kill_bonus", "coins_per_wave")),
        (
            "unlock_free_upgrades",
            ("free_attack_upgrade", "free_defense_upgrade", "free_utility_upgrade"),
        ),
    ],
)
def test_unlock_tiles_name_the_canonical_stats_they_reveal(
    unlock_id: str, children: tuple[str, ...]
) -> None:
    unlock = by_id(unlock_id)
    assert unlock is not None
    assert unlock.unlock is True
    assert unlock.unlocks == children
    assert all(by_id(child) is not None for child in children)
    positions = {upgrade.id: index for index, upgrade in enumerate(CATALOG)}
    assert all(positions[unlock_id] < positions[child] for child in children)


def test_catalog_payload_is_json_ready_and_does_not_expose_mutable_aliases() -> None:
    payload = catalog_payload()
    assert json.loads(json.dumps(payload))[0] == payload[0]
    assert isinstance(payload[0]["aliases"], list)
    assert isinstance(payload[0]["unlocks"], list)
    assert payload[0]["category"] == "ATTACK"


@pytest.mark.parametrize(
    "upgrade_id,value,target,want",
    [
        ("health", 100, 100, True),
        ("health", 99, 100, False),
        ("wall_rebuild", 100, 100, True),
        ("wall_rebuild", 101, 100, False),
        ("shockwave_frequency", 90, 100, True),
    ],
)
def test_target_reached_respects_each_stat_direction(
    upgrade_id: str, value: float, target: float, want: bool
) -> None:
    assert target_reached(upgrade_id, value, target) is want


def test_catalog_records_and_rules_are_frozen() -> None:
    damage = by_id("damage")
    assert damage is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        damage.name = "Oops"
    with pytest.raises(dataclasses.FrozenInstanceError):
        UpgradeRule("damage").enabled = False


@pytest.mark.parametrize(
    "raw,field",
    [
        ({"upgrade_id": "not_real"}, "upgrade_id"),
        ({"upgrade_id": "damage", "enabled": "yes"}, "enabled"),
        ({"upgrade_id": "damage", "target": True}, "target"),
        ({"upgrade_id": "damage", "target": -1}, "target"),
        ({"upgrade_id": "damage", "target": float("nan")}, "target"),
        ({"upgrade_id": "damage", "priority": 1}, "priority"),
    ],
)
def test_upgrade_rule_strictly_rejects_invalid_nested_json(
    raw: dict[str, object], field: str
) -> None:
    with pytest.raises(PolicyError) as caught:
        UpgradeRule.from_dict(raw)
    assert caught.value.field == field


def test_battle_rules_reject_workshop_unlock_tiles() -> None:
    with pytest.raises(PolicyError) as caught:
        UpgradeRule("unlock_cash_bonuses")
    assert caught.value.field == "upgrade_id"


@pytest.mark.parametrize(
    "raw,field",
    [
        ({"rules": None}, "rules"),
        ({"rules": ["damage"]}, "rules"),
        ({"rules": [{"upgrade_id": "damage"}, {"upgrade_id": "damage"}]}, "rules"),
        ({"enabled": 1}, "enabled"),
        ({"preset": "aggressive"}, "preset"),
        ({"purpose": "speedrun"}, "purpose"),
        ({"economy_until_wave": True}, "economy_until_wave"),
        ({"survival_buffer": 0}, "survival_buffer"),
        ({"survival_buffer": float("inf")}, "survival_buffer"),
        ({"cash_reserve": -1}, "cash_reserve"),
        ({"max_scrolls": -1}, "max_scrolls"),
        ({"mystery": 3}, "mystery"),
    ],
)
def test_policy_strictly_rejects_invalid_json(
    raw: dict[str, object], field: str
) -> None:
    with pytest.raises(PolicyError) as caught:
        AutopilotPolicy.from_dict(raw)
    assert caught.value.field == field


def test_policy_round_trip_preserves_rules_targets_and_run_purpose() -> None:
    original = AutopilotPolicy(
        enabled=True,
        preset="manual",
        purpose="milestone",
        rules=(
            UpgradeRule("health", target=500),
            UpgradeRule("cash_bonus", enabled=False),
        ),
        economy_until_wave=30,
        survival_buffer=1.5,
        cash_reserve=75,
        max_scrolls=4,
    )
    assert AutopilotPolicy.from_dict(original.to_dict()) == original
    assert json.loads(json.dumps(original.to_dict()))["purpose"] == "milestone"


def test_strategy_round_trip_preserves_autopilot_and_workshop_spending_limits() -> None:
    original = Strategy.from_config().merged(
        {
            "autopilot": {
                "enabled": True,
                "preset": "manual",
                "purpose": "milestone",
                "rules": [{"upgrade_id": "health", "target": 500}],
            },
            "shopping": {
                "enabled": True,
                "armed": True,
                "coin_reserve": 1000,
                "coin_budget": 250,
                "allow_unlocks": True,
                "workshop": [
                    {
                        "name": "Health",
                        "category": "DEFENSE",
                        "enabled": True,
                        "target": 75,
                    }
                ],
            },
        }
    )
    restored = Strategy.from_dict(original.to_dict())
    assert restored == original
    assert restored.autopilot.rules[0].upgrade_id == "health"
    assert restored.shopping.workshop[0].target == 75


def test_old_profile_defaults_to_disabled_autopilot_and_no_workshop_coin_budget() -> None:
    raw = Strategy.from_config().to_dict()
    del raw["autopilot"]
    for field in ("coin_reserve", "coin_budget", "allow_unlocks"):
        del raw["shopping"][field]
    for row in raw["shopping"]["workshop"]:
        row.pop("target", None)

    restored = Strategy.from_dict(raw)

    assert restored.autopilot == AutopilotPolicy()
    assert restored.shopping.coin_reserve == 0
    assert restored.shopping.coin_budget == 0
    assert restored.shopping.allow_unlocks is False


def test_strategy_rejects_an_unknown_nested_autopilot_field() -> None:
    raw = Strategy.from_config().to_dict()
    raw["autopilot"]["guess_when_uncertain"] = True
    with pytest.raises(ControlError) as caught:
        Strategy.from_dict(raw)
    assert caught.value.field == "guess_when_uncertain"


@pytest.mark.parametrize(
    "raw,field",
    [
        ({"coin_reserve": -1}, "coin_reserve"),
        ({"coin_budget": -1}, "coin_budget"),
        ({"allow_unlocks": "yes"}, "allow_unlocks"),
        (
            {
                "workshop": [
                    {"name": "Health", "category": "DEFENSE", "target": -1}
                ]
            },
            "target",
        ),
        (
            {
                "workshop": [
                    {
                        "name": "Health",
                        "category": "DEFENSE",
                        "target": float("inf"),
                    }
                ]
            },
            "target",
        ),
    ],
)
def test_shopping_strictly_rejects_invalid_spending_controls(
    raw: dict[str, object], field: str
) -> None:
    with pytest.raises(ControlError) as caught:
        Shopping.from_dict(raw)
    assert caught.value.field == field


def test_presets_are_returned_as_independent_immutable_rule_lists() -> None:
    assert preset_rules("manual") == ()
    assert preset_rules("turtle")[0].upgrade_id == "cash_per_wave"
    assert {rule.upgrade_id for rule in preset_rules("health")} >= {
        "health",
        "defense_percent",
        "lifesteal",
        "attack_speed",
        "knockback_chance",
        "orbs",
    }
    with pytest.raises(PolicyError):
        preset_rules("unknown")


def test_disabled_policy_never_authorizes_a_purchase() -> None:
    decision = choose(
        AutopilotPolicy(rules=(UpgradeRule("damage"),)),
        {"damage": observation(1)},
        {},
    )
    assert decision.phase == "disabled"
    assert decision.upgrade_id is None


def test_manual_policy_chooses_the_first_enabled_available_rule() -> None:
    policy = AutopilotPolicy(
        enabled=True,
        rules=(
            UpgradeRule("cash_bonus", enabled=False),
            UpgradeRule("health"),
            UpgradeRule("damage"),
        ),
    )
    decision = choose(
        policy,
        {"health": observation(20), "damage": observation(4)},
        {},
    )
    assert decision.phase == "manual"
    assert decision.upgrade_id == "health"


def test_target_completion_skips_that_rule_and_chooses_the_next_one() -> None:
    policy = AutopilotPolicy(
        enabled=True,
        rules=(UpgradeRule("health", target=100), UpgradeRule("damage")),
    )
    decision = choose(
        policy,
        {"health": observation(100), "damage": observation(8)},
        {},
    )
    assert decision.upgrade_id == "damage"


@pytest.mark.parametrize("upgrade_id", ["wall_rebuild", "shockwave_frequency"])
def test_decreasing_stats_buy_above_target_and_complete_at_target(
    upgrade_id: str,
) -> None:
    policy = AutopilotPolicy(
        enabled=True,
        rules=(UpgradeRule(upgrade_id, target=100),),
    )
    assert choose(policy, {upgrade_id: observation(120)}, {}).upgrade_id == upgrade_id
    complete = choose(policy, {upgrade_id: observation(100)}, {})
    assert complete.phase == "complete"
    assert complete.upgrade_id is None


def test_completed_or_unavailable_rules_never_authorize_spending() -> None:
    policy = AutopilotPolicy(
        enabled=True,
        rules=(UpgradeRule("health", target=100),),
    )
    decision = choose(policy, {"health": observation(50, status="locked")}, {})
    assert decision.phase == "wait"
    assert decision.upgrade_id is None


@pytest.mark.parametrize("status", ["affordable", "unknown", "unreadable", "locked", "maxed"])
def test_only_the_available_status_can_authorize_spending(status: str) -> None:
    policy = AutopilotPolicy(enabled=True, rules=(UpgradeRule("damage"),))
    decision = choose(policy, {"damage": observation(1, status=status)}, {})
    assert decision.upgrade_id is None


def test_an_available_row_with_an_unreadable_price_cannot_authorize_spending() -> None:
    policy = AutopilotPolicy(enabled=True, rules=(UpgradeRule("damage"),))
    row = observation(1)
    row["price"] = None
    decision = choose(policy, {"damage": row}, {})
    assert decision.upgrade_id is None


def test_an_available_row_with_an_unreadable_value_cannot_authorize_spending() -> None:
    policy = AutopilotPolicy(enabled=True, rules=(UpgradeRule("damage"),))
    row = observation(1)
    row["value"] = None
    decision = choose(policy, {"damage": row}, {})
    assert decision.upgrade_id is None


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_an_available_row_with_a_non_finite_value_cannot_authorize_spending(
    value: float,
) -> None:
    policy = AutopilotPolicy(enabled=True, rules=(UpgradeRule("damage"),))
    decision = choose(policy, {"damage": observation(value)}, {})
    assert decision.upgrade_id is None


def test_cash_reserve_blocks_a_purchase_that_would_cross_the_floor() -> None:
    policy = AutopilotPolicy(
        enabled=True,
        rules=(UpgradeRule("damage"),),
        cash_reserve=25,
    )
    decision = choose(
        policy,
        {"damage": observation(1, price=80)},
        {"cash": 100},
    )
    assert decision.phase == "wait"
    assert decision.upgrade_id is None


def test_cash_reserve_waits_when_cash_is_unknown() -> None:
    policy = AutopilotPolicy(
        enabled=True,
        rules=(UpgradeRule("damage"),),
        cash_reserve=25,
    )
    decision = choose(policy, {"damage": observation(1, price=10)}, {})
    assert decision.upgrade_id is None


def test_turtle_waits_when_required_combat_or_defense_values_are_unknown() -> None:
    policy = AutopilotPolicy(enabled=True, preset="turtle")
    decision = choose(
        policy,
        {"cash_per_wave": observation(2), "defense_percent": observation(50)},
        {"wave": 5, "enemy_damage": 100},
    )
    assert decision.phase == "wait"
    assert decision.upgrade_id is None
    assert "Defense Absolute" in decision.reason


def test_turtle_survival_buffer_interrupts_early_economy() -> None:
    policy = AutopilotPolicy(enabled=True, preset="turtle", survival_buffer=1.2)
    observations = {
        "cash_per_wave": observation(2),
        "cash_bonus": observation(1.1),
        "defense_percent": observation(50),
        "defense_absolute": observation(20),
    }
    decision = choose(
        policy,
        observations,
        {"wave": 5, "enemy_damage": 100},
    )
    assert decision.phase == "survival"
    assert decision.upgrade_id == "defense_absolute"


def test_turtle_keeps_building_economy_when_the_buffer_is_safe() -> None:
    policy = AutopilotPolicy(enabled=True, preset="turtle", survival_buffer=1.2)
    observations = {
        "cash_per_wave": observation(2),
        "defense_percent": observation(50),
        "defense_absolute": observation(75),
    }
    decision = choose(
        policy,
        observations,
        {"wave": 5, "enemy_damage": 100},
    )
    assert decision.phase == "economy"
    assert decision.upgrade_id == "cash_per_wave"


def test_turtle_yields_to_health_after_reaching_the_current_thorns_breakpoint() -> None:
    policy = AutopilotPolicy(enabled=True, preset="turtle", economy_until_wave=20)
    observations = {
        "cash_bonus": observation(1.1),
        "defense_percent": observation(50),
        "defense_absolute": observation(75),
        "thorns": observation(11),
        "health": observation(100),
    }
    decision = choose(
        policy,
        observations,
        {"wave": 21, "enemy_damage": 100},
    )
    assert decision.phase == "survival"
    assert decision.upgrade_id == "health"


@pytest.mark.parametrize(
    "wave,current_thorns,want_breakpoint",
    [(21, 10, 11), (41, 11, 21), (81, 21, 34), (161, 34, 51)],
)
def test_turtle_advances_thorns_at_staged_breakpoints(
    wave: int, current_thorns: int, want_breakpoint: int
) -> None:
    policy = AutopilotPolicy(enabled=True, preset="turtle", economy_until_wave=20)
    observations = {
        "defense_percent": observation(50),
        "defense_absolute": observation(75),
        "thorns": observation(current_thorns),
        "health": observation(100),
    }
    decision = choose(
        policy,
        observations,
        {"wave": wave, "enemy_damage": 100},
    )
    assert decision.phase == "survival"
    assert decision.upgrade_id == "thorns"
    assert str(want_breakpoint) in decision.reason


def test_health_guide_waits_without_a_health_ratio() -> None:
    policy = AutopilotPolicy(enabled=True, preset="health")
    decision = choose(policy, {"cash_bonus": observation(1.1)}, {"wave": 5})
    assert decision.phase == "wait"
    assert decision.upgrade_id is None


def test_health_guide_interrupts_economy_when_tower_health_is_low() -> None:
    policy = AutopilotPolicy(enabled=True, preset="health")
    observations = {
        "cash_bonus": observation(1.1),
        "health": observation(100),
        "defense_percent": observation(40),
    }
    decision = choose(
        policy,
        observations,
        {"wave": 5, "health": 30, "max_health": 100},
    )
    assert decision.phase == "survival"
    assert decision.upgrade_id == "health"


def test_health_guide_switches_from_economy_after_the_configured_wave() -> None:
    policy = AutopilotPolicy(enabled=True, preset="health", economy_until_wave=20)
    observations = {
        "cash_bonus": observation(1.1),
        "health": observation(100),
        "defense_percent": observation(40),
    }
    decision = choose(
        policy,
        observations,
        {"wave": 21, "health": 100, "max_health": 100},
    )
    assert decision.phase == "survival"
    assert decision.upgrade_id == "health"

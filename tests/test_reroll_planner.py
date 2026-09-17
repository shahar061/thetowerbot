"""Reroll decisions use verified account progress, not elapsed time."""

from __future__ import annotations

from fleet.reroll_planner import RerollFacts, choose_next


def facts(**changes: object) -> RerollFacts:
    values: dict[str, object] = dict(account_id="ACCOUNT-A", best_tier_1_wave=1,
                                    purchases={}, values={}, wallet_coins=None,
                                    lifetime_coins=None, prices={})
    values.update(changes)
    return RerollFacts(**values)


def test_early_plan_rotates_from_damage_to_attack_speed() -> None:
    first = choose_next(facts())
    assert first.stage == "opening"
    assert first.upgrade_id == "damage"
    assert first.state == "observe_price"
    second = choose_next(facts(purchases={"damage": 1}))
    assert second.upgrade_id == "attack_speed"


def test_guide_avoids_range_and_prioritizes_turtle_after_wave_20() -> None:
    decision = choose_next(facts(best_tier_1_wave=20))
    assert decision.stage == "turtle"
    assert decision.upgrade_id == "unlock_defense_upgrades"
    assert decision.upgrade_id != "unlock_range_upgrades"
    next_decision = choose_next(facts(best_tier_1_wave=20, purchases={
        "unlock_defense_upgrades": 1, "unlock_thorns": 1}))
    assert next_decision.upgrade_id in {"defense_absolute", "thorns"}


def test_verified_purchase_moves_the_plan_and_unverified_does_not() -> None:
    baseline = choose_next(facts())
    assert choose_next(facts(purchases={})).upgrade_id == baseline.upgrade_id
    assert choose_next(facts(purchases={"damage": 1})).upgrade_id != baseline.upgrade_id


def test_utility_unlocks_follow_the_visible_workshop_order() -> None:
    purchases = {"damage": 1, "attack_speed": 1}
    assert choose_next(facts(purchases=purchases)).upgrade_id == "unlock_cash_bonuses"
    assert choose_next(facts(purchases={**purchases, "unlock_cash_bonuses": 1})).upgrade_id == "unlock_coin_bonuses"


def test_price_and_balance_decide_buy_or_save() -> None:
    inputs = dict(prices={"damage": 120}, lifetime_coins=200)
    saving = choose_next(facts(wallet_coins=80, **inputs))
    assert saving.state == "save_coins" and saving.price == 120
    assert saving.wallet_coins == 80 and saving.lifetime_coins == 200
    buying = choose_next(facts(wallet_coins=120, **inputs))
    assert buying.state == "buy" and buying.price == 120
    assert "60%" in buying.reason


def test_unknown_wallet_does_not_authorize_purchase() -> None:
    decision = choose_next(facts(prices={"damage": 20}))
    assert decision.state == "observe_balance"


def test_purchase_above_strategy_spend_limit_stays_in_save_state() -> None:
    decision = choose_next(facts(wallet_coins=120, prices={"damage": 100},
                                 spend_fraction=.5))
    assert decision.state == "save_coins"
    assert "80 more coins" in decision.reason


def test_wave_60_stops_workshop_planning_for_stones() -> None:
    decision = choose_next(facts(best_tier_1_wave=60, wallet_coins=1000))
    assert decision.stage == "stones"
    assert decision.upgrade_id is None
    assert decision.state == "needs_operator"


def test_decisions_remain_bound_to_the_input_account() -> None:
    a = choose_next(facts(account_id="ACCOUNT-A", purchases={"damage": 2}))
    b = choose_next(facts(account_id="ACCOUNT-B"))
    assert a.account_id == "ACCOUNT-A" and b.account_id == "ACCOUNT-B"
    assert a.upgrade_id != b.upgrade_id

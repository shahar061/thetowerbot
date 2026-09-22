"""Reroll decisions use verified account progress, not elapsed time."""

from __future__ import annotations

from fleet.reroll_planner import RerollFacts, choose_next, project_next


def facts(**changes: object) -> RerollFacts:
    values: dict[str, object] = dict(account_id="ACCOUNT-A", best_tier_1_wave=1,
                                    purchases={}, values={}, wallet_coins=None,
                                    lifetime_coins=None, prices={})
    values.update(changes)
    return RerollFacts(**values)


def test_early_plan_leads_with_the_unlock_the_whole_build_depends_on() -> None:
    """The head of the order, after value propagation.

    This test used to read `first.upgrade_id == "damage"` and, after one
    Damage, `second.upgrade_id == "attack_speed"` - the top two rows of the
    old `weight / (1 + purchases)` rotation, where Damage (12) beat
    Unlock Defense Upgrades (10.5) on its own declared weight.

    It no longer does, and the change is the feature rather than a
    regression: `value_propagation` gives an enabler a discounted share of
    the best thing it leads to, so Unlock Defense Upgrades is worth
    10.5 + 0.6 * (9 + 0.6 * 12) = 20.22 - the thorns chain it opens - and is
    bought first. Buying it then unblocks Unlock Thorns (16.2), which is
    what the second assertion now pins.
    """
    first = choose_next(facts())
    assert first.stage == "opening"
    assert first.upgrade_id == "unlock_defense_upgrades"
    assert first.state == "observe_price"
    second = choose_next(facts(purchases={"unlock_defense_upgrades": 1}))
    assert second.upgrade_id == "unlock_thorns"


def test_guide_avoids_range_and_prioritizes_turtle_after_wave_20() -> None:
    decision = choose_next(facts(best_tier_1_wave=20))
    assert decision.stage == "turtle"
    assert decision.upgrade_id == "unlock_defense_upgrades"
    assert decision.upgrade_id != "unlock_range_upgrades"
    next_decision = choose_next(facts(best_tier_1_wave=20, purchases={
        "unlock_defense_upgrades": 1, "unlock_thorns": 1}))
    assert next_decision.upgrade_id in {"defense_absolute", "thorns"}


def test_verified_purchase_moves_the_plan_and_unverified_does_not() -> None:
    """Only a ledger-verified purchase moves the plan on.

    The moving purchase was `{"damage": 1}`, which moved the old rotation
    because every buy divided that row's weight. A milestone is met or not,
    so the purchase that moves the plan is now a purchase of the row the
    plan actually recommended - which is the stronger statement anyway: the
    planner does not repeat a recommendation the account has carried out.
    """
    baseline = choose_next(facts())
    assert choose_next(facts(purchases={})).upgrade_id == baseline.upgrade_id
    assert choose_next(
        facts(purchases={baseline.upgrade_id: 1})).upgrade_id != baseline.upgrade_id


def test_utility_unlocks_follow_the_visible_workshop_order() -> None:
    """Economy unlocks come after the defense chain, in tab order.

    The account is the same one this test always described - the defense
    chain bought, the economy chain untouched - with one reading added:
    `values={"thorns": 51.}`. The old rotation reached the economy unlocks
    because five Thorns purchases had divided that row's weight down to 2.4;
    the pipeline stops buying Thorns when it reaches the value the build
    names, which is what `builds.Build.targets` means, so the account has to
    have reached it for the same question to be asked.
    """
    purchases = {"damage": 3, "attack_speed": 3,
                 "unlock_defense_upgrades": 1, "defense_absolute": 5,
                 "unlock_thorns": 1, "thorns": 4}
    values = {"thorns": 51.}
    assert choose_next(facts(purchases=purchases,
                             values=values)).upgrade_id == "unlock_cash_bonuses"
    assert choose_next(facts(values=values, purchases={
        **purchases, "unlock_cash_bonuses": 1,
        "cash_bonus": 1})).upgrade_id == "unlock_coin_bonuses"


def test_price_and_balance_decide_buy_or_save() -> None:
    """The money states, priced on whichever row the plan actually picked.

    `prices={"damage": 120}` became `{"unlock_defense_upgrades": 120}`:
    `fleet/reroll_progress.py` only ever prices the upgrade a first, unpriced
    `choose_next` named, so the key in this fixture means "the price of the
    chosen row", and the chosen row moved. Nothing else here changed.
    """
    inputs = dict(prices={"unlock_defense_upgrades": 120}, lifetime_coins=200)
    saving = choose_next(facts(wallet_coins=80, **inputs))
    assert saving.state == "save_coins" and saving.price == 120
    assert saving.wallet_coins == 80 and saving.lifetime_coins == 200
    buying = choose_next(facts(wallet_coins=120, **inputs))
    assert buying.state == "buy" and buying.price == 120
    assert "60%" in buying.reason


def test_unknown_wallet_does_not_authorize_purchase() -> None:
    # Priced row follows the plan's pick; see test_price_and_balance_decide_buy_or_save.
    decision = choose_next(facts(prices={"unlock_defense_upgrades": 20}))
    assert decision.state == "observe_balance"


def test_purchase_above_strategy_spend_limit_stays_in_save_state() -> None:
    # Priced row follows the plan's pick; see test_price_and_balance_decide_buy_or_save.
    decision = choose_next(facts(wallet_coins=120,
                                 prices={"unlock_defense_upgrades": 100},
                                 spend_fraction=.5))
    assert decision.state == "save_coins"
    assert "80 more coins" in decision.reason


def test_wave_60_stops_workshop_planning_for_stones() -> None:
    decision = choose_next(facts(best_tier_1_wave=60, wallet_coins=1000))
    assert decision.stage == "stones"
    assert decision.upgrade_id is None
    assert decision.state == "needs_operator"


def test_decisions_remain_bound_to_the_input_account() -> None:
    # Was `purchases={"damage": 2}`, which separated the two accounts only
    # through the old rotation's per-purchase weight division. A purchase of
    # the row the plan recommends separates them under either ranking.
    a = choose_next(facts(account_id="ACCOUNT-A",
                          purchases={"unlock_defense_upgrades": 1}))
    b = choose_next(facts(account_id="ACCOUNT-B"))
    assert a.account_id == "ACCOUNT-A" and b.account_id == "ACCOUNT-B"
    assert a.upgrade_id != b.upgrade_id


def test_ten_buy_preview_brings_turtle_unlocks_before_more_economy() -> None:
    preview = project_next(facts(), limit=10)
    ids = [step.upgrade_id for step in preview]
    assert len(ids) == 10
    # Was `["damage", "attack_speed"]`, the head of the old rotation. The
    # preview now opens on the chain those two used to delay - see
    # test_early_plan_leads_with_the_unlock_the_whole_build_depends_on.
    assert ids[:2] == ["unlock_defense_upgrades", "unlock_thorns"]
    assert ids.index("unlock_defense_upgrades") < ids.index("defense_absolute")
    assert ids.index("unlock_thorns") < ids.index("thorns")
    assert ids.index("unlock_defense_upgrades") < ids.index("unlock_cash_bonuses")
    assert [step.position for step in preview] == list(range(1, 11))
    assert all(step.account_id == "ACCOUNT-A" for step in preview)


def test_current_early_plan_prioritizes_defense_after_basic_attack() -> None:
    decision = choose_next(facts(purchases={"damage": 1, "attack_speed": 1}))
    assert decision.upgrade_id == "unlock_defense_upgrades"

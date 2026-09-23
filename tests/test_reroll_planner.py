"""Reroll decisions use verified account progress, not elapsed time."""

from __future__ import annotations

from collections import Counter

import pytest

from fleet.reroll_planner import DRAW_SHARPNESS, RerollFacts, choose_next, project_next


def facts(**changes: object) -> RerollFacts:
    # draw_sharpness=None: these tests pin the ranked order itself. The draw
    # on top of it has its own tests at the bottom of this file.
    values: dict[str, object] = dict(account_id="ACCOUNT-A", best_tier_1_wave=1,
                                    purchases={}, values={}, wallet_coins=None,
                                    lifetime_coins=None, prices={},
                                    draw_sharpness=None)
    values.update(changes)
    return RerollFacts(**values)


def test_early_plan_leads_with_basic_attack_before_the_unlocks() -> None:
    """Damage (100) and Attack Speed (90) sit above every unlock, including
    the 81.6 `value_propagation` credits Unlock Cash Bonuses with, so a fresh
    account buys attack first - two levels of Damage, then Attack Speed."""
    first = choose_next(facts())
    assert first.stage == "opening"
    assert first.upgrade_id == "damage"
    assert first.state == "observe_price"
    second = choose_next(facts(purchases={"damage": 2}))
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
    """Only a ledger-verified purchase moves the plan on.

    The moving purchase was `{"damage": 1}`, which moved the old rotation
    because every buy divided that row's weight. A milestone is met or not,
    so the purchase that moves the plan is now a purchase of the row the
    plan actually recommended - which is the stronger statement anyway: the
    planner does not repeat a recommendation the account has carried out.
    """
    baseline = choose_next(facts())
    assert choose_next(facts(purchases={})).upgrade_id == baseline.upgrade_id
    # Two, because the opening lets Damage reach level 2 before any Coins/Wave.
    assert choose_next(
        facts(purchases={baseline.upgrade_id: 2})).upgrade_id != baseline.upgrade_id


def test_utility_unlocks_follow_the_visible_workshop_order() -> None:
    """Economy unlocks are bought in tab order: Cash Bonuses, then Coin Bonuses.

    Attack is past its allowance (3 of 2 with no Coins/Wave), so the Coins
    group is next whatever the defense chain holds.
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

    `fleet/reroll_progress.py` only ever prices the upgrade a first, unpriced
    `choose_next` named, so the key in this fixture means "the price of the
    chosen row".
    """
    inputs = dict(prices={"damage": 120}, lifetime_coins=200)
    saving = choose_next(facts(wallet_coins=80, **inputs))
    assert saving.state == "save_coins" and saving.price == 120
    assert saving.wallet_coins == 80 and saving.lifetime_coins == 200
    buying = choose_next(facts(wallet_coins=120, **inputs))
    assert buying.state == "buy" and buying.price == 120
    assert "60%" in buying.reason


def test_unknown_wallet_does_not_authorize_purchase() -> None:
    # Priced row follows the plan's pick; see test_price_and_balance_decide_buy_or_save.
    decision = choose_next(facts(prices={"damage": 20}))
    assert decision.state == "observe_balance"


def test_purchase_above_strategy_spend_limit_stays_in_save_state() -> None:
    # Priced row follows the plan's pick; see test_price_and_balance_decide_buy_or_save.
    decision = choose_next(facts(wallet_coins=120,
                                 prices={"damage": 100},
                                 spend_fraction=.5))
    assert decision.state == "save_coins"
    assert "80 more coins" in decision.reason


def test_wave_60_stops_workshop_planning_for_stones() -> None:
    decision = choose_next(facts(best_tier_1_wave=60, wallet_coins=1000))
    assert decision.stage == "stones"
    assert decision.upgrade_id is None
    assert decision.state == "needs_operator"


def test_decisions_remain_bound_to_the_input_account() -> None:
    # A purchase of the row the plan recommends separates the two accounts.
    a = choose_next(facts(account_id="ACCOUNT-A",
                          purchases={"damage": 2}))
    b = choose_next(facts(account_id="ACCOUNT-B"))
    assert a.account_id == "ACCOUNT-A" and b.account_id == "ACCOUNT-B"
    assert a.upgrade_id != b.upgrade_id


# The whole opening, as ranked (no draw): attack to its allowance of
# 2 + 1 x Coins/Wave, the coin unlocks, then each Coins/Wave level buys one
# more level of each attack row, until Coins/Wave stops at 3. Then the Thorns
# chain with two levels of Defense Absolute; Thorns runs to 51 as one step.
_OPENING_ORDER = [
    "damage", "damage", "attack_speed", "attack_speed",
    "unlock_cash_bonuses", "unlock_coin_bonuses", "coins_per_wave",
    "damage", "attack_speed", "coins_per_wave",
    "damage", "attack_speed", "coins_per_wave",
    "damage", "attack_speed",
    "unlock_defense_upgrades", "unlock_thorns",
    "defense_absolute", "defense_absolute", "thorns",
]


def test_the_opening_projects_attack_then_coins_then_thorns() -> None:
    preview = project_next(facts(), limit=40)
    assert [step.upgrade_id for step in preview] == _OPENING_ORDER
    assert [step.position for step in preview] == list(range(1, len(_OPENING_ORDER) + 1))
    assert all(step.account_id == "ACCOUNT-A" for step in preview)


def test_attack_waits_for_coins_per_wave_once_it_reaches_its_allowance() -> None:
    at_cap = {"damage": 2, "attack_speed": 2}
    assert choose_next(facts(purchases=at_cap)).upgrade_id == "unlock_cash_bonuses"
    one_level = {**at_cap, "unlock_cash_bonuses": 1, "unlock_coin_bonuses": 1,
                 "coins_per_wave": 1}
    assert choose_next(facts(purchases=one_level)).upgrade_id == "damage"


def test_the_thorns_chain_waits_until_coins_per_wave_reaches_three() -> None:
    bought = {"damage": 5, "attack_speed": 5, "unlock_cash_bonuses": 1,
              "unlock_coin_bonuses": 1, "coins_per_wave": 2}
    assert choose_next(facts(purchases=bought)).upgrade_id == "coins_per_wave"
    bought["coins_per_wave"] = 3
    assert choose_next(facts(purchases=bought)).upgrade_id == "unlock_defense_upgrades"


# -- the draw ----------------------------------------------------------------

def test_the_draw_is_reproducible_for_one_account_and_purchase_count() -> None:
    drawn = facts(draw_sharpness=DRAW_SHARPNESS)
    assert len({choose_next(drawn).upgrade_id for _ in range(5)}) == 1


def test_the_draw_mostly_keeps_the_top_pick_but_not_always() -> None:
    """Fresh accounts: Damage 100, Attack Speed 90, then Unlock Cash Bonuses
    at 81.6 - at sharpness 6 that is about 55%, 29% and 16%."""
    picks = Counter(
        choose_next(facts(account_id=f"ACCOUNT-{n}",
                          draw_sharpness=DRAW_SHARPNESS)).upgrade_id
        for n in range(600))
    assert set(picks) == {"damage", "attack_speed", "unlock_cash_bonuses"}
    assert picks["damage"] > picks["attack_speed"] > 0
    assert 0.40 < picks["damage"] / 600 < 0.62


def test_the_draw_never_picks_a_row_that_is_blocked_or_past_its_cap() -> None:
    at_cap = {"damage": 2, "attack_speed": 2}
    for n in range(200):
        picked = choose_next(facts(account_id=f"ACCOUNT-{n}", purchases=at_cap,
                                   draw_sharpness=DRAW_SHARPNESS)).upgrade_id
        # Coins/Wave, Defense Absolute, Unlock Thorns and Thorns are all
        # behind unlocks nobody has bought; attack is at its allowance.
        assert picked in {"unlock_cash_bonuses", "unlock_defense_upgrades"}


def test_a_drawn_pick_says_so_in_its_reason() -> None:
    reasons = [choose_next(facts(account_id=f"ACCOUNT-{n}",
                                 draw_sharpness=DRAW_SHARPNESS))
               for n in range(100)]
    off_top = [d for d in reasons if d.upgrade_id != "damage"]
    assert off_top and all("drawn" in d.reason and "Damage" in d.reason for d in off_top)
    on_top = [d for d in reasons if d.upgrade_id == "damage"]
    assert on_top and all("drawn" not in d.reason for d in on_top)


@pytest.mark.parametrize("sharpness", [0, -1])
def test_a_non_positive_sharpness_is_rejected(sharpness: float) -> None:
    with pytest.raises(ValueError, match="sharpness"):
        choose_next(facts(draw_sharpness=sharpness))

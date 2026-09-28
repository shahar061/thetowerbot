from __future__ import annotations

from typing import Any

import lab_catalog
from fleet.build_route import RouteBaseline, RouteDocument
from fleet.lab_list import SlotSaving
from fleet.resource_blocks import LabFacts, LabPlan, evaluate_lab_plan

NOW = 1_000_000.0
ENTRIES = [
    {"id": "gs", "lab_id": "labs.game-speed", "to_level": 7, "tier": "S+", "pin_slot": 1},
    {"id": "ls50", "lab_id": "labs.labs-speed", "to_level": 50, "tier": "S", "pin_slot": 2},
    {"id": "cw", "lab_id": "labs.coins-wave", "to_level": 10, "tier": "B"},
    {"id": "ckb", "lab_id": "labs.coins-kill-bonus", "to_level": 30, "tier": "A"},
    {"id": "ls99", "lab_id": "labs.labs-speed", "to_level": 99, "tier": "A", "pin_slot": 2},
]
GS = ENTRIES[0]


class Route:
    """The evaluator reads .labs, .gems, .rules and .revision - like resolve_route's result."""

    def __init__(self, entries: list[dict[str, Any]] = ENTRIES, rules: dict | None = None) -> None:
        raw = RouteDocument.compatibility().to_dict()["baseline"]
        raw["labs"] = {"slot1_research": "game_speed", "steps": ["research_game_speed"], "mode": "blocks",
                       "blocks": [{"id": "labs.list", "type": "lab_list", "entries": entries}]}
        raw["rules"] = rules or {"coins": {"lab_share": {"mode": "just_in_time", "pct": 25}},
                                 "labs": {"filler": {"enabled": False}}}
        baseline = RouteBaseline.from_dict(raw)
        self.labs, self.gems, self.rules, self.revision = baseline.labs, baseline.gems, baseline.rules, 3


def slot(state: str, research: str | None = None, level: int | None = None,
         finish: float | None = None) -> dict[str, Any]:
    return {"state": state, "research_id": research, "target_level": level, "expected_finish": finish,
            "observed_at": NOW - 5, "confirmed": True}


def facts(**changes: Any) -> LabFacts:
    base = dict(now=NOW, wallet_coins=100_000, available_coins=100_000, best_tier_1_wave=200,
                best_waves={1: 200}, coins_per_hour=10_000.0,
                slots={1: slot("idle"), 2: slot("idle"), 3: slot("locked"), 4: slot("locked"), 5: slot("locked")},
                completed_levels={"labs.game-speed": 3, "labs.labs-speed": 10, "labs.coins-wave": 2,
                                  "labs.coins-kill-bonus": 5},
                account_id="acct")
    base.update(changes)
    return LabFacts(**base)


def plan(route: Route | None = None, **changes: Any) -> LabPlan:
    return evaluate_lab_plan(route or Route(), facts(**changes))


def plan_with_unread_levels(route: Route | None = None) -> LabPlan:
    """No lab level read anywhere: every entry is skipped as "level unread".

    Shared with the saving-plan tests, which assert Workshop is not frozen here.
    """
    return plan(route, completed_levels={})


def test_pins_hold_slots_one_and_two() -> None:
    result = plan()
    assert result.slots[0].next.lab_id == "labs.game-speed" and result.slots[0].next.level == 4
    assert result.slots[1].next.lab_id == "labs.labs-speed" and result.slots[1].next.level == 11
    assert result.slots[2].next is None and "not owned" in result.slots[2].why[-1]


def test_pin_releases_once_game_speed_is_maxed() -> None:
    result = plan(completed_levels={"labs.game-speed": 7, "labs.labs-speed": 10, "labs.coins-wave": 2,
                                    "labs.coins-kill-bonus": 5})
    assert result.slots[0].next.lab_id == "labs.coins-wave"


def test_locked_pin_falls_through_to_the_list() -> None:
    result = plan(best_tier_1_wave=100, best_waves={1: 100})
    assert result.slots[1].next.lab_id == "labs.coins-wave"
    assert any("locked" in line for line in result.slots[1].why)


def test_pinned_lab_never_leaves_its_slot() -> None:
    owned = {1: slot("researching", "labs.game-speed", 4, NOW + 3600),
             2: slot("researching", "labs.labs-speed", 50, NOW + 3600),
             3: slot("idle"), 4: slot("locked"), 5: slot("locked")}
    result = plan(slots=owned, completed_levels={"labs.game-speed": 3, "labs.labs-speed": 49,
                                                 "labs.coins-wave": 10, "labs.coins-kill-bonus": 30})
    assert result.slots[2].next is None
    assert any(line == "ls99: pinned elsewhere" for line in result.slots[2].why)
    assert result.slots[1].next.lab_id == "labs.labs-speed" and result.slots[1].next.level == 51


def test_two_slots_never_pick_the_same_lab() -> None:
    owned = {1: slot("idle"), 2: slot("idle"), 3: slot("idle"), 4: slot("idle"), 5: slot("locked")}
    result = plan(slots=owned, completed_levels={"labs.game-speed": 7, "labs.labs-speed": 10,
                                                 "labs.coins-wave": 2, "labs.coins-kill-bonus": 5})
    picked = [s.next.lab_id for s in result.slots if s.next is not None]
    assert len(picked) == len(set(picked))


def test_running_slot_targets_its_next_level_at_completion() -> None:
    owned = {1: slot("researching", "labs.game-speed", 4, NOW + 7200), 2: slot("idle"),
             3: slot("locked"), 4: slot("locked"), 5: slot("locked")}
    result = plan(slots=owned)
    assert result.slots[0].next.lab_id == "labs.game-speed" and result.slots[0].next.level == 5


def test_beyond_save_window_is_skipped() -> None:
    entries = [GS, {"id": "c", "lab_id": "labs.coins-kill-bonus", "to_level": 30, "tier": "C"},
               {"id": "cw", "lab_id": "labs.coins-wave", "to_level": 10, "tier": "B"}]
    result = plan(Route(entries), wallet_coins=0, available_coins=0,
                  completed_levels={"labs.game-speed": 7, "labs.coins-kill-bonus": 5, "labs.coins-wave": 2})
    assert result.slots[0].next.lab_id == "labs.coins-wave"
    assert any("beyond save window" in line for line in result.slots[0].why)


def test_unknown_income_lets_only_top_tiers_wait() -> None:
    result = plan(wallet_coins=0, available_coins=0, coins_per_hour=None,
                  completed_levels={"labs.game-speed": 7, "labs.labs-speed": 10,
                                    "labs.coins-wave": 2, "labs.coins-kill-bonus": 5})
    assert result.slots[0].next is None


def test_unread_levels_skip_with_a_reason() -> None:
    result = plan_with_unread_levels()
    # Game Speed's level comes from completed_levels or the slot-1 record; both are empty.
    assert result.slots[0].next is None
    assert result.slots[1].next is None
    assert any("level unread" in line for line in result.slots[1].why)


def test_start_manually_note_for_unautomated_targets() -> None:
    result = plan()
    assert result.slots[1].note == "Start manually" and not result.slots[1].automated


def test_slot_track_strategies_are_untouched() -> None:
    from fleet.resource_blocks import template_lab_blocks
    raw = RouteDocument.compatibility().to_dict()["baseline"]
    raw["labs"].update(mode="blocks", blocks=list(template_lab_blocks()))
    baseline = RouteBaseline.from_dict(raw)
    result = evaluate_lab_plan(baseline, facts())
    assert result.saving is None and all(s.role == "target" for s in result.slots)


def test_each_slot_is_judged_against_the_whole_wallet() -> None:
    """A slot's target doesn't take coins from later slots; the saving plan weighs them together."""
    entries = [GS, {"id": "as", "lab_id": "labs.attack-speed", "to_level": 5, "tier": "C"},
               {"id": "cb", "lab_id": "labs.cash-bonus", "to_level": 5, "tier": "C"}]
    result = plan(Route(entries), wallet_coins=50, available_coins=50, coins_per_hour=None,
                  completed_levels={"labs.game-speed": 7, "labs.attack-speed": 0, "labs.cash-bonus": 0})
    assert (result.slots[0].next.lab_id, result.slots[0].next.price) == ("labs.attack-speed", 30)
    assert (result.slots[1].next.lab_id, result.slots[1].next.price) == ("labs.cash-bonus", 30)
    assert result.slots[1].covered is True


def test_running_elsewhere_and_claimed_are_separate_reasons() -> None:
    entries = [GS, {"id": "cw", "lab_id": "labs.coins-wave", "to_level": 10, "tier": "B"},
               {"id": "ckb", "lab_id": "labs.coins-kill-bonus", "to_level": 30, "tier": "A"}]
    owned = {1: slot("idle"), 2: slot("idle"), 3: slot("researching", "labs.coins-wave", 3, NOW + 600),
             4: slot("locked"), 5: slot("locked")}
    result = plan(Route(entries), slots=owned,
                  completed_levels={"labs.game-speed": 7, "labs.coins-wave": 2, "labs.coins-kill-bonus": 5})
    assert result.slots[0].next.lab_id == "labs.coins-kill-bonus"
    assert "cw: running elsewhere" in result.slots[0].why
    assert result.slots[1].next is None
    assert "cw: running elsewhere" in result.slots[1].why
    assert "ckb: claimed" in result.slots[1].why
    assert result.slots[2].next.lab_id == "labs.coins-wave" and result.slots[2].next.level == 4


def test_no_survivor_names_the_most_common_skip_reason() -> None:
    entries = [GS, {"id": "up", "lab_id": "labs.unlock-perks", "to_level": 1, "tier": "S+"},
               {"id": "ls", "lab_id": "labs.labs-speed", "to_level": 50, "tier": "S"},
               {"id": "cw", "lab_id": "labs.coins-wave", "to_level": 10, "tier": "B"}]
    result = plan(Route(entries), best_tier_1_wave=100, best_waves={1: 100, 2: 10},
                  completed_levels={"labs.game-speed": 3, "labs.unlock-perks": 0, "labs.labs-speed": 10,
                                    "labs.coins-wave": 10})
    assert result.slots[1].next is None
    # The closing skip line, then step 4's filler line (fillers are off in the default Route).
    assert result.slots[1].why[-2:] == ("No entry can run now: locked for 2 of 3 remaining entries"
                                        " (needs Tier 2 wave 150)", "Fillers off")
    assert plan_with_unread_levels().slots[1].why[-2:] == (
        "No entry can run now: level unread for 3 of 4 remaining entries", "Fillers off")


def test_unlock_facts_are_never_guessed() -> None:
    entries = [GS, {"id": "up", "lab_id": "labs.unlock-perks", "to_level": 1, "tier": "S+"},
               {"id": "spb", "lab_id": "labs.standard-perks-bonus", "to_level": 10, "tier": "S"}]
    unread = plan(Route(entries), completed_levels={"labs.game-speed": 3, "labs.standard-perks-bonus": 0})
    assert unread.slots[1].next is None
    assert "up: unlock unread: Tier 2 best wave" in unread.slots[1].why
    assert "spb: unlock unread: Unlock Perks level" in unread.slots[1].why
    unlocked = plan(Route(entries), best_waves={1: 200, 2: 150},
                    completed_levels={"labs.game-speed": 3, "labs.unlock-perks": 1,
                                      "labs.standard-perks-bonus": 0})
    assert unlocked.slots[1].next.lab_id == "labs.standard-perks-bonus"


FILLER_RULES = {"coins": {"lab_share": {"mode": "just_in_time", "pct": 25}},
                "labs": {"filler": {"enabled": True, "max_price_pct_of_wallet": 10, "min_hours": 1}}}


def evaluate(route: Route, **changes: Any) -> tuple[list[Any], list[Any], int | None]:
    from fleet.lab_list import _evaluate_slots
    from fleet.resource_blocks import _slot_context
    lab_facts = facts(**changes)
    return _evaluate_slots(route, lab_facts, _slot_context(lab_facts))


def test_unaffordable_target_gets_shortest_cheap_filler() -> None:
    # Game Speed L4 costs 50,000; the wallet holds 20,000, so the price cap is 2,000. Both
    # Coins/Wave L3 (178 coins, 960 s) and Coins/Kill L6 (1,350 coins, 4,800 s) fit the cap and
    # the ~4.2h gap (income 10,000/h at a 75% margin); the shorter one wins.
    cw3, ckb6 = lab_catalog.level("labs.coins-wave", 3), lab_catalog.level("labs.coins-kill-bonus", 6)
    assert cw3.seconds < ckb6.seconds and ckb6.coins <= 2_000
    plans, savings, left = evaluate(Route(rules=FILLER_RULES), wallet_coins=20_000, available_coins=20_000)
    slot1 = plans[0]
    assert slot1.role == "filler" and slot1.saving_for.lab_id == "labs.game-speed"
    assert (slot1.next.lab_id, slot1.next.level) == ("labs.coins-wave", 3)
    assert slot1.covered is True
    assert "Filler Coins / Wave L3 while saving for Game Speed L4" in slot1.why
    # Game Speed is saved for until the filler ends; slot 2's Labs Speed starts now.
    assert savings == [SlotSaving(1, slot1.saving_for, NOW + cw3.seconds, True, "S+")]
    assert plans[1].role == "target" and plans[1].covered is True
    assert left == 20_000 - cw3.coins - plans[1].next.price


def test_filler_over_price_cap_is_refused() -> None:
    # Cap = 500 coins: Coins/Wave L10 (6,180) and Coins/Kill L30 (147,960) are both over it.
    result = plan(Route(rules=FILLER_RULES), wallet_coins=5_000, available_coins=5_000,
                  completed_levels={"labs.game-speed": 3, "labs.labs-speed": 10,
                                    "labs.coins-wave": 9, "labs.coins-kill-bonus": 29})
    assert result.slots[0].role == "target" and result.slots[0].next.lab_id == "labs.game-speed"
    assert result.slots[0].covered is False
    assert result.slots[0].why[-1] == "No filler fits the price cap and the gap"


def test_filler_longer_than_gap_is_refused() -> None:
    # The only candidate is Coins/Kill L7 (2,130 coins, 6,960 s); the wallet is 49,000 of Game
    # Speed's 50,000. At 1,500/h net income the gap is (50,000 - 46,870) / 1,500 = 2.09h, so it
    # fits; at 7.5M/h the gap collapses to min_hours (1h), which the 1.9h filler overruns.
    entries = [GS, {"id": "ckb", "lab_id": "labs.coins-kill-bonus", "to_level": 30, "tier": "A"}]
    levels = {"labs.game-speed": 3, "labs.coins-kill-bonus": 6}
    slow = plan(Route(entries, FILLER_RULES), wallet_coins=49_000, available_coins=49_000,
                coins_per_hour=2_000.0, completed_levels=levels)
    assert slow.slots[0].role == "filler" and slow.slots[0].next.level == 7
    fast = plan(Route(entries, FILLER_RULES), wallet_coins=49_000, available_coins=49_000,
                coins_per_hour=10_000_000.0, completed_levels=levels)
    assert fast.slots[0].role == "target" and fast.slots[0].next.lab_id == "labs.game-speed"
    assert fast.slots[0].why[-1] == "No filler fits the price cap and the gap"


def test_unknown_income_caps_filler_at_min_hours() -> None:
    # Coins/Kill L6 runs 4,800 s: it fits the ~4.2h gap with income known, not the 1h min_hours.
    entries = [GS, {"id": "ckb", "lab_id": "labs.coins-kill-bonus", "to_level": 30, "tier": "A"}]
    levels = {"labs.game-speed": 3, "labs.coins-kill-bonus": 5}
    known = plan(Route(entries, FILLER_RULES), wallet_coins=20_000, available_coins=20_000,
                 completed_levels=levels)
    assert known.slots[0].role == "filler" and known.slots[0].next.level == 6
    unknown = plan(Route(entries, FILLER_RULES), wallet_coins=20_000, available_coins=20_000,
                   coins_per_hour=None, completed_levels=levels)
    assert unknown.slots[0].role == "target" and unknown.slots[0].next.lab_id == "labs.game-speed"
    # A filler within min_hours still runs: Coins/Wave L3 takes 960 s.
    short = plan(Route(rules=FILLER_RULES), wallet_coins=20_000, available_coins=20_000, coins_per_hour=None)
    assert short.slots[0].role == "filler" and short.slots[0].next.seconds == 960


def test_slot_without_a_target_says_no_filler_fits() -> None:
    # Game Speed is maxed and Coins/Wave L10 (6,180, tier C) is beyond its 0h save window, so slot 1
    # has no target; the same lab is over the 200-coin filler cap.
    entries = [GS, {"id": "cw", "lab_id": "labs.coins-wave", "to_level": 10, "tier": "C"}]
    plans, savings, _ = evaluate(Route(entries, FILLER_RULES), wallet_coins=2_000, available_coins=2_000,
                                 completed_levels={"labs.game-speed": 7, "labs.coins-wave": 9})
    assert plans[0].next is None and plans[0].covered is None
    assert plans[0].why[-2].startswith("No entry can run now")
    assert plans[0].why[-1] == "No filler fits the price cap and the gap"
    assert savings == []


def test_disabled_filler_leaves_slot_waiting_on_target() -> None:
    result = plan(wallet_coins=20_000, available_coins=20_000)
    assert result.slots[0].role == "target" and result.slots[0].next.lab_id == "labs.game-speed"
    assert result.slots[0].covered is False
    assert result.slots[0].why[-1] == "Fillers off"


def test_owned_unread_slot_targets_now_without_a_filler() -> None:
    owned = {1: slot("idle"), 2: slot("owned_unread"), 3: slot("locked"), 4: slot("locked"), 5: slot("locked")}
    plans, savings, _ = evaluate(Route(rules=FILLER_RULES), slots=owned, wallet_coins=5_000,
                                 available_coins=5_000)
    assert plans[1].role == "target" and plans[1].next.lab_id == "labs.labs-speed"
    assert [s for s in savings if s.slot == 2] == [SlotSaving(2, plans[1].next, NOW, False, "S")]


def test_researching_slot_targets_its_completion() -> None:
    owned = {1: slot("researching", "labs.game-speed", 4, NOW + 7200), 2: slot("idle"),
             3: slot("locked"), 4: slot("locked"), 5: slot("locked")}
    plans, savings, _ = evaluate(Route(rules=FILLER_RULES), slots=owned)
    assert plans[0].role == "target" and plans[0].next.level == 5
    assert savings[0] == SlotSaving(1, plans[0].next, NOW + 7200, False, "S+", researching=True)


def test_plan_carries_saving_for_filler_target() -> None:
    result = plan(Route(rules=FILLER_RULES), wallet_coins=20_000, available_coins=20_000)
    assert result.saving is not None
    slot1 = next(t for t in result.saving.targets if t.slot == 1)
    assert slot1.lab_id == "labs.game-speed" and slot1.needed_at == NOW + result.slots[0].next.seconds
    # Every start happening now (slot 1's filler, slot 2's Labs Speed) is paid before saving.
    spent = sum(s.next.price for s in result.slots if s.now.state == "idle" and s.covered is True)
    assert result.saving.wallet == 20_000 - spent
    # The filler slot's covered describes the filler starting now; its target's lives in the plan.
    assert result.slots[0].role == "filler" and result.slots[0].covered is True
    assert slot1.covered is False


def test_researching_slot_covered_comes_from_the_saving_plan() -> None:
    owned = {1: slot("researching", "labs.game-speed", 4, NOW + 7200), 2: slot("idle"),
             3: slot("locked"), 4: slot("locked"), 5: slot("locked")}
    # Game Speed L5 (150,000) is due in 2h. Labs Speed L11 (16,710) starts now and leaves
    # 140,000; 7,500/h of income makes up the last 10,000 in 1.3h.
    funded = plan(slots=owned, wallet_coins=156_710, available_coins=156_710)
    assert funded.slots[0].covered is True
    assert next(t for t in funded.saving.targets if t.slot == 1).covered is True
    poor = plan(slots=owned, wallet_coins=0, available_coins=0)
    assert poor.slots[0].covered is False


def test_unknown_completion_leaves_researching_slot_uncovered() -> None:
    owned = {1: slot("researching", "labs.game-speed", 4, None), 2: slot("idle"),
             3: slot("locked"), 4: slot("locked"), 5: slot("locked")}
    result = plan(slots=owned)
    assert result.slots[0].next.lab_id == "labs.game-speed" and result.slots[0].covered is None
    target = next(t for t in result.saving.targets if t.slot == 1)
    assert target.covered is None and target.skipped_reason == "completion time unknown"


def test_unread_levels_do_not_freeze_workshop() -> None:
    result = plan_with_unread_levels()
    assert result.saving.reserve == 0 and result.saving.workshop_budget == 100_000

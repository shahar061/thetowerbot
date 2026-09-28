from __future__ import annotations

from typing import Any

from fleet.build_route import RouteBaseline, RouteDocument
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
    assert result.slots[1].why[-1] == ("No entry can run now: locked for 2 of 3 remaining entries"
                                       " (needs Tier 2 wave 150)")
    assert plan_with_unread_levels().slots[1].why[-1] == (
        "No entry can run now: level unread for 3 of 4 remaining entries")


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

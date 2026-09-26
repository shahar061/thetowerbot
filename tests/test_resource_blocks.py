"""Labs and Gems lanes: bounded blocks, a fixed automated set, and a pure plan."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from fleet import resource_blocks as rb


def labs() -> list[dict[str, Any]]:
    return deepcopy(list(rb.template_lab_blocks()))


def gems() -> list[dict[str, Any]]:
    return deepcopy(list(rb.template_gem_blocks()))


def test_template_lanes_validate_and_keep_the_invariants() -> None:
    lab_blocks, gem_blocks = rb.validate_labs(labs()), rb.validate_gems(gems())
    slot1 = next(track for track in lab_blocks if 1 in track["slots"])
    assert slot1["children"][0] == {"id": "labs.slot1.game_speed", "type": "research",
                                    "lab_id": "labs.game-speed", "to_level": 7,
                                    "label": "Game Speed to max"}
    assert gem_blocks[0]["type"] == "unlock_lab_slot" and gem_blocks[0]["slot"] == 2
    assert [block["slot"] for block in gem_blocks if block["type"] == "unlock_lab_slot"] == [2, 3, 4, 5]
    assert sorted(slot for track in lab_blocks for slot in track["slots"]) == [1, 2, 3, 4, 5]


def test_automated_set_is_exactly_game_speed_in_slot_one_and_lab_two() -> None:
    assert rb.AUTOMATED == frozenset({("labs", "research", "labs.game-speed", 1),
                                      ("gems", "unlock_lab_slot", "", 2)})
    assert rb.automated_list() == [
        {"lane": "gems", "type": "unlock_lab_slot", "slot": 2},
        {"lane": "labs", "type": "research", "lab_id": "labs.game-speed", "slot": 1}]
    assert rb.research_automated("labs.game-speed", 1)
    assert not rb.research_automated("labs.game-speed", 2)
    assert not rb.research_automated("labs.attack-speed", 1)
    assert rb.gem_automated({"type": "unlock_lab_slot", "slot": 2})
    assert not rb.gem_automated({"type": "unlock_lab_slot", "slot": 3})


def test_gem_path_must_start_with_lab_slot_two_and_unlock_in_order() -> None:
    first_card = gems()
    first_card.insert(0, first_card.pop(4))
    with pytest.raises(ValueError, match="start by unlocking lab slot 2"):
        rb.validate_gems(first_card)
    out_of_order = gems()
    out_of_order[1], out_of_order[2] = out_of_order[2], out_of_order[1]
    with pytest.raises(ValueError, match="unlock in order"):
        rb.validate_gems(out_of_order)


def test_slot_one_track_must_start_with_game_speed_and_slots_have_one_track() -> None:
    wrong_first = labs()
    wrong_first[0]["children"].reverse()
    with pytest.raises(ValueError, match="slot 1 track must start with Game Speed"):
        rb.validate_labs(wrong_first)
    doubled = labs()
    doubled[1]["slots"] = [1, 2]
    with pytest.raises(ValueError, match="lab slot 1 belongs to two tracks"):
        rb.validate_labs(doubled)


@pytest.mark.parametrize("mutate", [
    lambda blocks: blocks[0]["children"].append({"id": "rush", "type": "rush", "lab_id": "labs.game-speed"}),
    lambda blocks: blocks[0]["children"][0].update(rush_with_gems=True),
    lambda blocks: blocks[0]["children"].append({"id": "x", "type": "research", "lab_id": "labs.invented", "to_level": 1}),
    lambda blocks: blocks[0]["children"][0].update(to_level=8),
    lambda blocks: blocks[0]["children"].append({"id": "labs.slot1.game_speed", "type": "wait"}),
])
def test_rushing_unknown_labs_and_malformed_blocks_are_rejected(mutate: Any) -> None:
    blocks = labs()
    mutate(blocks)
    with pytest.raises(ValueError):
        rb.validate_labs(blocks)


def test_nesting_is_bounded() -> None:
    nested: list[dict[str, Any]] = [{"id": "leaf", "type": "wait"}]
    for index in range(10):
        nested = [{"id": f"c{index}", "type": "condition", "field": "best_tier_1_wave",
                   "cmp": "gte", "value": 1, "then": nested, "else": []}]
    blocks = labs()
    blocks[3]["children"] = nested
    with pytest.raises(ValueError, match="bounded"):
        rb.validate_labs(blocks)


def test_pool_limits_may_tighten_but_never_loosen_the_rule() -> None:
    blocks = rb.validate_labs(labs())  # the slot-2 short-lab pool sets max_seconds 1800
    rb.check_pool_limits(blocks, max_seconds=None, max_price_pct=None)
    rb.check_pool_limits(blocks, max_seconds=3600, max_price_pct=10)
    with pytest.raises(ValueError, match="max_seconds is looser"):
        rb.check_pool_limits(blocks, max_seconds=600, max_price_pct=None)
    looser = labs()
    looser[3]["children"][0]["max_price_pct_of_wallet"] = 50
    with pytest.raises(ValueError, match="max_price_pct_of_wallet is looser"):
        rb.check_pool_limits(rb.validate_labs(looser), max_seconds=None, max_price_pct=10)


def test_legacy_steps_translate_to_equivalent_blocks() -> None:
    assert rb.legacy_gem_blocks(("unlock_lab_slot_2", "cards", "unlock_lab_slot_4", "unlock_lab_slot_3")) == (
        {"id": "legacy.gems.unlock_lab_slot_2", "type": "unlock_lab_slot", "slot": 2},
        {"id": "legacy.gems.cards", "type": "buy_cards", "purpose": "card_missions"},
        {"id": "legacy.gems.unlock_lab_slot_4", "type": "unlock_lab_slot", "slot": 3},
        {"id": "legacy.gems.unlock_lab_slot_3", "type": "unlock_lab_slot", "slot": 4})
    assert rb.legacy_gem_blocks(("unlock_lab_slot_2", "card_slot"))[1] == {
        "id": "legacy.gems.card_slot", "type": "card_slots", "up_to": 2, "when_usable_card": False}
    assert rb.legacy_lab_blocks(("research_game_speed", "slot2_research")) == (
        {"id": "legacy.labs.slot1", "type": "slot_track", "slots": [1], "children": [
            {"id": "legacy.labs.game_speed", "type": "research", "lab_id": "labs.game-speed", "to_level": 7}]},
        {"id": "legacy.labs.slot2", "type": "slot_track", "slots": [2], "children": [
            {"id": "legacy.labs.slot2.pool", "type": "lab_pool", "lab_ids": list(rb.LEGACY_SLOT2_POOL),
             "selection": "cheapest"}]})

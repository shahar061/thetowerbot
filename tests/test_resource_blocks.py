"""Labs and Gems lanes: bounded blocks, a fixed automated set, and a pure plan."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from fleet import resource_blocks as rb
from fleet.build_route import RouteDocument, resolve_route
from fleet.resource_blocks import LabFacts, evaluate_lab_plan


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


def template_route(**rules: Any):
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["gems"].update(mode="blocks", blocks=list(rb.template_gem_blocks()))
    raw["baseline"]["labs"].update(mode="blocks", blocks=list(rb.template_lab_blocks()))
    merged = rb.template_rules()
    for section, values in rules.items():
        merged[section] = {**merged[section], **values}
    raw["baseline"]["rules"] = merged
    return resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")


def waiting(level: int = 3, observed_at: float = 900.) -> dict[str, Any]:
    return {"kind": "wait_coins", "price": 12000, "game_speed_level": level,
            "observed_at": observed_at, "job_completes_at": None}


def test_nothing_read_means_unknown_slots_never_a_guess() -> None:
    plan = evaluate_lab_plan(template_route(), LabFacts(now=1000.))
    assert [slot.now.state for slot in plan.slots] == ["unknown"] * 5
    assert plan.gems.next is not None and plan.gems.next.type == "unlock_lab_slot"
    assert (plan.gems.price, plan.gems.need, plan.gems.have) == (100, 100, None)


def test_game_speed_next_level_uses_the_catalog_price_and_the_wallet() -> None:
    plan = evaluate_lab_plan(template_route(), LabFacts(now=1000., wallet_coins=20000, slot1=waiting()))
    slot1 = plan.slots[0]
    assert slot1.now.state == "idle"
    assert slot1.next is not None
    assert (slot1.next.lab_id, slot1.next.level, slot1.next.price, slot1.next.seconds) == (
        "labs.game-speed", 3, 12000, 35280)
    assert slot1.covered is True and slot1.automated is True
    poorer = evaluate_lab_plan(template_route(), LabFacts(now=1000., wallet_coins=5000, slot1=waiting()))
    assert poorer.slots[0].covered is False


def test_researching_slot_never_shows_a_negative_timer() -> None:
    running = {"kind": "wait_running", "game_speed_level": 3, "observed_at": 900.,
               "job_completes_at": 5000.}
    before = evaluate_lab_plan(template_route(), LabFacts(now=4000., slot1=running)).slots[0]
    assert (before.now.state, before.now.completes_at, before.now.overdue_seconds) == ("researching", 5000., None)
    assert before.next is not None and before.next.level == 4  # queued after the running level
    after = evaluate_lab_plan(template_route(), LabFacts(now=8000., slot1=running)).slots[0]
    assert after.now.overdue_seconds == 3000.
    stale = evaluate_lab_plan(template_route(), LabFacts(now=900. + 86401., slot1=running)).slots[0]
    assert stale.now.stale is True


def test_slot_two_and_the_inferred_later_slots() -> None:
    locked = evaluate_lab_plan(template_route(), LabFacts(
        now=1000., slot2={"status": "locked", "wallet_gems": 60, "observed_at": 900.}))
    assert [slot.now.state for slot in locked.slots[1:]] == ["locked"] * 4
    owned = evaluate_lab_plan(template_route(), LabFacts(
        now=1000., slot2={"status": "owned", "wallet_gems": 5, "observed_at": 900.}))
    assert [slot.now.state for slot in owned.slots[1:]] == ["owned_unread", "unknown", "unknown", "unknown"]


def test_auto_start_off_is_not_automated_and_says_so() -> None:
    plan = evaluate_lab_plan(template_route(labs={"auto_start": False}),
                             LabFacts(now=1000., wallet_coins=20000, slot1=waiting()))
    assert plan.slots[0].automated is False
    assert plan.slots[0].note == "Auto-start off"


def test_maxed_game_speed_moves_slot_one_and_slot_two_on_as_planned_steps() -> None:
    done = {"kind": "done", "game_speed_level": 7, "observed_at": 900., "job_completes_at": None}
    plan = evaluate_lab_plan(template_route(), LabFacts(now=1000., wallet_coins=10**7, slot1=done))
    slot1, slot2 = plan.slots[0], plan.slots[1]
    assert slot1.next is not None and slot1.next.lab_id == "labs.attack-speed"
    assert (slot1.next.price, slot1.covered, slot1.automated) == (None, None, False)
    assert slot2.next is not None and slot2.next.lab_id == "labs.labs-speed"


def test_a_condition_on_an_unread_fact_leaves_the_slot_without_a_next_lab() -> None:
    slot2 = evaluate_lab_plan(template_route(), LabFacts(now=1000.)).slots[1]
    assert slot2.next is None
    assert any("unread" in line for line in slot2.why)


def test_pool_selection_and_limits() -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["labs"].update(mode="blocks", blocks=[
        {"id": "one", "type": "slot_track", "slots": [1], "children": [
            {"id": "gs", "type": "research", "lab_id": "labs.game-speed", "to_level": 7}]},
        {"id": "two", "type": "slot_track", "slots": [2], "children": [
            {"id": "pool", "type": "lab_pool", "selection": "cheapest",
             "lab_ids": ["labs.coins-wave", "labs.game-speed"]}]}])
    facts = LabFacts(now=1000., wallet_coins=20000, slot1=waiting())
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    picked = evaluate_lab_plan(route, facts).slots[1]
    assert picked.next is not None and picked.next.lab_id == "labs.game-speed"  # known price wins
    assert picked.automated is False  # a pool pick in slot 2 is not the automated set
    raw["baseline"]["labs"]["blocks"][1]["children"][0]["max_seconds"] = 600
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    assert evaluate_lab_plan(route, facts).slots[1].next.lab_id == "labs.coins-wave"


def test_gem_path_have_need_and_keep() -> None:
    locked = {"status": "locked", "wallet_gems": 60, "observed_at": 900.}
    plan = evaluate_lab_plan(template_route(), LabFacts(now=1000., wallet_gems=60, slot2=locked))
    assert (plan.gems.next.block_id, plan.gems.have, plan.gems.need, plan.gems.automated) == (
        "gems.lab2", 60, 100, True)
    assert [step.state for step in plan.gems.steps][:2] == ["current", "next"]
    kept = evaluate_lab_plan(template_route(gems={"keep": 50}), LabFacts(now=1000., wallet_gems=60, slot2=locked))
    assert kept.gems.need == 150
    owned = evaluate_lab_plan(template_route(), LabFacts(
        now=1000., wallet_gems=60, slot2={"status": "owned", "wallet_gems": 60, "observed_at": 900.}))
    assert [step.state for step in owned.gems.steps][:2] == ["done", "current"]
    assert (owned.gems.next.block_id, owned.gems.price, owned.gems.automated) == ("gems.lab3", 400, False)
    off = evaluate_lab_plan(template_route(gems={"auto_unlock_lab_slots": False}),
                            LabFacts(now=1000., wallet_gems=60, slot2=locked))
    assert off.gems.automated is False


def test_a_legacy_steps_route_is_evaluated_through_its_translation() -> None:
    route = resolve_route(RouteDocument.compatibility(), "Air_38", "a1")
    plan = evaluate_lab_plan(route, LabFacts(now=1000., wallet_coins=20000, slot1=waiting()))
    assert plan.slots[0].next.lab_id == "labs.game-speed" and plan.slots[0].automated
    assert plan.gems.next.block_id == "legacy.gems.unlock_lab_slot_2"

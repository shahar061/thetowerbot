"""Labs and Gems lanes: bounded blocks, a fixed automated set, and a pure plan."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
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


def test_automated_set_retains_only_legacy_game_speed_route() -> None:
    assert rb.AUTOMATED == frozenset({("labs", "research", "labs.game-speed", 1)})
    assert rb.automated_list() == [
        {"lane": "labs", "type": "research", "lab_id": "labs.game-speed", "slot": 1}]
    assert rb.research_automated("labs.game-speed", 1)
    assert not rb.research_automated("labs.game-speed", 2)
    assert not rb.research_automated("labs.attack-speed", 1)
    assert not rb.gem_automated({"type": "unlock_lab_slot", "slot": 2})
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
    assert all(slot.now.owned is None and slot.now.research_id is None for slot in plan.slots)
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


@pytest.mark.parametrize(("value", "expected"), [
    ("enabled", "enabled"), ("disabled", "disabled"), ("unknown", "unknown"),
    (None, "unknown"), ("on", "unknown"), ([], "unknown"),
])
def test_lab_plan_preserves_observed_repeat_state_without_guessing(value: object, expected: str) -> None:
    record = {"state": "researching", "research_id": "labs.game-speed", "target_level": 3,
              "observed_at": 900., "expected_finish": 5000., "native_repeat": value, "confirmed": True}
    plan = evaluate_lab_plan(template_route(), LabFacts(now=1000., slots={1: record}))
    assert plan.slots[0].now.native_repeat == expected


def test_legacy_lab_observation_has_unknown_repeat_state() -> None:
    running = {"kind": "wait_running", "game_speed_level": 3, "observed_at": 900.,
               "job_completes_at": 5000.}
    plan = evaluate_lab_plan(template_route(), LabFacts(now=1000., slot1=running))
    assert plan.slots[0].now.native_repeat == "unknown"


def test_slot_two_and_the_inferred_later_slots() -> None:
    locked = evaluate_lab_plan(template_route(), LabFacts(
        now=1000., slot2={"status": "locked", "wallet_gems": 60, "observed_at": 900.}))
    assert [slot.now.state for slot in locked.slots[1:]] == ["locked", "unknown", "unknown", "unknown"]


def test_observed_slot_has_current_research_and_ownership_without_execution_claim() -> None:
    observed = {3: {"state": "researching", "research_id": "labs.coins-wave",
                    "target_level": 4, "expected_finish": 2000.,
                    "observed_at": 990., "confirmed": False, "preview_only": True}}
    plan = evaluate_lab_plan(template_route(), LabFacts(now=1000., slots=observed))
    slot = plan.slots[2]
    assert slot.now.research_id == "labs.coins-wave"
    assert slot.now.research_name == "Coins / Wave"
    assert slot.now.owned is True
    assert slot.now.evidence_status == "historical"
    assert slot.capabilities == {"observe": True, "plan": True, "execute": False, "rehearse": False}
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
    assert picked.next is not None and picked.next.lab_id == "labs.coins-wave"  # slot 1 reserves Game Speed
    assert picked.automated is False  # a pool pick in slot 2 is not the automated set
    raw["baseline"]["labs"]["blocks"][1]["children"][0]["max_seconds"] = 600
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    assert evaluate_lab_plan(route, facts).slots[1].next.lab_id == "labs.coins-wave"


def test_pool_uses_another_research_when_picker_blocks_its_first_choice() -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw['baseline']['rules']['labs'].update(auto_start=True, direct_start=True)
    raw['baseline']['labs'].update(mode='blocks', blocks=[
        {'id': 'one', 'type': 'slot_track', 'slots': [1], 'children': [
            {'id': 'speed', 'type': 'research', 'lab_id': 'labs.game-speed', 'to_level': 7}]},
        {'id': 'two', 'type': 'slot_track', 'slots': [2], 'children': [
            {'id': 'pool', 'type': 'lab_pool', 'selection': 'ordered',
             'lab_ids': ['labs.health', 'labs.cash-bonus']}]}])
    route = resolve_route(RouteDocument.from_dict(raw), 'Air_38', 'a1')
    facts = LabFacts(now=1000., wallet_coins=20000, available_coins=20000,
                     slots={2: {'state': 'idle', 'confirmed': True, 'observed_at': 990.}},
                     slot_ownership={2: {'status': 'owned'}},
                     worker='Air_38', account_id='a1')
    assert evaluate_lab_plan(route, facts).slots[1].next.lab_id == 'labs.health'
    blocked = replace(facts, reserved_research=frozenset({'labs.health'}))
    assert evaluate_lab_plan(route, blocked).slots[1].next.lab_id == 'labs.cash-bonus'


def test_pool_containing_a_lab_with_a_list_unlock_does_not_raise() -> None:
    # labs.labs-speed's unlock is a v2 condition tuple, not the old dict. A pool
    # that considers it must not crash calling .get() on that tuple (F1/R-F1).
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["labs"].update(mode="blocks", blocks=[
        {"id": "one", "type": "slot_track", "slots": [1], "children": [
            {"id": "gs", "type": "research", "lab_id": "labs.game-speed", "to_level": 7}]},
        {"id": "two", "type": "slot_track", "slots": [2], "children": [
            {"id": "pool", "type": "lab_pool", "selection": "cheapest",
             "lab_ids": ["labs.labs-speed", "labs.coins-wave"]}]}])
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    facts = LabFacts(now=1000., wallet_coins=20000, slot1=waiting())
    plan = evaluate_lab_plan(route, facts)
    assert plan.slots[1].next is not None


def test_direct_start_can_plan_affordable_pool_lab_with_unread_level() -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["rules"]["labs"].update(auto_start=True, direct_start=True)
    raw["baseline"]["labs"].update(mode="blocks", blocks=[
        {"id": "one", "type": "slot_track", "slots": [1], "children": [
            {"id": "gs", "type": "research", "lab_id": "labs.game-speed", "to_level": 7}]},
        {"id": "two", "type": "slot_track", "slots": [2], "on_blocked": "skip", "children": [
            {"id": "cheap", "type": "lab_pool", "selection": "ordered",
             "lab_ids": ["labs.health"]}]}])
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    facts = LabFacts(now=1000., wallet_coins=50, available_coins=50,
                     slot1=waiting(), slots={2: {"state": "idle", "confirmed": True,
                                                "observed_at": 990.}},
                     slot_ownership={2: {"status": "owned"}}, worker="Air_38", account_id="a1")
    slot = evaluate_lab_plan(route, facts).slots[1]
    assert slot.next is not None and (slot.next.lab_id, slot.next.level, slot.next.price) == (
        "labs.health", 1, 30)
    assert slot.automated is True


def test_gem_path_have_need_and_keep() -> None:
    locked = {"status": "locked", "wallet_gems": 60, "observed_at": 900.}
    plan = evaluate_lab_plan(template_route(), LabFacts(now=1000., wallet_gems=60, slot2=locked))
    assert (plan.gems.next.block_id, plan.gems.have, plan.gems.need, plan.gems.automated) == (
        "gems.lab2", 60, 100, False)
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


def test_slot_policy_defaults_and_validation() -> None:
    blocks = rb.validate_labs(labs())
    assert all(track.get("paused", False) is False and track.get("on_blocked", "wait") == "wait"
               for track in blocks)
    paused = labs()
    paused[1]["paused"] = True
    paused[1]["on_blocked"] = "skip"
    paused[1]["slot_policies"] = {"2": {"paused": False, "on_blocked": "wait"}}
    assert rb.validate_labs(paused)[1]["paused"] is True
    assert rb.validate_labs(paused)[1]["slot_policies"]["2"]["paused"] is False
    for field, value in (("paused", 1), ("on_blocked", "drop"), ("on_blocked", [])):
        invalid = labs()
        invalid[1][field] = value
        with pytest.raises(ValueError):
            rb.validate_labs(invalid)
    invalid = labs()
    invalid[2]["slot_policies"] = {"5": {"paused": True}}
    with pytest.raises(ValueError, match="slot policies"):
        rb.validate_labs(invalid)


def test_shared_track_reserves_distinct_research_and_skip_uses_next_target(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["labs"].update(mode="blocks", blocks=[
        {"id": "first", "type": "slot_track", "slots": [1], "children": [
            {"id": "speed", "type": "research", "lab_id": "labs.game-speed", "to_level": 7}]},
        {"id": "shared", "type": "slot_track", "slots": [3, 4], "on_blocked": "skip", "children": [
            {"id": "coins", "type": "research", "lab_id": "labs.coins-wave", "to_level": 10},
            {"id": "cash", "type": "research", "lab_id": "labs.cash-bonus", "to_level": 10}]}])
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    original = rb._option
    monkeypatch.setattr(rb, "_option", lambda lab_id, level: (
        rb.SlotNext(lab_id, lab_id, level, 100, 60)
        if lab_id in {"labs.coins-wave", "labs.cash-bonus"} else original(lab_id, level)))
    observed = {3: {"state": "idle", "confirmed": True}, 4: {"state": "idle", "confirmed": True}}
    plan = evaluate_lab_plan(route, LabFacts(now=1000., wallet_coins=1000, slots=observed, completed_levels={
        "labs.coins-wave": 1, "labs.cash-bonus": 1}))
    assert [plan.slots[i - 1].next.lab_id for i in (3, 4)] == ["labs.coins-wave", "labs.cash-bonus"]
    assert plan.slots[2].next.level == 2
    assert plan.slots[3].next.level == 2


def test_running_research_and_unknown_slot_do_not_erase_or_duplicate() -> None:
    facts = LabFacts(now=1000., slots={3: {"state": "researching", "research_id": "labs.coins-wave",
                                          "target_level": 4, "observed_at": 900.},
                                        4: {"state": "unknown"}},
                     completed_levels={"labs.coins-wave": 3})
    plan = evaluate_lab_plan(template_route(), facts)
    assert plan.slots[2].now.state == "researching"
    assert plan.slots[3].next is None


def test_paused_slot_does_not_allocate_and_unknown_cost_is_uncovered(monkeypatch: pytest.MonkeyPatch) -> None:
    original = rb._option
    monkeypatch.setattr(rb, "_option", lambda lab_id, level: (  # every lab but Game Speed unpriced
        original(lab_id, level) if lab_id == "labs.game-speed" else rb.SlotNext(lab_id, lab_id, level, None, None)))
    raw = RouteDocument.compatibility().to_dict()
    tracks = labs()
    tracks[2]["slot_policies"] = {"3": {"paused": True, "on_blocked": "skip"}}
    raw["baseline"]["labs"].update(mode="blocks", blocks=tracks)
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    plan = evaluate_lab_plan(route, LabFacts(now=1000., wallet_coins=100000,
        completed_levels={"labs.coins-wave": 1}, slots={3: {"state": "idle", "confirmed": True}}))
    assert plan.slots[2].next is None
    assert plan.slots[3].next is not None and plan.slots[3].covered is None
    assert plan.slots[4].next is not None and plan.slots[4].covered is None
    assert not plan.slots[4].automated


def test_wait_policy_holds_duplicate_and_skip_policy_moves_past_unknown_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = RouteDocument.compatibility().to_dict()
    base = {"id": "first", "type": "slot_track", "slots": [1], "children": [
        {"id": "speed", "type": "research", "lab_id": "labs.game-speed", "to_level": 7}]}
    second = {"id": "second", "type": "slot_track", "slots": [2], "children": [
        {"id": "duplicate", "type": "research", "lab_id": "labs.game-speed", "to_level": 7},
        {"id": "alternative", "type": "research", "lab_id": "labs.cash-bonus", "to_level": 10}]}
    raw["baseline"]["labs"].update(mode="blocks", blocks=[base, second])
    original = rb._option
    monkeypatch.setattr(rb, "_option", lambda lab_id, level: (
        rb.SlotNext(lab_id, lab_id, level, 100, 60)
        if lab_id == "labs.cash-bonus" else original(lab_id, level)))
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    facts = LabFacts(now=1000., wallet_coins=20000, slot1=waiting(), slots={2: {"state": "idle"}},
                     completed_levels={"labs.cash-bonus": 2})
    assert evaluate_lab_plan(route, facts).slots[1].next is None
    second["on_blocked"] = "skip"
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    assert evaluate_lab_plan(route, facts).slots[1].next.lab_id == "labs.cash-bonus"


def test_conservative_coin_budget_is_shared_across_idle_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["labs"].update(mode="blocks", blocks=[
        {"id": "first", "type": "slot_track", "slots": [1], "children": [
            {"id": "speed", "type": "research", "lab_id": "labs.game-speed", "to_level": 7}]},
        {"id": "second", "type": "slot_track", "slots": [2], "children": [
            {"id": "cash", "type": "research", "lab_id": "labs.cash-bonus", "to_level": 3}]}])
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    original = rb._option
    monkeypatch.setattr(rb, "_option", lambda lab_id, level: (
        rb.SlotNext(lab_id, "Cash Bonus", level, 8000, 60)
        if lab_id == "labs.cash-bonus" else original(lab_id, level)))
    facts = LabFacts(now=1000., wallet_coins=50000, available_coins=15000,
                     slot1=waiting(), slots={2: {"state": "idle"}},
                     completed_levels={"labs.cash-bonus": 1})
    plan = evaluate_lab_plan(route, facts)
    assert plan.slots[0].covered is True
    assert plan.slots[1].covered is False


def test_persisted_slot_preview_is_account_bound_and_plan_only(tmp_path: Path) -> None:
    from fleet.build_route_preview_facts import read_lab_slots

    account = "ACCOUNT-A"
    key = hashlib.sha256(account.encode()).hexdigest()
    path = tmp_path / f"lab-runtime-{key}.json"
    payload = {"version": 1, "scope": {"account_id": account, "lease_id": "old",
                                           "generation": "old", "epoch": 0},
               "slots": [{"slot": 1, "scope": {"account_id": account, "lease_id": "old",
                                                       "generation": "old", "epoch": 0},
                          "state": "researching", "research_id": "labs.game-speed",
                          "target_level": 3, "observed_at": 900.}]}
    path.write_text(json.dumps(payload))
    assert read_lab_slots(tmp_path, "ACCOUNT-B") == {}
    slots = read_lab_slots(tmp_path, account)
    assert slots[1]["research_id"] == "labs.game-speed"
    plan = evaluate_lab_plan(template_route(), LabFacts(now=1000., slots=slots))
    assert plan.slots[0].now.state == "researching"
    assert plan.slots[0].capabilities["execute"] is False


def test_persisted_idle_slot_from_previous_lease_is_never_executable(tmp_path: Path) -> None:
    from fleet.build_route_preview_facts import read_lab_slots

    account = "ACCOUNT-A"
    key = hashlib.sha256(account.encode()).hexdigest()
    path = tmp_path / f"lab-runtime-{key}.json"
    scope = {"account_id": account, "lease_id": "old", "generation": "old", "epoch": 0}
    path.write_text(json.dumps({"version": 1, "scope": scope, "slots": [
        {"slot": 1, "scope": scope, "state": "idle", "confirmed": True,
         "evidence_status": "verified", "observed_at": 900.}]}))
    slots = read_lab_slots(tmp_path, account)
    assert slots[1]["observed_at"] == 900.
    assert slots[1]["confirmed"] is False
    plan = evaluate_lab_plan(template_route(), LabFacts(now=1000., wallet_coins=20000,
        slot1=waiting(), slots=slots, account_id=account))
    assert plan.slots[0].now.state == "idle"
    assert plan.slots[0].capabilities["execute"] is False


def test_skip_moves_past_unknown_level_price_prerequisite_and_overbudget(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["labs"].update(mode="blocks", blocks=[
        {"id": "speed", "type": "slot_track", "slots": [1], "children": [
            {"id": "gs", "type": "research", "lab_id": "labs.game-speed", "to_level": 7}]},
        {"id": "choice", "type": "slot_track", "slots": [2], "on_blocked": "skip", "children": [
            {"id": "unknown-level", "type": "research", "lab_id": "labs.coins-wave", "to_level": 10},
            {"id": "unknown-price", "type": "research", "lab_id": "labs.cash-bonus", "to_level": 10},
            {"id": "unread-prereq", "type": "research", "lab_id": "labs.labs-speed", "to_level": 10},
            {"id": "expensive", "type": "research", "lab_id": "labs.attack-speed", "to_level": 10},
            {"id": "eligible", "type": "research", "lab_id": "labs.coins-kill-bonus", "to_level": 10}]}])
    original = rb._option
    monkeypatch.setattr(rb, "_option", lambda lab_id, level: (
        rb.SlotNext(lab_id, lab_id, level, 1000 if lab_id == "labs.attack-speed" else 100, 60)
        if lab_id in {"labs.labs-speed", "labs.attack-speed", "labs.coins-kill-bonus"}
        else rb.SlotNext(lab_id, lab_id, level, None, None) if lab_id == "labs.cash-bonus"
        else original(lab_id, level)))
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    facts = LabFacts(now=1000., available_coins=200, slots={2: {"state": "idle"}},
        completed_levels={"labs.cash-bonus": 1, "labs.labs-speed": 1,
                          "labs.attack-speed": 1, "labs.coins-kill-bonus": 1})
    selected = evaluate_lab_plan(route, facts).slots[1]
    assert selected.next is not None and selected.next.lab_id == "labs.coins-kill-bonus"
    assert selected.covered is True
    assert any("unknown" in reason or "unread" in reason for reason in selected.why)
    raw["baseline"]["labs"]["blocks"][1]["on_blocked"] = "wait"
    waiting_plan = evaluate_lab_plan(resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1"), facts)
    assert waiting_plan.slots[1].next.lab_id == "labs.coins-wave"


def test_slot_one_skip_cannot_bypass_unfinished_game_speed() -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["labs"].update(mode="blocks", blocks=[
        {"id": "one", "type": "slot_track", "slots": [1], "on_blocked": "skip", "children": [
            {"id": "gs", "type": "research", "lab_id": "labs.game-speed", "to_level": 7},
            {"id": "later", "type": "research", "lab_id": "labs.attack-speed", "to_level": 10}]}])
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    plan = evaluate_lab_plan(route, LabFacts(now=1000., wallet_coins=0, slot1=waiting()))
    assert plan.slots[0].next is not None and plan.slots[0].next.lab_id == "labs.game-speed"


def test_missing_or_future_slot_time_is_stale() -> None:
    facts = LabFacts(now=1000., slots={1: {"state": "idle", "confirmed": True},
                                       2: {"state": "idle", "confirmed": True,
                                           "observed_at": 1001.}})
    plan = evaluate_lab_plan(template_route(), facts)
    assert plan.slots[0].now.stale is True
    assert plan.slots[1].now.stale is True
    assert plan.slots[0].capabilities["execute"] is False


def test_uncalibrated_routes_stay_planning_only_with_a_visible_reason() -> None:
    plan = evaluate_lab_plan(template_route(), LabFacts(
        now=1000., wallet_coins=20000, wallet_gems=160,
        slot2={"status": "locked", "wallet_gems": 160, "observed_at": 990.}))
    assert plan.slots[0].automated is True  # legacy slot-one Game Speed regression
    for slot in plan.slots[2:]:
        assert slot.next is not None and slot.automated is False
        assert any(line.startswith("Planning only:") for line in slot.why)
    assert plan.gems.automated is False
    assert any(line == "gems.lab2: Rehearsing slot 2 · 0/2 dry runs" for line in plan.gems.why)


def test_gem_lane_reads_every_slot_from_slot_ownership() -> None:
    owned = {"status": "owned", "wallet_gems": 500, "observed_at": 900.}
    locked = {"status": "locked", "wallet_gems": 500, "observed_at": 900.}
    plan = evaluate_lab_plan(template_route(), LabFacts(now=1000., wallet_gems=500,
                                                        slot_ownership={2: owned, 3: locked}))
    assert [step.state for step in plan.gems.steps][:3] == ["done", "current", "next"]
    assert (plan.gems.next.block_id, plan.gems.next.slot, plan.gems.price) == ("gems.lab3", 3, 400)
    assert plan.slots[2].now.state == "locked"


def test_a_complete_strip_proves_the_lower_slots_owned() -> None:
    plan = evaluate_lab_plan(template_route(), LabFacts(now=1000., wallet_gems=5, owned_floor=3))
    assert plan.gems.next.block_id == "gems.lab4"


def test_next_unlock_slot_follows_the_gem_lane() -> None:
    blocks = rb.template_gem_blocks()
    assert rb.next_unlock_slot(blocks, {}) == 2
    assert rb.next_unlock_slot(blocks, {2: {"status": "owned"}}) == 3
    assert rb.next_unlock_slot(blocks, {}, owned_floor=5) is None  # the next step is card slots
    cards_first = rb.validate_gems([
        {"id": "lab2", "type": "unlock_lab_slot", "slot": 2},
        {"id": "cards", "type": "buy_cards", "purpose": "card_missions"},
        {"id": "lab3", "type": "unlock_lab_slot", "slot": 3}])
    assert rb.next_unlock_slot(cards_first, {2: {"status": "owned"}}) is None
    assert rb.next_unlock_slot(rb.legacy_gem_blocks(("unlock_lab_slot_2",)),
                               {2: {"status": "owned"}}) is None


def test_gem_automation_follows_the_rollout_stage() -> None:
    from lab_unlock_rollout import SlotRollout
    block = {"type": "unlock_lab_slot", "slot": 2}
    assert not rb.gem_automated(block)
    assert not rb.gem_automated(block, {2: SlotRollout()})
    assert rb.gem_automated(block, {2: SlotRollout(stage="canary", canary_worker="Air_1")})
    assert rb.gem_automated(block, {2: SlotRollout(stage="fleet")})
    assert not rb.gem_automated(block, {2: SlotRollout(stage="halted", halted_reason="x")})
    plan = evaluate_lab_plan(template_route(), LabFacts(
        now=1000., wallet_gems=160, slot_ownership={2: {"status": "locked"}},
        rollout={2: SlotRollout(stage="halted", halted_reason="x")}, worker="Air_38"))
    assert plan.gems.automated is False and "gems.lab2: Halted: x" in plan.gems.why

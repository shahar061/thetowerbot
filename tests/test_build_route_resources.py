"""Gem and lab path nodes distinguish automation from future plans."""

from __future__ import annotations

from dataclasses import replace

import pytest

import lab_catalog
from fleet.build_route import RouteDocument, resolve_route
from fleet.build_route_eval import RouteFacts, evaluate_resources
from fleet.resource_blocks import template_gem_blocks, template_lab_blocks
from lab_unlock_rollout import DryRun, SlotRollout


def _route(steps: list[str] | None = None):
    raw = RouteDocument.compatibility().to_dict()
    if steps is not None:
        raw["baseline"]["gems"]["steps"] = steps
    return resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")


def _facts() -> RouteFacts:
    return RouteFacts("a1", "Air_38", "main_menu", 100, 101,
                      wallet_coins=350, wallet_gems=99,
                      lab_slot2_owned=False, game_speed_maxed=False,
                      lab_decision_kind="start", lab_price=300)


def test_first_hundred_gems_waits_then_unlocks_second_lab() -> None:
    assert evaluate_resources(_route(), _facts()).gem_step.status == "blocked"
    at_hundred = evaluate_resources(_route(), replace(_facts(), wallet_gems=100))
    assert at_hundred.gem_step.status == "supported"
    assert at_hundred.gem_step.action == "unlock_lab_slot_2"


def test_gem_step_follows_the_rollout_stage() -> None:
    route, facts = _route(), replace(_facts(), wallet_gems=150)
    assert evaluate_resources(route, facts).gem_step.reason == "Rehearsing slot 2 · 0/2 dry runs"
    one = SlotRollout(dry_runs=(DryRun("Air_38", 1., 100, 150, "a1"),))
    assert evaluate_resources(route, facts, {2: one}).gem_step.reason == "Rehearsing slot 2 · 1/2 dry runs"
    held = SlotRollout(stage="canary", canary_worker="Air_38")
    step = evaluate_resources(route, facts, {2: held}).gem_step
    assert (step.status, step.reason) == ("supported", "Canary: Air_38 unlocks slot 2 next visit")
    other = evaluate_resources(route, facts, {2: replace(held, canary_worker="Air_39")}).gem_step
    assert (other.status, other.reason) == ("blocked", "Waiting for canary")
    fleet = evaluate_resources(route, facts, {2: SlotRollout(stage="fleet")}).gem_step
    assert (fleet.action, fleet.status, fleet.reason) == ("unlock_lab_slot_2", "supported", "Unlocking slot 2")
    halted = SlotRollout(stage="halted", halted_reason="price 120", evidence=("a.png",))
    step = evaluate_resources(route, facts, {2: halted}).gem_step
    assert (step.status, step.reason) == ("blocked", "Halted: price 120 · 1 evidence frame(s)")
    short = evaluate_resources(route, replace(facts, wallet_gems=60), {2: SlotRollout(stage="fleet")})
    assert short.gem_step.reason == "Save 40 more gems"


def test_owned_slots_wait_for_the_lab_starter() -> None:
    step = evaluate_resources(_route(), replace(_facts(), lab_slot_status={"2": "owned"})).gem_step
    assert (step.action, step.status, step.reason) == (
        "lab_slot_2_owned", "supported", "Slot 2 owned · waiting for lab starter")
    three = evaluate_resources(_route(["unlock_lab_slot_2", "unlock_lab_slot_3"]),
        replace(_facts(), wallet_gems=500, lab_slot_status={"2": "owned", "3": "locked"})).gem_step
    assert (three.action, three.reason) == ("unlock_lab_slot_3", "Rehearsing slot 3 · 0/2 dry runs")


def test_a_slot_without_a_catalog_price_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lab_catalog, "lab_slot_gems", lambda slot: None)
    step = evaluate_resources(_route(), replace(_facts(), wallet_gems=500)).gem_step
    assert (step.status, step.reason) == ("blocked", "Slot 2 price unknown")


def test_auto_unlock_off_is_only_planned() -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["rules"]["gems"]["auto_unlock_lab_slots"] = False
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    step = evaluate_resources(route, replace(_facts(), wallet_gems=150)).gem_step
    assert step.status == "planned"
    assert "auto-unlock off" in step.reason.lower()


def test_owned_second_lab_releases_reserve_and_future_card_step_is_planned() -> None:
    result = evaluate_resources(_route(["unlock_lab_slot_2", "cards"]),
        replace(_facts(), wallet_gems=25, lab_slot2_owned=True))
    assert result.gem_step.action == "cards"
    assert result.gem_step.status == "planned"
    assert "not automated" in result.gem_step.reason.lower()


def test_game_speed_uses_existing_lab_decision_and_stops_when_maxed() -> None:
    route = _route()
    assert evaluate_resources(route, _facts()).lab_step.status == "supported"
    waiting = evaluate_resources(route, replace(_facts(), lab_decision_kind="wait_coins",
                                                 wallet_coins=122))
    assert waiting.lab_step.status == "blocked"
    assert evaluate_resources(route, replace(_facts(), game_speed_maxed=True)).lab_step.action != "research_game_speed"


def test_unverified_resource_state_never_authorizes_spend() -> None:
    result = evaluate_resources(_route(), replace(_facts(), wallet_gems=None,
                                                   lab_decision_kind=None))
    assert result.gem_step.status == "unknown"
    assert result.lab_step.status == "unknown"


def test_blocks_mode_lanes_round_trip_and_steps_shape_still_works() -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["gems"].update(mode="blocks", blocks=list(template_gem_blocks()))
    raw["baseline"]["labs"].update(mode="blocks", blocks=list(template_lab_blocks()))
    route = RouteDocument.from_dict(raw)
    assert route.baseline.gems.blocks[0]["slot"] == 2
    assert RouteDocument.from_dict(route.to_dict()) == route
    assert RouteDocument.compatibility().baseline.gems.mode == "steps"


@pytest.mark.parametrize(("lane", "blocks", "message"), [
    ("gems", [{"id": "c", "type": "buy_cards", "purpose": "card_missions"}], "start by unlocking lab slot 2"),
    ("labs", [{"id": "t", "type": "slot_track", "slots": [1], "children": [{"id": "w", "type": "wait"}]}],
     "must start with Game Speed"),
])
def test_invalid_resource_blocks_are_rejected_on_save(lane: str, blocks: list, message: str) -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"][lane].update(mode="blocks", blocks=blocks)
    with pytest.raises(ValueError, match=message):
        RouteDocument.from_dict(raw)


def test_blocks_require_blocks_mode() -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["gems"]["blocks"] = list(template_gem_blocks())
    with pytest.raises(ValueError, match="blocks require blocks mode"):
        RouteDocument.from_dict(raw)


def test_blocks_mode_resource_evaluation_names_the_next_planned_block() -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["gems"].update(mode="blocks", blocks=list(template_gem_blocks()))
    raw["baseline"]["labs"].update(mode="blocks", blocks=list(template_lab_blocks()))
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    result = evaluate_resources(route, replace(_facts(), lab_slot2_owned=True, game_speed_maxed=True))
    # Slot 2 owned proves nothing about slot 3 under the rollout-based gem
    # lane (only an owned higher slot, or a locked lower one, proves a slot's
    # status), so with no lab_slot_status facts slot 3 is genuinely unknown.
    assert (result.gem_step.action, result.gem_step.status) == ("unlock_lab_slot_3", "unknown")
    assert (result.lab_step.action, result.lab_step.status) == ("research_labs.attack-speed", "planned")


def test_blocks_mode_lab_step_never_raises_when_no_track_owns_slot_1() -> None:
    """A slot-1 track is required at save time, but resource_evaluation must
    stay defensive: a labs.blocks tuple built any other way (e.g. an account
    override) with no slot-1 track must degrade to "no future plan", not
    raise StopIteration into the caller."""
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["gems"].update(mode="blocks", blocks=list(template_gem_blocks()))
    raw["baseline"]["labs"].update(mode="blocks", blocks=list(template_lab_blocks()))
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    no_slot_1 = replace(route, labs=replace(route.labs, blocks=(
        {"id": "labs.slot2", "type": "slot_track", "slots": [2], "children": []},
    )))
    result = evaluate_resources(no_slot_1, replace(_facts(), lab_slot2_owned=True, game_speed_maxed=True))
    assert (result.lab_step.action, result.lab_step.status) == ("game_speed_maxed", "supported")


def test_lab_list_lane_names_the_next_non_game_speed_entry() -> None:
    raw = RouteDocument.compatibility().to_dict()
    entries = [{"id": "gs", "lab_id": "labs.game-speed", "to_level": 7, "tier": "S+", "pin_slot": 1},
               {"id": "cw", "lab_id": "labs.coins-wave", "to_level": 10, "tier": "B"}]
    raw["baseline"]["labs"].update(mode="blocks", blocks=[{"id": "l", "type": "lab_list", "entries": entries}])
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    result = evaluate_resources(route, replace(_facts(), game_speed_maxed=True))
    assert (result.lab_step.action, result.lab_step.status) == ("research_labs.coins-wave", "planned")
    only_game_speed = replace(route, labs=replace(route.labs, blocks=(
        {"id": "l", "type": "lab_list", "entries": entries[:1]},)))
    result = evaluate_resources(only_game_speed, replace(_facts(), game_speed_maxed=True))
    assert (result.lab_step.action, result.lab_step.status) == ("game_speed_maxed", "supported")

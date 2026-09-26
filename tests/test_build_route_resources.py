"""Gem and lab path nodes distinguish automation from future plans."""

from __future__ import annotations

from dataclasses import replace

import pytest

from fleet.build_route import RouteDocument, resolve_route
from fleet.build_route_eval import RouteFacts, evaluate_resources
from fleet.resource_blocks import template_gem_blocks, template_lab_blocks


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
    assert (result.gem_step.action, result.gem_step.status) == ("unlock_lab_slot_3", "planned")
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

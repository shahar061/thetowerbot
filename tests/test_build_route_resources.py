"""Gem and lab path nodes distinguish automation from future plans."""

from __future__ import annotations

from dataclasses import replace

from fleet.build_route import RouteDocument, resolve_route
from fleet.build_route_eval import RouteFacts, evaluate_resources


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

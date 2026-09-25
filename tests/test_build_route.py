"""The route is a validated, account-bound policy document."""

from __future__ import annotations

from copy import deepcopy

import pytest

from fleet.build_route import RouteDocument, resolve_route


def _route() -> dict[str, object]:
    return RouteDocument.compatibility().to_dict()


def test_compatibility_route_round_trips_without_changing_current_spending() -> None:
    route = RouteDocument.compatibility()
    assert route.revision == 0
    assert route.baseline.workshop.mode == "legacy_planner"
    assert route.baseline.gems.lab_slot2_reserve == 100
    assert route.baseline.labs.slot1_research == "game_speed"
    assert RouteDocument.from_dict(route.to_dict()) == route


def test_unknown_workshop_id_is_rejected() -> None:
    raw = _route()
    raw["baseline"]["workshop"]["priority_ids"] = ["not_a_real_upgrade"]
    with pytest.raises(ValueError, match="unknown Workshop upgrade ID"):
        RouteDocument.from_dict(raw)


def test_duplicate_workshop_priority_is_rejected() -> None:
    raw = _route()
    raw["baseline"]["workshop"]["priority_ids"] = ["damage", "damage"]
    with pytest.raises(ValueError, match="duplicate Workshop priority"):
        RouteDocument.from_dict(raw)


@pytest.mark.parametrize("value", [-1, 101, True, 30.5])
def test_out_of_range_or_non_integer_budget_is_rejected(value: object) -> None:
    raw = _route()
    raw["baseline"]["workshop"]["coin_spend_limit_pct"] = value
    with pytest.raises(ValueError, match="coin_spend_limit_pct"):
        RouteDocument.from_dict(raw)


def test_zero_weight_is_rejected() -> None:
    raw = _route()
    raw["baseline"]["workshop"]["weights"] = {"damage": 0}
    with pytest.raises(ValueError, match="positive integer"):
        RouteDocument.from_dict(raw)


def test_overlapping_inclusive_wave_phases_are_rejected() -> None:
    raw = _route()
    raw["baseline"]["battle"]["branches"][0]["phases"] = [
        {"id": "early", "start_wave": 1, "end_wave": 10, "priority_ids": ["cash_per_wave"]},
        {"id": "late", "start_wave": 10, "end_wave": 20, "priority_ids": ["damage"]},
    ]
    with pytest.raises(ValueError, match="overlapping wave phases"):
        RouteDocument.from_dict(raw)


def test_rule_ids_are_unique_across_workshop_branches_and_phases() -> None:
    raw = _route()
    raw["baseline"]["battle"]["branches"][0]["phases"][0]["id"] = "workshop.default"
    with pytest.raises(ValueError, match="duplicate route rule ID"):
        RouteDocument.from_dict(raw)


def test_cyclic_rule_dependency_is_rejected() -> None:
    raw = _route()
    raw["dependencies"] = {"workshop.default": ["battle.opening"],
                           "battle.opening": ["workshop.default"]}
    with pytest.raises(ValueError, match="cyclic route dependency"):
        RouteDocument.from_dict(raw)


def test_old_account_override_is_inactive_for_replacement_account() -> None:
    raw = _route()
    raw["overrides"] = {"Air_38": {"account_id": "old", "patches": {
        "workshop.default": {"coin_spend_limit_pct": 30}}}}
    route = RouteDocument.from_dict(raw)
    assert resolve_route(route, "Air_38", "old").workshop.coin_spend_limit_pct == 30
    replacement = resolve_route(route, "Air_38", "new")
    assert replacement.override_state == "inactive_account_changed"
    assert replacement.workshop.coin_spend_limit_pct == 100
    assert resolve_route(route, "Air_39", "other").override_state == "none"


def test_unknown_keys_are_rejected() -> None:
    raw = deepcopy(_route())
    raw["baseline"]["workshop"]["surprise"] = True
    with pytest.raises(ValueError, match="unknown route field"):
        RouteDocument.from_dict(raw)

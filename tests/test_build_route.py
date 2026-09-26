"""The route is a validated, account-bound policy document."""

from __future__ import annotations

from copy import deepcopy

import pytest

from fleet.build_route import RouteDocument, RouteRules, resolve_route
from fleet.resource_blocks import template_lab_blocks


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


def test_a_route_without_rules_gets_todays_defaults() -> None:
    route = RouteDocument.from_dict(_route())
    assert route.baseline.rules == RouteRules()
    rules = route.to_dict()["baseline"]["rules"]
    assert rules["coins"] == {"lab_share": {"mode": "when_affordable", "pct": 25},
                              "workshop_spend_limit_pct": 100}
    assert rules["labs"]["auto_start"] is True and rules["labs"]["idle_fill"] == "leave_idle"
    assert rules["gems"] == {"auto_unlock_lab_slots": True, "spend_limit_pct": 100, "keep": 0}


def test_moved_limits_fill_the_rules_when_rules_are_absent_and_are_dual_written() -> None:
    raw = _route()
    raw["baseline"]["workshop"]["coin_spend_limit_pct"] = 30
    raw["baseline"]["gems"]["spend_limit_pct"] = 40
    del raw["baseline"]["rules"]
    route = RouteDocument.from_dict(raw)
    assert route.baseline.rules.coins.workshop_spend_limit_pct == 30
    assert route.baseline.rules.gems.spend_limit_pct == 40
    saved = route.to_dict()["baseline"]
    assert saved["workshop"]["coin_spend_limit_pct"] == 30
    assert saved["rules"]["coins"]["workshop_spend_limit_pct"] == 30
    assert saved["gems"]["spend_limit_pct"] == 40 and saved["rules"]["gems"]["spend_limit_pct"] == 40


def test_legacy_limit_edit_wins_over_a_stale_rule() -> None:
    raw = RouteDocument.from_dict(_route()).to_dict()   # carries rules at 100
    raw["baseline"]["workshop"]["coin_spend_limit_pct"] = 30  # a pre-rules client edit
    route = RouteDocument.from_dict(raw)
    assert route.baseline.workshop.coin_spend_limit_pct == 30
    assert route.baseline.rules.coins.workshop_spend_limit_pct == 30
    only_rules = RouteDocument.from_dict(_route()).to_dict()
    del only_rules["baseline"]["workshop"]["coin_spend_limit_pct"]
    only_rules["baseline"]["rules"]["coins"]["workshop_spend_limit_pct"] = 55
    assert RouteDocument.from_dict(only_rules).baseline.workshop.coin_spend_limit_pct == 55


def test_dual_written_limits_load_consistently() -> None:
    raw = RouteDocument.from_dict(_route()).to_dict()
    raw["baseline"]["workshop"]["coin_spend_limit_pct"] = 40
    raw["baseline"]["rules"]["coins"]["workshop_spend_limit_pct"] = 40
    raw["baseline"]["gems"]["spend_limit_pct"] = 60
    raw["baseline"]["rules"]["gems"]["spend_limit_pct"] = 60
    route = RouteDocument.from_dict(raw)
    assert route.baseline.rules.coins.workshop_spend_limit_pct == 40
    assert route.baseline.rules.gems.spend_limit_pct == 60
    assert route.baseline.workshop.coin_spend_limit_pct == 40
    assert route.baseline.gems.spend_limit_pct == 60


def test_override_coin_limit_maps_to_the_rule() -> None:
    raw = _route()
    raw["overrides"] = {"Air_38": {"account_id": "a1", "patches": {
        "workshop.default": {"coin_spend_limit_pct": 30}}}}
    effective = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    assert effective.workshop.coin_spend_limit_pct == 30
    assert effective.rules.coins.workshop_spend_limit_pct == 30
    assert resolve_route(RouteDocument.from_dict(raw), "Air_38", "other").rules == RouteRules()


@pytest.mark.parametrize(("path", "value", "message"), [
    (("coins", "lab_share", "mode"), "rush", "unknown lab_share mode"),
    (("coins", "lab_share", "pct"), 95, "lab_share pct"),
    (("labs", "auto_start"), "yes", "auto_start must be boolean"),
    (("labs", "idle_fill"), "fill_everything", "unknown idle_fill"),
    (("labs", "pool", "selection"), "random", "unknown pool selection"),
    (("labs", "pool", "max_seconds"), 5, "max_seconds"),
    (("gems", "keep"), -1, "keep"),
    (("gems", "surprise"), 1, "unknown route field"),
])
def test_invalid_rules_are_rejected(path: tuple[str, ...], value: object, message: str) -> None:
    raw = RouteDocument.from_dict(_route()).to_dict()
    target = raw["baseline"]["rules"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match=message):
        RouteDocument.from_dict(raw)


def test_lab_pool_blocks_may_only_tighten_the_pool_rule() -> None:
    raw = RouteDocument.from_dict(_route()).to_dict()
    raw["baseline"]["labs"].update(mode="blocks", blocks=list(template_lab_blocks()))
    raw["baseline"]["rules"]["labs"]["pool"]["max_seconds"] = 3600
    assert RouteDocument.from_dict(raw).baseline.labs.mode == "blocks"  # 1800 <= 3600
    raw["baseline"]["rules"]["labs"]["pool"]["max_seconds"] = 600
    with pytest.raises(ValueError, match="looser than the strategy rule"):
        RouteDocument.from_dict(raw)

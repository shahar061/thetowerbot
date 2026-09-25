"""Account overrides inherit the fleet route and stop on identity change."""

from __future__ import annotations

import pytest

from fleet.build_route import RouteDocument, resolve_route


def test_worker_patch_changes_only_matching_account() -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["workshop"].update({"mode": "priorities", "priority_ids": ["damage"]})
    raw["overrides"] = {"Air_38": {"account_id": "old", "patches": {
        "workshop.default": {"priority_ids": ["cash_per_wave", "damage"]}}}}
    route = RouteDocument.from_dict(raw)
    assert resolve_route(route, "Air_38", "old").workshop.priority_ids[0] == "cash_per_wave"
    assert resolve_route(route, "Air_38", "new").override_state == "inactive_account_changed"
    assert resolve_route(route, "Air_38", "new").workshop.priority_ids[0] == "damage"
    assert resolve_route(route, "Air_39", "other").workshop.priority_ids == route.baseline.workshop.priority_ids


def test_battle_phase_patch_affects_one_account_only() -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["battle"] = {"mode": "phases", "branches": [
        {"id": "fallback", "min_best_tier_1_wave": None, "phases": [
            {"id": "opening", "start_wave": 1, "end_wave": None,
             "priority_ids": ["defense_absolute"]}]}]}
    raw["overrides"] = {"Air_38": {"account_id": "a1", "patches": {
        "opening": {"priority_ids": ["cash_per_wave", "defense_absolute"],
                    "cash_spend_limit_pct": 30}}}}
    route = RouteDocument.from_dict(raw)
    one = resolve_route(route, "Air_38", "a1")
    other = resolve_route(route, "Air_39", "a2")
    assert one.battle.branches[0].phases[0].priority_ids[0] == "cash_per_wave"
    assert one.battle.branches[0].phases[0].cash_spend_limit_pct == 30
    assert other.battle.branches[0].phases[0].priority_ids == ("defense_absolute",)


def test_unknown_patch_rule_or_field_is_rejected() -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["overrides"] = {"Air_38": {"account_id": "a1", "patches": {
        "gone": {"priority_ids": ["damage"]}}}}
    with pytest.raises(ValueError, match="unknown override rule"):
        RouteDocument.from_dict(raw)
    raw["overrides"]["Air_38"]["patches"] = {"workshop.default": {"made_up": 1}}
    with pytest.raises(ValueError, match="unknown route field"):
        RouteDocument.from_dict(raw)

from __future__ import annotations

import pytest

from fleet.build_route import DEFAULT_WINDOW_HOURS, RouteBaseline, RouteDocument, RouteRules

LIST = [{"id": "labs.list", "type": "lab_list", "entries": [
    {"id": "gs", "lab_id": "labs.game-speed", "to_level": 7, "tier": "S+", "pin_slot": 1}]}]


def _baseline(rules: dict, labs: dict | None = None) -> dict:
    raw = RouteDocument.compatibility().to_dict()["baseline"]
    raw["rules"] = rules
    if labs is not None:
        raw["labs"] = labs
    return raw


def test_old_rules_load_with_defaults() -> None:
    rules = RouteRules.from_dict({"coins": {"lab_share": {"mode": "save_pct", "pct": 25}}})
    assert rules.labs.saving.income_margin_pct == 75
    assert rules.labs.saving.window_hours == DEFAULT_WINDOW_HOURS
    assert (rules.labs.filler.enabled, rules.labs.filler.max_price_pct_of_wallet,
            rules.labs.filler.min_hours) == (True, 10, 1.0)
    assert rules.labs.native_repeat == "unchanged"


@pytest.mark.parametrize("mode", ["unchanged", "enabled", "disabled"])
def test_native_repeat_round_trips_without_changing_bot_auto_start(mode: str) -> None:
    rules = RouteRules.from_dict({"labs": {"native_repeat": mode, "auto_start": False}})
    assert rules.labs.native_repeat == mode
    restored = RouteRules.from_dict(rules.to_dict())
    assert restored.labs.native_repeat == mode
    assert restored.labs.auto_start is False


@pytest.mark.parametrize("mode", ["on", "", None, True, 1, [], {}])
def test_native_repeat_rejects_unsupported_modes(mode: object) -> None:
    with pytest.raises(ValueError, match="native_repeat"):
        RouteRules.from_dict({"labs": {"native_repeat": mode}})


def test_saving_and_filler_round_trip() -> None:
    raw = {"coins": {"lab_share": {"mode": "just_in_time", "pct": 25}},
           "labs": {"saving": {"income_margin_pct": 80, "window_hours": {"S+": 96, "S": 24, "A": 12, "B": 4, "C": 0}},
                    "filler": {"enabled": False, "max_price_pct_of_wallet": 5, "min_hours": 0.5}}}
    rules = RouteRules.from_dict(raw)
    assert RouteRules.from_dict(rules.to_dict()) == rules
    assert rules.labs.saving.window_hours["S+"] == 96 and rules.labs.filler.min_hours == 0.5


@pytest.mark.parametrize("labs", [
    {"saving": {"income_margin_pct": 40}},
    {"saving": {"window_hours": {"S+": 72}}},
    {"saving": {"window_hours": {"S+": 200, "S": 24, "A": 12, "B": 4, "C": 0}}},
    {"filler": {"max_price_pct_of_wallet": 0}},
    {"filler": {"min_hours": 0.1}},
    {"filler": {"enabled": "yes"}},
])
def test_rejects_out_of_range_saving_and_filler(labs: dict) -> None:
    with pytest.raises(ValueError):
        RouteRules.from_dict({"labs": labs})


def test_just_in_time_requires_lab_list() -> None:
    with pytest.raises(ValueError, match="ranked lab list"):
        RouteBaseline.from_dict(_baseline({"coins": {"lab_share": {"mode": "just_in_time", "pct": 25}}}))


def test_just_in_time_with_lab_list_loads() -> None:
    labs = {"slot1_research": "game_speed", "steps": ["research_game_speed"], "mode": "blocks", "blocks": LIST}
    baseline = RouteBaseline.from_dict(_baseline({"coins": {"lab_share": {"mode": "just_in_time", "pct": 25}}}, labs))
    assert baseline.rules.coins.lab_share.mode == "just_in_time"

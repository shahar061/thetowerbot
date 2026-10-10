from __future__ import annotations

import pytest

from policy import PolicyError, choose


@pytest.mark.parametrize("uid", ["coins_per_wave", "coins_per_kill_bonus", "unlock_cash_bonuses"])
def test_forbidden_tournament_rules(uid: str) -> None:
    from tournament_policy import TournamentConfig
    with pytest.raises(PolicyError):
        TournamentConfig.from_dict({"rules": [{"upgrade_id": uid}]})


def test_cash_opening_is_capped_and_growth_rotates() -> None:
    from tournament_policy import TournamentConfig, PurchaseCursor, resolve_policy, advance_purchase
    cfg = TournamentConfig.from_dict({
        "opening_cash": {"cash_bonus_target": 2, "cash_per_wave_target": 10,
                         "cash_budget": 15, "until_wave": 20},
        "rules": [{"upgrade_id": "health"}, {"upgrade_id": "damage"}],
    })
    rows = {uid: {"value": 1, "price": 10, "status": "available"}
            for uid in ("cash_bonus", "cash_per_wave", "health", "damage", "coins_per_wave")}
    combat = {"cash": 100, "wave": 1}
    cursor = PurchaseCursor()
    assert choose(resolve_policy(cfg, cursor, rows, combat), rows, combat).upgrade_id == "cash_bonus"
    cursor = advance_purchase(cursor, "cash_bonus", 10, cfg.rules)
    # Only $5 remains in the opening budget; a $10 cash row cannot be bought.
    assert choose(resolve_policy(cfg, cursor, rows, combat), rows, combat).upgrade_id == "health"
    cursor = advance_purchase(cursor, "health", 10, cfg.rules)
    assert choose(resolve_policy(cfg, cursor, rows, combat), rows, combat).upgrade_id == "damage"
    assert "coins_per_wave" not in [r.upgrade_id for r in resolve_policy(cfg, cursor, rows, combat).rules]


def test_partial_opening_and_unknown_wave_skip_cash() -> None:
    from tournament_policy import TournamentConfig, PurchaseCursor, resolve_policy
    cfg = TournamentConfig.from_dict({"opening_cash": {"cash_budget": 100}})
    rows = {uid: {"value": 1, "price": 10, "status": "available"}
            for uid in ("cash_bonus", "health")}
    assert choose(resolve_policy(cfg, PurchaseCursor(), rows, {"cash": 100}), rows,
                  {"cash": 100}).upgrade_id == "health"


@pytest.mark.parametrize("value", [True, -1, float("inf"), "10"])
def test_invalid_cash_limit_rejected(value: object) -> None:
    from tournament_policy import TournamentConfig
    with pytest.raises(PolicyError):
        TournamentConfig.from_dict({"opening_cash": {"cash_budget": value}})


def test_main_and_fleet_configuration_roundtrip() -> None:
    from strategy import Strategy
    from fleet.build_route import RouteBaseline
    raw = Strategy.from_config().to_dict()
    raw["tournament"] = {"enabled": True, "public_name": "TowerPlayer"}
    saved = Strategy.from_dict(raw)
    assert saved.to_dict()["tournament"]["public_name"] == "TowerPlayer"
    assert saved.merged({"tournament": {"enabled": False}}).tournament.enabled is False
    baseline = RouteBaseline.from_dict({"tournament": raw["tournament"]})
    assert baseline.to_dict()["tournament"]["public_name"] == "TowerPlayer"


def test_purchase_receipt_changes_authorization_before_next_action() -> None:
    from tournament_policy import TournamentConfig, PurchaseCursor, resolve_policy, advance_purchase
    cfg = TournamentConfig()
    before = resolve_policy(cfg, PurchaseCursor(), {}, {"cash": 100, "wave": 1})
    cursor = advance_purchase(PurchaseCursor(), "health", 10, cfg.rules)
    after = resolve_policy(cfg, cursor, {}, {"cash": 90, "wave": 1})
    assert before.single_purchase
    assert before.decision_token != after.decision_token

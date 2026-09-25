"""Never Buy is a hard exclusion, and spend limits use wallet share."""

from __future__ import annotations

from dataclasses import replace

import upgrades
from fleet.build_route import RouteDocument, resolve_route
from fleet.build_route_eval import RouteFacts, evaluate_workshop
from fleet.reroll_planner import RerollFacts, choose_next, project_next


def _route(*, banned: list[str] | None = None, priorities: list[str] | None = None,
           pct: int = 100) -> RouteDocument:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["workshop"].update({
        "mode": "priorities", "banned_upgrade_ids": banned or [],
        "priority_ids": priorities or ["damage"], "coin_spend_limit_pct": pct,
    })
    return RouteDocument.from_dict(raw)


def _facts() -> RouteFacts:
    return RouteFacts("a1", "Air_38", "workshop", 100, 101, 1,
                      wallet_coins=140, prices={"damage": 30,
                                                "attack_speed": 12,
                                                "defense_absolute": 8},
                      utility_spent_coins=0)


def test_ban_skips_survival_starter_and_projection() -> None:
    facts = RerollFacts("a1", 1, {}, {}, 140, None,
                        {"damage": 10, "attack_speed": 12},
                        utility_spent_coins=0, draw_sharpness=None)
    banned = frozenset({"damage"})
    assert choose_next(facts, banned_upgrade_ids=banned).upgrade_id == "attack_speed"
    assert all(step.upgrade_id != "damage" for step in
               project_next(facts, banned_upgrade_ids=banned))


def test_ban_skips_cheap_defense_filler() -> None:
    facts = RerollFacts("a1", 25, {"unlock_defense_upgrades": 1, "unlock_thorns": 1,
                                   "thorns": 7, "defense_absolute": 5},
                        {"thorns": 34.}, 100, None,
                        {"thorns": 409, "defense_absolute": 8},
                        utility_spent_coins=None, draw_sharpness=None)
    decision = choose_next(facts, banned_upgrade_ids=frozenset({"defense_absolute"}))
    assert decision.upgrade_id != "defense_absolute"


def test_banning_every_workshop_id_returns_a_reasoned_wait() -> None:
    banned = frozenset(upgrade.id for upgrade in upgrades.CATALOG)
    facts = RerollFacts("a1", 1, {}, {}, 140, None, {"damage": 10},
                        utility_spent_coins=0, draw_sharpness=None)
    decision = choose_next(facts, banned_upgrade_ids=banned)
    assert decision.upgrade_id is None
    assert "Never Buy" in decision.reason


def test_thirty_percent_limit_is_not_weighted_luck() -> None:
    route = resolve_route(_route(priorities=["damage", "attack_speed"], pct=30), "Air_38", "a1")
    evaluation = evaluate_workshop(route, _facts(), None)
    assert evaluation.trace.spend_ceiling == 42
    assert evaluation.decision is not None
    assert evaluation.decision.upgrade_id == "damage"
    assert evaluation.trace.draw_gate is None


def test_spend_limit_rejects_costly_starter_and_uses_cheaper_candidate() -> None:
    route = resolve_route(_route(priorities=["damage", "attack_speed"], pct=30), "Air_38", "a1")
    facts = replace(_facts(), prices={"damage": 50, "attack_speed": 12})
    evaluation = evaluate_workshop(route, facts, None)
    assert evaluation.decision is not None
    assert evaluation.decision.upgrade_id == "attack_speed"
    assert evaluation.decision.price == 12


def test_unknown_wallet_cannot_authorize_percentage_spend() -> None:
    route = resolve_route(_route(pct=30), "Air_38", "a1")
    evaluation = evaluate_workshop(route, replace(_facts(), wallet_coins=None), None)
    assert evaluation.status == "unknown"
    assert evaluation.decision is None


def test_banned_unlock_blocks_its_child() -> None:
    route = resolve_route(_route(banned=["unlock_defense_upgrades"],
                                 priorities=["defense_absolute", "damage"]), "Air_38", "a1")
    evaluation = evaluate_workshop(route, _facts(), None)
    assert evaluation.decision is not None
    assert evaluation.decision.upgrade_id != "defense_absolute"
    assert any("blocked by Never Buy" in reason for reason in evaluation.trace.rejected)

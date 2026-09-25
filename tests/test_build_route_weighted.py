"""Weighted luck is a separate gate with stable retry semantics."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import db
import upgrades
from fleet.build_route import RouteDocument, resolve_route
from fleet.build_route_eval import RouteFacts, evaluate_workshop
from fleet.build_route_runtime import BuildRouteRuntime


def _route(chance: int) -> object:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["workshop"].update({
        "mode": "priorities", "priority_ids": ["damage", "attack_speed", "health"],
        "coin_spend_limit_pct": 30, "draw_chance_pct": chance,
        "weights": {"damage": 6, "attack_speed": 3, "health": 1},
    })
    return resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")


def _facts() -> RouteFacts:
    return RouteFacts("a1", "Air_38", "workshop", 100, 101, 1,
                      wallet_coins=140, prices={"damage": 10, "attack_speed": 10,
                                                "health": 10}, utility_spent_coins=0,
                      visit_id="visit-1")


def test_zero_percent_luck_uses_top_ranked_candidate() -> None:
    result = evaluate_workshop(_route(0), _facts(), None)
    assert result.decision is not None
    assert result.decision.upgrade_id == "damage"
    assert result.trace.draw_gate is None


def test_full_luck_displays_eligible_odds_after_budget_filtering() -> None:
    result = evaluate_workshop(_route(100), _facts(), None)
    assert result.trace.eligible_odds == {"damage": .6, "attack_speed": .3, "health": .1}
    assert result.trace.spend_ceiling == 42
    assert result.trace.draw_gate is not None
    assert result.pending is not None


def test_weighted_candidates_exclude_descendants_of_banned_unlocks() -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["workshop"].update({
        "mode": "priorities", "priority_ids": ["defense_absolute", "damage"],
        "banned_upgrade_ids": ["unlock_defense_upgrades"],
        "draw_chance_pct": 100,
    })
    route = resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")
    facts = replace(_facts(), purchases={"unlock_defense_upgrades": 1},
                    prices={"defense_absolute": 5, "damage": 10})
    result = evaluate_workshop(route, facts, None)
    assert result.decision is not None
    assert result.decision.upgrade_id == "damage"
    assert "defense_absolute" not in result.trace.eligible_odds


def test_rescan_keeps_pending_selection_even_when_wallet_changes() -> None:
    route = _route(100)
    first = evaluate_workshop(route, _facts(), None)
    assert first.pending is not None
    repeated = evaluate_workshop(route, replace(_facts(), wallet_coins=100), first.pending)
    assert repeated.decision is not None and first.decision is not None
    assert repeated.decision.upgrade_id == first.decision.upgrade_id
    assert repeated.pending == first.pending


def test_pending_selection_is_invalidated_by_new_account_or_revision() -> None:
    route = _route(100)
    first = evaluate_workshop(route, _facts(), None)
    assert first.pending is not None
    new_account = replace(_facts(), account_id="a2")
    result = evaluate_workshop(resolve_route(RouteDocument.compatibility(), "Air_38", "a2"),
                               new_account, first.pending)
    assert result.pending != first.pending


def test_confirmed_purchase_advances_sequence_but_rescan_does_not(tmp_path: Path) -> None:
    worker = tmp_path / "workers" / "Air_38"
    worker.mkdir(parents=True)
    db.bind_account(worker / "tower_bot.db", "a1")
    runtime = BuildRouteRuntime(tmp_path, "Air_38", "a1")
    first = evaluate_workshop(_route(100), _facts(), None)
    assert first.pending is not None
    runtime.remember_pending(first.pending)
    assert runtime.pending() == first.pending
    selected = upgrades.by_id(first.pending.chosen_id)
    assert selected is not None
    with db.connect(worker / "tower_bot.db") as connection:
        cursor = connection.execute(
            "INSERT INTO ledger(ts,kind,item,category,currency,delta,dry_run,detail) "
            "VALUES(1,'WORKSHOP_BUY',?,?,'coins',-10,0,?)",
            (selected.name, selected.category, json.dumps({"verdict": "bought"})))
        event_id = cursor.lastrowid
    runtime.confirm_purchase(event_id, first.pending.chosen_id)
    assert runtime.pending() is None
    assert runtime.sequence() == 1
    with db.reader(worker / "tower_bot.db") as connection:
        audit = connection.execute("SELECT detail FROM ledger WHERE kind='ROUTE_DECISION'").fetchone()
    assert audit is not None
    detail = json.loads(audit["detail"])
    assert detail["route_revision"] == 0
    assert detail["purchase_event_id"] == event_id
    assert detail["eligible_weights"] == {"damage": 6, "attack_speed": 3, "health": 1}
    assert detail["selected_upgrade_id"] == first.pending.chosen_id

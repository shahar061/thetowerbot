"""Conditional battle phases have explicit evidence and budget gates."""

from __future__ import annotations

from dataclasses import replace

from fleet.build_route import RouteDocument, resolve_route
from fleet.build_route_eval import RouteFacts, evaluate_battle
from policy import AutopilotPolicy, UpgradeRule, choose


def _route() -> RouteDocument:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["battle"] = {"mode": "phases", "branches": [
        {"id": "opening", "min_best_tier_1_wave": None, "phases": [
            {"id": "fallback", "start_wave": 1, "end_wave": None,
             "priority_ids": ["defense_absolute"], "cash_spend_limit_pct": 100}]},
        {"id": "economy_50", "min_best_tier_1_wave": 50, "phases": [
            {"id": "first_10", "start_wave": 1, "end_wave": 10,
             "priority_ids": ["cash_per_wave", "defense_absolute"],
             "cash_spend_limit_pct": 30, "emergency_survival": False},
            {"id": "after_10", "start_wave": 11, "end_wave": None,
             "priority_ids": ["defense_absolute"], "cash_spend_limit_pct": 100}]},
    ]}
    return RouteDocument.from_dict(raw)


def _facts() -> RouteFacts:
    return RouteFacts("a1", "Air_38", "battle", 100, 101,
                      best_tier_1_wave=50, run_id=8, wave=7, battle_cash=100,
                      upgrade_rows={"cash_per_wave": {"status": "available", "value": 1,
                                                       "price": 20, "observed_at": 100},
                                    "defense_absolute": {"status": "available", "value": 2,
                                                         "price": 8, "observed_at": 100}})


def test_best_wave_gate_and_inclusive_phase_boundaries() -> None:
    route = resolve_route(_route(), "Air_38", "a1")
    assert evaluate_battle(route, replace(_facts(), best_tier_1_wave=49), None).trace.branch_id == "opening"
    assert evaluate_battle(route, _facts(), None).trace.phase_id == "first_10"
    assert evaluate_battle(route, replace(_facts(), wave=10), None).trace.phase_id == "first_10"
    assert evaluate_battle(route, replace(_facts(), wave=11), None).trace.phase_id == "after_10"
    assert evaluate_battle(route, replace(_facts(), best_tier_1_wave=None), None).trace.branch_id == "opening"


def test_cash_budget_and_missing_evidence_never_authorize_spend() -> None:
    route = resolve_route(_route(), "Air_38", "a1")
    result = evaluate_battle(route, _facts(), None)
    assert result.trace.spend_ceiling == 30
    assert result.decision is not None and result.decision.upgrade_id == "cash_per_wave"
    expensive = replace(_facts(), upgrade_rows={**_facts().upgrade_rows,
        "cash_per_wave": {"status": "available", "value": 1, "price": 31, "observed_at": 100}})
    assert evaluate_battle(route, expensive, None).decision.upgrade_id == "defense_absolute"
    for missing in (replace(_facts(), wave=None), replace(_facts(), battle_cash=None),
                    replace(_facts(), run_id=None)):
        assert evaluate_battle(route, missing, None).status == "unknown"


def test_stale_row_does_not_enter_candidate_pool() -> None:
    route = resolve_route(_route(), "Air_38", "a1")
    stale = replace(_facts(), upgrade_rows={**_facts().upgrade_rows,
        "cash_per_wave": {"status": "available", "value": 1, "price": 20, "observed_at": 30}})
    result = evaluate_battle(route, stale, None)
    assert result.decision is not None and result.decision.upgrade_id == "defense_absolute"


def test_policy_percentage_uses_current_cash_not_a_fixed_reserve() -> None:
    policy = AutopilotPolicy(enabled=True, preset="manual",
        rules=(UpgradeRule("cash_per_wave"), UpgradeRule("defense_absolute")),
        cash_spend_limit_pct=30)
    rows = {"cash_per_wave": {"status": "available", "value": 1, "price": 31},
            "defense_absolute": {"status": "available", "value": 2, "price": 8}}
    assert choose(policy, rows, {"cash": 100}).upgrade_id == "defense_absolute"
    assert choose(policy, rows, {"cash": 110}).upgrade_id == "cash_per_wave"

"""A preview and a worker must explain the same verified decision."""

from __future__ import annotations

from dataclasses import replace

from fleet.build_route import RouteDocument, resolve_route
from fleet.build_route_eval import RouteFacts, evaluate
from fleet.reroll_planner import RerollFacts, choose_next


def _facts() -> RouteFacts:
    return RouteFacts(
        account_id="a1", worker="Air_38", screen="workshop", observed_at=100,
        now=101, best_tier_1_wave=2, wallet_coins=120, prices={"damage": 10},
        purchases={}, values={},
    )


def test_compatibility_route_uses_existing_planner_choice() -> None:
    facts = _facts()
    route = resolve_route(RouteDocument.compatibility(), "Air_38", "a1")
    evaluation = evaluate(route, facts, None)
    legacy = choose_next(RerollFacts("a1", 2, {}, {}, 120, None, {"damage": 10}))
    assert evaluation.decision is not None
    assert evaluation.decision.upgrade_id == legacy.upgrade_id
    assert evaluation.revision == 0
    assert evaluation.trace.matched_rule_id == "workshop.default"
    assert evaluation.trace.price_source == "worker price evidence"


def test_missing_wallet_does_not_recommend_a_purchase() -> None:
    route = resolve_route(RouteDocument.compatibility(), "Air_38", "a1")
    evaluation = evaluate(route, replace(_facts(), wallet_coins=None), None)
    assert evaluation.status == "unknown"
    assert evaluation.decision is None
    assert "wallet" in evaluation.trace.reason.lower()


def test_stale_evidence_does_not_recommend_a_purchase() -> None:
    route = resolve_route(RouteDocument.compatibility(), "Air_38", "a1")
    evaluation = evaluate(route, replace(_facts(), now=500), None)
    assert evaluation.status == "unknown"
    assert evaluation.decision is None
    assert evaluation.trace.evidence_age_seconds == 400


def test_wave_sixty_remains_operator_decision() -> None:
    route = resolve_route(RouteDocument.compatibility(), "Air_38", "a1")
    evaluation = evaluate(route, replace(_facts(), best_tier_1_wave=60), None)
    assert evaluation.status == "blocked"
    assert evaluation.decision is not None
    assert evaluation.decision.state == "needs_operator"


def test_preview_from_main_menu_is_projected_not_observed_workshop_price() -> None:
    route = resolve_route(RouteDocument.compatibility(), "Air_38", "a1")
    evaluation = evaluate(route, replace(_facts(), screen="main_menu"), None)
    assert evaluation.status == "projected"
    assert evaluation.trace.price_source == "worker price evidence"


def test_variant_is_included_in_trace() -> None:
    route = resolve_route(RouteDocument.compatibility(), "Air_38", "a1")
    evaluation = evaluate(route, replace(_facts(), variant="income_first"), None)
    assert evaluation.trace.variant == "income_first"
